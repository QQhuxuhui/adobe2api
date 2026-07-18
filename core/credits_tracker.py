from __future__ import annotations

import json
import logging
import math
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("adobe2api.credits")


@dataclass(frozen=True)
class _MeasurementTask:
    token_id: str
    attribution_key: str
    request_id: str
    log_id: str
    payload: dict
    cost_key: str
    state_version: int
    completions_at_finish: int
    overlapped: bool
    log_generation: int | None


def _normalized_resolution(value: object) -> str:
    resolution = str(value or "").strip().upper()
    return resolution if resolution in {"1K", "2K", "4K"} else ""


def _video_engine(model_id: str, config: dict) -> str:
    engine = str(config.get("engine") or "").strip().lower()
    if engine:
        return engine
    lowered = model_id.lower()
    if lowered.startswith("firefly-sora2-pro-"):
        return "sora2-pro"
    if lowered.startswith("firefly-sora2-"):
        return "sora2"
    if lowered.startswith("firefly-veo31-fast-"):
        return "veo31-fast"
    if lowered.startswith("firefly-veo31-"):
        return "veo31-standard"
    if lowered.startswith("firefly-kling-o3-"):
        return "kling-o3"
    if lowered.startswith("firefly-kling3-"):
        return "kling3"
    return ""


def derive_cost_key(
    model_id: str,
    output_resolution: Optional[str],
    model_catalog: dict[str, dict],
    video_model_catalog: dict[str, dict],
) -> str:
    normalized_model = str(model_id or "").strip()
    if not normalized_model:
        return "unknown"

    video_config = video_model_catalog.get(normalized_model)
    if isinstance(video_config, dict):
        engine = _video_engine(normalized_model, video_config)
        try:
            duration = int(video_config.get("duration") or 0)
        except Exception:
            duration = 0
        if engine and duration > 0:
            key = f"{engine}:{duration}s"
            resolution = str(video_config.get("resolution") or "").strip().lower()
            if resolution and engine not in {"sora2", "sora2-pro"}:
                key = f"{key}:{resolution}"
            return key
        return normalized_model

    image_config = model_catalog.get(normalized_model)
    family = ""
    resolution = _normalized_resolution(output_resolution)
    if isinstance(image_config, dict):
        upstream_id = str(image_config.get("upstream_model_id") or "").strip().lower()
        upstream_version = (
            str(image_config.get("upstream_model_version") or "").strip().lower()
        )
        if upstream_id == "gpt-image":
            family = "gpt-image"
        elif upstream_version == "nano-banana-2":
            family = "nano-banana-pro"
        elif upstream_version == "nano-banana-3":
            family = "nano-banana-2"
        if not resolution:
            resolution = _normalized_resolution(image_config.get("output_resolution"))
    elif normalized_model.startswith("gemini-") and "-image" in normalized_model:
        family = normalized_model.removesuffix("-preview")

    if family and resolution:
        return f"{family}:{resolution}"
    return normalized_model


class CreditsTracker:
    def __init__(
        self,
        *,
        refresh_manager: Any,
        token_manager: Any,
        log_store: Any,
        learned_costs_path: Path,
        model_catalog: dict[str, dict],
        video_model_catalog: dict[str, dict],
        queue_size: int = 200,
        start_worker: bool = True,
    ) -> None:
        self._refresh_manager = refresh_manager
        self._token_manager = token_manager
        self._log_store = log_store
        self._learned_costs_path = Path(learned_costs_path)
        self._model_catalog = model_catalog
        self._video_model_catalog = video_model_catalog
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._learned_costs = self._load_learned_costs()
        self._active_requests: dict[str, set[str]] = {}
        self._overlapped_requests: set[tuple[str, str]] = set()
        self._completions_since_snapshot: dict[str, int] = {}
        self._state_versions: dict[str, int] = {}
        self._balance_snapshots: dict[str, float] = {}
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._run,
                name="credits-tracker",
                daemon=True,
            )
            self._worker.start()

    @property
    def learned_costs(self) -> dict[str, float]:
        with self._lock:
            return dict(self._learned_costs)

    def _load_learned_costs(self) -> dict[str, float]:
        try:
            payload = json.loads(self._learned_costs_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}

        learned: dict[str, float] = {}
        for raw_key, raw_value in payload.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or value < 0:
                continue
            learned[key] = value
        return learned

    @staticmethod
    def _finite_number(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _attribution_key(token_id: str, account_id: Optional[str]) -> str:
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            return f"account:{normalized_account}"
        return f"token:{str(token_id or '').strip()}"

    def begin(
        self,
        token_id: str,
        request_id: str,
        *,
        account_id: Optional[str] = None,
    ) -> None:
        token_key = str(token_id or "").strip()
        request_key = str(request_id or "").strip()
        if not token_key or not request_key:
            return
        attribution_key = self._attribution_key(token_key, account_id)
        with self._lock:
            active = self._active_requests.setdefault(attribution_key, set())
            if request_key in active:
                return
            if active:
                self._overlapped_requests.update(
                    (attribution_key, active_request_id) for active_request_id in active
                )
                self._overlapped_requests.add((attribution_key, request_key))
            active.add(request_key)
            self._state_versions[attribution_key] = (
                self._state_versions.get(attribution_key, 0) + 1
            )

    def _finish(
        self,
        token_id: str,
        request_id: str,
        *,
        account_id: Optional[str],
        completed: bool,
    ) -> tuple[int, int, bool] | None:
        token_key = str(token_id or "").strip()
        request_key = str(request_id or "").strip()
        if not token_key or not request_key:
            return None
        attribution_key = self._attribution_key(token_key, account_id)
        with self._lock:
            active = self._active_requests.get(attribution_key)
            if not active or request_key not in active:
                return None
            active.remove(request_key)
            if not active:
                self._active_requests.pop(attribution_key, None)
            overlapped = (
                attribution_key,
                request_key,
            ) in self._overlapped_requests
            self._overlapped_requests.discard((attribution_key, request_key))
            if completed:
                self._completions_since_snapshot[attribution_key] = (
                    self._completions_since_snapshot.get(attribution_key, 0) + 1
                )
            self._state_versions[attribution_key] = (
                self._state_versions.get(attribution_key, 0) + 1
            )
            return (
                self._state_versions[attribution_key],
                self._completions_since_snapshot.get(attribution_key, 0),
                overlapped,
            )

    def finish(
        self,
        token_id: str,
        request_id: str,
        *,
        account_id: Optional[str] = None,
        completed: bool = False,
    ) -> None:
        self._finish(
            token_id,
            request_id,
            account_id=account_id,
            completed=completed,
        )

    def complete(
        self,
        *,
        token_id: str,
        account_id: Optional[str] = None,
        request_id: str,
        log_id: str,
        log_generation: int | None = None,
        payload: dict,
        model_id: str,
        output_resolution: Optional[str],
    ) -> None:
        attribution = self._finish(
            token_id,
            request_id,
            account_id=account_id,
            completed=True,
        )
        normalized_log_id = str(log_id or "").strip()
        if (
            attribution is None
            or not normalized_log_id
            or not isinstance(payload, dict)
        ):
            return
        state_version, completions_at_finish, overlapped = attribution
        attribution_key = self._attribution_key(token_id, account_id)
        if log_generation is None:
            try:
                log_generation = int(self._log_store.generation)
            except Exception:
                log_generation = None
        task = _MeasurementTask(
            token_id=str(token_id).strip(),
            attribution_key=attribution_key,
            request_id=str(request_id).strip(),
            log_id=normalized_log_id,
            payload=dict(payload),
            cost_key=derive_cost_key(
                model_id,
                output_resolution,
                self._model_catalog,
                self._video_model_catalog,
            ),
            state_version=state_version,
            completions_at_finish=completions_at_finish,
            overlapped=overlapped,
            log_generation=log_generation,
        )
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            self._backfill_estimate(task)

    def _backfill(
        self,
        task: _MeasurementTask,
        credits_used: float | None,
        credits_source: str | None,
    ) -> None:
        payload = dict(task.payload)
        payload["credits_used"] = credits_used
        payload["credits_source"] = credits_source if credits_used is not None else None
        try:
            conditional_upsert = getattr(
                self._log_store,
                "upsert_if_generation",
                None,
            )
            if task.log_generation is not None and callable(conditional_upsert):
                conditional_upsert(task.log_id, payload, task.log_generation)
            else:
                self._log_store.upsert(task.log_id, payload)
        except Exception:
            logger.warning("failed to backfill credits for log_id=%s", task.log_id)

    def _backfill_estimate(self, task: _MeasurementTask) -> None:
        with self._lock:
            learned_value = self._learned_costs.get(task.cost_key)
        self._backfill(
            task,
            learned_value,
            "estimated" if learned_value is not None else None,
        )

    def _save_learned_costs_locked(self) -> None:
        self._learned_costs_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._learned_costs_path.with_suffix(
            f"{self._learned_costs_path.suffix}.tmp"
        )
        temp_path.write_text(
            json.dumps(
                self._learned_costs, ensure_ascii=False, indent=2, sort_keys=True
            ),
            encoding="utf-8",
        )
        temp_path.replace(self._learned_costs_path)

    def _process_task(self, task: _MeasurementTask) -> None:
        token_info = self._token_manager.get_by_id(task.token_id)
        token_snapshot = self._finite_number(
            token_info.get("credits_used") if isinstance(token_info, dict) else None
        )

        with self._lock:
            previous_used = self._balance_snapshots.get(
                task.attribution_key,
                token_snapshot,
            )
            refresh_version = self._state_versions.get(task.attribution_key, 0)
            clean = (
                not task.overlapped
                and previous_used is not None
                and task.state_version == refresh_version
                and task.completions_at_finish == 1
                and self._completions_since_snapshot.get(task.attribution_key, 0) == 1
                and not self._active_requests.get(task.attribution_key)
            )

        try:
            result = self._refresh_manager.refresh_credits_for_token_id(
                task.token_id,
                handle_auth=True,
            )
            credits = result.get("credits") if isinstance(result, dict) else None
            new_used = self._finite_number(
                credits.get("used") if isinstance(credits, dict) else None
            )
            if new_used is None:
                raise ValueError("credits balance is missing used value")
        except Exception as exc:
            logger.warning(
                "credits refresh failed for token_id=%s: %s",
                task.token_id,
                str(exc)[:160],
            )
            self._backfill_estimate(task)
            return

        with self._lock:
            clean = (
                clean
                and self._state_versions.get(task.attribution_key, 0) == refresh_version
            )
            self._completions_since_snapshot[task.attribution_key] = 0
            self._balance_snapshots[task.attribution_key] = new_used

        delta = (
            round(new_used - previous_used, 6) if previous_used is not None else None
        )
        if clean and delta is not None and delta > 0:
            with self._lock:
                self._learned_costs[task.cost_key] = delta
                try:
                    self._save_learned_costs_locked()
                except Exception as exc:
                    logger.warning(
                        "failed to persist learned credit costs: %s", str(exc)[:160]
                    )
            self._backfill(task, delta, "measured")
            return

        self._backfill_estimate(task)

    def process_next(self) -> bool:
        try:
            task = self._queue.get_nowait()
        except queue.Empty:
            return False
        try:
            if isinstance(task, _MeasurementTask):
                self._process_task(task)
        finally:
            self._queue.task_done()
        return True

    def _run(self) -> None:
        while True:
            try:
                task = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                if task is None:
                    return
                if isinstance(task, _MeasurementTask):
                    self._process_task(task)
            except Exception:
                logger.exception("unexpected credits tracker worker failure")
            finally:
                self._queue.task_done()

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=2.0)
