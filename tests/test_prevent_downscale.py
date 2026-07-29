"""显式 size 作为交付下界的回归测试。

背景：quality 直接决定输出档位(low→1K/medium→2K/high→4K)，size 只用来定比例，
于是 `2048x2048 + quality=low` 会交付 1024x1024——客户明确要 2K 却拿到 1K。
官方语义中 size 与 quality 正交（实测 2048x2048+low 返回 2048x2048），
这里保证不缩水，同时保留反向的超额交付（1024x1024+high 仍给 4K）。
"""

import pytest

from core.models.payloads import gpt_image_pixels_from_ratio
from core.models.resolver import prevent_gpt_image_downscale, resolve_image_geometry


@pytest.mark.parametrize(
    ("size", "ratio", "current", "expected"),
    [
        # 缩水 → 抬档
        ("2048x2048", "1:1", "1K", "2K"),
        ("1536x1024", "3:2", "1K", "2K"),
        ("3840x2160", "16:9", "1K", "4K"),  # 需要连抬两级
        ("2560x1440", "16:9", "1K", "2K"),
        # 请求尺寸恰好等于该档尺寸 → 不动
        ("1280x720", "16:9", "1K", "1K"),
        ("1456x624", "21:9", "1K", "1K"),
        ("1024x1024", "1:1", "1K", "1K"),
        # 超额交付 → 保留，不下调
        ("1024x1024", "1:1", "2K", "2K"),
        ("1024x1024", "1:1", "4K", "4K"),
        # 无法作为下界的输入 → 不动
        ("", "1:1", "1K", "1K"),
        ("auto", "1:1", "1K", "1K"),
        ("0x0", "1:1", "1K", "1K"),
        ("-1x100", "1:1", "1K", "1K"),
        ("2048x2048", "1:1", "未知档", "未知档"),
    ],
)
def test_prevent_gpt_image_downscale(size, ratio, current, expected):
    assert prevent_gpt_image_downscale(size, ratio, current) == expected


@pytest.mark.parametrize("ratio", ["1:1", "5:4", "4:3", "3:2", "16:9", "21:9"])
def test_no_ratio_ever_delivers_smaller_than_requested(ratio):
    """对每个比例的三档尺寸，请求该尺寸时交付不得更小。"""
    for tier in ("1K", "2K", "4K"):
        px = gpt_image_pixels_from_ratio(ratio, tier)
        size = f"{px['width']}x{px['height']}"
        resolved = prevent_gpt_image_downscale(size, ratio, "1K")
        delivered = gpt_image_pixels_from_ratio(ratio, resolved)
        assert delivered["width"] >= px["width"]
        assert delivered["height"] >= px["height"]


def test_geometry_end_to_end_low_quality_keeps_requested_size():
    """端到端：2048x2048 + low 不再降到 1K。"""
    geo = resolve_image_geometry({"size": "2048x2048", "quality": "low"}, "gpt-image-2")
    assert geo.output_resolution == "2K"
    px = gpt_image_pixels_from_ratio(geo.aspect_ratio, geo.output_resolution)
    assert (px["width"], px["height"]) == (2048, 2048)


def test_geometry_end_to_end_high_quality_upgrade_preserved():
    """端到端：1024x1024 + high 仍然升到 4K（超额交付不受影响）。"""
    geo = resolve_image_geometry({"size": "1024x1024", "quality": "high"}, "gpt-image-2")
    assert geo.output_resolution == "4K"


def test_geometry_end_to_end_default_quality_unchanged():
    """端到端：不传 quality 的行为保持原样（按 size 最长边推档）。"""
    geo = resolve_image_geometry({"size": "1456x624"}, "gpt-image-2")
    assert geo.output_resolution == "2K"
