import logging
import io
from pathlib import Path

import pytest
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
from core.stores import JobStore


class DomainError(Exception):
    pass


class NoopCreditsTracker:
    def begin(self, *args, **kwargs):
        pass

    def finish(self, *args, **kwargs):
        pass

    def complete(self, **kwargs):
        pass


class CaptureCreditsTracker(NoopCreditsTracker):
    def __init__(self):
        self.begins: list[tuple[tuple, dict]] = []
        self.finishes: list[tuple[tuple, dict]] = []
        self.completions: list[dict] = []

    def begin(self, *args, **kwargs):
        self.begins.append((args, kwargs))

    def finish(self, *args, **kwargs):
        self.finishes.append((args, kwargs))

    def complete(self, **kwargs):
        self.completions.append(kwargs)


class CaptureRequestLogStore:
    def __init__(self):
        self.records: list[dict] = []

    def upsert(self, log_id: str, payload: dict):
        self.records.append({**payload, "id": log_id})


class JobTokenManager:
    def get_available(self, strategy: str):
        assert strategy == "round_robin"
        return "token-value"

    def get_meta_by_value(self, token: str):
        assert token == "token-value"
        return {
            "token_id": "token-1",
            "token_account_id": "account-1",
            "token_account_name": "Primary",
            "token_account_email": "primary@example.com",
            "token_source": "manual",
        }

    def report_exhausted(self, token: str):
        raise AssertionError("unexpected quota failure")

    def report_invalid(self, token: str):
        raise AssertionError("unexpected auth failure")


class JobAdobeClient:
    retry_enabled = True
    retry_max_attempts = 1
    token_rotation_strategy = "round_robin"
    gpt_image_quality = "standard"
    generate_timeout = 60

    def __init__(self):
        self.generate_kwargs = None

    def upload_image(self, token, image_bytes, mime):
        return "source-1"

    def generate(self, **kwargs):
        assert kwargs["token"] == "token-value"
        self.generate_kwargs = kwargs
        return b"generated", {"progress": 100}


def png_bytes(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


def make_client(
    tmp_path: Path,
    *,
    store=None,
    token_manager=None,
    adobe_client=None,
    credits_tracker=None,
    request_log_store=None,
    input_images=None,
    execute_retries: bool = False,
):
    credit_contexts: list[tuple[str, str]] = []
    api = FastAPI()
    api.include_router(
        build_generation_router(
            store=store or object(),
            token_manager=token_manager or object(),
            client=adobe_client or object(),
            credits_tracker=credits_tracker or NoopCreditsTracker(),
            request_log_store=request_log_store or CaptureRequestLogStore(),
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
            run_with_token_retries=(
                (lambda **kwargs: kwargs["run_once"]("token-value"))
                if execute_retries
                else (lambda **kwargs: {"ok": True})
            ),
            set_request_error_detail=lambda request, **kwargs: "ERR-TEST",
            set_request_preview=lambda request, url, kind="image": None,
            public_image_url=lambda request, job_id: f"/generated/{job_id}.png",
            public_generated_url=lambda request, filename: f"/generated/{filename}",
            resolve_video_options=lambda data: (True, "", "frame"),
            load_input_images=lambda messages: list(input_images or []),
            normalize_image_mime=lambda mime: str(mime or "image/jpeg"),
            set_request_logging_fields=lambda request, model, prompt: None,
            prepare_video_source_image=lambda image, ratio, resolution: (
                image,
                "image/png",
            ),
            video_ext_from_meta=lambda meta: "mp4",
            extract_prompt_from_messages=lambda messages: "draw this",
            sse_chat_stream=lambda payload: iter(()),
            on_generated_file_written=lambda path, old_size, new_size: None,
            quota_error_cls=DomainError,
            auth_error_cls=DomainError,
            upstream_temp_error_cls=DomainError,
            logger=logging.getLogger("test-generation-credit-context"),
        )
    )
    return TestClient(api), credit_contexts


def test_openai_image_route_captures_resolved_model_and_resolution(tmp_path: Path):
    client, credit_contexts = make_client(tmp_path)

    response = client.post(
        "/v1/images/generations",
        json={
            "model": "firefly-gpt-image",
            "prompt": "draw this",
            "quality": "high",
            "size": "1024x1024",
        },
    )

    assert response.status_code == 200
    assert credit_contexts == [("firefly-gpt-image", "4K")]


def test_chat_free_resolves_after_loading_primary_image(tmp_path: Path):
    adobe = JobAdobeClient()
    client, _ = make_client(
        tmp_path,
        token_manager=JobTokenManager(),
        adobe_client=adobe,
        input_images=[(png_bytes(1000, 1379), "image/png")],
        execute_retries=True,
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-image-2",
            "messages": [{"role": "user", "content": "edit"}],
            "aspect_ratio": "free",
        },
    )

    assert response.status_code == 200, response.text
    assert adobe.generate_kwargs["aspect_ratio"] == "3:4"


def test_video_chat_route_captures_catalog_resolution(tmp_path: Path):
    client, credit_contexts = make_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "firefly-veo31-8s-16x9-1080p",
            "messages": [{"role": "user", "content": "draw this"}],
        },
    )

    assert response.status_code == 200
    assert credit_contexts == [("firefly-veo31-8s-16x9-1080p", "1080p")]


def test_v1_models_exposes_public_video_aliases(tmp_path: Path):
    client, _credit_contexts = make_client(tmp_path)
    response = client.get("/v1/models")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert {
        "sora-2",
        "sora-2-pro",
        "veo-3.1-generate-preview",
        "veo-3.1-fast-generate-preview",
    }.issubset(ids)


def test_chat_video_alias_uses_request_parameters_for_credit_mapping(tmp_path: Path):
    client, credit_contexts = make_client(tmp_path)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "veo-3.1-generate-preview",
            "messages": [{"role": "user", "content": "p"}],
            "duration": 8,
            "aspect_ratio": "9:16",
            "resolution": "1080p",
        },
    )
    assert response.status_code == 200
    assert credit_contexts == [("firefly-veo31-8s-9x16-1080p", "1080p")]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seed", 1),
        ("safetySettings", {"level": "strict"}),
        ("image", {"bytes": "not-supported"}),
    ],
)
def test_chat_video_alias_rejects_nonempty_unsupported_parameters(
    tmp_path: Path, field: str, value
):
    client, _credit_contexts = make_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "veo-3.1-generate-preview",
            "messages": [{"role": "user", "content": "p"}],
            field: value,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"


def test_async_job_success_updates_log_and_submits_credit_measurement(tmp_path: Path):
    jobs = JobStore()
    tokens = JobTokenManager()
    adobe_client = JobAdobeClient()
    tracker = CaptureCreditsTracker()
    request_logs = CaptureRequestLogStore()
    client, credit_contexts = make_client(
        tmp_path,
        store=jobs,
        token_manager=tokens,
        adobe_client=adobe_client,
        credits_tracker=tracker,
        request_log_store=request_logs,
    )

    response = client.post(
        "/api/v1/generate",
        json={
            "prompt": "draw this",
            "aspect_ratio": "16:9",
            "output_resolution": "2K",
            "model": "firefly-gpt-image",
        },
    )

    assert response.status_code == 200
    job = jobs.get(response.json()["task_id"])
    assert job is not None and job.status == "succeeded"
    assert credit_contexts == [("firefly-gpt-image", "2K")]
    assert tracker.begins[0] == (
        ("token-1", tracker.begins[0][0][1]),
        {"account_id": "account-1"},
    )
    assert len(tracker.completions) == 1
    completion = tracker.completions[0]
    assert completion["token_id"] == "token-1"
    assert completion["account_id"] == "account-1"
    assert completion["model_id"] == "firefly-gpt-image"
    assert completion["output_resolution"] == "2K"
    assert completion["payload"]["task_status"] == "COMPLETED"
    assert request_logs.records[-1]["token_account_email"] == "primary@example.com"


def test_async_job_defaults_omitted_ratio_to_auto(tmp_path: Path):
    jobs = JobStore()
    adobe_client = JobAdobeClient()
    client, credit_contexts = make_client(
        tmp_path,
        store=jobs,
        token_manager=JobTokenManager(),
        adobe_client=adobe_client,
        credits_tracker=CaptureCreditsTracker(),
    )

    response = client.post(
        "/api/v1/generate",
        json={"prompt": "draw this", "output_resolution": "2K"},
    )

    assert response.status_code == 200
    assert adobe_client.generate_kwargs["aspect_ratio"] == "auto"
    assert credit_contexts == [("firefly-nano-banana-pro", "2K")]
