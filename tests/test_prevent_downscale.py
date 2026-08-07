"""GPT Image size and quality stay independent at geometry resolution time."""

import pytest

from core.models.payloads import gpt_image_pixels_from_ratio
from core.models.resolver import prevent_gpt_image_downscale, resolve_image_geometry


@pytest.mark.parametrize(
    ("size", "ratio", "current", "expected"),
    [
        ("2048x2048", "1:1", "1K", "2K"),
        ("1536x1024", "3:2", "1K", "2K"),
        ("3840x2160", "16:9", "1K", "4K"),
        ("2560x1440", "16:9", "1K", "2K"),
        ("1280x720", "16:9", "1K", "1K"),
        ("1456x624", "21:9", "1K", "1K"),
        ("1024x1024", "1:1", "1K", "1K"),
        ("1024x1024", "1:1", "2K", "2K"),
        ("1024x1024", "1:1", "4K", "4K"),
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
    for tier in ("1K", "2K", "4K"):
        px = gpt_image_pixels_from_ratio(ratio, tier)
        size = f"{px['width']}x{px['height']}"
        resolved = prevent_gpt_image_downscale(size, ratio, "1K")
        delivered = gpt_image_pixels_from_ratio(ratio, resolved)
        assert delivered["width"] >= px["width"]
        assert delivered["height"] >= px["height"]


def test_geometry_preserves_1024_size_when_quality_is_medium():
    geo = resolve_image_geometry(
        {"size": "1024x1024", "quality": "medium"}, "gpt-image-2"
    )

    assert geo.output_resolution == "1K"
    assert geo.output_size == {"width": 1024, "height": 1024}


def test_geometry_preserves_2048_size_when_quality_is_low():
    geo = resolve_image_geometry(
        {"size": "2048x2048", "quality": "low"}, "gpt-image-2"
    )

    assert geo.output_resolution == "2K"
    assert geo.output_size == {"width": 2048, "height": 2048}


def test_geometry_end_to_end_default_quality_unchanged():
    """未传 quality 时仍按 size 推导基础分辨率档。"""
    geo = resolve_image_geometry({"size": "1456x624"}, "gpt-image-2")
    assert geo.output_resolution == "2K"
    assert geo.output_size == {"width": 1456, "height": 624}


def test_fixed_gpt_image_keeps_legacy_downscale_protection():
    geo = resolve_image_geometry(
        {"size": "3840x3840", "quality": "low"},
        "firefly-gpt-image-2k-1x1",
    )

    assert geo.output_resolution == "4K"
    assert geo.output_size is None
