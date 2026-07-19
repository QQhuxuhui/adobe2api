import base64
import io
import json

import pytest
from PIL import Image

from api.openai_responses import (
    ResponsesRequestError,
    build_responses_image_response,
    encode_image_result,
    iter_responses_image_sse,
    parse_responses_image_request,
)


IMAGE_MODELS = {"gpt-image-1", "gpt-image-2", "firefly-gpt-image"}


def test_parse_image_only_model_with_string_input():
    parsed = parse_responses_image_request(
        {
            "model": "gpt-image-2",
            "input": "draw a blue square",
            "size": "1024x1024",
            "quality": "low",
        },
        IMAGE_MODELS,
    )
    assert parsed.inbound_model == "gpt-image-2"
    assert parsed.image_model == "gpt-image-2"
    assert parsed.prompt == "draw a blue square"
    assert parsed.size == "1024x1024"
    assert parsed.quality == "low"
    assert parsed.stream is False


def test_parse_official_image_tool_defaults_backend_model_and_tool_fields_win():
    parsed = parse_responses_image_request(
        {
            "model": "gpt-5.4-mini",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "edit this"},
                        {"type": "input_image", "image_url": "data:image/png;base64,YQ=="},
                    ],
                }
            ],
            "size": "1024x1024",
            "aspect_ratio": "16:9",
            "quality": "low",
            "tools": [
                {
                    "type": "image_generation",
                    "size": "1536x1024",
                    "aspect_ratio": "free",
                    "quality": "high",
                    "output_format": "webp",
                    "output_compression": 73,
                    "action": "edit",
                }
            ],
            "tool_choice": "required",
            "stream": True,
        },
        IMAGE_MODELS,
    )
    assert parsed.inbound_model == "gpt-5.4-mini"
    assert parsed.image_model == "gpt-image-2"
    assert parsed.prompt == "edit this"
    assert parsed.input_image_urls == ("data:image/png;base64,YQ==",)
    assert parsed.size == "1536x1024"
    assert parsed.aspect_ratio == "free"
    assert parsed.quality == "high"
    assert parsed.output_format == "webp"
    assert parsed.output_compression == 73
    assert parsed.action == "edit"
    assert parsed.stream is True


def test_parse_input_image_accepts_object_url_compatibility_shape():
    parsed = parse_responses_image_request(
        {
            "model": "gpt-image-2",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "edit this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/source.png"},
                        },
                    ],
                }
            ],
        },
        IMAGE_MODELS,
    )
    assert parsed.input_image_urls == ("https://example.com/source.png",)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"model": "gpt-image-2", "input": ""}, "input is required"),
        ({"model": "gpt-5.4-mini", "input": "draw"}, "image_generation tool is required"),
        ({"model": "gpt-image-2", "input": "draw", "tool_choice": "none"}, "tool_choice"),
        ({"model": "gpt-image-2", "input": "draw", "background": "transparent"}, "transparent"),
        ({"model": "gpt-image-2", "input": "draw", "partial_images": 1}, "partial_images"),
        (
            {
                "model": "gpt-image-2",
                "input": [{"role": "user", "content": [{"type": "input_image", "file_id": "file_1"}]}],
                "prompt": "edit",
            },
            "file_id",
        ),
        (
            {
                "model": "gpt-image-2",
                "input": "draw",
                "tools": [{"type": "image_generation", "input_fidelity": "high"}],
            },
            "input_fidelity",
        ),
    ],
)
def test_parse_rejects_unsupported_requests(payload, message):
    with pytest.raises(ResponsesRequestError, match=message):
        parse_responses_image_request(payload, IMAGE_MODELS)


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp"])
def test_encode_image_result_returns_decodable_requested_format(output_format):
    result = encode_image_result(_png_bytes(), output_format, 80)
    with Image.open(io.BytesIO(base64.b64decode(result))) as decoded:
        assert decoded.format.lower() == output_format


def test_build_response_and_sse_use_image_generation_call():
    response = build_responses_image_response(
        response_id="resp_test",
        item_id="ig_test",
        created_at=123,
        model="gpt-image-2",
        result_b64="aW1hZ2U=",
        usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    assert response["output"] == [
        {
            "id": "ig_test",
            "type": "image_generation_call",
            "status": "completed",
            "result": "aW1hZ2U=",
        }
    ]
    chunks = list(iter_responses_image_sse(response))
    events = [chunk.split("\n", 1)[0] for chunk in chunks[:-1]]
    assert events == [
        "event: response.created",
        "event: response.output_item.added",
        "event: response.output_item.done",
        "event: response.completed",
    ]
    assert chunks[-1] == "data: [DONE]\n\n"
    completed = json.loads(chunks[-2].split("data: ", 1)[1])
    assert completed["response"]["output"][0]["type"] == "image_generation_call"
