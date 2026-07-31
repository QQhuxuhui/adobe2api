import base64
import json
import time
from typing import Any, Dict, List, Optional, Tuple


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


ASPECT_TO_SIZE = {
    "16:9": (2752, 1536),
    "9:16": (1536, 2752),
    "1:1": (1536, 1536),
    "4:3": (2048, 1536),
}
_STYLE_IDS = ["111dc692-d470-4eec-b791-3475abac4c46"]
_GENERATE_QUERY = (
    "mutation Generate($request: CreateGenerationRequest!) { "
    "generate(request: $request) { apiCreditCost generationId __typename } }"
)


def aspect_to_size(aspect: str) -> Tuple[int, int]:
    return ASPECT_TO_SIZE.get(aspect, ASPECT_TO_SIZE["1:1"])


def build_generate_payload(
    prompt: str,
    model_id: str,
    width: int,
    height: int,
    quantity: int = 1,
    init_image_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "width": width,
        "height": height,
        "prompt": (prompt or "").strip(),
        "quantity": max(1, min(4, int(quantity))),
        "style_ids": list(_STYLE_IDS),
        "prompt_enhance": "ON",
        "dimensions": f"{width}x{height}",
        "modelId": model_id,
        "negative_prompt": "",
        "guidance_scale": 7.0,
        "num_inference_steps": 30,
    }
    if init_image_ids:
        params["guidances"] = {
            "image_reference": [
                {"image": {"id": image_id, "type": "UPLOADED"}, "strength": "MID"}
                for image_id in init_image_ids
            ]
        }
    return {
        "operationName": "Generate",
        "variables": {"request": {"model": "nano-banana-2", "parameters": params, "public": True}},
        "query": _GENERATE_QUERY,
    }


def parse_generation_id(resp: Dict[str, Any]) -> str:
    gen_id = (((resp or {}).get("data") or {}).get("generate") or {}).get("generationId")
    if gen_id:
        return gen_id
    errors = [e.get("message", "") for e in (resp or {}).get("errors", []) if isinstance(e, dict)]
    raise LeonardoError(", ".join([m for m in errors if m]) or "Generate failed")


def build_status_query(gen_id: str) -> Dict[str, Any]:
    return {
        "operationName": "GetAIGenerationFeedStatuses",
        "variables": {"where": {"id": {"_eq": gen_id}}},
        "query": (
            "query GetAIGenerationFeedStatuses($where: generations_bool_exp = {}) { "
            "generations(where: $where) { id status __typename } }"
        ),
    }


def build_feed_query(gen_id: str) -> Dict[str, Any]:
    return {
        "operationName": "GetAIGenerationFeed",
        "variables": {"where": {"id": {"_eq": gen_id}}, "limit": 1},
        "query": (
            "query GetAIGenerationFeed($where: generations_bool_exp = {}, $limit: Int) { "
            "generations(where: $where, limit: $limit) { "
            "generated_images(order_by: [{url: desc}]) { url id __typename } __typename } }"
        ),
    }


def parse_generation_status(resp: Dict[str, Any]) -> str:
    gens = ((resp or {}).get("data") or {}).get("generations") or []
    return gens[0].get("status", "PENDING") if gens else "PENDING"


def parse_image_urls(resp: Dict[str, Any]) -> List[str]:
    gens = ((resp or {}).get("data") or {}).get("generations") or []
    if not gens:
        return []
    return [img.get("url") for img in gens[0].get("generated_images", []) if img.get("url")]
