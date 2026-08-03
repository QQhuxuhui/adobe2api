import base64
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from curl_cffi.requests import Session as CurlSession
from curl_cffi.requests.exceptions import RequestException as CurlRequestException


class LeonardoError(Exception):
    """leonardo_client 统一异常。"""


class LeonardoRetryUnsafeError(LeonardoError):
    """单发(非幂等)请求失败且服务端可能已受理：禁止自动重试，避免重复扣费。"""


class LeonardoGraphQLError(LeonardoError):
    """GraphQL 业务错误；保留操作名以区分只读查询和非幂等 mutation。"""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        codes: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.operation = str(operation or "")
        self.codes = frozenset(
            str(code or "").strip().lower().replace("_", "-")
            for code in (codes or [])
            if str(code or "").strip()
        )


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
    *,
    model_slug: str = "nano-banana-2",
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
        # request.model 是服务端校验的模型 slug（= sdVersion 小写连字符化），
        # 它决定实际出图模型并覆盖 parameters.modelId；两者需成对匹配。
        "variables": {"request": {"model": model_slug, "parameters": params, "public": True}},
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


GRAPHQL_URL = "https://api.leonardo.ai/v1/graphql"
_HTTP_ATTEMPTS = 3
_RETRY_BACKOFF = 0.5
_RETRYABLE_OPERATIONS = frozenset({
    "GetTokenBalance",
    "GetAIGenerationFeedStatuses",
    "GetAIGenerationFeed",
})
_BASE_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://app.leonardo.ai",
    "referer": "https://app.leonardo.ai/",
    "x-leo-schema-version": "latest",
}


class LeonardoClient:
    def __init__(self, *, gql=None):
        self._gql_fn = gql  # 可注入；None 时用真实 HTTP

    @staticmethod
    def _timeout_for_deadline(timeout: float, deadline: Optional[float]) -> float:
        fixed_timeout = max(0.001, float(timeout))
        if deadline is None:
            return fixed_timeout
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            raise LeonardoError("Leonardo request deadline exceeded")
        return min(fixed_timeout, remaining)

    def _http_gql(
        self,
        token: str,
        payload: Dict[str, Any],
        *,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        headers = dict(_BASE_HEADERS)
        headers["authorization"] = f"Bearer {token}"
        operation = str(payload.get("operationName") or "")
        attempts = _HTTP_ATTEMPTS if operation in _RETRYABLE_OPERATIONS else 1
        proxy = str(os.environ.get("LEONARDO_PROXY") or "").strip()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        if deadline is None:
            session = requests.Session()
            session.trust_env = False
        else:
            session = CurlSession(trust_env=False)
        try:
            for attempt in range(attempts):
                try:
                    request_timeout = self._timeout_for_deadline(60, deadline)
                    resp = session.post(
                        GRAPHQL_URL,
                        headers=headers,
                        json=payload,
                        timeout=request_timeout,
                        proxies=proxies,
                    )
                except (
                    requests.exceptions.RequestException,
                    CurlRequestException,
                ) as exc:
                    if attempt < attempts - 1:
                        sleep_for = _RETRY_BACKOFF
                        if deadline is not None:
                            remaining = float(deadline) - time.monotonic()
                            if remaining <= 0:
                                raise LeonardoError(
                                    "Leonardo request deadline exceeded"
                                ) from exc
                            sleep_for = min(sleep_for, remaining)
                        if sleep_for > 0:
                            time.sleep(sleep_for)
                        continue
                    if attempts > 1:
                        message = (
                            f"graphql {operation} failed after {attempts} "
                            f"attempts: {exc}"
                        )
                        raise LeonardoError(message) from exc
                    # 单发(非幂等, 如 Generate)传输失败: 服务端可能已接受 → 禁止重试
                    message = (
                        f"graphql {operation or 'request'} failed; request not retried "
                        f"to avoid duplicate side effects: {exc}"
                    )
                    raise LeonardoRetryUnsafeError(message) from exc
                if not resp.ok:
                    # 401/403/429 是网关层鉴权/限流拒绝：请求未被处理(未生效/未扣费)，
                    # 可安全切号并标记 token 失效/耗尽——不能当作"可能已生效"而禁止重试。
                    if resp.status_code in (401, 403, 429):
                        raise LeonardoError(f"graphql HTTP {resp.status_code}")
                    if attempts == 1:
                        # 其余单发 HTTP 错误(如 5xx)可能已生效 → 禁止重试(避免重复扣费)
                        raise LeonardoRetryUnsafeError(
                            f"graphql HTTP {resp.status_code}"
                        )
                    raise LeonardoError(f"graphql HTTP {resp.status_code}")
                return resp.json()
            raise LeonardoError("graphql request failed")
        finally:
            session.close()

    def _call(
        self,
        token: str,
        payload: Dict[str, Any],
        *,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        if self._gql_fn is not None:
            resp = self._gql_fn(token, payload)
        else:
            resp = self._http_gql(token, payload, deadline=deadline)
        if isinstance(resp, dict) and resp.get("errors"):
            operation = str(payload.get("operationName") or "")
            if operation == "Generate":
                data = resp.get("data")
                generate = data.get("generate") if isinstance(data, dict) else None
                generation_id = (
                    generate.get("generationId")
                    if isinstance(generate, dict)
                    else None
                )
                if generation_id:
                    return resp
            messages = [
                str(error.get("message", "")).strip()
                for error in resp["errors"]
                if isinstance(error, dict)
            ]
            codes = []
            for error in resp["errors"]:
                if not isinstance(error, dict):
                    continue
                extensions = error.get("extensions")
                if isinstance(extensions, dict) and extensions.get("code"):
                    codes.append(str(extensions["code"]))
            detail = "; ".join(message for message in messages if message)
            raise LeonardoGraphQLError(
                detail or "graphql error", operation=operation, codes=codes
            )
        return resp

    def get_credits(self, token: str) -> Optional[int]:
        return parse_token_balance(self._call(token, TOKEN_BALANCE_QUERY))

    def get_user_credits(self, token: str) -> Dict[str, int]:
        """查询用户 credits 详情（用于 refresh_mgr）"""
        result = self._call(token, TOKEN_BALANCE_QUERY)
        data = result.get("data", {})
        user_details = data.get("user_details", []) if isinstance(data, dict) else []
        details = user_details[0] if user_details else {}
        return {
            "subscriptionTokens": details.get("subscriptionTokens", 0),
            "gptTokens": details.get("apiCredit", 0),  # apiCredit 是 GPT token
        }

    def create_generation(
        self,
        token,
        prompt,
        model_id,
        aspect_ratio,
        quantity=1,
        init_image_ids=None,
        *,
        model_slug="nano-banana-2",
        deadline: Optional[float] = None,
    ) -> str:
        width, height = aspect_to_size(aspect_ratio)
        payload = build_generate_payload(
            prompt, model_id, width, height, quantity, init_image_ids, model_slug=model_slug
        )
        response = self._call(token, payload, deadline=deadline)
        try:
            return parse_generation_id(response)
        except LeonardoError as exc:
            raise LeonardoGraphQLError(
                str(exc), operation="Generate"
            ) from exc

    def poll_status(
        self, token: str, gen_id: str, *, deadline: Optional[float] = None
    ) -> str:
        return parse_generation_status(
            self._call(token, build_status_query(gen_id), deadline=deadline)
        )

    def get_image_urls(
        self, token: str, gen_id: str, *, deadline: Optional[float] = None
    ) -> List[str]:
        return parse_image_urls(
            self._call(token, build_feed_query(gen_id), deadline=deadline)
        )

    def wait_for_completion(
        self,
        token,
        gen_id,
        *,
        timeout=300,
        poll_interval=4,
        sleep=time.sleep,
        now=time.time,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        local_deadline = now() + timeout
        while now() < local_deadline:
            if deadline is not None and time.monotonic() >= deadline:
                break
            status = self.poll_status(token, gen_id, deadline=deadline)
            if status in ("COMPLETE", "COMPLETED"):
                return {
                    "success": True,
                    "images": self.get_image_urls(
                        token, gen_id, deadline=deadline
                    ),
                }
            if status in ("FAILED", "ERROR"):
                return {"success": False, "error": "generation failed"}
            sleep_for = poll_interval
            if deadline is not None:
                remaining = float(deadline) - time.monotonic()
                if remaining <= 0:
                    break
                sleep_for = min(sleep_for, remaining)
            if sleep_for > 0:
                sleep(sleep_for)
        return {"success": False, "error": "generation timeout"}
