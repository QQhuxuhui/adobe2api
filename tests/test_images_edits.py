import base64
import io
import json
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from api.routes.generation import build_generation_router
from core.models import (
    MODEL_CATALOG,
    SUPPORTED_RATIOS,
    VIDEO_MODEL_CATALOG,
    resolve_image_geometry,
    resolve_model,
)


class QuotaError(Exception):
    pass


class AuthError(Exception):
    pass


class UpstreamError(Exception):
    pass


class FakeAdobeClient:
    generate_timeout = 60
    gpt_image_quality = "standard"

    def __init__(self):
        self.uploads: list[tuple[str, bytes, str]] = []
        self.generate_kwargs: dict | None = None
        self.generate_error: Exception | None = None

    def upload_image(self, token: str, image_bytes: bytes, mime: str) -> str:
        self.uploads.append((token, image_bytes, mime))
        return f"img-{len(self.uploads)}"

    def generate(self, **kwargs):
        if self.generate_error is not None:
            raise self.generate_error
        self.generate_kwargs = kwargs
        return b"edited-image-bytes", {"progress": 100}


def png_bytes(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (40, 80, 120)).save(output, format="PNG")
    return output.getvalue()


def data_url(image_bytes: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(image_bytes).decode()


def recording_load_input_images(recorded: list):
    """最小可用的 load_input_images 替身: 记录入参并解出 data URL 字节。

    真实实现在 app.py::_load_input_images（还负责 http 下载/大小上限），
    这里只验证 generation.py 是否按约定的 messages 结构调用它。
    """

    def _load(messages) -> list[tuple[bytes, str]]:
        recorded.append(messages)
        loaded: list[tuple[bytes, str]] = []
        for message in messages:
            for part in message.get("content", []):
                url = part["image_url"]["url"]
                header, _, encoded = url.partition(",")
                mime = header.removeprefix("data:").removesuffix(";base64")
                loaded.append((base64.b64decode(encoded), mime))
        return loaded

    return _load


def make_client(
    tmp_path: Path,
    adobe_client: FakeAdobeClient,
    load_input_images=lambda messages: [],
):
    credit_contexts: list[tuple[str, str]] = []
    logging_fields: list[tuple[str, str]] = []
    api = FastAPI()
    api.include_router(
        build_generation_router(
            store=object(),
            token_manager=object(),
            client=adobe_client,
            credits_tracker=object(),
            request_log_store=object(),
            generated_dir=tmp_path,
            model_catalog=MODEL_CATALOG,
            video_model_catalog=VIDEO_MODEL_CATALOG,
            supported_ratios=SUPPORTED_RATIOS,
            resolve_model=resolve_model,
            resolve_image_geometry=resolve_image_geometry,
            require_service_api_key=lambda request: None,
            set_request_task_progress=lambda request, **kwargs: None,
            set_request_credit_context=lambda request, model, resolution: (
                credit_contexts.append((model, resolution))
            ),
            run_with_token_retries=lambda **kwargs: kwargs["run_once"]("token-value"),
            set_request_error_detail=lambda request, **kwargs: "ERR-TEST",
            set_request_preview=lambda request, url, kind="image": None,
            public_image_url=lambda request, job_id: f"/generated/{job_id}.png",
            public_generated_url=lambda request, filename: f"/generated/{filename}",
            resolve_video_options=lambda data: (True, "", "frame"),
            load_input_images=load_input_images,
            normalize_image_mime=lambda mime: str(mime or "image/jpeg"),
            set_request_logging_fields=lambda request, model, prompt: (
                logging_fields.append((model, prompt))
            ),
            prepare_video_source_image=lambda image, ratio, resolution: (
                image,
                "image/png",
            ),
            video_ext_from_meta=lambda meta: "mp4",
            extract_prompt_from_messages=lambda messages: "",
            sse_chat_stream=lambda payload: iter(()),
            on_generated_file_written=lambda path, old_size, new_size: None,
            quota_error_cls=QuotaError,
            auth_error_cls=AuthError,
            upstream_temp_error_cls=UpstreamError,
            logger=logging.getLogger("test-images-edits"),
        )
    )
    return TestClient(api), credit_contexts, logging_fields


def test_edits_happy_path_defaults_to_b64_json(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, credit_contexts, logging_fields = make_client(tmp_path, adobe)
    image = png_bytes(1536, 1024)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "make it night", "model": "gpt-image-2", "size": "1536x1024"},
        files={"image": ("a.png", image, "image/png")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "gpt-image-2"
    # 不带 response_format 时对齐真实 gpt-image: 返回 b64_json 而非 url
    # (codex 等客户端硬解 b64_json, 拿到 url 会报 missing field b64_json)
    assert "url" not in body["data"][0]
    assert base64.b64decode(body["data"][0]["b64_json"]) == b"edited-image-bytes"
    # 输入图 token 按官方 patch 公式计入 usage: 1536x1024 → 48*32 = 1536
    assert body["usage"]["input_tokens_details"]["image_tokens"] == 1536
    assert credit_contexts == [("gpt-image-2", "2K")]
    assert logging_fields == [("gpt-image-2", "make it night")]
    assert adobe.uploads == [("token-value", image, "image/png")]
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1"]
    assert adobe.generate_kwargs["aspect_ratio"] == "3:2"
    assert adobe.generate_kwargs["upstream_model_id"] == "gpt-image"
    assert adobe.generate_kwargs["quality_level"] == "standard"


def test_edits_accepts_bracket_field_name_and_multiple_images(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "merge these"},
        files=[
            ("image[]", ("a.png", png_bytes(1200, 800), "image/png")),
            ("image[]", ("b.png", png_bytes(800, 1200), "image/png")),
        ],
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1", "img-2"]
    body = response.json()
    # 1200x800 → ceil(1200/32)*ceil(800/32) = 38*25 = 950, 竖版对称同值, 求和 1900
    assert body["usage"]["input_tokens_details"]["image_tokens"] == 1900


def test_edits_free_uses_first_image_ratio_for_gpt_image(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "merge", "model": "gpt-image-2", "aspect_ratio": "free"},
        files=[
            ("image[]", ("portrait.png", png_bytes(1000, 1379), "image/png")),
            ("image[]", ("landscape.png", png_bytes(1600, 900), "image/png")),
        ],
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["aspect_ratio"] == "3:4"
    assert adobe.generate_kwargs["output_size"] is None


def test_edits_free_passes_primary_image_size_to_auto_capable_model(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={
            "prompt": "edit",
            "model": "firefly-nano-banana-pro",
            "aspect_ratio": "auto",
        },
        files={"image": ("portrait.png", png_bytes(1000, 1379), "image/png")},
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["aspect_ratio"] == "auto"
    assert adobe.generate_kwargs["fallback_aspect_ratio"] == "3:4"
    size = adobe.generate_kwargs["output_size"]
    assert size["width"] < size["height"]
    assert abs(size["width"] / size["height"] - 1000 / 1379) < 0.01


def test_edits_free_rejects_unreadable_first_image(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "edit", "model": "gpt-image-2", "aspect_ratio": "free"},
        files=[
            ("image[]", ("broken.png", b"not-an-image", "image/png")),
            ("image[]", ("valid.png", png_bytes(1600, 900), "image/png")),
        ],
    )

    assert response.status_code == 400
    assert "first input image" in response.json()["error"]["message"]
    assert adobe.uploads == []


def test_edits_explicit_url_response(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night", "response_format": "url"},
        files={"image": ("a.png", png_bytes(1024, 1024), "image/png")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["url"].startswith("/generated/")


def test_edits_b64_json_response(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night", "response_format": "b64_json"},
        files={"image": ("a.png", png_bytes(1024, 1024), "image/png")},
    )

    assert response.status_code == 200, response.text
    b64 = response.json()["data"][0]["b64_json"]
    assert base64.b64decode(b64) == b"edited-image-bytes"


def test_edits_mask_is_ignored(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night"},
        files=[
            ("image", ("a.png", png_bytes(1024, 1024), "image/png")),
            ("mask", ("m.png", b"mask-bytes", "image/png")),
        ],
    )

    assert response.status_code == 200, response.text
    # mask 不作为输入图上传
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1"]


def test_edits_requires_prompt(tmp_path: Path):
    client, _, _ = make_client(tmp_path, FakeAdobeClient())

    response = client.post(
        "/v1/images/edits",
        files={"image": ("a.png", b"png-bytes", "image/png")},
    )

    assert response.status_code == 400
    assert "prompt" in response.json()["error"]["message"]


def test_edits_requires_image(tmp_path: Path):
    client, _, _ = make_client(tmp_path, FakeAdobeClient())

    response = client.post("/v1/images/edits", data={"prompt": "night"})

    assert response.status_code == 400
    assert "image" in response.json()["error"]["message"]


def test_edits_rejects_bad_response_format(tmp_path: Path):
    client, _, _ = make_client(tmp_path, FakeAdobeClient())

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night", "response_format": "hex"},
        files={"image": ("a.png", b"png-bytes", "image/png")},
    )

    assert response.status_code == 400


def test_edits_json_body_with_images_image_url(tmp_path: Path):
    adobe = FakeAdobeClient()
    recorded: list = []
    client, credit_contexts, logging_fields = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images(recorded)
    )
    image = png_bytes(1536, 1024)

    response = client.post(
        "/v1/images/edits",
        json={
            "model": "gpt-image-2",
            "images": [{"image_url": data_url(image)}],
            "prompt": "变成黑夜",
            "size": "1536x1024",
            "n": 1,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "gpt-image-2"
    assert base64.b64decode(body["data"][0]["b64_json"]) == b"edited-image-bytes"
    assert adobe.uploads == [("token-value", image, "image/png")]
    assert adobe.generate_kwargs["aspect_ratio"] == "3:2"
    assert credit_contexts == [("gpt-image-2", "2K")]
    assert logging_fields == [("gpt-image-2", "变成黑夜")]
    # 传给 load_input_images 的是 chat 风格 messages,复用 app.py 的下载/校验逻辑
    assert recorded == [
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url(image)}}
                ],
            }
        ]
    ]


def test_edits_json_body_accepts_top_level_image_url_string(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "model": "gpt-image-2",
            "image_url": data_url(png_bytes(1024, 1024)),
            "prompt": "night",
        },
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1"]


def test_edits_json_body_accepts_multiple_images(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "images": [
                {"image_url": data_url(png_bytes(1200, 800))},
                {"image_url": data_url(png_bytes(800, 1200))},
            ],
            "prompt": "merge these",
        },
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1", "img-2"]


def test_edits_json_body_requires_prompt(tmp_path: Path):
    client, _, _ = make_client(
        tmp_path, FakeAdobeClient(), load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={"images": [{"image_url": data_url(png_bytes(64, 64))}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "prompt is required"


def test_edits_json_body_requires_image(tmp_path: Path):
    client, _, _ = make_client(
        tmp_path, FakeAdobeClient(), load_input_images=recording_load_input_images([])
    )

    response = client.post("/v1/images/edits", json={"prompt": "night"})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "image is required"


def test_edits_json_body_rejects_more_than_six_images(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "prompt": "merge",
            "images": [{"image_url": data_url(png_bytes(64, 64))} for _ in range(7)],
        },
    )

    assert response.status_code == 400
    assert "at most 6" in response.json()["error"]["message"]
    assert adobe.uploads == []


def test_edits_json_body_rejects_invalid_json(tmp_path: Path):
    client, _, _ = make_client(tmp_path, FakeAdobeClient())

    response = client.post(
        "/v1/images/edits",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_edits_json_body_explicit_url_response(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "images": [{"image_url": data_url(png_bytes(1024, 1024))}],
            "prompt": "night",
            "response_format": "url",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["url"].startswith("/generated/")


def test_edits_json_body_mask_is_ignored(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "images": [{"image_url": data_url(png_bytes(1024, 1024))}],
            "mask": {"image_url": data_url(png_bytes(64, 64))},
            "prompt": "night",
        },
    )

    assert response.status_code == 200, response.text
    # mask 不作为输入图上传
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1"]


def test_edits_json_body_network_failure_is_retryable_and_not_leaky(tmp_path: Path):
    """外链拉取的网络异常(超时/DNS)不是 HTTPException,不能冒泡成未处理异常。

    回 503 让上游网关可以换号重试,且不能把原始异常文本(含内网主机名)回给客户端。
    """

    def _boom(messages):
        raise OSError(
            "HTTPSConnectionPool(host='10.0.0.5', port=443): Max retries exceeded"
        )

    client, _, _ = make_client(tmp_path, FakeAdobeClient(), load_input_images=_boom)

    response = client.post(
        "/v1/images/edits",
        json={"images": [{"image_url": "https://example.com/a.png"}], "prompt": "night"},
    )

    assert response.status_code == 503
    message = response.json()["error"]["message"]
    assert "10.0.0.5" not in message
    assert "HTTPSConnectionPool" not in message


def test_edits_json_body_does_not_duplicate_images_across_fields(tmp_path: Path):
    """同一张图同时出现在 image 和 images 时只能上传一次,否则重复计费。"""
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )
    url = data_url(png_bytes(1024, 1024))

    response = client.post(
        "/v1/images/edits",
        json={"image": url, "images": [{"image_url": url}], "prompt": "night"},
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1"]
    # 只上传一次 → 只计一张: 1024x1024 → 32*32 = 1024
    assert response.json()["usage"]["input_tokens_details"]["image_tokens"] == 1024


def test_edits_json_body_without_content_type_header(tmp_path: Path):
    """有客户端发 JSON 不带 Content-Type,不能因此掉进空表单分支误报 prompt is required。"""
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )
    payload = json.dumps(
        {"images": [{"image_url": data_url(png_bytes(1024, 1024))}], "prompt": "night"}
    ).encode()

    response = client.post(
        "/v1/images/edits", content=payload, headers={"Content-Type": "text/plain"}
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1"]


def test_edits_json_body_rejects_non_string_prompt(tmp_path: Path):
    """prompt 传成数组时不能 str() 出 "['a', 'b']" 当提示词烧额度。"""
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "images": [{"image_url": data_url(png_bytes(64, 64))}],
            "prompt": ["make", "it", "night"],
        },
    )

    assert response.status_code == 400
    assert "prompt" in response.json()["error"]["message"]
    assert adobe.uploads == []


def test_edits_json_body_ignores_malformed_image_entries(tmp_path: Path):
    """images 里混进 null/数字时跳过而不是崩。"""
    adobe = FakeAdobeClient()
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images([])
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "images": [None, 123, {"image_url": data_url(png_bytes(1024, 1024))}, {}],
            "prompt": "night",
        },
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1"]


def test_edits_maps_quota_error_to_429(tmp_path: Path):
    adobe = FakeAdobeClient()
    adobe.generate_error = QuotaError("quota")
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night"},
        files={"image": ("a.png", png_bytes(1024, 1024), "image/png")},
    )

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"
