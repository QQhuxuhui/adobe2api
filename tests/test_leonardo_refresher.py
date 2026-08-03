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
    def __init__(self, *, token=None, error=None):
        self.token = token
        self.error = error

    def fetch_token(self):
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


def test_run_once_pushes_fresh_token_and_becomes_healthy():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(token=token)

    delay = service.run_once()

    assert sink.received == [(token, "Primary")]
    assert state.snapshot() == {
        "state": "healthy",
        "session_state": "authenticated",
        "last_success_at": 10000,
        "current_token_exp": 13600,
        "consecutive_failures": 0,
        "last_error_kind": None,
    }
    assert delay == 3000


def test_run_once_success_clears_prior_failure_counters():
    # 非空基线：先失败使 consecutive_failures>0 且 last_error_kind 被置，再成功清零。
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(source_error=RefreshFetchError("network"))

    service.run_once()
    assert state.snapshot()["consecutive_failures"] == 1
    assert state.snapshot()["last_error_kind"] == "network"

    service.source.error = None
    service.source.token = token
    delay = service.run_once()

    snap = state.snapshot()
    assert snap["state"] == "healthy"
    assert snap["consecutive_failures"] == 0
    assert snap["last_error_kind"] is None
    assert delay == 3000


def test_run_once_schedules_from_time_after_refresh_work():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    current_time = [10000]

    class SlowSource(_SessionSource):
        def fetch_token(self):
            current_time[0] = 11000
            return super().fetch_token()

    source = SlowSource(token=token)
    sink = _TokenSink()
    state = RuntimeState()
    service = RefresherService(
        source=source,
        sink=sink,
        state=state,
        config=_config(),
        now=lambda: current_time[0],
    )

    delay = service.run_once()

    assert delay == 2000
    assert state.snapshot()["last_success_at"] == 11000


def test_run_once_does_not_push_low_ttl_token():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 10599})
    service, state, sink = _service(token=token)

    delay = service.run_once()

    assert delay == 60
    assert sink.received == []
    assert state.snapshot()["state"] == "refresh_retrying"
    assert state.snapshot()["session_state"] == "authenticated"
    assert state.snapshot()["last_error_kind"] == "stale_token"


def test_run_once_marks_explicit_null_session_as_login_required():
    service, state, sink = _service(source_error=LoginRequiredError())

    assert service.run_once() == 60

    assert sink.received == []
    assert state.snapshot()["state"] == "login_required"
    assert state.snapshot()["session_state"] == "login_required"
    assert state.snapshot()["consecutive_failures"] == 1


def test_run_once_keeps_session_unknown_on_proxy_failure():
    service, state, sink = _service(
        source_error=RefreshFetchError("proxy"),
    )

    assert service.run_once() == 60

    assert sink.received == []
    assert state.snapshot()["state"] == "refresh_retrying"
    assert state.snapshot()["session_state"] == "unknown"
    assert state.snapshot()["last_error_kind"] == "proxy"


def test_run_once_marks_dead_browser_control_unhealthy():
    service, state, sink = _service(
        source_error=RefreshFetchError("browser_control"),
    )

    assert service.run_once() == 60

    assert sink.received == []
    assert state.snapshot()["state"] == "browser_unavailable"
    assert state.snapshot()["session_state"] == "unknown"
    assert state.snapshot()["last_error_kind"] == "browser_control"


def test_run_once_marks_push_failure_without_declaring_logout():
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(
        token=token,
        sink_error=TokenPushError("http_503"),
    )

    assert service.run_once() == 60

    assert sink.received == []
    assert state.snapshot()["state"] == "push_failed"
    assert state.snapshot()["session_state"] == "authenticated"
    assert state.snapshot()["last_error_kind"] == "http_503"


def test_run_once_treats_non_id_token_as_login_required():
    token = _jwt({"token_use": "access", "sub": "leo-1", "exp": 13600})
    service, state, sink = _service(token=token)

    assert service.run_once() == 60

    assert sink.received == []
    assert state.snapshot()["state"] == "login_required"
    assert state.snapshot()["last_error_kind"] == "invalid_token"


@pytest.mark.parametrize("exp", [float("nan"), float("inf"), float("-inf")])
def test_run_once_treats_non_finite_exp_as_invalid_token(exp):
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": exp})
    service, state, sink = _service(token=token)

    assert service.run_once() == 60

    assert sink.received == []
    assert state.snapshot()["state"] == "login_required"
    assert state.snapshot()["last_error_kind"] == "invalid_token"


def test_health_server_returns_runtime_snapshot_and_404_elsewhere():
    state = RuntimeState()
    state.mark_healthy(now=10000, exp=13600)
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
    state.mark_failure(
        state="browser_unavailable",
        session_state="unknown",
        error_kind="browser_control",
    )
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
    state.mark_failure(
        state="login_required",
        session_state="login_required",
        error_kind="login_required",
    )
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

    def goto(self, url, **kwargs):
        self.goto_calls.append({"url": url, **kwargs})

    def evaluate(self, expression):
        self.evaluate_calls.append(expression)
        if isinstance(self.fetch_result, Exception):
            raise self.fetch_result
        return self.fetch_result


class _BrowserContext:
    def __init__(self, fetch_result):
        self.page = _BrowserPage(fetch_result=fetch_result)
        self.pages = [self.page]
        self.init_scripts = []
        self.added_cookies = []
        self.clear_calls = 0
        self.closed = False

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def clear_cookies(self):
        self.clear_calls += 1

    def add_cookies(self, cookies):
        self.added_cookies.append(cookies)

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _Chromium:
    def __init__(self, context):
        self.context = context
        self.calls = []

    def launch_persistent_context(self, profile_dir, **kwargs):
        self.calls.append({"profile_dir": profile_dir, **kwargs})
        return self.context


class _Playwright:
    def __init__(self, context):
        self.chromium = _Chromium(context)
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

    def fetch(self):
        self.calls += 1
        return None if self._none else (self._cookie, self._fingerprint)


def _browser_source(fetch_result, *, provider=None):
    context = _BrowserContext(fetch_result)
    playwright = _Playwright(context)
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
    assert call["profile_dir"] == "/profile"
    assert call["headless"] is True
    assert call["chromium_sandbox"] is False
    assert "--disable-blink-features=AutomationControlled" in call["args"]
    assert call["ignore_default_args"] == ["--enable-automation"]
    assert "Windows NT 10.0" in call["user_agent"]
    assert call["proxy"] == {"server": "http://proxy:10809"}
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

        def fetch(self):
            self.calls += 1
            return (
                "__Secure-better-auth.session_token=v%d" % self.calls,
                "fp%d" % self.calls,
            )

    source, playwright, context = _browser_source(response, provider=_RotatingProvider())
    source.open()
    source.fetch_token()
    source.fetch_token()
    source.close()

    assert context.clear_calls == 2               # 两次指纹不同都重注入
    assert len(context.page.goto_calls) == 2       # 每次变更都重新导航


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
    assert stop_event.delays == [3000]
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

    assert stop_event.delays == [60, 60]
    assert state.snapshot()["consecutive_failures"] == 3


def test_runtime_opens_and_closes_all_owned_resources():
    events = []
    token = _jwt({"token_use": "id", "sub": "leo-1", "exp": 13600})

    class Source:
        def __init__(self):
            self._opened = False

        def open(self):
            events.append("source.open")
            self._opened = True

        def fetch_token(self):
            # run() 不再 eager-open；由 fetch_token 懒加载驱动（真实契约）。
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
    assert stop_event.delays == [3000]


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

        def fetch_token(self):
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
