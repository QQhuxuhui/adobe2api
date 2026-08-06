"""余额驱动复活的乱序防护。

cookie 刷新不再复活配额耗尽的账号（见 test_account_quota_retirement），复活只剩
「余额回来了」这一条路。麻烦在于余额请求可能在耗尽事件之前发出、之后才返回：
那份「余额还有」的旧快照不能把刚耗尽的账号放回池子。

用账号级递增版本号（quota_epoch）定序，而不是 credits.updated_at ——
后者是 int(time.time())，秒级精度，同秒发生时根本分不出先后。
"""

import pytest

from core.token_mgr import TokenManager


@pytest.fixture
def make_tm(tmp_path, monkeypatch):
    import core.token_mgr as tm_mod

    monkeypatch.setattr(tm_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tm_mod, "DATA_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(tm_mod, "LEGACY_DATA_FILE", tmp_path / "tokens_legacy.json")
    return TokenManager


def _seed(tm, rows):
    tm.tokens = [
        {
            "id": r["id"],
            "value": r["value"],
            "status": r.get("status", "active"),
            "fails": 0,
            "added_at": 0,
            "error_until": 0,
            "last_used_at": 0,
            "type": r.get("type", "adobe"),
            "account_id": r.get("account_id", r["value"]),
        }
        for r in rows
    ]
    tm.save()
    return tm


def _pool(make_tm):
    tm = make_tm()
    return _seed(
        tm,
        [
            {"id": "a1", "value": "tok-a1", "account_id": "acct-1"},
            {"id": "a2", "value": "tok-a2", "account_id": "acct-1"},
            {"id": "b1", "value": "tok-b1", "account_id": "acct-2"},
        ],
    )


def _credits(available, **kw):
    payload = {
        "total": 1000,
        "used": 1000 - available,
        "available": available,
        "available_until": None,
        "updated_at": 1_700_000_000,
    }
    payload.update(kw)
    return payload


def _status(tm, tid):
    return next(t["status"] for t in tm.tokens if t["id"] == tid)


# --- 正常复活 ---


def test_current_epoch_credits_revive_whole_account(make_tm):
    tm = _pool(make_tm)
    tm.report_account_exhausted("acct-1")
    epoch = tm.quota_epoch("acct-1")

    tm.set_credits_and_maybe_revive("a1", _credits(500), observed_quota_epoch=epoch)

    assert _status(tm, "a1") == "active"
    assert _status(tm, "a2") == "active", "同账号另一行也要一起回池"


def test_zero_balance_does_not_revive(make_tm):
    tm = _pool(make_tm)
    tm.report_account_exhausted("acct-1")
    epoch = tm.quota_epoch("acct-1")
    tm.set_credits_and_maybe_revive("a1", _credits(0), observed_quota_epoch=epoch)
    assert _status(tm, "a1") == "exhausted"


def test_missing_epoch_does_not_revive(make_tm):
    """调用方没提供版本时保守处理：宁可不复活。"""
    tm = _pool(make_tm)
    tm.report_account_exhausted("acct-1")
    tm.set_credits_and_maybe_revive("a1", _credits(500), observed_quota_epoch=None)
    assert _status(tm, "a1") == "exhausted"


def test_plain_set_credits_never_revives(make_tm):
    tm = _pool(make_tm)
    tm.report_account_exhausted("acct-1")
    tm.set_credits("a1", _credits(500))
    assert _status(tm, "a1") == "exhausted"


# --- 乱序防护 ---


def test_stale_snapshot_cannot_revive(make_tm):
    """时序：读到余额>0 → 期间撞配额耗尽 → 旧结果才落盘。不得复活。"""
    tm = _pool(make_tm)
    epoch_at_request = tm.quota_epoch("acct-1")  # 查询前

    tm.report_account_exhausted("acct-1")  # 查询在飞期间发生

    tm.set_credits_and_maybe_revive(
        "a1", _credits(800), observed_quota_epoch=epoch_at_request
    )
    assert _status(tm, "a1") == "exhausted"
    assert _status(tm, "a2") == "exhausted"


def test_same_second_events_are_ordered_by_epoch(make_tm):
    """时间戳方案会漏的用例：耗尽与余额刷新发生在同一秒。"""
    tm = _pool(make_tm)
    epoch_at_request = tm.quota_epoch("acct-1")
    tm.report_account_exhausted("acct-1")

    same_second = 1_700_000_000
    for t in tm.tokens:
        if t["id"] == "a1":
            t["quota_exhausted_at_probe"] = same_second

    tm.set_credits_and_maybe_revive(
        "a1",
        _credits(800, updated_at=same_second),
        observed_quota_epoch=epoch_at_request,
    )
    assert _status(tm, "a1") == "exhausted", "同秒也必须按版本号判定，不能靠时间戳"


def test_snapshot_epoch_is_recorded_for_fast_path(make_tm):
    tm = _pool(make_tm)
    stale = tm.quota_epoch("acct-1")
    tm.report_account_exhausted("acct-1")
    tm.set_credits_and_maybe_revive("a1", _credits(0), observed_quota_epoch=stale)

    row = next(t for t in tm.tokens if t["id"] == "a1")
    assert row["credits_quota_epoch"] == stale
    assert row["credits_quota_epoch"] != tm.quota_epoch("acct-1"), (
        "失配标记要留下来，调度侧 fast-path 据此忽略这份快照"
    )


def test_epoch_zero_is_a_valid_version(make_tm):
    """账号从未耗尽过时 epoch 是 0；不能被 `or` 兜底当成缺失。"""
    tm = _pool(make_tm)
    for t in tm.tokens:
        if t["account_id"] == "acct-1":
            t["status"] = "exhausted"

    tm.set_credits_and_maybe_revive("a1", _credits(500), observed_quota_epoch=0)
    assert _status(tm, "a1") == "active"
    assert next(t for t in tm.tokens if t["id"] == "a1")["credits_quota_epoch"] == 0


# --- 字段完整性 ---


def test_all_credit_fields_are_preserved(make_tm):
    tm = _pool(make_tm)
    tm.set_credits_and_maybe_revive(
        "a1",
        _credits(300, available_until="2026-09-01T00:00:00Z"),
        observed_quota_epoch=0,
    )
    row = next(t for t in tm.tokens if t["id"] == "a1")
    assert row["credits_total"] == 1000
    assert row["credits_used"] == 700
    assert row["credits_available"] == 300
    assert row["credits_available_until"] == "2026-09-01T00:00:00Z"
    assert row["credits_updated_at"] == 1_700_000_000
    assert row["credits_error"] == ""


def test_unknown_token_id_returns_none(make_tm):
    tm = _pool(make_tm)
    assert tm.set_credits_and_maybe_revive("nope", _credits(1), 0) is None


@pytest.mark.parametrize("bad", ["abc", None, float("nan"), float("inf")])
def test_dirty_available_never_revives_and_never_raises(make_tm, bad):
    tm = _pool(make_tm)
    tm.report_account_exhausted("acct-1")
    epoch = tm.quota_epoch("acct-1")
    payload = _credits(0)
    payload["available"] = bad
    tm.set_credits_and_maybe_revive("a1", payload, observed_quota_epoch=epoch)
    assert _status(tm, "a1") == "exhausted"


# --- 复活触发器：批量刷新必须选中 exhausted ---


def test_credit_refresh_list_includes_exhausted_and_error(make_tm):
    tm = make_tm()
    _seed(
        tm,
        [
            {"id": "a1", "value": "v1", "account_id": "acct-1", "status": "active"},
            {"id": "b1", "value": "v2", "account_id": "acct-2", "status": "exhausted"},
            {"id": "c1", "value": "v3", "account_id": "acct-3", "status": "error"},
            {"id": "d1", "value": "v4", "account_id": "acct-4", "status": "invalid"},
        ],
    )
    ids = tm.list_credit_refresh_ids()
    assert set(ids) == {"a1", "b1", "c1"}
    assert "d1" not in ids, "invalid 是人工介入才恢复的终态，不必反复查余额"


def test_credit_refresh_list_dedupes_by_account(make_tm):
    tm = _pool(make_tm)
    ids = tm.list_credit_refresh_ids()
    assert ids == ["a1", "b1"], "同账号查一行就够"


def test_scheduling_pool_still_excludes_exhausted(make_tm):
    """放宽的只是余额刷新集合，调度集合不能跟着放宽。"""
    tm = _pool(make_tm)
    tm.report_account_exhausted("acct-1")
    assert tm.list_active_ids() == ["b1"]
    assert "a1" in tm.list_credit_refresh_ids()
