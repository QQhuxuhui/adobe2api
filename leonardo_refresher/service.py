import base64
import json
import math
import threading
import time
from typing import Callable

from leonardo_refresher.config import RefresherConfig

MAX_BROWSER_CONTROL_FAILURES = 3
# 失效账号(掉登录/坏 token)的最短重试间隔：避免死号每 15s 各自硬刷一次浏览器，
# 既降负荷，也减少机房 IP 高频请求进一步触发上游风控。瞬时错误(网络/代理/浏览器控制)
# 不退避，仍每轮重试。
FAILURE_BACKOFF_SECONDS = 120

# 登录账号指纹前缀。必须与 adapters.LOGIN_MARKER 保持一致；此处本地定义而非 import，
# 因为 adapters 反过来 import service（循环 import）。
_LOGIN_MARKER = "__login__"


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
        # cid -> 最早可再次尝试的时间戳；失效账号退避，避免每 15s 猛刷
        self._retry_after = {}
        # 登录账号 cid -> 上次见到的 fingerprint(带 credential_rev)；rev 变→立即重验
        self._login_fp = {}
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
            self._retry_after.clear()
            return self.config.poll_interval_seconds

        self.state.set_global_error(None)
        present = {cid for cid, _, _ in cookies}
        self.state.prune(present)
        drop_context = getattr(self.source, "drop_context", None)
        # 缺席账号(cookie 或 login)：回收其浏览器 context，再清各缓存。
        for cid in set(self._known) | set(self._retry_after) | set(self._login_fp):
            if cid not in present and callable(drop_context):
                drop_context(cid)
        for cid in list(self._known):
            if cid not in present:
                self._known.pop(cid, None)
        for cid in list(self._retry_after):
            if cid not in present:
                self._retry_after.pop(cid, None)
        for cid in list(self._login_fp):
            if cid not in present:
                self._login_fp.pop(cid, None)

        now = int(self.now())
        for cid, cookie_str, fingerprint in cookies:
            # 仅登录账号：fingerprint 带 credential_rev，rev 变(或首见)→ 立即重验。
            # cookie 账号指纹变化＝正常轮换，不走此逻辑(清缓存会导致无谓重刷)。
            if fingerprint.startswith(_LOGIN_MARKER):
                prev_fp = self._login_fp.get(cid)
                if prev_fp is not None and prev_fp != fingerprint:
                    # credential_rev 变：作废旧 token/退避与旧 context，强制立即重登+重验。
                    self._known.pop(cid, None)
                    self._retry_after.pop(cid, None)
                    if callable(drop_context):
                        drop_context(cid)
                self._login_fp[cid] = fingerprint
                # 首见(prev_fp is None)无需清缓存：_known 本就无此 cid，下方 gate 自然放行重验。
            known_exp = self._known.get(cid)
            # 已健康且距过期还足够 → 跳过（不跑浏览器）
            if known_exp is not None and (known_exp - now) >= self.config.safety_margin_seconds:
                continue
            # 失效账号退避：掉登录/坏 token 的账号在退避期内不重试，避免每 15s 猛刷
            retry_at = self._retry_after.get(cid)
            if retry_at is not None and now < retry_at:
                continue
            self._refresh_one(cid, cookie_str, fingerprint)

        return self.config.poll_interval_seconds

    def _refresh_one(self, cid: str, cookie_str: str, fingerprint: str) -> None:
        try:
            token = self.source.fetch_token_for(cid, cookie_str, fingerprint)
        except CookieRequiredError:
            self._retry_after[cid] = int(self.now()) + FAILURE_BACKOFF_SECONDS
            self.state.mark_account_failure(
                cid, state="login_required",
                session_state="login_required", error_kind="cookie_required")
            return
        except LoginRequiredError:
            self._retry_after[cid] = int(self.now()) + FAILURE_BACKOFF_SECONDS
            self.state.mark_account_failure(
                cid, state="login_required",
                session_state="login_required", error_kind="login_required")
            return
        except RefreshFetchError as exc:
            if exc.kind == "browser_control":
                self._browser_control_this_pass = True
                self.state.set_browser_ok(False)
                self.state.mark_account_failure(
                    cid, state="browser_unavailable",
                    session_state="unknown", error_kind="browser_control")
            else:
                self.state.mark_account_failure(
                    cid, state="refresh_retrying",
                    session_state="unknown", error_kind=exc.kind)
            return
        except Exception:
            self.state.mark_account_failure(
                cid, state="refresh_retrying",
                session_state="unknown", error_kind="unexpected_fetch_error")
            return

        try:
            claims = decode_id_token(token)
        except ValueError:
            self._retry_after[cid] = int(self.now()) + FAILURE_BACKOFF_SECONDS
            self.state.mark_account_failure(
                cid, state="login_required",
                session_state="login_required", error_kind="invalid_token")
            return

        exp = int(claims["exp"])
        if exp - int(self.now()) < self.config.safety_margin_seconds:
            self.state.mark_account_failure(
                cid, state="refresh_retrying",
                session_state="authenticated", error_kind="stale_token")
            return

        try:
            self.sink.push(token, self.config.account_label)
        except TokenPushError as exc:
            self.state.mark_account_failure(
                cid, state="push_failed",
                session_state="authenticated", error_kind=exc.kind)
            return
        except Exception:
            self.state.mark_account_failure(
                cid, state="push_failed",
                session_state="authenticated", error_kind="unexpected_push_error")
            return

        self._known[cid] = exp
        self._retry_after.pop(cid, None)
        self.state.mark_account_healthy(cid, now=int(self.now()), exp=exp)

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
