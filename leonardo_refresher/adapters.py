import hashlib
import json
import time
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

# 自动登录账号在 list_cookies 里用这个 fingerprint 作标记，fetch_token_for 据此分流到登录路径
LOGIN_MARKER = "__login__"

# 在已过 Vercel 的登录页里 in-page fetch better-auth 邮箱登录端点，带解出的 Turnstile token。
# 裸 HTTP 会撞 Vercel 429，必须页内发；契约实测确认：header x-captcha-response。
_SIGNIN_SCRIPT = """
async (a) => {
  const h = {'content-type':'application/json'};
  h['x-captcha-response'] = a.token;
  try {
    const r = await fetch('/api/auth/sign-in/email', {
      method:'POST', headers:h, credentials:'include',
      body: JSON.stringify({email:a.email, password:a.password, rememberMe:true, callbackURL:'/'})
    });
    let t=''; try { t = await r.text(); } catch(e) {}
    return {status:r.status, body:t.slice(0,200)};
  } catch(e) { return {status:-1, body:String(e)}; }
}
"""
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

    def fetch_logins(self):
        """拉取待自动登录的账号凭据，返回 [{id,email,password,credential_rev}, ...]。

        404/空 → []；网络错误/HTTP≥400/非 JSON → RefreshFetchError 交由上层归入重试。
        每项须齐 id+email+password 才纳入；credential_rev 缺省 1。
        """
        try:
            resp = self._session.get(
                f"{self._base_url}/api/v1/tokens/leonardo/logins",
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise RefreshFetchError("network") from exc
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise RefreshFetchError(f"logins_http_{resp.status_code}")
        try:
            data = resp.json()
        except (TypeError, ValueError) as exc:
            raise RefreshFetchError("invalid_response") from exc
        out = []
        for x in (data or {}).get("logins") or []:
            if x.get("id") and x.get("email") and x.get("password"):
                out.append({
                    "id": str(x["id"]),
                    "email": str(x["email"]),
                    "password": str(x["password"]),
                    "credential_rev": int(x.get("credential_rev") or 1),
                })
        return out

    def report_login(self, id, credential_rev, status, last_error_kind=None, balance=None):
        """回报一次自动登录结果（成功/失败/余额）。回报失败绝不打断刷新主流程。"""
        try:
            self._session.post(
                f"{self._base_url}/api/v1/tokens/leonardo/login/report",
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                json={
                    "id": id,
                    "credential_rev": int(credential_rev),
                    "status": status,
                    "last_error_kind": last_error_kind,
                    "balance": balance,
                },
                timeout=15,
            )
        except Exception:  # noqa: BLE001 - 回报失败不打断刷新
            pass

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
        pxy = self._config.playwright_proxy()  # 解析 user:pass@host:port；空＝直连
        if pxy:
            launch_kwargs["proxy"] = pxy
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

    @staticmethod
    def _session_token_prefix(ctx) -> str:
        """当前 cookie 罐里 session_token 的前 8 位（诊断用：判断轮换前后是否变化）。"""
        try:
            for c in ctx.cookies():
                if c.get("name") == "__Secure-better-auth.session_token":
                    return str(c.get("value") or "")[:8] or "none"
        except Exception:  # noqa: BLE001
            return "err"
        return "none"

    @staticmethod
    def _triple_present(ctx) -> int:
        """better-auth 三件套在罐里齐了几件（诊断用）。"""
        try:
            names = {c.get("name") for c in ctx.cookies()}
        except Exception:  # noqa: BLE001
            return -1
        return sum(1 for n in _BETTER_AUTH_ORDER if n in names)

    @staticmethod
    def _log_cycle(cookie_id, result, st_before, st_after, triple, auth_calls) -> None:
        """每次续期打一行诊断（flush，绕开容器 stdout 缓冲）。

        用于事后确定性区分「代码并发轮换污染(候选A)」vs「上游会话被撤销(候选B)」：
        - auth_reqs>1 说明 SPA 仍在并发调 get-session；
        - 相邻两周期 st_before/st_after 变化或出现两个轮换 token → 指向 A；
        - st 逐字节相同却 body0=null → 指向 B。
        """
        try:
            if isinstance(result, dict):
                status = result.get("status")
                body0 = str(result.get("body") or "")[:48]
            else:
                status, body0 = "?", ""
            print(
                "[leo-refresh] cid=%s status=%s auth_reqs=%s st_before=%s "
                "st_after=%s triple=%s body0=%r"
                % (str(cookie_id)[:8], status, auth_calls, st_before,
                   st_after, triple, body0),
                flush=True,
            )
        except Exception:  # noqa: BLE001 - 诊断日志绝不能影响主流程
            pass

    def list_cookies(self):
        """返回待刷新条目 [(id, cookie, fingerprint), ...]。

        含两类：①用户上传的 cookie（fetch_all）；②配置的自动登录账号——以
        fingerprint=LOGIN_MARKER 标记、cookie 位放 "email\\npassword"，由 fetch_token_for
        分流到登录路径（掉线自动重登，不入 cookie 存储、不攒垃圾）。
        """
        fetch_all = getattr(self._cookie_provider, "fetch_all", None)
        entries = list(fetch_all() if callable(fetch_all) else [])
        for email, password in getattr(self._config, "login_accounts", ()) or ():
            cid = "login:" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
            entries.append((cid, email + "\n" + password, LOGIN_MARKER))
        return entries

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

    def _do_get_session(self, ctx):
        """在给定上下文里跑一次 get-session：拦 SPA 自发 auth、加载首页、页内 fetch。

        返回 (result, st_before, st_after, triple, auth_calls)；不解析、不持久化，供
        cookie 路径与登录路径共用。浏览器控制类异常由调用方分类。
        """
        page = ctx.new_page()
        auth_calls = {"n": 0}

        def _count_auth(req):
            try:
                if "/api/auth/get-session" in getattr(req, "url", ""):
                    auth_calls["n"] += 1
            except Exception:  # noqa: BLE001
                pass

        try:
            page.on("request", _count_auth)
        except Exception:  # noqa: BLE001 - 老/假 page 无 on 不致命
            pass
        st_before, st_after, triple = "none", "none", -1
        try:
            # 加载首页前拦掉 SPA 自发的 /api/auth/*，避免它与我们显式 get-session 并发轮换
            # session_token、污染写回分支（审计候选 A）。仍加载首页以保 Cloudflare/Vercel 放行。
            try:
                page.route("**/api/auth/**", lambda route: route.abort())
            except Exception:  # noqa: BLE001
                pass
            page.goto(LEONARDO_HOME_URL, wait_until="domcontentloaded", timeout=60000)
            st_before = self._session_token_prefix(ctx)
            try:
                page.unroute("**/api/auth/**")
            except Exception:  # noqa: BLE001
                pass
            result = page.evaluate(_SESSION_FETCH_SCRIPT)
            st_after = self._session_token_prefix(ctx)
            triple = self._triple_present(ctx)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        return result, st_before, st_after, triple, auth_calls["n"]

    def _ensure_context(self, cookie_id: str, initial_fp) -> dict:
        if self._browser is None:
            try:
                self.open()
            except Exception as exc:
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
        acct = self._accounts.get(cookie_id)
        if acct is None:
            try:
                acct = {"context": self._new_context(), "fp": initial_fp}
            except Exception as exc:
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
            self._accounts[cookie_id] = acct
        return acct

    def fetch_token_for(self, cookie_id: str, cookie_str: str, fingerprint: str) -> str:
        if fingerprint == LOGIN_MARKER:
            email, _, password = str(cookie_str or "").partition("\n")
            return self._fetch_token_login(cookie_id, email, password)

        acct = self._ensure_context(cookie_id, None)
        ctx = acct["context"]
        try:
            if fingerprint != acct["fp"]:
                # 新账号 或 用户重导了新 cookie → 从存储 cookie 起步
                self._apply_cookies_to(ctx, cookie_str)
                acct["fp"] = fingerprint
            result, st_before, st_after, triple, calls = self._do_get_session(ctx)
            self._log_cycle(cookie_id, result, st_before, st_after, triple, calls)
            self._persist_rotated(ctx, cookie_id, cookie_str, acct)
        except Exception as exc:
            if self._is_browser_control_error(exc):
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
            kind = "proxy" if "proxy" in str(exc).lower() else "network"
            raise RefreshFetchError(kind) from exc
        return self._parse_session(result)

    def _fetch_token_login(self, cookie_id: str, email: str, password: str) -> str:
        """自动登录账号：先用上下文里已有会话续期；会话死/首次则登录后再取。

        会话在"服务器 headless + 住宅代理"里原生诞生并始终从同一出口使用，规避
        "复制会话异地使用被判盗用、~2.5h 被撤销"。登录失败/仍取不到 → LoginRequiredError。
        """
        acct = self._ensure_context(cookie_id, LOGIN_MARKER)
        ctx = acct["context"]
        try:
            try:
                result, stb, sta, triple, calls = self._do_get_session(ctx)
                token = self._parse_session(result)  # 会话仍活 → 直接续期成功
                self._log_cycle(cookie_id, result, stb, sta, triple, calls)
                return token
            except LoginRequiredError:
                pass  # 会话死或首次 → 走登录
            self._login_in_context(ctx, email, password)  # 失败抛 LoginRequiredError
            result, stb, sta, triple, calls = self._do_get_session(ctx)
            self._log_cycle(cookie_id, result, stb, sta, triple, calls)
            return self._parse_session(result)  # 登录后仍取不到 → LoginRequiredError 上抛
        except (LoginRequiredError, RefreshFetchError):
            raise
        except Exception as exc:
            if self._is_browser_control_error(exc):
                self._reset_browser()
                raise RefreshFetchError("browser_control") from exc
            kind = "proxy" if "proxy" in str(exc).lower() else "network"
            raise RefreshFetchError(kind) from exc

    def _login_in_context(self, ctx, email: str, password: str) -> None:
        token = self._solve_turnstile(self._config.turnstile_sitekey)
        if not token:
            print("[leo-login] email=%s turnstile_solve=FAIL" % str(email)[:16], flush=True)
            raise LoginRequiredError()
        page = ctx.new_page()
        try:
            page.goto(self._config.login_url, wait_until="domcontentloaded", timeout=60000)
            res = page.evaluate(_SIGNIN_SCRIPT, {"token": token, "email": email, "password": password})
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            got = bool(extract_session_cookie_string(ctx.cookies()))
        except Exception:  # noqa: BLE001
            got = False
        status = res.get("status") if isinstance(res, dict) else None
        print("[leo-login] email=%s signin_status=%s got_session=%s" % (str(email)[:16], status, got), flush=True)
        if not got:
            raise LoginRequiredError()

    @staticmethod
    def _yc_post(url, payload):
        resp = requests.post(url, json=payload, timeout=35)
        return resp.json()

    def _solve_turnstile(self, sitekey: str):
        """用 YesCaptcha 解 Turnstile，返回 token；无 key/失败/超时返回 None。"""
        key = str(self._config.yescaptcha_key or "").strip()
        if not key:
            return None
        try:
            r = self._yc_post(
                "https://api.yescaptcha.com/createTask",
                {"clientKey": key, "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": self._config.login_url,
                    "websiteKey": sitekey,
                }},
            )
            if r.get("errorId"):
                return None
            tid = r.get("taskId")
            for _ in range(40):
                time.sleep(3)
                r2 = self._yc_post(
                    "https://api.yescaptcha.com/getTaskResult",
                    {"clientKey": key, "taskId": tid},
                )
                if r2.get("errorId"):
                    return None
                if r2.get("status") == "ready":
                    return (r2.get("solution") or {}).get("token")
        except Exception:  # noqa: BLE001 - 解码失败视为无 token
            return None
        return None

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
