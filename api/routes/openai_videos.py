from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from core.video_tasks import (
    VideoTaskCapacityError,
    VideoTaskRecord,
    VideoTaskSpec,
    VideoTaskStorageError,
)


OPENAI_VIDEO_MAX_BODY_BYTES = 1024 * 1024
SORA_MODELS = frozenset({"sora-2", "sora-2-pro"})
SORA_SECONDS = frozenset({4, 8, 12})
SORA_SIZE_MAP = {
    "1280x720": ("16:9", "720p"),
    "720x1280": ("9:16", "720p"),
}
SORA_PRO_SIZE_MAP = {
    **SORA_SIZE_MAP,
    "1792x1024": ("16:9", "1080p"),
    "1024x1792": ("9:16", "1080p"),
}
UNSUPPORTED_VIDEO_INPUT_FIELDS = frozenset(
    {
        "input_reference",
        "characters",
        "character",
        "image",
        "images",
        "video",
        "reference_images",
    }
)


class OpenAIVideoRequestError(Exception):
    def __init__(self, message: str, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = str(code)
        self.status_code = int(status_code)


def openai_video_error(
    status_code: int,
    message: str,
    code: str,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=int(status_code),
        content={
            "error": {
                "message": str(message),
                "type": str(error_type),
                "code": str(code),
            }
        },
    )


async def read_openai_video_body(request: Request) -> bytes:
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            declared = int(raw_length)
        except ValueError:
            declared = -1
        if declared > OPENAI_VIDEO_MAX_BODY_BYTES:
            raise OpenAIVideoRequestError(
                "Request body exceeds 1 MiB",
                "request_too_large",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > OPENAI_VIDEO_MAX_BODY_BYTES:
            raise OpenAIVideoRequestError(
                "Request body exceeds 1 MiB",
                "request_too_large",
            )
        chunks.append(chunk)
    raw_body = b"".join(chunks)
    request._body = raw_body
    return raw_body


async def parse_openai_video_body(request: Request) -> dict[str, Any]:
    raw_body = await read_openai_video_body(request)
    content_type = str(request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as exc:
            raise OpenAIVideoRequestError(
                "Invalid multipart form",
                "invalid_multipart_form",
            ) from exc
        return {str(key): value for key, value in form.multi_items()}
    if content_type.startswith("application/json") or not content_type:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise OpenAIVideoRequestError("Invalid JSON body", "invalid_json") from exc
        if not isinstance(payload, dict):
            raise OpenAIVideoRequestError(
                "Request body must be a JSON object",
                "invalid_request",
            )
        return payload
    raise OpenAIVideoRequestError(
        "Content-Type must be application/json or multipart/form-data",
        "unsupported_content_type",
    )


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _parse_seconds(value: Any) -> int:
    if value is None or value == "":
        return 4
    if isinstance(value, bool):
        raise OpenAIVideoRequestError(
            "seconds must be one of 4, 8, or 12",
            "invalid_seconds",
        )
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise OpenAIVideoRequestError(
            "seconds must be one of 4, 8, or 12",
            "invalid_seconds",
        ) from exc
    if str(value).strip() not in {str(seconds), f"{seconds}.0"}:
        raise OpenAIVideoRequestError(
            "seconds must be one of 4, 8, or 12",
            "invalid_seconds",
        )
    if seconds not in SORA_SECONDS:
        raise OpenAIVideoRequestError(
            "seconds must be one of 4, 8, or 12",
            "invalid_seconds",
        )
    return seconds


def _serialize_video(record: VideoTaskRecord) -> dict:
    payload: dict[str, Any] = {
        "id": record.id,
        "object": "video",
        "model": record.model,
        "status": record.status,
        "progress": int(max(0, min(float(record.progress or 0), 100))),
        "seconds": str(int(record.duration)),
        "size": record.requested_size,
        "created_at": int(record.created_at or 0),
    }
    if record.completed_at is not None:
        payload["completed_at"] = int(record.completed_at)
    if record.status == "failed":
        payload["error"] = {
            "code": record.error_code or "generation_failed",
            "message": record.error_message or "Video generation failed",
        }
    return payload


def build_openai_videos_router(
    *,
    task_manager,
    task_store,
    require_service_api_key: Callable[[Request], None],
    public_generated_url: Callable[[Request, str], str],
) -> APIRouter:
    router = APIRouter()

    def authorize(request: Request) -> JSONResponse | None:
        try:
            require_service_api_key(request)
        except HTTPException as exc:
            return openai_video_error(
                int(exc.status_code or 401),
                str(exc.detail or "Invalid API key"),
                "invalid_api_key",
                "authentication_error",
            )
        return None

    @router.post("/v1/videos")
    async def create_video(request: Request):
        auth_error = authorize(request)
        if auth_error is not None:
            return auth_error
        try:
            body = await parse_openai_video_body(request)
            model = body.get("model")
            if not isinstance(model, str) or model.strip() not in SORA_MODELS:
                raise OpenAIVideoRequestError(
                    "model must be sora-2 or sora-2-pro",
                    "invalid_model",
                )
            model = model.strip()
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise OpenAIVideoRequestError(
                    "prompt is required",
                    "missing_prompt",
                )
            prompt = prompt.strip()
            for field in UNSUPPORTED_VIDEO_INPUT_FIELDS:
                if field in body and _nonempty(body.get(field)):
                    raise OpenAIVideoRequestError(
                        f"Unsupported parameter: {field}",
                        "unsupported_parameter",
                    )

            seconds = _parse_seconds(body.get("seconds"))
            size = body.get("size") or "1280x720"
            if not isinstance(size, str):
                raise OpenAIVideoRequestError("Invalid video size", "invalid_size")
            size = size.strip()
            allowed_sizes = SORA_PRO_SIZE_MAP if model == "sora-2-pro" else SORA_SIZE_MAP
            if size not in allowed_sizes:
                raise OpenAIVideoRequestError(
                    f"Invalid size for {model}",
                    "invalid_size",
                )
            aspect_ratio, resolution = allowed_sizes[size]
            suffix = aspect_ratio.replace(":", "x")
            model_family = "sora2-pro" if model == "sora-2-pro" else "sora2"
            upstream_model = (
                "openai:firefly:colligo:sora2-pro"
                if model == "sora-2-pro"
                else "openai:firefly:colligo:sora2"
            )
            task_id = f"video_{uuid.uuid4().hex}"
            log_id = str(getattr(request.state, "log_id", "") or uuid.uuid4().hex[:12])
            url_prefix = str(public_generated_url(request, ""))
            spec = VideoTaskSpec(
                id=task_id,
                protocol="openai",
                model=model,
                prompt=prompt,
                prompt_preview=prompt.replace("\r", " ").replace("\n", " ")[:180],
                engine=model_family,
                upstream_model=upstream_model,
                duration=seconds,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                requested_size=size,
                negative_prompt="",
                credit_model_id=f"firefly-{model_family}-{seconds}s-{suffix}",
                result_url_prefix=url_prefix,
                log_id=log_id,
            )
            request.state.log_model = model
            request.state.log_prompt_preview = spec.prompt_preview
            created = task_manager.submit(spec)
            request.state.log_managed_externally = True
            return _serialize_video(created)
        except OpenAIVideoRequestError as exc:
            return openai_video_error(
                exc.status_code,
                exc.message,
                exc.code,
            )
        except VideoTaskCapacityError as exc:
            return openai_video_error(429, str(exc), "queue_full", "rate_limit_error")
        except VideoTaskStorageError:
            return openai_video_error(
                500,
                "Unable to persist video task",
                "task_storage_failed",
                "server_error",
            )

    @router.get("/v1/videos/{task_id}")
    def get_video(task_id: str, request: Request):
        auth_error = authorize(request)
        if auth_error is not None:
            return auth_error
        record = task_store.get(task_id)
        if record is None or record.protocol != "openai":
            return openai_video_error(404, "Video not found", "video_not_found")
        return _serialize_video(record)

    @router.get("/v1/videos/{task_id}/content")
    def get_video_content(task_id: str, request: Request):
        auth_error = authorize(request)
        if auth_error is not None:
            return auth_error
        record = task_store.get(task_id)
        if record is None or record.protocol != "openai":
            return openai_video_error(404, "Video not found", "video_not_found")
        if record.status in {"queued", "in_progress"}:
            return openai_video_error(
                409,
                "Video is not completed yet",
                "video_not_ready",
            )
        if record.status == "failed":
            return openai_video_error(
                424,
                record.error_message or "Video generation failed",
                record.error_code or "generation_failed",
                "server_error",
            )
        path = Path(str(record.result_path or ""))
        if not path.exists() or not path.is_file():
            return openai_video_error(
                404,
                "Generated video file is no longer available",
                "video_file_not_found",
            )
        return FileResponse(
            path=path,
            media_type=record.result_mime or "video/mp4",
            filename=path.name,
        )

    return router
