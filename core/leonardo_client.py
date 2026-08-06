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


# 只有这三项能用于「网页会话出图」；apiCredit / streamTokens 属官方 API 通道，
# 出图扣不到它们。实测：账号 subscriptionTokens 只剩 41 时仍能显示 apiCredit=100000，
# 若把 5 项相加会显示 20 万可用，而实际一张图都出不了（每张约 250）。
_GENERATION_CREDIT_FIELDS = ("subscriptionTokens", "paidTokens", "rolloverTokens")
# 另计通道（仅展示，不计入出图可用额度）
_OTHER_CREDIT_FIELDS = ("apiCredit", "streamTokens")
_CREDIT_FIELDS = _GENERATION_CREDIT_FIELDS + _OTHER_CREDIT_FIELDS

TOKEN_BALANCE_QUERY = {
    "operationName": "GetTokenBalance",
    "variables": {},
    "query": (
        "query GetTokenBalance { user_details { "
        "subscriptionTokens paidTokens rolloverTokens apiCredit streamTokens __typename } }"
    ),
}


def _credit_value(details: Dict[str, Any], key: str) -> int:
    value = (details or {}).get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return 0


def sum_credits(details: Dict[str, Any]) -> int:
    """**出图可用**额度（不含 apiCredit/streamTokens 这类出图扣不到的通道）。"""
    return sum(_credit_value(details, key) for key in _GENERATION_CREDIT_FIELDS)


def credit_breakdown(details: Dict[str, Any]) -> Dict[str, int]:
    """额度明细：available=出图可用；其余通道单列，便于页面区分展示。"""
    return {
        "available": sum_credits(details),
        "subscription_tokens": _credit_value(details, "subscriptionTokens"),
        "paid_tokens": _credit_value(details, "paidTokens"),
        "rollover_tokens": _credit_value(details, "rolloverTokens"),
        "api_credit": _credit_value(details, "apiCredit"),
        "stream_tokens": _credit_value(details, "streamTokens"),
    }


def parse_token_balance(resp: Dict[str, Any]) -> Optional[int]:
    rows = ((resp or {}).get("data") or {}).get("user_details") or []
    if not rows:
        return None
    return sum_credits(rows[0])


# 上游「实际接受」的出图尺寸，按模型族 × 比例 × 分辨率档列出（线上逐张量像素校准）。
# 不在表内的尺寸会被上游静默改写成别的比例，或直接被拒绝，因此这里只列实测可用值：
#   nano-banana 系：1:1 只有 1024²(1K)/2048²(2K)（发 1536² 会被降到 1024²）；
#                   无 4:3（发 2048x1536 回 2048x2048 方图；官方档 2368x1792 被直接拒）。
#   gpt-image 系：1:1=1536²、4:3=2048x1536、16:9/9:16 同 nano-banana，均精确接受。
LEONARDO_SIZES: dict[str, dict[str, dict[str, Tuple[int, int]]]] = {
    # gemini 族（gemini-image-2 / nano-banana-2）：上游只认一组离散尺寸，
    # 下列均为 2026-08-04 对 pro 与 flash 两个模型实测「接受」的尺寸
    # （非表内尺寸如 1152x864/1360x1024/2464x1856 一律被上游拒绝）。
    "gemini": {
        "1:1": {"1K": (1024, 1024), "2K": (2048, 2048)},
        "4:3": {"1K": (1024, 768), "2K": (2048, 1536)},
        "3:4": {"1K": (768, 1024), "2K": (1536, 2048)},
        "3:2": {"1K": (1536, 1024), "2K": (2304, 1536)},
        "2:3": {"1K": (1024, 1536), "2K": (1536, 2304)},
        "16:9": {"1K": (2752, 1536), "2K": (2752, 1536)},
        "9:16": {"1K": (1536, 2752), "2K": (1536, 2752)},
    },
    # gpt-image-2：16:9(2752x1536) 与 9:16(1536x2752) 实测被上游**确定性拒绝**
    # （累计 7 次全失败、0 积分消耗），故不提供——保留会让客户端拿到 500 而非明确 400。
    "gpt": {
        "1:1": {"1K": (1536, 1536), "2K": (1536, 1536)},
        "4:3": {"1K": (2048, 1536), "2K": (2048, 1536)},
    },
    # gpt-image-1 与 gpt-image-2 并不同族：实测发 1536² 会被上游改写成 1024²。
    # 该模型暂不使用，未逐比例实测，故只保留已验证的 1:1，其余比例一律 400——
    # 未经验证就宣称支持，会重演「静默返回错尺寸/错比例」的问题。
    "gpt-image-1": {
        "1:1": {"1K": (1024, 1024), "2K": (1024, 1024)},
    },
}
_STYLE_IDS = ["111dc692-d470-4eec-b791-3475abac4c46"]
_GENERATE_QUERY = (
    "mutation Generate($request: CreateGenerationRequest!) { "
    "generate(request: $request) { apiCreditCost generationId __typename } }"
)


def leonardo_family(model_slug: Optional[str]) -> str:
    """模型族：gpt-image-1 自成一族（尺寸与 gpt-image-2 不同，实测确认）；
    其余 gpt-image-* 为 gpt 族；nano-banana-2 / gemini-image-2 为 gemini 族。"""
    slug = str(model_slug or "")
    if slug == "gpt-image-1":
        return "gpt-image-1"
    return "gpt" if slug.startswith("gpt-image") else "gemini"


def leonardo_supported_aspects(model_slug: Optional[str]) -> Tuple[str, ...]:
    """该模型上游真正能出的比例（其余一律 400，不静默改写）。"""
    return tuple(LEONARDO_SIZES[leonardo_family(model_slug)])


def aspect_to_size(
    aspect: str,
    *,
    model_slug: Optional[str] = None,
    output_resolution: str = "2K",
) -> Optional[Tuple[int, int]]:
    """(比例, 分辨率档) → 上游可用像素；不可实现返回 None（由上层转 400）。"""
    ratios = LEONARDO_SIZES[leonardo_family(model_slug)]
    by_res = ratios.get(str(aspect or "").strip())
    if not by_res:
        return None
    res = str(output_resolution or "2K").strip().upper()
    return by_res.get(res) or by_res.get("2K")


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


# --- 图生图（omni edit）---
# 参数结构取自 Leonardo 前端真实构造并经线上验证：必须带 omni_edit，且**不能**混入
# 文生图参数(style_ids/modelId/guidance_scale/num_inference_steps/negative_prompt/
# dimensions)，否则上游一律拒绝。参考图强度只接受 LOW/MID/HIGH。
def UPLOAD_INIT_IMAGE_QUERY(extension: str = "png") -> Dict[str, Any]:
    ext = (str(extension or "png").lower().lstrip(".") or "png")
    return {
        "operationName": "CreateUploadInitImage",
        "variables": {},
        "query": (
            "mutation CreateUploadInitImage { uploadInitImage(arg1: {extension: \"%s\"}) "
            "{ id url fields key __typename } }" % ext
        ),
    }


def parse_init_image_upload(resp: Dict[str, Any]) -> Dict[str, Any]:
    node = ((resp or {}).get("data") or {}).get("uploadInitImage") or {}
    image_id = str(node.get("id") or "").strip()
    url = str(node.get("url") or "").strip()
    if not image_id or not url:
        raise LeonardoError("uploadInitImage returned no upload target")
    try:
        fields = json.loads(node.get("fields") or "{}")
    except (TypeError, ValueError) as exc:
        raise LeonardoError("uploadInitImage returned malformed fields") from exc
    return {"id": image_id, "url": url, "fields": fields, "key": node.get("key") or ""}


def build_edit_payload(
    prompt: str,
    model_slug: str,
    width: int,
    height: int,
    init_image_ids: List[str],
    quantity: int = 1,
    strength: str = "MID",
) -> Dict[str, Any]:
    ids = [str(i).strip() for i in (init_image_ids or []) if str(i).strip()]
    if not ids:
        raise ValueError("init_image_ids is required for image edit")
    return {
        "operationName": "Generate",
        "variables": {
            "request": {
                "model": model_slug,
                "parameters": {
                    "omni_edit": True,
                    "prompt": (prompt or "").strip(),
                    "prompt_enhance": "OFF",
                    "quantity": max(1, min(4, int(quantity))),
                    "guidances": {
                        "image_reference": [
                            {
                                "image": {"id": image_id, "type": "UPLOADED"},
                                "strength": strength,
                            }
                            for image_id in ids
                        ]
                    },
                    "width": int(width),
                    "height": int(height),
                },
                "public": True,
            }
        },
        "query": _GENERATE_QUERY,
    }


def parse_generation_id(resp: Dict[str, Any]) -> str:
    gen_id = (((resp or {}).get("data") or {}).get("generate") or {}).get("generationId")
    if gen_id:
        return gen_id
    errors = [e.get("message", "") for e in (resp or {}).get("errors", []) if isinstance(e, dict)]
    raise LeonardoError(", ".join([m for m in errors if m]) or "Generate failed")


def parse_generation_cost(resp: Dict[str, Any]) -> Optional[int]:
    """Generate mutation 回报的本次生成实际积分成本（apiCreditCost），拿不到则 None。"""
    cost = (((resp or {}).get("data") or {}).get("generate") or {}).get("apiCreditCost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return int(cost)
    return None


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

    def get_credits(
        self, token: str, deadline: Optional[float] = None
    ) -> Optional[int]:
        return parse_token_balance(
            self._call(token, TOKEN_BALANCE_QUERY, deadline=deadline)
        )

    def get_user_credits(self, token: str) -> Dict[str, int]:
        """查询用户 credits 详情（用于 refresh_mgr）"""
        result = self._call(token, TOKEN_BALANCE_QUERY)
        data = result.get("data", {})
        user_details = data.get("user_details", []) if isinstance(data, dict) else []
        details = user_details[0] if user_details else {}
        # available = 出图可用额度；apiCredit/streamTokens 是另一通道，出图扣不到，
        # 单列出来供页面区分展示，绝不能并入 available（否则余额显示会严重虚高）。
        return credit_breakdown(details)

    def upload_init_image(
        self,
        token: str,
        image_bytes: bytes,
        extension: str = "png",
        deadline: Optional[float] = None,
    ) -> str:
        """上传参考图：申请预签名地址 → POST 到 S3 → 返回 init image id。"""
        up = parse_init_image_upload(
            self._call(token, UPLOAD_INIT_IMAGE_QUERY(extension), deadline=deadline)
        )
        files = {k: (None, str(v)) for k, v in (up["fields"] or {}).items()}
        files["file"] = (f"image.{extension}", image_bytes, f"image/{extension}")
        # S3 直传是独立于 GraphQL 的另一条 I/O，此前写死 120s 且不看 deadline：
        # 6 张参考图能串行阻塞 720 秒，把端到端时限整个穿透。
        upload_timeout = self._timeout_for_deadline(120, deadline)
        try:
            resp = requests.post(up["url"], files=files, timeout=upload_timeout)
        except Exception as exc:  # noqa: BLE001 - 传输失败统一转领域错误
            raise LeonardoError(f"init image upload failed: {exc}") from exc
        if getattr(resp, "status_code", 0) not in (200, 201, 204):
            raise LeonardoError(
                f"init image upload rejected: HTTP {getattr(resp, 'status_code', '?')}"
            )
        return up["id"]

    def create_edit_generation(
        self,
        token: str,
        prompt: str,
        model_slug: str,
        width: int,
        height: int,
        init_image_ids: List[str],
        quantity: int = 1,
        deadline: Optional[float] = None,
        on_cost=None,
    ) -> str:
        payload = build_edit_payload(
            prompt, model_slug, width, height, init_image_ids, quantity
        )
        response = self._call(token, payload, deadline=deadline)
        try:
            gen_id = parse_generation_id(response)
        except LeonardoError as exc:
            raise LeonardoGraphQLError(str(exc), operation="Generate") from exc
        if on_cost is not None:
            cost = parse_generation_cost(response)
            if cost is not None:
                on_cost(cost)
        return gen_id

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
        output_resolution: str = "2K",
        deadline: Optional[float] = None,
        on_cost=None,
    ) -> str:
        size = aspect_to_size(
            aspect_ratio, model_slug=model_slug, output_resolution=output_resolution
        )
        if size is None:
            # 兜底：路由应已 400 拦截；到这里说明比例对该模型不可实现，
            # 绝不能退回别的尺寸（上游会静默改写成别的比例）。
            raise LeonardoError(
                f"aspect ratio {aspect_ratio} is not supported by model {model_slug}"
            )
        width, height = size
        payload = build_generate_payload(
            prompt, model_id, width, height, quantity, init_image_ids, model_slug=model_slug
        )
        response = self._call(token, payload, deadline=deadline)
        try:
            gen_id = parse_generation_id(response)
        except LeonardoError as exc:
            raise LeonardoGraphQLError(
                str(exc), operation="Generate"
            ) from exc
        if on_cost is not None:
            # 上游回报的本次实际积分成本（精确值，优于余额差分估算）
            cost = parse_generation_cost(response)
            if cost is not None:
                on_cost(cost)
        return gen_id

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
