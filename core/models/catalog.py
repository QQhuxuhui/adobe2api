from __future__ import annotations

SUPPORTED_RATIOS = {
    "1:1",
    "1:8",
    "1:4",
    "5:4",
    "9:16",
    "21:9",
    "4:1",
    "16:9",
    "4:3",
    "3:2",
    "4:5",
    "3:4",
    "8:1",
    "2:3",
}
GEMINI_PRO_FIXED_RATIOS = (
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
)
GEMINI_FLASH_FIXED_RATIOS = (
    *GEMINI_PRO_FIXED_RATIOS,
    "1:8",
    "1:4",
    "4:1",
    "8:1",
)
RATIO_SUFFIX_MAP = {
    "1:1": "1x1",
    "16:9": "16x9",
    "9:16": "9x16",
    "4:3": "4x3",
    "3:4": "3x4",
}
NANO_BANANA2_RATIO_SUFFIX_MAP = {
    **RATIO_SUFFIX_MAP,
    "1:8": "1x8",
    "1:4": "1x4",
    "4:1": "4x1",
    "8:1": "8x1",
}
GPT_IMAGE_RATIO_SUFFIX_MAP = {
    "1:1": "1x1",
    "5:4": "5x4",
    "9:16": "9x16",
    "21:9": "21x9",
    "16:9": "16x9",
    "3:2": "3x2",
    "4:3": "4x3",
    "4:5": "4x5",
    "3:4": "3x4",
    "2:3": "2x3",
}
GPT_IMAGE_FIXED_RATIOS = tuple(GPT_IMAGE_RATIO_SUFFIX_MAP)

MODEL_CATALOG: dict[str, dict] = {}


def _register_nano_banana_family(
    prefix: str,
    *,
    upstream_model_id: str,
    upstream_model_version: str,
    family_label: str,
    ratio_suffix_map: dict[str, str] = RATIO_SUFFIX_MAP,
    supported_ratios: tuple[str, ...] = GEMINI_PRO_FIXED_RATIOS,
) -> None:
    for res in ("1k", "2k", "4k"):
        for ratio, suffix in ratio_suffix_map.items():
            model_id = f"{prefix}-{res}-{suffix}"
            MODEL_CATALOG[model_id] = {
                "upstream_model": "google:firefly:colligo:nano-banana-pro",
                "upstream_model_id": upstream_model_id,
                "upstream_model_version": upstream_model_version,
                "output_resolution": res.upper(),
                "aspect_ratio": ratio,
                "supports_auto_aspect_ratio": True,
                "supported_aspect_ratios": supported_ratios,
                "description": f"{family_label} ({res.upper()} {ratio})",
            }


def _register_gpt_image_family() -> None:
    for res in ("1k", "2k", "4k"):
        for ratio, suffix in GPT_IMAGE_RATIO_SUFFIX_MAP.items():
            model_id = f"firefly-gpt-image-{res}-{suffix}"
            MODEL_CATALOG[model_id] = {
                "upstream_model": "openai:firefly:gpt-image",
                "upstream_model_id": "gpt-image",
                "upstream_model_version": "2",
                "output_resolution": res.upper(),
                "aspect_ratio": ratio,
                "supports_auto_aspect_ratio": False,
                "supported_aspect_ratios": GPT_IMAGE_FIXED_RATIOS,
                "description": f"Firefly GPT Image ({res.upper()} {ratio})",
            }


_register_nano_banana_family(
    "firefly-nano-banana-pro",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-2",
    family_label="Firefly Nano Banana Pro",
)
_register_nano_banana_family(
    "firefly-nano-banana",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-2",
    family_label="Firefly Nano Banana",
)
_register_nano_banana_family(
    "firefly-nano-banana2",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-3",
    family_label="Firefly Nano Banana 2",
    ratio_suffix_map=NANO_BANANA2_RATIO_SUFFIX_MAP,
    supported_ratios=GEMINI_FLASH_FIXED_RATIOS,
)
_register_gpt_image_family()


def _register_base_model(
    model_id: str,
    *,
    upstream_model: str,
    upstream_model_id: str,
    upstream_model_version: str,
    label: str,
    supports_auto_aspect_ratio: bool,
    supported_aspect_ratios: tuple[str, ...],
) -> None:
    # 基础模型: 不带分辨率/比例后缀。分辨率由请求 quality(1k/2k/4k) 决定,
    # 比例由请求 aspect_ratio 或 size 决定(不写死 aspect_ratio → resolver 用请求值)。
    # 目的: 下游只用一个模型名 + 传参自适应,不必为每个 res×ratio 组合各配一个模型。
    MODEL_CATALOG[model_id] = {
        "upstream_model": upstream_model,
        "upstream_model_id": upstream_model_id,
        "upstream_model_version": upstream_model_version,
        "output_resolution": "2K",  # 默认;quality 参数可覆盖为 1K/4K
        "dynamic": True,
        "supports_auto_aspect_ratio": supports_auto_aspect_ratio,
        "supported_aspect_ratios": supported_aspect_ratios,
        "description": f"{label} (动态分辨率/比例,由请求参数决定)",
    }


_register_base_model(
    "firefly-gpt-image",
    upstream_model="openai:firefly:gpt-image",
    upstream_model_id="gpt-image",
    upstream_model_version="2",
    label="Firefly GPT Image",
    supports_auto_aspect_ratio=False,
    supported_aspect_ratios=GPT_IMAGE_FIXED_RATIOS,
)
_register_base_model(
    "firefly-nano-banana-pro",
    upstream_model="google:firefly:colligo:nano-banana-pro",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-2",
    label="Firefly Nano Banana Pro",
    supports_auto_aspect_ratio=True,
    supported_aspect_ratios=GEMINI_PRO_FIXED_RATIOS,
)
_register_base_model(
    "firefly-nano-banana2",
    upstream_model="google:firefly:colligo:nano-banana-pro",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-3",
    label="Firefly Nano Banana 2",
    supports_auto_aspect_ratio=True,
    supported_aspect_ratios=GEMINI_FLASH_FIXED_RATIOS,
)

# OpenAI 兼容别名: 上游模型名以 "gpt-image-" 开头(sub2api /v1/images/generations
# 要求上游模型是 gpt-image-* 才放行)。行为同 firefly-gpt-image(动态 gpt-image)。
# sub2api 里把 gpt-image-2 映射成 gpt-image-2(恒等)即可同时过校验并被本服务接受。
_register_base_model(
    "gpt-image-1",
    upstream_model="openai:firefly:gpt-image",
    upstream_model_id="gpt-image",
    upstream_model_version="2",
    label="Firefly GPT Image (gpt-image-1 别名)",
    supports_auto_aspect_ratio=False,
    supported_aspect_ratios=GPT_IMAGE_FIXED_RATIOS,
)
_register_base_model(
    "gpt-image-2",
    upstream_model="openai:firefly:gpt-image",
    upstream_model_id="gpt-image",
    upstream_model_version="2",
    label="Firefly GPT Image (gpt-image-2 别名)",
    supports_auto_aspect_ratio=False,
    supported_aspect_ratios=GPT_IMAGE_FIXED_RATIOS,
)

DEFAULT_MODEL_ID = "firefly-nano-banana-pro-2k-16x9"
DEFAULT_AUTO_MODEL_ID = "firefly-nano-banana-pro"

VIDEO_MODEL_CATALOG: dict[str, dict] = {
    "firefly-sora2-4s-9x16": {
        "duration": 4,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (4s 9:16)",
    },
    "firefly-sora2-4s-16x9": {
        "duration": 4,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (4s 16:9)",
    },
    "firefly-sora2-8s-9x16": {
        "duration": 8,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (8s 9:16)",
    },
    "firefly-sora2-8s-16x9": {
        "duration": 8,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (8s 16:9)",
    },
    "firefly-sora2-12s-9x16": {
        "duration": 12,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (12s 9:16)",
    },
    "firefly-sora2-12s-16x9": {
        "duration": 12,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (12s 16:9)",
    },
}

for dur in (4, 8, 12):
    for ratio in ("9:16", "16:9"):
        model_id = f"firefly-sora2-pro-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "duration": dur,
            "aspect_ratio": ratio,
            "upstream_model": "openai:firefly:colligo:sora2-pro",
            "description": f"Firefly Sora2 Pro video model ({dur}s {ratio})",
        }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"firefly-veo31-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "engine": "veo31-standard",
                "upstream_model": "google:firefly:colligo:veo31",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "description": f"Firefly Veo31 video model ({dur}s {ratio} {res})",
            }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"firefly-veo31-ref-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "engine": "veo31-standard",
                "upstream_model": "google:firefly:colligo:veo31",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "reference_mode": "image",
                "description": f"Firefly Veo31 Ref video model ({dur}s {ratio} {res})",
            }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"firefly-veo31-fast-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "engine": "veo31-fast",
                "upstream_model": "google:firefly:colligo:veo31-fast",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "description": f"Firefly Veo31 Fast video model ({dur}s {ratio} {res})",
            }

for dur in (5, 15):
    for ratio in ("16:9", "9:16"):
        model_id = f"firefly-kling-o3-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "engine": "kling-o3",
            "upstream_model": "kling:firefly:colligo:o3",
            "duration": dur,
            "aspect_ratio": ratio,
            "resolution": "1080p",
            "description": f"Firefly Kling O3 video model ({dur}s {ratio})",
        }

for dur in (5, 10, 15):
    for ratio in ("16:9", "9:16"):
        model_id = f"firefly-kling3-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "engine": "kling3",
            "upstream_model": "kling:firefly:colligo:3.0",
            "duration": dur,
            "aspect_ratio": ratio,
            "resolution": "720p",
            "generate_audio": True,
            "description": f"Firefly Kling 3.0 video model ({dur}s {ratio} 720p)",
        }

# Public model aliases used by new-api.  The concrete Adobe suffix is selected
# from request parameters by core.models.video_resolver rather than being a
# separate model entry for every duration/ratio/resolution combination.
VIDEO_MODEL_CATALOG.update(
    {
        "sora-2": {
            "dynamic": True,
            "engine": "sora2",
            "upstream_model": "openai:firefly:colligo:sora2",
            "default_duration": 4,
            "default_size": "720x1280",
            "description": "Sora 2 video (parameters select Adobe output)",
        },
        "sora-2-pro": {
            "dynamic": True,
            "engine": "sora2-pro",
            "upstream_model": "openai:firefly:colligo:sora2-pro",
            "default_duration": 4,
            "default_size": "720x1280",
            "description": "Sora 2 Pro video (parameters select Adobe output)",
        },
        "veo-3.1-generate-preview": {
            "dynamic": True,
            "engine": "veo31-standard",
            "upstream_model": "google:firefly:colligo:veo31",
            "default_duration": 8,
            "default_aspect_ratio": "16:9",
            "default_resolution": "720p",
            "description": "Veo 3.1 video (parameters select Adobe output)",
        },
        "veo-3.1-fast-generate-preview": {
            "dynamic": True,
            "engine": "veo31-fast",
            "upstream_model": "google:firefly:colligo:veo31-fast",
            "default_duration": 8,
            "default_aspect_ratio": "16:9",
            "default_resolution": "720p",
            "description": "Veo 3.1 Fast video (parameters select Adobe output)",
        },
    }
)
