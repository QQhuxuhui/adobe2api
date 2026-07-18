from __future__ import annotations

import asyncio
import time
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routes.admin import build_admin_router
from api.schemas import ConfigUpdateRequest
import core.config_mgr as config_mgr_module
from core.adobe_client import (
    AdobeRequestError,
    AuthError,
    QuotaExhaustedError,
    UpstreamTemporaryError,
)
from core.refresh_mgr import refresh_manager

_refresh_start = refresh_manager.start
refresh_manager.start = lambda: None
try:
    import app as app_module  # noqa: E402
finally:
    refresh_manager.start = _refresh_start


class FakeClient:
    retry_enabled = True
    retry_max_attempts = 3
    retry_backoff_seconds = 1.0
    token_rotation_strategy = "round_robin"

    def __init__(self, delay: float = 0.0):
        self.delay = delay

    def should_retry_temporary_error(self, exc: UpstreamTemporaryError) -> bool:
        return True

    def _retry_delay_for_attempt(self, attempt: int) -> float:
        return self.delay


class FakeTokenManager:
    def __init__(self, tokens: list[str], auth_status: str = "invalid"):
        self.tokens = list(tokens)
        self.auth_status = auth_status
        self.selection_calls = 0
        self.successes: list[str] = []
        self.exhausted: list[str] = []
        self.auth_failures: list[str] = []
        self.attempt_logs: list[dict] = []
        self.progress_updates: list[dict] = []

    def get_available(self, strategy: str) -> str | None:
        self.selection_calls += 1
        return self.tokens.pop(0) if self.tokens else None

    def get_meta_by_value(self, token: str) -> dict:
        return {"token": token}

    def report_success(self, token: str) -> None:
        self.successes.append(token)

    def report_exhausted(self, token: str) -> None:
        self.exhausted.append(token)

    def handle_auth_failure(self, token: str) -> dict:
        self.auth_failures.append(token)
        return {"status": self.auth_status}


def make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/v1beta/models/gemini-3-pro-image:generateContent",
            "query_string": b"",
            "headers": [],
        }
    )


class FakeCreditsTracker:
    def __init__(self):
        self.begins: list[tuple[str, str]] = []
        self.finishes: list[tuple[str, str, bool]] = []
        self.completions: list[dict] = []

    def begin(
        self, token_id: str, request_id: str, *, account_id: str | None = None
    ) -> None:
        del account_id
        self.begins.append((token_id, request_id))

    def finish(
        self,
        token_id: str,
        request_id: str,
        *,
        account_id: str | None = None,
        completed: bool = False,
    ) -> None:
        del account_id
        self.finishes.append((token_id, request_id, completed))

    def complete(self, **kwargs) -> None:
        self.completions.append(kwargs)


class TokenMetadata:
    def get_meta_by_value(self, token: str) -> dict:
        return {
            "token_id": token,
            "token_account_id": f"account-{token}",
            "token_account_name": token,
            "token_account_email": f"{token}@example.com",
            "token_source": "manual",
        }


class MiddlewareLogStore:
    def __init__(self):
        self.records: list[dict] = []

    def add_payload(self, payload: dict) -> None:
        self.records.append(dict(payload))

    def upsert(self, log_id: str, payload: dict) -> None:
        self.records.append({**payload, "id": log_id})


class MiddlewareLiveStore:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def upsert(self, log_id: str, payload: dict) -> None:
        self.items[log_id] = {**self.items.get(log_id, {}), **payload}

    def remove(self, log_id: str) -> None:
        self.items.pop(log_id, None)


def test_token_context_tracks_first_binding_and_finishes_before_retry_switch(
    monkeypatch,
):
    tracker = FakeCreditsTracker()
    request = make_request()
    request.state.log_id = "root-log"
    monkeypatch.setattr(app_module, "token_manager", TokenMetadata())
    monkeypatch.setattr(app_module, "credits_tracker", tracker, raising=False)

    app_module._set_request_token_context(request, "token-1", 1)
    app_module._set_request_token_context(request, "token-2", 2)

    assert tracker.begins == [("token-1", "root-log"), ("token-2", "root-log")]
    assert tracker.finishes == [("token-1", "root-log", False)]


def test_middleware_submits_final_successful_attempt_for_credit_measurement(
    monkeypatch,
):
    tracker = FakeCreditsTracker()
    logs = MiddlewareLogStore()
    live = MiddlewareLiveStore()
    monkeypatch.setattr(app_module, "credits_tracker", tracker, raising=False)
    monkeypatch.setattr(app_module, "log_store", logs)
    monkeypatch.setattr(app_module, "live_log_store", live)
    request = make_request()
    success_payload = {
        "id": "root-a2",
        "ts": 10,
        "status_code": 200,
        "task_status": "COMPLETED",
        "model": "firefly-gpt-image",
        "token_id": "token-2",
    }

    async def call_next(req: Request):
        root_id = req.state.log_id
        req.state.log_credit_token_id = "token-2"
        req.state.log_credit_request_id = root_id
        req.state.log_output_resolution = "4K"
        req.state.log_has_attempt_logs = True
        req.state.log_attempt_records = [
            {**success_payload, "id": f"{root_id}-a2"}
        ]
        return Response(status_code=200)

    asyncio.run(app_module.request_logger(request, call_next))

    assert len(tracker.completions) == 1
    completion = tracker.completions[0]
    assert completion["log_id"].endswith("-a2")
    assert completion["token_id"] == "token-2"
    assert completion["model_id"] == "firefly-gpt-image"
    assert completion["output_resolution"] == "4K"
    assert completion["payload"]["status_code"] == 200


def test_middleware_failure_releases_tracking_without_measurement(monkeypatch):
    tracker = FakeCreditsTracker()
    monkeypatch.setattr(app_module, "credits_tracker", tracker, raising=False)
    monkeypatch.setattr(app_module, "log_store", MiddlewareLogStore())
    monkeypatch.setattr(app_module, "live_log_store", MiddlewareLiveStore())
    request = make_request()

    async def call_next(req: Request):
        root_id = req.state.log_id
        req.state.log_credit_token_id = "token-1"
        req.state.log_credit_request_id = root_id
        req.state.log_has_attempt_logs = True
        req.state.log_attempt_records = [
            {
                "id": f"{root_id}-a1",
                "ts": 10,
                "status_code": 500,
                "task_status": "FAILED",
                "model": "firefly-gpt-image",
                "token_id": "token-1",
            }
        ]
        return Response(status_code=500)

    asyncio.run(app_module.request_logger(request, call_next))

    assert tracker.completions == []
    assert len(tracker.finishes) == 1
    assert tracker.finishes[0][0] == "token-1"
    assert tracker.finishes[0][2] is False


def test_middleware_final_http_failure_does_not_measure_successful_attempt(
    monkeypatch,
):
    tracker = FakeCreditsTracker()
    monkeypatch.setattr(app_module, "credits_tracker", tracker, raising=False)
    monkeypatch.setattr(app_module, "log_store", MiddlewareLogStore())
    monkeypatch.setattr(app_module, "live_log_store", MiddlewareLiveStore())
    request = make_request()

    async def call_next(req: Request):
        root_id = req.state.log_id
        req.state.log_credit_token_id = "token-1"
        req.state.log_credit_account_id = "account-1"
        req.state.log_credit_request_id = root_id
        req.state.log_has_attempt_logs = True
        req.state.log_attempt_records = [
            {
                "id": f"{root_id}-a1",
                "ts": 10,
                "status_code": 200,
                "task_status": "COMPLETED",
                "model": "firefly-gpt-image",
                "token_id": "token-1",
            }
        ]
        return Response(status_code=500)

    asyncio.run(app_module.request_logger(request, call_next))

    assert tracker.completions == []
    assert len(tracker.finishes) == 1


@pytest.fixture
def retry_env(monkeypatch):
    tokens = FakeTokenManager(["token-1"])
    client = FakeClient()
    monkeypatch.setattr(app_module, "token_manager", tokens)
    monkeypatch.setattr(app_module, "client", client)
    monkeypatch.setattr(
        app_module,
        "_set_request_token_context",
        lambda request, token, attempt: {"token": token},
    )
    monkeypatch.setattr(
        app_module,
        "_append_attempt_log",
        lambda **kwargs: tokens.attempt_logs.append(kwargs),
    )
    monkeypatch.setattr(
        app_module,
        "_set_request_task_progress",
        lambda request, **kwargs: tokens.progress_updates.append(kwargs),
    )
    return tokens, client


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (QuotaExhaustedError("quota"), QuotaExhaustedError),
        (AuthError("auth"), AuthError),
        (
            UpstreamTemporaryError("temporary", status_code=503, error_type="status"),
            UpstreamTemporaryError,
        ),
    ],
)
def test_domain_mode_reraises_original_retry_exception(
    retry_env, error: Exception, expected_type: type[Exception]
):
    with pytest.raises(expected_type) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=lambda token: (_ for _ in ()).throw(error),
            set_request_error_detail=lambda *args, **kwargs: "ERR-TEST",
            reraise_domain=True,
        )

    assert caught.value is error


def test_domain_mode_turns_empty_pool_into_temporary_error(monkeypatch):
    tokens = FakeTokenManager([])
    monkeypatch.setattr(app_module, "token_manager", tokens)
    monkeypatch.setattr(app_module, "client", FakeClient())

    with pytest.raises(UpstreamTemporaryError) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=lambda token: token,
            reraise_domain=True,
        )

    assert caught.value.status_code == 503
    assert caught.value.error_type == "upstream_unavailable"


def test_quota_and_auth_failures_keep_token_health_and_attempt_logs(
    retry_env, monkeypatch
):
    tokens, _client = retry_env

    with pytest.raises(QuotaExhaustedError):
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=lambda token: (_ for _ in ()).throw(
                QuotaExhaustedError("quota")
            ),
            set_request_error_detail=lambda *args, **kwargs: "ERR-TEST",
            reraise_domain=True,
        )

    assert tokens.exhausted == ["token-1"]
    assert tokens.attempt_logs[0]["status_code"] == 429
    assert tokens.progress_updates[0]["task_status"] == "IN_PROGRESS"

    auth_tokens = FakeTokenManager(["token-2"])
    monkeypatch.setattr(app_module, "token_manager", auth_tokens)
    monkeypatch.setattr(
        app_module,
        "_append_attempt_log",
        lambda **kwargs: auth_tokens.attempt_logs.append(kwargs),
    )
    monkeypatch.setattr(
        app_module,
        "_set_request_task_progress",
        lambda request, **kwargs: auth_tokens.progress_updates.append(kwargs),
    )

    with pytest.raises(AuthError):
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=lambda token: (_ for _ in ()).throw(AuthError("auth")),
            set_request_error_detail=lambda *args, **kwargs: "ERR-TEST",
            reraise_domain=True,
        )

    assert auth_tokens.auth_failures == ["token-2"]
    assert auth_tokens.attempt_logs[0]["status_code"] == 401


def test_default_mode_keeps_empty_pool_http_exception(monkeypatch):
    monkeypatch.setattr(app_module, "token_manager", FakeTokenManager([]))
    monkeypatch.setattr(app_module, "client", FakeClient())

    with pytest.raises(HTTPException) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="chat.completions",
            run_once=lambda token: token,
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == "No active tokens available in the pool"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (QuotaExhaustedError("quota"), 503),
        (AuthError("auth"), 401),
        (
            UpstreamTemporaryError("temporary", status_code=503, error_type="status"),
            503,
        ),
    ],
)
def test_default_mode_keeps_existing_domain_http_mapping(
    retry_env, error: Exception, expected_status: int
):
    with pytest.raises(HTTPException) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="chat.completions",
            run_once=lambda token: (_ for _ in ()).throw(error),
            set_request_error_detail=lambda *args, **kwargs: "ERR-TEST",
        )

    assert caught.value.status_code == expected_status


def test_adobe_error_is_only_reraised_in_domain_mode(retry_env):
    error = AdobeRequestError(
        "bad upstream job",
        status_code=422,
        error_type="invalid_request_error",
    )

    with pytest.raises(AdobeRequestError) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=lambda token: (_ for _ in ()).throw(error),
            set_request_error_detail=lambda *args, **kwargs: "ERR-TEST",
            reraise_domain=True,
        )
    assert caught.value is error


def test_default_mode_still_wraps_adobe_error(retry_env):
    error = AdobeRequestError("bad upstream job", status_code=422)

    with pytest.raises(HTTPException) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="chat.completions",
            run_once=lambda token: (_ for _ in ()).throw(error),
            set_request_error_detail=lambda *args, **kwargs: "ERR-TEST",
        )

    assert caught.value.status_code == 422
    assert caught.value.detail == "bad upstream job"


def test_expired_deadline_stops_before_token_selection(monkeypatch):
    tokens = FakeTokenManager(["token-1"])
    monkeypatch.setattr(app_module, "token_manager", tokens)
    monkeypatch.setattr(app_module, "client", FakeClient())

    with pytest.raises(UpstreamTemporaryError) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=lambda token: token,
            reraise_domain=True,
            deadline=time.monotonic() - 1,
        )

    assert caught.value.error_type == "timeout"
    assert tokens.selection_calls == 0


def test_deadline_is_rechecked_immediately_before_run_once(monkeypatch):
    tokens = FakeTokenManager(["token-1"])
    monkeypatch.setattr(app_module, "token_manager", tokens)
    monkeypatch.setattr(app_module, "client", FakeClient())
    monkeypatch.setattr(
        app_module,
        "_set_request_token_context",
        lambda request, token, attempt: {"token": token},
    )
    clock = {"now": 100.0}
    original_get_available = tokens.get_available

    def select_after_budget(strategy: str):
        token = original_get_available(strategy)
        clock["now"] = 101.0
        return token

    monkeypatch.setattr(tokens, "get_available", select_after_budget)
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock["now"])
    run_calls: list[str] = []

    with pytest.raises(UpstreamTemporaryError) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=lambda token: run_calls.append(token),
            reraise_domain=True,
            deadline=100.5,
        )

    assert caught.value.error_type == "timeout"
    assert run_calls == []


def test_retry_sleep_is_capped_by_remaining_deadline(monkeypatch):
    tokens = FakeTokenManager(["token-1", "token-2"])
    monkeypatch.setattr(app_module, "token_manager", tokens)
    monkeypatch.setattr(app_module, "client", FakeClient(delay=10.0))
    monkeypatch.setattr(
        app_module,
        "_set_request_token_context",
        lambda request, token, attempt: {"token": token},
    )
    monkeypatch.setattr(app_module, "_append_attempt_log", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "_set_request_task_progress", lambda *args, **kwargs: None)

    clock = {"now": 100.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)
    attempts: list[str] = []

    def run_once(token: str):
        attempts.append(token)
        raise UpstreamTemporaryError("temporary", status_code=503, error_type="status")

    with pytest.raises(UpstreamTemporaryError) as caught:
        app_module._run_with_token_retries(
            request=make_request(),
            operation_name="gemini.generateContent",
            run_once=run_once,
            set_request_error_detail=lambda *args, **kwargs: "ERR-TEST",
            reraise_domain=True,
            deadline=100.25,
        )

    assert caught.value.error_type == "timeout"
    assert attempts == ["token-1"]
    assert sleeps == [pytest.approx(0.25)]


def test_success_reporting_is_unchanged(retry_env):
    tokens, _client = retry_env

    result = app_module._run_with_token_retries(
        request=make_request(),
        operation_name="chat.completions",
        run_once=lambda token: "ok",
    )

    assert result == "ok"
    assert tokens.successes == ["token-1"]


class FakeConfigManager:
    def __init__(self):
        self.data = {
            "gemini_native_deadline_seconds": 500,
            "generated_max_size_mb": 1024,
            "generated_prune_size_mb": 200,
        }

    def get_all(self) -> dict:
        return dict(self.data)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def update_all(self, values: dict) -> None:
        self.data.update(values)


def make_admin_client(config: FakeConfigManager) -> TestClient:
    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=object(),
            config_manager=config,
            refresh_manager=object(),
            log_store=object(),
            error_store=object(),
            live_log_store=object(),
            require_admin_auth=lambda request: None,
            is_admin_authenticated=lambda request: True,
            apply_client_config=lambda: None,
            get_generated_storage_stats=lambda: {},
        )
    )
    return TestClient(api)


def test_deadline_config_is_in_schema_and_defaults_to_500(monkeypatch, tmp_path: Path):
    request = ConfigUpdateRequest(gemini_native_deadline_seconds=321)
    assert request.model_dump(exclude_unset=True) == {
        "gemini_native_deadline_seconds": 321
    }
    config_dir = tmp_path / "config"
    monkeypatch.setattr(config_mgr_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_mgr_module, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.setattr(
        config_mgr_module, "LEGACY_CONFIG_FILE", tmp_path / "data" / "config.json"
    )
    manager = config_mgr_module.ConfigManager()
    assert manager.get("gemini_native_deadline_seconds") == 500


def test_deadline_schema_rejects_boolean():
    with pytest.raises(ValueError):
        ConfigUpdateRequest(gemini_native_deadline_seconds=True)


@pytest.mark.parametrize("value", [0, -1])
def test_admin_rejects_non_positive_deadline(value: int):
    config = FakeConfigManager()
    response = make_admin_client(config).put(
        "/api/v1/config", json={"gemini_native_deadline_seconds": value}
    )

    assert response.status_code == 400
    assert "positive integer" in response.json()["detail"]
    assert config.data["gemini_native_deadline_seconds"] == 500


def test_admin_persists_positive_deadline():
    config = FakeConfigManager()
    response = make_admin_client(config).put(
        "/api/v1/config", json={"gemini_native_deadline_seconds": 420}
    )

    assert response.status_code == 200
    assert response.json()["gemini_native_deadline_seconds"] == 420
    assert config.data["gemini_native_deadline_seconds"] == 420


def test_admin_rejects_boolean_deadline():
    config = FakeConfigManager()
    response = make_admin_client(config).put(
        "/api/v1/config", json={"gemini_native_deadline_seconds": True}
    )

    assert response.status_code == 422
    assert config.data["gemini_native_deadline_seconds"] == 500
