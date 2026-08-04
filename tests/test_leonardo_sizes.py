"""Leonardo 出图尺寸按「模型族」适配（线上实测校准）。

实测结论（逐张量返回图像素）：
- nano-banana 系(gemini-image-2 / nano-banana-2)：1:1 只有 1024²(1K) 与 2048²(2K)；
  发 1536² 会被降到 1024²；**没有 4:3**——发 2048x1536 会被上游改写成 2048x2048(方图)，
  官方 4:3 档 2368x1792 直接被拒。
- gpt-image 系：1:1=1536²、4:3=2048x1536 均被精确接受。
不可实现的比例一律 400，不静默降级/改写。
"""
import pytest

from core.leonardo_client import (
    aspect_to_size,
    leonardo_family,
    leonardo_supported_aspects,
)

GEMINI_SLUGS = ("nano-banana-2", "gemini-image-2")
GPT_SLUGS = ("gpt-image-2",)


@pytest.mark.parametrize("slug", GEMINI_SLUGS)
def test_family_gemini(slug):
    assert leonardo_family(slug) == "gemini"


@pytest.mark.parametrize("slug", GPT_SLUGS)
def test_family_gpt(slug):
    assert leonardo_family(slug) == "gpt"


@pytest.mark.parametrize("slug", GEMINI_SLUGS)
def test_gemini_square_honours_resolution(slug):
    # imageSize 真正生效：1K→1024²、2K→2048²（旧实现恒发 1536² 被降到 1024²）
    assert aspect_to_size("1:1", model_slug=slug, output_resolution="1K") == (1024, 1024)
    assert aspect_to_size("1:1", model_slug=slug, output_resolution="2K") == (2048, 2048)


@pytest.mark.parametrize("slug", GEMINI_SLUGS)
def test_gemini_wide_ratios(slug):
    assert aspect_to_size("16:9", model_slug=slug, output_resolution="2K") == (2752, 1536)
    assert aspect_to_size("9:16", model_slug=slug, output_resolution="2K") == (1536, 2752)


@pytest.mark.parametrize("slug", GEMINI_SLUGS)
def test_gemini_has_no_4x3(slug):
    # 上游无 4:3 档位 → 返回 None（路由据此 400），绝不回退成方图
    assert aspect_to_size("4:3", model_slug=slug, output_resolution="2K") is None
    assert "4:3" not in leonardo_supported_aspects(slug)


@pytest.mark.parametrize("slug", GPT_SLUGS)
def test_gpt_sizes(slug):
    assert aspect_to_size("1:1", model_slug=slug, output_resolution="2K") == (1536, 1536)
    assert aspect_to_size("4:3", model_slug=slug, output_resolution="2K") == (2048, 1536)
    assert aspect_to_size("16:9", model_slug=slug, output_resolution="2K") == (2752, 1536)
    assert "4:3" in leonardo_supported_aspects(slug)


def test_gpt_image_1_is_its_own_family():
    # 实测：gpt-image-1 发 1536² 会被上游改写成 1024²，与 gpt-image-2 不同族。
    # 该模型暂不使用、未逐比例实测 → 只保留已验证的 1:1，其余一律 None(→400)。
    assert leonardo_family("gpt-image-1") == "gpt-image-1"
    assert aspect_to_size("1:1", model_slug="gpt-image-1") == (1024, 1024)
    assert leonardo_supported_aspects("gpt-image-1") == ("1:1",)
    for unverified in ("16:9", "9:16", "4:3"):
        assert aspect_to_size(unverified, model_slug="gpt-image-1") is None


def test_unknown_aspect_is_none_not_fallback():
    # 旧实现把未知比例静默回退成 1:1；现在必须显式 None，让上层 400
    assert aspect_to_size("7:5", model_slug="gpt-image-2") is None
    assert aspect_to_size("21:9", model_slug="gemini-image-2") is None


def test_supported_aspects_are_all_resolvable():
    for slug in GEMINI_SLUGS + GPT_SLUGS:
        for aspect in leonardo_supported_aspects(slug):
            for res in ("1K", "2K"):
                size = aspect_to_size(aspect, model_slug=slug, output_resolution=res)
                assert size is not None, (slug, aspect, res)
                w, h = size
                assert w > 0 and h > 0
