import base64
import json
import threading
import time

import pytest

from core.token_mgr import TokenManager


def _jwt(payload: dict) -> str:
    """构造可被 base64 解码的 JWT（header.sig 用固定占位，payload 是真实 base64url）。"""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"x.{body}.sig"


def _leonardo_cognito_jwt() -> str:
    return _jwt({
        "sub": "a6fbcd6a-039f-445c-83e6-6822b7e113d5",   # Cognito user UUID
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123",
        "cognito:username": "canva-user-123",
        "exp": int(time.time()) + 3600,
    })


def _adobe_jwt() -> str:
    return _jwt({
        "user_id": "ADOBE_USER_123",
        "exp": int(time.time()) + 3600,
    })


@pytest.fixture
def fresh_tm(tmp_path, monkeypatch):
    import core.token_mgr as tm_mod
    monkeypatch.setattr(tm_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tm_mod, "DATA_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(tm_mod, "LEGACY_DATA_FILE", tmp_path / "tokens_legacy.json")
    return TokenManager()


def test_add_auto_tags_leonardo_token(fresh_tm):
    token = _leonardo_cognito_jwt()
    t = fresh_tm.add(token)
    assert t.get("type") == "leonardo"


def test_add_does_not_tag_adobe_token(fresh_tm):
    t = fresh_tm.add(_adobe_jwt())
    assert t.get("type") != "leonardo"


def test_add_meta_type_overrides_auto_detect(fresh_tm):
    t = fresh_tm.add(_leonardo_cognito_jwt(), meta={"type": "custom"})
    assert t.get("type") == "custom"


def test_upsert_auto_refresh_does_not_tag_leonardo(fresh_tm):
    t = fresh_tm.upsert_auto_refresh_token(_leonardo_cognito_jwt(), profile_id="p1")
    assert t.get("type") != "leonardo"


def test_get_available_leonardo_type_filter(fresh_tm):
    leo = fresh_tm.add(_leonardo_cognito_jwt())
    fresh_tm.add(_adobe_jwt())
    result = fresh_tm.get_available(token_type="leonardo")
    assert result == leo["value"]


def test_get_available_default_excludes_leonardo(fresh_tm):
    fresh_tm.add(_leonardo_cognito_jwt())
    adobe = fresh_tm.add(_adobe_jwt())
    result = fresh_tm.get_available()  # 默认 adobe，排除 leonardo
    assert result == adobe["value"]


def test_get_available_none_filter_returns_any(fresh_tm):
    leo = fresh_tm.add(_leonardo_cognito_jwt())
    result = fresh_tm.get_available(token_type=None)  # 显式不过滤
    assert result == leo["value"]


def test_get_available_leonardo_filter_returns_none_when_no_match(fresh_tm):
    fresh_tm.add(_adobe_jwt())
    result = fresh_tm.get_available(token_type="leonardo")
    assert result is None


def test_account_id_from_leonardo_token(fresh_tm):
    token = _leonardo_cognito_jwt()
    # account_id_from_token 取 sub（Cognito user UUID），稳定可去重
    assert fresh_tm.account_id_from_token(token) == "a6fbcd6a-039f-445c-83e6-6822b7e113d5"


def test_list_active_account_tokens_includes_type(fresh_tm):
    fresh_tm.add(_leonardo_cognito_jwt())
    items = fresh_tm.list_active_account_tokens()
    leo_items = [i for i in items if i.get("token", "").startswith("x.")]
    assert len(leo_items) == 1
    assert leo_items[0].get("type") == "leonardo"
