from pathlib import Path

from core.stores import RequestLogStore


def make_payload(
    item_id: str,
    *,
    ts: float,
    status_code: int = 200,
    preview_kind: str = "image",
    credits_used: float | None = None,
    model: str | None = None,
) -> dict:
    return {
        "id": item_id,
        "ts": ts,
        "method": "POST",
        "path": "/v1/images/generations",
        "status_code": status_code,
        "duration_sec": 1,
        "operation": "images.generations",
        "preview_kind": preview_kind,
        "credits_used": credits_used,
        "model": model,
    }


def test_list_uses_latest_payload_for_each_log_id(tmp_path: Path):
    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    store.add_payload(make_payload("a", ts=10, credits_used=None))
    store.add_payload(make_payload("b", ts=20, preview_kind="video"))
    store.upsert("a", make_payload("a", ts=10, credits_used=12))

    rows, total = store.list(limit=20, page=1)

    assert total == 2
    assert [row["id"] for row in rows] == ["b", "a"]
    assert next(row for row in rows if row["id"] == "a")["credits_used"] == 12


def test_list_paginates_unique_logs_in_timestamp_order(tmp_path: Path):
    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    store.add_payload(make_payload("a", ts=10))
    store.add_payload(make_payload("b", ts=30))
    store.add_payload(make_payload("c", ts=20))
    store.upsert("a", make_payload("a", ts=10, credits_used=4))

    first_page, first_total = store.list(limit=2, page=1)
    second_page, second_total = store.list(limit=2, page=2)

    assert first_total == second_total == 3
    assert [row["id"] for row in first_page] == ["b", "c"]
    assert [row["id"] for row in second_page] == ["a"]
    assert second_page[0]["credits_used"] == 4


def test_stats_counts_only_the_latest_payload_for_each_log_id(tmp_path: Path):
    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    store.add_payload(make_payload("a", ts=10, status_code=500))
    store.add_payload(make_payload("b", ts=20, preview_kind="video"))
    store.upsert("a", make_payload("a", ts=10, status_code=200))

    stats = store.stats()

    assert stats["total_requests"] == 2
    assert stats["failed_requests"] == 0
    assert stats["generated_images"] == 1
    assert stats["generated_videos"] == 1


def test_list_filters_and_paginates_by_model(tmp_path: Path):
    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    store.add_payload(make_payload("a", ts=10, model="firefly-v3"))
    store.add_payload(make_payload("b", ts=20, model="veo-3"))
    store.add_payload(make_payload("c", ts=30, model="Firefly-V3"))
    store.add_payload(make_payload("d", ts=40))

    rows, total = store.list(limit=20, page=1, model="firefly-v3")

    assert total == 2
    assert [row["id"] for row in rows] == ["c", "a"]

    second_page, second_total = store.list(limit=1, page=2, model="firefly-v3")

    assert second_total == 2
    assert [row["id"] for row in second_page] == ["a"]

    all_rows, all_total = store.list(limit=20, page=1, model="  ")

    assert all_total == 4
    assert len(all_rows) == 4


def test_stats_and_models_respect_model_filter(tmp_path: Path):
    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    store.add_payload(make_payload("a", ts=10, model="firefly-v3"))
    store.add_payload(
        make_payload("b", ts=20, model="veo-3", preview_kind="video")
    )
    store.add_payload(
        make_payload("c", ts=30, model="veo-3", status_code=500)
    )
    store.add_payload(make_payload("d", ts=40))

    stats = store.stats(model="veo-3")

    assert stats["total_requests"] == 2
    assert stats["failed_requests"] == 1
    assert stats["generated_images"] == 0
    assert stats["generated_videos"] == 1
    assert store.models() == ["firefly-v3", "veo-3"]


def test_truncation_retains_max_items_unique_logs_after_backfills(tmp_path: Path):
    log_path = tmp_path / "request_logs.jsonl"
    store = RequestLogStore(log_path, max_items=20)

    for index in range(100):
        item_id = f"log-{index}"
        store.add_payload(make_payload(item_id, ts=index))
        store.upsert(
            item_id,
            make_payload(item_id, ts=index, credits_used=index + 1),
        )

    rows, total = store.list(limit=100, page=1)

    assert total == 20
    assert len(rows) == 20
    assert [row["id"] for row in rows] == [
        f"log-{index}" for index in range(99, 79, -1)
    ]
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 20
