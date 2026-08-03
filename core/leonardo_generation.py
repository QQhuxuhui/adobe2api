import time
from typing import Any, Dict, List, Optional

from core.leonardo_client import LeonardoError


class LeonardoGenerationError(LeonardoError):
    """生成已提交后失败(轮询超时/上游 FAILED)。重试会重复扣费，不得自动重发。"""


_SUPPORTED_ASPECTS = {"16:9", "9:16", "1:1", "4:3"}
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
    from core.leonardo_client import LeonardoRetryUnsafeError

    if isinstance(exc, (LeonardoGenerationError, LeonardoRetryUnsafeError)):
        return "unsafe"
    message = str(exc).lower()
    if any(kw in message for kw in ("invalid", "jwt", "unauthorized", "verify", "signature")):
        return "auth"
    if any(kw in message for kw in ("quota", "insufficient", "exhausted", "credits")):
        return "quota"
    return "temp"


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
    timeout: int = 300,
    poll_interval: int = 4,
    now=time.time,
) -> Dict[str, Any]:
    if not (model_id or "").strip():
        raise LeonardoError("model_id is required")

    aspect = to_aspect(size=size, aspect_ratio=aspect_ratio)
    quantity = clamp_quantity(n)

    gen_id = client.create_generation(
        token, prompt, model_id, aspect, quantity=quantity, model_slug=model_slug
    )
    try:
        result = client.wait_for_completion(
            token, gen_id, timeout=timeout, poll_interval=poll_interval
        )
    except LeonardoError as exc:
        # mutation 已提交: 轮询/取图期间任何失败(含传输耗尽的 LeonardoError)
        # 都不得换号重发, 统一转 LeonardoGenerationError(不可重试)
        raise LeonardoGenerationError(str(exc)) from exc
    if not result.get("success"):
        raise LeonardoGenerationError(str(result.get("error") or "generation failed"))

    urls: List[str] = result.get("images") or []
    return {
        "created": int(now()),
        "data": [{"url": url} for url in urls],
        "provider": {
            "generation_id": gen_id,
            "aspect_ratio": aspect,
            "model_id": model_id,
        },
    }
