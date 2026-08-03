from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.leonardo_client as leonardo_client_module
import core.refresh_mgr as refresh_mgr_module
import core.token_mgr as token_mgr_module
from core.refresh_mgr import CreditsAuthError, RefreshManager
from core.token_mgr import TokenManager


def make_refresh_manager() -> RefreshManager:
    manager = object.__new__(RefreshManager)
    manager._requests_proxies = lambda: None
    return manager


class FakeLeonardoClient:
    def __init__(self, credits: dict | None = None, error: Exception | None = None):
        self._credits = credits or {}
        self._error = error

    def get_user_credits(self, token: str) -> dict:
        if self._error is not None:
            raise self._error
        return self._credits


def patch_leonardo_client(monkeypatch, client) -> None:
    monkeypatch.setattr(
        leonardo_client_module, "LeonardoClient", lambda *args, **kwargs: client
    )


def test_fetch_leonardo_credits_uses_set_credits_field_names(monkeypatch):
    manager = make_refresh_manager()
    patch_leonardo_client(
        monkeypatch,
        FakeLeonardoClient({"available": 150, "subscription_tokens": 120,
                            "api_credit": 99999}),
    )

    credits = manager._fetch_leonardo_credits({"value": "leo-token"})

    assert credits["total"] == 150
    assert credits["available"] == 150
    assert credits["used"] == 0
    assert credits["available_until"] is None


def test_fetch_leonardo_credits_rejects_empty_token():
    manager = make_refresh_manager()

    with pytest.raises(RuntimeError):
        manager._fetch_leonardo_credits({"value": "  "})


def test_fetch_leonardo_credits_maps_upstream_failure_to_auth_error(monkeypatch):
    manager = make_refresh_manager()
    patch_leonardo_client(
        monkeypatch, FakeLeonardoClient(error=RuntimeError("graphql HTTP 401"))
    )

    with pytest.raises(CreditsAuthError) as caught:
        manager._fetch_leonardo_credits({"value": "leo-token"})

    assert caught.value.status_code == 401


def test_refresh_credits_persists_leonardo_balance(monkeypatch, tmp_path):
    monkeypatch.setattr(token_mgr_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(token_mgr_module, "DATA_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(token_mgr_module, "LEGACY_DATA_FILE", tmp_path / "legacy.json")
    store = TokenManager()
    token = store.add("leo-token", {"type": "leonardo"})
    monkeypatch.setattr(refresh_mgr_module, "token_manager", store)
    patch_leonardo_client(
        monkeypatch,
        FakeLeonardoClient({"available": 150, "subscription_tokens": 120,
                            "api_credit": 99999}),
    )
    manager = make_refresh_manager()

    result = manager.refresh_credits_for_token_id(token["id"])

    stored = store.get_by_id(token["id"])
    assert stored["credits_total"] == 150
    assert stored["credits_available"] == 150
    assert stored["credits_used"] == 0
    assert result["credits"]["total"] == 150
