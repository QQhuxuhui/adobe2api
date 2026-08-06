"""Leonardo 积分估价表。

存在的理由：精确成本的两个来源线上都不可靠——上游 apiCreditCost 恒为 null，
余额差分在账号被他处并发使用时会被判污染而整条丢弃。没有估算兜底的话，
请求日志里的积分列大面积为空，单次消耗看不出、成本也没法预估。

数字取自 README「单张积分成本」的线上实测表。
"""

import pytest

from core.leonardo_pricing import estimate_credits, is_leonardo_model


def _leo(slug):
    return {"upstream_model": f"leonardo:{slug}"}


ADOBE = {"upstream_model": "firefly-nano-banana-pro"}


def test_only_leonardo_models_are_priced():
    assert is_leonardo_model(_leo("nano-banana-2"))
    assert not is_leonardo_model(ADOBE)
    assert not is_leonardo_model({})
    assert not is_leonardo_model(None)
    assert estimate_credits(ADOBE, output_resolution="2K") is None


# --- 按张固定计费 ---


@pytest.mark.parametrize(
    "resolution,expected", [("1K", 80.0), ("2K", 120.0), ("1k", 80.0)]
)
def test_flash_is_priced_by_resolution_tier(resolution, expected):
    assert estimate_credits(_leo("nano-banana-2"), output_resolution=resolution) == expected


def test_pro_is_flat_regardless_of_resolution():
    for resolution in ("1K", "2K", "4K"):
        assert estimate_credits(_leo("gemini-image-2"), output_resolution=resolution) == 140.0


def test_unknown_resolution_tier_returns_none():
    """宁可留空也不要写一个错数字——空着好排查。"""
    assert estimate_credits(_leo("nano-banana-2"), output_resolution="8K") is None


# --- 按像素线性计费 ---


@pytest.mark.parametrize(
    "w,h,expected",
    [
        (1536, 1536, 146.4),   # README 实测 1:1 ≈146
        (2048, 1536, 195.0),   # README 实测 4:3 ≈195
    ],
)
def test_gpt_image_is_priced_per_megapixel(w, h, expected):
    got = estimate_credits(_leo("gpt-image-2"), width=w, height=h)
    assert got == pytest.approx(expected, abs=1.0)


def test_pixel_priced_model_without_size_returns_none():
    assert estimate_credits(_leo("gpt-image-2")) is None
    assert estimate_credits(_leo("gpt-image-2"), width=0, height=0) is None


# --- 张数与图生图 ---


def test_quantity_multiplies():
    assert estimate_credits(_leo("gemini-image-2"), quantity=3) == 420.0


@pytest.mark.parametrize("bad", [None, 0, -1, "x"])
def test_bad_quantity_falls_back_to_one(bad):
    assert estimate_credits(_leo("gemini-image-2"), quantity=bad) == 140.0


def test_edit_uses_its_own_rate():
    """图生图（omni edit）上游是另一套计价，不能套用同模型的文生图单价。"""
    assert estimate_credits(_leo("nano-banana-2"), output_resolution="2K") == 120.0
    assert estimate_credits(_leo("nano-banana-2"), output_resolution="2K", is_edit=True) == 292.0


def test_unknown_slug_returns_none():
    assert estimate_credits(_leo("some-future-model"), output_resolution="2K") is None


@pytest.mark.parametrize("w,h", [("abc", 100), (None, None)])
def test_dirty_size_never_raises(w, h):
    assert estimate_credits(_leo("gpt-image-2"), width=w, height=h) is None
