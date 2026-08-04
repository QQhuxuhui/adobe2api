"""429 不再被自己放大：候选载荷循环遇到限流要立刻停手，并把 Retry-After 带出来。

`generate()` 会依次尝试多个候选载荷，用来绕开「载荷形状被上游拒绝」的情况。
但 429 跟载荷形状无关——继续换形状重试只会在毫秒内把同一个账号再打几次，
反而把限流打得更死。
"""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.adobe_client import (
    AdobeClient,
    UpstreamTemporaryError,
    retry_after_seconds,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        return {}


def make_client() -> AdobeClient:
    client = AdobeClient.__new__(AdobeClient)
    client.api_key = "test-key"
    client.impersonate = "chrome124"
    client.proxy = ""
    client.user_agent = "test-agent"
    client.sec_ch_ua = '"Chromium";v="124"'
    client.gpt_image_quality = "low"
    client.submit_url = "https://example.invalid/submit"
    return client


# gpt-image 的图生图会产生 3 个候选载荷（subject / referenceImages / localBlobRef），
# 是本仓库里唯一会多次提交的形状，也就是 429 会被放大 3 倍的那条路径。
MULTI_CANDIDATE = dict(
    aspect_ratio="1:1",
    output_resolution="2K",
    upstream_model_id="gpt-image",
    upstream_model_version="v1",
    source_image_ids=["img1"],
)


def _run_generate(monkeypatch, responses, **overrides):
    """跑一次 generate()，返回 (抛出的异常, 实际发出的请求次数)。"""
    client = make_client()
    calls = []

    def fake_post_json(url, headers, payload, deadline=None):
        calls.append(payload)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(client, "_post_json", fake_post_json)
    monkeypatch.setattr(client, "_submit_headers", lambda token, prompt=None: {})

    kwargs = {**MULTI_CANDIDATE, **overrides}
    with pytest.raises(Exception) as excinfo:
        client.generate(token="tok", prompt="hello", **kwargs)
    return excinfo.value, len(calls)


def test_the_fixture_really_produces_multiple_candidates(monkeypatch):
    """守住上面几个测试的前提：这个配置必须真的会多次提交，否则它们无意义。"""
    _, calls = _run_generate(monkeypatch, [FakeResponse(status_code=400, text="bad shape")])
    assert calls == 3


def test_rate_limit_stops_the_candidate_loop_immediately(monkeypatch):
    exc, calls = _run_generate(monkeypatch, [FakeResponse(status_code=429, text="slow down")])
    assert calls == 1, "429 之后不该再拿同一个 token 试别的候选载荷"
    assert isinstance(exc, UpstreamTemporaryError)
    assert exc.status_code == 429


def test_server_error_also_stops_the_candidate_loop(monkeypatch):
    _, calls = _run_generate(monkeypatch, [FakeResponse(status_code=503, text="oops")])
    assert calls == 1


def test_payload_rejection_still_tries_other_candidates(monkeypatch):
    """400 才是候选载荷机制真正要处理的情况，不能被上面的改动误伤。"""
    responses = [
        FakeResponse(status_code=400, text="bad shape"),
        FakeResponse(status_code=400, text="bad shape"),
        FakeResponse(status_code=200),
    ]
    client = make_client()
    calls = []

    def fake_post_json(url, headers, payload, deadline=None):
        calls.append(payload)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(client, "_post_json", fake_post_json)
    monkeypatch.setattr(client, "_submit_headers", lambda token, prompt=None: {})
    monkeypatch.setattr(
        client, "_poll_and_download", lambda *a, **k: (b"img", {}), raising=False
    )

    try:
        client.generate(token="tok", prompt="hello", **MULTI_CANDIDATE)
    except Exception:
        pass
    assert len(calls) == 3, "载荷形状被拒时必须继续试下一个候选"


def test_rate_limited_error_carries_retry_after(monkeypatch):
    exc, _ = _run_generate(
        monkeypatch,
        [FakeResponse(status_code=429, headers={"retry-after": "120"}, text="slow down")],
    )
    assert exc.retry_after == 120.0


def test_error_without_retry_after_leaves_it_none(monkeypatch):
    exc, _ = _run_generate(monkeypatch, [FakeResponse(status_code=429, text="slow down")])
    assert exc.retry_after is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("120", 120.0),
        ("0.5", 0.5),
        ("", None),
        ("0", None),
        ("-5", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # HTTP-date 形式不解析
        (None, None),
    ],
)
def test_retry_after_seconds_parsing(raw, expected):
    headers = {} if raw is None else {"retry-after": raw}
    assert retry_after_seconds(FakeResponse(headers=headers)) == expected


def test_retry_after_seconds_tolerates_missing_headers():
    class NoHeaders:
        pass

    assert retry_after_seconds(NoHeaders()) is None
