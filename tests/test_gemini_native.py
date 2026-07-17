from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path
from typing import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.models.gemini_usage as gemini_usage  # noqa: E402
from api.routes.gemini_native import build_gemini_native_router  # noqa: E402
from core.adobe_client import (  # noqa: E402
    AdobeRequestError,
    AuthError,
    QuotaExhaustedError,
    UpstreamTemporaryError,
)


class FakeConfig:
    def __init__(self, *, api_key: str = "test-key", deadline=500):
        self.values = {
            "api_key": api_key,
            "gemini_native_deadline_seconds": deadline,
        }

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class FakeAdobeClient:
    generate_timeout = 300

    def __init__(self, image_bytes: bytes = b"generated-png"):
        self.image_bytes = image_bytes
        self.upload_calls: list[dict] = []
        self.generate_calls: list[dict] = []
        self.fail_generate_for: dict[str, Exception] = {}
        self.write_partial_before_failure = False

    def upload_image(
        self,
        token: str,
        image_bytes: bytes,
        mime_type: str,
        deadline: float | None = None,
    ) -> str:
        self.upload_calls.append(
            {
                "token": token,
                "image_bytes": image_bytes,
                "mime_type": mime_type,
                "deadline": deadline,
            }
        )
        return f"{token}-image-{len(self.upload_calls)}"

    def generate(self, **kwargs):
        self.generate_calls.append(dict(kwargs))
        error = self.fail_generate_for.get(kwargs["token"])
        if error is not None:
            if self.write_partial_before_failure:
                kwargs["out_path"].write_bytes(b"partial")
            raise error
        kwargs["out_path"].write_bytes(self.image_bytes)
        return None, {"status": "SUCCEEDED", "outputs": [{"image": {}}]}


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        client: FakeAdobeClient | None = None,
        config: FakeConfig | None = None,
        retry_runner: Callable | None = None,
    ):
        self.client_impl = client or FakeAdobeClient()
        self.config = config or FakeConfig()
        self.accounted: list[tuple[Path, int, int]] = []
        self.previews: list[tuple[str, str]] = []
        self.logging_fields: list[tuple[str | None, str | None]] = []
        self.progress: list[dict] = []
        self.error_details: list[dict] = []

        def default_retry_runner(*, run_once, **kwargs):
            del kwargs
            return run_once("token-1")

        api = FastAPI()
        api.include_router(
            build_gemini_native_router(
                config_manager=self.config,
                client=self.client_impl,
                generated_dir=tmp_path,
                run_with_token_retries=retry_runner or default_retry_runner,
                set_request_error_detail=self._set_error_detail,
                set_request_task_progress=self._set_progress,
                set_request_logging_fields=self._set_logging_fields,
                set_request_preview=self._set_preview,
                public_image_url=lambda request, job_id: f"/generated/{job_id}.png",
                on_generated_file_written=self._account_file,
                quota_error_cls=QuotaExhaustedError,
                auth_error_cls=AuthError,
                upstream_temp_error_cls=UpstreamTemporaryError,
                adobe_error_cls=AdobeRequestError,
                logger=FakeLogger(),
            )
        )
        self.http = TestClient(api)

    def _set_error_detail(self, request, **kwargs):
        del request
        self.error_details.append(kwargs)
        return "ERR-TEST"

    def _set_progress(self, request, **kwargs):
        del request
        self.progress.append(kwargs)

    def _set_logging_fields(self, request, model, prompt):
        request.state.log_model = model
        request.state.log_prompt_preview = prompt
        self.logging_fields.append((model, prompt))

    def _set_preview(self, request, url, kind="image"):
        del request
        self.previews.append((url, kind))

    def _account_file(self, path: Path, old_size: int, new_size: int):
        self.accounted.append((path, old_size, new_size))


class FakeLogger:
    def __init__(self):
        self.exceptions: list[str] = []

    def exception(self, message: str):
        self.exceptions.append(message)


def image_request(
    *,
    text: str = "draw this",
    ratio: str = "1:1",
    size: str = "1K",
    inline_image: bytes | None = None,
) -> dict:
    parts: list[dict] = [{"text": text}]
    if inline_image is not None:
        parts.append(
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(inline_image).decode("ascii"),
                }
            }
        )
    return {
        "systemInstruction": {"parts": [{"text": "system"}]},
        "contents": [
            {"role": "user", "parts": parts},
            {"role": "model", "parts": [{"text": "history"}]},
        ],
        "generationConfig": {
            "imageConfig": {"aspectRatio": ratio, "imageSize": size},
            "candidateCount": 1,
        },
    }


def post(
    harness: Harness,
    model: str,
    action: str,
    payload: dict,
    *,
    key: str | None = "test-key",
    query_key: str | None = None,
):
    headers = {"content-type": "application/json"}
    if key is not None:
        headers["x-goog-api-key"] = key
    suffix = f"?key={query_key}" if query_key is not None else ""
    return harness.http.post(
        f"/v1beta/models/{model}:{action}{suffix}", headers=headers, json=payload
    )


def assert_google_error(response, code: int, status: str):
    assert response.status_code == code
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["status"] == status
    assert isinstance(response.json()["error"]["message"], str)


def test_auth_accepts_header_and_query_key_and_rejects_missing(tmp_path: Path):
    harness = Harness(tmp_path)

    assert post(
        harness,
        "gemini-2.0-flash",
        "countTokens",
        image_request(),
    ).status_code == 200
    assert post(
        harness,
        "gemini-2.0-flash",
        "countTokens",
        image_request(),
        key=None,
        query_key="test-key",
    ).status_code == 200
    assert_google_error(
        post(
            harness,
            "gemini-2.0-flash",
            "countTokens",
            image_request(),
            key=None,
        ),
        401,
        "UNAUTHENTICATED",
    )
    assert_google_error(
        post(
            harness,
            "gemini-2.0-flash",
            "countTokens",
            image_request(),
            key="wrong",
        ),
        401,
        "UNAUTHENTICATED",
    )


def test_model_list_and_single_model_use_the_same_registry(tmp_path: Path):
    harness = Harness(tmp_path)
    listing = harness.http.get(
        "/v1beta/models", headers={"x-goog-api-key": "test-key"}
    )

    assert listing.status_code == 200
    models = listing.json()["models"]
    assert len(models) == 8
    assert all(item["name"].startswith("models/") for item in models)
    assert all(
        item["supportedGenerationMethods"]
        == ["generateContent", "streamGenerateContent", "countTokens"]
        for item in models
    )

    model_id = "gemini-3-pro-image"
    single = harness.http.get(
        f"/v1beta/models/{model_id}", headers={"x-goog-api-key": "test-key"}
    )
    assert single.status_code == 200
    assert single.json() == next(
        item for item in models if item["name"] == f"models/{model_id}"
    )


def test_model_routes_return_google_not_found(tmp_path: Path):
    harness = Harness(tmp_path)
    assert_google_error(
        harness.http.get(
            "/v1beta/models/unknown", headers={"x-goog-api-key": "test-key"}
        ),
        404,
        "NOT_FOUND",
    )
    assert_google_error(
        post(harness, "unknown", "generateContent", image_request()),
        404,
        "NOT_FOUND",
    )
    assert_google_error(
        post(harness, "gemini-3-pro-image", "unknownAction", image_request()),
        404,
        "NOT_FOUND",
    )
    assert harness.client_impl.generate_calls == []


def test_count_tokens_flattens_all_text_and_prices_images_without_adobe(
    tmp_path: Path,
):
    harness = Harness(tmp_path)
    response = post(
        harness,
        "gemini-3-pro-image",
        "countTokens",
        image_request(inline_image=b"input"),
    )

    assert response.status_code == 200
    assert response.json()["totalTokens"] == 566
    assert response.json()["promptTokensDetails"] == [
        {"modality": "TEXT", "tokenCount": 6},
        {"modality": "IMAGE", "tokenCount": 560},
    ]
    assert harness.client_impl.upload_calls == []
    assert harness.client_impl.generate_calls == []
    assert harness.logging_fields == [
        ("gemini-3-pro-image", "system\ndraw this\nhistory")
    ]


@pytest.mark.parametrize(
    "model",
    [
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-3-pro-preview",
        "gemini-3.1-pro-preview",
    ],
)
def test_text_health_models_return_deterministic_canned_response(
    tmp_path: Path, model: str
):
    harness = Harness(tmp_path)
    response = post(harness, model, "generateContent", image_request())

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidates"][0]["content"]["parts"] == [{"text": "ok"}]
    assert payload["candidates"][0]["finishReason"] == "STOP"
    assert payload["modelVersion"] == model
    assert payload["usageMetadata"]["candidatesTokenCount"] == 1
    assert payload["usageMetadata"]["serviceTier"] == "standard"
    assert harness.client_impl.generate_calls == []


def test_text_model_rejects_inline_image_without_adobe(tmp_path: Path):
    harness = Harness(tmp_path)
    response = post(
        harness,
        "gemini-2.0-flash",
        "generateContent",
        image_request(inline_image=b"input"),
    )

    assert_google_error(response, 400, "INVALID_ARGUMENT")
    assert harness.client_impl.upload_calls == []
    assert harness.client_impl.generate_calls == []


def test_pro_generation_reads_disk_and_returns_inline_png(
    monkeypatch, tmp_path: Path
):
    values = iter([90, 155])
    monkeypatch.setattr(
        gemini_usage, "gemini_usage_rand", lambda low, high: next(values)
    )
    harness = Harness(tmp_path)
    response = post(
        harness,
        "gemini-3-pro-image-preview",
        "generateContent",
        image_request(size="2K", inline_image=b"input"),
    )

    assert response.status_code == 200
    payload = response.json()
    candidate = payload["candidates"][0]
    assert candidate["index"] == 0
    assert candidate["content"]["role"] == "model"
    assert candidate["finishReason"] == "STOP"
    assert base64.b64decode(
        candidate["content"]["parts"][0]["inlineData"]["data"], validate=True
    ) == b"generated-png"
    assert payload["modelVersion"] == "gemini-3-pro-image-preview"
    assert payload["usageMetadata"]["thoughtsTokenCount"] == 155
    assert payload["usageMetadata"]["serviceTier"] == "standard"
    assert "trafficType" not in payload["usageMetadata"]
    assert len(harness.accounted) == 1
    assert harness.accounted[0][2] == len(b"generated-png")
    assert harness.previews[0][1] == "image"


def test_flash_generation_omits_index_and_has_flash_identity(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(gemini_usage, "gemini_usage_rand", lambda low, high: 411)
    harness = Harness(tmp_path)
    response = post(
        harness,
        "gemini-3.1-flash-image",
        "generateContent",
        image_request(size="2K"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert "index" not in payload["candidates"][0]
    assert payload["usageMetadata"]["trafficType"] == "ON_DEMAND"
    assert "thoughtsTokenCount" not in payload["usageMetadata"]
    assert "serviceTier" not in payload["usageMetadata"]
    assert payload["usageMetadata"]["candidatesTokensDetails"] == [
        {"modality": "TEXT", "tokenCount": 411},
        {"modality": "IMAGE", "tokenCount": 1680},
    ]


def test_stream_success_is_one_complete_data_event_without_done(tmp_path: Path):
    harness = Harness(tmp_path)
    response = post(
        harness,
        "gemini-2.0-flash",
        "streamGenerateContent",
        image_request(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("data: ") == 1
    assert response.text.endswith("\n\n")
    assert "[DONE]" not in response.text
    event_payload = json.loads(response.text.removeprefix("data: ").strip())
    assert event_payload["candidates"][0]["content"]["parts"] == [{"text": "ok"}]


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (QuotaExhaustedError("quota"), 429, "RESOURCE_EXHAUSTED"),
        (AuthError("auth"), 401, "UNAUTHENTICATED"),
        (
            UpstreamTemporaryError("temporary", status_code=503),
            503,
            "UNAVAILABLE",
        ),
        (AdobeRequestError("job failed"), 500, "INTERNAL"),
        (RuntimeError("unexpected"), 500, "INTERNAL"),
    ],
)
def test_generation_errors_map_before_stream_opens(
    tmp_path: Path, error: Exception, code: int, status: str
):
    def raise_error(**kwargs):
        del kwargs
        raise error

    harness = Harness(tmp_path, retry_runner=raise_error)
    response = post(
        harness,
        "gemini-3-pro-image",
        "streamGenerateContent",
        image_request(),
    )

    assert_google_error(response, code, status)
    assert not response.headers["content-type"].startswith("text/event-stream")
    if isinstance(error, RuntimeError):
        assert len(harness.error_details) == 1
        assert harness.error_details[0]["include_traceback"] is True


def test_retry_reuploads_images_for_the_second_token(tmp_path: Path):
    client = FakeAdobeClient()
    client.fail_generate_for["token-1"] = UpstreamTemporaryError(
        "temporary", status_code=503
    )

    def retry_twice(*, run_once, **kwargs):
        del kwargs
        try:
            return run_once("token-1")
        except UpstreamTemporaryError:
            return run_once("token-2")

    harness = Harness(tmp_path, client=client, retry_runner=retry_twice)
    response = post(
        harness,
        "gemini-3-pro-image",
        "generateContent",
        image_request(inline_image=b"input"),
    )

    assert response.status_code == 200
    assert [call["token"] for call in client.upload_calls] == ["token-1", "token-2"]
    assert [call["token"] for call in client.generate_calls] == ["token-1", "token-2"]
    assert list(tmp_path.glob("*.png"))


def test_failed_generation_deletes_partial_file_and_skips_accounting(tmp_path: Path):
    client = FakeAdobeClient()
    client.fail_generate_for["token-1"] = AdobeRequestError("failed")
    client.write_partial_before_failure = True
    harness = Harness(tmp_path, client=client)
    response = post(
        harness,
        "gemini-3-pro-image",
        "generateContent",
        image_request(),
    )

    assert_google_error(response, 500, "INTERNAL")
    assert list(tmp_path.glob("*.png")) == []
    assert harness.accounted == []


def test_generation_passes_one_absolute_deadline_to_upload_and_generate(
    tmp_path: Path,
):
    harness = Harness(tmp_path)
    response = post(
        harness,
        "gemini-3-pro-image",
        "generateContent",
        image_request(inline_image=b"input"),
    )

    assert response.status_code == 200
    upload_deadline = harness.client_impl.upload_calls[0]["deadline"]
    generate_deadline = harness.client_impl.generate_calls[0]["deadline"]
    assert upload_deadline == generate_deadline
    assert isinstance(upload_deadline, float)


@pytest.mark.parametrize("deadline", [0, -1, True, "invalid"])
def test_invalid_deadline_configuration_returns_internal(
    tmp_path: Path, deadline
):
    harness = Harness(tmp_path, config=FakeConfig(deadline=deadline))
    response = post(
        harness,
        "gemini-3-pro-image",
        "generateContent",
        image_request(),
    )

    assert_google_error(response, 500, "INTERNAL")
    assert harness.client_impl.generate_calls == []


def test_non_stream_large_base64_response_is_complete_in_process(tmp_path: Path):
    image_bytes = b"x" * (6 * 1024 * 1024)
    harness = Harness(tmp_path, client=FakeAdobeClient(image_bytes))
    response = post(
        harness,
        "gemini-3-pro-image",
        "generateContent",
        image_request(),
    )

    assert response.status_code == 200
    encoded = response.json()["candidates"][0]["content"]["parts"][0][
        "inlineData"
    ]["data"]
    assert len(encoded) >= 8 * 1024 * 1024
    assert base64.b64decode(encoded, validate=True) == image_bytes


class CaptureLogStore:
    def __init__(self):
        self.records: list[dict] = []

    def add_payload(self, payload: dict):
        self.records.append(dict(payload))

    def upsert(self, log_id: str, payload: dict):
        stored = dict(payload)
        stored["id"] = log_id
        self.records.append(stored)


class CaptureLiveStore:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def upsert(self, log_id: str, payload: dict):
        self.items[log_id] = {**self.items.get(log_id, {}), **payload}

    def remove(self, log_id: str):
        self.items.pop(log_id, None)


class CaptureErrorStore:
    def __init__(self):
        self.records: list[object] = []

    def add(self, record):
        self.records.append(record)


def import_full_app():
    if "app" in sys.modules:
        return sys.modules["app"]
    from core.refresh_mgr import refresh_manager

    original_start = refresh_manager.start
    refresh_manager.start = lambda: None
    try:
        return importlib.import_module("app")
    finally:
        refresh_manager.start = original_start


def patch_full_app_state(monkeypatch):
    app_module = import_full_app()

    log_store = CaptureLogStore()
    live_store = CaptureLiveStore()
    error_store = CaptureErrorStore()
    monkeypatch.setattr(app_module, "log_store", log_store)
    monkeypatch.setattr(app_module, "live_log_store", live_store)
    monkeypatch.setattr(app_module, "error_store", error_store)
    original_get = app_module.config_manager.get

    def config_get(key: str, default=None):
        if key == "api_key":
            return "test-key"
        return original_get(key, default)

    monkeypatch.setattr(app_module.config_manager, "get", config_get)
    return app_module, TestClient(app_module.app), log_store, live_store, error_store


def test_full_app_resolves_every_gemini_operation():
    app_module = import_full_app()

    assert app_module._resolve_request_operation("GET", "/v1beta/models") == (
        "gemini.models.list"
    )
    assert app_module._resolve_request_operation(
        "GET", "/v1beta/models/gemini-3-pro-image"
    ) == "gemini.models.get"
    assert app_module._resolve_request_operation(
        "POST", "/v1beta/models/gemini-3-pro-image:generateContent"
    ) == "gemini.generateContent"
    assert app_module._resolve_request_operation(
        "POST", "/v1beta/models/gemini-3-pro-image:streamGenerateContent"
    ) == "gemini.streamGenerateContent"
    assert app_module._resolve_request_operation(
        "POST", "/v1beta/models/gemini-3-pro-image:countTokens"
    ) == "gemini.countTokens"


def test_full_app_logs_gemini_paths_without_base64(monkeypatch):
    _app, http, logs, live, errors = patch_full_app_state(monkeypatch)
    marker_bytes = b"SENSITIVE_BASE64_MARKER"
    requests = [
        http.get("/v1beta/models", headers={"x-goog-api-key": "test-key"}),
        http.get(
            "/v1beta/models/gemini-3-pro-image",
            headers={"x-goog-api-key": "test-key"},
        ),
        http.post(
            "/v1beta/models/gemini-2.0-flash:generateContent",
            headers={"x-goog-api-key": "test-key"},
            json={"contents": [{"parts": [{"text": "health prompt"}]}]},
        ),
        http.post(
            "/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse",
            headers={"x-goog-api-key": "test-key"},
            json={"contents": [{"parts": [{"text": "stream prompt"}]}]},
        ),
        http.post(
            "/v1beta/models/gemini-3-pro-image:countTokens",
            headers={"x-goog-api-key": "test-key"},
            json={
                "contents": [
                    {
                        "parts": [
                            {"text": "count prompt"},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(marker_bytes).decode("ascii"),
                                }
                            },
                        ]
                    }
                ]
            },
        ),
        http.post(
            "/v1beta/models/gemini-3-pro-image:generateContent",
            headers={
                "x-goog-api-key": "test-key",
                "content-type": "application/json",
            },
            content=b"[]",
        ),
    ]

    assert [response.status_code for response in requests] == [200, 200, 200, 200, 200, 400]
    operations = {record["operation"] for record in logs.records}
    assert {
        "gemini.models.list",
        "gemini.models.get",
        "gemini.generateContent",
        "gemini.streamGenerateContent",
        "gemini.countTokens",
    } <= operations
    generate_record = next(
        record
        for record in logs.records
        if record["operation"] == "gemini.generateContent"
        and record["status_code"] == 200
    )
    assert generate_record["model"] == "gemini-2.0-flash"
    assert generate_record["prompt_preview"] == "health prompt"
    count_record = next(
        record
        for record in logs.records
        if record["operation"] == "gemini.countTokens"
    )
    assert count_record["model"] == "gemini-3-pro-image"
    assert count_record["prompt_preview"] == "count prompt"
    assert live.items == {}
    serialized = repr(logs.records) + repr(errors.records)
    assert "SENSITIVE_BASE64_MARKER" not in serialized
    assert base64.b64encode(marker_bytes).decode("ascii") not in serialized


def test_gemini_middleware_never_calls_unbounded_request_body(
    monkeypatch,
):
    from starlette.requests import Request

    _app, http, _logs, _live, _errors = patch_full_app_state(monkeypatch)

    async def forbidden_body(self):
        del self
        raise AssertionError("Gemini middleware must not call Request.body")

    monkeypatch.setattr(Request, "body", forbidden_body)
    response = http.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        headers={"x-goog-api-key": "test-key"},
        json={"contents": [{"parts": [{"text": "health"}]}]},
    )

    assert response.status_code == 200
    assert response.json()["candidates"][0]["content"]["parts"] == [{"text": "ok"}]


def test_legacy_chat_logging_still_reads_and_replays_body(monkeypatch):
    app_module, http, logs, _live, _errors = patch_full_app_state(monkeypatch)
    monkeypatch.setattr(
        app_module.token_manager,
        "get_available",
        lambda strategy: None,
    )
    response = http.post(
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={
            "model": "firefly-nano-banana-pro",
            "messages": [{"role": "user", "content": "legacy prompt"}],
        },
    )

    assert response.status_code == 503
    record = next(
        item for item in logs.records if item["operation"] == "chat.completions"
    )
    assert record["model"] == "firefly-nano-banana-pro"
    assert record["prompt_preview"] == "legacy prompt"
