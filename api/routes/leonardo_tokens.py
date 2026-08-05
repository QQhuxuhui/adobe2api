import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid

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
    # 旧的单 cookie 文件（仅用于向后迁移）
    return token_mgr_module.CONFIG_DIR / "leonardo_cookie.json"


def _cookies_path():
    # 新的多 cookie 文件：按指纹去重的列表，支持多个 Leonardo 账号
    return token_mgr_module.CONFIG_DIR / "leonardo_cookies.json"


def _ensure_ids(items: list) -> list:
    """给每条 cookie 补一个稳定 id：轮换会改指纹，但 id 不变，写回/删除都按 id。"""
    changed = False
    for it in items:
        if not str(it.get("id") or "").strip():
            it["id"] = uuid.uuid4().hex[:12]
            changed = True
    if changed:
        _save_cookies(items)
    return items


def _load_cookies() -> list:
    """读多 cookie 列表；不存在时从旧单 cookie 文件迁移。返回 [{id,cookie,fingerprint,updated_at}]。"""
    path = _cookies_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("cookies") if isinstance(data, dict) else data
            items = [
                x
                for x in (items or [])
                if isinstance(x, dict) and str(x.get("cookie") or "").strip()
            ]
            return _ensure_ids(items)
        except Exception:  # noqa: BLE001 - 损坏当作空
            return []
    old = _cookie_path()
    if old.exists():
        try:
            d = json.loads(old.read_text(encoding="utf-8"))
            if str(d.get("cookie") or "").strip():
                return _ensure_ids(
                    [
                        {
                            "cookie": d["cookie"],
                            "fingerprint": d.get("fingerprint", ""),
                            "updated_at": d.get("updated_at"),
                        }
                    ]
                )
        except Exception:  # noqa: BLE001
            pass
    return []


def _save_cookies(items: list) -> None:
    path = _cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": items}), encoding="utf-8")


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
    """抽取 better-auth 三条，按指纹 upsert 进多账号列表（不覆盖其它账号）。

    同一 cookie 再次导入只更新时间；不同 cookie（不同账号/新登录）追加为新条目。
    refresh-key 接口与后台「导入 Leonardo Cookie」共用此实现。非法 cookie 抛 ValueError。
    """
    cookie = extract_better_auth_cookies(raw_cookie)  # 非法时 ValueError
    fingerprint = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
    updated_at = int(time.time())
    items = _load_cookies()
    for it in items:
        if it.get("fingerprint") == fingerprint:
            it["cookie"] = cookie
            it["updated_at"] = updated_at
            _save_cookies(items)
            return {"id": it["id"], "fingerprint": fingerprint, "updated_at": updated_at, "count": len(items)}
    new_id = uuid.uuid4().hex[:12]
    items.append({"id": new_id, "cookie": cookie, "fingerprint": fingerprint, "updated_at": updated_at})
    _save_cookies(items)
    return {"id": new_id, "fingerprint": fingerprint, "updated_at": updated_at, "count": len(items)}


def update_leonardo_cookie(cookie_id: str, raw_cookie: str) -> dict:
    """会话 cookie 被上游轮换后回写：按稳定 id 就地更新那条（指纹随之变化）。

    指纹会变、id 不变，所以按 id 定位不会像按指纹那样找不到而追加、攒垃圾。
    找不到该 id 则不动（返回 updated=0），避免误建重复账号。
    """
    cid = str(cookie_id or "").strip()
    cookie = extract_better_auth_cookies(raw_cookie)
    fingerprint = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
    updated_at = int(time.time())
    items = _load_cookies()
    for it in items:
        if it.get("id") == cid:
            it["cookie"] = cookie
            it["fingerprint"] = fingerprint
            it["updated_at"] = updated_at
            _save_cookies(items)
            return {"updated": 1, "id": cid, "fingerprint": fingerprint, "count": len(items)}
    return {"updated": 0, "id": cid, "count": len(items)}


def remove_leonardo_cookie(fingerprint: str) -> dict:
    """删除一条已导入的 cookie（用于清理刷不出来的失效账号）。

    优先按稳定 id 精确匹配（指纹会随轮换变，按 id 才可靠）；兼容旧的指纹前缀匹配。
    返回删除条数与剩余数。
    """
    key = str(fingerprint or "").strip()
    if not key:
        return {"removed": 0, "count": len(_load_cookies())}
    items = _load_cookies()
    kept = [
        it
        for it in items
        if it.get("id") != key
        and not str(it.get("fingerprint") or "").startswith(key)
    ]
    _save_cookies(kept)
    return {"removed": len(items) - len(kept), "count": len(kept)}


def list_leonardo_cookies() -> list:
    """全部已导入 cookie（含明文，仅供 refresh-key 接口内部使用）。"""
    return [
        {
            "id": it.get("id", ""),
            "cookie": it.get("cookie", ""),
            "fingerprint": it.get("fingerprint", ""),
            "updated_at": it.get("updated_at"),
        }
        for it in _load_cookies()
    ]


def read_leonardo_cookie_status() -> dict:
    """后台展示用的 cookie 状态——只回 id/指纹/时间，绝不回传 cookie 明文。"""
    items = _load_cookies()
    cookies = [
        {
            "id": str(it.get("id") or ""),
            "fingerprint": str(it.get("fingerprint") or ""),
            "updated_at": it.get("updated_at"),
        }
        for it in items
    ]
    return {
        "uploaded": bool(items),
        "count": len(items),
        "cookies": cookies,
        # 向后兼容旧字段（取第一条）
        "fingerprint": cookies[0]["fingerprint"] if cookies else "",
        "updated_at": cookies[0]["updated_at"] if cookies else None,
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
            # 带 cookie_id 表示 refresher 回写某账号轮换后的 cookie（按 id 就地更新）
            if req.cookie_id:
                return update_leonardo_cookie(req.cookie_id, req.cookie)
            return store_leonardo_cookie(req.cookie)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="cookie must contain better-auth session cookies",
            )

    @router.get("/api/v1/tokens/leonardo/cookies")
    def get_leonardo_cookies(request: Request):
        """多账号：返回全部已导入 cookie，供 refresher 逐个刷新。"""
        _require_refresh_key(request)
        return {"cookies": list_leonardo_cookies()}

    @router.get("/api/v1/tokens/leonardo/cookie")
    def get_leonardo_cookie(request: Request):
        # 向后兼容：返回第一条（旧版 refresher 单账号用）
        _require_refresh_key(request)
        items = list_leonardo_cookies()
        if not items:
            raise HTTPException(status_code=404, detail="no cookie uploaded")
        return items[0]

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
