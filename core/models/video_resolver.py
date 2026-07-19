from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .catalog import RATIO_SUFFIX_MAP, VIDEO_MODEL_CATALOG


class VideoModelRequestError(ValueError):
    """A request parameter cannot be represented by the selected video model."""

    def __init__(self, message: str, code: str = "invalid_video_parameter") -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class ResolvedVideoModel:
    public_model_id: str
    catalog_model_id: str
    engine: str
    upstream_model: str
    duration: int
    aspect_ratio: str
    resolution: str
    requested_size: str
    credit_model_id: str
    reference_mode: str = "frame"
    generate_audio: bool = True
    negative_prompt: str = ""


_SORA_DURATIONS = frozenset({4, 8, 12})
_VEO_DURATIONS = frozenset({4, 6, 8})
_SORA_SIZES = {
    "1280x720": ("16:9", "720p"),
    "720x1280": ("9:16", "720p"),
}
_SORA_PRO_SIZES = {
    **_SORA_SIZES,
    "1792x1024": ("16:9", "1080p"),
    "1024x1792": ("9:16", "1080p"),
}
_RATIO_SIZE = {
    "16:9": "1280x720",
    "9:16": "720x1280",
}


def _value(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None and data[key] != "":
            return data[key]
    return default


def _duration(value: Any, allowed: frozenset[int], default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise VideoModelRequestError("duration must be an integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VideoModelRequestError("duration must be an integer") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise VideoModelRequestError("duration must be an integer")
    parsed = int(numeric)
    if parsed not in allowed:
        raise VideoModelRequestError(
            f"duration must be one of {', '.join(map(str, sorted(allowed)))}"
        )
    return parsed


def _ratio(value: Any, default: str) -> str:
    if value is None or value == "":
        ratio = default
    elif not isinstance(value, str):
        raise VideoModelRequestError("aspect ratio must be 16:9 or 9:16")
    else:
        ratio = value.strip()
    if ratio not in {"16:9", "9:16"}:
        raise VideoModelRequestError("aspect ratio must be 16:9 or 9:16")
    return ratio


def _resolution(value: Any, default: str) -> str:
    if value is None or value == "":
        resolution = default
    elif not isinstance(value, str):
        raise VideoModelRequestError("resolution must be 720p or 1080p")
    else:
        resolution = value.strip().lower()
    if resolution not in {"720p", "1080p"}:
        raise VideoModelRequestError("resolution must be 720p or 1080p")
    return resolution


def _requested_size_for(ratio: str, resolution: str) -> str:
    if resolution == "1080p":
        return "1920x1080" if ratio == "16:9" else "1080x1920"
    return _RATIO_SIZE[ratio]


def _resolve_sora(model_id: str, conf: Mapping[str, Any], data: Mapping[str, Any]) -> ResolvedVideoModel:
    is_pro = model_id == "sora-2-pro"
    size = str(_value(data, "size", default=conf.get("default_size", "720x1280"))).strip()
    sizes = _SORA_PRO_SIZES if is_pro else _SORA_SIZES
    if size not in sizes:
        raise VideoModelRequestError(f"Invalid size for {model_id}")
    ratio, resolution = sizes[size]
    duration = _duration(
        _value(data, "seconds", "duration", "durationSeconds"),
        _SORA_DURATIONS,
        int(conf.get("default_duration", 4)),
    )
    family = "sora2-pro" if is_pro else "sora2"
    return ResolvedVideoModel(
        public_model_id=model_id,
        catalog_model_id=model_id,
        engine=str(conf.get("engine") or family),
        upstream_model=str(conf.get("upstream_model") or ""),
        duration=duration,
        aspect_ratio=ratio,
        resolution=resolution,
        requested_size=size,
        credit_model_id=f"firefly-{family}-{duration}s-{RATIO_SUFFIX_MAP[ratio]}",
    )


def _resolve_veo(model_id: str, conf: Mapping[str, Any], data: Mapping[str, Any]) -> ResolvedVideoModel:
    duration = _duration(
        _value(data, "durationSeconds", "duration_seconds", "duration"),
        _VEO_DURATIONS,
        int(conf.get("default_duration", 8)),
    )
    ratio = _ratio(
        _value(data, "aspectRatio", "aspect_ratio"),
        str(conf.get("default_aspect_ratio") or "16:9"),
    )
    resolution = _resolution(
        _value(data, "resolution"),
        str(conf.get("default_resolution") or "720p"),
    )
    if resolution == "1080p" and duration != 8:
        raise VideoModelRequestError("1080p resolution requires duration 8")
    family = "veo31-fast" if str(conf.get("engine")) == "veo31-fast" else "veo31"
    return ResolvedVideoModel(
        public_model_id=model_id,
        catalog_model_id=model_id,
        engine=str(conf.get("engine") or "veo31-standard"),
        upstream_model=str(conf.get("upstream_model") or ""),
        duration=duration,
        aspect_ratio=ratio,
        resolution=resolution,
        requested_size=_requested_size_for(ratio, resolution),
        credit_model_id=f"firefly-{family}-{duration}s-{RATIO_SUFFIX_MAP[ratio]}-{resolution}",
    )


def _resolve_legacy(model_id: str, conf: Mapping[str, Any]) -> ResolvedVideoModel:
    configured_engine = str(conf.get("engine") or "").strip()
    if configured_engine:
        engine = configured_engine
    elif model_id.startswith("firefly-sora2-pro-"):
        engine = "sora2-pro"
    elif model_id.startswith("firefly-sora2-"):
        engine = "sora2"
    else:
        engine = "sora2"
    ratio = str(conf.get("aspect_ratio") or "16:9")
    resolution = str(conf.get("resolution") or "720p").lower()
    duration = int(conf.get("duration") or 4)
    upstream = str(conf.get("upstream_model") or "")
    return ResolvedVideoModel(
        public_model_id=model_id,
        catalog_model_id=model_id,
        engine=engine,
        upstream_model=upstream,
        duration=duration,
        aspect_ratio=ratio,
        resolution=resolution,
        requested_size=_requested_size_for(ratio, resolution),
        credit_model_id=model_id,
        reference_mode=str(conf.get("reference_mode") or "frame"),
        generate_audio=bool(conf.get("generate_audio", True)),
    )


def resolve_video_model(model_id: str, data: Mapping[str, Any] | None = None) -> ResolvedVideoModel:
    public_model_id = str(model_id or "").strip()
    conf = VIDEO_MODEL_CATALOG.get(public_model_id)
    if not isinstance(conf, Mapping):
        raise VideoModelRequestError(f"Invalid video model: {public_model_id}", "invalid_model")
    request_data = data if isinstance(data, Mapping) else {}
    if public_model_id in {"sora-2", "sora-2-pro"}:
        resolved = _resolve_sora(public_model_id, conf, request_data)
    elif public_model_id in {"veo-3.1-generate-preview", "veo-3.1-fast-generate-preview"}:
        resolved = _resolve_veo(public_model_id, conf, request_data)
    else:
        resolved = _resolve_legacy(public_model_id, conf)
    generate_audio = _value(request_data, "generate_audio", "generateAudio", default=resolved.generate_audio)
    if isinstance(generate_audio, str):
        normalized = generate_audio.strip().lower()
        if normalized in {"true", "1"}:
            generate_audio = True
        elif normalized in {"false", "0"}:
            generate_audio = False
        else:
            raise VideoModelRequestError("generate_audio must be boolean")
    elif not isinstance(generate_audio, bool):
        raise VideoModelRequestError("generate_audio must be boolean")
    reference_mode = str(
        _value(request_data, "video_reference_mode", "videoReferenceMode", "reference_mode", "referenceMode", default=resolved.reference_mode)
    ).strip().lower()
    if reference_mode not in {"frame", "image"}:
        raise VideoModelRequestError("reference mode must be frame or image")
    negative_prompt = str(_value(request_data, "negative_prompt", "negativePrompt", default="") or "").strip()
    return ResolvedVideoModel(
        **{
            **resolved.__dict__,
            "generate_audio": generate_audio,
            "reference_mode": resolved.reference_mode if "reference_mode" not in request_data and "video_reference_mode" not in request_data and "videoReferenceMode" not in request_data and "referenceMode" not in request_data else reference_mode,
            "negative_prompt": negative_prompt,
        }
    )
