import json
from typing import Callable, Optional

import requests

from leonardo_refresher.config import RefresherConfig
from leonardo_refresher.service import (
    LoginRequiredError,
    RefreshFetchError,
    TokenPushError,
)


LEONARDO_HOME_URL = "https://app.leonardo.ai/"
_SESSION_FETCH_SCRIPT = """
async () => {
    const response = await fetch('/api/auth/get-session', {
        credentials: 'include',
        cache: 'no-store'
    });
    return {
        status: response.status,
        content_type: response.headers.get('content-type') || '',
        body: await response.text()
    };
}
"""


def _start_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


class PlaywrightSessionSource:
    def __init__(
        self,
        *,
        config: RefresherConfig,
        playwright_factory: Callable = _start_playwright,
    ):
        self._config = config
        self._playwright_factory = playwright_factory
        self._playwright = None
        self._context = None
        self._visible_page = None
        self._controller_page = None

    def open(self) -> None:
        if self._context is not None:
            return

        self._playwright = self._playwright_factory()
        # 容器内关闭 Chromium 内建沙箱：Docker+非 root+用户命名空间下开启会
        # "Chromium sandboxing failed!" 无法启动。隔离由容器兜底（seccomp
        # profile + 非 root pwuser + 独立网络命名空间）。
        launch_kwargs = {"headless": False, "chromium_sandbox": False}
        if self._config.proxy:  # 空＝直连，不传 proxy（proxy={"server":""} 非法）
            launch_kwargs["proxy"] = {"server": self._config.proxy}
        self._context = self._playwright.chromium.launch_persistent_context(
            self._config.profile_dir,
            **launch_kwargs,
        )
        self._visible_page = (
            self._context.pages[0]
            if self._context.pages
            else self._context.new_page()
        )
        self._visible_page.goto(
            LEONARDO_HOME_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        self._controller_page = self._context.new_page()
        self._controller_page.goto(
            LEONARDO_HOME_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        self._visible_page.bring_to_front()

    def fetch_token(self) -> str:
        if self._controller_page is None:
            try:
                self.open()
            except Exception as exc:
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
        try:
            result = self._controller_page.evaluate(_SESSION_FETCH_SCRIPT)
        except Exception as exc:
            if self._is_browser_control_error(exc):
                self._reset_browser()
                try:
                    self.open()
                    result = self._controller_page.evaluate(_SESSION_FETCH_SCRIPT)
                except Exception as recovery_exc:
                    self._reset_browser()
                    raise RefreshFetchError("browser_control") from recovery_exc
            else:
                kind = "proxy" if "proxy" in str(exc).lower() else "network"
                raise RefreshFetchError(kind) from exc

        if not isinstance(result, dict):
            raise RefreshFetchError("invalid_response")
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError) as exc:
            raise RefreshFetchError("invalid_response") from exc
        content_type = str(result.get("content_type") or "").lower()
        body = str(result.get("body") or "")

        if status in {403, 451}:
            raise RefreshFetchError("geo_embargo")
        if status == 401:
            raise LoginRequiredError()
        # 先判 >=400（如 CF/nginx 的 5xx HTML 网关页），再把 200 的 HTML 视为登录页；
        # 否则 5xx+text/html 会被误判成掉登录 → 误报"需重登"。
        if status >= 400:
            raise RefreshFetchError(f"http_{status}")
        if "text/html" in content_type:
            raise LoginRequiredError()

        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise RefreshFetchError("invalid_response") from exc
        if payload is None or not isinstance(payload, dict):
            raise LoginRequiredError()
        session = payload.get("session")
        if not isinstance(session, dict):
            raise LoginRequiredError()
        token = str(session.get("accessToken") or "").strip()
        if not token:
            raise LoginRequiredError()
        return token

    @staticmethod
    def _is_browser_control_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "target closed",
                "target page, context or browser has been closed",
                "browser has been closed",
                "browser closed",
                "connection closed",
            )
        )

    def _reset_browser(self) -> None:
        context = self._context
        playwright = self._playwright
        self._context = None
        self._playwright = None
        self._visible_page = None
        self._controller_page = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def close(self) -> None:
        self._reset_browser()


class Adobe2ApiTokenSink:
    def __init__(
        self,
        *,
        base_url: str,
        refresh_key: str,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ):
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._refresh_key = str(refresh_key or "")
        self._session = session_factory()
        self._session.trust_env = False

    def push(self, token: str, label: Optional[str]) -> dict:
        response = None
        try:
            response = self._session.post(
                f"{self._base_url}/api/v1/tokens/leonardo",
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                json={"token": token, "label": label},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            status_code = getattr(response, "status_code", None)
            kind = f"http_{status_code}" if status_code else "http_error"
            raise TokenPushError(kind) from exc
        except requests.RequestException as exc:
            raise TokenPushError("network") from exc
        except (TypeError, ValueError) as exc:
            raise TokenPushError("invalid_response") from exc

        if not isinstance(payload, dict):
            raise TokenPushError("invalid_response")
        return payload

    def close(self) -> None:
        self._session.close()
