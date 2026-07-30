"""输入图归一化测试。

核心保证：
1. 接收上限放宽到 30MB，超限才拒；
2. 送往 Firefly 的字节始终 ≤10MB（已验证包络不放宽）；
3. **计费按客户原图尺寸算，与压缩无关** —— 这是整个方案成立的前提。
"""

import io

import pytest
from PIL import Image

from core.models.input_image import (
    MAX_ACCEPTED_IMAGE_BYTES,
    MAX_UPSTREAM_IMAGE_BYTES,
    InputImageError,
    normalize_input_image,
)
from core.models.resolver import build_image_usage, input_image_tokens


def _noisy_png(width: int, height: int) -> bytes:
    """高熵图：PNG 压不动，用来可靠地造出超限体积。"""
    import random

    rnd = random.Random(1234)
    image = Image.new("RGB", (width, height))
    image.putdata([
        (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        for _ in range(width * height)
    ])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    return buffer.getvalue()


def _plain_png(width: int, height: int, alpha: bool = False) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    color = (40, 80, 120, 128) if alpha else (40, 80, 120)
    buffer = io.BytesIO()
    Image.new(mode, (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_under_limit_passes_through_untouched():
    raw = _plain_png(1024, 1024)
    assert len(raw) <= MAX_UPSTREAM_IMAGE_BYTES
    out, mime, width, height = normalize_input_image(raw, "image/png")
    assert out is raw, "未超限不应重编码"
    assert mime == "image/png"
    assert (width, height) == (1024, 1024)


def test_rejects_beyond_accepted_limit():
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_ACCEPTED_IMAGE_BYTES + 1)
    with pytest.raises(InputImageError) as excinfo:
        normalize_input_image(oversized, "image/png")
    assert "30MB" in str(excinfo.value)


def test_rejects_undecodable():
    with pytest.raises(InputImageError) as excinfo:
        normalize_input_image(b"not-an-image", "image/png")
    assert "cannot be decoded" in str(excinfo.value)


def test_rejects_empty():
    with pytest.raises(InputImageError):
        normalize_input_image(b"", "image/png")


def test_oversized_is_compressed_under_upstream_limit_and_keeps_original_dims():
    raw = _noisy_png(2600, 2000)  # 高熵未压缩 PNG，约 15MB
    assert len(raw) > MAX_UPSTREAM_IMAGE_BYTES, f"夹具没超限: {len(raw)}"
    out, mime, width, height = normalize_input_image(raw, "image/png")
    assert len(out) <= MAX_UPSTREAM_IMAGE_BYTES, "上传字节必须收敛到 10MB 内"
    assert mime == "image/jpeg", "无 alpha 走 JPEG 才压得下来"
    assert (width, height) == (2600, 2000), "返回的必须是原图尺寸"
    # 上传的图仍可解码
    Image.open(io.BytesIO(out)).load()


def test_oversized_with_alpha_stays_png():
    raw = _noisy_png(2600, 2000)
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    raw_alpha = buffer.getvalue()
    assert len(raw_alpha) > MAX_UPSTREAM_IMAGE_BYTES
    out, mime, width, height = normalize_input_image(raw_alpha, "image/png")
    assert mime == "image/png", "带透明通道不能转 JPEG"
    assert len(out) <= MAX_UPSTREAM_IMAGE_BYTES
    assert (width, height) == (2600, 2000)
    assert Image.open(io.BytesIO(out)).mode == "RGBA"


@pytest.mark.parametrize(
    ("width", "height"),
    [(4032, 3024), (6000, 4000), (5000, 5000), (3840, 2160), (2600, 2000)],
)
def test_typical_ratios_keep_tokens_after_shrink(width, height):
    """常见照片比例缩到长边 2048/3072 后 token 不变（仍在 1536 patch 封顶区）。"""
    original = input_image_tokens(width, height)
    longest = max(width, height)
    for edge in (3072, 2048):
        if edge >= longest:
            continue
        scale = edge / longest
        shrunk = input_image_tokens(round(width * scale), round(height * scale))
        assert shrunk == original, f"{width}x{height} 缩到长边 {edge} 后 token 变了"


@pytest.mark.parametrize(
    ("width", "height", "expected_loss"),
    [(8000, 2000, 420), (12000, 1500, 1014), (2000, 8000, 420)],
)
def test_extreme_ratios_would_underbill_if_measured_after_shrink(
    width, height, expected_loss
):
    """极端比例缩小后会掉出 1536 patch 封顶区 —— 这正是必须按原图尺寸计费的原因。

    若改成按压缩后尺寸算，全景/长条图会被少收（8:1 少 66%）。
    本测试锁死这个前提，防止将来有人"顺手"把计费改成用压缩后的字节。
    """
    original = input_image_tokens(width, height)
    scale = 2048 / max(width, height)
    shrunk = input_image_tokens(round(width * scale), round(height * scale))
    assert shrunk == original - expected_loss, "封顶行为变了，需重新评估计费口径"


def test_usage_uses_original_dimensions_not_compressed_bytes():
    """端到端：条目带原图尺寸时，usage 按原图算，与上传字节尺寸无关。"""
    compressed = _plain_png(512, 512)  # 假装是压缩后的小图
    usage = build_image_usage(
        "x", "2K", "1:1", [(compressed, "image/jpeg", 4032, 3024)]
    )
    assert usage["input_tokens_details"]["image_tokens"] == input_image_tokens(4032, 3024)


def test_usage_falls_back_to_bytes_when_dims_absent():
    """旧式 2 元组仍按字节解码算，保持向后兼容。"""
    raw = _plain_png(1024, 1024)
    usage = build_image_usage("x", "2K", "1:1", [(raw, "image/png")])
    assert usage["input_tokens_details"]["image_tokens"] == input_image_tokens(1024, 1024)
