from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Callable


TERMINAL_VIDEO_TASK_STATUSES = frozenset({"completed", "failed"})
ACTIVE_VIDEO_TASK_STATUSES = frozenset({"queued", "in_progress"})


class VideoTaskCapacityError(RuntimeError):
    pass


class VideoTaskStorageError(RuntimeError):
    pass


class VideoTaskExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        error_code: str = "generation_failed",
        status_code: int = 500,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = int(status_code)


@dataclass(frozen=True)
class VideoTaskSpec:
    id: str
    protocol: str
    model: str
    prompt: str
    prompt_preview: str
    engine: str
    upstream_model: str
    duration: int
    aspect_ratio: str
    resolution: str
    requested_size: str
    negative_prompt: str
    credit_model_id: str
    result_url_prefix: str
    log_id: str


@dataclass
class VideoTaskRecord:
    id: str
    protocol: str
    model: str
    prompt_preview: str
    engine: str
    duration: int
    aspect_ratio: str
    resolution: str
    requested_size: str
    log_id: str
    status: str = "queued"
    progress: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    result_path: str | None = None
    result_mime: str | None = None
    result_url: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None


@dataclass(frozen=True)
class VideoTaskOutcome:
    result_path: Path
    result_mime: str
    result_url: str


class VideoTaskStore:
    def __init__(self, file_path: Path, max_items: int = 500) -> None:
        self._file_path = Path(file_path)
        self._max_items = max(1, int(max_items or 500))
        self._lock = threading.Lock()
        self._items: dict[str, VideoTaskRecord] = {}
        self._append_since_compact = 0
        self._compact_interval = 200
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._file_path.touch()
        with self._lock:
            self._load_locked()
            self._fail_interrupted_locked()

    @staticmethod
    def _record_from_payload(payload: dict) -> VideoTaskRecord | None:
        if not isinstance(payload, dict):
            return None
        allowed = {field.name for field in fields(VideoTaskRecord)}
        values = {key: value for key, value in payload.items() if key in allowed}
        try:
            record = VideoTaskRecord(**values)
        except (TypeError, ValueError):
            return None
        if not str(record.id or "").strip():
            return None
        return record

    def _load_locked(self) -> None:
        items: dict[str, VideoTaskRecord] = {}
        try:
            lines = self._file_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise VideoTaskStorageError(str(exc)) from exc
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            record = self._record_from_payload(payload)
            if record is not None:
                items[record.id] = record
        self._items = items

    def _append_locked(self, record: VideoTaskRecord) -> None:
        try:
            with self._file_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        except OSError as exc:
            raise VideoTaskStorageError(str(exc)) from exc
        self._append_since_compact += 1

    def _compact_if_needed_locked(self) -> None:
        terminal_count = sum(
            record.status not in ACTIVE_VIDEO_TASK_STATUSES
            for record in self._items.values()
        )
        if (
            self._append_since_compact < self._compact_interval
            and terminal_count <= self._max_items
        ):
            return
        try:
            self._compact_locked()
        except VideoTaskStorageError:
            # The append already made the current state durable. Keep serving it
            # and retry maintenance compaction on the next write.
            return

    def _fail_interrupted_locked(self) -> None:
        now = time.time()
        interrupted = [
            record
            for record in self._items.values()
            if record.status in ACTIVE_VIDEO_TASK_STATUSES
        ]
        for record in interrupted:
            updated = replace(
                record,
                status="failed",
                error_code="service_restarted",
                error_message="Video task interrupted by service restart",
                completed_at=now,
            )
            self._append_locked(updated)
            self._items[updated.id] = updated
        self._compact_if_needed_locked()

    def create(self, record: VideoTaskRecord) -> VideoTaskRecord:
        item = replace(record)
        with self._lock:
            if item.id in self._items:
                raise VideoTaskStorageError(f"video task already exists: {item.id}")
            self._append_locked(item)
            self._items[item.id] = item
            self._compact_if_needed_locked()
        return replace(item)

    def update(self, task_id: str, **changes) -> VideoTaskRecord | None:
        allowed = {field.name for field in fields(VideoTaskRecord)} - {"id"}
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"unknown video task fields: {sorted(invalid)}")
        with self._lock:
            current = self._items.get(str(task_id or ""))
            if current is None:
                return None
            updated = replace(current, **changes)
            self._append_locked(updated)
            self._items[updated.id] = updated
            self._compact_if_needed_locked()
            return replace(updated)

    def get(self, task_id: str) -> VideoTaskRecord | None:
        with self._lock:
            record = self._items.get(str(task_id or ""))
            return replace(record) if record is not None else None

    def list(self) -> list[VideoTaskRecord]:
        with self._lock:
            ordered = sorted(
                self._items.values(),
                key=lambda item: (float(item.created_at or 0), item.id),
                reverse=True,
            )
            return [replace(record) for record in ordered]

    def _compact_locked(self) -> None:
        active = [
            record
            for record in self._items.values()
            if record.status in ACTIVE_VIDEO_TASK_STATUSES
        ]
        terminal = sorted(
            (
                record
                for record in self._items.values()
                if record.status not in ACTIVE_VIDEO_TASK_STATUSES
            ),
            key=lambda item: (float(item.created_at or 0), item.id),
            reverse=True,
        )[: self._max_items]
        kept = active + terminal
        kept_ids = {record.id for record in kept}
        temp_path = self._file_path.with_suffix(self._file_path.suffix + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                for record in sorted(
                    kept,
                    key=lambda item: (float(item.created_at or 0), item.id),
                ):
                    handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            temp_path.replace(self._file_path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise VideoTaskStorageError(str(exc)) from exc
        self._items = {
            task_id: record
            for task_id, record in self._items.items()
            if task_id in kept_ids
        }
        self._append_since_compact = 0

    def compact(self) -> None:
        with self._lock:
            self._compact_locked()


VideoTaskRunner = Callable[
    [VideoTaskSpec, Callable[[float], None]],
    VideoTaskOutcome,
]


class VideoTaskManager:
    def __init__(
        self,
        store: VideoTaskStore,
        runner: VideoTaskRunner,
        *,
        max_workers: int = 2,
        max_pending: int = 20,
        on_submitted: Callable[[VideoTaskSpec, VideoTaskRecord], None] | None = None,
    ) -> None:
        self.store = store
        self._runner = runner
        self._max_workers = max(1, int(max_workers or 1))
        self._max_pending = max(0, int(max_pending or 0))
        self._on_submitted = on_submitted
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="video-task",
        )
        self._admission = threading.BoundedSemaphore(
            self._max_workers + self._max_pending
        )
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._closed = False

    @staticmethod
    def _record_from_spec(spec: VideoTaskSpec) -> VideoTaskRecord:
        return VideoTaskRecord(
            id=spec.id,
            protocol=spec.protocol,
            model=spec.model,
            prompt_preview=spec.prompt_preview,
            engine=spec.engine,
            duration=int(spec.duration),
            aspect_ratio=spec.aspect_ratio,
            resolution=spec.resolution,
            requested_size=spec.requested_size,
            log_id=spec.log_id,
            created_at=time.time(),
        )

    def submit(self, spec: VideoTaskSpec) -> VideoTaskRecord:
        with self._lock:
            if self._closed:
                raise VideoTaskCapacityError("video task manager is closed")
        if not self._admission.acquire(blocking=False):
            raise VideoTaskCapacityError("video task queue is full")

        record = self._record_from_spec(spec)
        created: VideoTaskRecord | None = None
        submission_phase = "store"
        try:
            created = self.store.create(record)
            if self._on_submitted is not None:
                submission_phase = "submitted_hook"
                self._on_submitted(spec, created)
            submission_phase = "executor"
            future = self._executor.submit(self._run, spec)
        except Exception as exc:
            if created is not None:
                try:
                    self.store.update(
                        spec.id,
                        status="failed",
                        error_code="task_submission_failed",
                        error_message=str(exc)[:500],
                        completed_at=time.time(),
                    )
                except Exception:
                    pass
            self._admission.release()
            if submission_phase == "submitted_hook":
                raise VideoTaskStorageError(str(exc)) from exc
            if submission_phase == "executor" and isinstance(exc, RuntimeError):
                raise VideoTaskCapacityError("video task manager is closed") from exc
            raise

        with self._lock:
            self._futures[spec.id] = future
        future.add_done_callback(
            lambda completed, task_id=spec.id: self._future_finished(
                task_id, completed
            )
        )
        return created

    def _future_finished(self, task_id: str, future: Future) -> None:
        with self._lock:
            self._futures.pop(task_id, None)
        self._admission.release()

    def _run(self, spec: VideoTaskSpec) -> None:
        self.store.update(
            spec.id,
            status="in_progress",
            started_at=time.time(),
            progress=0.0,
        )

        def update_progress(value: float) -> None:
            try:
                normalized = max(0.0, min(float(value), 99.0))
            except (TypeError, ValueError):
                return
            self.store.update(spec.id, progress=normalized)

        try:
            outcome = self._runner(spec, update_progress)
            self.store.update(
                spec.id,
                status="completed",
                progress=100.0,
                result_path=str(outcome.result_path),
                result_mime=str(outcome.result_mime),
                result_url=str(outcome.result_url),
                completed_at=time.time(),
                error_code=None,
                error_message=None,
            )
        except Exception as exc:
            self.store.update(
                spec.id,
                status="failed",
                error_code=str(getattr(exc, "error_code", "") or "generation_failed"),
                error_message=str(exc)[:500],
                completed_at=time.time(),
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._futures.items())
        for task_id, future in pending:
            if future.cancel():
                self.store.update(
                    task_id,
                    status="failed",
                    error_code="service_shutdown",
                    error_message="Video task cancelled during service shutdown",
                    completed_at=time.time(),
                )
        self._executor.shutdown(wait=False, cancel_futures=True)


def build_video_task_runner(
    *,
    token_manager,
    client,
    credits_tracker,
    request_log_store,
    generated_dir: Path,
    generate_video,
    on_generated_file_written,
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    adobe_error_cls,
    logger,
) -> VideoTaskRunner:
    def log_payload(
        spec: VideoTaskSpec,
        *,
        started_at: float,
        status_code: int,
        task_status: str,
        token_meta: dict | None,
        token_attempt: int | None,
        preview_url: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> dict:
        meta = token_meta or {}
        is_success = task_status == "COMPLETED"
        return {
            "id": spec.log_id,
            "ts": time.time(),
            "method": "POST",
            "path": (
                "/v1/videos"
                if spec.protocol == "openai"
                else f"/v1beta/models/{spec.model}:predictLongRunning"
            ),
            "status_code": int(status_code),
            "duration_sec": int(max(0.0, time.time() - started_at)),
            "operation": (
                "videos.create"
                if spec.protocol == "openai"
                else "gemini.predictLongRunning"
            ),
            "preview_url": preview_url,
            "preview_kind": "video" if is_success and preview_url else None,
            "model": spec.model,
            "prompt_preview": spec.prompt_preview,
            "error": error,
            "error_code": error_code,
            "task_status": task_status,
            "task_progress": 100.0 if is_success else None,
            "upstream_job_id": None,
            "retry_after": None,
            "token_id": str(meta.get("token_id") or "") or None,
            "token_account_name": str(meta.get("token_account_name") or "")
            or None,
            "token_account_email": str(meta.get("token_account_email") or "")
            or None,
            "token_source": str(meta.get("token_source") or "") or None,
            "token_attempt": token_attempt,
            "credits_used": None,
            "credits_source": None,
        }

    def runner(
        spec: VideoTaskSpec,
        progress_callback: Callable[[float], None],
    ) -> VideoTaskOutcome:
        started_at = time.time()
        max_attempts = int(
            client.retry_max_attempts if getattr(client, "retry_enabled", False) else 1
        )
        max_attempts = max(1, max_attempts)
        last_error: Exception | None = None
        last_meta: dict = {}
        last_attempt: int | None = None
        tried_identities: set[str] = set()

        def token_identity(token: str, meta: dict) -> str:
            return str(meta.get("refresh_profile_id") or "").strip() or token

        for attempt in range(1, max_attempts + 1):
            token = ""
            token_meta: dict = {}
            fetch_attempts = 0
            while not token:
                fetch_attempts += 1
                candidate = str(
                    token_manager.get_available(
                        strategy=getattr(client, "token_rotation_strategy", None)
                    )
                    or ""
                ).strip()
                if not candidate:
                    break
                candidate_meta = token_manager.get_meta_by_value(candidate) or {}
                if token_identity(candidate, candidate_meta) not in tried_identities:
                    token = candidate
                    token_meta = candidate_meta
                    break
                if fetch_attempts >= max(1, len(tried_identities) + 1):
                    break
            if not token:
                if last_error is None:
                    last_error = VideoTaskExecutionError(
                        "No active tokens available in the pool",
                        "no_active_tokens",
                        503,
                    )
                break

            tried_identities.add(token_identity(token, token_meta))
            token_id = str(token_meta.get("token_id") or "").strip()
            account_id = str(token_meta.get("token_account_id") or "").strip()
            last_meta = token_meta
            last_attempt = attempt
            if token_id:
                credits_tracker.begin(
                    token_id,
                    spec.id,
                    account_id=account_id or None,
                )

            def upstream_progress(update: dict) -> None:
                if not isinstance(update, dict):
                    return
                value = update.get("task_progress")
                if value is not None:
                    progress_callback(value)

            try:
                generated = generate_video(
                    client=client,
                    token=token,
                    video_conf={
                        "engine": spec.engine,
                        "upstream_model": spec.upstream_model,
                        "resolution": spec.resolution,
                    },
                    prompt=spec.prompt,
                    aspect_ratio=spec.aspect_ratio,
                    duration=spec.duration,
                    generated_dir=generated_dir,
                    task_id=spec.id,
                    resolution=spec.resolution,
                    negative_prompt=spec.negative_prompt,
                    generate_audio=True,
                    source_image_ids=[],
                    entity_refs=None,
                    reference_mode="frame",
                    timeout=max(int(getattr(client, "generate_timeout", 600)), 600),
                    progress_cb=upstream_progress,
                    on_generated_file_written=on_generated_file_written,
                )
                token_manager.report_success(token)
                result_url = (
                    f"{spec.result_url_prefix.rstrip('/')}/{generated.path.name}"
                )
                payload = log_payload(
                    spec,
                    started_at=started_at,
                    status_code=200,
                    task_status="COMPLETED",
                    token_meta=token_meta,
                    token_attempt=attempt,
                    preview_url=result_url,
                )
                log_generation = request_log_store.upsert(spec.log_id, payload)
                if token_id:
                    credits_tracker.complete(
                        token_id=token_id,
                        account_id=account_id or None,
                        request_id=spec.id,
                        log_id=spec.log_id,
                        log_generation=log_generation,
                        payload=payload,
                        model_id=spec.credit_model_id,
                        output_resolution=spec.resolution,
                    )
                return VideoTaskOutcome(
                    result_path=generated.path,
                    result_mime=generated.mime_type,
                    result_url=result_url,
                )
            except quota_error_cls as exc:
                token_manager.report_exhausted(token)
                last_error = VideoTaskExecutionError(
                    str(exc), "quota_exceeded", 429
                )
                retryable = attempt < max_attempts
            except auth_error_cls as exc:
                handler = getattr(token_manager, "handle_auth_failure", None)
                if callable(handler):
                    handler(token)
                else:
                    reporter = getattr(token_manager, "report_invalid", None)
                    if callable(reporter):
                        reporter(token)
                last_error = VideoTaskExecutionError(
                    str(exc), "authentication_failed", 401
                )
                retryable = attempt < max_attempts
            except upstream_temp_error_cls as exc:
                last_error = exc
                should_retry = getattr(client, "should_retry_temporary_error", None)
                retryable = attempt < max_attempts and (
                    bool(should_retry(exc)) if callable(should_retry) else True
                )
            except adobe_error_cls as exc:
                last_error = exc
                retryable = False
            except Exception as exc:
                last_error = exc
                retryable = False

            if token_id:
                credits_tracker.finish(
                    token_id,
                    spec.id,
                    account_id=account_id or None,
                    completed=False,
                )
            if not retryable:
                break
            delay_fn = getattr(client, "_retry_delay_for_attempt", None)
            delay = float(delay_fn(attempt) if callable(delay_fn) else 0.0)
            if delay > 0:
                time.sleep(delay)

        final_error = last_error or VideoTaskExecutionError(
            "Video generation failed",
            "generation_failed",
        )
        status_code = int(getattr(final_error, "status_code", None) or 500)
        request_log_store.upsert(
            spec.log_id,
            log_payload(
                spec,
                started_at=started_at,
                status_code=status_code,
                task_status="FAILED",
                token_meta=last_meta,
                token_attempt=last_attempt,
                error=str(final_error)[:500],
                error_code=str(
                    getattr(final_error, "error_code", "")
                    or "generation_failed"
                ),
            ),
        )
        if logger is not None:
            logger.warning("video task failed id=%s: %s", spec.id, final_error)
        raise final_error

    return runner
