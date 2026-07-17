from __future__ import annotations

from random import randint
from typing import Callable

from core.models.resolver import _count_text_tokens

CANNED_TEXT = "ok"
gemini_usage_rand: Callable[[int, int], int] = randint

_INPUT_IMAGE_TOKENS = {"pro": 560, "flash": 1120}
_IMAGE_OUTPUT_TOKENS = {
    "pro": {"1K": 1120, "2K": 1120, "4K": 2000},
    "flash": {"1K": 1120, "2K": 1680, "4K": 2520},
}
_PRO_TEXT_BOUNDS = {"1K": (78, 92), "2K": (80, 100), "4K": (92, 112)}
_PRO_THOUGHT_BOUNDS = {"1K": (115, 140), "2K": (145, 165), "4K": (150, 170)}
_FLASH_TEXT_BOUNDS = {"1K": (250, 320), "2K": (380, 440), "4K": (520, 600)}


def count_text_tokens(text: str) -> int:
    value = str(text or "")
    return 0 if not value else _count_text_tokens(value)


def build_prompt_usage(
    prompt: str, image_count: int, family: str
) -> tuple[int, list[dict[str, int | str]]]:
    text_tokens = count_text_tokens(prompt)
    safe_image_count = max(0, int(image_count or 0))
    image_tokens = safe_image_count * _INPUT_IMAGE_TOKENS.get(family, 0)
    details: list[dict[str, int | str]] = []
    if text_tokens > 0:
        details.append({"modality": "TEXT", "tokenCount": text_tokens})
    if image_tokens > 0:
        details.append({"modality": "IMAGE", "tokenCount": image_tokens})
    return text_tokens + image_tokens, details


def build_count_tokens_response(prompt: str, image_count: int, family: str) -> dict:
    total, details = build_prompt_usage(prompt, image_count, family)
    result = {"totalTokens": total}
    if details:
        result["promptTokensDetails"] = details
    return result


def build_image_usage_metadata(
    prompt: str, image_count: int, family: str, image_size: str
) -> dict:
    normalized_size = str(image_size or "1K").upper()
    if family not in _IMAGE_OUTPUT_TOKENS or normalized_size not in {"1K", "2K", "4K"}:
        raise ValueError("unsupported Gemini usage profile")

    prompt_tokens, prompt_details = build_prompt_usage(prompt, image_count, family)
    image_output = _IMAGE_OUTPUT_TOKENS[family][normalized_size]
    result: dict[str, object] = {"promptTokenCount": prompt_tokens}
    if prompt_details:
        result["promptTokensDetails"] = prompt_details

    if family == "pro":
        text_output = gemini_usage_rand(*_PRO_TEXT_BOUNDS[normalized_size])
        thoughts = gemini_usage_rand(*_PRO_THOUGHT_BOUNDS[normalized_size])
        candidates = image_output + text_output
        result.update(
            {
                "candidatesTokenCount": candidates,
                "candidatesTokensDetails": [
                    {"modality": "IMAGE", "tokenCount": image_output}
                ],
                "thoughtsTokenCount": thoughts,
                "totalTokenCount": prompt_tokens + candidates + thoughts,
                "serviceTier": "standard",
            }
        )
        return result

    text_output = gemini_usage_rand(*_FLASH_TEXT_BOUNDS[normalized_size])
    candidates = image_output + text_output
    result.update(
        {
            "candidatesTokenCount": candidates,
            "candidatesTokensDetails": [
                {"modality": "TEXT", "tokenCount": text_output},
                {"modality": "IMAGE", "tokenCount": image_output},
            ],
            "totalTokenCount": prompt_tokens + candidates,
            "trafficType": "ON_DEMAND",
        }
    )
    return result


def build_canned_usage_metadata(prompt: str) -> dict:
    prompt_tokens, prompt_details = build_prompt_usage(prompt, 0, "text")
    candidate_tokens = count_text_tokens(CANNED_TEXT)
    result: dict[str, object] = {
        "promptTokenCount": prompt_tokens,
        "candidatesTokenCount": candidate_tokens,
        "totalTokenCount": prompt_tokens + candidate_tokens,
        "serviceTier": "standard",
    }
    if prompt_details:
        result["promptTokensDetails"] = prompt_details
    return result
