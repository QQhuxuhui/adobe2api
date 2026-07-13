from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from .catalog import DEFAULT_MODEL_ID, MODEL_CATALOG, SUPPORTED_RATIOS


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
# 每张输入图(图生图/改图)的估算 token(gpt-image 输入图约此量级)
INPUT_IMAGE_TOKENS = 300


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
    input_images: int = 0,
) -> dict:
    """按 OpenAI gpt-image-1 口径构造 usage(token 计费用)。
    输出图像 token = 表[质量档(由分辨率映射)][朝向(由比例映射)];
    输入 = 提示词 token(CJK 感知) + 输入图 token(图生图/改图)。
    同时给出 chat(prompt/completion) 与 responses/images(input/output) 两套命名,
    图像输出 token 放进 output_tokens_details.image_tokens(下游图像计费取此字段)。
    """
    quality = _RES_TO_QUALITY.get(str(output_resolution or "2K").upper(), "medium")
    orient = _orientation_of(ratio)
    img_out = _GPT_IMAGE_OUTPUT_TOKENS[quality][orient]

    text_in = _count_text_tokens(prompt)
    img_in = max(0, int(input_images or 0)) * INPUT_IMAGE_TOKENS
    input_tokens = text_in + img_in

    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": img_out,
        "total_tokens": input_tokens + img_out,
        "input_tokens": input_tokens,
        "output_tokens": img_out,
        "input_tokens_details": {"text_tokens": text_in, "image_tokens": img_in},
        "output_tokens_details": {"image_tokens": img_out},
        "completion_tokens_details": {"image_tokens": img_out},
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


def resolve_ratio_and_resolution(
    data: dict, model_id: Optional[str]
) -> tuple[str, str, str]:
    ratio = str(data.get("aspect_ratio") or "").strip() or ratio_from_size(
        data.get("size", "1024x1024")
    )
    if ratio not in SUPPORTED_RATIOS:
        ratio = "1:1"

    resolved_model_id = model_id or DEFAULT_MODEL_ID
    if resolved_model_id not in MODEL_CATALOG:
        resolved_model_id = DEFAULT_MODEL_ID
    model_conf = MODEL_CATALOG[resolved_model_id]

    output_resolution = model_conf["output_resolution"]
    # 基础模型(dynamic)或未指定模型时: 分辨率由请求参数决定(下游传参自适应)。
    # quality 兼容 OpenAI 官方取值(gpt-image-1: low/medium/high; dall-e-3: standard/hd)
    # 与简写(1k/2k/4k/ultra); 未传时从 size 推,再兜底 2K。
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

    model_ratio = model_conf.get("aspect_ratio")
    if model_ratio:
        ratio = model_ratio

    return ratio, output_resolution, resolved_model_id
