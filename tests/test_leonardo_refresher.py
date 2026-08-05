import base64
import json
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
import requests

from leonardo_refresher.adapters import (
    Adobe2ApiCookieProvider,
    Adobe2ApiTokenSink,
    PlaywrightSessionSource,
)
from leonardo_refresher.__main__ import run
from leonardo_refresher.config import RefresherConfig
from leonardo_refresher.health import start_health_server
from leonardo_refresher.service import (
    BrowserControlUnavailableError,
    LoginRequiredError,
    RefreshFetchError,
    RefresherService,
    RuntimeState,
    TokenPushError,
    calculate_next_delay,
)


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256"}).encode()
    ).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _install_required_env(monkeypatch):
    monkeypatch.setenv("ADOBE2API_BASE_URL", "http://adobe2api:6001")
    monkeypatch.setenv("LEONARDO_REFRESH_KEY", "refresh-key")
    monkeypatch.setenv("LEONARDO_PROXY", "http://proxy:10809")
    monkeypatch.setenv("NOVNC_PASSWORD", "vnc-password")


@pytest.mark.parametrize(
    "missing",
    ["LEONARDO_REFRESH_KEY"],
)
def test_config_requires_security_and_network_settings(monkeypatch, missing):
    _install_required_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ValueError, match=missing):
        RefresherConfig.from_env()


def test_config_allows_empty_proxy_for_direct_connection(monkeypatch):
    # 出口非受限地区可留空代理＝直连
    _install_required_env(monkeypatch)
    monkeypatch.delenv("LEONARDO_PROXY", raising=False)
    config = RefresherConfig.from_env()
    assert config.proxy == ""


def test_config_loads_defaults_and_normalizes_base_url(monkeypatch):
    _install_required_env(monkeypatch)
    monkeypatch.setenv("ADOBE2API_BASE_URL", "http://adobe2api:6001/")

    config = RefresherConfig.from_env()

    assert config.adobe2api_base_url == "http://adobe2api:6001"
    assert config.account_label == "Leonardo"
    assert config.refresh_interval_seconds == 3000
    assert config.safety_margin_seconds == 600
    assert config.min_interval_seconds == 60
    assert config.health_host == "0.0.0.0"
    assert config.health_port == 8080


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MIN_INTERVAL_SECONDS", "0"),
        ("SAFETY_MARGIN_SECONDS", "60"),
        ("REFRESH_INTERVAL_SECONDS", "60"),
        ("HEALTH_PORT", "70000"),
    ],
)
def test_config_rejects_invalid_numeric_relationships(monkeypatch, name, value):
    _install_required_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        RefresherConfig.from_env()


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [
        (7200, 3000),
        (3600, 3000),
        (601, 60),
        (600, 60),
        (300, 60),
    ],
)
def test_calculate_next_delay_respects_expiry_margin(ttl, expected):
    assert calculate_next_delay(
        exp=10000 + ttl,
        now=10000,
        min_interval=60,
        refresh_interval=3000,
        safety_margin=600,
    ) == expected


class _SessionSource:
    """单账号假 source：暴露多账号接口（一个 cookie），便于沿用旧断言。"""

    def __init__(self, *, token=None, error=None, fingerprint="fp-1"):
        self.token = token
        self.error = error
        self.fingerprint = fingerprint
        self.fetch_calls = []

    def list_cookies(self):
        return [("id-1", "cookie-1", self.fingerprint)]

    def fetch_token_for(self, cookie_id, cookie_str, fingerprint):
        self.fetch_calls.append((cookie_id, cookie_str, fingerprint))
        if self.error is not None:
            raise self.error
        return self.token


class _TokenSink:
    def __init__(self, *, error=None):
        self.error = error
        self.received = []

    def push(self, token, label):
        if self.error is not None:
            raise self.error
        self.received.append((token, label))
        return {"status": "updated"}


def _config() -> RefresherConfig:
    return RefresherConfig(
        adobe2api_base_url="http://adobe2api:6001",
        refresh_key="refresh-key",
        proxy="http://proxy:10809",
        novnc_password="vnc-password",
        account_label="Primary",
        refresh_interval_seconds=3000,
        safety_margin_seconds=600,
        min_interval_seconds=60,
        poll_interval_seconds=15,
    )


def _service(*, token=None, source_error=None, sink_error=None, now=10000):
    source = _SessionSource(token=token, error=source_error)
    sink = _TokenSink(error=sink_error)
    state = RuntimeState()
    service = RefresherService(
        source=source,
        sink=sink,
        state=state,
        config=_config(),
        now=lambda: now,
    )
    return service, state, sink


POLL = 15  # config.poll_interval_seconds


def test_run_once_pushes_fresh_token_and_becomes_healthy():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(token=token)

    delay = service.run_once()

    assert sink.received == [(token, "Primary")]
    snap = state.snapshot()
    assert snap["state"] == "healthy"
    assert snap["session_state"] == "authenticated"
    assert snap["last_success_at"] == 10000
    assert snap["current_token_exp"] == 13600
    assert snap["healthy_accounts"] == 1
    assert snap["total_accounts"] == 1
    assert snap["last_error_kind"] is None
    assert delay == POLL


def test_run_once_success_clears_prior_failure():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(source_error=RefreshFetchError("network"))

    service.run_once()
    assert state.snapshot()["state"] == "refresh_retrying"
    assert state.snapshot()["last_error_kind"] == "network"

    service.source.error = None
    service.source.token = token
    delay = service.run_once()

    snap = state.snapshot()
    assert snap["state"] == "healthy"
    assert snap["last_error_kind"] is None
    assert delay == POLL


def test_run_once_always_returns_poll_interval():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(token=token)
    assert service.run_once() == POLL
    # 已健康且未近过期 → 第二轮跳过刷新（不重复 push），仍返回 poll
    assert service.run_once() == POLL
    assert sink.received == [(token, "Primary")]


def test_run_once_does_not_push_low_ttl_token():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 10599})
    service, state, sink = _service(token=token)

    delay = service.run_once()

    assert delay == POLL
    assert sink.received == []
    assert state.snapshot()["state"] == "refresh_retrying"
    assert state.snapshot()["last_error_kind"] == "stale_token"


def test_run_once_marks_explicit_null_session_as_login_required():
    service, state, sink = _service(source_error=LoginRequiredError())

    assert service.run_once() == POLL

    assert sink.received == []
    assert state.snapshot()["state"] == "login_required"


def test_run_once_keeps_session_unknown_on_proxy_failure():
    service, state, sink = _service(source_error=RefreshFetchError("proxy"))

    assert service.run_once() == POLL

    assert sink.received == []
    assert state.snapshot()["state"] == "refresh_retrying"
    assert state.snapshot()["last_error_kind"] == "proxy"


def test_run_once_marks_dead_browser_control_unhealthy():
    service, state, sink = _service(source_error=RefreshFetchError("browser_control"))

    assert service.run_once() == POLL

    assert sink.received == []
    assert state.snapshot()["state"] == "browser_unavailable"
    assert state.snapshot()["last_error_kind"] == "browser_control"


def test_run_once_marks_push_failure_without_declaring_logout():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(token=token, sink_error=TokenPushError("http_503"))

    assert service.run_once() == POLL

    assert sink.received == []
    assert state.snapshot()["state"] == "push_failed"
    assert state.snapshot()["last_error_kind"] == "http_503"


def test_run_once_treats_non_id_token_as_login_required():
    token = _jwt({"token_use": "access", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(token=token)

    assert service.run_once() == POLL

    assert sink.received == []
    assert state.snapshot()["state"] == "login_required"
    assert state.snapshot()["last_error_kind"] == "invalid_token"


@pytest.mark.parametrize("exp", [float("nan"), float("inf"), float("-inf")])
def test_run_once_treats_non_finite_exp_as_invalid_token(exp):
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": exp})
    service, state, sink = _service(token=token)

    assert service.run_once() == POLL

    assert sink.received == []
    assert state.snapshot()["state"] == "login_required"
    assert state.snapshot()["last_error_kind"] == "invalid_token"


def test_run_once_multiple_accounts_each_pushed():
    """多账号：两个 cookie → 两次 push、两个健康账号。"""
    t1 = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    t2 = _jwt({"token_use": "id", "sub": "leo-2", "exp": 13600})

    class MultiSource:
        def list_cookies(self):
            return [("idA", "cookieA", "fpA"), ("idB", "cookieB", "fpB")]

        def fetch_token_for(self, cid, cookie, fp):
            return t1 if cid == "idA" else t2

    sink = _TokenSink()
    state = RuntimeState()
    service = RefresherService(
        source=MultiSource(), sink=sink, state=state, config=_config(),
        now=lambda: 10000,
    )
    assert service.run_once() == POLL
    assert set(t for t, _ in sink.received) == {t1, t2}
    snap = state.snapshot()
    assert snap["healthy_accounts"] == 2
    assert snap["total_accounts"] == 2
    assert snap["state"] == "healthy"


def test_run_once_new_cookie_picked_up_next_pass():
    """导入即刷新：账号数从 1 变 2，下一轮就把新账号刷进来。"""
    t = _jwt({"token_use": "id", "sub": "leo-x", "exp": 13600})
    cookies = [("idA", "cookieA", "fpA")]

    class GrowingSource:
        def list_cookies(self):
            return list(cookies)

        def fetch_token_for(self, cid, cookie, fp):
            return t

    sink = _TokenSink()
    state = RuntimeState()
    service = RefresherService(
        source=GrowingSource(), sink=sink, state=state, config=_config(),
        now=lambda: 10000,
    )
    service.run_once()
    assert state.snapshot()["healthy_accounts"] == 1
    cookies.append(("idB", "cookieB", "fpB"))  # 用户导入了第二个账号
    service.run_once()
    assert state.snapshot()["healthy_accounts"] == 2


def test_health_server_returns_runtime_snapshot_and_404_elsewhere():
    state = RuntimeState()
    state.mark_account_healthy("fp-1", now=10000, exp=13600)
    server = start_health_server(state, host="127.0.0.1", port=0)
    try:
        with urlopen(
            f"http://127.0.0.1:{server.port}/healthz",
            timeout=2,
        ) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json"
            assert json.loads(response.read()) == state.snapshot()

        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"http://127.0.0.1:{server.port}/missing", timeout=2)
        assert exc_info.value.code == 404
    finally:
        server.close()


def test_health_server_returns_503_when_browser_control_is_unavailable():
    state = RuntimeState()
    state.set_browser_ok(False)
    server = start_health_server(state, host="127.0.0.1", port=0)
    try:
        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"http://127.0.0.1:{server.port}/healthz", timeout=2)
        assert exc_info.value.code == 503
        assert json.loads(exc_info.value.read())["state"] == "browser_unavailable"
    finally:
        server.close()


def test_health_server_stays_200_on_login_required():
    # 登录过期不得让 healthcheck 变红（否则 Docker 反复重启容器）——仅 browser_unavailable 才 503。
    state = RuntimeState()
    state.set_global_error("cookie_required")  # 无账号 → login_required
    server = start_health_server(state, host="127.0.0.1", port=0)
    try:
        with urlopen(
            f"http://127.0.0.1:{server.port}/healthz",
            timeout=2,
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["state"] == "login_required"
    finally:
        server.close()


class _PushResponse:
    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"status": "updated"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"upstream returned {self.status_code}")

    def json(self):
        return self._payload


class _PushSession:
    def __init__(self, *, response=None, error=None):
        self.response = response or _PushResponse()
        self.error = error
        self.trust_env = True
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class _GetResponse:
    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _GetSession:
    def __init__(self, *, response=None, error=None):
        self.response = response
        self.error = error
        self.trust_env = True
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        pass


def test_cookie_provider_returns_cookie_and_fingerprint():
    session = _GetSession(
        response=_GetResponse(payload={"cookie": "__Secure-better-auth.session_token=t", "fingerprint": "fp9"})
    )
    provider = Adobe2ApiCookieProvider(
        base_url="http://adobe2api:6001", refresh_key="k",
        session_factory=lambda: session,
    )
    assert provider.fetch() == ("__Secure-better-auth.session_token=t", "fp9")
    assert session.trust_env is False
    assert session.calls[0]["headers"]["X-Leonardo-Refresh-Key"] == "k"
    assert session.calls[0]["url"].endswith("/api/v1/tokens/leonardo/cookie")


def test_cookie_provider_returns_none_when_not_uploaded():
    session = _GetSession(response=_GetResponse(status_code=404))
    provider = Adobe2ApiCookieProvider(
        base_url="http://adobe2api:6001", refresh_key="k",
        session_factory=lambda: session,
    )
    assert provider.fetch() is None


def test_cookie_provider_network_error_is_retryable():
    session = _GetSession(error=requests.ConnectionError("boom"))
    provider = Adobe2ApiCookieProvider(
        base_url="http://adobe2api:6001", refresh_key="k",
        session_factory=lambda: session,
    )
    with pytest.raises(RefreshFetchError) as exc_info:
        provider.fetch()
    assert exc_info.value.kind == "network"


def test_fetch_logins_returns_list():
    session = _GetSession(response=_GetResponse(payload={"logins": [
        {"id": "i1", "email": "a@b.co", "password": "pw", "credential_rev": 2}]}))
    p = Adobe2ApiCookieProvider(base_url="http://x", refresh_key="k", session_factory=lambda: session)
    assert p.fetch_logins() == [{"id": "i1", "email": "a@b.co", "password": "pw", "credential_rev": 2}]
    assert session.calls[0]["url"].endswith("/api/v1/tokens/leonardo/logins")


def test_fetch_logins_404_raises_route_missing():
    # 404 = /logins 路由未部署（旧 adobe2api / 滚动升级），非「无账号」→ 抛错交上层回退 env，
    # 绝不返回 [] 把它当权威空登录源（那样会误清 env 登录账号）。
    session = _GetSession(response=_GetResponse(status_code=404))
    p = Adobe2ApiCookieProvider(base_url="http://x", refresh_key="k", session_factory=lambda: session)
    with pytest.raises(RefreshFetchError) as exc:
        p.fetch_logins()
    assert exc.value.kind == "logins_http_404"


def test_list_cookies_404_logins_falls_back_to_env():
    # 端点缺失(404) → fetch_logins 抛 RefreshFetchError → list_cookies 回退 env（保护 env 账号）。
    session = _GetSession(response=_GetResponse(status_code=404))
    provider = Adobe2ApiCookieProvider(
        base_url="http://x", refresh_key="k", session_factory=lambda: session)
    src = PlaywrightSessionSource(
        config=_login_config(login_accounts=(("env@x.co", "envpw"),)),
        cookie_provider=provider,
        playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()),
    )
    login = [e for e in src.list_cookies() if e[2].startswith(LOGIN_MARKER)][0]
    assert login[1] == "env@x.co\nenvpw"
    assert login[2] == LOGIN_MARKER  # 裸标记（非 rev 版）


def test_report_login_posts_and_swallows_errors():
    session = _PushSession(error=requests.ConnectionError("down"))
    p = Adobe2ApiCookieProvider(base_url="http://x", refresh_key="k", session_factory=lambda: session)
    p.report_login("i1", 2, "ok", balance=5.0)  # 不抛
    assert session.calls[0]["json"] == {"id": "i1", "credential_rev": 2, "status": "ok",
                                        "last_error_kind": None, "balance": 5.0}


def test_token_sink_ignores_environment_proxy_and_sends_scoped_key():
    session = _PushSession(response=_PushResponse(payload={"status": "created"}))
    sink = Adobe2ApiTokenSink(
        base_url="http://adobe2api:6001/",
        refresh_key="secret",
        session_factory=lambda: session,
    )

    result = sink.push("jwt", "Primary")
    sink.close()

    assert result == {"status": "created"}
    assert session.trust_env is False
    assert session.calls == [
        {
            "url": "http://adobe2api:6001/api/v1/tokens/leonardo",
            "headers": {"X-Leonardo-Refresh-Key": "secret"},
            "json": {"token": "jwt", "label": "Primary"},
            "timeout": 15,
        }
    ]
    assert session.closed is True


def test_token_sink_sanitizes_http_failure():
    session = _PushSession(response=_PushResponse(status_code=503))
    sink = Adobe2ApiTokenSink(
        base_url="http://adobe2api:6001",
        refresh_key="secret",
        session_factory=lambda: session,
    )

    with pytest.raises(TokenPushError) as exc_info:
        sink.push("sensitive-jwt", "Primary")

    assert exc_info.value.kind == "http_503"
    assert "sensitive-jwt" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_token_sink_sanitizes_transport_failure():
    session = _PushSession(error=requests.ConnectionError("contains-sensitive-data"))
    sink = Adobe2ApiTokenSink(
        base_url="http://adobe2api:6001",
        refresh_key="secret",
        session_factory=lambda: session,
    )

    with pytest.raises(TokenPushError) as exc_info:
        sink.push("sensitive-jwt", "Primary")

    assert exc_info.value.kind == "network"
    assert "contains-sensitive-data" not in str(exc_info.value)


class _BrowserPage:
    def __init__(self, *, fetch_result=None):
        self.fetch_result = fetch_result
        self.goto_calls = []
        self.evaluate_calls = []
        self.route_calls = []
        self.unroute_calls = []
        self.request_handlers = []
        self.closed = False

    def on(self, event, handler):
        self.request_handlers.append((event, handler))

    def route(self, pattern, handler):
        self.route_calls.append(pattern)

    def unroute(self, pattern):
        self.unroute_calls.append(pattern)

    def goto(self, url, **kwargs):
        self.goto_calls.append({"url": url, **kwargs})

    def evaluate(self, expression):
        self.evaluate_calls.append(expression)
        if isinstance(self.fetch_result, Exception):
            raise self.fetch_result
        return self.fetch_result

    def close(self):
        self.closed = True


class _BrowserContext:
    def __init__(self, fetch_result):
        self._fetch_result = fetch_result
        self.init_scripts = []
        self.added_cookies = []
        self.clear_calls = 0
        self.closed = False
        self.pages_created = []
        self._cookies = []

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def clear_cookies(self):
        self.clear_calls += 1
        self._cookies = []

    def add_cookies(self, cookies):
        self.added_cookies.append(cookies)
        self._cookies = list(cookies)

    def cookies(self, *args, **kwargs):
        return list(self._cookies)

    def new_page(self):
        page = _BrowserPage(fetch_result=self._fetch_result)
        self.pages_created.append(page)
        return page

    @property
    def page(self):
        # 便捷：返回最近创建的 page（多数测试只跑一次）
        return self.pages_created[-1] if self.pages_created else _BrowserPage()

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self, context):
        self._context = context
        self.contexts_created = []
        self.closed = False

    def new_context(self, **kwargs):
        self.contexts_created.append(kwargs)
        return self._context

    def close(self):
        self.closed = True


class _Chromium:
    def __init__(self, browser):
        self.browser = browser
        self.calls = []

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        return self.browser


class _Playwright:
    def __init__(self, browser):
        self.chromium = _Chromium(browser)
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeCookieProvider:
    def __init__(self, *, cookie="__Secure-better-auth.session_token=tok.sig",
                 fingerprint="fp1", none=False):
        self._cookie = cookie
        self._fingerprint = fingerprint
        self._none = none
        self.calls = 0

    def fetch_all(self):
        self.calls += 1
        return [] if self._none else [("id1", self._cookie, self._fingerprint)]

    def store(self, cookie, cookie_id=None):
        return ""  # 无轮换/成功但不改指纹


def _browser_source(fetch_result, *, provider=None):
    context = _BrowserContext(fetch_result)
    browser = _Browser(context)
    playwright = _Playwright(browser)
    source = PlaywrightSessionSource(
        config=_config(),
        cookie_provider=provider or _FakeCookieProvider(),
        playwright_factory=lambda: playwright,
    )
    return source, playwright, context


def test_browser_source_headless_loads_cookies_and_fetches():
    response = {
        "status": 200,
        "content_type": "application/json",
        "body": json.dumps({"session": {"accessToken": "fresh-jwt"}}),
    }
    source, playwright, context = _browser_source(response)

    source.open()
    token = source.fetch_token()
    source.close()

    call = playwright.chromium.calls[0]
    assert call["headless"] is True
    assert call["chromium_sandbox"] is False
    assert "--disable-blink-features=AutomationControlled" in call["args"]
    assert call["ignore_default_args"] == ["--enable-automation"]
    assert call["proxy"] == {"server": "http://proxy:10809"}
    # UA/locale/viewport 现在在 new_context 上
    ctx_kwargs = playwright.chromium.browser.contexts_created[0]
    assert "Windows NT 10.0" in ctx_kwargs["user_agent"]
    assert any("webdriver" in s for s in context.init_scripts)
    # cookie 被解析并注入
    assert context.clear_calls == 1
    names = [c["name"] for c in context.added_cookies[0]]
    assert "__Secure-better-auth.session_token" in names
    assert context.page.goto_calls[0]["url"] == "https://app.leonardo.ai/"
    assert token == "fresh-jwt"
    assert "fetch('/api/auth/get-session'" in context.page.evaluate_calls[0]
    assert context.closed is True
    assert playwright.stopped is True


def test_browser_source_no_cookie_uploaded_raises_cookie_required():
    from leonardo_refresher.service import CookieRequiredError

    source, _, _ = _browser_source(
        {"status": 200, "content_type": "application/json", "body": "{}"},
        provider=_FakeCookieProvider(none=True),
    )
    source.open()
    with pytest.raises(CookieRequiredError):
        source.fetch_token()
    source.close()


def test_browser_source_reapplies_cookies_on_fingerprint_change():
    response = {
        "status": 200,
        "content_type": "application/json",
        "body": json.dumps({"session": {"accessToken": "t"}}),
    }

    class _RotatingProvider:
        def __init__(self):
            self.calls = 0

        def fetch_all(self):
            self.calls += 1
            # 同一个 id，指纹变化（模拟用户对该账号重导了新 cookie）→ 应重注入
            return [(
                "id1",
                "__Secure-better-auth.session_token=v%d" % self.calls,
                "fp%d" % self.calls,
            )]

        def store(self, cookie, cookie_id=None):
            return ""

    source, playwright, context = _browser_source(response, provider=_RotatingProvider())
    source.open()
    source.fetch_token()
    source.fetch_token()
    source.close()

    assert context.clear_calls == 2               # 两次指纹不同都重注入
    assert len(context.pages_created) == 2         # 每次都开新 page 导航


@pytest.mark.parametrize(
    "fetch_result",
    [
        {"status": 200, "content_type": "application/json", "body": "null"},
        {"status": 200, "content_type": "text/html", "body": "<html>Login</html>"},
        {
            "status": 200,
            "content_type": "application/json",
            "body": json.dumps({"session": None}),
        },
    ],
)
def test_browser_source_maps_missing_session_to_login_required(fetch_result):
    source, _, _ = _browser_source(fetch_result)
    source.open()

    with pytest.raises(LoginRequiredError):
        source.fetch_token()

    source.close()


def test_browser_source_omits_proxy_when_empty():
    context = _BrowserContext(
        {"status": 200, "content_type": "application/json",
         "body": json.dumps({"session": {"accessToken": "t"}})}
    )
    playwright = _Playwright(context)
    cfg = RefresherConfig(
        adobe2api_base_url="http://adobe2api:6001", refresh_key="k", proxy="",
        account_label="Primary", refresh_interval_seconds=3000,
        safety_margin_seconds=600, min_interval_seconds=60,
    )
    source = PlaywrightSessionSource(
        config=cfg, cookie_provider=_FakeCookieProvider(),
        playwright_factory=lambda: playwright,
    )
    source.open()
    source.close()
    assert "proxy" not in playwright.chromium.calls[0]


@pytest.mark.parametrize("status", [403, 451])
def test_browser_source_maps_geo_status(status):
    response = {"status": status, "content_type": "text/plain", "body": "blocked"}
    source, _, _ = _browser_source(response)
    source.open()

    with pytest.raises(RefreshFetchError) as exc_info:
        source.fetch_token()

    assert exc_info.value.kind == "geo_embargo"
    source.close()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_browser_source_maps_5xx_html_gateway_to_retryable(status):
    response = {
        "status": status,
        "content_type": "text/html",
        "body": "<html>Bad gateway</html>",
    }
    source, _, _ = _browser_source(response)
    source.open()

    with pytest.raises(RefreshFetchError) as exc_info:
        source.fetch_token()

    assert exc_info.value.kind == f"http_{status}"
    source.close()


def test_browser_source_maps_401_to_login_required():
    response = {"status": 401, "content_type": "application/json", "body": "{}"}
    source, _, _ = _browser_source(response)
    source.open()

    with pytest.raises(LoginRequiredError):
        source.fetch_token()

    source.close()


def test_browser_source_sanitizes_browser_transport_error():
    source, _, _ = _browser_source(RuntimeError("proxy secret leaked here"))
    source.open()

    with pytest.raises(RefreshFetchError) as exc_info:
        source.fetch_token()

    assert exc_info.value.kind == "proxy"
    assert "secret" not in str(exc_info.value)
    source.close()


def test_browser_source_control_error_resets_and_raises():
    source, playwright, context = _browser_source(
        RuntimeError("Target page, context or browser has been closed")
    )
    source.open()

    with pytest.raises(RefreshFetchError) as exc_info:
        source.fetch_token()

    assert exc_info.value.kind == "browser_control"
    assert context.closed is True                 # 控制通道错误重置浏览器
    assert playwright.stopped is True


class _StopAfterFirstWait:
    def __init__(self):
        self.delays = []

    def is_set(self):
        return False

    def wait(self, delay):
        self.delays.append(delay)
        return True


def test_run_forever_refreshes_immediately_and_waits_interruptibly():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(token=token)
    stop_event = _StopAfterFirstWait()

    service.run_forever(stop_event)

    assert sink.received == [(token, "Primary")]
    assert stop_event.delays == [15]
    assert state.snapshot()["state"] == "healthy"


def test_run_forever_exits_after_three_browser_control_failures():
    class StopAfterThreeWaits:
        def __init__(self):
            self.delays = []

        def is_set(self):
            return False

        def wait(self, delay):
            self.delays.append(delay)
            return len(self.delays) >= 3

    source = _SessionSource(error=RefreshFetchError("browser_control"))
    sink = _TokenSink()
    state = RuntimeState()
    service = RefresherService(
        source=source,
        sink=sink,
        state=state,
        config=_config(),
        now=lambda: 10000,
    )
    stop_event = StopAfterThreeWaits()

    with pytest.raises(BrowserControlUnavailableError):
        service.run_forever(stop_event)

    assert stop_event.delays == [15, 15]
    assert state.snapshot()["state"] == "browser_unavailable"


def test_runtime_opens_and_closes_all_owned_resources():
    events = []
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})

    class Source:
        def __init__(self):
            self._opened = False

        def open(self):
            events.append("source.open")
            self._opened = True

        def list_cookies(self):
            return [("id", "cookie", "fp")]

        def fetch_token_for(self, cid, cookie, fp):
            # run() 不再 eager-open；由 fetch 懒加载驱动（真实契约）。
            if not self._opened:
                self.open()
            events.append("source.fetch")
            return token

        def close(self):
            events.append("source.close")

    class Sink:
        def push(self, pushed_token, label):
            assert pushed_token == token
            assert label == "Primary"
            events.append("sink.push")
            return {"status": "updated"}

        def close(self):
            events.append("sink.close")

    class Health:
        def close(self):
            events.append("health.close")

    stop_event = _StopAfterFirstWait()

    result = run(
        config=_config(),
        source_factory=lambda config: Source(),
        sink_factory=lambda base_url, refresh_key: Sink(),
        health_factory=lambda state, host, port: Health(),
        stop_event=stop_event,
        now=lambda: 10000,
    )

    assert result == 0
    assert events == [
        "source.open",
        "source.fetch",
        "sink.push",
        "source.close",
        "sink.close",
        "health.close",
    ]
    assert stop_event.delays == [15]


def test_runtime_survives_transient_startup_open_failure():
    # 启动瞬时 open 失败不得让容器崩溃：应经 fetch_token 懒加载自愈，不 eager-open。
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})

    class FlakySource:
        def __init__(self):
            self.open_calls = 0
            self.opened = False

        def open(self):
            self.open_calls += 1
            if self.open_calls == 1:
                raise RuntimeError("startup proxy down")
            self.opened = True

        def list_cookies(self):
            return [("id", "cookie", "fp")]

        def fetch_token_for(self, cid, cookie, fp):
            if not self.opened:
                try:
                    self.open()
                except Exception as exc:
                    raise RefreshFetchError("browser_control") from exc
            return token

        def close(self):
            pass

    class Sink:
        def push(self, pushed_token, label):
            return {"status": "updated"}

        def close(self):
            pass

    class Health:
        def close(self):
            pass

    class StopAfterTwoWaits:
        def __init__(self):
            self.delays = []

        def is_set(self):
            return False

        def wait(self, delay):
            self.delays.append(delay)
            return len(self.delays) >= 2

    source = FlakySource()
    result = run(
        config=_config(),
        source_factory=lambda config: source,
        sink_factory=lambda base_url, refresh_key: Sink(),
        health_factory=lambda state, host, port: Health(),
        stop_event=StopAfterTwoWaits(),
        now=lambda: 10000,
    )

    assert result == 0
    assert source.open_calls == 2  # 首轮懒开失败、次轮重试成功，进程未崩溃


def test_runtime_closes_remaining_resources_when_one_close_fails():
    events = []
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})

    class Source:
        def open(self):
            pass

        def fetch_token(self):
            return token

        def close(self):
            events.append("source.close")
            raise RuntimeError("source close failed")

    class Sink:
        def push(self, pushed_token, label):
            return {"status": "updated"}

        def close(self):
            events.append("sink.close")

    class Health:
        def close(self):
            events.append("health.close")

    with pytest.raises(RuntimeError, match="source close failed"):
        run(
            config=_config(),
            source_factory=lambda config: Source(),
            sink_factory=lambda base_url, refresh_key: Sink(),
            health_factory=lambda state, host, port: Health(),
            stop_event=_StopAfterFirstWait(),
            now=lambda: 10000,
        )

    assert events == ["source.close", "sink.close", "health.close"]


# ---- 自动登录（email/password + YesCaptcha 解 Turnstile 铸新会话）----

from leonardo_refresher.adapters import LOGIN_MARKER
from leonardo_refresher.config import playwright_proxy, _parse_login_accounts


def test_config_parses_login_accounts_json():
    accs = _parse_login_accounts('[{"email":"a@b.co","password":"pw1"},{"email":"c@d.co","password":"pw2"}]')
    assert accs == (("a@b.co", "pw1"), ("c@d.co", "pw2"))
    assert _parse_login_accounts("") == ()
    assert _parse_login_accounts("not json") == ()
    # 缺字段的条目跳过
    assert _parse_login_accounts('[{"email":"x@y.z"}]') == ()


def test_playwright_proxy_splits_auth():
    assert playwright_proxy("http://user:pass@1.2.3.4:7260") == {
        "server": "http://1.2.3.4:7260", "username": "user", "password": "pass"}
    assert playwright_proxy("http://1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    assert playwright_proxy("") is None


def _login_config(**kw):
    base = dict(
        adobe2api_base_url="http://adobe2api:6001", refresh_key="k", proxy="",
        account_label="Primary", refresh_interval_seconds=3000,
        safety_margin_seconds=600, min_interval_seconds=60, poll_interval_seconds=15,
        login_accounts=(("a@b.co", "pw"),), yescaptcha_key="yc-key",
    )
    base.update(kw)
    return RefresherConfig(**base)


class _LoginCtx:
    """模拟登录用上下文：未登录时 get-session 返回 null，signin 后种下会话 cookie。"""
    def __init__(self):
        self.logged_in = False
        self.init_scripts = []
        self.closed = False

    def add_init_script(self, s):
        self.init_scripts.append(s)

    def clear_cookies(self):
        pass

    def add_cookies(self, c):
        pass

    def cookies(self, *a, **k):
        if not self.logged_in:
            return []
        return [
            {"name": "__Secure-better-auth.session_token", "value": "tok.sig"},
            {"name": "__Secure-better-auth.session_data.0", "value": "p0"},
            {"name": "__Secure-better-auth.session_data.1", "value": "p1"},
        ]

    def new_page(self):
        return _LoginPage(self)

    def close(self):
        self.closed = True


class _LoginPage:
    def __init__(self, ctx):
        self.ctx = ctx

    def on(self, e, h):
        pass

    def route(self, p, h):
        pass

    def unroute(self, p):
        pass

    def goto(self, u, **k):
        pass

    def evaluate(self, expr, arg=None):
        if "sign-in/email" in expr:  # _SIGNIN_SCRIPT → 模拟登录成功
            self.ctx.logged_in = True
            return {"status": 200, "body": "{}"}
        # _SESSION_FETCH_SCRIPT
        if self.ctx.logged_in:
            return {"status": 200, "content_type": "application/json",
                    "body": json.dumps({"session": {"accessToken": "fresh-jwt"}})}
        return {"status": 200, "content_type": "application/json", "body": "null"}

    def close(self):
        pass


class _LoginBrowser:
    def __init__(self):
        self.contexts = []
        self.closed = False

    def new_context(self, **k):
        c = _LoginCtx()
        self.contexts.append(c)
        return c

    def close(self):
        self.closed = True


class _LoginChromium:
    def __init__(self, browser):
        self.browser = browser
        self.calls = []

    def launch(self, **k):
        self.calls.append(k)
        return self.browser


class _LoginPlaywright:
    def __init__(self, browser):
        self.chromium = _LoginChromium(browser)
        self.stopped = False

    def stop(self):
        self.stopped = True


class _EmptyProvider:
    def fetch_all(self):
        return []


def _login_source(config=None):
    browser = _LoginBrowser()
    pw = _LoginPlaywright(browser)
    src = PlaywrightSessionSource(
        config=config or _login_config(),
        cookie_provider=_EmptyProvider(),
        playwright_factory=lambda: pw,
    )
    return src, pw, browser


def test_login_account_appears_in_list_cookies():
    src, _, _ = _login_source()
    entries = src.list_cookies()
    login = [e for e in entries if e[2] == LOGIN_MARKER]
    assert len(login) == 1
    cid, cstr, fp = login[0]
    assert cid.startswith("login:")
    assert cstr == "a@b.co\npw"  # email\npassword


def test_login_account_logs_in_then_returns_token(monkeypatch):
    src, pw, browser = _login_source()
    monkeypatch.setattr(src, "_solve_turnstile", lambda sk: "turnstile-tok")
    src.open()
    cid, cstr, fp = [e for e in src.list_cookies() if e[2] == LOGIN_MARKER][0]
    token = src.fetch_token_for(cid, cstr, fp)
    assert token == "fresh-jwt"                 # 首次 get-session=null→登录→再取成功
    assert browser.contexts[0].logged_in is True


def test_login_account_reuses_live_session_without_solving(monkeypatch):
    src, pw, browser = _login_source()
    # 让上下文一开始就"已登录"（会话仍活），则不应触发解码/登录
    calls = {"solve": 0}
    monkeypatch.setattr(src, "_solve_turnstile", lambda sk: calls.__setitem__("solve", calls["solve"] + 1) or "t")
    src.open()
    cid, cstr, fp = [e for e in src.list_cookies() if e[2] == LOGIN_MARKER][0]
    src._accounts[cid] = {"context": browser.new_context(), "fp": LOGIN_MARKER}
    src._accounts[cid]["context"].logged_in = True
    token = src.fetch_token_for(cid, cstr, fp)
    assert token == "fresh-jwt"
    assert calls["solve"] == 0                  # 会话活着 → 不解码不登录


def test_login_failure_when_turnstile_unsolved(monkeypatch):
    src, _, _ = _login_source()
    monkeypatch.setattr(src, "_solve_turnstile", lambda sk: None)  # 解码失败
    src.open()
    cid, cstr, fp = [e for e in src.list_cookies() if e[2] == LOGIN_MARKER][0]
    with pytest.raises(LoginRequiredError):
        src.fetch_token_for(cid, cstr, fp)


def test_login_source_uses_authenticated_proxy():
    cfg = _login_config(proxy="http://u:p@9.9.9.9:7000")
    src, pw, _ = _login_source(config=cfg)
    src.open()
    assert pw.chromium.calls[0]["proxy"] == {"server": "http://9.9.9.9:7000", "username": "u", "password": "p"}


# ---- 登录源：端点(fetch_logins)优先，env 兜底；rev fingerprint；drop_context；余额上报 ----


class _LoginProvOK:
    """有 fetch_logins 端点、返回一条登录凭据。"""
    def fetch_all(self):
        return []

    def fetch_logins(self):
        return [{"id": "i1", "email": "a@b.co", "password": "pw", "credential_rev": 3}]


class _LoginProvFail:
    def fetch_all(self):
        return []

    def fetch_logins(self):
        raise RefreshFetchError("network")


class _ReportProv:
    """记录 report_login 调用（fetch_all 空，无 fetch_logins → env/手工 fingerprint）。"""
    def __init__(self):
        self.reports = []

    def fetch_all(self):
        return []

    def report_login(self, id, credential_rev, status, last_error_kind=None, balance=None):
        self.reports.append((id, credential_rev, status, last_error_kind, balance))


def test_list_cookies_uses_endpoint_login_source():
    src = PlaywrightSessionSource(
        config=_login_config(login_accounts=(("env@x.co", "envpw"),)),
        cookie_provider=_LoginProvOK(),
        playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()),
    )
    entries = src.list_cookies()
    login = [e for e in entries if e[2].startswith(LOGIN_MARKER)][0]
    assert login[0] == "i1" and login[2] == f"{LOGIN_MARKER}:3" and login[1] == "a@b.co\npw"
    # 端点成功(即使只返回一条) → 不并入 env 账号
    assert not any(e[1].startswith("env@x.co") for e in entries)


def test_list_cookies_empty_endpoint_does_not_fall_back_to_env():
    class _EmptyLogins:
        def fetch_all(self):
            return []

        def fetch_logins(self):
            return []  # 端点成功但空 → 以存储为准，不回退 env

    src = PlaywrightSessionSource(
        config=_login_config(login_accounts=(("env@x.co", "envpw"),)),
        cookie_provider=_EmptyLogins(),
        playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()),
    )
    entries = src.list_cookies()
    assert not any(e[2].startswith(LOGIN_MARKER) for e in entries)
    assert not any(e[1].startswith("env@x.co") for e in entries)


def test_list_cookies_falls_back_to_env_on_fetch_error():
    src = PlaywrightSessionSource(
        config=_login_config(login_accounts=(("env@x.co", "envpw"),)),
        cookie_provider=_LoginProvFail(),
        playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()),
    )
    login = [e for e in src.list_cookies() if e[2].startswith(LOGIN_MARKER)][0]
    assert login[1] == "env@x.co\nenvpw"
    assert login[2] == LOGIN_MARKER


def test_drop_context_closes_and_removes():
    src, pw, browser = _login_source()
    src.open()
    src._accounts["cid"] = {"context": browser.new_context(), "fp": LOGIN_MARKER}
    ctx = src._accounts["cid"]["context"]
    src.drop_context("cid")
    assert "cid" not in src._accounts and ctx.closed is True


def test_drop_context_missing_is_noop():
    src, _, _ = _login_source()
    src.drop_context("nope")  # 不抛


def test_login_success_reports_ok_with_balance(monkeypatch):
    prov = _ReportProv()
    src = PlaywrightSessionSource(
        config=_login_config(),
        cookie_provider=prov,
        playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()),
    )
    monkeypatch.setattr(src, "_solve_turnstile", lambda sk: "t")
    monkeypatch.setattr(src, "_get_balance", lambda: 7.5)
    src.open()
    token = src.fetch_token_for("i9", "a@b.co\npw", f"{LOGIN_MARKER}:5")
    assert token == "fresh-jwt"
    assert prov.reports == [("i9", 5, "ok", None, 7.5)]  # rev 透传 + 余额上报


def test_login_failure_reports_captcha_then_reraises(monkeypatch):
    prov = _ReportProv()
    src = PlaywrightSessionSource(
        config=_login_config(),
        cookie_provider=prov,
        playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()),
    )
    monkeypatch.setattr(src, "_solve_turnstile", lambda sk: None)  # 解不出 → captcha
    monkeypatch.setattr(src, "_get_balance", lambda: 2.0)
    src.open()
    with pytest.raises(LoginRequiredError):
        src.fetch_token_for("i9", "a@b.co\npw", f"{LOGIN_MARKER}:5")
    assert prov.reports == [("i9", 5, "login_required", "captcha", 2.0)]


def test_get_balance_reads_yescaptcha(monkeypatch):
    src, _, _ = _login_source()
    monkeypatch.setattr(src, "_yc_post", lambda url, payload: {"errorId": 0, "balance": "12.34"})
    assert src._get_balance() == 12.34


def test_get_balance_none_without_key():
    src, _, _ = _login_source(config=_login_config(yescaptcha_key=""))
    assert src._get_balance() is None


# ---- service：credential_rev 变化立即重验 + 缺席账号回收 context ----


def test_credential_rev_change_forces_relogin():
    """登录账号 rev 升级 → 清 _known + drop_context → 本轮立即重新 fetch。"""
    token = _jwt({"token_use": "id", "sub": "s", "exp": 13600})
    dropped = []

    class Src:
        def __init__(self):
            self.rev = 1
            self.fetch_calls = []

        def list_cookies(self):
            return [("i1", "a@b.co\npw", f"{LOGIN_MARKER}:{self.rev}")]

        def fetch_token_for(self, cid, cookie, fp):
            self.fetch_calls.append((cid, fp))
            return token

        def drop_context(self, cid):
            dropped.append(cid)

    src = Src()
    service = RefresherService(
        source=src, sink=_TokenSink(), state=RuntimeState(),
        config=_config(), now=lambda: 10000,
    )

    service.run_once()
    assert state_state(service) == "healthy"
    assert len(src.fetch_calls) == 1
    assert service._known["i1"] == 13600
    # 首见时也记指纹，但首见不算「变化」→ 不 drop
    assert dropped == []

    # 同 rev 第二轮：已健康未近过期 → 跳过，不 fetch、不 drop
    service.run_once()
    assert len(src.fetch_calls) == 1
    assert dropped == []

    # rev 升到 2（凭据改）→ 清 _known + drop_context + 本轮重新 fetch
    src.rev = 2
    service.run_once()
    assert dropped == ["i1"]
    assert len(src.fetch_calls) == 2          # 清缓存后立即重验
    assert service._login_fp["i1"] == f"{LOGIN_MARKER}:2"


def test_credential_rev_change_clears_known_when_relogin_fails():
    """rev 变化必须真正清空 _known：让重登失败，可直接观测 _known 被清。"""
    token = _jwt({"token_use": "id", "sub": "s", "exp": 13600})

    class Src:
        def __init__(self):
            self.rev = 1
            self.dropped = []

        def list_cookies(self):
            return [("i1", "a@b.co\npw", f"{LOGIN_MARKER}:{self.rev}")]

        def fetch_token_for(self, cid, cookie, fp):
            if self.rev == 1:
                return token
            raise LoginRequiredError()  # rev 变后重登失败 → _known 应保持被清

        def drop_context(self, cid):
            self.dropped.append(cid)

    src = Src()
    service = RefresherService(
        source=src, sink=_TokenSink(), state=RuntimeState(),
        config=_config(), now=lambda: 10000,
    )
    service.run_once()
    assert service._known.get("i1") == 13600

    src.rev = 2
    service.run_once()
    assert "i1" not in service._known          # rev 变 → 缓存被清且未回填
    assert src.dropped == ["i1"]


def test_absent_login_cid_drops_context():
    """账号被删除 → 下一轮 prune 阶段经 retain_contexts 以 source 为准回收其 context。"""
    token = _jwt({"token_use": "id", "sub": "s", "exp": 13600})
    retained = []

    class Src:
        def __init__(self):
            # 保留一个 cookie 账号使列表非空（走正常 prune 路径，非空列表分支）
            self.items = [("i1", "a@b.co\npw", f"{LOGIN_MARKER}:1"),
                          ("keep", "cookieK", "fpK")]

        def list_cookies(self):
            return list(self.items)

        def fetch_token_for(self, cid, cookie, fp):
            return token

        def drop_context(self, cid):
            pass  # rev 变化路径才用；本例不触发

        def retain_contexts(self, present):
            retained.append(set(present))

    src = Src()
    service = RefresherService(
        source=src, sink=_TokenSink(), state=RuntimeState(),
        config=_config(), now=lambda: 10000,
    )
    service.run_once()
    assert retained[-1] == {"i1", "keep"}      # 两账号都在 → 都保留

    src.items = [("keep", "cookieK", "fpK")]   # 删掉登录账号 i1
    service.run_once()
    assert retained[-1] == {"keep"}            # 缺席登录 cid 被 retain 排除（回收）
    assert "i1" not in service._login_fp        # _login_fp 也被 prune


def test_untracked_context_pruned_via_retain():
    """瞬时失败过的 cid 从未进 _known/_retry_after，删号时仍应被 retain_contexts 回收
    （旧的 tracked-cid drop 循环看不到它，会泄漏 Playwright context）。"""
    token = _jwt({"token_use": "id", "sub": "s", "exp": 13600})

    class Src:
        def __init__(self):
            self.items = [("keep", "cookieK", "fpK")]
            # ghost：曾建过 context 但从没进 _known/_retry_after
            self._accounts = {"ghost": object(), "keep": object()}
            self.closed = []

        def list_cookies(self):
            return list(self.items)

        def fetch_token_for(self, cid, cookie, fp):
            return token

        def drop_context(self, cid):
            self._accounts.pop(cid, None)

        def retain_contexts(self, present):
            for cid in list(self._accounts):
                if cid not in present:
                    self.closed.append(cid)
                    self._accounts.pop(cid, None)

    src = Src()
    service = RefresherService(
        source=src, sink=_TokenSink(), state=RuntimeState(),
        config=_config(), now=lambda: 10000,
    )
    service.run_once()
    # present={"keep"}；ghost 不在列表、也不在任何 tracked set → retain 回收
    assert src.closed == ["ghost"]
    assert "ghost" not in src._accounts


def test_empty_cookies_prunes_contexts_and_clears_login_fp():
    """空列表分支：以 retain_contexts(set()) 回收全部 context，并清 _login_fp。"""
    calls = []

    class Src:
        def list_cookies(self):
            return []

        def retain_contexts(self, present):
            calls.append(set(present))

    src = Src()
    service = RefresherService(
        source=src, sink=_TokenSink(), state=RuntimeState(),
        config=_config(), now=lambda: 10000,
    )
    service._login_fp["stale"] = f"{LOGIN_MARKER}:1"
    service.run_once()
    assert calls == [set()]
    assert service._login_fp == {}


def test_retain_contexts_closes_and_pops_absent():
    """PlaywrightSessionSource.retain_contexts：关闭并移除不在 present 里的 context。"""
    src, pw, browser = _login_source()
    src.open()
    keep_ctx = browser.new_context()
    drop_ctx = browser.new_context()
    src._accounts["keep"] = {"context": keep_ctx, "fp": LOGIN_MARKER}
    src._accounts["drop"] = {"context": drop_ctx, "fp": LOGIN_MARKER}
    src.retain_contexts({"keep"})
    assert "drop" not in src._accounts and drop_ctx.closed is True
    assert "keep" in src._accounts and keep_ctx.closed is False


def test_cookie_fingerprint_change_does_not_clear_or_drop():
    """cookie 账号指纹变化＝正常轮换：不得清缓存/丢 context（仅 login 走 rev 逻辑）。"""
    token = _jwt({"token_use": "id", "sub": "s", "exp": 13600})
    dropped = []

    class Src:
        def __init__(self):
            self.fp = "fp1"
            self.fetch_calls = 0

        def list_cookies(self):
            return [("c1", "cookie", self.fp)]

        def fetch_token_for(self, cid, cookie, fp):
            self.fetch_calls += 1
            return token

        def drop_context(self, cid):
            dropped.append(cid)

    src = Src()
    service = RefresherService(
        source=src, sink=_TokenSink(), state=RuntimeState(),
        config=_config(), now=lambda: 10000,
    )
    service.run_once()
    assert src.fetch_calls == 1

    src.fp = "fp2"                              # cookie 重导，指纹变
    service.run_once()
    # 已健康且未近过期 → 跳过；cookie 指纹变化不触发 drop/清缓存/重 fetch
    assert src.fetch_calls == 1
    assert dropped == []


def state_state(service) -> str:
    return service.state.snapshot()["state"]
