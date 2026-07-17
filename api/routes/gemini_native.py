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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.models.gemini_usage import (
    CANNED_TEXT,
    build_canned_usage_metadata,
    build_count_tokens_response,
    build_image_usage_metadata,
)

GEMINI_NATIVE_MAX_BODY_BYTES = 48 * 1024 * 1024
GEMINI_MAX_IMAGE_BYTES = 10 * 1024 * 1024
GEMINI_MAX_TOTAL_IMAGE_BYTES = 30 * 1024 * 1024
GEMINI_MAX_IMAGES = 6
GEMINI_MAX_ENCODED_IMAGE_CHARS = 4 * math.ceil(GEMINI_MAX_IMAGE_BYTES / 3)

PRO_RATIOS = frozenset({"1:1", "16:9", "9:16", "4:3", "3:4"})
FLASH_RATIOS = frozenset({*PRO_RATIOS, "1:8", "1:4", "4:1", "8:1"})
TEST_TEXT_MODELS = frozenset(
    {
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-3-pro-preview",
        "gemini-3.1-pro-preview",
    }
)
GEMINI_NATIVE_ACTIONS = frozenset(
    {"generateContent", "streamGenerateContent", "countTokens"}
)


@dataclass(frozen=True)
class GeminiModelSpec:
    model_id: str
    display_name: str
    family: str
    upstream_model_id: str | None
    upstream_model_version: str | None
    aspect_ratios: frozenset[str]


@dataclass(frozen=True)
class ParsedGeminiRequest:
    prompt: str
    images: Sequence[tuple[bytes, str]]
    aspect_ratio: str
    image_size: str
    candidate_count: int


class GeminiNativeError(Exception):
    def __init__(self, code: int, message: str, status: str):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.status = str(status)


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
    if model_spec is None or action not in GEMINI_NATIVE_ACTIONS:
        raise GeminiNativeError(404, "Model or action not found", "NOT_FOUND")
    return model_spec, action


async def read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > GEMINI_NATIVE_MAX_BODY_BYTES:
            raise _invalid("Request body is too large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > GEMINI_NATIVE_MAX_BODY_BYTES:
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
        raise _invalid("Inline image exceeds 10 MiB")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("Invalid inline image base64")
    if len(decoded) > GEMINI_MAX_IMAGE_BYTES:
        raise _invalid("Inline image exceeds 10 MiB")
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

    generation_config = payload.get("generationConfig", {})
    if not isinstance(generation_config, dict):
        raise _invalid("generationConfig must be an object")
    image_config = generation_config.get("imageConfig", {})
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

    aspect_ratio = image_config.get("aspectRatio", "1:1")
    if not isinstance(aspect_ratio, str):
        raise _invalid("aspectRatio must be a supported string")
    if model_spec.family != "text" and aspect_ratio not in model_spec.aspect_ratios:
        raise _invalid("Unsupported aspectRatio")

    image_size_value = image_config.get("imageSize", "1K")
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
            "generateContent",
            "streamGenerateContent",
            "countTokens",
        ],
    }


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
    set_request_preview,
    public_image_url,
    on_generated_file_written,
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    adobe_error_cls,
    logger,
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

    @router.post("/v1beta/models/{model_action}")
    async def invoke_model(model_action: str, request: Request):
        try:
            require_api_key(request)
            spec, action = resolve_model_action(model_action)
            raw_body = await read_limited_body(request)
            parsed = parse_gemini_request(raw_body, spec)
            set_request_logging_fields(request, spec.model_id, parsed.prompt)

            if action == "countTokens":
                return build_count_tokens_response(
                    parsed.prompt, len(parsed.images), spec.family
                )

            if spec.family == "text":
                payload = build_canned_response(spec, parsed.prompt)
            else:
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
                            aspect_ratio=parsed.aspect_ratio,
                            output_resolution=parsed.image_size,
                            upstream_model_id=str(spec.upstream_model_id),
                            upstream_model_version=str(spec.upstream_model_version),
                            source_image_ids=source_ids,
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
