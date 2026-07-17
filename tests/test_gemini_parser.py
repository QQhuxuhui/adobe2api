import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.routes.gemini_native as gemini_native
from api.routes.gemini_native import (
    FLASH_RATIOS,
    GEMINI_MAX_ENCODED_IMAGE_CHARS,
    GEMINI_MAX_IMAGE_BYTES,
    GEMINI_MAX_IMAGES,
    GEMINI_MAX_TOTAL_IMAGE_BYTES,
    GEMINI_MODELS,
    GEMINI_NATIVE_MAX_BODY_BYTES,
    PRO_RATIOS,
    TEST_TEXT_MODELS,
    GeminiNativeError,
    decode_inline_image,
    parse_gemini_request,
    read_limited_body,
    resolve_model_action,
)


def _request_for_chunks(
    chunks: list[bytes], content_length: int | None = None
) -> tuple[Request, list[int]]:
    pending = list(chunks)
    receive_calls: list[int] = []

    async def receive():
        receive_calls.append(1)
        body = pending.pop(0) if pending else b""
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(pending),
        }

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1beta/models/test:generateContent",
            "headers": headers,
            "query_string": b"",
        },
        receive,
    )
    return request, receive_calls


def _read_chunks(chunks: list[bytes], content_length: int | None = None) -> bytes:
    request, _ = _request_for_chunks(chunks, content_length)
    return asyncio.run(read_limited_body(request))


def _body(
    parts: list[dict] | None = None,
    *,
    contents: object | None = None,
    system_instruction: object | None = None,
    generation_config: object | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "contents": contents
        if contents is not None
        else [{"parts": parts if parts is not None else [{"text": "draw"}]}]
    }
    if system_instruction is not None:
        payload["systemInstruction"] = system_instruction
    if generation_config is not None:
        payload["generationConfig"] = generation_config
    return json.dumps(payload).encode("utf-8")


def _inline(data: bytes = b"image", mime_type: object = "image/png") -> dict:
    return {
        "inlineData": {
            "mimeType": mime_type,
            "data": base64.b64encode(data).decode("ascii"),
        }
    }


def _assert_invalid(callable_obj, message: str | None = None) -> GeminiNativeError:
    with pytest.raises(GeminiNativeError) as exc_info:
        callable_obj()
    error = exc_info.value
    assert error.code == 400
    assert error.status == "INVALID_ARGUMENT"
    if message is not None:
        assert error.message == message
    return error


def _pro_spec():
    return GEMINI_MODELS["gemini-3-pro-image"]


def _flash_spec():
    return GEMINI_MODELS["gemini-3.1-flash-image"]


def test_model_registry_contains_exact_supported_aliases():
    expected_image_models = {
        "gemini-3-pro-image",
        "gemini-3-pro-image-preview",
        "gemini-3.1-flash-image",
        "gemini-3.1-flash-image-preview",
    }
    assert set(GEMINI_MODELS) == expected_image_models | set(TEST_TEXT_MODELS)
    for model_id in ("gemini-3-pro-image", "gemini-3-pro-image-preview"):
        spec = GEMINI_MODELS[model_id]
        assert spec.model_id == model_id
        assert spec.family == "pro"
        assert spec.upstream_model_id == "gemini-flash"
        assert spec.upstream_model_version == "nano-banana-2"
        assert spec.aspect_ratios == PRO_RATIOS
    for model_id in (
        "gemini-3.1-flash-image",
        "gemini-3.1-flash-image-preview",
    ):
        spec = GEMINI_MODELS[model_id]
        assert spec.model_id == model_id
        assert spec.family == "flash"
        assert spec.upstream_model_id == "gemini-flash"
        assert spec.upstream_model_version == "nano-banana-3"
        assert spec.aspect_ratios == FLASH_RATIOS
    for model_id in TEST_TEXT_MODELS:
        spec = GEMINI_MODELS[model_id]
        assert spec.family == "text"
        assert spec.upstream_model_id is None
        assert spec.upstream_model_version is None
        assert spec.aspect_ratios == frozenset()


@pytest.mark.parametrize(
    "model_id",
    [
        "gemini-3-pro-image",
        "gemini-3-pro-image-preview",
        "gemini-3.1-flash-image",
        "gemini-3.1-flash-image-preview",
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-3-pro-preview",
        "gemini-3.1-pro-preview",
    ],
)
@pytest.mark.parametrize(
    "action", ["generateContent", "streamGenerateContent", "countTokens"]
)
def test_resolve_model_action_accepts_supported_models_and_actions(model_id, action):
    spec, resolved_action = resolve_model_action(f"{model_id}:{action}")
    assert spec is GEMINI_MODELS[model_id]
    assert resolved_action == action


@pytest.mark.parametrize(
    "model_action",
    [
        "unknown-model:generateContent",
        "gemini-3-pro-image:unknownAction",
        "gemini-3-pro-image",
        ":generateContent",
        "gemini-3-pro-image:",
    ],
)
def test_resolve_model_action_rejects_unknown_model_or_action(model_action):
    with pytest.raises(GeminiNativeError) as exc_info:
        resolve_model_action(model_action)
    assert exc_info.value.code == 404
    assert exc_info.value.status == "NOT_FOUND"


def test_read_limited_body_uses_exact_64_mib_limit_and_caches_body():
    assert GEMINI_NATIVE_MAX_BODY_BYTES == 64 * 1024 * 1024
    request, calls = _request_for_chunks([b'{"contents":', b"[]}"])
    body = asyncio.run(read_limited_body(request))
    assert body == b'{"contents":[]}'
    assert request._body == body
    assert len(calls) == 2


def test_read_limited_body_rejects_large_content_length_before_receive():
    request, calls = _request_for_chunks(
        [b"not consumed"], GEMINI_NATIVE_MAX_BODY_BYTES + 1
    )
    _assert_invalid(
        lambda: asyncio.run(read_limited_body(request)), "Request body is too large"
    )
    assert calls == []


@pytest.mark.parametrize("content_length", [None, 4])
def test_read_limited_body_rejects_stream_crossing_limit(
    monkeypatch, content_length
):
    monkeypatch.setattr(gemini_native, "GEMINI_NATIVE_MAX_BODY_BYTES", 5)
    request, calls = _request_for_chunks([b"abc", b"def"], content_length)
    _assert_invalid(
        lambda: asyncio.run(read_limited_body(request)), "Request body is too large"
    )
    assert len(calls) == 2


@pytest.mark.parametrize("raw_body", [b"[]", b"null", b'"text"', b"42"])
def test_parse_rejects_non_object_top_level(raw_body):
    _assert_invalid(lambda: parse_gemini_request(raw_body, _pro_spec()))


@pytest.mark.parametrize("raw_body", [b"{", b'\xff{"contents":[]}'])
def test_parse_rejects_malformed_json_and_invalid_utf8(raw_body):
    _assert_invalid(
        lambda: parse_gemini_request(raw_body, _pro_spec()),
        "Invalid JSON request body",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"contents": []},
        {"contents": {}},
        {"contents": "hello"},
        {"contents": None},
    ],
)
def test_parse_requires_non_empty_contents_array(payload):
    raw_body = json.dumps(payload).encode("utf-8")
    _assert_invalid(lambda: parse_gemini_request(raw_body, _pro_spec()))


@pytest.mark.parametrize(
    "contents",
    [
        ["turn"],
        [{"parts": {}}],
        [{"parts": "text"}],
        [{"parts": ["part"]}],
        [{}],
    ],
)
def test_parse_rejects_invalid_content_and_part_shapes(contents):
    _assert_invalid(
        lambda: parse_gemini_request(_body(contents=contents), _pro_spec())
    )


@pytest.mark.parametrize(
    "system_instruction",
    ["instruction", [], {}, {"parts": {}}, {"parts": ["part"]}],
)
def test_parse_rejects_invalid_system_instruction_shapes(system_instruction):
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(system_instruction=system_instruction), _pro_spec()
        )
    )


@pytest.mark.parametrize(
    "generation_config",
    ["config", [], {"imageConfig": "image"}, {"imageConfig": []}],
)
def test_parse_rejects_invalid_generation_config_shapes(generation_config):
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(generation_config=generation_config), _pro_spec()
        )
    )


@pytest.mark.parametrize("text", [None, 1, True, [], {}])
def test_parse_rejects_non_string_text(text):
    _assert_invalid(
        lambda: parse_gemini_request(_body(parts=[{"text": text}]), _pro_spec())
    )


@pytest.mark.parametrize(
    "inline_data",
    [
        None,
        "image",
        [],
        {},
        {"data": "", "mimeType": "image/png"},
        {"data": 1, "mimeType": "image/png"},
        {"data": "aW1hZ2U=", "mimeType": ""},
        {"data": "aW1hZ2U=", "mimeType": 1},
    ],
)
def test_parse_rejects_invalid_inline_data_shapes(inline_data):
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(parts=[{"inlineData": inline_data}]), _pro_spec()
        )
    )


def test_parse_ignores_unknown_but_structurally_valid_parts():
    parsed = parse_gemini_request(
        _body(parts=[{"fileData": {"fileUri": "https://example.test/a.png"}}, {"text": "draw"}]),
        _pro_spec(),
    )
    assert parsed.prompt == "draw"
    assert parsed.images == ()


def test_parse_flattens_system_and_all_content_text_in_order():
    raw_body = _body(
        contents=[
            {"role": "user", "parts": [{"text": "first"}, {"text": ""}]},
            {"role": "model", "parts": [{"text": "second"}]},
            {"role": "user", "parts": [{"text": "third"}]},
        ],
        system_instruction={"parts": [{"text": "system one"}, {"text": "system two"}]},
    )
    parsed = parse_gemini_request(raw_body, _pro_spec())
    assert parsed.prompt == "system one\nsystem two\nfirst\nsecond\nthird"


def test_parse_accepts_empty_text_when_an_image_exists():
    parsed = parse_gemini_request(
        _body(parts=[{"text": ""}, _inline(b"image")]), _pro_spec()
    )
    assert parsed.prompt == ""
    assert parsed.images == ((b"image", "image/png"),)


@pytest.mark.parametrize("parts", [[], [{"text": ""}], [{"fileData": {}}]])
def test_parse_rejects_empty_prompt_without_images(parts):
    _assert_invalid(lambda: parse_gemini_request(_body(parts=parts), _pro_spec()))


def test_parse_rejects_whitespace_only_prompt_without_images():
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(parts=[{"text": " \n\t "}]), _pro_spec()
        )
    )


@pytest.mark.parametrize("field_name", ["inlineData", "inline_data"])
def test_parse_accepts_camel_and_snake_case_inline_data(field_name):
    inline = _inline(b"image/jpeg", "image/jpeg")["inlineData"]
    parsed = parse_gemini_request(
        _body(parts=[{"text": "draw"}, {field_name: inline}]), _pro_spec()
    )
    assert parsed.images == ((b"image/jpeg", "image/jpeg"),)


def test_parse_decodes_only_first_six_structurally_valid_images(monkeypatch):
    decoded_inputs: list[str] = []

    def fake_decode(data: str, mime_type: str):
        decoded_inputs.append(data)
        return data.encode("ascii"), mime_type

    monkeypatch.setattr(gemini_native, "decode_inline_image", fake_decode)
    parts = [{"text": "draw"}]
    for index in range(GEMINI_MAX_IMAGES + 2):
        parts.append(
            {
                "inlineData": {
                    "data": str(index + 1),
                    "mimeType": "image/png",
                }
            }
        )
    parsed = parse_gemini_request(_body(parts=parts), _pro_spec())
    assert decoded_inputs == ["1", "2", "3", "4", "5", "6"]
    assert len(parsed.images) == GEMINI_MAX_IMAGES


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ("image/jpeg", "image/jpeg"),
        ("IMAGE/JPG", "image/jpeg"),
        ("image/png", "image/png"),
        (" image/webp ", "image/webp"),
    ],
)
def test_decode_inline_image_accepts_and_normalizes_allowed_mime_types(
    mime_type, expected
):
    decoded, normalized_mime = decode_inline_image(
        base64.b64encode(b"image").decode("ascii"), mime_type
    )
    assert decoded == b"image"
    assert normalized_mime == expected


@pytest.mark.parametrize("mime_type", ["text/plain", "image/gif", "", "application/json"])
def test_decode_inline_image_rejects_unsupported_mime_types(mime_type):
    _assert_invalid(
        lambda: decode_inline_image(
            base64.b64encode(b"image").decode("ascii"), mime_type
        ),
        "Unsupported inline image MIME type",
    )


@pytest.mark.parametrize("data", ["not-base64!", "abcde", "a==="])
def test_decode_inline_image_uses_strict_base64(data):
    _assert_invalid(
        lambda: decode_inline_image(data, "image/png"),
        "Invalid inline image base64",
    )


def test_decode_inline_image_rejects_encoded_data_before_decoding(monkeypatch):
    decode_calls: list[int] = []

    def fail_if_called(data, validate):
        decode_calls.append(len(data))
        raise AssertionError("base64 decoder must not be called")

    monkeypatch.setattr(gemini_native.base64, "b64decode", fail_if_called)
    oversized = "A" * (GEMINI_MAX_ENCODED_IMAGE_CHARS + 1)
    _assert_invalid(
        lambda: decode_inline_image(oversized, "image/png"),
        "Inline image exceeds 20 MiB",
    )
    assert decode_calls == []


def test_decode_inline_image_rejects_decoded_data_above_limit(monkeypatch):
    monkeypatch.setattr(
        gemini_native.base64,
        "b64decode",
        lambda data, validate: b"x" * (GEMINI_MAX_IMAGE_BYTES + 1),
    )
    _assert_invalid(
        lambda: decode_inline_image("AAAA", "image/png"),
        "Inline image exceeds 20 MiB",
    )


def test_parse_rejects_total_decoded_images_above_limit(monkeypatch):
    image = b"x" * (11 * 1024 * 1024)
    monkeypatch.setattr(
        gemini_native, "decode_inline_image", lambda data, mime_type: (image, mime_type)
    )
    parts = [{"text": "draw"}] + [
        {"inlineData": {"data": "AAAA", "mimeType": "image/png"}}
        for _ in range(4)
    ]
    _assert_invalid(lambda: parse_gemini_request(_body(parts=parts), _pro_spec()))
    assert len(image) * 4 > GEMINI_MAX_TOTAL_IMAGE_BYTES


@pytest.mark.parametrize("ratio", sorted(PRO_RATIOS))
def test_pro_accepts_its_exact_ratio_set(ratio):
    parsed = parse_gemini_request(
        _body(generation_config={"imageConfig": {"aspectRatio": ratio}}),
        _pro_spec(),
    )
    assert parsed.aspect_ratio == ratio


@pytest.mark.parametrize("ratio", sorted(FLASH_RATIOS))
def test_flash_accepts_its_exact_ratio_set(ratio):
    parsed = parse_gemini_request(
        _body(generation_config={"imageConfig": {"aspectRatio": ratio}}),
        _flash_spec(),
    )
    assert parsed.aspect_ratio == ratio


@pytest.mark.parametrize("ratio", sorted(FLASH_RATIOS - PRO_RATIOS))
def test_pro_rejects_flash_only_ratios(ratio):
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(generation_config={"imageConfig": {"aspectRatio": ratio}}),
            _pro_spec(),
        )
    )


@pytest.mark.parametrize("ratio", ["16:10", "5:3", "7:1", 1, None])
def test_parse_rejects_unknown_or_non_string_ratios(ratio):
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(generation_config={"imageConfig": {"aspectRatio": ratio}}),
            _flash_spec(),
        )
    )


def test_parse_uses_default_ratio_and_image_size():
    parsed = parse_gemini_request(_body(), _pro_spec())
    assert parsed.aspect_ratio == "1:1"
    assert parsed.image_size == "1K"


@pytest.mark.parametrize(
    ("image_size", "expected"),
    [("1K", "1K"), ("1k", "1K"), ("2k", "2K"), ("4K", "4K")],
)
def test_parse_accepts_image_size_case_insensitively(image_size, expected):
    parsed = parse_gemini_request(
        _body(generation_config={"imageConfig": {"imageSize": image_size}}),
        _pro_spec(),
    )
    assert parsed.image_size == expected


@pytest.mark.parametrize("image_size", ["0.5K", "8K", "", 1, None, True])
def test_parse_rejects_unsupported_image_sizes(image_size):
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(generation_config={"imageConfig": {"imageSize": image_size}}),
            _pro_spec(),
        )
    )


@pytest.mark.parametrize("generation_config", [None, {}, {"candidateCount": 1}])
def test_parse_accepts_absent_or_single_candidate(generation_config):
    parsed = parse_gemini_request(
        _body(generation_config=generation_config), _pro_spec()
    )
    assert parsed.candidate_count == 1


@pytest.mark.parametrize("candidate_count", [True, False, 0, 2, -1, "1", 1.0, None])
def test_parse_rejects_invalid_candidate_count(candidate_count):
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(generation_config={"candidateCount": candidate_count}),
            _pro_spec(),
        )
    )


def test_text_model_accepts_health_check_prompt_without_image_config():
    parsed = parse_gemini_request(
        _body(parts=[{"text": "health check"}]),
        GEMINI_MODELS["gemini-2.0-flash"],
    )
    assert parsed.prompt == "health check"
    assert parsed.images == ()


def test_text_model_rejects_inline_images():
    _assert_invalid(
        lambda: parse_gemini_request(
            _body(parts=[{"text": "health check"}, _inline()]),
            GEMINI_MODELS["gemini-2.0-flash"],
        )
    )


def test_deep_structure_validation_finishes_before_image_decoding(monkeypatch):
    decode_calls: list[str] = []

    def fake_decode(data: str, mime_type: str):
        decode_calls.append(data)
        return b"image", mime_type

    monkeypatch.setattr(gemini_native, "decode_inline_image", fake_decode)
    raw_body = _body(
        contents=[
            {"parts": [{"inlineData": {"data": "AAAA", "mimeType": "image/png"}}]},
            {"parts": [{"text": 42}]},
        ]
    )
    _assert_invalid(lambda: parse_gemini_request(raw_body, _pro_spec()))
    assert decode_calls == []
