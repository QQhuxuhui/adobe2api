import dataclasses
import threading
import time
from pathlib import Path

import pytest

from core.video_tasks import (
    VideoTaskCapacityError,
    VideoTaskExecutionError,
    VideoTaskManager,
    VideoTaskOutcome,
    VideoTaskRecord,
    VideoTaskSpec,
    VideoTaskStorageError,
    VideoTaskStore,
    build_video_task_runner,
)
from core.video_generation import GeneratedVideoFile


def make_record(
    task_id: str,
    *,
    status: str = "queued",
    created_at: float | None = None,
) -> VideoTaskRecord:
    return VideoTaskRecord(
        id=task_id,
        protocol="openai",
        model="sora-2",
        prompt_preview="prompt",
        engine="sora2",
        duration=4,
        aspect_ratio="16:9",
        resolution="720p",
        requested_size="1280x720",
        log_id=f"log-{task_id}",
        status=status,
        created_at=float(created_at if created_at is not None else time.time()),
    )


def make_spec(task_id: str) -> VideoTaskSpec:
    return VideoTaskSpec(
        id=task_id,
        protocol="openai",
        model="sora-2",
        prompt="full prompt",
        prompt_preview="full prompt",
        engine="sora2",
        upstream_model="openai:firefly:colligo:sora2",
        duration=4,
        aspect_ratio="16:9",
        resolution="720p",
        requested_size="1280x720",
        negative_prompt="",
        credit_model_id="firefly-sora2-4s-16x9",
        result_url_prefix="https://example.test/generated/",
        log_id=f"log-{task_id}",
    )


def wait_for_status(
    store: VideoTaskStore,
    task_id: str,
    status: str,
    timeout: float = 2.0,
) -> VideoTaskRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get(task_id)
        if record is not None and record.status == status:
            return record
        time.sleep(0.01)
    record = store.get(task_id)
    raise AssertionError(
        f"task {task_id} did not reach {status}; current={getattr(record, 'status', None)}"
    )


def test_store_reloads_latest_state_and_fails_interrupted_tasks(tmp_path: Path):
    path = tmp_path / "video_tasks.jsonl"
    store = VideoTaskStore(path, max_items=3)
    store.create(make_record("queued", status="queued"))
    store.create(make_record("running", status="in_progress"))
    store.update("queued", progress=10)

    reloaded = VideoTaskStore(path, max_items=3)

    queued = reloaded.get("queued")
    running = reloaded.get("running")
    assert queued is not None and queued.status == "failed"
    assert running is not None and running.status == "failed"
    assert queued.error_code == "service_restarted"
    assert running.error_code == "service_restarted"
    assert queued.completed_at is not None
    assert running.completed_at is not None


def test_store_ignores_bad_json_and_compacts_by_task_id(tmp_path: Path):
    path = tmp_path / "video_tasks.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    store = VideoTaskStore(path, max_items=2)
    for index, task_id in enumerate(("a", "b", "c"), start=1):
        store.create(
            make_record(task_id, status="completed", created_at=float(index))
        )
        store.update(task_id, progress=100)

    store.compact()

    assert {row.id for row in store.list()} == {"b", "c"}
    persisted = path.read_text(encoding="utf-8").splitlines()
    assert len(persisted) == 2


def test_store_automatically_enforces_terminal_task_limit(tmp_path: Path):
    path = tmp_path / "video_tasks.jsonl"
    store = VideoTaskStore(path, max_items=2)

    for index, task_id in enumerate(("a", "b", "c"), start=1):
        store.create(
            make_record(task_id, status="completed", created_at=float(index))
        )

    assert {row.id for row in store.list()} == {"b", "c"}
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_automatic_compaction_failure_does_not_turn_durable_create_into_error(
    tmp_path: Path,
    monkeypatch,
):
    store = VideoTaskStore(tmp_path / "video_tasks.jsonl")
    store._compact_interval = 1

    def fail_compaction():
        raise VideoTaskStorageError("temporary compaction failure")

    monkeypatch.setattr(store, "_compact_locked", fail_compaction)

    created = store.create(make_record("durable"))

    assert created.id == "durable"
    assert store.get("durable") is not None
    reloaded = VideoTaskStore(tmp_path / "video_tasks.jsonl")
    assert reloaded.get("durable") is not None


def test_manager_completes_task_and_persists_outcome(tmp_path: Path):
    store = VideoTaskStore(tmp_path / "tasks.jsonl")

    def runner(spec, progress):
        progress(37)
        return VideoTaskOutcome(
            result_path=tmp_path / f"{spec.id}.mp4",
            result_mime="video/mp4",
            result_url=f"{spec.result_url_prefix}{spec.id}.mp4",
        )

    manager = VideoTaskManager(store, runner, max_workers=1, max_pending=0)
    try:
        created = manager.submit(make_spec("done"))
        assert created.status in {"queued", "in_progress"}
        completed = wait_for_status(store, "done", "completed")
        assert completed.progress == 100
        assert completed.result_path == str(tmp_path / "done.mp4")
        assert completed.result_mime == "video/mp4"
        assert completed.result_url == "https://example.test/generated/done.mp4"
    finally:
        manager.close()


class FailingOnceInProgressStore(VideoTaskStore):
    def __init__(self, path: Path):
        super().__init__(path)
        self.remaining_failures = 1

    def update(self, task_id: str, **changes):
        if self.remaining_failures and changes.get("status") == "in_progress":
            self.remaining_failures -= 1
            raise VideoTaskStorageError("transient state write failure")
        return super().update(task_id, **changes)


def test_manager_converts_in_progress_store_failure_to_terminal_failed(tmp_path: Path):
    store = FailingOnceInProgressStore(tmp_path / "tasks.jsonl")
    manager = VideoTaskManager(
        store,
        lambda spec, progress: VideoTaskOutcome(
            tmp_path / f"{spec.id}.mp4", "video/mp4", "url"
        ),
        max_workers=1,
        max_pending=0,
    )
    try:
        manager.submit(make_spec("state-write-fails"))
        failed = wait_for_status(store, "state-write-fails", "failed")
        assert failed.error_code == "task_state_persist_failed"
    finally:
        manager.close()


def test_manager_close_waits_for_running_worker(tmp_path: Path):
    started = threading.Event()
    release = threading.Event()
    store = VideoTaskStore(tmp_path / "tasks.jsonl")

    def runner(spec, progress):
        started.set()
        assert release.wait(timeout=2)
        return VideoTaskOutcome(tmp_path / f"{spec.id}.mp4", "video/mp4", "url")

    manager = VideoTaskManager(store, runner, max_workers=1, max_pending=0)
    manager.submit(make_spec("close-waits"))
    assert started.wait(timeout=1)
    release.set()
    manager.close()
    assert store.get("close-waits").status == "completed"


def test_manager_rejects_when_running_and_pending_capacity_is_full(
    tmp_path: Path,
):
    started = threading.Event()
    gate = threading.Event()
    store = VideoTaskStore(tmp_path / "tasks.jsonl")

    def runner(spec, progress):
        started.set()
        assert gate.wait(timeout=2)
        return VideoTaskOutcome(tmp_path / f"{spec.id}.mp4", "video/mp4", "url")

    manager = VideoTaskManager(store, runner, max_workers=1, max_pending=1)
    try:
        manager.submit(make_spec("one"))
        assert started.wait(timeout=1)
        manager.submit(make_spec("two"))

        with pytest.raises(VideoTaskCapacityError):
            manager.submit(make_spec("three"))

        assert store.get("three") is None
        gate.set()
        wait_for_status(store, "one", "completed")
        wait_for_status(store, "two", "completed")
    finally:
        gate.set()
        manager.close()


def test_manager_releases_slot_after_failure(tmp_path: Path):
    calls = 0
    store = VideoTaskStore(tmp_path / "tasks.jsonl")

    def runner(spec, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return VideoTaskOutcome(tmp_path / f"{spec.id}.mp4", "video/mp4", "url")

    manager = VideoTaskManager(store, runner, max_workers=1, max_pending=0)
    try:
        manager.submit(make_spec("one"))
        failed = wait_for_status(store, "one", "failed")
        assert failed.error_code == "generation_failed"
        assert failed.error_message == "boom"

        manager.submit(make_spec("two"))
        wait_for_status(store, "two", "completed")
    finally:
        manager.close()


def test_manager_calls_terminal_hook_for_worker_failure(tmp_path: Path):
    store = VideoTaskStore(tmp_path / "tasks.jsonl")
    terminal: list[VideoTaskRecord] = []

    def runner(spec, progress):
        raise RuntimeError("token pool unavailable")

    manager = VideoTaskManager(
        store,
        runner,
        max_workers=1,
        max_pending=0,
        on_terminal=lambda record: terminal.append(record),
    )
    try:
        manager.submit(make_spec("terminal-hook"))
        failed = wait_for_status(store, "terminal-hook", "failed")
        assert failed.error_code == "generation_failed"
        assert [record.id for record in terminal] == ["terminal-hook"]
    finally:
        manager.close()


def test_manager_close_marks_not_started_tasks_failed(tmp_path: Path):
    started = threading.Event()
    gate = threading.Event()
    store = VideoTaskStore(tmp_path / "tasks.jsonl")

    def runner(spec, progress):
        started.set()
        assert gate.wait(timeout=2)
        return VideoTaskOutcome(tmp_path / f"{spec.id}.mp4", "video/mp4", "url")

    manager = VideoTaskManager(store, runner, max_workers=1, max_pending=1)
    manager.submit(make_spec("running"))
    assert started.wait(timeout=1)
    manager.submit(make_spec("pending"))

    gate.set()
    manager.close()

    pending = wait_for_status(store, "pending", "failed")
    assert pending.error_code == "service_shutdown"
    wait_for_status(store, "running", "completed")


def test_manager_calls_submitted_hook_before_worker_starts(tmp_path: Path):
    store = VideoTaskStore(tmp_path / "tasks.jsonl")
    submitted: list[str] = []

    def runner(spec, progress):
        assert submitted == [spec.id]
        return VideoTaskOutcome(tmp_path / f"{spec.id}.mp4", "video/mp4", "url")

    manager = VideoTaskManager(
        store,
        runner,
        max_workers=1,
        max_pending=0,
        on_submitted=lambda spec, record: submitted.append(record.id),
    )
    try:
        manager.submit(make_spec("hooked"))
        wait_for_status(store, "hooked", "completed")
        assert submitted == ["hooked"]
    finally:
        manager.close()


def test_manager_reports_submitted_hook_failure_as_storage_error(tmp_path: Path):
    store = VideoTaskStore(tmp_path / "tasks.jsonl")

    def fail_log_write(spec, record):
        raise OSError("request log is read-only")

    manager = VideoTaskManager(
        store,
        lambda spec, progress: VideoTaskOutcome(
            tmp_path / f"{spec.id}.mp4", "video/mp4", "url"
        ),
        max_workers=1,
        max_pending=0,
        on_submitted=fail_log_write,
    )
    try:
        with pytest.raises(VideoTaskStorageError, match="request log is read-only"):
            manager.submit(make_spec("log-failed"))
        failed = store.get("log-failed")
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "task_submission_failed"
    finally:
        manager.close()


class FakeTokenManager:
    def __init__(self) -> None:
        self.reported_success: list[str] = []
        self.reported_exhausted: list[str] = []

    def get_available(self, strategy=None):
        return "token-value"

    def get_meta_by_value(self, token):
        return {
            "token_id": "token-id",
            "token_account_id": "account-id",
            "token_account_name": "Account",
            "token_account_email": "a@example.test",
            "token_source": "manual",
        }

    def report_success(self, token):
        self.reported_success.append(token)

    def report_exhausted(self, token):
        self.reported_exhausted.append(token)


class FakeCreditsTracker:
    def __init__(self) -> None:
        self.begin_calls: list[tuple] = []
        self.finish_calls: list[dict] = []
        self.complete_calls: list[dict] = []

    def begin(self, token_id, request_id, *, account_id=None):
        self.begin_calls.append((token_id, request_id, account_id))

    def finish(
        self,
        token_id,
        request_id,
        *,
        account_id=None,
        completed=False,
    ):
        self.finish_calls.append(
            {
                "token_id": token_id,
                "request_id": request_id,
                "account_id": account_id,
                "completed": completed,
            }
        )

    def complete(self, **kwargs):
        self.complete_calls.append(kwargs)


class FakeRequestLogStore:
    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}

    def upsert(self, log_id, payload):
        self.payloads[log_id] = {"id": log_id, **payload}
        return 7


class FakeWorkerClient:
    retry_enabled = False
    retry_max_attempts = 1
    token_rotation_strategy = "round_robin"
    generate_timeout = 600


class WorkerFailure(RuntimeError):
    pass


class WorkerQuotaError(RuntimeError):
    pass


class WorkerAuthError(RuntimeError):
    pass


class WorkerTemporaryError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def make_worker_runner(
    tmp_path: Path,
    *,
    generate_error: Exception | None = None,
    token_manager=None,
    client=None,
):
    token_manager = token_manager or FakeTokenManager()
    client = client or FakeWorkerClient()
    credits = FakeCreditsTracker()
    logs = FakeRequestLogStore()

    def generate_video(**kwargs):
        if generate_error is not None:
            raise generate_error
        path = tmp_path / f"{kwargs['task_id']}.mp4"
        path.write_bytes(b"video")
        return GeneratedVideoFile(path, "video/mp4", {"contentType": "video/mp4"})

    runner = build_video_task_runner(
        token_manager=token_manager,
        client=client,
        credits_tracker=credits,
        request_log_store=logs,
        generated_dir=tmp_path,
        generate_video=generate_video,
        on_generated_file_written=lambda path, old, new: None,
        quota_error_cls=WorkerQuotaError,
        auth_error_cls=WorkerAuthError,
        upstream_temp_error_cls=WorkerTemporaryError,
        adobe_error_cls=WorkerFailure,
        logger=None,
    )
    return runner, token_manager, credits, logs


def test_worker_writes_completed_log_and_submits_credit_measurement(tmp_path: Path):
    runner, token_manager, credits, logs = make_worker_runner(tmp_path)
    progress_values: list[float] = []

    outcome = runner(make_spec("video-ok"), progress_values.append)

    payload = logs.payloads["log-video-ok"]
    assert outcome.result_path == tmp_path / "video-ok.mp4"
    assert outcome.result_url == "https://example.test/generated/video-ok.mp4"
    assert payload["task_status"] == "COMPLETED"
    assert payload["preview_kind"] == "video"
    assert payload["model"] == "sora-2"
    assert payload["token_id"] == "token-id"
    assert token_manager.reported_success == ["token-value"]
    assert credits.begin_calls == [("token-id", "video-ok", "account-id")]
    assert credits.finish_calls == []
    assert credits.complete_calls[0]["model_id"] == "firefly-sora2-4s-16x9"
    assert credits.complete_calls[0]["log_generation"] == 7


def test_worker_failure_finishes_credit_without_completion(tmp_path: Path):
    runner, _token_manager, credits, logs = make_worker_runner(
        tmp_path,
        generate_error=WorkerFailure("boom"),
    )

    with pytest.raises(WorkerFailure, match="boom"):
        runner(make_spec("video-fail"), lambda value: None)

    assert credits.complete_calls == []
    assert credits.finish_calls == [
        {
            "token_id": "token-id",
            "request_id": "video-fail",
            "account_id": "account-id",
            "completed": False,
        }
    ]
    assert logs.payloads["log-video-fail"]["task_status"] == "FAILED"
    assert logs.payloads["log-video-fail"]["error"] == "boom"


def test_worker_does_not_retry_the_same_token_identity(tmp_path: Path):
    class RetryClient(FakeWorkerClient):
        retry_enabled = True
        retry_max_attempts = 3

        @staticmethod
        def _retry_delay_for_attempt(attempt):
            return 0

    calls = 0

    def generate_video(**kwargs):
        nonlocal calls
        calls += 1
        raise WorkerQuotaError("quota exhausted")

    token_manager = FakeTokenManager()
    credits = FakeCreditsTracker()
    logs = FakeRequestLogStore()
    runner = build_video_task_runner(
        token_manager=token_manager,
        client=RetryClient(),
        credits_tracker=credits,
        request_log_store=logs,
        generated_dir=tmp_path,
        generate_video=generate_video,
        on_generated_file_written=lambda path, old, new: None,
        quota_error_cls=WorkerQuotaError,
        auth_error_cls=WorkerAuthError,
        upstream_temp_error_cls=WorkerTemporaryError,
        adobe_error_cls=WorkerFailure,
        logger=None,
    )

    with pytest.raises(VideoTaskExecutionError) as error:
        runner(make_spec("quota"), lambda value: None)

    assert calls == 1
    assert error.value.error_code == "quota_exceeded"
    assert error.value.status_code == 429
    assert token_manager.reported_exhausted == ["token-value"]
    assert logs.payloads["log-quota"]["status_code"] == 429
    assert logs.payloads["log-quota"]["error_code"] == "quota_exceeded"


def test_worker_uploads_source_images_and_passes_ids(tmp_path: Path):
    """图生视频：spec 携带的原图必须先上传 Adobe 拿 id，再传给 generate_video。"""

    class UploadClient(FakeWorkerClient):
        def __init__(self) -> None:
            self.uploads = []

        def upload_image(
            self, token, image_bytes, mime_type="image/jpeg", deadline=None
        ):
            self.uploads.append((token, image_bytes, mime_type))
            return f"img-{len(self.uploads)}"

    client = UploadClient()
    captured: dict = {}
    token_manager = FakeTokenManager()

    def generate_video(**kwargs):
        captured.update(kwargs)
        path = tmp_path / f"{kwargs['task_id']}.mp4"
        path.write_bytes(b"video")
        return GeneratedVideoFile(path, "video/mp4", {"contentType": "video/mp4"})

    runner = build_video_task_runner(
        token_manager=token_manager,
        client=client,
        credits_tracker=FakeCreditsTracker(),
        request_log_store=FakeRequestLogStore(),
        generated_dir=tmp_path,
        generate_video=generate_video,
        on_generated_file_written=lambda path, old, new: None,
        quota_error_cls=WorkerQuotaError,
        auth_error_cls=WorkerAuthError,
        upstream_temp_error_cls=WorkerTemporaryError,
        adobe_error_cls=WorkerFailure,
        logger=None,
    )

    spec = dataclasses.replace(
        make_spec("video-img"),
        source_images=((b"png-bytes", "image/png"),),
    )
    outcome = runner(spec, lambda value: None)

    assert outcome.result_path == tmp_path / "video-img.mp4"
    assert client.uploads == [("token-value", b"png-bytes", "image/png")]
    assert captured["source_image_ids"] == ["img-1"]


def test_worker_without_source_images_passes_empty_ids(tmp_path: Path):
    captured: dict = {}

    def generate_video(**kwargs):
        captured.update(kwargs)
        path = tmp_path / f"{kwargs['task_id']}.mp4"
        path.write_bytes(b"video")
        return GeneratedVideoFile(path, "video/mp4", {"contentType": "video/mp4"})

    runner = build_video_task_runner(
        token_manager=FakeTokenManager(),
        client=FakeWorkerClient(),
        credits_tracker=FakeCreditsTracker(),
        request_log_store=FakeRequestLogStore(),
        generated_dir=tmp_path,
        generate_video=generate_video,
        on_generated_file_written=lambda path, old, new: None,
        quota_error_cls=WorkerQuotaError,
        auth_error_cls=WorkerAuthError,
        upstream_temp_error_cls=WorkerTemporaryError,
        adobe_error_cls=WorkerFailure,
        logger=None,
    )

    runner(make_spec("video-noimg"), lambda value: None)
    assert captured["source_image_ids"] == []
