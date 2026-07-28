"""输入图 32px patch 公式的实测点回归。

期望值来自 sub2api docs/GPT_IMAGE_2_TOKEN_REFERENCE.md §6:
gpt-image-2 官方直连与 codex 两条管线的实测 token, 公式必须逐点吻合。
"""

import io

import pytest
from PIL import Image

from core.models.resolver import (
    _input_image_tokens_from_bytes,
    build_image_usage,
    input_image_tokens,
)


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (256, 256, 256),
        (512, 512, 1024),
        (512, 1024, 512),
        (550, 368, 704),
        (768, 768, 1024),
        (1024, 1024, 1024),
        (1280, 720, 920),
        (1536, 1024, 1536),
        (1536, 1536, 1521),  # 超限后 sqrt 缩小 + 0.99 收敛
        (2048, 1152, 1508),  # 两次 0.99 迭代
        (3840, 2160, 1508),
    ],
)
def test_input_image_tokens_measured_points(width, height, expected):
    assert input_image_tokens(width, height) == expected


def test_input_image_tokens_degenerate_dimensions():
    assert input_image_tokens(0, 100) == 0
    assert input_image_tokens(-1, 100) == 0


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height)).save(buf, format="PNG")
    return buf.getvalue()


def test_tokens_from_bytes_decodes_real_png():
    assert _input_image_tokens_from_bytes(_png(1280, 720)) == 920


def test_tokens_from_bytes_falls_back_on_undecodable():
    assert _input_image_tokens_from_bytes(b"not an image") == 1024


def test_build_image_usage_sums_per_image_tokens():
    usage = build_image_usage(
        "make it night",
        "2K",
        "1:1",
        [(_png(1024, 1024), "image/png"), (_png(3840, 2160), "image/png")],
    )
    assert usage["input_tokens_details"]["image_tokens"] == 1024 + 1508
    assert (
        usage["input_tokens"]
        == usage["input_tokens_details"]["text_tokens"] + 1024 + 1508
    )


def test_build_image_usage_no_input_images():
    usage = build_image_usage("draw a cat", "1K", "1:1", ())
    assert usage["input_tokens_details"]["image_tokens"] == 0
