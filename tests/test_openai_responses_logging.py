import json

import app


def test_responses_path_maps_to_responses_create():
    assert app._resolve_request_operation("POST", "/v1/responses") == "responses.create"


def test_logging_extracts_responses_string_input_without_base64_output():
    fields = app._extract_logging_fields(
        json.dumps(
            {
                "model": "gpt-image-2",
                "input": "draw a blue square",
                "result": "A" * 10000,
            }
        ).encode()
    )
    assert fields == {
        "model": "gpt-image-2",
        "prompt_preview": "draw a blue square",
    }


def test_logging_extracts_last_user_input_text_only():
    fields = app._extract_logging_fields(
        json.dumps(
            {
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "edit this image"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64," + "A" * 10000,
                            },
                        ],
                    }
                ],
            }
        ).encode()
    )
    assert fields == {
        "model": "gpt-5.4-mini",
        "prompt_preview": "edit this image",
    }
