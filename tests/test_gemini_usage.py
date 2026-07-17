import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.models.gemini_usage as usage


def test_empty_text_does_not_create_a_text_token():
    total, details = usage.build_prompt_usage("", 1, "pro")
    assert total == 560
    assert details == [{"modality": "IMAGE", "tokenCount": 560}]


def test_pro_2k_usage_has_pro_identity(monkeypatch):
    values = iter([90, 155])
    monkeypatch.setattr(usage, "gemini_usage_rand", lambda low, high: next(values))
    result = usage.build_image_usage_metadata("abcd", 2, "pro", "2K")
    assert result["promptTokenCount"] == 1121
    assert result["candidatesTokenCount"] == 1210
    assert result["thoughtsTokenCount"] == 155
    assert result["totalTokenCount"] == 2486
    assert result["candidatesTokensDetails"] == [
        {"modality": "IMAGE", "tokenCount": 1120}
    ]
    assert result["serviceTier"] == "standard"
    assert "trafficType" not in result


def test_flash_2k_usage_has_flash_identity(monkeypatch):
    monkeypatch.setattr(usage, "gemini_usage_rand", lambda low, high: 411)
    result = usage.build_image_usage_metadata("abcd", 1, "flash", "2K")
    assert result["promptTokenCount"] == 1121
    assert result["candidatesTokenCount"] == 2091
    assert result["totalTokenCount"] == 3212
    assert result["candidatesTokensDetails"] == [
        {"modality": "TEXT", "tokenCount": 411},
        {"modality": "IMAGE", "tokenCount": 1680},
    ]
    assert result["trafficType"] == "ON_DEMAND"
    assert "thoughtsTokenCount" not in result
    assert "serviceTier" not in result


def test_canned_usage_is_deterministic():
    result = usage.build_canned_usage_metadata("ping")
    assert usage.CANNED_TEXT == "ok"
    assert result["candidatesTokenCount"] == 1
    assert result["totalTokenCount"] == result["promptTokenCount"] + 1
    assert result["serviceTier"] == "standard"
    assert "thoughtsTokenCount" not in result
    assert "candidatesTokensDetails" not in result


@pytest.mark.parametrize(
    ("image_size", "image_tokens", "text_bounds", "thought_bounds"),
    [
        ("1K", 1120, (78, 92), (115, 140)),
        ("2K", 1120, (80, 100), (145, 165)),
        ("4K", 2000, (92, 112), (150, 170)),
    ],
)
def test_pro_output_bands_and_random_bounds(
    monkeypatch, image_size, image_tokens, text_bounds, thought_bounds
):
    calls = []

    def use_lower_bound(low, high):
        calls.append((low, high))
        return low

    monkeypatch.setattr(usage, "gemini_usage_rand", use_lower_bound)
    result = usage.build_image_usage_metadata("abcd", 0, "pro", image_size)

    assert calls == [text_bounds, thought_bounds]
    assert result["candidatesTokenCount"] == image_tokens + text_bounds[0]
    assert result["candidatesTokensDetails"] == [
        {"modality": "IMAGE", "tokenCount": image_tokens}
    ]
    assert result["thoughtsTokenCount"] == thought_bounds[0]
    assert result["totalTokenCount"] == (
        result["promptTokenCount"]
        + result["candidatesTokenCount"]
        + result["thoughtsTokenCount"]
    )


@pytest.mark.parametrize(
    ("image_size", "image_tokens", "text_bounds"),
    [
        ("1K", 1120, (250, 320)),
        ("2K", 1680, (380, 440)),
        ("4K", 2520, (520, 600)),
    ],
)
def test_flash_output_bands_and_random_bounds(
    monkeypatch, image_size, image_tokens, text_bounds
):
    calls = []

    def use_upper_bound(low, high):
        calls.append((low, high))
        return high

    monkeypatch.setattr(usage, "gemini_usage_rand", use_upper_bound)
    result = usage.build_image_usage_metadata("abcd", 0, "flash", image_size)

    assert calls == [text_bounds]
    assert result["candidatesTokenCount"] == image_tokens + text_bounds[1]
    assert result["candidatesTokensDetails"] == [
        {"modality": "TEXT", "tokenCount": text_bounds[1]},
        {"modality": "IMAGE", "tokenCount": image_tokens},
    ]
    assert result["totalTokenCount"] == (
        result["promptTokenCount"] + result["candidatesTokenCount"]
    )


@pytest.mark.parametrize(("family", "per_image"), [("pro", 560), ("flash", 1120)])
def test_prompt_usage_prices_every_input_image(family, per_image):
    total, details = usage.build_prompt_usage("abcd", 3, family)
    assert total == 1 + 3 * per_image
    assert details == [
        {"modality": "TEXT", "tokenCount": 1},
        {"modality": "IMAGE", "tokenCount": 3 * per_image},
    ]


def test_count_tokens_reports_prompt_side_only():
    result = usage.build_count_tokens_response("abcd", 2, "flash")
    assert result == {
        "totalTokens": 2241,
        "promptTokensDetails": [
            {"modality": "TEXT", "tokenCount": 1},
            {"modality": "IMAGE", "tokenCount": 2240},
        ],
    }


def test_count_tokens_omits_empty_text_detail():
    result = usage.build_count_tokens_response("", 1, "pro")
    assert result == {
        "totalTokens": 560,
        "promptTokensDetails": [{"modality": "IMAGE", "tokenCount": 560}],
    }


def test_count_tokens_omits_details_when_prompt_is_empty():
    assert usage.build_count_tokens_response("", 0, "pro") == {"totalTokens": 0}
