import base64
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.leonardo_tokens import build_leonardo_token_router
from core.token_mgr import TokenManager


LEONARDO_ISSUER = (
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xkVMuCqeu"
)
LEONARDO_AUDIENCE = "29lhcpsoi9crda0du1s0ampft3"


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256"}).encode()
    ).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _valid_payload(*, exp: int, sub: str = "leo-1") -> dict:
    return {
        "iss": LEONARDO_ISSUER,
        "aud": LEONARDO_AUDIENCE,
        "token_use": "id",
        "sub": sub,
        "exp": exp,
    }


@pytest.fixture
def token_manager(tmp_path, monkeypatch):
    import core.token_mgr as token_mgr_module

    monkeypatch.setattr(token_mgr_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(token_mgr_module, "DATA_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(
        token_mgr_module,
        "LEGACY_DATA_FILE",
        tmp_path / "tokens_legacy.json",
    )
    return TokenManager()


@pytest.fixture
def client(token_manager):
    api = FastAPI()
    api.include_router(build_leonardo_token_router(token_manager=token_manager))
    return TestClient(api)


@pytest.fixture
def authorized_headers(monkeypatch):
    monkeypatch.setenv("LEONARDO_REFRESH_KEY", "correct-key")
    monkeypatch.setenv("LEONARDO_TOKEN_MIN_TTL_SECONDS", "600")
    return {"X-Leonardo-Refresh-Key": "correct-key"}


def test_endpoint_is_disabled_without_configured_key(client, monkeypatch):
    monkeypatch.delenv("LEONARDO_REFRESH_KEY", raising=False)

    response = client.post(
        "/api/v1/tokens/leonardo",
        json={"token": "x.y.z"},
    )

    assert response.status_code == 503


@pytest.mark.parametrize("provided", [None, "wrong-key"])
def test_endpoint_rejects_missing_or_wrong_key(client, monkeypatch, provided):
    monkeypatch.setenv("LEONARDO_REFRESH_KEY", "correct-key")
    headers = {} if provided is None else {"X-Leonardo-Refresh-Key": provided}

    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=headers,
        json={"token": "x.y.z"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://example.invalid/pool"),
        ("aud", "wrong-client"),
        ("token_use", "access"),
        ("sub", ""),
    ],
)
def test_endpoint_rejects_wrong_claim(
    client,
    authorized_headers,
    claim,
    value,
):
    payload = _valid_payload(exp=int(time.time()) + 3600)
    payload[claim] = value

    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": _jwt(payload)},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Leonardo token"}


def test_endpoint_rejects_malformed_token(client, authorized_headers):
    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": "not-a-jwt"},
    )

    assert response.status_code == 400


def test_endpoint_rejects_token_below_minimum_ttl(client, authorized_headers):
    token = _jwt(_valid_payload(exp=int(time.time()) + 599))

    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": token},
    )

    assert response.status_code == 400


@pytest.mark.parametrize("exp", [float("nan"), float("inf"), float("-inf")])
def test_endpoint_rejects_non_finite_exp(client, authorized_headers, exp):
    token = _jwt(_valid_payload(exp=exp))

    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": token},
    )

    assert response.status_code == 400


def test_endpoint_upserts_without_returning_raw_token(
    client,
    token_manager,
    authorized_headers,
):
    token = _jwt(_valid_payload(exp=int(time.time()) + 3600))

    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": token, "label": "Primary"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["account_id"] == "leo-1"
    assert response.json()["expires_at"] > int(time.time())
    assert token not in response.text
    assert len(token_manager.tokens) == 1
    assert token_manager.tokens[0]["refresh_profile_name"] == "Primary"


def test_endpoint_reports_created_updated_noop(
    client,
    token_manager,
    authorized_headers,
):
    now = int(time.time())

    def post(exp):
        return client.post(
            "/api/v1/tokens/leonardo",
            headers=authorized_headers,
            json={"token": _jwt(_valid_payload(exp=exp)), "label": "Primary"},
        )

    r1 = post(now + 3600)
    assert r1.json()["status"] == "created"
    tid = r1.json()["token_id"]

    r2 = post(now + 7200)  # 更新的 exp、不同 value → updated，保留同一记录 id
    assert r2.json()["status"] == "updated"
    assert r2.json()["token_id"] == tid
    assert r2.json()["account_id"] == "leo-1"
    assert r2.json()["expires_at"] > r1.json()["expires_at"]

    r3 = post(now + 1800)  # exp 倒退 → noop
    assert r3.json()["status"] == "noop"
    assert len(token_manager.tokens) == 1


def test_endpoint_uses_configurable_issuer_and_audience(
    client,
    authorized_headers,
    monkeypatch,
):
    monkeypatch.setenv("LEONARDO_COGNITO_ISSUER", "https://issuer.example/pool")
    monkeypatch.setenv("LEONARDO_COGNITO_AUDIENCE", "custom-client")
    payload = _valid_payload(exp=int(time.time()) + 3600)
    payload["iss"] = "https://issuer.example/pool"
    payload["aud"] = "custom-client"

    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": _jwt(payload)},
    )

    assert response.status_code == 200


# ---------------- Leonardo cookie 上传/拉取端点 ----------------

_SAMPLE_COOKIE = (
    "anonymous-id=abc; "
    "__Secure-better-auth.session_token=tok123.sig456; "
    "__Secure-better-auth.session_data.0=DATA0PART; "
    "__Secure-better-auth.session_data.1=DATA1PART; "
    "_ga=GA1.1.x"
)


def test_cookie_endpoint_disabled_without_key(client, monkeypatch):
    monkeypatch.delenv("LEONARDO_REFRESH_KEY", raising=False)
    r = client.post("/api/v1/tokens/leonardo/cookie", json={"cookie": _SAMPLE_COOKIE})
    assert r.status_code == 503


def test_cookie_endpoint_rejects_wrong_key(client, monkeypatch):
    monkeypatch.setenv("LEONARDO_REFRESH_KEY", "correct-key")
    r = client.post(
        "/api/v1/tokens/leonardo/cookie",
        headers={"X-Leonardo-Refresh-Key": "nope"},
        json={"cookie": _SAMPLE_COOKIE},
    )
    assert r.status_code == 401


def test_cookie_upload_extracts_better_auth_and_roundtrips(client, authorized_headers):
    up = client.post(
        "/api/v1/tokens/leonardo/cookie",
        headers=authorized_headers,
        json={"cookie": _SAMPLE_COOKIE},
    )
    assert up.status_code == 200
    fp = up.json()["fingerprint"]
    assert fp

    got = client.get("/api/v1/tokens/leonardo/cookie", headers=authorized_headers)
    assert got.status_code == 200
    assert got.json()["fingerprint"] == fp
    cookie = got.json()["cookie"]
    # 只保留 better-auth 三条，剔除无关 cookie
    assert "__Secure-better-auth.session_token=tok123.sig456" in cookie
    assert "__Secure-better-auth.session_data.0=DATA0PART" in cookie
    assert "__Secure-better-auth.session_data.1=DATA1PART" in cookie
    assert "anonymous-id" not in cookie and "_ga" not in cookie


def test_cookie_upload_rejects_without_session_token(client, authorized_headers):
    r = client.post(
        "/api/v1/tokens/leonardo/cookie",
        headers=authorized_headers,
        json={"cookie": "anonymous-id=abc; _ga=GA1.1.x"},
    )
    assert r.status_code == 400


def test_cookie_get_returns_404_when_none(client, authorized_headers):
    r = client.get("/api/v1/tokens/leonardo/cookie", headers=authorized_headers)
    assert r.status_code == 404
