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
    """多账号状态：每个账号(按 cookie 指纹)各记一份，snapshot 聚合出顶层 state。

    health.py 靠 snapshot()["state"] 判 503，故顶层字段保持兼容。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._accounts = {}      # fingerprint -> dict
        self._browser_ok = True
        self._global_error = None  # cookie_required / 拉列表失败等全局问题

    def set_browser_ok(self, ok: bool) -> None:
        with self._lock:
            self._browser_ok = bool(ok)

    def set_global_error(self, error_kind) -> None:
        with self._lock:
            self._global_error = error_kind

    def prune(self, present_fingerprints) -> None:
        keep = set(present_fingerprints or ())
        with self._lock:
            for fp in list(self._accounts):
                if fp not in keep:
                    self._accounts.pop(fp, None)

    def mark_account_healthy(self, fingerprint: str, *, now: int, exp: int) -> None:
        with self._lock:
            self._accounts[fingerprint] = {
                "state": "healthy",
                "session_state": "authenticated",
                "last_success_at": int(now),
                "current_token_exp": int(exp),
                "error_kind": None,
            }

    def mark_account_failure(
        self, fingerprint: str, *, state: str, session_state: str, error_kind: str
    ) -> None:
        with self._lock:
            acc = dict(self._accounts.get(fingerprint) or {})
            acc.update(
                {
                    "state": state,
                    "session_state": session_state,
                    "error_kind": error_kind,
                }
            )
            acc.setdefault("last_success_at", None)
            acc.setdefault("current_token_exp", None)
            self._accounts[fingerprint] = acc

    def snapshot(self) -> dict:
        with self._lock:
            accounts = {fp: dict(a) for fp, a in self._accounts.items()}
            browser_ok = self._browser_ok
            global_error = self._global_error
        healthy = [a for a in accounts.values() if a.get("state") == "healthy"]
        total = len(accounts)
        if not browser_ok:
            state = "browser_unavailable"
        elif healthy:
            state = "healthy"
        elif total == 0 or global_error == "cookie_required":
            state = "login_required"
        else:
            # 没有健康账号：反映最需要关注的账号状态
            states = {a.get("state") for a in accounts.values()}
            if "login_required" in states:
                state = "login_required"
            elif "push_failed" in states:
                state = "push_failed"
            else:
                state = "refresh_retrying"
        newest = max(
            healthy, key=lambda a: a.get("last_success_at") or 0, default=None
        )
        errored = next(
            (a.get("error_kind") for a in accounts.values() if a.get("error_kind")),
            None,
        )
        return {
            "state": state,
            "session_state": "authenticated" if healthy else "unknown",
            "healthy_accounts": len(healthy),
            "total_accounts": total,
            "last_success_at": (newest or {}).get("last_success_at"),
            "current_token_exp": (newest or {}).get("current_token_exp"),
            "last_error_kind": global_error or errored,
            "accounts": [
                {"fingerprint": fp[:12], **a} for fp, a in accounts.items()
            ],
        }


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
        # fingerprint -> 已知 token 过期时间；决定某账号是否需要再刷
        self._known = {}
        self._browser_control_this_pass = False

    def run_once(self) -> int:
        """遍历所有已导入 cookie：新账号 / 快过期的立即刷新并入池。返回下次轮询间隔。"""
        self._browser_control_this_pass = False
        self.state.set_browser_ok(True)  # 乐观，若本轮撞到 browser_control 再置回
        try:
            cookies = self.source.list_cookies()
        except RefreshFetchError as exc:
            self.state.set_global_error(exc.kind)
            return self.config.poll_interval_seconds
        except Exception:
            self.state.set_global_error("unexpected_fetch_error")
            return self.config.poll_interval_seconds

        if not cookies:
            self.state.set_global_error("cookie_required")
            self.state.prune(set())
            self._known.clear()
            return self.config.poll_interval_seconds

        self.state.set_global_error(None)
        present = {fp for _, fp in cookies}
        self.state.prune(present)
        for fp in list(self._known):
            if fp not in present:
                self._known.pop(fp, None)

        now = int(self.now())
        for cookie_str, fingerprint in cookies:
            known_exp = self._known.get(fingerprint)
            # 新账号，或距过期不足安全边界 → 需要刷新；否则跳过（不跑浏览器）
            if known_exp is not None and (known_exp - now) >= self.config.safety_margin_seconds:
                continue
            self._refresh_one(cookie_str, fingerprint)

        return self.config.poll_interval_seconds

    def _refresh_one(self, cookie_str: str, fingerprint: str) -> None:
        try:
            token = self.source.fetch_token_for(cookie_str, fingerprint)
        except CookieRequiredError:
            self.state.mark_account_failure(
                fingerprint, state="login_required",
                session_state="login_required", error_kind="cookie_required")
            return
        except LoginRequiredError:
            self.state.mark_account_failure(
                fingerprint, state="login_required",
                session_state="login_required", error_kind="login_required")
            return
        except RefreshFetchError as exc:
            if exc.kind == "browser_control":
                self._browser_control_this_pass = True
                self.state.set_browser_ok(False)
                self.state.mark_account_failure(
                    fingerprint, state="browser_unavailable",
                    session_state="unknown", error_kind="browser_control")
            else:
                self.state.mark_account_failure(
                    fingerprint, state="refresh_retrying",
                    session_state="unknown", error_kind=exc.kind)
            return
        except Exception:
            self.state.mark_account_failure(
                fingerprint, state="refresh_retrying",
                session_state="unknown", error_kind="unexpected_fetch_error")
            return

        try:
            claims = decode_id_token(token)
        except ValueError:
            self.state.mark_account_failure(
                fingerprint, state="login_required",
                session_state="login_required", error_kind="invalid_token")
            return

        exp = int(claims["exp"])
        if exp - int(self.now()) < self.config.safety_margin_seconds:
            self.state.mark_account_failure(
                fingerprint, state="refresh_retrying",
                session_state="authenticated", error_kind="stale_token")
            return

        try:
            self.sink.push(token, self.config.account_label)
        except TokenPushError as exc:
            self.state.mark_account_failure(
                fingerprint, state="push_failed",
                session_state="authenticated", error_kind=exc.kind)
            return
        except Exception:
            self.state.mark_account_failure(
                fingerprint, state="push_failed",
                session_state="authenticated", error_kind="unexpected_push_error")
            return

        self._known[fingerprint] = exp
        self.state.mark_account_healthy(fingerprint, now=int(self.now()), exp=exp)

    def run_forever(self, stop_event) -> None:
        browser_control_failures = 0
        while not stop_event.is_set():
            self.run_once()
            if self._browser_control_this_pass:
                browser_control_failures += 1
            else:
                browser_control_failures = 0
            if browser_control_failures >= MAX_BROWSER_CONTROL_FAILURES:
                raise BrowserControlUnavailableError(
                    "browser control remained unavailable"
                )
            if stop_event.wait(self.config.poll_interval_seconds):
                break
