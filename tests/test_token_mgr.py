import base64
import json
import logging
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


def test_has_active_token_by_type(fresh_tm):
    # 空池
    assert fresh_tm.has_active_token() is False
    assert fresh_tm.has_active_token("leonardo") is False
    assert fresh_tm.has_active_token("adobe") is False
    # 只有 Adobe
    fresh_tm.add(_adobe_jwt())
    assert fresh_tm.has_active_token() is True
    assert fresh_tm.has_active_token("adobe") is True
    assert fresh_tm.has_active_token("leonardo") is False
    # 再加 Leonardo → 两类都有
    fresh_tm.add(_leonardo_cognito_jwt())
    assert fresh_tm.has_active_token("leonardo") is True
    assert fresh_tm.has_active_token("adobe") is True


def test_add_meta_type_overrides_auto_detect(fresh_tm):
    t = fresh_tm.add(_leonardo_cognito_jwt(), meta={"type": "custom"})
    assert t.get("type") == "custom"


def test_upsert_auto_refresh_does_not_tag_leonardo(fresh_tm):
    t = fresh_tm.upsert_auto_refresh_token(_leonardo_cognito_jwt(), profile_id="p1")
    assert t.get("type") != "leonardo"


def test_upsert_leonardo_token_creates_typed_active_record(fresh_tm):
    token = _jwt({"sub": "leo-1", "exp": 2000})

    result = fresh_tm.upsert_leonardo_token(token, "leo-1", "Primary")

    assert result["status"] == "created"
    assert result["token"]["type"] == "leonardo"
    assert result["token"]["source"] == "leonardo_refresher"
    assert result["token"]["refresh_profile_name"] == "Primary"
    assert result["token"]["status"] == "active"
    assert result["token"]["account_id"] == "leo-1"


def test_upsert_leonardo_token_updates_and_resets_status(fresh_tm):
    old = _jwt({"sub": "leo-1", "exp": 2000})
    new = _jwt({"sub": "leo-1", "exp": 3000})
    item = fresh_tm.add(
        old,
        meta={"type": "leonardo", "status": "invalid", "fails": 3},
    )

    result = fresh_tm.upsert_leonardo_token(new, "leo-1", "Primary")

    assert result["status"] == "updated"
    assert result["token"]["id"] == item["id"]
    assert result["token"]["value"] == new
    assert result["token"]["status"] == "active"
    assert result["token"]["fails"] == 0
    assert result["token"]["error_until"] == 0


def test_upsert_leonardo_token_keeps_only_newest_account_record(fresh_tm):
    fresh_tm.add(
        _jwt({"sub": "leo-1", "exp": 2000}),
        meta={"type": "leonardo"},
    )
    newest = fresh_tm.add(
        _jwt({"sub": "leo-1", "exp": 3000}),
        meta={"type": "leonardo"},
    )
    adobe = fresh_tm.add(
        _jwt({"sub": "leo-1", "exp": 4000}),
        meta={"type": "adobe"},
    )

    result = fresh_tm.upsert_leonardo_token(
        _jwt({"sub": "leo-1", "exp": 2500}),
        "leo-1",
        "Primary",
    )

    matching_leonardo = [
        item
        for item in fresh_tm.tokens
        if item.get("type") == "leonardo"
        and item.get("account_id") == "leo-1"
    ]
    assert result["status"] == "noop"
    assert result["token"]["id"] == newest["id"]
    assert len(matching_leonardo) == 1
    assert fresh_tm.get_by_id(adobe["id"]) is not None


def test_upsert_leonardo_token_is_noop_for_identical_active_record(fresh_tm):
    token = _jwt({"sub": "leo-1", "exp": 3000})
    first = fresh_tm.upsert_leonardo_token(token, "leo-1", "Primary")

    result = fresh_tm.upsert_leonardo_token(token, "leo-1", "Primary")

    assert result["status"] == "noop"
    assert result["token"] == first["token"]


def test_upsert_leonardo_token_requires_matching_account_id(fresh_tm):
    token = _jwt({"sub": "leo-1", "exp": 3000})

    with pytest.raises(ValueError, match="account_id"):
        fresh_tm.upsert_leonardo_token(token, "leo-2", "Primary")


def test_upsert_leonardo_token_dedups_and_updates_on_newer_token(fresh_tm):
    fresh_tm.add(_jwt({"sub": "leo-1", "exp": 2000}), meta={"type": "leonardo"})
    fresh_tm.add(_jwt({"sub": "leo-1", "exp": 3000}), meta={"type": "leonardo"})
    newer = _jwt({"sub": "leo-1", "exp": 4000})

    result = fresh_tm.upsert_leonardo_token(newer, "leo-1", "Primary")

    matching = [
        t for t in fresh_tm.tokens
        if t.get("type") == "leonardo" and t.get("account_id") == "leo-1"
    ]
    assert result["status"] == "updated"
    assert len(matching) == 1              # 去重成一条
    assert matching[0]["value"] == newer   # 更新为更新的 token
    assert matching[0]["status"] == "active"


def test_upsert_leonardo_token_single_record_exp_regression_is_noop(fresh_tm):
    kept = fresh_tm.upsert_leonardo_token(
        _jwt({"sub": "leo-1", "exp": 3000}), "leo-1", "Primary"
    )
    older = _jwt({"sub": "leo-1", "exp": 2000})

    result = fresh_tm.upsert_leonardo_token(older, "leo-1", "Primary")

    assert result["status"] == "noop"
    assert result["token"]["value"] == kept["token"]["value"]  # 保留更新的那条


@pytest.mark.parametrize("label", [None, ""])
def test_upsert_leonardo_token_label_falls_back_to_account_id(fresh_tm, label):
    token = _jwt({"sub": "leo-1", "exp": 3000})
    result = fresh_tm.upsert_leonardo_token(token, "leo-1", label)
    assert result["token"]["refresh_profile_name"] == "leo-1"


def test_token_save_keeps_previous_file_when_atomic_replace_fails(
    fresh_tm,
    monkeypatch,
):
    import core.token_mgr as tm_mod

    fresh_tm.add(_adobe_jwt())
    before = tm_mod.DATA_FILE.read_text(encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(tm_mod.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        fresh_tm.upsert_leonardo_token(
            _jwt({"sub": "leo-1", "exp": 3000}),
            "leo-1",
            "Primary",
        )

    assert tm_mod.DATA_FILE.read_text(encoding="utf-8") == before
    assert json.loads(before)
    assert list(tm_mod.DATA_FILE.parent.glob(".tokens.json.*.tmp")) == []
    assert [item.get("type") for item in fresh_tm.tokens] == [None]


def test_unreadable_current_token_file_is_preserved(
    tmp_path,
    monkeypatch,
    caplog,
):
    import core.token_mgr as tm_mod

    data_file = tmp_path / "tokens.json"
    data_file.write_text("{truncated", encoding="utf-8")
    monkeypatch.setattr(tm_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tm_mod, "DATA_FILE", data_file)
    monkeypatch.setattr(tm_mod, "LEGACY_DATA_FILE", tmp_path / "legacy.json")

    with caplog.at_level(logging.ERROR):
        manager = TokenManager()

    assert manager.tokens == []
    assert "refusing to overwrite" in caplog.text
    with pytest.raises(RuntimeError, match="unreadable token file"):
        manager.add(_adobe_jwt())
    assert data_file.read_text(encoding="utf-8") == "{truncated"


def test_atomic_save_preserves_existing_file_mode(fresh_tm):
    import core.token_mgr as tm_mod

    fresh_tm.add(_adobe_jwt())
    tm_mod.DATA_FILE.chmod(0o640)

    fresh_tm.upsert_leonardo_token(
        _jwt({"sub": "leo-1", "exp": 3000}),
        "leo-1",
        "Primary",
    )

    assert tm_mod.DATA_FILE.stat().st_mode & 0o777 == 0o640


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
