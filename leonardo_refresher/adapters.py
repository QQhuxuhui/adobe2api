import json
from typing import Callable, Optional, Tuple

import requests

from leonardo_refresher.config import RefresherConfig
from leonardo_refresher.service import (
    CookieRequiredError,
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

# 反检测：Canva/Cloudflare Turnstile 靠 navigator.webdriver / --enable-automation
# 判机器人。此处仅用于承载已在本地(住宅 IP、真实浏览器)登录得到的 cookie，
# headless 下调 get-session 续期，不做交互登录；反检测降低续期请求被风控概率。
_ANTIDETECT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_ANTIDETECT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]
_ANTIDETECT_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.navigator.chrome={runtime:{}};"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
)


def _start_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


_BETTER_AUTH_ORDER = (
    "__Secure-better-auth.session_token",
    "__Secure-better-auth.session_data.0",
    "__Secure-better-auth.session_data.1",
)


def extract_session_cookie_string(cookies) -> str:
    """从浏览器上下文 cookie 列表拼出 better-auth 会话串（顺序固定，便于比较）。

    缺 session_token 视为无效（返回空串），避免把半截会话写回存储。
    """
    by_name = {}
    for item in cookies or []:
        name = str((item or {}).get("name") or "").strip()
        if name in _BETTER_AUTH_ORDER:
            by_name[name] = str(item.get("value") or "")
    if not by_name.get(_BETTER_AUTH_ORDER[0]):
        return ""
    return "; ".join(
        f"{name}={by_name[name]}" for name in _BETTER_AUTH_ORDER if name in by_name
    )


class Adobe2ApiCookieProvider:
    """从 adobe2api 拉取已上传的 Leonardo cookie（refresh key 鉴权）。

    返回 (cookie_str, fingerprint)；尚未上传返回 None；网络/HTTP 错误抛
    RefreshFetchError 交由上层归入 refresh_retrying。
    """

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

    def fetch(self) -> Optional[Tuple[str, str]]:
        try:
            resp = self._session.get(
                f"{self._base_url}/api/v1/tokens/leonardo/cookie",
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise RefreshFetchError("network") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RefreshFetchError(f"cookie_http_{resp.status_code}")
        try:
            data = resp.json()
        except (TypeError, ValueError) as exc:
            raise RefreshFetchError("invalid_response") from exc
        cookie = str((data or {}).get("cookie") or "").strip()
        fingerprint = str((data or {}).get("fingerprint") or "").strip()
        if not cookie or not fingerprint:
            return None
        return cookie, fingerprint

    def store(self, cookie_str: str) -> bool:
        """把浏览器里轮换后的会话 cookie 存回 adobe2api。

        better-auth 会轮换 session token；不回写的话，容器重启时会重新注入那份
        已作废的原始 cookie，导致 login_required、被迫人工重导账号。
        回写失败只记为 False，绝不影响本次 token 刷新。
        """
        cookie = str(cookie_str or "").strip()
        if not cookie:
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/api/v1/tokens/leonardo/cookie",
                json={"cookie": cookie},
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                timeout=15,
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响刷新主流程
            return False
        return int(getattr(resp, "status_code", 0)) < 400

    def close(self) -> None:
        self._session.close()


class PlaywrightSessionSource:
    def __init__(
        self,
        *,
        config: RefresherConfig,
        cookie_provider,
        playwright_factory: Callable = _start_playwright,
    ):
        self._config = config
        self._cookie_provider = cookie_provider
        self._playwright_factory = playwright_factory
        self._playwright = None
        self._context = None
        self._page = None
        self._loaded_fingerprint = None

    def open(self) -> None:
        if self._context is not None:
            return
        self._playwright = self._playwright_factory()
        # headless：不做交互登录（登录在本地完成、cookie 上传），容器内无 GUI。
        # chromium_sandbox=False：Docker+非 root 下开内建沙箱会 "sandboxing failed"。
        launch_kwargs = {
            "headless": True,
            "chromium_sandbox": False,
            "args": list(_ANTIDETECT_ARGS),
            "ignore_default_args": ["--enable-automation"],
            "user_agent": _ANTIDETECT_UA,
            "locale": "en-US",
            "viewport": {"width": 1280, "height": 800},
        }
        if self._config.proxy:  # 空＝直连（proxy={"server":""} 非法）
            launch_kwargs["proxy"] = {"server": self._config.proxy}
        self._context = self._playwright.chromium.launch_persistent_context(
            self._config.profile_dir,
            **launch_kwargs,
        )
        self._context.add_init_script(_ANTIDETECT_JS)
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else self._context.new_page()
        )

    def _apply_cookies(self, cookie_str: str) -> None:
        cookies = []
        for pair in str(cookie_str or "").split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookies.append(
                    {"name": name.strip(), "value": value.strip(), "url": LEONARDO_HOME_URL}
                )
        self._context.clear_cookies()
        if cookies:
            self._context.add_cookies(cookies)

    def _persist_rotated_cookie(self, loaded_cookie: str) -> None:
        """会话 cookie 被上游轮换后回写存储，避免重启时注入作废的旧 cookie。"""
        store = getattr(self._cookie_provider, "store", None)
        if not callable(store):
            return
        try:
            current = extract_session_cookie_string(self._context.cookies())
        except Exception:  # noqa: BLE001 - 读取失败不影响刷新
            return
        if not current or current == str(loaded_cookie or "").strip():
            return
        if store(current):
            # 存储已更新，指纹随之变化；置空以便下轮按新 cookie 重新注入
            self._loaded_fingerprint = None

    def fetch_token(self) -> str:
        if self._context is None:
            try:
                self.open()
            except Exception as exc:
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc

        provided = self._cookie_provider.fetch()
        if not provided:
            raise CookieRequiredError()
        cookie_str, fingerprint = provided

        try:
            if fingerprint != self._loaded_fingerprint:
                self._apply_cookies(cookie_str)
                self._page.goto(
                    LEONARDO_HOME_URL, wait_until="domcontentloaded", timeout=60000
                )
                self._loaded_fingerprint = fingerprint
            result = self._page.evaluate(_SESSION_FETCH_SCRIPT)
            self._persist_rotated_cookie(cookie_str)
        except Exception as exc:
            if self._is_browser_control_error(exc):
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
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
        # 先判 >=400（CF/nginx 5xx HTML 网关页），再把 200 的 HTML 视为登录页。
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
        self._page = None
        self._loaded_fingerprint = None
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
