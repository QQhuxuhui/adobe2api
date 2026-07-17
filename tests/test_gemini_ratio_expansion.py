"""比例扩展回归测试：pro/flash 应接受 Gemini 官方全部 10 个比例，
且 size_from_ratio 对新比例返回真实尺寸而非回退 16:9。

先失败后通过（TDD）：改代码前 2:3/3:2/4:5/5:4/21:9 会被 400 拒绝、
size_from_ratio 会回退 16:9。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routes.gemini_native import (
    FLASH_RATIOS,
    PRO_RATIOS,
    GEMINI_MODELS,
    parse_gemini_request,
)
from core.models.payloads import size_from_ratio

NEW_RATIOS = ["2:3", "3:2", "4:5", "5:4", "21:9"]
# Gemini 3 Pro Image 官方 10 比例
GEMINI_OFFICIAL_RATIOS = {
    "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
}


def _body(ratio):
    return json.dumps(
        {
            "contents": [{"parts": [{"text": "draw"}]}],
            "generationConfig": {"imageConfig": {"aspectRatio": ratio}},
        }
    ).encode("utf-8")


def _pro():
    return GEMINI_MODELS["gemini-3-pro-image"]


def _flash():
    return GEMINI_MODELS["gemini-3.1-flash-image"]


def test_pro_whitelist_covers_all_official_ratios():
    assert GEMINI_OFFICIAL_RATIOS <= PRO_RATIOS
    assert GEMINI_OFFICIAL_RATIOS <= FLASH_RATIOS


@pytest.mark.parametrize("ratio", NEW_RATIOS)
def test_pro_accepts_new_ratios(ratio):
    parsed = parse_gemini_request(_body(ratio), _pro())
    assert parsed.aspect_ratio == ratio


@pytest.mark.parametrize("ratio", NEW_RATIOS)
def test_flash_accepts_new_ratios(ratio):
    parsed = parse_gemini_request(_body(ratio), _flash())
    assert parsed.aspect_ratio == ratio


@pytest.mark.parametrize("ratio", NEW_RATIOS)
@pytest.mark.parametrize("level", ["1K", "2K", "4K"])
def test_size_from_ratio_has_real_dims_for_new_ratios(ratio, level):
    dims = size_from_ratio(ratio, level)
    w, h = dims["width"], dims["height"]
    # 不得回退到 16:9（宽>高且约 1.78）
    rw, rh = (int(x) for x in ratio.split(":"))
    expected = rw / rh
    actual = w / h
    assert abs(actual - expected) / expected < 0.02, (
        f"{ratio}@{level} -> {w}x{h} 比例 {actual:.3f} 偏离目标 {expected:.3f}（疑似回退）"
    )
    assert w % 16 == 0 and h % 16 == 0
