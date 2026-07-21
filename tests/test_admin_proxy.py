from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.routes.admin as admin_routes_module
import api.schemas as schemas_module
from api.routes.admin import build_admin_router


class FakeConfigManager:
    def get(self, key: str, default=None):
        return default

    def get_all(self) -> dict:
        return {}


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class RecordingSession:
    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: Exception | None = None,
    ):
        self.response = response
        self.error = error
        self.trust_env = True
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("test session requires a response or error")
        return self.response

    def close(self) -> None:
        self.closed = True


def make_admin_client(*, authenticated: bool = True) -> TestClient:
    def require_admin_auth(request) -> None:
        if not authenticated:
            raise HTTPException(status_code=401, detail="Unauthorized")

    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=object(),
            config_manager=FakeConfigManager(),
            refresh_manager=object(),
            log_store=object(),
            error_store=object(),
            live_log_store=object(),
            require_admin_auth=require_admin_auth,
            is_admin_authenticated=lambda request: authenticated,
            apply_client_config=lambda: None,
            get_generated_storage_stats=lambda: {},
        )
    )
    return TestClient(api)


def install_session(monkeypatch, session: RecordingSession) -> None:
    fake_requests = SimpleNamespace(
        Session=lambda: session,
        exceptions=requests.exceptions,
    )
    monkeypatch.setattr(admin_routes_module, "requests", fake_requests, raising=False)


def test_proxy_request_schema_defaults_to_direct():
    request_type = getattr(schemas_module, "ProxyTestRequest", None)
    assert request_type is not None
    assert request_type().model_dump() == {"proxy": ""}


def test_direct_probe_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy.test:8080")
    session = RecordingSession(response=FakeResponse(401))
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": ""},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status_code"] == 401
    assert response.json()["via"] == "direct"
    assert response.json()["latency_ms"] >= 0
    assert session.trust_env is False
    assert session.calls == [
        (
            "https://firefly.adobe.io/",
            {"timeout": 10, "allow_redirects": False, "proxies": None},
        )
    ]
    assert session.closed is True


def test_explicit_proxy_is_used_for_both_schemes(monkeypatch):
    session = RecordingSession(response=FakeResponse(404))
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": "http://127.0.0.1:7890"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["via"] == "proxy"
    assert session.calls[0][1]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (requests.exceptions.Timeout("slow upstream"), "timeout"),
        (requests.exceptions.ProxyError("proxy rejected"), "proxy_error"),
        (requests.exceptions.ConnectionError("connection refused"), "connection_error"),
        (requests.exceptions.RequestException("request failed"), "request_error"),
    ],
)
def test_network_errors_return_stable_sanitized_codes(
    monkeypatch,
    error: Exception,
    expected_code: str,
):
    session = RecordingSession(error=error)
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": "https://user:secret@proxy.example:8443"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == expected_code
    assert response.json()["via"] == "proxy"
    assert response.json()["latency_ms"] >= 0
    assert "secret" not in response.text
    assert str(error) not in response.text
    assert session.closed is True


@pytest.mark.parametrize(
    "proxy",
    [
        "proxy.example:8080",
        "http://",
        "http://bad host:8080",
        "http://proxy.example:0",
        "http://proxy.example:70000",
        "ftp://proxy.example:21",
        "socks5://proxy.example:1080",
    ],
)
def test_invalid_proxy_urls_return_400_without_network(
    monkeypatch,
    proxy: str,
):
    session = RecordingSession(response=FakeResponse(200))
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": proxy},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid proxy URL"
    assert session.calls == []


def test_proxy_probe_requires_admin_auth(monkeypatch):
    session = RecordingSession(response=FakeResponse(200))
    install_session(monkeypatch, session)

    response = make_admin_client(authenticated=False).post(
        "/api/v1/config/test-proxy",
        json={"proxy": ""},
    )

    assert response.status_code == 401
    assert session.calls == []


def test_unauthenticated_malformed_proxy_request_returns_401_without_echo():
    response = make_admin_client(authenticated=False).post(
        "/api/v1/config/test-proxy",
        json={"proxy": ["https://user:secret@proxy.example"]},
    )

    assert response.status_code == 401
    assert "secret" not in response.text
    assert "proxy.example" not in response.text


def test_authenticated_malformed_proxy_request_is_sanitized():
    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": ["https://user:secret@proxy.example"]},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid proxy request"}
    assert "secret" not in response.text
    assert "proxy.example" not in response.text


def test_proxy_test_frontend_contract():
    repo_root = Path(__file__).resolve().parent.parent
    html = (repo_root / "static" / "admin.html").read_text(encoding="utf-8")
    script = (repo_root / "static" / "admin.js").read_text(encoding="utf-8")
    styles = (repo_root / "static" / "admin.css").read_text(encoding="utf-8")

    assert 'id="testProxyBtn"' in html
    assert 'id="proxyTestResult"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert '/static/admin.js?v=20260721-1' in html
    assert 'confProxy.addEventListener("input", invalidateProxyTestResult)' in script
    assert "proxyTestGate.invalidate();" in script
    assert 'fetch("/api/v1/config/test-proxy"' in script
    assert "PROXY_TEST_ERROR_MESSAGES" in script
    assert 'testProxyBtn.textContent = "测试中..."' in script
    assert "finally" in script[script.index('if (testProxyBtn)'):]
    assert ".proxy-test-row" in styles
    assert ".proxy-test-result.is-success" in styles
    assert ".proxy-test-result.is-error" in styles
