import base64
import json
import time
from typing import Any, Dict, List, Optional


class LeonardoError(Exception):
    """leonardo_client 统一异常。"""


def _b64url_json(segment: str) -> Dict[str, Any]:
    try:
        pad = "=" * ((4 - len(segment) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(segment + pad).decode("utf-8"))
    except Exception:
        return {}


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    data = _b64url_json(parts[1])
    return data if isinstance(data, dict) else {}


def token_exp(token: str) -> int:
    exp = decode_jwt_payload(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


def is_fresh_token(token: str, min_ttl_seconds: int = 120, *, now=time.time) -> bool:
    if (token or "").count(".") != 2:
        return False
    exp = token_exp(token)
    if not exp:
        return True
    return exp > int(now()) + max(30, int(min_ttl_seconds))


def is_likely_leonardo_token(token: str) -> bool:
    payload = decode_jwt_payload(token)
    if not payload:
        return False
    if "cognito-idp" in str(payload.get("iss", "")).lower():
        return True
    if str(payload.get("token_use", "")).lower() in {"id", "access"}:
        return True
    if "cognito:username" in payload:
        return True
    aud = payload.get("aud")
    return isinstance(aud, str) and aud.startswith("https://cognito-idp")


_CREDIT_FIELDS = ("subscriptionTokens", "paidTokens", "rolloverTokens", "apiCredit", "streamTokens")

TOKEN_BALANCE_QUERY = {
    "operationName": "GetTokenBalance",
    "variables": {},
    "query": (
        "query GetTokenBalance { user_details { "
        "subscriptionTokens paidTokens rolloverTokens apiCredit streamTokens __typename } }"
    ),
}


def sum_credits(details: Dict[str, Any]) -> int:
    total = 0
    for key in _CREDIT_FIELDS:
        value = (details or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def parse_token_balance(resp: Dict[str, Any]) -> Optional[int]:
    rows = ((resp or {}).get("data") or {}).get("user_details") or []
    if not rows:
        return None
    return sum_credits(rows[0])
