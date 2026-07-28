from __future__ import annotations

import io
import math
import warnings
from dataclasses import dataclass
from math import gcd
from typing import Optional, Sequence

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

from .catalog import (
    DEFAULT_AUTO_MODEL_ID,
    DEFAULT_MODEL_ID,
    MODEL_CATALOG,
    SUPPORTED_RATIOS,
)
from .payloads import size_from_dimensions


# OpenAI gpt-image-1 官方"输出图像 token"表: [质量档][朝向]。
# 依据 OpenAI 文档: 1024x1024/1024x1536/1536x1024 在 low/medium/high 下的输出 token。
# adobe 的分辨率档(1K/2K/4K)映射为 OpenAI 质量档(low/medium/high);
# aspect_ratio 映射为朝向(方形/竖版/横版)。这样下游看到的 token 与真 gpt-image-1 一致。
_GPT_IMAGE_OUTPUT_TOKENS = {
    "low":    {"square": 272,  "portrait": 408,  "landscape": 400},
    "medium": {"square": 1056, "portrait": 1584, "landscape": 1568},
    "high":   {"square": 4160, "portrait": 6240, "landscape": 6208},
}
_RES_TO_QUALITY = {"1K": "low", "2K": "medium", "4K": "high"}
# 输入图 token 按 OpenAI 官方 32px patch 公式逐张精确计算(gpt-image 系列实测一致):
# 最长边 <1024 先按 min(2, 1024/最长边) 放大; patch 数 = ceil(w/32)*ceil(h/32);
# 超过 1536 patch 按 sqrt(1536/patches) 等比缩小并以 0.99 步进收敛。
# 下游(sub2api)对远程 URL 改图会采信这里的 image_tokens 明细, 不能再用写死的估算值。
_INPUT_IMAGE_PATCH_LIMIT = 1536
_INPUT_IMAGE_UPSCALE_TARGET = 1024
# 图片字节读不出尺寸时的兜底(官方 1024x1024 的 patch 数, 最常见档)。
# 正常流程走不到: 能被 Adobe 接受生成的图, PIL 均能读出尺寸。
_INPUT_IMAGE_FALLBACK_TOKENS = 1024


def input_image_tokens(width: int, height: int) -> int:
    """OpenAI 官方输入图 32px patch 公式(与 gpt-image-2 官方直连/codex 实测点全部吻合)。"""
    if width <= 0 or height <= 0:
        return 0
    w, h = float(width), float(height)
    longest = max(w, h)
    if longest < _INPUT_IMAGE_UPSCALE_TARGET:
        factor = min(2.0, _INPUT_IMAGE_UPSCALE_TARGET / longest)
        w *= factor
        h *= factor
    patches = math.ceil(w / 32) * math.ceil(h / 32)
    if patches > _INPUT_IMAGE_PATCH_LIMIT:
        factor = math.sqrt(_INPUT_IMAGE_PATCH_LIMIT / patches)
        w *= factor
        h *= factor
        while math.ceil(w / 32) * math.ceil(h / 32) > _INPUT_IMAGE_PATCH_LIMIT:
            w *= 0.99
            h *= 0.99
        patches = math.ceil(w / 32) * math.ceil(h / 32)
    return patches


def _input_image_tokens_from_bytes(image_bytes: bytes) -> int:
    """从图片字节读出尺寸并套 patch 公式; 读不出时用兜底值, 不让 usage 构造失败。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                width, height = int(image.width), int(image.height)
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        TypeError,
        UnidentifiedImageError,
        ValueError,
    ):
        return _INPUT_IMAGE_FALLBACK_TOKENS
    tokens = input_image_tokens(width, height)
    return tokens if tokens > 0 else _INPUT_IMAGE_FALLBACK_TOKENS


def _orientation_of(ratio: str) -> str:
    try:
        w, h = (float(x) for x in str(ratio or "1:1").split(":")[:2])
        if w <= 0 or h <= 0:
            return "square"
        r = w / h
    except (ValueError, TypeError, ZeroDivisionError):
        return "square"
    if r > 1.1:
        return "landscape"
    if r < 0.91:
        return "portrait"
    return "square"


def _count_text_tokens(text: str) -> int:
    # 粗估但比"字符数/4"准: CJK 字符按 1 token/字, 其余按 ~4 字符/token
    s = str(text or "")
    cjk = sum(
        1 for c in s
        if "一" <= c <= "鿿" or "぀" <= c <= "ヿ" or "가" <= c <= "힣"
    )
    return max(1, cjk + (len(s) - cjk) // 4)


def build_image_usage(
    prompt: str,
    output_resolution: str,
    ratio: str = "1:1",
    input_images: Sequence[tuple[bytes, str]] = (),
) -> dict:
    """按 OpenAI gpt-image-1 口径构造 usage(token 计费用)。
    输出图像 token = 表[质量档(由分辨率映射)][朝向(由比例映射)];
    输入 = 提示词 token(CJK 感知) + 输入图 token(按官方 32px patch 公式逐张精确计算)。
    字段结构对齐真实 gpt-image /v1/images/generations 返回: 仅 input/output
    命名(无 chat 的 prompt/completion 冗余),input/output_tokens_details 各含
    image_tokens 与 text_tokens。下游图像计费取 output_tokens_details.image_tokens。
    """
    quality = _RES_TO_QUALITY.get(str(output_resolution or "2K").upper(), "medium")
    orient = _orientation_of(ratio)
    img_out = _GPT_IMAGE_OUTPUT_TOKENS[quality][orient]

    text_in = _count_text_tokens(prompt)
    img_in = sum(
        _input_image_tokens_from_bytes(image_bytes)
        for image_bytes, _mime in (input_images or ())
    )
    input_tokens = text_in + img_in

    return {
        "input_tokens": input_tokens,
        "output_tokens": img_out,
        "total_tokens": input_tokens + img_out,
        "input_tokens_details": {"image_tokens": img_in, "text_tokens": text_in},
        "output_tokens_details": {"image_tokens": img_out, "text_tokens": 0},
    }


def resolve_model(model_id: Optional[str]) -> dict:
    if not model_id:
        return MODEL_CATALOG[DEFAULT_MODEL_ID]
    if model_id not in MODEL_CATALOG:
        raise HTTPException(status_code=400, detail=f"Invalid model: {model_id}")
    return MODEL_CATALOG[model_id]


def ratio_from_size(size: str) -> str:
    s = str(size or "").strip().lower()
    mapping = {
        "1024x1024": "1:1",
        "1536x1536": "1:1",
        "2048x2048": "1:1",
        "1024x1792": "9:16",
        "1536x2752": "9:16",
        "1792x1024": "16:9",
        "2752x1536": "16:9",
        "2048x1536": "4:3",
        "1536x2048": "3:4",
        "1536x1024": "3:2",  # OpenAI gpt-image-1 横版
        "1024x1536": "2:3",  # OpenAI gpt-image-1 竖版
    }
    if s in mapping:
        return mapping[s]
    # 兜底: 从任意 WxH 计算最接近的受支持比例(容纳官方/自定义尺寸)
    if "x" in s:
        try:
            w, h = (int(v) for v in s.split("x")[:2])
            if w > 0 and h > 0:
                target = w / h
                return min(
                    SUPPORTED_RATIOS,
                    key=lambda r: abs(
                        target - int(r.split(":")[0]) / int(r.split(":")[1])
                    ),
                )
        except (ValueError, ZeroDivisionError):
            pass
    return "1:1"


def resolution_from_size(size: str) -> Optional[str]:
    """从 size 的最大边推分辨率档(仅当未显式传 quality 时用)。"""
    s = str(size or "").strip().lower()
    if "x" not in s:
        return None
    try:
        w, h = (int(v) for v in s.split("x")[:2])
    except (ValueError, TypeError):
        return None
    m = max(w, h)
    if m <= 0:
        return None
    if m <= 1024:
        return "1K"
    if m <= 2048:
        return "2K"
    return "4K"


@dataclass(frozen=True)
class ResolvedAspectRatio:
    aspect_ratio: str
    usage_ratio: str
    output_size: Optional[dict[str, int]]
    fallback_aspect_ratio: Optional[str] = None


@dataclass(frozen=True)
class ResolvedImageGeometry:
    aspect_ratio: str
    usage_ratio: str
    output_resolution: str
    model_id: str
    output_size: Optional[dict[str, int]]
    fallback_aspect_ratio: Optional[str] = None


def nearest_supported_ratio(
    width: int, height: int, candidates: Sequence[str]
) -> str:
    if width <= 0 or height <= 0:
        return "1:1"
    ordered = tuple(candidates)
    if not ordered:
        return "1:1"
    source_log_ratio = math.log(width / height)

    def distance(candidate: str) -> float:
        try:
            candidate_width, candidate_height = (
                int(value) for value in candidate.split(":", 1)
            )
            if candidate_width <= 0 or candidate_height <= 0:
                return math.inf
            return abs(source_log_ratio - math.log(candidate_width / candidate_height))
        except (TypeError, ValueError, ZeroDivisionError):
            return math.inf

    return min(ordered, key=distance)


def _dimensions_from_size(size: object) -> Optional[tuple[int, int]]:
    value = str(size or "").strip().lower()
    if "x" not in value:
        return None
    try:
        width, height = (int(part) for part in value.split("x", 1))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _primary_image_dimensions(
    input_images: Sequence[tuple[bytes, str]],
) -> tuple[int, int]:
    try:
        image_bytes = input_images[0][0]
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                width, height = int(image.width), int(image.height)
                orientation = 1
                raw_exif = image.info.get("exif")
                if isinstance(raw_exif, bytes) and raw_exif:
                    try:
                        exif = Image.Exif()
                        exif.load(raw_exif)
                        orientation = int(exif.get(274, 1) or 1)
                    except (OSError, SyntaxError, TypeError, ValueError):
                        orientation = 1
                image.verify()
        if orientation in {5, 6, 7, 8}:
            width, height = height, width
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        IndexError,
        OSError,
        TypeError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=400, detail="first input image cannot be decoded"
        ) from exc
    if width <= 0 or height <= 0:
        raise HTTPException(
            status_code=400, detail="first input image has invalid dimensions"
        )
    return width, height


def _reduced_ratio(width: int, height: int) -> str:
    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def resolve_requested_aspect_ratio(
    requested_ratio: object,
    *,
    input_images: Sequence[tuple[bytes, str]],
    supported_ratios: Sequence[str],
    supports_auto: bool,
    output_resolution: str,
    size: object = None,
) -> ResolvedAspectRatio:
    normalized = str(requested_ratio or "").strip().lower()
    ordered_ratios = tuple(supported_ratios)
    if normalized not in {"free", "auto"}:
        ratio = normalized if normalized in ordered_ratios else "1:1"
        return ResolvedAspectRatio(ratio, ratio, None)

    dimensions: Optional[tuple[int, int]] = None
    from_primary_image = bool(input_images)
    if from_primary_image:
        dimensions = _primary_image_dimensions(input_images)
    else:
        dimensions = _dimensions_from_size(size)

    if dimensions is None:
        if supports_auto:
            return ResolvedAspectRatio("auto", "1:1", None)
        return ResolvedAspectRatio("1:1", "1:1", None)

    width, height = dimensions
    if supports_auto and from_primary_image:
        fallback_ratio = nearest_supported_ratio(width, height, ordered_ratios)
        return ResolvedAspectRatio(
            "auto",
            _reduced_ratio(width, height),
            size_from_dimensions(width, height, output_resolution),
            fallback_ratio,
        )

    ratio = nearest_supported_ratio(width, height, ordered_ratios)
    return ResolvedAspectRatio(ratio, ratio, None)


def _output_resolution(data: dict, model_id: Optional[str], model_conf: dict) -> str:
    output_resolution = str(model_conf["output_resolution"])
    if not model_id or model_conf.get("dynamic"):
        quality = str(data.get("quality") or "").lower().strip()
        if quality in ("4k", "ultra", "high"):
            output_resolution = "4K"
        elif quality in ("2k", "hd", "medium"):
            output_resolution = "2K"
        elif quality in ("1k", "low", "standard"):
            output_resolution = "1K"
        else:
            output_resolution = resolution_from_size(data.get("size")) or "2K"
    return output_resolution


def resolve_image_geometry(
    data: dict,
    model_id: Optional[str],
    input_images: Sequence[tuple[bytes, str]] = (),
) -> ResolvedImageGeometry:
    requested_ratio = str(
        data.get("aspect_ratio") or data.get("aspectRatio") or ""
    ).strip().lower() or "auto"
    requested_size = data.get("size")
    if not model_id and requested_ratio in {"free", "auto"}:
        resolved_model_id = DEFAULT_AUTO_MODEL_ID
    else:
        resolved_model_id = model_id or DEFAULT_MODEL_ID
    if resolved_model_id not in MODEL_CATALOG:
        resolved_model_id = DEFAULT_MODEL_ID
    model_conf = MODEL_CATALOG[resolved_model_id]
    output_resolution = _output_resolution(data, model_id, model_conf)

    fixed_model_ratio = str(model_conf.get("aspect_ratio") or "").strip()
    if model_id and fixed_model_ratio:
        requested_ratio = fixed_model_ratio

    supported_ratios = tuple(
        model_conf.get("supported_aspect_ratios") or sorted(SUPPORTED_RATIOS)
    )
    supports_auto = bool(model_conf.get("supports_auto_aspect_ratio"))
    if requested_ratio in {"free", "auto"}:
        resolved_ratio = resolve_requested_aspect_ratio(
            requested_ratio,
            input_images=input_images,
            supported_ratios=supported_ratios,
            supports_auto=supports_auto,
            output_resolution=output_resolution,
            size=requested_size,
        )
    elif requested_ratio in supported_ratios:
        resolved_ratio = ResolvedAspectRatio(requested_ratio, requested_ratio, None)
    elif requested_ratio in SUPPORTED_RATIOS and not supports_auto:
        try:
            width, height = (int(value) for value in requested_ratio.split(":", 1))
        except (TypeError, ValueError):
            width, height = 1, 1
        nearest = nearest_supported_ratio(width, height, supported_ratios)
        resolved_ratio = ResolvedAspectRatio(nearest, nearest, None)
    else:
        resolved_ratio = ResolvedAspectRatio("1:1", "1:1", None)

    return ResolvedImageGeometry(
        aspect_ratio=resolved_ratio.aspect_ratio,
        usage_ratio=resolved_ratio.usage_ratio,
        output_resolution=output_resolution,
        model_id=resolved_model_id,
        output_size=resolved_ratio.output_size,
        fallback_aspect_ratio=resolved_ratio.fallback_aspect_ratio,
    )


def resolve_ratio_and_resolution(
    data: dict, model_id: Optional[str]
) -> tuple[str, str, str]:
    geometry = resolve_image_geometry(data, model_id)
    return geometry.aspect_ratio, geometry.output_resolution, geometry.model_id
