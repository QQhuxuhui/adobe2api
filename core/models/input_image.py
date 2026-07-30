"""输入图归一化：放宽客户端上限，同时把送往 Firefly 的字节收敛到已验证范围。

背景与取舍：
- 旧行为是单张 >10MB 直接 400 拒绝，手机原图（常见 15~25MB）直接用不了。
- Firefly 上游真实能接受多大未经验证，≤10MB 是目前唯一验证过安全的包络，
  因此**接收上限放宽、上传上限不动**：超限的图在本地重编码/缩放到 10MB 以内。
- **usage 一律按客户提交的原始尺寸算**，与官方语义及 sub2api 侧的本地计算同口径。
  这条是必需的而非可选：常见照片比例（1:1/4:3/3:2/16:9）缩到长边 2048 后
  token 恰好不变（都仍在 1536 patch 封顶区），但极端比例会掉出封顶区而少算 ——
  实测 8000×2000 从 1444 掉到 1024、12000×1500 从 1526 掉到 512（少 66%）。
  若按压缩后尺寸计费，全景图会被严重少收。
"""

from __future__ import annotations

import io
import warnings
from typing import Optional

from PIL import Image, UnidentifiedImageError


# 客户端可提交的单张上限（超过直接拒，防止 6 张 × 超大图打爆内存）。
MAX_ACCEPTED_IMAGE_BYTES = 30 * 1024 * 1024
# 送往 Firefly 的单张上限：保持既有已验证包络，不放宽。
MAX_UPSTREAM_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ACCEPTED_IMAGE_MIB = MAX_ACCEPTED_IMAGE_BYTES // (1024 * 1024)
# 逐级尝试的长边（None = 原尺寸只重编码；只有仍超限才真正缩像素）。
_LONGEST_EDGE_LADDER = (None, 3072, 2048, 1536, 1024)
_JPEG_QUALITY_LADDER = (90, 80, 70)


class InputImageError(ValueError):
    """输入图不可用（过大 / 解码失败）。调用方转成 400。"""


def normalize_image_mime(mime_type: Optional[str]) -> str:
    allowed = {"image/jpeg", "image/png", "image/webp"}
    normalized = str(mime_type or "").lower().strip()
    if normalized == "image/jpg":
        return "image/jpeg"
    return normalized if normalized in allowed else "image/jpeg"


def _decode(image_bytes: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(image_bytes))
            image.load()
            return image
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InputImageError("image pixel dimensions are too large") from exc
    except (OSError, TypeError, UnidentifiedImageError, ValueError) as exc:
        raise InputImageError("image cannot be decoded") from exc


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info


def _encode(image: Image.Image, keep_alpha: bool, quality: int) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if keep_alpha:
        # 带透明通道只能留 PNG（JPEG 无 alpha）。
        image.convert("RGBA").save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "image/png"
    image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def normalize_input_image(
    image_bytes: bytes, mime_type: Optional[str] = None
) -> tuple[bytes, str, int, int]:
    """把单张输入图归一到可上传状态。

    返回 `(上传用字节, 上传用 mime, 原图宽, 原图高)`。
    宽高是**客户提交的原始像素**，供 usage 计费使用（不受压缩影响）。
    未超上传上限时原样返回，不做任何重编码（避免无谓画质损失）。
    """
    if not image_bytes:
        raise InputImageError("image is empty")
    if len(image_bytes) > MAX_ACCEPTED_IMAGE_BYTES:
        raise InputImageError(f"image too large, max {MAX_ACCEPTED_IMAGE_MIB}MB")

    image = _decode(image_bytes)
    original_width, original_height = int(image.width), int(image.height)

    if len(image_bytes) <= MAX_UPSTREAM_IMAGE_BYTES:
        return image_bytes, normalize_image_mime(mime_type), original_width, original_height

    keep_alpha = _has_alpha(image)
    longest = max(original_width, original_height)
    best: Optional[tuple[bytes, str]] = None
    for edge in _LONGEST_EDGE_LADDER:
        if edge is None:
            candidate = image
        else:
            if edge >= longest:
                continue  # 该档不会缩小（原尺寸已在 edge=None 试过）
            scale = edge / longest
            candidate = image.resize(
                (
                    max(1, round(original_width * scale)),
                    max(1, round(original_height * scale)),
                ),
                Image.LANCZOS,
            )
        for quality in _JPEG_QUALITY_LADDER:
            encoded, encoded_mime = _encode(candidate, keep_alpha, quality)
            if best is None or len(encoded) < len(best[0]):
                best = (encoded, encoded_mime)
            if len(encoded) <= MAX_UPSTREAM_IMAGE_BYTES:
                return encoded, encoded_mime, original_width, original_height
            if keep_alpha:
                break  # PNG 无损，调 quality 无意义，直接进下一档尺寸

    # 走完全部阶梯仍超限（极端病态图）：返回最小的那个，让上游自己判定。
    if best is None:
        raise InputImageError("image cannot be compressed")
    return best[0], best[1], original_width, original_height
