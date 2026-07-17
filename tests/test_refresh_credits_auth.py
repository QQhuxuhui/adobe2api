from __future__ import annotations

from pathlib import Path
import sys
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.refresh_mgr as refresh_mgr_module
from api.routes.admin import build_admin_router
from core.refresh_mgr import CreditsAuthError, RefreshManager
from core.token_mgr import TokenManager


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def make_refresh_manager() -> RefreshManager:
    manager = object.__new__(RefreshManager)
    manager._requests_proxies = lambda: None
    return manager


@pytest.mark.parametrize("status_code", [401, 403])
def test_fetch_credits_raises_typed_auth_error(monkeypatch, status_code: int):
    manager = make_refresh_manager()
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code),
    )

    with pytest.raises(RuntimeError) as caught:
        manager._fetch_credits_balance("token", "account")

    assert caught.value.__class__.__name__ == "CreditsAuthError"
    assert getattr(caught.value, "status_code", None) == status_code
    assert str(caught.value) == f"credits request failed: {status_code}"


@pytest.mark.parametrize("status_code", [429, 500])
def test_fetch_credits_keeps_non_auth_failures_as_runtime_error(
    monkeypatch, status_code: int
):
    manager = make_refresh_manager()
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code),
    )

    with pytest.raises(RuntimeError) as caught:
        manager._fetch_credits_balance("token", "account")

    assert type(caught.value) is RuntimeError
    assert str(caught.value) == f"credits request failed: {status_code}"


class FakeRefreshTokenStore:
    def __init__(self):
        self.credit_errors: list[tuple[str, str]] = []

    def upsert_auto_refresh_token(self, value: str, **kwargs) -> dict:
        assert value == "new-token"
        assert kwargs["profile_id"] == "profile-1"
        return {"id": "token-1"}

    def set_credits_error(self, token_id: str, message: str) -> None:
        self.credit_errors.append((token_id, message))


def make_refresh_once_manager() -> RefreshManager:
    manager = object.__new__(RefreshManager)
    manager._prepare_refresh = lambda profile_id: {
        "id": profile_id,
        "name": "Profile",
        "url": "https://refresh.test/token",
        "headers": {},
        "form": {},
    }
    manager._requests_proxies = lambda: None
    manager._fetch_account_info = lambda token: {}
    manager._mark_success = lambda profile_id, http_status: None
    return manager


def test_refresh_once_can_skip_credits_side_effect(monkeypatch):
    manager = make_refresh_once_manager()
    credits_calls: list[str] = []
    manager.refresh_credits_for_token_id = lambda token_id: credits_calls.append(token_id)
    token_store = FakeRefreshTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(200, {"access_token": "new-token"}),
    )

    result = manager.refresh_once("profile-1", refresh_credits=False)

    assert credits_calls == []
    assert token_store.credit_errors == []
    assert result["credits_error"] == ""


def test_refresh_once_keeps_existing_default_credits_side_effect(monkeypatch):
    manager = make_refresh_once_manager()
    credits_calls: list[str] = []
    manager.refresh_credits_for_token_id = lambda token_id: credits_calls.append(token_id)
    token_store = FakeRefreshTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(200, {"access_token": "new-token"}),
    )

    manager.refresh_once("profile-1")

    assert credits_calls == ["token-1"]


class FakeCookieRefreshManager:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    def refresh_once(self, profile_id: str, *, refresh_credits: bool = True) -> dict:
        self.calls.append((profile_id, refresh_credits))
        return {"status": "ok", "credits_error": ""}


def make_auto_token_manager() -> TokenManager:
    manager = object.__new__(TokenManager)
    manager._lock = threading.Lock()
    manager.tokens = [
        {
            "id": "token-1",
            "value": "old-token",
            "status": "active",
            "fails": 0,
            "error_until": 0,
            "auto_refresh": True,
            "refresh_profile_id": "profile-1",
        }
    ]
    manager.save = lambda: None
    return manager


def test_handle_auth_failure_forwards_refresh_credits_flag(monkeypatch):
    manager = make_auto_token_manager()
    fake_refresh_manager = FakeCookieRefreshManager()
    monkeypatch.setattr(refresh_mgr_module, "refresh_manager", fake_refresh_manager)

    result = manager.handle_auth_failure("old-token", refresh_credits=False)

    assert result["status"] == "refreshed"
    assert fake_refresh_manager.calls == [("profile-1", False)]


def test_manual_token_auth_failure_still_marks_invalid():
    manager = object.__new__(TokenManager)
    manager._lock = threading.Lock()
    manager.tokens = [
        {
            "id": "manual-1",
            "value": "manual-token",
            "status": "active",
            "fails": 0,
            "error_until": 0,
            "auto_refresh": False,
        }
    ]
    manager.save = lambda: None

    result = manager.handle_auth_failure("manual-token", refresh_credits=False)

    assert result["status"] == "invalid"
    assert manager.tokens[0]["status"] == "invalid"


class FakeCreditsTokenStore:
    def __init__(self, auth_status: str = "refreshed"):
        self.auth_status = auth_status
        self.record = {
            "id": "token-1",
            "value": "old-token",
            "status": "active",
        }
        self.auth_calls: list[tuple[str, bool]] = []
        self.saved_credits: list[tuple[str, dict]] = []

    def get_by_id(self, token_id: str) -> dict | None:
        if token_id != self.record["id"]:
            return None
        return dict(self.record)

    def handle_auth_failure(
        self, value: str, *, refresh_credits: bool = True
    ) -> dict:
        self.auth_calls.append((value, refresh_credits))
        if self.auth_status == "refreshed":
            self.record["value"] = "new-token"
        elif self.auth_status == "invalid":
            self.record["status"] = "invalid"
        return {
            "status": self.auth_status,
            "message": f"auth result: {self.auth_status}",
            "http_status": 401,
        }

    def set_credits(self, token_id: str, credits: dict) -> None:
        self.saved_credits.append((token_id, credits))


def make_credits_recovery_manager() -> RefreshManager:
    manager = object.__new__(RefreshManager)
    manager._extract_account_id = lambda token: f"account-for-{token}"
    return manager


def test_default_credits_refresh_does_not_invoke_auth_recovery(monkeypatch):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    manager._fetch_credits_balance = lambda token, account: (_ for _ in ()).throw(
        CreditsAuthError("credits request failed: 401", status_code=401)
    )

    with pytest.raises(CreditsAuthError):
        manager.refresh_credits_for_token_id("token-1")

    assert token_store.auth_calls == []
    assert token_store.record["status"] == "active"


def test_explicit_refresh_uses_old_and_new_token_once_each(monkeypatch):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    calls: list[tuple[str, str]] = []

    def fetch(token: str, account_id: str) -> dict:
        calls.append((token, account_id))
        if token == "old-token":
            raise CreditsAuthError("credits request failed: 401", status_code=401)
        return {"total": 100, "used": 25, "available": 75, "updated_at": 1}

    manager._fetch_credits_balance = fetch

    result = manager.refresh_credits_for_token_id("token-1", handle_auth=True)

    assert calls == [
        ("old-token", "account-for-old-token"),
        ("new-token", "account-for-new-token"),
    ]
    assert token_store.auth_calls == [("old-token", False)]
    assert token_store.saved_credits == [("token-1", result["credits"])]


def test_new_token_auth_error_does_not_recurse_or_invalidate(monkeypatch):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    calls: list[str] = []

    def fetch(token: str, account_id: str) -> dict:
        calls.append(token)
        raise CreditsAuthError("credits request failed: 401", status_code=401)

    manager._fetch_credits_balance = fetch

    with pytest.raises(CreditsAuthError):
        manager.refresh_credits_for_token_id("token-1", handle_auth=True)

    assert calls == ["old-token", "new-token"]
    assert token_store.auth_calls == [("old-token", False)]
    assert token_store.record["status"] == "active"
    assert token_store.saved_credits == []


@pytest.mark.parametrize("auth_status", ["invalid", "retry"])
def test_auth_recovery_terminal_results_raise_known_error(
    monkeypatch, auth_status: str
):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore(auth_status=auth_status)
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    manager._fetch_credits_balance = lambda token, account: (_ for _ in ()).throw(
        CreditsAuthError("credits request failed: 403", status_code=403)
    )

    with pytest.raises(CreditsAuthError) as caught:
        manager.refresh_credits_for_token_id("token-1", handle_auth=True)

    assert str(caught.value) == f"auth result: {auth_status}"
    assert caught.value.status_code == 403
    assert token_store.auth_calls == [("old-token", False)]


class FakeAdminConfig:
    def get(self, key: str, default=None):
        return default

    def get_all(self) -> dict:
        return {}


class FakeAdminTokenStore:
    def __init__(self):
        self.errors: list[tuple[str, str]] = []

    def get_by_id(self, token_id: str) -> dict | None:
        return {"id": token_id, "value": "token"} if token_id else None

    def list_active_ids(self) -> list[str]:
        return ["token-1"]

    def set_credits_error(self, token_id: str, message: str) -> None:
        self.errors.append((token_id, message))


class FakeAdminRefreshManager:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, bool]] = []

    def refresh_credits_for_token_id(
        self, token_id: str, handle_auth: bool = False
    ) -> dict:
        self.calls.append((token_id, handle_auth))
        if self.fail:
            raise CreditsAuthError("credits request failed: 401", status_code=401)
        return {"token_id": token_id, "credits": {"available": 10}}


def make_admin_client(
    token_store: FakeAdminTokenStore,
    refresh_manager: FakeAdminRefreshManager,
) -> TestClient:
    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=token_store,
            config_manager=FakeAdminConfig(),
            refresh_manager=refresh_manager,
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


def test_single_admin_refresh_enables_auth_recovery():
    token_store = FakeAdminTokenStore()
    refresh_manager = FakeAdminRefreshManager()
    client = make_admin_client(token_store, refresh_manager)

    response = client.post("/api/v1/tokens/token-1/credits/refresh")

    assert response.status_code == 200
    assert refresh_manager.calls == [("token-1", True)]
    assert token_store.errors == []


def test_single_admin_refresh_records_failure_once():
    token_store = FakeAdminTokenStore()
    refresh_manager = FakeAdminRefreshManager(fail=True)
    client = make_admin_client(token_store, refresh_manager)

    response = client.post("/api/v1/tokens/token-1/credits/refresh")

    assert response.status_code == 500
    assert refresh_manager.calls == [("token-1", True)]
    assert token_store.errors == [("token-1", "credits request failed: 401")]


def test_batch_admin_refresh_enables_auth_recovery():
    token_store = FakeAdminTokenStore()
    refresh_manager = FakeAdminRefreshManager()
    client = make_admin_client(token_store, refresh_manager)

    response = client.post(
        "/api/v1/tokens/credits/refresh-batch",
        json={"ids": ["token-1"]},
    )

    assert response.status_code == 200
    assert response.json()["refreshed_count"] == 1
    assert refresh_manager.calls == [("token-1", True)]


def test_single_refresh_frontend_reloads_tokens_in_finally():
    source = (Path(__file__).resolve().parent.parent / "static" / "admin.js").read_text(
        encoding="utf-8"
    )
    function_start = source.index("window.refreshTokenCredits = async")
    function_end = source.index("window.toggleAutoRefresh", function_start)
    function_source = source[function_start:function_end]

    assert "finally" in function_source
    assert "await loadTokens();" in function_source
