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
# Generate 出错默认按「可能已受理」处理(不可重试，防重复扣费)；下列措辞是上游
# **明确未受理**的拒绝——没建生成任务、不扣积分，可安全重试。
# "Pending generations limit exceeded" = 账号同时处理中的任务数超上限(并发限制)。
_SAFE_GENERATE_REJECTIONS = (
    "pending generations limit",
    "too many pending",
    "rate limit",
)
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
        # 并发上限是「明确未受理」的拒绝：上游没建生成任务、不扣积分，可安全重试。
        # 其余语义不明的 Generate 错误仍按不可重试处理（可能已受理并扣费）。
        if any(kw in str(exc).lower() for kw in _SAFE_GENERATE_REJECTIONS):
            return "temp"
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


def _extension_from_mime(mime: str) -> str:
    ext = str(mime or "").split("/")[-1].split(";")[0].strip().lower()
    return {"jpg": "jpeg"}.get(ext, ext) or "png"


def edit_images(
    client,
    token: str,
    *,
    prompt: str,
    model_slug: str,
    model_id: str,
    input_images,
    aspect_ratio: Optional[str] = None,
    size: Optional[str] = None,
    output_resolution: str = "2K",
    timeout: float = 300,
    poll_interval: int = 4,
    deadline: Optional[float] = None,
    now=time.time,
) -> Dict[str, Any]:
    """Leonardo 图生图：上传参考图 → omni edit 生成。

    输出尺寸沿用文生图的实测尺寸表（同模型同比例）。
    """
    from core.leonardo_client import aspect_to_size

    images = list(input_images or [])
    if not images:
        raise LeonardoError("edit requires at least one input image")

    aspect = to_aspect(size=size, aspect_ratio=aspect_ratio)
    wh = aspect_to_size(aspect, model_slug=model_slug, output_resolution=output_resolution)
    if wh is None:
        raise LeonardoError(
            f"aspect ratio {aspect} is not supported by model {model_slug}"
        )
    width, height = wh

    upload_kwargs = {"deadline": deadline} if deadline is not None else {}
    init_ids = [
        client.upload_init_image(
            token, image_bytes, extension=_extension_from_mime(mime), **upload_kwargs
        )
        for image_bytes, mime, *_ in images
    ]

    cost_holder: Dict[str, Any] = {}
    balance_before = _read_balance(client, token, deadline)
    create_kwargs = {"on_cost": lambda v: cost_holder.__setitem__("credit_cost", v)}
    if deadline is not None:
        create_kwargs["deadline"] = deadline
    gen_id = client.create_edit_generation(
        token, prompt, model_slug, width, height, init_ids, **create_kwargs
    )
    try:
        wait_kwargs = {"timeout": timeout, "poll_interval": poll_interval}
        if deadline is not None:
            wait_kwargs["deadline"] = deadline
        result = client.wait_for_completion(token, gen_id, **wait_kwargs)
    except LeonardoError as exc:
        raise LeonardoGenerationError(str(exc)) from exc
    if not result.get("success"):
        raise LeonardoGenerationError(str(result.get("error") or "generation failed"))

    credit_cost = cost_holder.get("credit_cost")
    credit_cost_source = "upstream" if credit_cost is not None else None
    if credit_cost is None:
        measured = _measure_cost(client, token, balance_before, deadline)
        if measured is not None:
            credit_cost, credit_cost_source = measured, "measured"

    return {
        "created": int(now()),
        "data": [{"url": url} for url in (result.get("images") or [])],
        "provider": {
            "generation_id": gen_id,
            "aspect_ratio": aspect,
            "model_id": model_id,
            "init_image_ids": init_ids,
            "credit_cost": credit_cost,
            "credit_cost_source": credit_cost_source,
            # 供按像素计费的模型估算成本（测不到时的兜底）
            "output_size": {"width": width, "height": height},
            "quantity": 1,
        },
    }


def _read_balance(client, token: str, deadline) -> Optional[int]:
    """读出图可用额度；余额查询不消耗积分。任何失败都不得影响出图。

    deadline 必须传下去：这个查询会走 GraphQL 重试（3 次 × 60s），
    形参在这里躺着不用的话，一次生成前后两次余额查询最坏能多阻塞 6 分钟，
    而且发生在端到端预算已经烧穿之后。
    """
    reader = getattr(client, "get_credits", None)
    if not callable(reader):
        return None
    try:
        try:
            value = reader(token, deadline=deadline)
        except TypeError:
            # 旧签名/测试桩不认识 deadline
            value = reader(token)
    except Exception:  # noqa: BLE001 - 记账失败不影响出图
        return None
    return value if isinstance(value, (int, float)) else None


# 单张实测成本的合理上限：实测各模型 80~262（gpt-image 约 62 积分/百万像素，
# 最大尺寸 4.23Mpx≈262）。超过此值几乎必然是**账号被并发使用**——同一账号若在
# 别处同时出图，余额差分会把别人的消耗算进来，那样记录的数字是错的。
_MAX_PLAUSIBLE_COST = 600


def _measure_cost(client, token: str, before, deadline) -> Optional[int]:
    """生成前后余额差分 = 本次单张成本。

    差值非正（额度刚回补）或大得离谱（账号被并发使用，混入了他人消耗）时返回
    None——宁可不记，也不记错账。
    """
    if before is None:
        return None
    after = _read_balance(client, token, deadline)
    if after is None:
        return None
    diff = int(before) - int(after)
    if diff <= 0 or diff > _MAX_PLAUSIBLE_COST:
        return None
    return diff


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
    # 与 client 内部解尺寸用的是同一张表；按像素计费的模型要靠它估成本
    from core.leonardo_client import aspect_to_size as _aspect_to_size

    _wh = _aspect_to_size(
        aspect, model_slug=model_slug, output_resolution=output_resolution
    )
    width, height = _wh if _wh else (None, None)

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
