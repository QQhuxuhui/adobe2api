import base64
import io
import json
import logging
import time
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
        self.upload_deadlines: list[float | None] = []
        self.generate_kwargs: dict | None = None
        self.generate_error: Exception | None = None

    # 签名跟真实 AdobeClient.upload_image 保持一致（含 deadline），
    # 否则替身会掩盖掉调用方漏传预算的问题。
    def upload_image(
        self,
        token: str,
        image_bytes: bytes,
        mime: str = "image/jpeg",
        deadline: float | None = None,
    ) -> str:
        self.uploads.append((token, image_bytes, mime))
        self.upload_deadlines.append(deadline)
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
    load_input_images=lambda messages, **kw: [],
    retry_kwargs: list | None = None,
):
    credit_contexts: list[tuple[str, str]] = []
    logging_fields: list[tuple[str, str]] = []

    def _retries(**kwargs):
        if retry_kwargs is not None:
            retry_kwargs.append(kwargs)
        return kwargs["run_once"]("token-value")

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
            run_with_token_retries=_retries,
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


def test_edits_request_quality_overrides_server_default(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={
            "prompt": "make it night",
            "model": "gpt-image-2",
            "quality": "medium",
            "size": "1024x1024",
        },
        files={"image": ("a.png", png_bytes(1024, 1024), "image/png")},
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["quality_level"] == "medium"
    assert adobe.generate_kwargs["output_resolution"] == "1K"
    assert adobe.generate_kwargs["output_size"] == {
        "width": 1024,
        "height": 1024,
    }


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
    # 归一化阶段就会解码每张图，坏图在这里被拦下（早于 free 比例解析，
    # 文案也不再限定"第一张"）。
    assert "cannot be decoded" in response.json()["error"]["message"]
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


def test_edits_json_explicit_size_beats_portrait_input_image(tmp_path: Path):
    """客诉复现: 参考图 9:16 + size 16:9 且不传 aspect_ratio, 结果按参考图出图。

    走 JSON body(images[{image_url}]) 这条下游 sub2api 实际用的链路,
    确认显式 size 一路传到上游 generate 的 aspect_ratio。
    """
    adobe = FakeAdobeClient()
    recorded: list = []
    client, _, _ = make_client(
        tmp_path, adobe, load_input_images=recording_load_input_images(recorded)
    )

    response = client.post(
        "/v1/images/edits",
        json={
            "model": "gpt-image-2",
            "prompt": "三视图设定图",
            "images": [{"image_url": data_url(png_bytes(900, 1600))}],
            "n": 1,
            "quality": "low",
            "size": "3840x2160",
        },
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["aspect_ratio"] == "16:9"
    assert adobe.generate_kwargs["upstream_model_id"] == "gpt-image"


def test_edits_propagates_deadline_to_upstream(tmp_path: Path):
    """端到端时限必须一路传到重试器和上传层。

    事故里这条路径 deadline=None，重试器的时限检查全程空转、上传各用固定超时，
    换一次号就整套重来，最后被下游 480s 掐断成 504。
    """
    adobe = FakeAdobeClient()
    retry_kwargs: list = []
    client, _, _ = make_client(tmp_path, adobe, retry_kwargs=retry_kwargs)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night", "model": "gpt-image-2"},
        files={"image": ("a.png", png_bytes(512, 512), "image/png")},
    )

    assert response.status_code == 200, response.text
    assert len(retry_kwargs) == 1
    deadline = retry_kwargs[0].get("deadline")
    assert deadline is not None, "重试器必须拿到 deadline，否则时限检查形同虚设"

    # 上传与生成共用同一个绝对截止时间：换号重试时不能各自重新计时，
    # 否则总时限永远到不了。
    assert adobe.upload_deadlines == [deadline]
    assert adobe.generate_kwargs["deadline"] == deadline


class _LeonardoOnlyTokenManager:
    """池里只有 Leonardo token → pool_prefers_leonardo 为真，edits 走 Leonardo 分支。"""

    def has_active_token(self, token_type=None):
        return token_type == "leonardo"


def test_multipart_deadline_returns_503_not_bare_500(tmp_path: Path, monkeypatch):
    """multipart 的 deadline 检查发生在 endpoint 的 try 之外。

    早期实现直接抛 UpstreamTemporaryError，会一路穿到 ASGI 层变成裸 500——
    而下游网关只对 503 换渠道重试，500 会被当成本端故障。
    """
    import api.routes.generation as gen_mod

    monkeypatch.setattr(gen_mod, "_edits_deadline", lambda: time.monotonic() - 1)
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night", "model": "gpt-image-2"},
        files={"image": ("a.png", png_bytes(512, 512), "image/png")},
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["type"] == "server_error"
    assert "deadline" in body["error"]["message"].lower()
    assert adobe.uploads == [], "预算已耗尽就不该再碰上游"


def test_json_loader_timeout_maps_to_503(tmp_path: Path):
    """JSON 分支的时限由 app.py 的 loader 抛出（那边另有单测），
    这里验证路由把它映射成 503 而不是 500——下游只对 503 换渠道重试。"""
    from core.adobe_client import UpstreamTemporaryError

    def timing_out_loader(messages, **kwargs):
        raise UpstreamTemporaryError(
            "Input image loading deadline exceeded",
            status_code=503,
            error_type="timeout",
        )

    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe, load_input_images=timing_out_loader)

    response = client.post(
        "/v1/images/edits",
        json={"prompt": "night", "model": "gpt-image-2",
              "image": data_url(png_bytes(64, 64))},
    )
    assert response.status_code == 503, response.text
    assert adobe.uploads == []


def test_leonardo_edits_receives_deadline(tmp_path: Path, monkeypatch):
    """Leonardo 分支的 edit_images 本来就支持 deadline，漏传的话
    上传+生成+轮询（最长 300s+）能整段穿透端到端时限。"""
    import api.routes.generation as gen_mod

    captured: dict = {}

    def fake_edit_images(client, token, **kwargs):
        captured.update(kwargs)
        return {
            "data": [{"url": "https://cdn.test/out.png"}],
            "provider": {"generation_id": "gen-1"},
        }

    class _Resp:
        content = b"leonardo-bytes"

    cdn_calls: list = []

    def fake_fetch(url, headers, *, max_seconds=None):
        cdn_calls.append(max_seconds)
        return _Resp()

    import core.leonardo_generation as leo_gen

    monkeypatch.setattr(leo_gen, "edit_images", fake_edit_images)
    monkeypatch.setattr(gen_mod, "_fetch_cdn_image", fake_fetch)
    monkeypatch.setattr(gen_mod, "_record_leonardo_credit_cost", lambda *a, **k: None)

    adobe = FakeAdobeClient()
    api = FastAPI()
    api.include_router(
        build_generation_router(
            store=object(),
            token_manager=_LeonardoOnlyTokenManager(),
            client=adobe,
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
            set_request_credit_context=lambda request, model, resolution: None,
            run_with_token_retries=lambda **kwargs: kwargs["run_once"]("leo-token"),
            set_request_error_detail=lambda request, **kwargs: "ERR-TEST",
            set_request_preview=lambda request, url, kind="image": None,
            public_image_url=lambda request, job_id: f"/generated/{job_id}.png",
            public_generated_url=lambda request, filename: f"/generated/{filename}",
            resolve_video_options=lambda data: (True, "", "frame"),
            load_input_images=lambda messages, **kw: [],
            normalize_image_mime=lambda mime: str(mime or "image/jpeg"),
            set_request_logging_fields=lambda request, model, prompt: None,
            prepare_video_source_image=lambda image, ratio, resolution: (image, "image/png"),
            video_ext_from_meta=lambda meta: "mp4",
            extract_prompt_from_messages=lambda messages: "",
            sse_chat_stream=lambda payload: iter(()),
            on_generated_file_written=lambda path, old_size, new_size: None,
            quota_error_cls=QuotaError,
            auth_error_cls=AuthError,
            upstream_temp_error_cls=UpstreamError,
            logger=logging.getLogger("test-images-edits-leo"),
        )
    )

    response = TestClient(api).post(
        "/v1/images/edits",
        data={"prompt": "watercolor", "model": "gpt-image-2"},
        files={"image": ("a.png", png_bytes(512, 512), "image/png")},
    )

    assert response.status_code == 200, response.text
    assert captured.get("deadline") is not None, "edit_images 必须拿到端到端时限"
    assert cdn_calls and cdn_calls[0] is not None, "CDN 下载也要吃同一份预算"
    assert cdn_calls[0] <= 300


class _FlakyUploadClient(FakeAdobeClient):
    """前 N 次上传成功，第 N+1 次抛错；换号后继续。"""

    def __init__(self, fail_after: int, error: Exception):
        super().__init__()
        self._fail_after = fail_after
        self._error = error
        self._raised = False

    def upload_image(self, token, image_bytes, mime="image/jpeg", deadline=None):
        if not self._raised and len(self.uploads) >= self._fail_after:
            self._raised = True
            raise self._error
        return super().upload_image(token, image_bytes, mime, deadline)


def test_edits_does_not_reupload_on_account_rotation(tmp_path: Path):
    """换号重试只补传缺的那几张，不把已成功的重传一遍。

    edits 一张图 = 一次上传调用，最多 6 张；重试全量重传等于自己放大对上传接口的
    压力，而 `upload image failed: 429` 正是现网占比最高的错误。
    Adobe 的 blob 跨账号通用（实测），所以换号后旧 id 仍然有效。
    """
    adobe = _FlakyUploadClient(fail_after=2, error=UpstreamError("upload image failed: 429"))
    tokens_used: list[str] = []

    def _retries(**kwargs):
        # 模拟换号：第一个账号在第 3 张上传时失败，换第二个账号继续
        for tok in ("token-A", "token-B"):
            tokens_used.append(tok)
            try:
                return kwargs["run_once"](tok)
            except UpstreamError:
                continue
        raise AssertionError("both accounts failed")

    client, _, _ = make_client(tmp_path, adobe)
    client.app.dependency_overrides = {}
    # 直接替换注入的重试器
    api = FastAPI()
    api.include_router(
        build_generation_router(
            store=object(), token_manager=object(), client=adobe,
            credits_tracker=object(), request_log_store=object(),
            generated_dir=tmp_path, model_catalog=MODEL_CATALOG,
            video_model_catalog=VIDEO_MODEL_CATALOG, supported_ratios=SUPPORTED_RATIOS,
            resolve_model=resolve_model, resolve_image_geometry=resolve_image_geometry,
            require_service_api_key=lambda request: None,
            set_request_task_progress=lambda request, **kw: None,
            set_request_credit_context=lambda request, m, r: None,
            run_with_token_retries=_retries,
            set_request_error_detail=lambda request, **kw: "ERR-TEST",
            set_request_preview=lambda request, url, kind="image": None,
            public_image_url=lambda request, job_id: f"/generated/{job_id}.png",
            public_generated_url=lambda request, fn: f"/generated/{fn}",
            resolve_video_options=lambda data: (True, "", "frame"),
            load_input_images=lambda messages, **kw: [],
            normalize_image_mime=lambda mime: str(mime or "image/jpeg"),
            set_request_logging_fields=lambda request, m, p: None,
            prepare_video_source_image=lambda i, r, res: (i, "image/png"),
            video_ext_from_meta=lambda meta: "mp4",
            extract_prompt_from_messages=lambda messages: "",
            sse_chat_stream=lambda payload: iter(()),
            on_generated_file_written=lambda p, o, n: None,
            quota_error_cls=QuotaError, auth_error_cls=AuthError,
            upstream_temp_error_cls=UpstreamError,
            logger=logging.getLogger("test-upload-cache"),
        )
    )
    resp = TestClient(api).post(
        "/v1/images/edits",
        data={"prompt": "merge", "model": "gpt-image-2"},
        files=[
            ("image[]", ("a.png", png_bytes(256, 256), "image/png")),
            ("image[]", ("b.png", png_bytes(256, 256), "image/png")),
            ("image[]", ("c.png", png_bytes(256, 256), "image/png")),
            ("image[]", ("d.png", png_bytes(256, 256), "image/png")),
        ],
    )

    assert resp.status_code == 200, resp.text
    assert tokens_used == ["token-A", "token-B"], "应当发生了一次换号"
    # 4 张图：账号A 传成 2 张后第 3 张失败；账号B 只需补第 3、4 张 = 总共 4 次上传。
    # 若换号后全量重传，总数会是 2(成功) + 1(失败不计入) + 4 = 6 次。
    assert len(adobe.uploads) == 4, (
        f"换号后应只补传缺的两张，实际上传 {len(adobe.uploads)} 次"
    )
    assert adobe.generate_kwargs["source_image_ids"] == [
        "img-1", "img-2", "img-3", "img-4",
    ]
