import json
from pathlib import Path

import pytest

from core.credits_tracker import CreditsTracker, derive_cost_key
from core.models import MODEL_CATALOG, VIDEO_MODEL_CATALOG
from core.stores import RequestLogRecord


class FakeTokenManager:
    def __init__(self, used_by_token: dict[str, float | None]):
        self.used_by_token = dict(used_by_token)

    def get_by_id(self, token_id: str):
        if token_id not in self.used_by_token:
            return None
        return {"id": token_id, "credits_used": self.used_by_token[token_id]}


class FakeRefreshManager:
    def __init__(self, token_manager: FakeTokenManager, results: list[object]):
        self.token_manager = token_manager
        self.results = list(results)
        self.calls: list[tuple[str, bool]] = []

    def refresh_credits_for_token_id(self, token_id: str, handle_auth: bool = False):
        self.calls.append((token_id, handle_auth))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        used = float(result)
        self.token_manager.used_by_token[token_id] = used
        return {"token_id": token_id, "credits": {"used": used}}


class CaptureLogStore:
    def __init__(self):
        self.upserts: list[tuple[str, dict]] = []

    def upsert(self, log_id: str, payload: dict):
        self.upserts.append((log_id, dict(payload)))


def make_tracker(
    tmp_path: Path,
    *,
    used_by_token: dict[str, float | None] | None = None,
    refresh_results: list[object] | None = None,
    learned: dict[str, float] | None = None,
    queue_size: int = 200,
    start_worker: bool = False,
) -> tuple[CreditsTracker, FakeTokenManager, FakeRefreshManager, CaptureLogStore]:
    learned_path = tmp_path / "credit_costs_learned.json"
    if learned is not None:
        learned_path.write_text(json.dumps(learned), encoding="utf-8")
    tokens = FakeTokenManager(used_by_token or {"token-1": 100})
    refresh = FakeRefreshManager(tokens, refresh_results or [112])
    logs = CaptureLogStore()
    tracker = CreditsTracker(
        refresh_manager=refresh,
        token_manager=tokens,
        log_store=logs,
        learned_costs_path=learned_path,
        model_catalog=MODEL_CATALOG,
        video_model_catalog=VIDEO_MODEL_CATALOG,
        queue_size=queue_size,
        start_worker=start_worker,
    )
    return tracker, tokens, refresh, logs


def test_close_is_idempotent_after_worker_shutdown(tmp_path: Path):
    tracker, _tokens, _refresh, _logs = make_tracker(
        tmp_path,
        start_worker=True,
    )

    tracker.close()
    tracker.close()

    assert tracker._worker is None
    assert tracker._queue.unfinished_tasks == 0


@pytest.mark.parametrize(
    ("model_id", "output_resolution", "expected"),
    [
        ("firefly-nano-banana-pro", "4K", "nano-banana-pro:4K"),
        ("firefly-nano-banana2", "2K", "nano-banana-2:2K"),
        ("firefly-gpt-image", "4K", "gpt-image:4K"),
        ("gpt-image-2", "1K", "gpt-image:1K"),
        ("gemini-3-pro-image-preview", "2K", "gemini-3-pro-image:2K"),
        ("gemini-3.1-flash-image-preview", "1K", "gemini-3.1-flash-image:1K"),
        ("firefly-sora2-12s-16x9", None, "sora2:12s"),
        ("firefly-sora2-pro-8s-9x16", None, "sora2-pro:8s"),
        ("firefly-veo31-8s-16x9-1080p", None, "veo31-standard:8s:1080p"),
        ("firefly-kling-o3-15s-16x9", None, "kling-o3:15s:1080p"),
        ("firefly-kling3-10s-9x16", None, "kling3:10s:720p"),
        ("unknown-model", "4K", "unknown-model"),
    ],
)
def test_derive_cost_key(model_id: str, output_resolution: str | None, expected: str):
    assert (
        derive_cost_key(
            model_id,
            output_resolution,
            MODEL_CATALOG,
            VIDEO_MODEL_CATALOG,
        )
        == expected
    )


def test_loads_only_finite_non_negative_learned_costs(tmp_path: Path):
    learned_path = tmp_path / "credit_costs_learned.json"
    learned_path.write_text(
        json.dumps({"valid": 12, "negative": -1, "text": "bad"}),
        encoding="utf-8",
    )

    tracker, _tokens, _refresh, _logs = make_tracker(tmp_path)

    assert tracker.learned_costs == {"valid": 12.0}


def test_corrupt_learned_cost_file_starts_empty(tmp_path: Path):
    learned_path = tmp_path / "credit_costs_learned.json"
    learned_path.write_text("{broken", encoding="utf-8")

    tracker, _tokens, _refresh, _logs = make_tracker(tmp_path)

    assert tracker.learned_costs == {}


def log_payload(log_id: str, model: str = "firefly-gpt-image") -> dict:
    return {
        "id": log_id,
        "ts": 100,
        "status_code": 200,
        "task_status": "COMPLETED",
        "model": model,
        "credits_used": None,
        "credits_source": None,
    }


def complete_one(
    tracker: CreditsTracker,
    *,
    request_id: str = "request-1",
    log_id: str = "log-1",
    token_id: str = "token-1",
    model_id: str = "firefly-gpt-image",
    output_resolution: str | None = "2K",
) -> None:
    tracker.begin(token_id, request_id)
    tracker.complete(
        token_id=token_id,
        request_id=request_id,
        log_id=log_id,
        payload=log_payload(log_id, model_id),
        model_id=model_id,
        output_resolution=output_resolution,
    )


def test_clean_measurement_backfills_and_persists_learned_cost(tmp_path: Path):
    tracker, _tokens, refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": 100},
        refresh_results=[112],
    )

    complete_one(tracker)

    assert logs.upserts == []
    assert tracker.process_next() is True
    assert refresh.calls == [("token-1", True)]
    assert logs.upserts[0][0] == "log-1"
    assert logs.upserts[0][1]["credits_used"] == 12
    assert logs.upserts[0][1]["credits_source"] == "measured"
    assert tracker.learned_costs == {"gpt-image:2K": 12.0}
    persisted = json.loads(
        (tmp_path / "credit_costs_learned.json").read_text(encoding="utf-8")
    )
    assert persisted == {"gpt-image:2K": 12.0}


def test_missing_previous_snapshot_uses_learned_estimate(tmp_path: Path):
    tracker, _tokens, _refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": None},
        refresh_results=[40],
        learned={"gpt-image:2K": 8},
    )

    complete_one(tracker)
    tracker.process_next()

    assert logs.upserts[0][1]["credits_used"] == 8
    assert logs.upserts[0][1]["credits_source"] == "estimated"


def test_balance_failure_uses_learned_estimate(tmp_path: Path):
    tracker, _tokens, _refresh, logs = make_tracker(
        tmp_path,
        refresh_results=[RuntimeError("balance unavailable")],
        learned={"gpt-image:2K": 6},
    )

    complete_one(tracker)
    tracker.process_next()

    assert logs.upserts[0][1]["credits_used"] == 6
    assert logs.upserts[0][1]["credits_source"] == "estimated"


@pytest.mark.parametrize("new_used", [100, 99])
def test_non_positive_delta_uses_learned_estimate(tmp_path: Path, new_used: float):
    tracker, _tokens, _refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": 100},
        refresh_results=[new_used],
        learned={"gpt-image:2K": 5},
    )

    complete_one(tracker)
    tracker.process_next()

    assert logs.upserts[0][1]["credits_used"] == 5
    assert logs.upserts[0][1]["credits_source"] == "estimated"
    assert tracker.learned_costs == {"gpt-image:2K": 5.0}


def test_unknown_cost_without_measurement_backfills_unknown(tmp_path: Path):
    tracker, _tokens, _refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": None},
        refresh_results=[10],
    )

    complete_one(
        tracker,
        model_id="unknown-model",
        output_resolution=None,
    )
    tracker.process_next()

    assert logs.upserts[0][1]["credits_used"] is None
    assert logs.upserts[0][1]["credits_source"] is None


def test_overlapping_requests_for_one_token_are_both_estimated(tmp_path: Path):
    tracker, _tokens, _refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": 100},
        refresh_results=[114, 114],
        learned={"gpt-image:2K": 7},
    )
    tracker.begin("token-1", "request-1")
    tracker.begin("token-1", "request-2")

    tracker.complete(
        token_id="token-1",
        request_id="request-1",
        log_id="log-1",
        payload=log_payload("log-1"),
        model_id="firefly-gpt-image",
        output_resolution="2K",
    )
    tracker.complete(
        token_id="token-1",
        request_id="request-2",
        log_id="log-2",
        payload=log_payload("log-2"),
        model_id="firefly-gpt-image",
        output_resolution="2K",
    )

    assert tracker.process_next() is True
    assert tracker.process_next() is True
    assert [payload["credits_used"] for _, payload in logs.upserts] == [7, 7]
    assert [payload["credits_source"] for _, payload in logs.upserts] == [
        "estimated",
        "estimated",
    ]


def test_two_tokens_for_one_account_share_attribution_state(tmp_path: Path):
    tracker, _tokens, _refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": 100, "token-2": 100},
        refresh_results=[124, 124],
        learned={"gpt-image:2K": 7},
    )
    tracker.begin("token-1", "request-1", account_id="account-1")
    tracker.begin("token-2", "request-2", account_id="account-1")

    tracker.complete(
        token_id="token-1",
        account_id="account-1",
        request_id="request-1",
        log_id="log-1",
        payload=log_payload("log-1"),
        model_id="firefly-gpt-image",
        output_resolution="2K",
    )
    tracker.complete(
        token_id="token-2",
        account_id="account-1",
        request_id="request-2",
        log_id="log-2",
        payload=log_payload("log-2"),
        model_id="firefly-gpt-image",
        output_resolution="2K",
    )

    tracker.process_next()
    tracker.process_next()

    assert [payload["credits_used"] for _, payload in logs.upserts] == [7, 7]
    assert all(payload["credits_source"] == "estimated" for _, payload in logs.upserts)
    assert tracker.learned_costs == {"gpt-image:2K": 7.0}


def test_sequential_tokens_for_one_account_reuse_account_balance_snapshot(
    tmp_path: Path,
):
    tracker, _tokens, _refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": 100, "token-2": 100},
        refresh_results=[112, 124],
    )

    tracker.begin("token-1", "request-1", account_id="account-1")
    tracker.complete(
        token_id="token-1",
        account_id="account-1",
        request_id="request-1",
        log_id="log-1",
        payload=log_payload("log-1"),
        model_id="firefly-gpt-image",
        output_resolution="2K",
    )
    tracker.process_next()
    tracker.begin("token-2", "request-2", account_id="account-1")
    tracker.complete(
        token_id="token-2",
        account_id="account-1",
        request_id="request-2",
        log_id="log-2",
        payload=log_payload("log-2"),
        model_id="firefly-gpt-image",
        output_resolution="2K",
    )
    tracker.process_next()

    assert [payload["credits_used"] for _, payload in logs.upserts] == [12, 12]
    assert all(payload["credits_source"] == "measured" for _, payload in logs.upserts)


def test_queue_full_backfills_estimate_without_refreshing_in_request_path(
    tmp_path: Path,
):
    tracker, _tokens, refresh, logs = make_tracker(
        tmp_path,
        used_by_token={"token-1": 100, "token-2": 200},
        refresh_results=[112],
        learned={"gpt-image:2K": 9},
        queue_size=1,
    )
    complete_one(tracker, request_id="request-1", log_id="log-1", token_id="token-1")

    complete_one(tracker, request_id="request-2", log_id="log-2", token_id="token-2")

    assert refresh.calls == []
    assert logs.upserts == [
        (
            "log-2",
            {
                **log_payload("log-2"),
                "credits_used": 9.0,
                "credits_source": "estimated",
            },
        )
    ]


def test_failed_request_finishes_without_queuing_measurement(tmp_path: Path):
    tracker, _tokens, refresh, logs = make_tracker(tmp_path)
    tracker.begin("token-1", "request-1")

    tracker.finish("token-1", "request-1", completed=False)

    assert tracker.process_next() is False
    assert refresh.calls == []
    assert logs.upserts == []


def test_clear_after_enqueue_prevents_credit_backfill_from_restoring_log(
    tmp_path: Path,
):
    from core.stores import RequestLogStore

    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    payload = log_payload("log-1")
    store.add_payload(payload)
    tokens = FakeTokenManager({"token-1": 100})
    refresh = FakeRefreshManager(tokens, [112])
    tracker = CreditsTracker(
        refresh_manager=refresh,
        token_manager=tokens,
        log_store=store,
        learned_costs_path=tmp_path / "credit_costs_learned.json",
        model_catalog=MODEL_CATALOG,
        video_model_catalog=VIDEO_MODEL_CATALOG,
        start_worker=False,
    )
    complete_one(tracker)

    store.clear()
    tracker.process_next()

    assert store.list() == ([], 0)


def test_clear_between_log_write_and_enqueue_uses_write_generation(tmp_path: Path):
    from core.stores import RequestLogStore

    store = RequestLogStore(tmp_path / "request_logs.jsonl")
    tokens = FakeTokenManager({"token-1": 100})
    refresh = FakeRefreshManager(tokens, [112])
    tracker = CreditsTracker(
        refresh_manager=refresh,
        token_manager=tokens,
        log_store=store,
        learned_costs_path=tmp_path / "credit_costs_learned.json",
        model_catalog=MODEL_CATALOG,
        video_model_catalog=VIDEO_MODEL_CATALOG,
        start_worker=False,
    )
    tracker.begin("token-1", "request-1")
    payload = log_payload("log-1")
    write_generation = store.add_payload(payload)

    store.clear()
    tracker.complete(
        token_id="token-1",
        request_id="request-1",
        log_id="log-1",
        log_generation=write_generation,
        payload=payload,
        model_id="firefly-gpt-image",
        output_resolution="2K",
    )
    tracker.process_next()

    assert store.list() == ([], 0)


def test_request_log_record_exposes_credit_backfill_fields():
    record = RequestLogRecord(
        id="log-1",
        ts=1,
        method="POST",
        path="/v1/images/generations",
        status_code=200,
        duration_sec=1,
        operation="images.generations",
        credits_used=12,
        credits_source="measured",
    )

    assert record.credits_used == 12
    assert record.credits_source == "measured"
