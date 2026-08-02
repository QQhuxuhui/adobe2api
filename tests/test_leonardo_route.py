import base64
import time
from unittest.mock import MagicMock

import pytest

import requests as req_mod

from core.adobe_client import (
    AdobeRequestError,
    AuthError,
    QuotaExhaustedError,
    UpstreamTemporaryError,
)
from core.leonardo_client import LeonardoError
from core.leonardo_generation import LeonardoGenerationError
from api.routes.generation import (
    _build_leonardo_run_once,
    _fetch_cdn_image,
    _map_leonardo_error,
)
import api.routes.generation as gen_mod


# --- 异常映射 ---

@pytest.mark.parametrize("message,expected_cls", [
    ("Could not verify JWT: JWSError JWSInvalidSignature", AuthError),
    ("invalid token", AuthError),
    ("unauthorized", AuthError),
    ("insufficient balance", QuotaExhaustedError),
    ("token balance exhausted", QuotaExhaustedError),
    ("graphql HTTP 500", UpstreamTemporaryError),
    ("graphql GetTokenBalance failed after 3 attempts: connection reset", UpstreamTemporaryError),
])
def test_leonardo_error_mapping(message, expected_cls):
    mapped = _map_leonardo_error(LeonardoError(message))
    assert type(mapped) is expected_cls


def test_generation_error_maps_to_non_retryable():
    # 已提交后的失败不可换号重试（会重复扣费）→ AdobeRequestError → 500
    mapped = _map_leonardo_error(LeonardoGenerationError("generation timeout"))
    assert isinstance(mapped, AdobeRequestError)
    mapped = _map_leonardo_error(LeonardoGenerationError("generation failed"))
    assert isinstance(mapped, AdobeRequestError)


# --- _build_leonardo_run_once 成功路径 ---

class _FakeLeoClient:
    def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1):
        return "gen-abc"


def _fake_img_resp(content: bytes = b"\x89PNG fake image bytes"):
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    return resp


def _build_run_once(**overrides):
    base = {
        "leo_client": _FakeLeoClient(),
        "request": MagicMock(),
        "prompt": "a red fox",
        "model_id": "7418e71f-4133-4e1b-9895-bee19f48f2ce",
        "size": None,
        "aspect_ratio": "1:1",
        "n": 1,
        "timeout": 300,
        "response_format": "b64_json",
        "resolved_model_id": "leonardo-nano-banana-2",
        "output_resolution": "2K",
        "public_image_url": lambda req, job_id: f"https://example.com/generated/{job_id}",
        "generated_dir": None,
        "on_generated_file_written": None,
        "set_request_preview": None,
    }
    base.update(overrides)
    return _build_leonardo_run_once(**base)


def test_run_once_success_b64(monkeypatch):
    import core.leonardo_generation as lg

    def fake_generate_images(**kw):
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/generations/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-abc", "aspect_ratio": "1:1", "model_id": "x"},
        }

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", lambda url, timeout, headers: _fake_img_resp())

    result = _build_run_once()("leo-token")
    assert result["model"] == "leonardo-nano-banana-2"
    assert len(result["data"]) == 1
    assert result["data"][0]["b64_json"] == base64.b64encode(b"\x89PNG fake image bytes").decode()
    assert result["data"][0]["revised_prompt"] == "a red fox"
    assert result["usage"]["total_tokens"] > 0


def test_run_once_success_url(monkeypatch, tmp_path):
    import core.leonardo_generation as lg

    def fake_generate_images(**kw):
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/generations/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-abc", "aspect_ratio": "1:1", "model_id": "x"},
        }

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", lambda url, timeout, headers: _fake_img_resp())

    gen_dir = tmp_path / "generated"
    gen_dir.mkdir()
    run_once = _build_run_once(
        response_format="url", generated_dir=gen_dir,
        on_generated_file_written=lambda p, a, b: None,
    )
    result = run_once("leo-token")
    assert result["data"][0]["url"].startswith("https://example.com/generated/")
    assert len(list(gen_dir.iterdir())) == 1  # CDN 图片确实落盘


def test_run_once_url_file_matches_served_url(monkeypatch, tmp_path):
    # 真实 public_image_url 在 job_id 后固定加 .png；落盘文件名必须与被返回的
    # url 引用名一致，否则 /generated/{filename} 命中不到 → 404。
    import core.leonardo_generation as lg

    def fake_generate_images(**kw):
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/generations/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-abc", "aspect_ratio": "1:1", "model_id": "x"},
        }

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", lambda url, timeout, headers: _fake_img_resp())

    gen_dir = tmp_path / "generated"
    gen_dir.mkdir()
    run_once = _build_run_once(
        response_format="url",
        generated_dir=gen_dir,
        public_image_url=lambda req, job_id: f"https://example.com/generated/{job_id}.png",
        on_generated_file_written=lambda p, a, b: None,
    )
    result = run_once("leo-token")
    served_name = result["data"][0]["url"].rsplit("/", 1)[1]
    on_disk = [p.name for p in gen_dir.iterdir()]
    assert served_name in on_disk


def test_run_once_auth_error_mapped(monkeypatch):
    import core.leonardo_generation as lg

    def boom(**kw):
        raise LeonardoError("Could not verify JWT: JWSError JWSInvalidSignature")

    monkeypatch.setattr(lg, "generate_images", boom)
    with pytest.raises(AuthError):
        _build_run_once()("leo-token")


def test_run_once_generation_error_mapped_non_retryable(monkeypatch):
    import core.leonardo_generation as lg

    def boom(**kw):
        raise LeonardoGenerationError("generation timeout")

    monkeypatch.setattr(lg, "generate_images", boom)
    with pytest.raises(AdobeRequestError):
        _build_run_once()("leo-token")


def test_run_once_cdn_fetch_failure_non_retryable(monkeypatch):
    import core.leonardo_generation as lg

    def fake_generate_images(**kw):
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/generations/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-abc", "aspect_ratio": "1:1", "model_id": "x"},
        }

    def boom(url, timeout, headers):
        raise req_mod.exceptions.ConnectionError("cdn down")

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", boom)
    monkeypatch.setattr(gen_mod.time, "sleep", lambda s: None)
    # 生成已成功但下载即使重试仍失败 → 不可重试重发生成 → AdobeRequestError
    with pytest.raises(AdobeRequestError):
        _build_run_once()("leo-token")


# --- CDN 下载重试（下载幂等，不涉及重复扣费） ---

def test_fetch_cdn_image_uses_extended_timeout(monkeypatch):
    seen = {}

    def capture_get(url, timeout, headers):
        seen["timeout"] = timeout
        return _fake_img_resp()

    monkeypatch.setattr(req_mod, "get", capture_get)
    _fetch_cdn_image("https://cdn.leonardo.ai/x.jpg", {"Accept": "image/*"})
    assert seen["timeout"] >= 120


def test_fetch_cdn_image_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky_get(url, timeout, headers):
        calls["n"] += 1
        if calls["n"] < 3:
            raise req_mod.exceptions.ReadTimeout("slow cdn")
        return _fake_img_resp(b"ok-bytes")

    monkeypatch.setattr(req_mod, "get", flaky_get)
    monkeypatch.setattr(gen_mod.time, "sleep", lambda s: None)
    resp = _fetch_cdn_image("https://cdn.leonardo.ai/x.jpg", {})
    assert resp.content == b"ok-bytes"
    assert calls["n"] == 3


def test_fetch_cdn_image_exhausts_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def always_fail(url, timeout, headers):
        calls["n"] += 1
        raise req_mod.exceptions.ConnectionError("cdn down")

    monkeypatch.setattr(req_mod, "get", always_fail)
    monkeypatch.setattr(gen_mod.time, "sleep", lambda s: None)
    with pytest.raises(req_mod.exceptions.ConnectionError):
        _fetch_cdn_image("https://cdn.leonardo.ai/x.jpg", {})
    assert calls["n"] == 3


def test_retry_unsafe_error_maps_to_non_retryable():
    from core.leonardo_client import LeonardoRetryUnsafeError
    mapped = _map_leonardo_error(
        LeonardoRetryUnsafeError(
            "graphql Generate failed; request not retried to avoid duplicate side effects: connection lost"
        )
    )
    assert isinstance(mapped, AdobeRequestError)


def test_generate_poll_error_now_maps_non_retryable_via_generation_error():
    """轮询期错误经 generate_images 转 LeonardoGenerationError 后 → 非重试。"""
    from core.leonardo_generation import LeonardoGenerationError
    mapped = _map_leonardo_error(
        LeonardoGenerationError(
            "graphql GetAIGenerationFeedStatuses failed after 3 attempts: connection reset"
        )
    )
    assert isinstance(mapped, AdobeRequestError)
