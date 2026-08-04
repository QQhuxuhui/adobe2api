import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import core.leonardo_client as lc
from core.leonardo_client import (
    LeonardoError,
    decode_jwt_payload,
    token_exp,
    is_fresh_token,
    is_likely_leonardo_token,
    TOKEN_BALANCE_QUERY,
    sum_credits,
    parse_token_balance,
    LEONARDO_SIZES,
    aspect_to_size,
    build_generate_payload,
    parse_generation_id,
    build_status_query,
    build_feed_query,
    parse_generation_status,
    parse_image_urls,
    LeonardoClient,
    GRAPHQL_URL,
)


def _jwt(payload: dict) -> str:
    def seg(d):
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return f"{seg({'alg':'none'})}.{seg(payload)}.sig"


class _RecordingHttpSession:
    def __init__(self, post):
        self._post = post
        self.trust_env = True
        self.closed = False

    def post(self, *args, **kwargs):
        return self._post(*args, **kwargs)

    def close(self):
        self.closed = True


def _install_http_session(monkeypatch, post):
    session = _RecordingHttpSession(post)

    def session_factory(**kwargs):
        session.trust_env = kwargs.get("trust_env", True)
        return session

    monkeypatch.setattr(lc.requests, "Session", lambda: session)
    monkeypatch.setattr(lc, "CurlSession", session_factory)
    return session


def _start_trickle_server(method: str, payload: bytes):
    class _Server(ThreadingHTTPServer):
        daemon_threads = True

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _respond(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            for byte in payload:
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.02)

        do_GET = _respond
        do_POST = _respond

    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/graphql"
    return server, thread, url


def test_decode_and_exp():
    tok = _jwt({"exp": 1900000000, "iss": "https://cognito-idp.us-east-1.amazonaws.com/x"})
    assert decode_jwt_payload(tok)["exp"] == 1900000000
    assert token_exp(tok) == 1900000000


def test_decode_non_jwt_returns_empty():
    assert decode_jwt_payload("not-a-jwt") == {}
    assert token_exp("nope") == 0


def test_is_fresh_uses_injected_now():
    tok = _jwt({"exp": 1000})
    assert is_fresh_token(tok, now=lambda: 800) is True      # 1000 > 800+... no: 800+120=920 < 1000
    assert is_fresh_token(tok, now=lambda: 950) is False     # 950+120=1070 > 1000 -> not fresh
    assert is_fresh_token("no-dots", now=lambda: 0) is False


def test_is_fresh_true_when_no_exp():
    assert is_fresh_token(_jwt({"sub": "x"}), now=lambda: 0) is True


def test_is_likely_leonardo_token_cognito_signals():
    assert is_likely_leonardo_token(_jwt({"iss": "https://cognito-idp.us-east-1.amazonaws.com/x"})) is True
    assert is_likely_leonardo_token(_jwt({"token_use": "access"})) is True
    assert is_likely_leonardo_token(_jwt({"cognito:username": "u"})) is True
    assert is_likely_leonardo_token(_jwt({"foo": "bar"})) is False


def test_leonardo_error_is_exception():
    assert issubclass(LeonardoError, Exception)


def test_sum_credits_counts_only_generation_usable():
    # apiCredit/streamTokens 是官方 API 通道，出图扣不到 → 不计入可用额度
    details = {"subscriptionTokens": 100, "paidTokens": 5, "rolloverTokens": 0,
               "apiCredit": 8500, "streamTokens": 3}
    assert sum_credits(details) == 105


def test_sum_credits_ignores_missing_and_nonnumeric():
    assert sum_credits({"subscriptionTokens": 10, "paidTokens": None, "apiCredit": "x"}) == 10


def test_parse_token_balance_from_response():
    resp = {"data": {"user_details": [{"subscriptionTokens": 850, "apiCredit": 0}]}}
    assert parse_token_balance(resp) == 850


def test_parse_token_balance_empty_returns_none():
    assert parse_token_balance({"data": {"user_details": []}}) is None
    assert parse_token_balance({}) is None


def test_token_balance_query_shape():
    assert TOKEN_BALANCE_QUERY["operationName"] == "GetTokenBalance"
    assert "user_details" in TOKEN_BALANCE_QUERY["query"]


def test_aspect_to_size_known_and_unsupported():
    # 尺寸按模型族取（详见 tests/test_leonardo_sizes.py）；未知比例不再静默回退 1:1，
    # 而是返回 None 让路由 400——回退会让下游拿到与请求不符的比例。
    assert aspect_to_size("16:9", model_slug="nano-banana-2") == (2752, 1536)
    assert aspect_to_size("9:16", model_slug="nano-banana-2") == (1536, 2752)
    assert aspect_to_size("weird", model_slug="nano-banana-2") is None
    assert set(LEONARDO_SIZES) == {"gemini", "gpt", "gpt-image-1"}


def test_build_generate_payload_core_fields():
    p = build_generate_payload("  a cat  ", "MODEL-123", 1536, 1536, quantity=9)
    assert p["operationName"] == "Generate"
    req = p["variables"]["request"]
    assert req["model"] == "nano-banana-2"           # 包裹层恒定
    params = req["parameters"]
    assert params["modelId"] == "MODEL-123"          # 动态模型
    assert params["prompt"] == "a cat"               # trim
    assert params["quantity"] == 4                   # 9 被夹到 [1,4]
    assert params["dimensions"] == "1536x1536"
    assert "guidances" not in params                 # 无参考图


def test_build_generate_payload_honors_model_slug():
    p = build_generate_payload("x", "UUID-9", 1024, 1024, model_slug="gpt-image-2")
    req = p["variables"]["request"]
    assert req["model"] == "gpt-image-2"          # 包裹层 = 目标模型 slug
    assert req["parameters"]["modelId"] == "UUID-9"


def test_build_generate_payload_with_reference_images():
    p = build_generate_payload("x", "M", 1536, 1536, init_image_ids=["img-1"])
    ref = p["variables"]["request"]["parameters"]["guidances"]["image_reference"]
    assert ref == [{"image": {"id": "img-1", "type": "UPLOADED"}, "strength": "MID"}]


def test_parse_generation_id_success():
    assert parse_generation_id({"data": {"generate": {"generationId": "gen-9"}}}) == "gen-9"


def test_parse_generation_id_raises_on_error():
    with pytest.raises(LeonardoError):
        parse_generation_id({"errors": [{"message": "quota exhausted"}]})


def test_status_and_feed_query_shape():
    assert build_status_query("gen-1")["operationName"] == "GetAIGenerationFeedStatuses"
    assert build_status_query("gen-1")["variables"]["where"]["id"]["_eq"] == "gen-1"
    assert build_feed_query("gen-1")["operationName"] == "GetAIGenerationFeed"


def test_parse_status():
    resp = {"data": {"generations": [{"id": "g", "status": "COMPLETE"}]}}
    assert parse_generation_status(resp) == "COMPLETE"


def test_parse_status_pending_when_empty():
    assert parse_generation_status({"data": {"generations": []}}) == "PENDING"
    assert parse_generation_status({}) == "PENDING"


def test_parse_image_urls():
    resp = {"data": {"generations": [{"generated_images": [
        {"url": "https://cdn/x1.jpg"}, {"url": None}, {"url": "https://cdn/x2.jpg"}]}]}}
    assert parse_image_urls(resp) == ["https://cdn/x1.jpg", "https://cdn/x2.jpg"]


def test_parse_image_urls_empty():
    assert parse_image_urls({"data": {"generations": []}}) == []


def test_create_generation_uses_gql_and_returns_id():
    seen = {}

    def fake_gql(token, payload):
        seen["op"] = payload["operationName"]
        return {"data": {"generate": {"generationId": "gen-42"}}}

    client = LeonardoClient(gql=fake_gql)
    gid = client.create_generation("TOK", "a cat", "M1", "1:1")
    assert gid == "gen-42"
    assert seen["op"] == "Generate"


def test_create_generation_preserves_id_from_partial_graphql_response():
    response = {
        "data": {
            "generate": {
                "generationId": "gen-already-created",
                "apiCreditCost": 1,
                "__typename": "GenerationResponse",
            }
        },
        "errors": [{"message": "internal resolver error"}],
    }
    client = LeonardoClient(gql=lambda token, payload: response)

    assert (
        client.create_generation("TOK", "a cat", "M1", "1:1")
        == "gen-already-created"
    )


def test_generate_graphql_error_without_id_is_retry_unsafe():
    from core.leonardo_generation import classify_leonardo_error

    response = {"data": {"generate": None}, "errors": [{"message": "internal resolver error"}]}
    client = LeonardoClient(gql=lambda token, payload: response)

    with pytest.raises(LeonardoError) as excinfo:
        client.create_generation("TOK", "a cat", "M1", "1:1")

    assert classify_leonardo_error(excinfo.value) == "unsafe"


def test_generate_response_without_id_is_retry_unsafe():
    from core.leonardo_generation import classify_leonardo_error

    client = LeonardoClient(gql=lambda token, payload: {"data": {"generate": {}}})

    with pytest.raises(LeonardoError) as excinfo:
        client.create_generation("TOK", "a cat", "M1", "1:1")

    assert classify_leonardo_error(excinfo.value) == "unsafe"


@pytest.mark.parametrize(
    "message",
    [
        "credits service temporarily unavailable",
        "quota service temporarily unavailable",
        "JWT verification service temporarily unavailable",
    ],
)
def test_ambiguous_generate_graphql_error_keywords_remain_retry_unsafe(message):
    from core.leonardo_generation import classify_leonardo_error

    response = {"errors": [{"message": message}]}
    client = LeonardoClient(gql=lambda token, payload: response)

    with pytest.raises(LeonardoError) as excinfo:
        client.create_generation("TOK", "a cat", "M1", "1:1")

    assert classify_leonardo_error(excinfo.value) == "unsafe"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("invalid-jwt", "auth"), ("insufficient-credits", "quota")],
)
def test_generate_graphql_definitive_rejection_codes_are_retryable(code, expected):
    from core.leonardo_generation import classify_leonardo_error

    response = {
        "errors": [
            {"message": "request rejected", "extensions": {"code": code}}
        ]
    }
    client = LeonardoClient(gql=lambda token, payload: response)

    with pytest.raises(LeonardoError) as excinfo:
        client.create_generation("TOK", "a cat", "M1", "1:1")

    assert classify_leonardo_error(excinfo.value) == expected


def test_get_credits_reports_generation_usable_only():
    # 只有 apiCredit 时出图可用额度为 0（该通道出图扣不到）
    client = LeonardoClient(gql=lambda t, p: {"data": {"user_details": [{"apiCredit": 8500}]}})
    assert client.get_credits("TOK") == 0
    client2 = LeonardoClient(
        gql=lambda t, p: {"data": {"user_details": [{"subscriptionTokens": 850, "apiCredit": 8500}]}}
    )
    assert client2.get_credits("TOK") == 850


def test_wait_for_completion_polls_then_succeeds():
    seq = iter(["PENDING", "COMPLETED"])

    def fake_gql(token, payload):
        op = payload["operationName"]
        if op == "GetAIGenerationFeedStatuses":
            return {"data": {"generations": [{"id": "g", "status": next(seq)}]}}
        if op == "GetAIGenerationFeed":
            return {"data": {"generations": [{"generated_images": [{"url": "https://cdn/final.jpg"}]}]}}
        return {}

    client = LeonardoClient(gql=fake_gql)
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    result = client.wait_for_completion("TOK", "g", timeout=60, poll_interval=1,
                                        sleep=lambda _s: None, now=lambda: next(ticks))
    assert result == {"success": True, "images": ["https://cdn/final.jpg"]}


def test_wait_for_completion_failed_status():
    client = LeonardoClient(gql=lambda t, p: {"data": {"generations": [{"status": "FAILED"}]}})
    result = client.wait_for_completion("TOK", "g", timeout=60, poll_interval=1,
                                        sleep=lambda _s: None, now=lambda: 0.0)
    assert result["success"] is False


def test_wait_for_completion_timeout():
    client = LeonardoClient(gql=lambda t, p: {"data": {"generations": [{"status": "PENDING"}]}})
    ticks = iter([0.0, 100.0, 200.0])
    result = client.wait_for_completion("TOK", "g", timeout=30, poll_interval=1,
                                        sleep=lambda _s: None, now=lambda: next(ticks))
    assert result["success"] is False


def test_graphql_url_constant():
    assert GRAPHQL_URL == "https://api.leonardo.ai/v1/graphql"


def test_wait_for_completion_accepts_COMPLETE_status():
    def fake_gql(token, payload):
        op = payload["operationName"]
        if op == "GetAIGenerationFeedStatuses":
            return {"data": {"generations": [{"id": "g", "status": "COMPLETE"}]}}
        if op == "GetAIGenerationFeed":
            return {
                "data": {
                    "generations": [
                        {"generated_images": [{"url": "https://cdn/x.jpg"}]}
                    ]
                }
            }
        return {}

    client = LeonardoClient(gql=fake_gql)
    ticks = iter([0.0, 1.0, 61.0])
    result = client.wait_for_completion(
        "TOK",
        "g",
        timeout=60,
        poll_interval=1,
        sleep=lambda _s: None,
        now=lambda: next(ticks),
    )
    assert result == {"success": True, "images": ["https://cdn/x.jpg"]}


def test_call_raises_on_graphql_errors():
    err = {
        "errors": [
            {
                "message": "Could not verify JWT: JWSError JWSInvalidSignature",
                "extensions": {"code": "invalid-jwt"},
            }
        ]
    }
    client = LeonardoClient(gql=lambda token, payload: err)

    with pytest.raises(LeonardoError, match="Could not verify JWT"):
        client.get_credits("TOK")
    with pytest.raises(LeonardoError, match="Could not verify JWT"):
        client.poll_status("TOK", "g")


def test_http_gql_uses_only_leonardo_proxy(monkeypatch):
    captured = {}

    class _R:
        ok = True

        def json(self):
            return {"data": {"user_details": []}}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return _R()

    monkeypatch.setenv("HTTP_PROXY", "http://wrong-global:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong-global:8080")
    monkeypatch.setenv("LEONARDO_PROXY", "http://leo-proxy:10809")
    session = _install_http_session(monkeypatch, fake_post)

    lc.LeonardoClient()._http_gql("TOK", lc.TOKEN_BALANCE_QUERY)

    assert session.trust_env is False
    assert captured["proxies"] == {
        "http": "http://leo-proxy:10809",
        "https": "http://leo-proxy:10809",
    }
    assert session.closed is True


def test_http_gql_ignores_environment_proxy_when_dedicated_proxy_is_empty(
    monkeypatch,
):
    captured = {}

    class _R:
        ok = True

        def json(self):
            return {"data": {"user_details": []}}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return _R()

    monkeypatch.setenv("HTTP_PROXY", "http://wrong-global:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong-global:8080")
    monkeypatch.delenv("LEONARDO_PROXY", raising=False)
    session = _install_http_session(monkeypatch, fake_post)

    lc.LeonardoClient()._http_gql("TOK", lc.TOKEN_BALANCE_QUERY)

    assert session.trust_env is False
    assert captured["proxies"] is None
    assert session.closed is True


def test_http_gql_retries_transient_query_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    class _R:
        ok = True

        def json(self):
            return {"data": {"user_details": [{"apiCredit": 1}]}}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lc.requests.exceptions.ConnectionError("transient boom")
        return _R()

    _install_http_session(monkeypatch, fake_post)
    monkeypatch.setattr(lc.time, "sleep", sleeps.append)

    out = lc.LeonardoClient()._http_gql("TOK", lc.TOKEN_BALANCE_QUERY)

    assert out == {"data": {"user_details": [{"apiCredit": 1}]}}
    assert calls["n"] == 2
    assert sleeps == [0.5]


def test_http_gql_raises_after_exhausting_query_retries(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls["n"] += 1
        raise lc.requests.exceptions.ConnectionError("down")

    _install_http_session(monkeypatch, fake_post)
    monkeypatch.setattr(lc.time, "sleep", sleeps.append)

    with pytest.raises(LeonardoError, match="after 3 attempts: down"):
        lc.LeonardoClient()._http_gql("TOK", lc.TOKEN_BALANCE_QUERY)
    assert calls["n"] == 3
    assert sleeps == [0.5, 0.5]


def test_http_gql_query_retries_stop_at_absolute_deadline(monkeypatch):
    clock = {"t": 100.0}
    timeouts = []
    sleeps = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        timeouts.append(timeout)
        clock["t"] += timeout
        raise lc.requests.exceptions.ReadTimeout("slow poll")

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    _install_http_session(monkeypatch, fake_post)
    monkeypatch.setattr(lc.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(lc.time, "sleep", fake_sleep)

    with pytest.raises(LeonardoError, match="deadline"):
        lc.LeonardoClient()._http_gql(
            "TOK", build_status_query("gen-1"), deadline=130.0
        )

    assert timeouts == [pytest.approx(30.0)]
    assert sleeps == []
    assert clock["t"] == pytest.approx(130.0)


def test_http_gql_deadline_is_total_timeout_for_trickling_response(monkeypatch):
    payload = b'{"data":{"generations":[]}}'
    server, thread, url = _start_trickle_server("POST", payload)
    monkeypatch.setattr(lc, "GRAPHQL_URL", url)
    started = time.monotonic()
    elapsed = None
    try:
        with pytest.raises(LeonardoError, match="deadline"):
            lc.LeonardoClient()._http_gql(
                "TOK",
                build_status_query("gen-1"),
                deadline=started + 0.15,
            )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert elapsed is not None and elapsed < 0.4


def test_http_gql_does_not_retry_generate_mutation(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls["n"] += 1
        raise lc.requests.exceptions.ConnectionError("connection lost")

    _install_http_session(monkeypatch, fake_post)
    monkeypatch.setattr(lc.time, "sleep", sleeps.append)
    payload = {
        "operationName": "Generate",
        "query": "mutation Generate { generate { generationId } }",
    }

    with pytest.raises(
        LeonardoError, match="not retried to avoid duplicate side effects"
    ):
        lc.LeonardoClient()._http_gql("TOK", payload)
    assert calls["n"] == 1
    assert sleeps == []


def test_http_gql_single_shot_transport_raises_retry_unsafe(monkeypatch):
    """单发(Generate)传输失败 → LeonardoRetryUnsafeError，禁止自动重试。"""
    calls = {"n": 0}
    monkeypatch.setattr(lc.time, "sleep", lambda _s: None)

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls["n"] += 1
        raise lc.requests.exceptions.ConnectionError("connection lost")

    _install_http_session(monkeypatch, fake_post)
    payload = {
        "operationName": "Generate",
        "query": "mutation Generate { generate { generationId } }",
    }
    with pytest.raises(
        lc.LeonardoRetryUnsafeError, match="not retried to avoid duplicate side effects"
    ):
        lc.LeonardoClient()._http_gql("TOK", payload)
    assert calls["n"] == 1


def test_http_gql_retryable_op_transport_exhaustion_is_plain_error(monkeypatch):
    """只读查询传输耗尽 → 仍抛普通 LeonardoError（不可重试语义只在单发场景）。"""
    calls = {"n": 0}
    monkeypatch.setattr(lc.time, "sleep", lambda _s: None)

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls["n"] += 1
        raise lc.requests.exceptions.ConnectionError("down")

    _install_http_session(monkeypatch, fake_post)
    with pytest.raises(lc.LeonardoError) as excinfo:
        lc.LeonardoClient()._http_gql("TOK", lc.TOKEN_BALANCE_QUERY)
    assert not isinstance(excinfo.value, lc.LeonardoRetryUnsafeError)
    assert calls["n"] == 3


def test_http_gql_single_shot_http_error_is_retry_unsafe(monkeypatch):
    """单发操作 HTTP 错误也可能已生效 → LeonardoRetryUnsafeError。"""
    class _R:
        ok = False
        status_code = 500

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        return _R()

    _install_http_session(monkeypatch, fake_post)
    payload = {
        "operationName": "Generate",
        "query": "mutation Generate { generate { generationId } }",
    }
    with pytest.raises(lc.LeonardoRetryUnsafeError, match="graphql HTTP 500"):
        lc.LeonardoClient()._http_gql("TOK", payload)
