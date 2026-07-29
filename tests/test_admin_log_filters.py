from __future__ import annotations

from pathlib import Path
import sys
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routes.admin import build_admin_router
from core.stores import LiveRequestStore, RequestLogStore


class FakeConfigManager:
    def get(self, key: str, default=None):
        return default

    def get_all(self) -> dict:
        return {}


def make_client(log_store, live_log_store) -> TestClient:
    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=object(),
            config_manager=FakeConfigManager(),
            refresh_manager=object(),
            log_store=log_store,
            error_store=object(),
            live_log_store=live_log_store,
            require_admin_auth=lambda request: None,
            is_admin_authenticated=lambda request: True,
            apply_client_config=lambda: None,
            get_generated_storage_stats=lambda: {},
        )
    )
    return TestClient(api)


def seed_logs(tmp_path: Path) -> RequestLogStore:
    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    now = time.time()
    store.add_payload(
        {
            "id": "a",
            "ts": now,
            "status_code": 200,
            "preview_kind": "image",
            "model": "firefly-v3",
        }
    )
    store.add_payload(
        {
            "id": "b",
            "ts": now,
            "status_code": 200,
            "preview_kind": "video",
            "model": "veo-3",
        }
    )
    store.add_payload(
        {
            "id": "c",
            "ts": now,
            "status_code": 500,
            "preview_kind": "video",
            "model": "veo-3",
        }
    )
    return store


def test_list_logs_filters_by_model(tmp_path: Path):
    client = make_client(seed_logs(tmp_path), LiveRequestStore())

    payload = client.get("/api/v1/logs", params={"model": "veo-3"}).json()

    assert payload["model"] == "veo-3"
    assert payload["total"] == 2
    assert {row["id"] for row in payload["logs"]} == {"b", "c"}


def test_list_logs_without_model_returns_everything(tmp_path: Path):
    client = make_client(seed_logs(tmp_path), LiveRequestStore())

    payload = client.get("/api/v1/logs").json()

    assert payload["model"] == ""
    assert payload["total"] == 3


def test_log_models_endpoint_lists_distinct_models(tmp_path: Path):
    client = make_client(seed_logs(tmp_path), LiveRequestStore())

    assert client.get("/api/v1/logs/models").json() == {
        "models": ["firefly-v3", "veo-3"]
    }


def test_stats_and_running_logs_filter_by_model(tmp_path: Path):
    live_store = LiveRequestStore()
    live_store.upsert(
        "run-1", {"model": "veo-3", "task_status": "IN_PROGRESS", "ts": time.time()}
    )
    live_store.upsert(
        "run-2",
        {"model": "firefly-v3", "task_status": "IN_PROGRESS", "ts": time.time()},
    )
    client = make_client(seed_logs(tmp_path), live_store)

    stats = client.get(
        "/api/v1/logs/stats", params={"range": "today", "model": "veo-3"}
    ).json()

    assert stats["model"] == "veo-3"
    assert stats["total_requests"] == 2
    assert stats["failed_requests"] == 1
    assert stats["generated_videos"] == 1
    assert stats["generated_images"] == 0
    assert stats["in_progress_requests"] == 1

    running = client.get("/api/v1/logs/running", params={"model": "veo-3"}).json()

    assert [item["id"] for item in running["items"]] == ["run-1"]
