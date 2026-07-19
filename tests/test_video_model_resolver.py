import pytest

from core.models.video_resolver import (
    VideoModelRequestError,
    resolve_video_model,
)


def test_sora_alias_defaults_to_new_api_portrait_size():
    resolved = resolve_video_model("sora-2", {})
    assert (resolved.duration, resolved.aspect_ratio, resolved.resolution) == (
        4,
        "9:16",
        "720p",
    )
    assert resolved.requested_size == "720x1280"
    assert resolved.credit_model_id == "firefly-sora2-4s-9x16"


def test_sora_pro_maps_high_size_to_1080p():
    resolved = resolve_video_model(
        "sora-2-pro", {"seconds": "12", "size": "1792x1024"}
    )
    assert (resolved.duration, resolved.aspect_ratio, resolved.resolution) == (
        12,
        "16:9",
        "1080p",
    )
    assert resolved.credit_model_id == "firefly-sora2-pro-12s-16x9"


def test_veo_fast_alias_maps_duration_ratio_and_resolution():
    resolved = resolve_video_model(
        "veo-3.1-fast-generate-preview",
        {"durationSeconds": 8, "aspectRatio": "9:16", "resolution": "1080p"},
    )
    assert resolved.engine == "veo31-fast"
    assert resolved.credit_model_id == "firefly-veo31-fast-8s-9x16-1080p"


def test_veo_rejects_1080p_when_duration_is_not_eight():
    with pytest.raises(VideoModelRequestError, match="1080p"):
        resolve_video_model(
            "veo-3.1-generate-preview",
            {"duration": 6, "resolution": "1080p"},
        )


def test_legacy_suffix_model_keeps_fixed_configuration():
    resolved = resolve_video_model("firefly-veo31-6s-16x9-720p", {})
    assert resolved.public_model_id == "firefly-veo31-6s-16x9-720p"
    assert resolved.credit_model_id == "firefly-veo31-6s-16x9-720p"
    assert (resolved.duration, resolved.aspect_ratio, resolved.resolution) == (
        6,
        "16:9",
        "720p",
    )


@pytest.mark.parametrize("field", ["aspectRatio", "resolution"])
@pytest.mark.parametrize("value", [False, 0, []])
def test_veo_rejects_falsy_explicit_parameter(field, value):
    with pytest.raises(VideoModelRequestError):
        resolve_video_model(
            "veo-3.1-generate-preview",
            {field: value},
        )
