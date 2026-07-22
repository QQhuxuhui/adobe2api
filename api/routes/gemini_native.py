from __future__ import annotations

import base64
import binascii
import json
import math
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.models.gemini_usage import (
    CANNED_TEXT,
    build_canned_usage_metadata,
    build_count_tokens_response,
    build_image_usage_metadata,
)
from core.models.catalog import (
    GEMINI_FLASH_FIXED_RATIOS,
    GEMINI_PRO_FIXED_RATIOS,
)
from core.models.resolver import resolve_requested_aspect_ratio
from core.video_tasks import (
    VideoTaskCapacityError,
    VideoTaskSpec,
    VideoTaskStorageError,
)

GEMINI_NATIVE_MAX_BODY_BYTES = 64 * 1024 * 1024
GEMINI_VIDEO_MAX_BODY_BYTES = 1024 * 1024
GEMINI_MAX_IMAGE_BYTES = 20 * 1024 * 1024
GEMINI_MAX_TOTAL_IMAGE_BYTES = 40 * 1024 * 1024
GEMINI_MAX_IMAGES = 6
GEMINI_MAX_ENCODED_IMAGE_CHARS = 4 * math.ceil(GEMINI_MAX_IMAGE_BYTES / 3)
GEMINI_MAX_IMAGE_MIB = GEMINI_MAX_IMAGE_BYTES // (1024 * 1024)

# Gemini 3 Pro Image(Nano Banana Pro)官方支持的 10 个比例。
PRO_RATIOS = frozenset(
    {"1:1", "16:9", "9:16", "4:3", "3:4", "2:3", "3:2", "4:5", "5:4", "21:9"}
)
# flash(Nano Banana 2)额外支持超长比例。
FLASH_RATIOS = frozenset({*PRO_RATIOS, "1:8", "1:4", "4:1", "8:1"})
TEST_TEXT_MODELS = frozenset(
    {
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-3-pro-preview",
        "gemini-3.1-pro-preview",
    }
)
GEMINI_CONTENT_ACTIONS = frozenset(
    {"generateContent", "streamGenerateContent", "countTokens"}
)
GEMINI_ACTION_ORDER = (
    "generateContent",
    "streamGenerateContent",
    "countTokens",
    "predictLongRunning",
)


@dataclass(frozen=True)
class GeminiModelSpec:
    model_id: str
    display_name: str
    family: str
    upstream_model_id: str | None
    upstream_model_version: str | None
    aspect_ratios: frozenset[str]
    supported_actions: frozenset[str] = GEMINI_CONTENT_ACTIONS
    video_engine: str | None = None
    video_upstream_model: str | None = None


@dataclass(frozen=True)
class ParsedGeminiRequest:
    prompt: str
    images: Sequence[tuple[bytes, str]]
    aspect_ratio: str
    image_size: str
    candidate_count: int


@dataclass(frozen=True)
class ParsedVeoRequest:
    prompt: str
    aspect_ratio: str
    duration: int
    resolution: str
    negative_prompt: str


class GeminiNativeError(Exception):
    def __init__(self, code: int, message: str, status: str):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.status = str(status)


def _nonempty_parameter(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _image_model(
    model_id: str,
    *,
    family: str,
    upstream_model_version: str,
    aspect_ratios: frozenset[str],
) -> GeminiModelSpec:
    return GeminiModelSpec(
        model_id=model_id,
        display_name=model_id,
        family=family,
        upstream_model_id="gemini-flash",
        upstream_model_version=upstream_model_version,
        aspect_ratios=aspect_ratios,
    )


def _video_model(
    model_id: str,
    *,
    engine: str,
    upstream_model: str,
) -> GeminiModelSpec:
    return GeminiModelSpec(
        model_id=model_id,
        display_name=model_id,
        family="video",
        upstream_model_id=None,
        upstream_model_version=None,
        aspect_ratios=frozenset({"16:9", "9:16"}),
        supported_actions=frozenset({"predictLongRunning"}),
        video_engine=engine,
        video_upstream_model=upstream_model,
    )


GEMINI_MODELS: dict[str, GeminiModelSpec] = {
    "gemini-3-pro-image": _image_model(
        "gemini-3-pro-image",
        family="pro",
        upstream_model_version="nano-banana-2",
        aspect_ratios=PRO_RATIOS,
    ),
    "gemini-3-pro-image-preview": _image_model(
        "gemini-3-pro-image-preview",
        family="pro",
        upstream_model_version="nano-banana-2",
        aspect_ratios=PRO_RATIOS,
    ),
    "gemini-3.1-flash-image": _image_model(
        "gemini-3.1-flash-image",
        family="flash",
        upstream_model_version="nano-banana-3",
        aspect_ratios=FLASH_RATIOS,
    ),
    "gemini-3.1-flash-image-preview": _image_model(
        "gemini-3.1-flash-image-preview",
        family="flash",
        upstream_model_version="nano-banana-3",
        aspect_ratios=FLASH_RATIOS,
    ),
    "veo-3.1-generate-preview": _video_model(
        "veo-3.1-generate-preview",
        engine="veo31-standard",
        upstream_model="google:firefly:colligo:veo31",
    ),
    "veo-3.1-fast-generate-preview": _video_model(
        "veo-3.1-fast-generate-preview",
        engine="veo31-fast",
        upstream_model="google:firefly:colligo:veo31-fast",
    ),
    **{
        model_id: GeminiModelSpec(
            model_id=model_id,
            display_name=model_id,
            family="text",
            upstream_model_id=None,
            upstream_model_version=None,
            aspect_ratios=frozenset(),
        )
        for model_id in sorted(TEST_TEXT_MODELS)
    },
}


def _invalid(message: str) -> GeminiNativeError:
    return GeminiNativeError(400, message, "INVALID_ARGUMENT")


def resolve_model_action(model_action: str) -> tuple[GeminiModelSpec, str]:
    if not isinstance(model_action, str):
        raise GeminiNativeError(404, "Model or action not found", "NOT_FOUND")
    model_id, separator, action = model_action.rpartition(":")
    model_spec = GEMINI_MODELS.get(model_id) if separator else None
    if model_spec is None or action not in model_spec.supported_actions:
        raise GeminiNativeError(404, "Model or action not found", "NOT_FOUND")
    return model_spec, action


async def read_limited_body(request: Request, max_bytes: int | None = None) -> bytes:
    limit = (
        GEMINI_NATIVE_MAX_BODY_BYTES
        if max_bytes is None
        else max(1, int(max_bytes))
    )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > limit:
            raise _invalid("Request body is too large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _invalid("Request body is too large")
        chunks.append(chunk)
    raw_body = b"".join(chunks)
    request._body = raw_body
    return raw_body


def decode_inline_image(data: str, mime_type: str) -> tuple[bytes, str]:
    normalized_mime = str(mime_type).strip().lower()
    if normalized_mime not in {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }:
        raise _invalid("Unsupported inline image MIME type")
    if len(data) > GEMINI_MAX_ENCODED_IMAGE_CHARS:
        raise _invalid(f"Inline image exceeds {GEMINI_MAX_IMAGE_MIB} MiB")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("Invalid inline image base64")
    if len(decoded) > GEMINI_MAX_IMAGE_BYTES:
        raise _invalid(f"Inline image exceeds {GEMINI_MAX_IMAGE_MIB} MiB")
    if normalized_mime == "image/jpg":
        normalized_mime = "image/jpeg"
    return decoded, normalized_mime


def _validate_parts(parts: Any, *, container_name: str) -> list[dict[str, Any]]:
    if not isinstance(parts, list):
        raise _invalid(f"{container_name}.parts must be an array")
    validated: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            raise _invalid(f"{container_name}.parts entries must be objects")
        if "text" in part and not isinstance(part["text"], str):
            raise _invalid("Part text must be a string")
        for inline_key in ("inlineData", "inline_data"):
            if inline_key not in part:
                continue
            inline_data = part[inline_key]
            if not isinstance(inline_data, dict):
                raise _invalid(f"{inline_key} must be an object")
            data = inline_data.get("data")
            mime_type = inline_data.get("mimeType")
            if not isinstance(data, str) or not data:
                raise _invalid(f"{inline_key}.data must be a non-empty string")
            if not isinstance(mime_type, str) or not mime_type.strip():
                raise _invalid(f"{inline_key}.mimeType must be a non-empty string")
        validated.append(part)
    return validated


def _config_field(config: dict[str, Any], camel: str, snake: str, default: Any) -> Any:
    if camel in config:
        return config[camel]
    if snake in config:
        return config[snake]
    return default


def _decode_request_json(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid("Invalid JSON request body")
    if not isinstance(payload, dict):
        raise _invalid("Request body must be an object")
    return payload


def parse_gemini_request(
    raw_body: bytes, model_spec: GeminiModelSpec
) -> ParsedGeminiRequest:
    payload = _decode_request_json(raw_body)

    contents = payload.get("contents")
    if not isinstance(contents, list) or not contents:
        raise _invalid("contents must be a non-empty array")

    # 与 Google proto3 JSON 一致:camelCase 与 snake_case 字段名都接受,camelCase 优先。
    generation_config = _config_field(payload, "generationConfig", "generation_config", {})
    if not isinstance(generation_config, dict):
        raise _invalid("generationConfig must be an object")
    image_config = _config_field(generation_config, "imageConfig", "image_config", {})
    if not isinstance(image_config, dict):
        raise _invalid("generationConfig.imageConfig must be an object")

    system_parts: list[dict[str, Any]] = []
    if "systemInstruction" in payload:
        system_instruction = payload["systemInstruction"]
        if not isinstance(system_instruction, dict):
            raise _invalid("systemInstruction must be an object")
        system_parts = _validate_parts(
            system_instruction.get("parts"), container_name="systemInstruction"
        )

    content_parts: list[list[dict[str, Any]]] = []
    for content in contents:
        if not isinstance(content, dict):
            raise _invalid("contents entries must be objects")
        content_parts.append(
            _validate_parts(content.get("parts"), container_name="content")
        )

    candidate_count = generation_config.get("candidateCount", 1)
    if type(candidate_count) is not int or candidate_count != 1:
        raise _invalid("candidateCount must be 1")

    aspect_ratio = _config_field(image_config, "aspectRatio", "aspect_ratio", "auto")
    if not isinstance(aspect_ratio, str):
        raise _invalid("aspectRatio must be a supported string")
    if (
        model_spec.family != "text"
        and aspect_ratio not in model_spec.aspect_ratios
        and aspect_ratio not in {"free", "auto"}
    ):
        raise _invalid("Unsupported aspectRatio")

    image_size_value = _config_field(image_config, "imageSize", "image_size", "1K")
    if not isinstance(image_size_value, str):
        raise _invalid("imageSize must be a supported string")
    image_size = image_size_value.upper()
    if image_size not in {"1K", "2K", "4K"}:
        raise _invalid("Unsupported imageSize")

    prompt_parts = [
        part["text"]
        for part in system_parts
        if "text" in part and part["text"]
    ]
    for parts in content_parts:
        prompt_parts.extend(
            part["text"] for part in parts if "text" in part and part["text"]
        )

    images: list[tuple[bytes, str]] = []
    total_image_bytes = 0
    for parts in content_parts:
        for part in parts:
            for inline_key in ("inlineData", "inline_data"):
                if inline_key not in part:
                    continue
                if model_spec.family == "text":
                    raise _invalid("Text models do not accept inline images")
                if len(images) >= GEMINI_MAX_IMAGES:
                    continue
                inline_data = part[inline_key]
                decoded, normalized_mime = decode_inline_image(
                    inline_data["data"], inline_data["mimeType"]
                )
                total_image_bytes += len(decoded)
                if total_image_bytes > GEMINI_MAX_TOTAL_IMAGE_BYTES:
                    raise _invalid("Inline images exceed 30 MiB total")
                images.append((decoded, normalized_mime))

    prompt = "\n".join(prompt_parts).strip()
    if not prompt and not images:
        raise _invalid("Request must include text or an inline image")

    return ParsedGeminiRequest(
        prompt=prompt,
        images=tuple(images),
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        candidate_count=candidate_count,
    )


def parse_veo_request(
    raw_body: bytes,
    model_spec: GeminiModelSpec,
) -> ParsedVeoRequest:
    if model_spec.family != "video":
        raise _invalid("Model does not support video generation")
    payload = _decode_request_json(raw_body)
    for field, value in payload.items():
        if field not in {"instances", "parameters"} and _nonempty_parameter(value):
            raise _invalid(f"Unsupported parameter: {field}")
    instances = payload.get("instances")
    if not isinstance(instances, list) or len(instances) != 1:
        raise _invalid("instances must contain exactly one item")
    instance = instances[0]
    if not isinstance(instance, dict):
        raise _invalid("instances entries must be objects")
    prompt = instance.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _invalid("instances[0].prompt must be a non-empty string")
    for field, value in instance.items():
        if field != "prompt" and _nonempty_parameter(value):
            raise _invalid(f"Unsupported parameter: {field}")
    for field in (
        "image",
        "video",
        "referenceImages",
        "reference_images",
        "lastFrame",
        "last_frame",
    ):
        if field in instance:
            raise _invalid(f"Unsupported parameter: {field}")

    parameters = payload.get("parameters", {})
    if not isinstance(parameters, dict):
        raise _invalid("parameters must be an object")
    allowed_parameter_fields = frozenset(
        {
            "aspectRatio",
            "aspect_ratio",
            "durationSeconds",
            "duration_seconds",
            "resolution",
            "negativePrompt",
            "negative_prompt",
            "sampleCount",
            "sample_count",
        }
    )
    for field, value in parameters.items():
        if field not in allowed_parameter_fields and _nonempty_parameter(value):
            raise _invalid(f"Unsupported parameter: {field}")
    for field in ("image", "video", "referenceImages", "reference_images"):
        if field in parameters:
            raise _invalid(f"Unsupported parameter: {field}")

    aspect_ratio = _config_field(
        parameters,
        "aspectRatio",
        "aspect_ratio",
        "16:9",
    )
    if not isinstance(aspect_ratio, str) or aspect_ratio not in model_spec.aspect_ratios:
        raise _invalid("Unsupported aspectRatio")

    raw_duration = _config_field(
        parameters,
        "durationSeconds",
        "duration_seconds",
        8,
    )
    if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
        raise _invalid("durationSeconds must be 4, 6, or 8")
    try:
        numeric_duration = float(raw_duration)
    except (TypeError, ValueError, OverflowError):
        raise _invalid("durationSeconds must be 4, 6, or 8")
    if not math.isfinite(numeric_duration):
        raise _invalid("durationSeconds must be 4, 6, or 8")
    duration = int(numeric_duration)
    if numeric_duration != float(duration) or duration not in {4, 6, 8}:
        raise _invalid("durationSeconds must be 4, 6, or 8")

    raw_resolution = parameters.get("resolution", "720p")
    if not isinstance(raw_resolution, str):
        raise _invalid("resolution must be 720p or 1080p")
    resolution = raw_resolution.strip().lower()
    if resolution not in {"720p", "1080p"}:
        raise _invalid("resolution must be 720p or 1080p")
    if resolution == "1080p" and duration != 8:
        raise _invalid("1080p resolution requires durationSeconds=8")

    negative_prompt = _config_field(
        parameters,
        "negativePrompt",
        "negative_prompt",
        "",
    )
    if not isinstance(negative_prompt, str):
        raise _invalid("negativePrompt must be a string")

    # 官方 Veo API 的合法参数（new-api 等网关会固定携带）；后端一次只产出一个视频
    sample_count = _config_field(parameters, "sampleCount", "sample_count", 1)
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count != 1
    ):
        raise _invalid("sampleCount must be 1")

    person_generation = _config_field(
        parameters,
        "personGeneration",
        "person_generation",
        None,
    )
    if person_generation not in (None, ""):
        raise _invalid("Unsupported parameter: personGeneration")

    return ParsedVeoRequest(
        prompt=prompt.strip(),
        aspect_ratio=aspect_ratio,
        duration=duration,
        resolution=resolution,
        negative_prompt=negative_prompt.strip(),
    )


def google_error(error: GeminiNativeError) -> JSONResponse:
    return JSONResponse(
        status_code=error.code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "status": error.status,
            }
        },
    )


def model_resource(spec: GeminiModelSpec) -> dict:
    return {
        "name": f"models/{spec.model_id}",
        "displayName": spec.display_name,
        "supportedGenerationMethods": [
            action for action in GEMINI_ACTION_ORDER if action in spec.supported_actions
        ],
    }


def video_proxy_uri(uri: str) -> str:
    parsed = urlsplit(str(uri or ""))
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "key"]
    query.append(("key", "proxy"))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def build_image_response(
    spec: GeminiModelSpec,
    parsed: ParsedGeminiRequest,
    image_bytes: bytes,
) -> dict:
    candidate: dict[str, Any] = {
        "content": {
            "parts": [
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            ],
            "role": "model",
        },
        "finishReason": "STOP",
    }
    if spec.family == "pro":
        candidate["index"] = 0
    return {
        "candidates": [candidate],
        "usageMetadata": build_image_usage_metadata(
            parsed.prompt,
            len(parsed.images),
            spec.family,
            parsed.image_size,
        ),
        "modelVersion": spec.model_id,
        "responseId": str(uuid.uuid4()),
    }


def build_canned_response(spec: GeminiModelSpec, prompt: str) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": CANNED_TEXT}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": build_canned_usage_metadata(prompt),
        "modelVersion": spec.model_id,
        "responseId": str(uuid.uuid4()),
    }


def sse_response(payload: dict) -> StreamingResponse:
    event = f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    return StreamingResponse(iter([event]), media_type="text/event-stream")


def build_gemini_native_router(
    *,
    config_manager,
    client,
    generated_dir,
    run_with_token_retries,
    set_request_error_detail,
    set_request_task_progress,
    set_request_logging_fields,
    set_request_credit_context,
    set_request_preview,
    public_image_url,
    on_generated_file_written,
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    adobe_error_cls,
    logger,
    video_task_manager=None,
    video_task_store=None,
    public_generated_url=None,
) -> APIRouter:
    router = APIRouter()

    def require_api_key(request: Request) -> None:
        required = str(config_manager.get("api_key", "") or "").strip()
        if not required:
            return
        candidates = [
            str(request.headers.get("x-goog-api-key") or "").strip(),
            str(request.query_params.get("key") or "").strip(),
        ]
        if not any(
            candidate and secrets.compare_digest(candidate, required)
            for candidate in candidates
        ):
            raise GeminiNativeError(401, "Invalid API key", "UNAUTHENTICATED")

    def get_model(model_id: str) -> GeminiModelSpec:
        spec = GEMINI_MODELS.get(str(model_id or ""))
        if spec is None:
            raise GeminiNativeError(404, "Model not found", "NOT_FOUND")
        return spec

    def error_response(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, GeminiNativeError):
            error = exc
        elif isinstance(exc, VideoTaskCapacityError):
            error = GeminiNativeError(
                429, "Video task queue is full", "RESOURCE_EXHAUSTED"
            )
        elif isinstance(exc, VideoTaskStorageError):
            error = GeminiNativeError(
                500, "Unable to persist video task", "INTERNAL"
            )
        elif isinstance(exc, quota_error_cls):
            error = GeminiNativeError(
                429, "Resource exhausted", "RESOURCE_EXHAUSTED"
            )
        elif isinstance(exc, auth_error_cls):
            error = GeminiNativeError(
                401, "Authentication failed", "UNAUTHENTICATED"
            )
        elif isinstance(exc, upstream_temp_error_cls):
            error = GeminiNativeError(
                503, "Upstream temporarily unavailable", "UNAVAILABLE"
            )
        elif isinstance(exc, adobe_error_cls):
            error = GeminiNativeError(500, "Image generation failed", "INTERNAL")
        else:
            set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            logger.exception("Unhandled Gemini native request error")
            error = GeminiNativeError(500, "Internal server error", "INTERNAL")
        request.state.log_error = error.message
        return google_error(error)

    def get_deadline() -> float:
        raw_value = config_manager.get("gemini_native_deadline_seconds", 500)
        if isinstance(raw_value, bool):
            raise GeminiNativeError(
                500, "Invalid Gemini native deadline configuration", "INTERNAL"
            )
        try:
            deadline_seconds = float(raw_value)
        except (TypeError, ValueError):
            deadline_seconds = 0
        if deadline_seconds <= 0:
            raise GeminiNativeError(
                500, "Invalid Gemini native deadline configuration", "INTERNAL"
            )
        return time.monotonic() + deadline_seconds

    @router.get("/v1beta/models")
    def list_models(request: Request):
        try:
            require_api_key(request)
            return {"models": [model_resource(spec) for spec in GEMINI_MODELS.values()]}
        except Exception as exc:
            return error_response(request, exc)

    @router.get("/v1beta/models/{model}")
    def retrieve_model(model: str, request: Request):
        try:
            require_api_key(request)
            spec = get_model(model)
            set_request_logging_fields(request, spec.model_id, None)
            return model_resource(spec)
        except Exception as exc:
            return error_response(request, exc)

    @router.get("/v1beta/models/{model}/operations/{operation_id}")
    def get_video_operation(model: str, operation_id: str, request: Request):
        try:
            require_api_key(request)
            spec = get_model(model)
            if spec.family != "video" or video_task_store is None:
                raise GeminiNativeError(404, "Operation not found", "NOT_FOUND")
            record = video_task_store.get(operation_id)
            if (
                record is None
                or record.protocol != "veo"
                or record.model != spec.model_id
            ):
                raise GeminiNativeError(404, "Operation not found", "NOT_FOUND")
            set_request_logging_fields(request, spec.model_id, None)
            name = f"models/{spec.model_id}/operations/{record.id}"
            if record.status == "completed":
                if not record.result_url:
                    raise GeminiNativeError(
                        500, "Video task has no result URL", "INTERNAL"
                    )
                return {
                    "name": name,
                    "done": True,
                    "response": {
                        "@type": (
                            "type.googleapis.com/google.ai.generativelanguage."
                            "v1beta.GenerateVideoResponse"
                        ),
                        "generateVideoResponse": {
                            "generatedSamples": [
                                {
                                    "video": {
                                        "uri": video_proxy_uri(record.result_url)
                                    }
                                }
                            ]
                        },
                    },
                }
            if record.status == "failed":
                return {
                    "name": name,
                    "done": True,
                    "error": {
                        "code": 13,
                        "message": record.error_message
                        or "Video generation failed",
                    },
                }
            return {
                "name": name,
                "done": False,
                "metadata": {
                    "progressPercent": int(
                        max(0, min(float(record.progress or 0), 100))
                    )
                },
            }
        except Exception as exc:
            return error_response(request, exc)

    @router.post("/v1beta/models/{model_action}")
    async def invoke_model(model_action: str, request: Request):
        try:
            require_api_key(request)
            spec, action = resolve_model_action(model_action)
            raw_body = await read_limited_body(
                request,
                max_bytes=(
                    GEMINI_VIDEO_MAX_BODY_BYTES if spec.family == "video" else None
                ),
            )
            if spec.family == "video":
                if video_task_manager is None or public_generated_url is None:
                    raise GeminiNativeError(
                        503, "Video task service is unavailable", "UNAVAILABLE"
                    )
                parsed_video = parse_veo_request(raw_body, spec)
                set_request_logging_fields(
                    request, spec.model_id, parsed_video.prompt
                )
                operation_id = f"operation_{uuid.uuid4().hex}"
                log_id = str(
                    getattr(request.state, "log_id", "") or uuid.uuid4().hex[:12]
                )
                ratio_suffix = parsed_video.aspect_ratio.replace(":", "x")
                catalog_prefix = (
                    "firefly-veo31-fast"
                    if spec.video_engine == "veo31-fast"
                    else "firefly-veo31"
                )
                task_spec = VideoTaskSpec(
                    id=operation_id,
                    protocol="veo",
                    model=spec.model_id,
                    prompt=parsed_video.prompt,
                    prompt_preview=parsed_video.prompt.replace("\r", " ")
                    .replace("\n", " ")[:180],
                    engine=str(spec.video_engine or ""),
                    upstream_model=str(spec.video_upstream_model or ""),
                    duration=parsed_video.duration,
                    aspect_ratio=parsed_video.aspect_ratio,
                    resolution=parsed_video.resolution,
                    requested_size=parsed_video.resolution,
                    negative_prompt=parsed_video.negative_prompt,
                    credit_model_id=(
                        f"{catalog_prefix}-{parsed_video.duration}s-"
                        f"{ratio_suffix}-{parsed_video.resolution}"
                    ),
                    result_url_prefix=str(public_generated_url(request, "")),
                    log_id=log_id,
                )
                video_task_manager.submit(task_spec)
                request.state.log_managed_externally = True
                return {
                    "name": f"models/{spec.model_id}/operations/{operation_id}"
                }
            parsed = parse_gemini_request(raw_body, spec)
            set_request_logging_fields(request, spec.model_id, parsed.prompt)

            if action != "countTokens" and spec.family != "text":
                set_request_credit_context(request, spec.model_id, parsed.image_size)

            if action == "countTokens":
                return build_count_tokens_response(
                    parsed.prompt, len(parsed.images), spec.family
                )

            if spec.family == "text":
                payload = build_canned_response(spec, parsed.prompt)
            else:
                ordered_ratios = (
                    GEMINI_FLASH_FIXED_RATIOS
                    if spec.family == "flash"
                    else GEMINI_PRO_FIXED_RATIOS
                )
                try:
                    geometry = resolve_requested_aspect_ratio(
                        parsed.aspect_ratio,
                        input_images=parsed.images,
                        supported_ratios=ordered_ratios,
                        supports_auto=True,
                        output_resolution=parsed.image_size,
                    )
                except HTTPException as exc:
                    raise _invalid(str(exc.detail)) from exc
                deadline = get_deadline()

                def run_once(token: str) -> dict:
                    source_ids = [
                        client.upload_image(
                            token,
                            image_bytes,
                            mime_type,
                            deadline=deadline,
                        )
                        for image_bytes, mime_type in parsed.images
                    ]
                    job_id = uuid.uuid4().hex
                    out_path = generated_dir / f"{job_id}.png"
                    generated_dir.mkdir(parents=True, exist_ok=True)
                    old_size = int(out_path.stat().st_size) if out_path.exists() else 0

                    def progress(update: dict) -> None:
                        set_request_task_progress(
                            request,
                            task_status=str(
                                update.get("task_status") or "IN_PROGRESS"
                            ),
                            task_progress=update.get("task_progress"),
                            upstream_job_id=update.get("upstream_job_id"),
                            retry_after=update.get("retry_after"),
                            error=update.get("error"),
                        )

                    try:
                        client.generate(
                            token=token,
                            prompt=parsed.prompt,
                            aspect_ratio=geometry.aspect_ratio,
                            output_resolution=parsed.image_size,
                            upstream_model_id=str(spec.upstream_model_id),
                            upstream_model_version=str(spec.upstream_model_version),
                            source_image_ids=source_ids,
                            output_size=geometry.output_size,
                            fallback_aspect_ratio=geometry.fallback_aspect_ratio,
                            timeout=client.generate_timeout,
                            out_path=out_path,
                            progress_cb=progress,
                            deadline=deadline,
                        )
                        image_bytes = out_path.read_bytes()
                        new_size = int(out_path.stat().st_size)
                    except Exception:
                        out_path.unlink(missing_ok=True)
                        raise

                    on_generated_file_written(out_path, old_size, new_size)
                    set_request_preview(
                        request,
                        public_image_url(request, job_id),
                        kind="image",
                    )
                    return build_image_response(spec, parsed, image_bytes)

                payload = await run_in_threadpool(
                    lambda: run_with_token_retries(
                        request=request,
                        operation_name=f"gemini.{action}",
                        run_once=run_once,
                        set_request_error_detail=set_request_error_detail,
                        reraise_domain=True,
                        deadline=deadline,
                    )
                )

            if action == "streamGenerateContent":
                return sse_response(payload)
            return payload
        except Exception as exc:
            return error_response(request, exc)

    return router
