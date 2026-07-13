from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from .catalog import DEFAULT_MODEL_ID, MODEL_CATALOG, SUPPORTED_RATIOS


# 图像输出 token 数(按分辨率),用于在响应 usage 里上报,好让下游网关(new-api/sub2api)
# 按 token 计费。量级对标 OpenAI gpt-image-1 的输出图像 token。可按成本/利润调整。
IMAGE_OUTPUT_TOKENS = {"1K": 1000, "2K": 2000, "4K": 4200}


def build_image_usage(prompt: str, output_resolution: str) -> dict:
    """构造图像生成的 usage(token 计费用)。
    prompt_tokens 按提示词粗估(~4 字符/token),completion_tokens 按分辨率给图像输出 token。
    """
    it = max(1, len(str(prompt or "")) // 4)
    ot = IMAGE_OUTPUT_TOKENS.get(str(output_resolution or "2K").upper(), 2000)
    # 同时给出两套命名: chat(prompt/completion) 与 responses/images(input/output),
    # 并把图像输出 token 放进 output_tokens_details.image_tokens——
    # 下游网关(sub2api/new-api)图像计费正是从这个字段取 image_output_tokens。
    return {
        "prompt_tokens": it,
        "completion_tokens": ot,
        "total_tokens": it + ot,
        "input_tokens": it,
        "output_tokens": ot,
        "output_tokens_details": {"image_tokens": ot},
        "completion_tokens_details": {"image_tokens": ot},
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
