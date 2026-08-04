"""better-auth 会轮换 session cookie；轮换后的值必须存回去。

实际踩坑：refresher 只在指纹变化时注入「存储的原始 cookie」，之后服务端轮换了
session，浏览器里是新值但我们从不回写。容器一重启就重新注入那份已作废的原始
cookie → login_required，用户被迫反复重导账号（本会话内发生了两次）。
"""
import pytest

from leonardo_refresher.adapters import (
    Adobe2ApiCookieProvider,
    extract_session_cookie_string,
)

BETTER_AUTH = [
    {"name": "__Secure-better-auth.session_token", "value": "NEW-TOKEN"},
    {"name": "__Secure-better-auth.session_data.0", "value": "d0"},
    {"name": "__Secure-better-auth.session_data.1", "value": "d1"},
    {"name": "unrelated", "value": "x"},
]


def test_extract_only_better_auth_cookies_in_stable_order():
    s = extract_session_cookie_string(BETTER_AUTH)
    assert "unrelated" not in s
    assert s.count("__Secure-better-auth") == 3
    # 顺序稳定，便于和已存 cookie 直接比较
    assert s == extract_session_cookie_string(list(reversed(BETTER_AUTH)))


def test_extract_returns_empty_without_session_token():
    assert extract_session_cookie_string(
        [{"name": "__Secure-better-auth.session_data.0", "value": "d0"}]
    ) == ""
    assert extract_session_cookie_string([]) == ""


class _Resp:
    def __init__(self, status=200):
        self.status_code = status

    def json(self):
        return {"fingerprint": "f", "updated_at": 1}


class _Session:
    def __init__(self):
        self.trust_env = True
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _Resp()


def _provider(session):
    return Adobe2ApiCookieProvider(
        base_url="http://api:6001", refresh_key="K",
        session_factory=lambda: session,
    )


def test_store_pushes_rotated_cookie():
    session = _Session()
    p = _provider(session)
    ok = p.store("__Secure-better-auth.session_token=NEW; __Secure-better-auth.session_data.0=d0")
    assert ok is True
    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"].endswith("/api/v1/tokens/leonardo/cookie")
    assert post["headers"]["X-Leonardo-Refresh-Key"] == "K"
    assert "NEW" in post["json"]["cookie"]


def test_store_ignores_empty_cookie():
    session = _Session()
    assert _provider(session).store("") is False
    assert session.posts == []


def test_store_never_raises_on_http_error():
    class _Bad(_Session):
        def post(self, *a, **k):
            raise RuntimeError("boom")

    # 回写失败不得影响本次 token 刷新
    assert _provider(_Bad()).store("__Secure-better-auth.session_token=x") is False
