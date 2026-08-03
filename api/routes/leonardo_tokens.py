import hmac
import math
import os
import time

from fastapi import APIRouter, HTTPException, Request

from api.schemas import LeonardoTokenUpsertRequest
from core.leonardo_client import decode_jwt_payload, token_exp


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


def build_leonardo_token_router(*, token_manager) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/tokens/leonardo")
    def upsert_leonardo_token(
        req: LeonardoTokenUpsertRequest,
        request: Request,
    ):
        required = str(os.getenv("LEONARDO_REFRESH_KEY", "") or "").strip()
        if not required:
            raise HTTPException(
                status_code=503,
                detail="Leonardo refresher disabled",
            )

        provided = request.headers.get("X-Leonardo-Refresh-Key", "")
        if not hmac.compare_digest(provided, required):
            raise HTTPException(status_code=401, detail="Unauthorized")

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
