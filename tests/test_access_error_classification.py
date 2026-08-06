"""401/403 的 x-access-error 分类。

事故背景：Adobe 现网返回的配额码是 quota_exhausted，而代码只认旧码 taste_exhausted，
于是配额耗尽被当成凭证失效 —— 账号不出池、反而每次都去刷一遍 cookie，
单个 edits 请求要在死号池里全量试错，被拖到 4~8 分钟。
"""

import logging

import pytest

from core.adobe_client import (
    AuthError,
    QuotaExhaustedError,
    access_error_of,
    raise_for_access_error,
)


class FakeResponse:
    def __init__(self, status_code=403, headers=None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self.text = "{}"


class HeadersRaise:
    """headers 访问即抛错的响应，模拟异常客户端对象。"""

    status_code = 403

    @property
    def headers(self):
        raise RuntimeError("headers unavailable")


@pytest.mark.parametrize("code", ["quota_exhausted", "taste_exhausted"])
@pytest.mark.parametrize("status", [401, 403])
def test_quota_codes_raise_quota_exhausted(code, status):
    resp = FakeResponse(status, {"x-access-error": code})
    with pytest.raises(QuotaExhaustedError):
        raise_for_access_error(resp)


@pytest.mark.parametrize(
    "raw",
    ["QUOTA_EXHAUSTED", " quota_exhausted ", "Taste_Exhausted"],
)
def test_quota_codes_are_normalized(raw):
    with pytest.raises(QuotaExhaustedError):
        raise_for_access_error(FakeResponse(403, {"x-access-error": raw}))


def test_missing_header_is_auth_error_not_typeerror():
    # 视频 submit 分支曾对 None 直接比较；这里确认缺头不会炸成 TypeError
    with pytest.raises(AuthError):
        raise_for_access_error(FakeResponse(403, {}))


def test_none_header_value_is_auth_error():
    with pytest.raises(AuthError):
        raise_for_access_error(FakeResponse(401, {"x-access-error": None}))


def test_unknown_code_is_auth_error_and_logged(caplog):
    """未知码按凭证失效处理，但必须留日志——Adobe 再改码时靠它发现。"""
    with caplog.at_level(logging.WARNING, logger="adobe2api"):
        with pytest.raises(AuthError):
            raise_for_access_error(
                FakeResponse(403, {"x-access-error": "some_new_code"}), "image.submit"
            )
    assert any("some_new_code" in r.getMessage() for r in caplog.records)


def test_known_codes_do_not_log_unknown_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="adobe2api"):
        with pytest.raises(QuotaExhaustedError):
            raise_for_access_error(FakeResponse(403, {"x-access-error": "quota_exhausted"}))
    assert not any("unknown x-access-error" in r.message for r in caplog.records)


def test_substring_match_is_not_used():
    """不做子串模糊匹配：含 exhausted 字样但语义不同的码不能被误判成配额耗尽。"""
    with pytest.raises(AuthError):
        raise_for_access_error(FakeResponse(403, {"x-access-error": "session_exhausted_retry"}))


def test_access_error_of_survives_broken_headers():
    assert access_error_of(HeadersRaise()) == ""
    with pytest.raises(AuthError):
        raise_for_access_error(HeadersRaise())


# --- 生产调用点确实接上了统一分类 ---


def _client():
    from core.adobe_client import AdobeClient

    return AdobeClient()


def test_image_submit_uses_unified_classifier(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "_post_json", lambda *a, **k: FakeResponse(403, {"x-access-error": "quota_exhausted"})
    )
    monkeypatch.setattr(client, "_submit_candidates", lambda *a, **k: [{}], raising=False)
    with pytest.raises(QuotaExhaustedError):
        client.generate(token="t", prompt="p")


def test_video_submit_uses_unified_classifier(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "_post_json", lambda *a, **k: FakeResponse(403, {"x-access-error": "quota_exhausted"})
    )
    with pytest.raises(QuotaExhaustedError):
        client.generate_video(token="t", video_conf={}, prompt="p")


def test_upload_image_uses_unified_classifier(monkeypatch):
    """edits 链路首跳：配额耗尽在上传阶段就会暴露。"""
    client = _client()
    monkeypatch.setattr(
        client, "_post_bytes", lambda *a, **k: FakeResponse(403, {"x-access-error": "quota_exhausted"})
    )
    with pytest.raises(QuotaExhaustedError):
        client.upload_image("t", b"\x00", "image/png")


def test_upload_image_without_access_error_still_auth_error(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_post_bytes", lambda *a, **k: FakeResponse(401, {}))
    with pytest.raises(AuthError):
        client.upload_image("t", b"\x00", "image/png")
