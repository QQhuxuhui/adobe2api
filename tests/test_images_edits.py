import base64
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.generation import build_generation_router
from core.models import (
    MODEL_CATALOG,
    SUPPORTED_RATIOS,
    VIDEO_MODEL_CATALOG,
    resolve_model,
    resolve_ratio_and_resolution,
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


def make_client(tmp_path: Path, adobe_client: FakeAdobeClient):
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
            resolve_ratio_and_resolution=resolve_ratio_and_resolution,
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
            load_input_images=lambda messages: [],
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


def test_edits_happy_path_url(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, credit_contexts, logging_fields = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "make it night", "model": "gpt-image-2", "size": "1536x1024"},
        files={"image": ("a.png", b"png-bytes", "image/png")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "gpt-image-2"
    assert body["data"][0]["url"].startswith("/generated/")
    # 输入图 token 计入 usage
    assert body["usage"]["input_tokens_details"]["image_tokens"] == 300
    assert credit_contexts == [("gpt-image-2", "2K")]
    assert logging_fields == [("gpt-image-2", "make it night")]
    assert adobe.uploads == [("token-value", b"png-bytes", "image/png")]
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
            ("image[]", ("a.png", b"first", "image/png")),
            ("image[]", ("b.jpg", b"second", "image/jpeg")),
        ],
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["source_image_ids"] == ["img-1", "img-2"]
    body = response.json()
    assert body["usage"]["input_tokens_details"]["image_tokens"] == 600


def test_edits_b64_json_response(tmp_path: Path):
    adobe = FakeAdobeClient()
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night", "response_format": "b64_json"},
        files={"image": ("a.png", b"png-bytes", "image/png")},
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
            ("image", ("a.png", b"png-bytes", "image/png")),
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


def test_edits_maps_quota_error_to_429(tmp_path: Path):
    adobe = FakeAdobeClient()
    adobe.generate_error = QuotaError("quota")
    client, _, _ = make_client(tmp_path, adobe)

    response = client.post(
        "/v1/images/edits",
        data={"prompt": "night"},
        files={"image": ("a.png", b"png-bytes", "image/png")},
    )

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"
