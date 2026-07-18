from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api.routes.openai_videos import build_openai_videos_router
from core.video_tasks import (
    VideoTaskCapacityError,
    VideoTaskRecord,
    VideoTaskSpec,
    VideoTaskStore,
)


class FakeTaskManager:
    def __init__(self, store: VideoTaskStore) -> None:
        self.store = store
        self.specs: list[VideoTaskSpec] = []
        self.reject_capacity = False

    def submit(self, spec: VideoTaskSpec) -> VideoTaskRecord:
        if self.reject_capacity:
            raise VideoTaskCapacityError("video task queue is full")
        self.specs.append(spec)
        return self.store.create(
            VideoTaskRecord(
                id=spec.id,
                protocol=spec.protocol,
                model=spec.model,
                prompt_preview=spec.prompt_preview,
                engine=spec.engine,
                duration=spec.duration,
                aspect_ratio=spec.aspect_ratio,
                resolution=spec.resolution,
                requested_size=spec.requested_size,
                log_id=spec.log_id,
                status="queued",
                created_at=1_700_000_000,
            )
        )


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.store = VideoTaskStore(tmp_path / "tasks.jsonl")
        self.manager = FakeTaskManager(self.store)
        api = FastAPI()

        def require_key(request: Request) -> None:
            auth = str(request.headers.get("authorization") or "")
            bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
            supplied = bearer or str(request.headers.get("x-api-key") or "")
            if supplied != "secret":
                raise HTTPException(status_code=401, detail="Invalid API key")

        api.include_router(
            build_openai_videos_router(
                task_manager=self.manager,
                task_store=self.store,
                require_service_api_key=require_key,
                public_generated_url=lambda request, filename: (
                    f"https://videos.example/generated/{filename}"
                ),
            )
        )
        self.client = TestClient(api)
        self.headers = {"Authorization": "Bearer secret"}

    def post_video(self, content_type: str, **fields):
        if content_type == "json":
            return self.client.post("/v1/videos", json=fields, headers=self.headers)
        files = {key: (None, str(value)) for key, value in fields.items()}
        return self.client.post("/v1/videos", files=files, headers=self.headers)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


@pytest.mark.parametrize("content_type", ["json", "multipart"])
def test_create_sora_video_maps_request_to_task(content_type: str, harness: Harness):
    response = harness.post_video(
        content_type,
        model="sora-2-pro",
        prompt="camera tracks a train",
        seconds="12",
        size="1792x1024",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("video_")
    assert payload["object"] == "video"
    assert payload["status"] == "queued"
    assert payload["seconds"] == "12"
    assert payload["size"] == "1792x1024"
    spec = harness.manager.specs[-1]
    assert (spec.duration, spec.aspect_ratio, spec.resolution) == (
        12,
        "16:9",
        "1080p",
    )
    assert spec.engine == "sora2-pro"
    assert spec.upstream_model == "openai:firefly:colligo:sora2-pro"
    assert spec.credit_model_id == "firefly-sora2-pro-12s-16x9"
    assert spec.result_url_prefix == "https://videos.example/generated/"


@pytest.mark.parametrize(
    "fields",
    [
        {"model": "sora-2", "prompt": "p", "seconds": 6},
        {"model": "sora-2", "prompt": "p", "size": "1792x1024"},
        {"model": "unknown", "prompt": "p"},
        {"model": "sora-2", "prompt": ""},
        {"model": "sora-2", "prompt": "p", "input_reference": "file"},
        {"model": "sora-2", "prompt": "p", "characters": [{"id": "c"}]},
    ],
)
def test_create_rejects_invalid_or_unsupported_fields(harness: Harness, fields):
    response = harness.client.post("/v1/videos", json=fields, headers=harness.headers)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_create_rejects_body_over_one_mib_before_json_parse(harness: Harness):
    response = harness.client.post(
        "/v1/videos",
        content=b"x" * (1024 * 1024 + 1),
        headers={**harness.headers, "content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_too_large"
    assert harness.manager.specs == []


def test_create_returns_429_when_task_capacity_is_full(harness: Harness):
    harness.manager.reject_capacity = True

    response = harness.client.post(
        "/v1/videos",
        json={"model": "sora-2", "prompt": "p"},
        headers=harness.headers,
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "queue_full"


def test_video_routes_require_service_key(harness: Harness):
    response = harness.client.post(
        "/v1/videos",
        json={"model": "sora-2", "prompt": "p"},
    )
    assert response.status_code == 401

    response = harness.client.post(
        "/v1/videos",
        json={"model": "sora-2", "prompt": "p"},
        headers={"X-API-Key": "secret"},
    )
    assert response.status_code == 200


def test_get_video_serializes_failed_task_error(harness: Harness):
    created = harness.post_video("json", model="sora-2", prompt="p").json()
    harness.store.update(
        created["id"],
        status="failed",
        progress=42,
        error_code="generation_failed",
        error_message="upstream failed",
        completed_at=1_700_000_100,
    )

    response = harness.client.get(
        f"/v1/videos/{created['id']}", headers=harness.headers
    )

    assert response.status_code == 200
    assert response.json()["error"] == {
        "code": "generation_failed",
        "message": "upstream failed",
    }
    assert response.json()["completed_at"] == 1_700_000_100


def test_content_returns_real_mime_and_rejects_nonterminal_states(
    harness: Harness,
    tmp_path: Path,
):
    created = harness.post_video("json", model="sora-2", prompt="p").json()
    task_id = created["id"]

    queued = harness.client.get(
        f"/v1/videos/{task_id}/content", headers=harness.headers
    )
    assert queued.status_code == 409

    video_path = tmp_path / "result.webm"
    video_path.write_bytes(b"webm-video")
    time_value = 1_700_000_100
    harness.store.update(
        task_id,
        status="completed",
        progress=100,
        result_path=str(video_path),
        result_mime="video/webm",
        completed_at=time_value,
    )
    completed = harness.client.get(
        f"/v1/videos/{task_id}/content", headers=harness.headers
    )
    assert time_value == 1_700_000_100
    assert completed.status_code == 200
    assert completed.headers["content-type"].startswith("video/webm")
    assert completed.content == b"webm-video"

    video_path.unlink()
    pruned = harness.client.get(
        f"/v1/videos/{task_id}/content", headers=harness.headers
    )
    assert pruned.status_code == 404


def test_get_and_content_hide_cross_protocol_tasks(harness: Harness):
    veo = replace(
        VideoTaskRecord(
            id="operation-id",
            protocol="openai",
            model="sora-2",
            prompt_preview="p",
            engine="sora2",
            duration=4,
            aspect_ratio="16:9",
            resolution="720p",
            requested_size="1280x720",
            log_id="log",
            created_at=1,
        ),
        protocol="veo",
        model="veo-3.1-generate-preview",
    )
    harness.store.create(veo)

    assert (
        harness.client.get("/v1/videos/operation-id", headers=harness.headers).status_code
        == 404
    )
    assert (
        harness.client.get(
            "/v1/videos/operation-id/content", headers=harness.headers
        ).status_code
        == 404
    )
