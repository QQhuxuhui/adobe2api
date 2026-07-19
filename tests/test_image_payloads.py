from core.models.payloads import build_image_payload_candidates


def build_payloads(**overrides):
    arguments = {
        "prompt": "draw",
        "aspect_ratio": "1:1",
        "output_resolution": "2K",
        "upstream_model_id": "gemini-flash",
        "upstream_model_version": "nano-banana-2",
    }
    arguments.update(overrides)
    return build_image_payload_candidates(**arguments)


def test_auto_payload_uses_size_override_without_aspect_ratio():
    candidates = build_payloads(
        aspect_ratio="auto",
        output_size={"width": 1744, "height": 2400},
        fallback_aspect_ratio="3:4",
        source_image_ids=["image-1", "image-2"],
    )
    payload = candidates[0]

    assert len(candidates) == 2
    assert payload["size"] == {"width": 1744, "height": 2400}
    assert "aspectRatio" not in payload["modelSpecificPayload"]
    assert payload["referenceBlobs"] == [
        {"id": "image-1", "usage": "general"},
        {"id": "image-2", "usage": "general"},
    ]
    assert candidates[1]["size"] == {"width": 1536, "height": 2048}
    assert candidates[1]["modelSpecificPayload"]["aspectRatio"] == "3:4"


def test_auto_without_image_or_size_attempts_auto_then_square_fallback():
    candidates = build_payloads(aspect_ratio="auto")

    assert len(candidates) == 2
    assert "size" not in candidates[0]
    assert "aspectRatio" not in candidates[0]["modelSpecificPayload"]
    assert candidates[1]["size"] == {"width": 2048, "height": 2048}
    assert candidates[1]["modelSpecificPayload"]["aspectRatio"] == "1:1"


def test_explicit_ratio_keeps_existing_size_and_aspect_ratio_payload():
    payload = build_payloads(aspect_ratio="3:4")[0]

    assert payload["size"] == {"width": 1536, "height": 2048}
    assert payload["modelSpecificPayload"]["aspectRatio"] == "3:4"
