import time
from typing import Any, Dict, List, Optional

from core.leonardo_client import LeonardoError


class LeonardoGenerationError(LeonardoError):
    """生成已提交后失败(轮询超时/上游 FAILED)。重试会重复扣费，不得自动重发。"""


_SUPPORTED_ASPECTS = {"16:9", "9:16", "1:1", "4:3"}
_GRAPHQL_AUTH_REJECTION_CODES = {
    "invalid-jwt",
    "unauthenticated",
    "unauthorized",
    "forbidden",
}
_GRAPHQL_QUOTA_REJECTION_CODES = {
    "insufficient-balance",
    "insufficient-credits",
    "quota-exhausted",
    "resource-exhausted",
}
_SIZE_TO_ASPECT = {
    "1024x1024": "1:1", "512x512": "1:1", "256x256": "1:1",
    "1792x1024": "16:9", "1536x1024": "16:9",
    "1024x1792": "9:16", "1024x1536": "9:16",
    "2048x1536": "4:3",
}


def to_aspect(size: Optional[str] = None, aspect_ratio: Optional[str] = None) -> str:
    ratio = (aspect_ratio or "").strip()
    if ratio in _SUPPORTED_ASPECTS:
        return ratio
    mapped = _SIZE_TO_ASPECT.get((size or "").strip().lower())
    return mapped or "1:1"


def classify_leonardo_error(exc: Exception) -> str:
    """把 Leonardo 异常分类，供各入口映射到自己的错误体系。

    返回："unsafe"（已提交后失败/单发可能已受理，换号重试会重复扣费 → 不可重试 500）、
    "auth"（JWT/鉴权失效 → 标失效并切号）、"quota"（额度耗尽 → 标耗尽并切号）、
    "temp"（HTTP/传输等临时故障 → 可重试）。
    """
    from core.leonardo_client import LeonardoGraphQLError, LeonardoRetryUnsafeError

    if isinstance(exc, (LeonardoGenerationError, LeonardoRetryUnsafeError)):
        return "unsafe"
    if isinstance(exc, LeonardoGraphQLError) and exc.operation == "Generate":
        if exc.codes & _GRAPHQL_AUTH_REJECTION_CODES:
            return "auth"
        if exc.codes & _GRAPHQL_QUOTA_REJECTION_CODES:
            return "quota"
        return "unsafe"
    message = str(exc).lower()
    # HTTP 状态优先(网关拒绝，语义明确)
    if "http 401" in message or "http 403" in message:
        return "auth"
    if "http 429" in message:
        return "quota"
    # 文本特征：避免裸 "invalid" 误伤 "invalid model" 等请求错误而误废健康 token
    if any(
        kw in message
        for kw in (
            "jwt", "unauthorized", "not authorized", "forbidden",
            "signature", "invalid token", "invalid jwt", "expired token",
            "token expired", "token has expired",
        )
    ):
        return "auth"
    # 不用裸 "balance"：会误伤操作名 GetTokenBalance；真实额度错误含 insufficient/exhausted
    if any(kw in message for kw in ("quota", "insufficient", "exhausted", "credits")):
        return "quota"
    return "temp"


def leonardo_geometry_error(model_slug, aspect: str) -> Optional[str]:
    """比例对该 Leonardo 模型是否可实现；不可实现返回给客户端的 400 文案，否则 None。

    上游对不支持的比例不会报错，而是**静默改写**（如 nano-banana 系收到 4:3 会回
    2048x2048 方图），因此必须在出图前拦下来，避免下游拿到错比例的图。
    """
    from core.leonardo_client import aspect_to_size, leonardo_supported_aspects

    if aspect_to_size(aspect, model_slug=model_slug) is not None:
        return None
    allowed = ", ".join(leonardo_supported_aspects(model_slug))
    return (
        f"aspect ratio {aspect} is not supported by this model; "
        f"supported ratios: {allowed}"
    )


def pool_prefers_leonardo(token_manager) -> bool:
    """共享公开名（gemini-3-pro-image / gpt-image-2 等）该不该用 Leonardo 后端。

    规则：池里有 Leonardo token 且**没有** Adobe token → 用 Leonardo（如搬瓦工 Leonardo-only）。
    有 Adobe（或 token_manager 不支持该判断）→ 用 Adobe（全功能，线上行为原样保留）。
    单后端部署各自命中一支；同时有两类时优先 Adobe。
    """
    has = getattr(token_manager, "has_active_token", None)
    if not callable(has):
        return False
    return bool(has("leonardo")) and not bool(has("adobe"))


def clamp_quantity(n) -> int:
    try:
        value = int(n)
    except (TypeError, ValueError):
        return 1
    return max(1, min(4, value))


def _read_balance(client, token: str, deadline) -> Optional[int]:
    """读出图可用额度；余额查询不消耗积分。任何失败都不得影响出图。"""
    reader = getattr(client, "get_credits", None)
    if not callable(reader):
        return None
    try:
        value = reader(token)
    except Exception:  # noqa: BLE001 - 记账失败不影响出图
        return None
    return value if isinstance(value, (int, float)) else None


def _measure_cost(client, token: str, before, deadline) -> Optional[int]:
    """生成前后余额差分 = 本次单张成本。

    差值非正（并发出图/额度刚回补等）时返回 None——宁可不记，也不记错账。
    """
    if before is None:
        return None
    after = _read_balance(client, token, deadline)
    if after is None:
        return None
    diff = int(before) - int(after)
    return diff if diff > 0 else None


def generate_images(
    client,
    token: str,
    *,
    prompt: str,
    model_id: str,
    model_slug: str = "nano-banana-2",
    size: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    n: int = 1,
    timeout: float = 300,
    poll_interval: int = 4,
    deadline: Optional[float] = None,
    output_resolution: str = "2K",
    now=time.time,
) -> Dict[str, Any]:
    if not (model_id or "").strip():
        raise LeonardoError("model_id is required")

    aspect = to_aspect(size=size, aspect_ratio=aspect_ratio)
    quantity = clamp_quantity(n)

    # 单张积分成本：优先用上游 Generate 回报的 apiCreditCost；实测该字段恒为 null，
    # 故回退到「生成前后余额差分」实测（余额查询免费，不消耗积分）。
    cost_holder: Dict[str, Any] = {}
    balance_before = _read_balance(client, token, deadline)
    create_kwargs = {
        "quantity": quantity,
        "model_slug": model_slug,
        "output_resolution": output_resolution,
        "on_cost": lambda value: cost_holder.__setitem__("credit_cost", value),
    }
    if deadline is not None:
        create_kwargs["deadline"] = deadline
    gen_id = client.create_generation(
        token, prompt, model_id, aspect, **create_kwargs
    )
    try:
        wait_kwargs = {"timeout": timeout, "poll_interval": poll_interval}
        if deadline is not None:
            wait_kwargs["deadline"] = deadline
        result = client.wait_for_completion(token, gen_id, **wait_kwargs)
    except LeonardoError as exc:
        # mutation 已提交: 轮询/取图期间任何失败(含传输耗尽的 LeonardoError)
        # 都不得换号重发, 统一转 LeonardoGenerationError(不可重试)
        raise LeonardoGenerationError(str(exc)) from exc
    if not result.get("success"):
        raise LeonardoGenerationError(str(result.get("error") or "generation failed"))

    urls: List[str] = result.get("images") or []
    credit_cost = cost_holder.get("credit_cost")
    credit_cost_source = "upstream" if credit_cost is not None else None
    if credit_cost is None:
        measured = _measure_cost(client, token, balance_before, deadline)
        if measured is not None:
            credit_cost, credit_cost_source = measured, "measured"
    return {
        "created": int(now()),
        "data": [{"url": url} for url in urls],
        "provider": {
            "generation_id": gen_id,
            "aspect_ratio": aspect,
            "model_id": model_id,
            "credit_cost": credit_cost,
            "credit_cost_source": credit_cost_source,
        },
    }
