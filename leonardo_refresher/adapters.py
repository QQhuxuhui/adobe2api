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

    def fetch_all(self):
        """多账号：拉取全部已导入 cookie，返回 [(id, cookie, fingerprint), ...]。空＝没导入。

        id 是稳定标识（轮换改指纹但 id 不变），refresher 据此维护每账号独立浏览器上下文。
        """
        try:
            resp = self._session.get(
                f"{self._base_url}/api/v1/tokens/leonardo/cookies",
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise RefreshFetchError("network") from exc
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise RefreshFetchError(f"cookie_http_{resp.status_code}")
        try:
            data = resp.json()
        except (TypeError, ValueError) as exc:
            raise RefreshFetchError("invalid_response") from exc
        out = []
        for item in (data or {}).get("cookies") or []:
            cid = str((item or {}).get("id") or "").strip()
            cookie = str((item or {}).get("cookie") or "").strip()
            fingerprint = str((item or {}).get("fingerprint") or "").strip()
            if cid and cookie and fingerprint:
                out.append((cid, cookie, fingerprint))
        return out

    def store(self, cookie_str: str, cookie_id: Optional[str] = None):
        """回写轮换后的 cookie。带 cookie_id 则按 id 就地更新那条。

        成功返回新指纹（str），失败返回 None。refresher 据返回的新指纹判定「已同步」，
        下一轮就复用上下文里的活 cookie、不再从存储重新注入。
        """
        cookie = str(cookie_str or "").strip()
        if not cookie:
            return None
        payload = {"cookie": cookie}
        if cookie_id:
            payload["cookie_id"] = str(cookie_id)
        try:
            resp = self._session.post(
                f"{self._base_url}/api/v1/tokens/leonardo/cookie",
                json=payload,
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                timeout=15,
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响刷新主流程
            return None
        if int(getattr(resp, "status_code", 0)) >= 400:
            return None
        try:
            return str((resp.json() or {}).get("fingerprint") or "")
        except Exception:  # noqa: BLE001
            return ""

    def close(self) -> None:
        self._session.close()


class PlaywrightSessionSource:
    """每个账号一个独立浏览器上下文（按稳定 id 索引）。

    关键：上下文一旦建立就保住该账号轮换后的活 cookie（含 Cloudflare 放行 cookie），
    后续 get-session 直接用上下文里的活值，**不再从存储重新注入**——这就是单账号版本
    健壮而旧的多账号版本（每轮 clear+重注入共享上下文）会把会话链打断的根因。
    只有用户重导了新 cookie（指纹与已同步值不同）才重新注入。回写只为重启后恢复。
    """

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
        self._browser = None
        # cookie_id -> {"context": ctx, "fp": 已同步的指纹}
        self._accounts = {}

    def open(self) -> None:
        if self._browser is not None:
            return
        self._playwright = self._playwright_factory()
        launch_kwargs = {
            "headless": True,
            "chromium_sandbox": False,
            "args": list(_ANTIDETECT_ARGS),
            "ignore_default_args": ["--enable-automation"],
        }
        if self._config.proxy:  # 空＝直连（proxy={"server":""} 非法）
            launch_kwargs["proxy"] = {"server": self._config.proxy}
        self._browser = self._playwright.chromium.launch(**launch_kwargs)

    def _new_context(self):
        ctx = self._browser.new_context(
            user_agent=_ANTIDETECT_UA,
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        ctx.add_init_script(_ANTIDETECT_JS)
        return ctx

    @staticmethod
    def _apply_cookies_to(ctx, cookie_str: str) -> None:
        cookies = []
        for pair in str(cookie_str or "").split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookies.append(
                    {"name": name.strip(), "value": value.strip(), "url": LEONARDO_HOME_URL}
                )
        ctx.clear_cookies()
        if cookies:
            ctx.add_cookies(cookies)

    def list_cookies(self):
        """多账号：返回全部已导入 cookie [(id, cookie, fingerprint), ...]。空＝没导入。"""
        fetch_all = getattr(self._cookie_provider, "fetch_all", None)
        return fetch_all() if callable(fetch_all) else []

    def fetch_token(self) -> str:
        """便捷：刷新第一条 cookie（单账号/测试用）。"""
        cookies = self.list_cookies()
        if not cookies:
            raise CookieRequiredError()
        cid, cookie_str, fingerprint = cookies[0]
        return self.fetch_token_for(cid, cookie_str, fingerprint)

    def _persist_rotated(self, ctx, cookie_id: str, loaded_cookie: str, acct: dict) -> None:
        """会话 cookie 被上游轮换后按 id 就地回写存储（供重启恢复）。"""
        store = getattr(self._cookie_provider, "store", None)
        if not callable(store):
            return
        try:
            current = extract_session_cookie_string(ctx.cookies())
        except Exception:  # noqa: BLE001 - 读取失败不影响刷新
            return
        if not current or current == str(loaded_cookie or "").strip():
            return
        new_fp = store(current, cookie_id=cookie_id)
        if new_fp:
            # 存储已同步到轮换后的指纹；标记 acct 已同步，下轮不再重注入、复用上下文活 cookie
            acct["fp"] = new_fp

    def fetch_token_for(self, cookie_id: str, cookie_str: str, fingerprint: str) -> str:
        if self._browser is None:
            try:
                self.open()
            except Exception as exc:
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc

        acct = self._accounts.get(cookie_id)
        if acct is None:
            try:
                acct = {"context": self._new_context(), "fp": None}
            except Exception as exc:
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
            self._accounts[cookie_id] = acct
        ctx = acct["context"]

        try:
            if fingerprint != acct["fp"]:
                # 新账号 或 用户重导了新 cookie → 从存储 cookie 起步
                self._apply_cookies_to(ctx, cookie_str)
                acct["fp"] = fingerprint
            page = ctx.new_page()
            try:
                page.goto(
                    LEONARDO_HOME_URL, wait_until="domcontentloaded", timeout=60000
                )
                result = page.evaluate(_SESSION_FETCH_SCRIPT)
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
            self._persist_rotated(ctx, cookie_id, cookie_str, acct)
        except Exception as exc:
            if self._is_browser_control_error(exc):
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
            kind = "proxy" if "proxy" in str(exc).lower() else "network"
            raise RefreshFetchError(kind) from exc

        return self._parse_session(result)

    @staticmethod
    def _parse_session(result) -> str:
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
        accounts = self._accounts
        browser = self._browser
        playwright = self._playwright
        self._accounts = {}
        self._browser = None
        self._playwright = None
        for acct in accounts.values():
            try:
                acct["context"].close()
            except Exception:  # noqa: BLE001
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
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
