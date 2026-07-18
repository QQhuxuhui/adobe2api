import base64
import re
import secrets
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from api.openai_responses import (
    ResponsesRequestError,
    build_responses_image_response,
    encode_image_result,
    iter_responses_image_sse,
    parse_responses_image_request,
)
from api.schemas import GenerateRequest
from core.entity_store import entity_store
from core.image_generation import generate_image_artifact
from core.models.resolver import build_image_usage
from core.stores import RequestLogRecord
from core.video_generation import generate_video_file


def build_generation_router(
    *,
    store,
    token_manager,
    client,
    credits_tracker,
    request_log_store,
    generated_dir: Path,
    model_catalog: dict,
    video_model_catalog: dict,
    supported_ratios: set,
    resolve_model: Callable[[str | None], dict],
    resolve_ratio_and_resolution: Callable[[dict, str | None], tuple[str, str, str]],
    require_service_api_key: Callable[[Request], None],
    set_request_task_progress: Callable[..., None],
    set_request_credit_context: Callable[[Request, str, str], None],
    run_with_token_retries: Callable[..., Any],
    set_request_error_detail: Callable[..., str],
    set_request_preview: Callable[[Request, str, str], None],
    public_image_url: Callable[[Request, str], str],
    public_generated_url: Callable[[Request, str], str],
    resolve_video_options: Callable[[dict], tuple[bool, str, str]],
    load_input_images: Callable[[Any], list[tuple[bytes, str]]],
    normalize_image_mime: Callable[[str], str],
    set_request_logging_fields: Callable[[Request, Any, Any], None],
    prepare_video_source_image: Callable[[bytes, str, str], tuple[bytes, str]],
    video_ext_from_meta: Callable[[dict], str],
    extract_prompt_from_messages: Callable[[Any], str],
    sse_chat_stream: Callable[[dict], Any],
    on_generated_file_written: Callable[[Path, int, int], None],
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    logger,
) -> APIRouter:
    router = APIRouter()
    entity_ref_re = re.compile(r"@entity:([^\s@]+)")

    def _nanoid(size: int = 21) -> str:
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
        return "".join(secrets.choice(alphabet) for _ in range(size))

    def _entity_name(item: dict) -> str:
        entity_value = item.get("entityValue")
        if isinstance(entity_value, dict):
            name = str(entity_value.get("displayName") or "").strip()
            if name:
                return name
        return str(item.get("name") or item.get("displayName") or "").strip()

    def _entity_urn(item: dict) -> str:
        for key in ("id", "urn", "entityId", "entityUrn"):
            val = str(item.get(key) or "").strip()
            if val:
                return val
        entity = item.get("entity")
        if isinstance(entity, dict):
            return _entity_urn(entity)
        return ""

    def _entity_names_from_prompt(raw_prompt: str) -> list[str]:
        matches = list(entity_ref_re.finditer(raw_prompt or ""))
        names: list[str] = []
        for match in matches:
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
        return names

    def _sync_entity_by_name(name: str) -> list[dict]:
        found: list[dict] = []
        for token_info in token_manager.list_active_account_tokens():
            token = str(token_info.get("token") or "").strip()
            account_id = str(token_info.get("account_id") or "").strip()
            if not token or not account_id:
                continue
            try:
                entities = client.list_entities(token, limit=100)
            except Exception:
                continue
            for item in entities:
                item_name = _entity_name(item)
                if item_name != name:
                    continue
                urn = _entity_urn(item)
                if not urn:
                    continue
                found.append(
                    entity_store.upsert(
                        entity_id=urn,
                        name=item_name,
                        entity_type=str(item.get("entityType") or item.get("type") or ""),
                        account_id=account_id,
                        account_name=str(token_info.get("account_name") or ""),
                        account_email=str(token_info.get("account_email") or ""),
                    )
                )
        return found

    def _resolve_entity_bindings(raw_prompt: str) -> tuple[str, list[dict]]:
        refs: list[dict] = []
        account_id = ""
        for name in _entity_names_from_prompt(raw_prompt):
            matches = entity_store.find_by_name(name)
            if not matches:
                matches = _sync_entity_by_name(name)
            account_ids = {
                str(item.get("account_id") or "").strip()
                for item in matches
                if str(item.get("account_id") or "").strip()
            }
            if not matches:
                raise HTTPException(status_code=400, detail=f"entity not found: {name}")
            if len(account_ids) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"entity name is ambiguous across accounts: {name}",
                )
            if len(matches) > 1 and len({str(item.get("id") or "") for item in matches}) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"entity name is ambiguous: {name}",
                )
            current_account = next(iter(account_ids), "")
            if not current_account:
                raise HTTPException(status_code=400, detail=f"entity has no account: {name}")
            if account_id and account_id != current_account:
                raise HTTPException(
                    status_code=400,
                    detail="entities in one prompt must belong to the same Adobe account",
                )
            account_id = current_account
            refs.append(
                {
                    "name": name,
                    "urn": str(matches[0].get("id") or "").strip(),
                    "account_id": account_id,
                }
            )
        return account_id, refs

    def _resolve_kling_entity_refs(
        token: str,
        raw_prompt: str,
        bound_refs: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        matches = list(entity_ref_re.finditer(raw_prompt or ""))
        if not matches:
            return raw_prompt, []
        if bound_refs is not None:
            by_name = {str(item.get("name") or "").strip(): item for item in bound_refs}
        else:
            entities = client.list_entities(token, limit=100)
            by_name = {_entity_name(item): item for item in entities if _entity_name(item)}
        refs: list[dict] = []
        replacements: dict[str, str] = {}
        for match in matches:
            name = match.group(1).strip()
            if name in replacements:
                continue
            item = by_name.get(name)
            if not item:
                raise HTTPException(status_code=400, detail=f"entity not found: {name}")
            urn = str(item.get("urn") or "").strip() if bound_refs is not None else _entity_urn(item)
            if not urn:
                raise HTTPException(status_code=400, detail=f"entity has no urn: {name}")
            mention_id = _nanoid()
            replacements[name] = mention_id
            refs.append({"name": name, "urn": urn, "mention_id": mention_id})

        def replace_match(match: re.Match) -> str:
            return f"@{replacements[match.group(1).strip()]}"

        return entity_ref_re.sub(replace_match, raw_prompt), refs

    @router.get("/v1/models")
    def list_models(request: Request):
        require_service_api_key(request)
        data = []
        for model_id, conf in model_catalog.items():
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "adobe2api",
                    "description": conf["description"],
                }
            )
        for model_id, conf in video_model_catalog.items():
            if bool(conf.get("hidden", False)):
                continue
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "adobe2api",
                    "description": conf["description"],
                }
            )
        return {"object": "list", "data": data}

    @router.post("/v1/images/generations")
    def openai_generate(data: dict, request: Request):
        require_service_api_key(request)

        prompt = data.get("prompt", "").strip()
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "prompt is required",
                        "type": "invalid_request_error",
                    }
                },
            )

        model_id = data.get("model")
        if str(model_id or "").strip() in video_model_catalog:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Use /v1/chat/completions for video generation",
                        "type": "invalid_request_error",
                    }
                },
            )
        ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
            data, model_id
        )
        model_conf = resolve_model(resolved_model_id)
        set_request_credit_context(request, resolved_model_id, output_resolution)

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                def _image_progress_cb(update: dict):
                    set_request_task_progress(
                        request,
                        task_status=str(update.get("task_status") or "IN_PROGRESS"),
                        task_progress=update.get("task_progress"),
                        upstream_job_id=update.get("upstream_job_id"),
                        retry_after=update.get("retry_after"),
                        error=update.get("error"),
                    )

                artifact = generate_image_artifact(
                    client=client,
                    token=token,
                    prompt=prompt,
                    aspect_ratio=ratio,
                    output_resolution=output_resolution,
                    model_config=model_conf,
                    generated_dir=generated_dir,
                    source_image_ids=[],
                    progress_cb=_image_progress_cb,
                    on_generated_file_written=on_generated_file_written,
                )
                image_url = public_image_url(request, artifact.job_id)
                set_request_preview(request, image_url, kind="image")
                return {
                    "created": int(time.time()),
                    "model": resolved_model_id,
                    "data": [{"url": image_url}],
                    # 该接口不上传输入图(client.generate 无 source_image_ids),
                    # 输入图 token 恒为 0,不按请求里未使用的图片字段虚计费
                    "usage": build_image_usage(prompt, output_resolution, ratio, 0),
                }

            return run_with_token_retries(
                request=request,
                operation_name="images.generations",
                run_once=_run_once,
            )

        except quota_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token quota exhausted",
                status_code=429,
                error_type="rate_limit_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token quota exhausted",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Token quota exhausted",
                        "type": "rate_limit_error",
                        "code": error_code,
                    }
                },
            )
        except auth_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token invalid or expired",
                status_code=401,
                error_type="authentication_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token invalid or expired",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Token invalid or expired",
                        "type": "authentication_error",
                        "code": error_code,
                    }
                },
            )
        except upstream_temp_error_cls as exc:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=exc,
                status_code=503,
                error_type="server_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )
        except HTTPException as exc:
            err_type = (
                "invalid_request_error"
                if 400 <= int(exc.status_code) < 500
                else "server_error"
            )
            error_code = set_request_error_detail(
                request,
                error=str(exc.detail),
                status_code=exc.status_code,
                error_type=err_type,
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": err_type,
                        "code": error_code,
                    }
                },
            )
        except Exception as exc:
            error_code = set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            logger.exception(
                "Unhandled error in /v1/images/generations log_id=%s model=%s",
                getattr(request.state, "log_id", ""),
                resolved_model_id,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )

    def _openai_image_error_response(
        request: Request, exc: Exception, *, endpoint: str, model_label: str
    ) -> JSONResponse:
        if isinstance(exc, quota_error_cls):
            message, status_code, err_type = (
                "Token quota exhausted",
                429,
                "rate_limit_error",
            )
        elif isinstance(exc, auth_error_cls):
            message, status_code, err_type = (
                "Token invalid or expired",
                401,
                "authentication_error",
            )
        elif isinstance(exc, upstream_temp_error_cls):
            message, status_code, err_type = str(exc), 503, "server_error"
        elif isinstance(exc, HTTPException):
            message = str(exc.detail)
            status_code = int(exc.status_code)
            err_type = (
                "invalid_request_error" if 400 <= status_code < 500 else "server_error"
            )
        else:
            message, status_code, err_type = str(exc), 500, "server_error"
            logger.exception(
                "Unhandled error in %s log_id=%s model=%s",
                endpoint,
                getattr(request.state, "log_id", ""),
                model_label,
            )

        include_traceback = status_code == 500 and not isinstance(exc, HTTPException)
        error_code = ""
        if not isinstance(exc, HTTPException) and not include_traceback:
            error_code = str(getattr(request.state, "log_error_code", "") or "")
        if not error_code:
            error_code = set_request_error_detail(
                request,
                error=exc if include_traceback else message,
                status_code=status_code,
                error_type=err_type,
                include_traceback=include_traceback,
            )
        set_request_task_progress(
            request, task_status="FAILED", task_progress=0.0, error=message
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {"message": message, "type": err_type, "code": error_code}
            },
        )

    @router.post("/v1/responses")
    def openai_responses(request: Request, data: Any = Body(None)):
        require_service_api_key(request)

        image_model_ids = {
            model_id
            for model_id, config in model_catalog.items()
            if str(config.get("upstream_model_id") or "") == "gpt-image"
        }
        try:
            parsed = parse_responses_image_request(data, image_model_ids)
            ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
                {"size": parsed.size, "quality": parsed.quality}, parsed.image_model
            )
            model_conf = resolve_model(resolved_model_id)
            input_images = load_input_images(parsed.image_loader_messages())
        except ResponsesRequestError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                        "param": exc.param,
                    }
                },
            )
        except Exception as exc:
            return _openai_image_error_response(
                request,
                exc,
                endpoint="/v1/responses",
                model_label=(
                    str(data.get("model") or "")
                    if isinstance(data, dict)
                    else ""
                ),
            )

        set_request_logging_fields(request, resolved_model_id, parsed.prompt)
        set_request_credit_context(request, resolved_model_id, output_resolution)
        set_request_task_progress(request, task_status="IN_PROGRESS", task_progress=0.0)

        def _run_once(token: str):
            source_image_ids = [
                client.upload_image(token, image_bytes, image_mime)
                for image_bytes, image_mime in input_images
            ]

            def _image_progress_cb(update: dict):
                set_request_task_progress(
                    request,
                    task_status=str(update.get("task_status") or "IN_PROGRESS"),
                    task_progress=update.get("task_progress"),
                    upstream_job_id=update.get("upstream_job_id"),
                    retry_after=update.get("retry_after"),
                    error=update.get("error"),
                )

            artifact = generate_image_artifact(
                client=client,
                token=token,
                prompt=parsed.prompt,
                aspect_ratio=ratio,
                output_resolution=output_resolution,
                model_config=model_conf,
                generated_dir=generated_dir,
                source_image_ids=source_image_ids,
                progress_cb=_image_progress_cb,
                on_generated_file_written=on_generated_file_written,
            )
            image_url = public_image_url(request, artifact.job_id)
            set_request_preview(request, image_url, kind="image")
            result_b64 = encode_image_result(
                artifact.image_bytes,
                parsed.output_format,
                parsed.output_compression,
            )
            usage = build_image_usage(
                parsed.prompt,
                output_resolution,
                ratio,
                len(source_image_ids),
            )
            return build_responses_image_response(
                response_id=f"resp_{uuid.uuid4().hex}",
                item_id=f"ig_{uuid.uuid4().hex}",
                created_at=int(time.time()),
                model=parsed.inbound_model,
                result_b64=result_b64,
                usage=usage,
            )

        try:
            response_payload = run_with_token_retries(
                request=request,
                operation_name="responses.create",
                run_once=_run_once,
            )
            set_request_task_progress(request, task_status="COMPLETED", task_progress=100.0)
            if parsed.stream:
                return StreamingResponse(
                    iter_responses_image_sse(response_payload),
                    media_type="text/event-stream",
                )
            return response_payload
        except Exception as exc:
            return _openai_image_error_response(
                request,
                exc,
                endpoint="/v1/responses",
                model_label=resolved_model_id,
            )

    @router.post("/v1/images/edits")
    async def openai_edit(request: Request):
        require_service_api_key(request)

        try:
            form = await request.form()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "invalid multipart/form-data body",
                        "type": "invalid_request_error",
                    }
                },
            )

        def _bad_request(message: str) -> JSONResponse:
            set_request_error_detail(
                request,
                error=message,
                status_code=400,
                error_type="invalid_request_error",
                include_traceback=False,
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": message,
                        "type": "invalid_request_error",
                    }
                },
            )

        prompt = str(form.get("prompt") or "").strip()
        if not prompt:
            return _bad_request("prompt is required")

        # OpenAI 各版 SDK 的图片字段名不统一: image / image[] / image[N]
        image_key_re = re.compile(r"^image(\[\d*\])?$")
        uploads = [
            value
            for key, value in form.multi_items()
            if image_key_re.match(key) and hasattr(value, "read")
        ]
        if not uploads:
            return _bad_request("image is required")
        if len(uploads) > 6:
            return _bad_request("at most 6 input images are supported")

        input_images: list[tuple[bytes, str]] = []
        for upload in uploads:
            image_bytes = await upload.read()
            if not image_bytes:
                return _bad_request("image file is empty")
            if len(image_bytes) > 10 * 1024 * 1024:
                return _bad_request("image too large, max 10MB")
            input_images.append(
                (image_bytes, normalize_image_mime(upload.content_type))
            )

        if form.get("mask") is not None:
            # Firefly 上游没有 mask 局部重绘能力: 接受该字段但整图编辑
            logger.info(
                "images.edits: mask ignored (unsupported upstream) log_id=%s",
                getattr(request.state, "log_id", ""),
            )

        response_format = str(form.get("response_format") or "url").strip().lower()
        if response_format not in {"url", "b64_json"}:
            return _bad_request("response_format must be url or b64_json")

        model_id = str(form.get("model") or "").strip() or None
        if model_id in video_model_catalog:
            return _bad_request("Use /v1/chat/completions for video generation")

        try:
            ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
                {"size": form.get("size"), "quality": form.get("quality")}, model_id
            )
            model_conf = resolve_model(resolved_model_id)
        except HTTPException as exc:
            return _openai_image_error_response(
                request, exc, endpoint="/v1/images/edits", model_label=str(model_id)
            )
        # multipart body 不经过全局中间件的 JSON 字段提取,这里主动上报
        set_request_logging_fields(request, resolved_model_id, prompt)
        set_request_credit_context(request, resolved_model_id, output_resolution)

        def _execute():
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                source_image_ids = [
                    client.upload_image(token, image_bytes, image_mime)
                    for image_bytes, image_mime in input_images
                ]

                def _image_progress_cb(update: dict):
                    set_request_task_progress(
                        request,
                        task_status=str(update.get("task_status") or "IN_PROGRESS"),
                        task_progress=update.get("task_progress"),
                        upstream_job_id=update.get("upstream_job_id"),
                        retry_after=update.get("retry_after"),
                        error=update.get("error"),
                    )

                artifact = generate_image_artifact(
                    client=client,
                    token=token,
                    prompt=prompt,
                    aspect_ratio=ratio,
                    output_resolution=output_resolution,
                    model_config=model_conf,
                    generated_dir=generated_dir,
                    source_image_ids=source_image_ids,
                    progress_cb=_image_progress_cb,
                    on_generated_file_written=on_generated_file_written,
                )
                image_url = public_image_url(request, artifact.job_id)
                set_request_preview(request, image_url, kind="image")
                if response_format == "b64_json":
                    item = {
                        "b64_json": base64.b64encode(artifact.image_bytes).decode()
                    }
                else:
                    item = {"url": image_url}
                return {
                    "created": int(time.time()),
                    "model": resolved_model_id,
                    "data": [item],
                    "usage": build_image_usage(
                        prompt, output_resolution, ratio, len(source_image_ids)
                    ),
                }

            return run_with_token_retries(
                request=request,
                operation_name="images.edits",
                run_once=_run_once,
            )

        try:
            return await run_in_threadpool(_execute)
        except Exception as exc:
            return _openai_image_error_response(
                request,
                exc,
                endpoint="/v1/images/edits",
                model_label=str(resolved_model_id),
            )

    @router.post("/api/v1/generate")
    def create_job(data: GenerateRequest, request: Request):
        require_service_api_key(request)

        prompt = data.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt cannot be empty")

        ratio = data.aspect_ratio.strip() or "16:9"
        if ratio not in supported_ratios:
            raise HTTPException(status_code=400, detail="unsupported aspect ratio")

        output_resolution = (data.output_resolution or "2K").upper()
        if output_resolution not in {"1K", "2K", "4K"}:
            raise HTTPException(status_code=400, detail="unsupported output_resolution")

        model_conf = resolve_model(data.model)
        if data.model:
            output_resolution = model_conf["output_resolution"]

        resolved_model_id = str(data.model or "").strip()
        if not resolved_model_id:
            resolved_model_id = next(
                (
                    candidate_id
                    for candidate_id, candidate_conf in model_catalog.items()
                    if candidate_conf is model_conf
                ),
                "unknown",
            )
        set_request_credit_context(request, resolved_model_id, output_resolution)

        job = store.create(prompt=prompt, aspect_ratio=ratio)
        request_started = time.time()
        request_log_id = str(
            getattr(request.state, "log_id", "") or f"job-{job.id}"
        ).strip()
        tracking_request_id = f"{request_log_id}:background"

        def job_log_payload(
            *,
            status_code: int,
            task_status: str,
            token_meta: dict | None = None,
            token_attempt: int | None = None,
            preview_url: str | None = None,
            error: str | None = None,
        ) -> dict:
            meta = token_meta if isinstance(token_meta, dict) else {}
            return asdict(
                RequestLogRecord(
                    id=request_log_id,
                    ts=request_started,
                    method="POST",
                    path="/api/v1/generate",
                    status_code=int(status_code),
                    duration_sec=int(max(0.0, time.time() - request_started)),
                    operation="api.generate",
                    preview_url=preview_url,
                    preview_kind="image" if preview_url else None,
                    model=resolved_model_id,
                    prompt_preview=prompt[:180],
                    error=(str(error)[:240] if error else None),
                    task_status=str(task_status or "").upper() or None,
                    task_progress=100.0 if task_status == "COMPLETED" else None,
                    token_id=str(meta.get("token_id") or "") or None,
                    token_account_name=(
                        str(meta.get("token_account_name") or "") or None
                    ),
                    token_account_email=(
                        str(meta.get("token_account_email") or "") or None
                    ),
                    token_source=str(meta.get("token_source") or "") or None,
                    token_attempt=token_attempt,
                )
            )

        def runner(job_id: str):
            store.update(job_id, status="running", progress=5.0)
            max_attempts = client.retry_max_attempts if client.retry_enabled else 1
            max_attempts = max(1, int(max_attempts))
            last_error = "No active tokens available in the pool"
            last_status_code = 503
            last_token_meta: dict = {}
            last_attempt: int | None = None

            for attempt in range(1, max_attempts + 1):
                token = token_manager.get_available(
                    strategy=client.token_rotation_strategy
                )
                if not token:
                    break

                token_meta = token_manager.get_meta_by_value(token)
                token_id = str(token_meta.get("token_id") or "").strip()
                account_id = str(
                    token_meta.get("token_account_id") or ""
                ).strip()
                last_token_meta = token_meta
                last_attempt = attempt
                if token_id:
                    credits_tracker.begin(
                        token_id,
                        tracking_request_id,
                        account_id=account_id or None,
                    )

                try:
                    out_path = generated_dir / f"{job_id}.png"
                    old_size = 0
                    try:
                        if out_path.exists():
                            old_size = int(out_path.stat().st_size)
                    except Exception:
                        old_size = 0

                    image_bytes, meta = client.generate(
                        token=token,
                        prompt=prompt,
                        aspect_ratio=ratio,
                        output_resolution=output_resolution,
                        upstream_model_id=str(
                            model_conf.get("upstream_model_id") or "gemini-flash"
                        ),
                        upstream_model_version=str(
                            model_conf.get("upstream_model_version") or "nano-banana-2"
                        ),
                        quality_level=(
                            client.gpt_image_quality
                            if str(model_conf.get("upstream_model_id") or "") == "gpt-image"
                            else None
                        ),
                        detail_level=model_conf.get("detail_level"),
                        out_path=out_path,
                    )
                    if image_bytes is not None:
                        out_path.write_bytes(image_bytes)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    progress = float(meta.get("progress") or 100.0)
                    image_url = public_image_url(request, job_id)
                    store.update(
                        job_id,
                        status="succeeded",
                        progress=max(progress, 100.0),
                        image_url=image_url,
                    )
                    payload = job_log_payload(
                        status_code=200,
                        task_status="COMPLETED",
                        token_meta=token_meta,
                        token_attempt=attempt,
                        preview_url=image_url,
                    )
                    write_generation = request_log_store.upsert(request_log_id, payload)
                    if token_id:
                        credits_tracker.complete(
                            token_id=token_id,
                            account_id=account_id or None,
                            request_id=tracking_request_id,
                            log_id=request_log_id,
                            log_generation=write_generation,
                            payload=payload,
                            model_id=resolved_model_id,
                            output_resolution=output_resolution,
                        )
                    return
                except quota_error_cls:
                    token_manager.report_exhausted(token)
                    last_error = "Token quota exhausted."
                    last_status_code = 429
                    retryable = attempt < max_attempts
                except auth_error_cls:
                    token_manager.report_invalid(token)
                    last_error = "Token invalid or expired."
                    last_status_code = 401
                    retryable = attempt < max_attempts
                except upstream_temp_error_cls as exc:
                    last_error = str(exc)
                    last_status_code = 503
                    retryable = (
                        attempt < max_attempts
                        and client.should_retry_temporary_error(exc)
                    )
                except Exception as exc:
                    if token_id:
                        credits_tracker.finish(
                            token_id,
                            tracking_request_id,
                            account_id=account_id or None,
                            completed=False,
                        )
                    store.update(job_id, status="failed", error=str(exc))
                    request_log_store.upsert(
                        request_log_id,
                        job_log_payload(
                            status_code=500,
                            task_status="FAILED",
                            token_meta=token_meta,
                            token_attempt=attempt,
                            error=str(exc),
                        ),
                    )
                    return

                if token_id:
                    credits_tracker.finish(
                        token_id,
                        tracking_request_id,
                        account_id=account_id or None,
                        completed=False,
                    )

                if retryable:
                    delay = client._retry_delay_for_attempt(attempt)
                    if delay > 0:
                        time.sleep(delay)
                    continue
                break

            store.update(job_id, status="failed", error=last_error)
            request_log_store.upsert(
                request_log_id,
                job_log_payload(
                    status_code=last_status_code,
                    task_status="FAILED",
                    token_meta=last_token_meta,
                    token_attempt=last_attempt,
                    error=last_error,
                ),
            )

        return JSONResponse(
            content={"task_id": job.id, "status": job.status},
            background=BackgroundTask(runner, job.id),
        )

    @router.get("/api/v1/generate/{task_id}")
    def get_job(task_id: str, request: Request):
        require_service_api_key(request)

        job = store.get(task_id)
        if not job:
            raise HTTPException(status_code=404, detail="task not found")
        return asdict(job)

    @router.post("/v1/chat/completions")
    def chat_completions(data: dict, request: Request):
        require_service_api_key(request)

        prompt = extract_prompt_from_messages(data.get("messages") or [])
        if not prompt:
            prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "messages or prompt is required",
                        "type": "invalid_request_error",
                    }
                },
            )

        model_id = str(data.get("model") or "").strip()
        if (
            model_id.startswith("firefly-sora2")
            or model_id.startswith("firefly-veo31-fast")
            or model_id.startswith("firefly-veo31-")
            or model_id.startswith("firefly-kling-")
        ) and model_id not in video_model_catalog:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Invalid video model. Use /v1/models to get supported firefly-sora2-*, firefly-veo31-*, firefly-veo31-fast-* or firefly-kling-* models",
                        "type": "invalid_request_error",
                    }
                },
            )
        video_conf = video_model_catalog.get(model_id)
        is_video_model = video_conf is not None
        resolved_model_id = model_id if is_video_model else None
        ratio = "9:16"
        output_resolution = "2K"
        duration = int(video_conf["duration"]) if video_conf else 12
        video_resolution = (
            str(video_conf.get("resolution") or "720p") if video_conf else "720p"
        )
        if video_conf:
            ratio = str(video_conf.get("aspect_ratio") or ratio)
        video_engine = str(video_conf.get("engine") or "sora2") if video_conf else ""
        generate_audio = True
        negative_prompt = ""
        video_reference_mode = (
            str(video_conf.get("reference_mode") or "frame") if video_conf else "frame"
        )
        if is_video_model:
            resolved_video_options = resolve_video_options(data)
            if (
                isinstance(resolved_video_options, tuple)
                and len(resolved_video_options) == 3
            ):
                generate_audio, negative_prompt, requested_reference_mode = (
                    resolved_video_options
                )
                if "reference_mode" not in (video_conf or {}):
                    video_reference_mode = requested_reference_mode
            else:
                generate_audio, negative_prompt = resolved_video_options
            if not any(k in data for k in ("generate_audio", "generateAudio")):
                generate_audio = bool(video_conf.get("generate_audio", generate_audio))
        else:
            ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
                data, model_id or None
            )
        image_model_conf = (
            resolve_model(resolved_model_id) if not is_video_model else {}
        )
        set_request_credit_context(
            request,
            str(resolved_model_id or model_id),
            video_resolution if is_video_model else output_resolution,
        )

        try:
            entity_account_id = ""
            kling_bound_refs: list[dict] | None = None
            if video_engine == "kling-o3":
                entity_account_id, kling_bound_refs = _resolve_entity_bindings(prompt)
            input_images = load_input_images(data.get("messages") or [])
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                source_image_ids: list[str] = []
                image_url = ""
                response_content = ""

                if is_video_model:
                    if (
                        video_engine == "veo31-standard"
                        and video_reference_mode == "image"
                    ):
                        max_video_inputs = 3
                    else:
                        max_video_inputs = (
                            2
                            if video_engine
                            in {"veo31-fast", "veo31-standard", "kling-o3", "kling3"}
                            else 1
                        )
                    if len(input_images) > max_video_inputs:
                        raise HTTPException(
                            status_code=400,
                            detail=f"video model supports at most {max_video_inputs} input image(s)",
                        )
                    for image_bytes, _image_mime in input_images[:max_video_inputs]:
                        prepared_bytes, prepared_mime = prepare_video_source_image(
                            image_bytes,
                            ratio,
                            video_resolution,
                        )
                        source_image_ids.append(
                            client.upload_image(token, prepared_bytes, prepared_mime)
                        )

                    def _video_progress_cb(update: dict):
                        set_request_task_progress(
                            request,
                            task_status=str(update.get("task_status") or "IN_PROGRESS"),
                            task_progress=update.get("task_progress"),
                            upstream_job_id=update.get("upstream_job_id"),
                            retry_after=update.get("retry_after"),
                            error=update.get("error"),
                        )

                    video_prompt = prompt
                    entity_refs = None
                    if video_engine == "kling-o3":
                        video_prompt, entity_refs = _resolve_kling_entity_refs(
                            token, prompt, kling_bound_refs
                        )

                    job_id = uuid.uuid4().hex
                    generated_video = generate_video_file(
                        client=client,
                        token=token,
                        video_conf=video_conf or {},
                        prompt=video_prompt,
                        aspect_ratio=ratio,
                        duration=duration,
                        generated_dir=generated_dir,
                        task_id=job_id,
                        resolution=video_resolution,
                        source_image_ids=source_image_ids,
                        entity_refs=entity_refs,
                        timeout=max(int(client.generate_timeout), 600),
                        negative_prompt=negative_prompt,
                        generate_audio=generate_audio,
                        reference_mode=video_reference_mode,
                        progress_cb=_video_progress_cb,
                        on_generated_file_written=on_generated_file_written,
                    )
                    filename = generated_video.path.name
                    image_url = public_generated_url(request, filename)
                    set_request_preview(request, image_url, kind="video")
                    response_content = (
                        f"```html\n<video src='{image_url}' controls></video>\n```"
                    )
                else:
                    for image_bytes, image_mime in input_images:
                        source_image_ids.append(
                            client.upload_image(
                                token, image_bytes, image_mime or "image/jpeg"
                            )
                        )

                    def _image_progress_cb(update: dict):
                        set_request_task_progress(
                            request,
                            task_status=str(update.get("task_status") or "IN_PROGRESS"),
                            task_progress=update.get("task_progress"),
                            upstream_job_id=update.get("upstream_job_id"),
                            retry_after=update.get("retry_after"),
                            error=update.get("error"),
                        )

                    artifact = generate_image_artifact(
                        client=client,
                        token=token,
                        prompt=prompt,
                        aspect_ratio=ratio,
                        output_resolution=output_resolution,
                        model_config=image_model_conf,
                        generated_dir=generated_dir,
                        source_image_ids=source_image_ids,
                        progress_cb=_image_progress_cb,
                        on_generated_file_written=on_generated_file_written,
                    )
                    image_url = public_image_url(request, artifact.job_id)
                    set_request_preview(request, image_url, kind="image")
                    response_content = f"![Generated Image]({image_url})"

                response_payload = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": resolved_model_id,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_content,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    # 输入图按实际上传给上游的张数计费(最后一条 user 消息、
                    # 最多 6 张),而非请求里出现的所有图片字段
                    "usage": build_image_usage(
                        prompt, output_resolution, ratio, len(source_image_ids)
                    ),
                }
                if bool(data.get("stream", False)):
                    return StreamingResponse(
                        sse_chat_stream(response_payload),
                        media_type="text/event-stream",
                    )
                return response_payload

            token_selector = None
            if entity_account_id:
                token_selector = lambda: token_manager.get_available_for_account(
                    entity_account_id, strategy=client.token_rotation_strategy
                )
            return run_with_token_retries(
                request=request,
                operation_name="chat.completions",
                run_once=_run_once,
                token_selector=token_selector,
            )
        except quota_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token quota exhausted",
                status_code=429,
                error_type="rate_limit_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token quota exhausted",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Token quota exhausted",
                        "type": "rate_limit_error",
                        "code": error_code,
                    }
                },
            )
        except auth_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token invalid or expired",
                status_code=401,
                error_type="authentication_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token invalid or expired",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Token invalid or expired",
                        "type": "authentication_error",
                        "code": error_code,
                    }
                },
            )
        except upstream_temp_error_cls as exc:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=exc,
                status_code=503,
                error_type="server_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )
        except HTTPException as exc:
            err_type = (
                "invalid_request_error"
                if 400 <= int(exc.status_code) < 500
                else "server_error"
            )
            error_code = set_request_error_detail(
                request,
                error=str(exc.detail),
                status_code=exc.status_code,
                error_type=err_type,
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": err_type,
                        "code": error_code,
                    }
                },
            )
        except Exception as exc:
            error_code = set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            logger.exception(
                "Unhandled error in /v1/chat/completions log_id=%s model=%s resolved_model=%s is_video_model=%s",
                getattr(request.state, "log_id", ""),
                model_id,
                resolved_model_id,
                is_video_model,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )

    return router
