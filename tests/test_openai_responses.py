import base64
import io
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from api.routes.generation import build_generation_router
from api.streaming import sse_chat_stream
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


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 0, 255)).save(output, format="PNG")
    return output.getvalue()


class FakeAdobeClient:
    generate_timeout = 60
    gpt_image_quality = "low"

    def __init__(self):
        self.generate_kwargs = None
        self.uploads = []
        self.error = None

    def upload_image(self, token, image_bytes, mime):
        self.uploads.append((token, image_bytes, mime))
        return f"source-{len(self.uploads)}"

    def generate(self, **kwargs):
        if self.error:
            raise self.error
        self.generate_kwargs = kwargs
        return png_bytes(), {"progress": 100}


class Harness:
    def __init__(self, tmp_path: Path, require_key=lambda request: None):
        self.adobe = FakeAdobeClient()
        self.credit_contexts = []
        self.previews = []
        self.retry_calls = []
        self.loaded_images = []
        api = FastAPI()

        def run_with_token_retries(**kwargs):
            self.retry_calls.append(kwargs["operation_name"])
            return kwargs["run_once"]("token-value")

        api.include_router(
            build_generation_router(
                store=object(),
                token_manager=object(),
                client=self.adobe,
                credits_tracker=object(),
                request_log_store=object(),
                generated_dir=tmp_path,
                model_catalog=MODEL_CATALOG,
                video_model_catalog=VIDEO_MODEL_CATALOG,
                supported_ratios=SUPPORTED_RATIOS,
                resolve_model=resolve_model,
                resolve_ratio_and_resolution=resolve_ratio_and_resolution,
                require_service_api_key=require_key,
                set_request_task_progress=lambda request, **kwargs: None,
                set_request_credit_context=lambda request, model, resolution: self.credit_contexts.append(
                    (model, resolution)
                ),
                run_with_token_retries=run_with_token_retries,
                set_request_error_detail=lambda request, **kwargs: "ERR-TEST",
                set_request_preview=lambda request, url, kind="image": self.previews.append(
                    (url, kind)
                ),
                public_image_url=lambda request, job_id: f"https://images.test/{job_id}.png",
                public_generated_url=lambda request, filename: f"https://images.test/{filename}",
                resolve_video_options=lambda data: (True, "", "frame"),
                load_input_images=lambda messages: self.loaded_images,
                normalize_image_mime=lambda mime: str(mime or "image/jpeg"),
                set_request_logging_fields=lambda request, model, prompt: None,
                prepare_video_source_image=lambda image, ratio, resolution: (
                    image,
                    "image/png",
                ),
                video_ext_from_meta=lambda meta: "mp4",
                extract_prompt_from_messages=lambda messages: "",
                sse_chat_stream=sse_chat_stream,
                on_generated_file_written=lambda path, old, new: None,
                quota_error_cls=QuotaError,
                auth_error_cls=AuthError,
                upstream_temp_error_cls=UpstreamError,
                logger=logging.getLogger("test-openai-responses"),
            )
        )
        self.http = TestClient(api)


def test_non_streaming_response_returns_image_generation_call(tmp_path: Path):
    harness = Harness(tmp_path)
    response = harness.http.post(
        "/v1/responses",
        json={
            "model": "gpt-image-2",
            "input": "draw a blue square",
            "size": "1024x1024",
            "quality": "low",
            "stream": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["type"] == "image_generation_call"
    assert base64.b64decode(payload["output"][0]["result"]) == png_bytes()
    assert "images.test" not in payload["output"][0]["result"]
    assert harness.credit_contexts == [("gpt-image-2", "1K")]
    assert harness.retry_calls == ["responses.create"]
    assert harness.previews[0][1] == "image"


def test_responses_requires_service_authentication(tmp_path: Path):
    def reject(request):
        raise HTTPException(status_code=401, detail="invalid api key")

    harness = Harness(tmp_path, require_key=reject)
    response = harness.http.post(
        "/v1/responses",
        json={"model": "gpt-image-2", "input": "draw"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("body", [[], None])
def test_responses_rejects_non_object_json_body(tmp_path: Path, body):
    response = Harness(tmp_path).http.post("/v1/responses", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.json()["error"]["message"] == "request body must be an object"


def test_streaming_response_emits_image_events_in_order(tmp_path: Path):
    harness = Harness(tmp_path)
    response = harness.http.post(
        "/v1/responses",
        json={"model": "gpt-image-2", "input": "draw", "stream": True},
    )
    assert response.status_code == 200
    event_names = [
        line[7:] for line in response.text.splitlines() if line.startswith("event: ")
    ]
    assert event_names == [
        "response.created",
        "response.output_item.added",
        "response.output_item.done",
        "response.completed",
    ]
    assert "response.output_text" not in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_text_model_with_image_tool_uses_gpt_image_2_backend(tmp_path: Path):
    harness = Harness(tmp_path)
    response = harness.http.post(
        "/v1/responses",
        json={
            "model": "gpt-5.4-mini",
            "input": "draw",
            "tools": [{"type": "image_generation", "quality": "high"}],
            "tool_choice": {"type": "image_generation"},
        },
    )
    assert response.status_code == 200
    assert response.json()["model"] == "gpt-5.4-mini"
    assert harness.credit_contexts == [("gpt-image-2", "4K")]


def test_input_image_is_uploaded_and_forwarded(tmp_path: Path):
    harness = Harness(tmp_path)
    harness.loaded_images = [(png_bytes(), "image/png")]
    response = harness.http.post(
        "/v1/responses",
        json={
            "model": "gpt-image-2",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "edit"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,aW1hZ2U=",
                        },
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert harness.adobe.uploads == [("token-value", png_bytes(), "image/png")]
    assert harness.adobe.generate_kwargs["source_image_ids"] == ["source-1"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"model": "gpt-image-2", "input": ""}, "input is required"),
        (
            {"model": "gpt-image-2", "input": "draw", "tool_choice": "none"},
            "tool_choice",
        ),
        (
            {
                "model": "gpt-image-2",
                "input": "draw",
                "background": "transparent",
            },
            "transparent",
        ),
        (
            {"model": "gpt-image-2", "input": "draw", "partial_images": 1},
            "partial_images",
        ),
        (
            {"model": "gpt-5.4-mini", "input": "draw"},
            "image_generation tool is required",
        ),
    ],
)
def test_responses_validation_errors_are_openai_shaped(
    tmp_path: Path, payload, message
):
    response = Harness(tmp_path).http.post("/v1/responses", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert message in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("error", "status_code", "error_type"),
    [
        (QuotaError("quota"), 429, "rate_limit_error"),
        (AuthError("auth"), 401, "authentication_error"),
        (UpstreamError("upstream"), 503, "server_error"),
    ],
)
def test_responses_maps_generation_errors(
    tmp_path: Path, error, status_code, error_type
):
    harness = Harness(tmp_path)
    harness.adobe.error = error
    response = harness.http.post(
        "/v1/responses",
        json={"model": "gpt-image-2", "input": "draw"},
    )
    assert response.status_code == status_code
    assert response.json()["error"]["type"] == error_type
