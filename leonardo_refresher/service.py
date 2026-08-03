import base64
import json
import math
import threading
import time
from typing import Callable

from leonardo_refresher.config import RefresherConfig

MAX_BROWSER_CONTROL_FAILURES = 3


class BrowserControlUnavailableError(RuntimeError):
    pass


class LoginRequiredError(Exception):
    pass


class CookieRequiredError(LoginRequiredError):
    """尚未上传 Leonardo cookie（或已清空）。与掉登录同类(需人工上传),
    但 error_kind 用 cookie_required 区分"从未上传" vs "cookie 过期"。"""


class RefreshFetchError(Exception):
    def __init__(self, kind: str):
        self.kind = str(kind or "fetch_error")
        super().__init__(self.kind)


class TokenPushError(Exception):
    def __init__(self, kind: str):
        self.kind = str(kind or "push_error")
        super().__init__(self.kind)


def calculate_next_delay(
    *,
    exp: int,
    now: int,
    min_interval: int,
    refresh_interval: int,
    safety_margin: int,
) -> int:
    return max(
        int(min_interval),
        min(
            int(refresh_interval),
            int(exp) - int(now) - int(safety_margin),
        ),
    )


def decode_id_token(token: str) -> dict:
    parts = str(token or "").strip().split(".")
    if len(parts) != 3:
        raise ValueError("invalid token shape")
    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid token payload") from exc
    exp = data.get("exp") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("token_use") != "id"
        or not str(data.get("sub") or "").strip()
        or not isinstance(exp, (int, float))
        or isinstance(exp, bool)
        or not math.isfinite(exp)
    ):
        raise ValueError("invalid ID token claims")
    return data


class RuntimeState:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "state": "starting",
            "session_state": "unknown",
            "last_success_at": None,
            "current_token_exp": None,
            "consecutive_failures": 0,
            "last_error_kind": None,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def mark_failure(
        self,
        *,
        state: str,
        session_state: str,
        error_kind: str,
    ) -> None:
        with self._lock:
            self._data["state"] = state
            self._data["session_state"] = session_state
            self._data["consecutive_failures"] += 1
            self._data["last_error_kind"] = error_kind

    def mark_healthy(self, *, now: int, exp: int) -> None:
        with self._lock:
            self._data.update(
                {
                    "state": "healthy",
                    "session_state": "authenticated",
                    "last_success_at": int(now),
                    "current_token_exp": int(exp),
                    "consecutive_failures": 0,
                    "last_error_kind": None,
                }
            )


class RefresherService:
    def __init__(
        self,
        *,
        source,
        sink,
        state: RuntimeState,
        config: RefresherConfig,
        now: Callable[[], float] = time.time,
    ):
        self.source = source
        self.sink = sink
        self.state = state
        self.config = config
        self.now = now

    def run_once(self) -> int:
        try:
            token = self.source.fetch_token()
        except CookieRequiredError:
            self.state.mark_failure(
                state="login_required",
                session_state="login_required",
                error_kind="cookie_required",
            )
            return self.config.min_interval_seconds
        except LoginRequiredError:
            self.state.mark_failure(
                state="login_required",
                session_state="login_required",
                error_kind="login_required",
            )
            return self.config.min_interval_seconds
        except RefreshFetchError as exc:
            self.state.mark_failure(
                state=(
                    "browser_unavailable"
                    if exc.kind == "browser_control"
                    else "refresh_retrying"
                ),
                session_state="unknown",
                error_kind=exc.kind,
            )
            return self.config.min_interval_seconds
        except Exception:
            self.state.mark_failure(
                state="refresh_retrying",
                session_state="unknown",
                error_kind="unexpected_fetch_error",
            )
            return self.config.min_interval_seconds

        try:
            claims = decode_id_token(token)
        except ValueError:
            self.state.mark_failure(
                state="login_required",
                session_state="login_required",
                error_kind="invalid_token",
            )
            return self.config.min_interval_seconds

        exp = int(claims["exp"])
        observed_at = int(self.now())
        if exp - observed_at < self.config.safety_margin_seconds:
            self.state.mark_failure(
                state="refresh_retrying",
                session_state="authenticated",
                error_kind="stale_token",
            )
            return self.config.min_interval_seconds

        try:
            self.sink.push(token, self.config.account_label)
        except TokenPushError as exc:
            self.state.mark_failure(
                state="push_failed",
                session_state="authenticated",
                error_kind=exc.kind,
            )
            return self.config.min_interval_seconds
        except Exception:
            self.state.mark_failure(
                state="push_failed",
                session_state="authenticated",
                error_kind="unexpected_push_error",
            )
            return self.config.min_interval_seconds

        completed_at = int(self.now())
        self.state.mark_healthy(now=completed_at, exp=exp)
        return calculate_next_delay(
            exp=exp,
            now=completed_at,
            min_interval=self.config.min_interval_seconds,
            refresh_interval=self.config.refresh_interval_seconds,
            safety_margin=self.config.safety_margin_seconds,
        )

    def run_forever(self, stop_event) -> None:
        browser_control_failures = 0
        while not stop_event.is_set():
            delay = self.run_once()
            if self.state.snapshot()["state"] == "browser_unavailable":
                browser_control_failures += 1
            else:
                browser_control_failures = 0
            if browser_control_failures >= MAX_BROWSER_CONTROL_FAILURES:
                raise BrowserControlUnavailableError(
                    "browser control remained unavailable"
                )
            if stop_event.wait(delay):
                break
