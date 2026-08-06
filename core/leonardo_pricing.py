"""Leonardo 单张积分成本的估价表。

为什么需要它：Leonardo 的精确成本只有两个来源，线上都不可靠——
  - 上游 `apiCreditCost` 实测恒为 null（见 core/leonardo_client.py:parse_generation_cost）；
  - 余额差分实测在账号被他处并发使用时会混入别人的消耗，代码对离谱差值直接丢弃。
结果是请求日志里 credits_used 大面积为空，既看不出单次消耗，也无法预估成本。

这张表把 README 里实测出来的单价固化成代码，让**每个请求都有积分数字**：
测得到就用实测值（credits_source="measured"），测不到就按表估算
（credits_source="estimated"），前端能区分两者。

数字来源：线上余额差分实测，见 README「单张积分成本」。
gpt-image 系是按像素线性计费（约 62 积分/百万像素），三次独立测量都落在这个斜率上，
所以用公式而不是枚举尺寸；其余模型按张固定。
"""

from typing import Optional

# 按像素线性计费的模型：积分/百万像素
_PER_MEGAPIXEL = {
    "gpt-image-2": 62.0,
}

# 按张固定计费的模型：slug -> {output_resolution -> 积分}
# "*" 表示与尺寸无关
_FLAT_RATE = {
    "nano-banana-2": {"1K": 80.0, "2K": 120.0},
    "gemini-image-2": {"*": 140.0},
    # gpt-image-1 只验证过 1024²=135，与 gpt-image-2 不是同一族、
    # 也不落在 62/Mpx 那条线上（按该斜率只有约 65）。它上游也只支持这一个尺寸，
    # 其余尺寸直接 400，所以按固定单价处理，不要外推。
    "gpt-image-1": {"*": 135.0},
}

# 图生图（omni edit）实测单张成本。上游对编辑走的是另一套计价，
# 与同模型文生图不同，故单列。
_EDIT_FLAT_RATE = 292.0


def _slug_of(model_config) -> str:
    """从 catalog 条目取 Leonardo 的 upstream slug；非 Leonardo 返回空串。"""
    upstream = str((model_config or {}).get("upstream_model") or "")
    if not upstream.startswith("leonardo:"):
        return ""
    return upstream.split(":", 1)[1].strip()


def is_leonardo_model(model_config) -> bool:
    return bool(_slug_of(model_config))


def estimate_credits(
    model_config,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    output_resolution: str = "2K",
    quantity: int = 1,
    is_edit: bool = False,
) -> Optional[float]:
    """估算本次请求消耗的积分；估不出来返回 None。

    宁可返回 None 也不要瞎猜——日志里空着比写一个错数字好排查。
    """
    slug = _slug_of(model_config)
    if not slug:
        return None
    try:
        count = max(1, int(quantity or 1))
    except (TypeError, ValueError):
        count = 1

    if is_edit:
        return _EDIT_FLAT_RATE * count

    flat = _FLAT_RATE.get(slug)
    if flat:
        per_image = flat.get(str(output_resolution or "").upper()) or flat.get("*")
        return per_image * count if per_image else None

    rate = _PER_MEGAPIXEL.get(slug)
    if rate and width and height:
        try:
            megapixels = (int(width) * int(height)) / 1_000_000
        except (TypeError, ValueError):
            return None
        if megapixels <= 0:
            return None
        return round(rate * megapixels, 1) * count
    return None
