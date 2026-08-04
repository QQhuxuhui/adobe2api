import hashlib
import hmac
import json
import math
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request

import core.token_mgr as token_mgr_module
from api.schemas import LeonardoCookieUploadRequest, LeonardoTokenUpsertRequest
from core.leonardo_client import decode_jwt_payload, token_exp


_BETTER_AUTH_COOKIES = (
    "__Secure-better-auth.session_token",
    "__Secure-better-auth.session_data.0",
    "__Secure-better-auth.session_data.1",
)


def _require_refresh_key(request: Request) -> None:
    required = str(os.getenv("LEONARDO_REFRESH_KEY", "") or "").strip()
    if not required:
        raise HTTPException(status_code=503, detail="Leonardo refresher disabled")
    provided = request.headers.get("X-Leonardo-Refresh-Key", "")
    if not hmac.compare_digest(provided, required):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _cookie_path():
    return token_mgr_module.CONFIG_DIR / "leonardo_cookie.json"


def extract_better_auth_cookies(raw: str) -> str:
    """从整条 cookie 头抽取 better-auth 会话 cookie，拼成 name=value; 串。

    至少要含 session_token；否则视为无效上传。
    """
    parts = []
    for name in _BETTER_AUTH_COOKIES:
        m = re.search(re.escape(name) + r"=([^;]+)", raw or "")
        if m:
            parts.append(f"{name}={m.group(1).strip()}")
    if not any(p.startswith("__Secure-better-auth.session_token=") for p in parts):
        raise ValueError("missing better-auth session_token")
    return "; ".join(parts)


DEFAULT_ISSUER = (
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xkVMuCqeu"
)
DEFAULT_AUDIENCE = "29lhcpsoi9crda0du1s0ampft3"
DEFAULT_MIN_TTL_SECONDS = 600


def _configured_min_ttl() -> int:
    raw = os.getenv(
        "LEONARDO_TOKEN_MIN_TTL_SECONDS",
        str(DEFAULT_MIN_TTL_SECONDS),
    )
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_MIN_TTL_SECONDS


def validate_leonardo_id_token(token: str, *, now: int) -> dict:
    token_value = str(token or "").strip()
    if token_value.startswith("Bearer "):
        token_value = token_value[7:].strip()

    payload = decode_jwt_payload(token_value)
    issuer = str(
        os.getenv("LEONARDO_COGNITO_ISSUER", DEFAULT_ISSUER) or DEFAULT_ISSUER
    ).strip()
    audience = str(
        os.getenv("LEONARDO_COGNITO_AUDIENCE", DEFAULT_AUDIENCE)
        or DEFAULT_AUDIENCE
    ).strip()
    exp = payload.get("exp")
    valid_exp = (
        isinstance(exp, (int, float))
        and not isinstance(exp, bool)
        and math.isfinite(exp)
    )
    if (
        payload.get("iss") != issuer
        or payload.get("aud") != audience
        or payload.get("token_use") != "id"
        or not str(payload.get("sub") or "").strip()
        or not valid_exp
        or int(exp) - int(now) < _configured_min_ttl()
    ):
        raise ValueError("invalid Leonardo ID token")
    return payload


def store_leonardo_cookie(raw_cookie: str) -> dict:
    """抽取 better-auth 三条并落盘，返回 {fingerprint, updated_at}。

    refresh-key 接口与后台「导入 Leonardo Cookie」共用此实现，避免两套逻辑走偏。
    非 Leonardo cookie 抛 ValueError。
    """
    cookie = extract_better_auth_cookies(raw_cookie)  # 非法时 ValueError
    fingerprint = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
    updated_at = int(time.time())
    path = _cookie_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"cookie": cookie, "fingerprint": fingerprint, "updated_at": updated_at}
        ),
        encoding="utf-8",
    )
    return {"fingerprint": fingerprint, "updated_at": updated_at}


def read_leonardo_cookie_status() -> dict:
    """后台展示用的 cookie 状态——只回指纹与时间，绝不回传 cookie 明文。"""
    path = _cookie_path()
    if not path.exists():
        return {"uploaded": False, "fingerprint": "", "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 文件损坏当作未上传
        return {"uploaded": False, "fingerprint": "", "updated_at": None}
    return {
        "uploaded": bool(data.get("cookie")),
        "fingerprint": str(data.get("fingerprint") or ""),
        "updated_at": data.get("updated_at"),
    }


def build_leonardo_token_router(*, token_manager) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/tokens/leonardo/cookie")
    def upload_leonardo_cookie(
        req: LeonardoCookieUploadRequest,
        request: Request,
    ):
        _require_refresh_key(request)
        try:
            return store_leonardo_cookie(req.cookie)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="cookie must contain better-auth session cookies",
            )

    @router.get("/api/v1/tokens/leonardo/cookie")
    def get_leonardo_cookie(request: Request):
        _require_refresh_key(request)
        path = _cookie_path()
        if not path.exists():
            raise HTTPException(status_code=404, detail="no cookie uploaded")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(status_code=404, detail="no cookie uploaded")
        return {
            "cookie": data.get("cookie", ""),
            "fingerprint": data.get("fingerprint", ""),
            "updated_at": data.get("updated_at"),
        }

    @router.post("/api/v1/tokens/leonardo")
    def upsert_leonardo_token(
        req: LeonardoTokenUpsertRequest,
        request: Request,
    ):
        _require_refresh_key(request)

        try:
            claims = validate_leonardo_id_token(req.token, now=int(time.time()))
            result = token_manager.upsert_leonardo_token(
                req.token,
                str(claims["sub"]).strip(),
                req.label,
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid Leonardo token",
            )

        item = result["token"]
        return {
            "status": result["status"],
            "token_id": item["id"],
            "account_id": item["account_id"],
            "expires_at": token_exp(item["value"]),
        }

    return router
