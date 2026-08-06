"""配额出池的账号级语义。

事故背景：出池原来按 token 字符串精确匹配，只标一行。同一账号常有两行 token
（手动导入一行、自动刷新一行），另一行照样会被选中，死号根本没离开调度池。
更隐蔽的是请求在飞期间 cookie 刷新会换掉 token 值，事后拿旧值反查直接落空。

配套修正：cookie 刷新不得复活 exhausted 账号——否则每个刷新周期（现网约 15h）
都会把死号送回池里重新挨撞。
"""

import base64
import json
import time

import pytest

from core.token_mgr import TokenManager, retire_account_for_quota


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
            "type": "adobe",
            "account_id": r.get("account_id", r["value"]),
            **{k: v for k, v in r.items() if k.startswith(("refresh_", "auto_"))},
        }
        for r in rows
    ]
    tm.save()
    return tm


def _two_rows_one_account(make_tm):
    """同一账号的两行 token：一行手动导入，一行自动刷新。"""
    tm = make_tm()
    return _seed(
        tm,
        [
            {"id": "manual", "value": "tok-manual", "account_id": "acct-1"},
            {
                "id": "auto",
                "value": "tok-auto",
                "account_id": "acct-1",
                "auto_refresh": True,
                "refresh_profile_id": "prof-1",
            },
            {"id": "other", "value": "tok-other", "account_id": "acct-2"},
        ],
    )


def _status(tm, tid):
    return next(t["status"] for t in tm.tokens if t["id"] == tid)


# --- 账号级出池 ---


def test_exhausting_one_row_retires_whole_account(make_tm):
    tm = _two_rows_one_account(make_tm)
    assert tm.report_account_exhausted("acct-1") is True
    assert _status(tm, "manual") == "exhausted"
    assert _status(tm, "auto") == "exhausted"
    assert _status(tm, "other") == "active", "别的账号不该被牵连"


def test_retired_account_leaves_scheduling_pool(make_tm):
    tm = _two_rows_one_account(make_tm)
    tm.report_account_exhausted("acct-1")
    for _ in range(10):
        assert tm.get_available(strategy="least_recently_used") == "tok-other"


def test_compat_wrapper_resolves_account_from_token_value(make_tm):
    tm = _two_rows_one_account(make_tm)
    tm.report_exhausted("tok-manual")
    assert _status(tm, "auto") == "exhausted", "旧接口也必须整账号出池"


def test_compat_wrapper_logs_when_token_unknown(make_tm, caplog):
    tm = _two_rows_one_account(make_tm)
    with caplog.at_level("WARNING"):
        tm.report_exhausted("no-such-token")
    assert any("not found" in r.getMessage() for r in caplog.records)
    assert _status(tm, "manual") == "active"


def test_report_invalid_stays_token_scoped(make_tm):
    """凭证失效只标这一行——粒度和配额出池是故意不同的。

    同账号的手动导入行和自动刷新行持有两个不同的 access token，
    一行过期不代表另一行过期。按账号连坐的话，一行陈旧 token 会把
    持有新 token 的兄弟行一起打死，整账号要等下一轮 cookie 刷新（默认 15h）
    才可能恢复。
    """
    tm = _two_rows_one_account(make_tm)
    tm.report_invalid("tok-auto")
    assert _status(tm, "auto") == "invalid"
    assert _status(tm, "manual") == "active", "同账号的另一行 token 仍然可用"
    assert tm.get_available(strategy="least_recently_used") is not None


def test_expired_manual_token_does_not_kill_the_account(make_tm):
    """生产主路径回归：手动行 401 → handle_auth_failure → report_invalid。"""
    tm = _two_rows_one_account(make_tm)
    result = tm.handle_auth_failure("tok-manual")  # 手动行没有 auto_refresh
    assert result["status"] == "invalid"
    assert _status(tm, "manual") == "invalid"
    assert _status(tm, "auto") == "active", "自动刷新行持有的是另一个 token"
    assert "auto" in tm.list_active_ids()


def test_report_invalid_does_not_downgrade_exhausted(make_tm):
    """配额耗尽是更强的终态，不能被降级成 invalid。

    降级后 cookie 刷新的守卫（只认 exhausted）就失效了，
    死号会被下一轮刷新洗回调度池——正是本次事故要根除的行为。
    """
    tm = _two_rows_one_account(make_tm)
    tm.report_account_exhausted("acct-1")
    tm.report_invalid("tok-auto")
    assert _status(tm, "auto") == "exhausted"

    tm.upsert_auto_refresh_token("tok-auto-NEW", "prof-1")
    assert _status(tm, "auto") == "exhausted", "守卫仍然生效，没有被洗白"


def test_account_level_invalid_also_spares_exhausted(make_tm):
    tm = _two_rows_one_account(make_tm)
    tm.report_account_exhausted("acct-1")
    tm.report_account_invalid("acct-1")
    assert _status(tm, "manual") == "exhausted"
    assert _status(tm, "auto") == "exhausted"


def test_empty_account_key_is_noop(make_tm):
    tm = _two_rows_one_account(make_tm)
    assert tm.report_account_exhausted("") is False
    assert tm.report_account_exhausted("   ") is False
    assert _status(tm, "manual") == "active"


# --- 并发回归：租约期间 token 值被刷新替换 ---


def test_retirement_survives_token_value_rotation(make_tm):
    """请求在飞时 cookie 刷新换掉了 token 值，出池仍须命中账号。

    这是旧实现最隐蔽的漏标路径：事后拿旧 token 值反查会落空。
    """
    tm = _two_rows_one_account(make_tm)
    account_key = tm.account_key_for_id("auto")  # 请求发起前拿到的稳定账号键
    tm.upsert_auto_refresh_token("tok-auto-NEW", "prof-1")  # 期间刷新换值
    assert _status(tm, "auto") == "active"

    retire_account_for_quota(tm, account_key=account_key, token="tok-auto")
    assert _status(tm, "auto") == "exhausted"
    assert _status(tm, "manual") == "exhausted"


def test_stale_token_value_lookup_would_have_missed(make_tm):
    """反证：只用旧 token 值反查确实标不上——说明账号键不是多余的。"""
    tm = _two_rows_one_account(make_tm)
    tm.upsert_auto_refresh_token("tok-auto-NEW", "prof-1")
    tm.report_exhausted("tok-auto")  # 旧值，已不存在
    assert _status(tm, "auto") == "active"


# --- account_key_for_id ---


def test_account_key_for_id_uses_stable_id(make_tm):
    tm = _two_rows_one_account(make_tm)
    assert tm.account_key_for_id("auto") == "acct-1"
    tm.upsert_auto_refresh_token("tok-auto-NEW", "prof-1")
    assert tm.account_key_for_id("auto") == "acct-1", "换值后 id 仍稳定"


def test_account_key_for_id_falls_back_to_value(make_tm):
    tm = _two_rows_one_account(make_tm)
    assert tm.account_key_for_id("", fallback_value="tok-manual") == "acct-1"
    assert tm.account_key_for_id("nope", fallback_value="tok-manual") == "acct-1"


def test_account_key_for_id_returns_empty_when_unknown(make_tm):
    tm = _two_rows_one_account(make_tm)
    assert tm.account_key_for_id("nope", fallback_value="nope") == ""


# --- quota_epoch ---


def test_quota_epoch_increments_per_event(make_tm):
    tm = _two_rows_one_account(make_tm)
    assert tm.quota_epoch("acct-1") == 0
    tm.report_account_exhausted("acct-1")
    assert tm.quota_epoch("acct-1") == 1
    tm.report_account_exhausted("acct-1")
    assert tm.quota_epoch("acct-1") == 2
    assert tm.quota_epoch("acct-2") == 0, "版本号按账号独立"


def test_quota_epoch_survives_restart(make_tm):
    tm = _two_rows_one_account(make_tm)
    tm.report_account_exhausted("acct-1")
    tm.report_account_exhausted("acct-1")

    reloaded = make_tm()  # 进程重启：从 tokens.json 重建
    assert reloaded.quota_epoch("acct-1") == 2
    reloaded.report_account_exhausted("acct-1")
    assert reloaded.quota_epoch("acct-1") == 3, "重启后必须继续递增，不能回绕"


# --- cookie 自动刷新不得复活 exhausted ---


def test_auto_refresh_does_not_revive_exhausted(make_tm):
    tm = _two_rows_one_account(make_tm)
    tm.report_account_exhausted("acct-1")
    tm.upsert_auto_refresh_token("tok-auto-NEW2", "prof-1")
    assert _status(tm, "auto") == "exhausted"
    assert next(t["value"] for t in tm.tokens if t["id"] == "auto") == "tok-auto-NEW2", (
        "token 值仍要更新，只是不复活状态"
    )


@pytest.mark.parametrize("status", ["error", "invalid"])
def test_auto_refresh_still_revives_other_statuses(make_tm, status):
    """反向回归：别把该救的号也一起卡死。"""
    tm = _two_rows_one_account(make_tm)
    for t in tm.tokens:
        if t["id"] == "auto":
            t["status"] = status
            t["fails"] = 5
    tm.upsert_auto_refresh_token("tok-auto-NEW3", "prof-1")
    assert _status(tm, "auto") == "active"
    assert next(t["fails"] for t in tm.tokens if t["id"] == "auto") == 0


# --- 共享 helper 的三条输入路径 ---


class _Recorder:
    """只实现新接口的桩，用来确认走的是账号级路径。"""

    def __init__(self):
        self.account_calls = []
        self.value_calls = []

    def account_key_for_id(self, tid, fallback_value=""):
        return {"t1": "acct-1"}.get(tid, "")

    def report_account_exhausted(self, key):
        self.account_calls.append(key)
        return True

    def report_exhausted(self, value):
        self.value_calls.append(value)


def test_helper_prefers_explicit_account_key():
    rec = _Recorder()
    assert retire_account_for_quota(rec, account_key="acct-9", token="tok") is True
    assert rec.account_calls == ["acct-9"]
    assert rec.value_calls == []


def test_helper_resolves_from_token_id():
    rec = _Recorder()
    assert retire_account_for_quota(rec, token="tok", token_id="t1") is True
    assert rec.account_calls == ["acct-1"]


def test_helper_falls_back_and_warns_when_unresolvable(caplog):
    rec = _Recorder()
    with caplog.at_level("WARNING"):
        assert retire_account_for_quota(rec, token="tok", token_id="zzz") is False
    assert rec.value_calls == ["tok"], "认不出账号时至少要用旧接口兜底"
    assert any("account key unresolved" in r.getMessage() for r in caplog.records)


def test_helper_tolerates_legacy_manager_without_new_methods():
    """老测试桩/旧版本 manager 没有新方法时不能炸。"""

    class Legacy:
        def __init__(self):
            self.value_calls = []

        def report_exhausted(self, value):
            self.value_calls.append(value)

    legacy = Legacy()
    assert retire_account_for_quota(legacy, token="tok") is False
    assert legacy.value_calls == ["tok"]


def _leo_jwt(sub: str, exp_offset: int = 3600) -> str:
    """可被 account_id_from_token / _decode_jwt_exp 解析的 Leonardo token。"""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(
        json.dumps(
            {
                "sub": sub,
                "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc",
                "cognito:username": "leo-user",
                "exp": int(time.time()) + exp_offset,
            }
        ).encode()
    ).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _seed_leonardo(tm, status: str, fails: int = 0):
    aid = "a6fbcd6a-039f-445c-83e6-6822b7e113d5"
    tm.tokens = [
        {
            "id": "leo",
            "value": _leo_jwt(aid, 1800),
            "status": status,
            "fails": fails,
            "added_at": 0,
            "error_until": 0,
            "last_used_at": 0,
            "type": "leonardo",
            "account_id": aid,
        }
    ]
    tm.save()
    return aid


def test_leonardo_refresh_also_respects_exhausted(make_tm):
    """Leonardo 侧同一条规矩：refresher 推新 token 不得复活配额死号。

    只堵 Adobe 一侧的话状态机是半残的——Leonardo 号照样会被洗回池子。
    """
    tm = make_tm()
    aid = _seed_leonardo(tm, "active")
    tm.report_account_exhausted(aid)

    new_token = _leo_jwt(aid, 7200)
    tm.upsert_leonardo_token(new_token, aid)

    row = next(t for t in tm.tokens if t["id"] == "leo")
    assert row["status"] == "exhausted", "刷新不得复活配额死号"
    assert row["value"] == new_token, "但 token 值仍要更新"


def test_leonardo_refresh_still_revives_non_exhausted(make_tm):
    """反向回归：别把该救的号也一起卡死。"""
    tm = make_tm()
    aid = _seed_leonardo(tm, "invalid", fails=3)
    tm.upsert_leonardo_token(_leo_jwt(aid, 7200), aid)
    row = next(t for t in tm.tokens if t["id"] == "leo")
    assert row["status"] == "active"
    assert row["fails"] == 0


def test_leonardo_exhausted_account_can_come_back(make_tm):
    """Leonardo 的完整闭环：出池 → 额度恢复 → 回池。

    这是加 upsert_leonardo_token 守卫时差点踩的坑：守卫堵掉了「refresher 推新
    token 顺手复活」这条旧退路，而余额驱动复活当时只接了 Adobe 分支，
    结果 Leonardo 号一旦 exhausted 就永久出不来，只能手工删行。
    """
    tm = make_tm()
    aid = _seed_leonardo(tm, "active")

    # 注意 token_type：默认是 adobe，Leonardo 行本来就会被类型过滤掉，
    # 用默认值断言 is None 会假通过。
    assert tm.get_available(strategy="least_recently_used", token_type="leonardo")

    tm.report_account_exhausted(aid)
    assert _status(tm, "leo") == "exhausted"
    assert (
        tm.get_available(strategy="least_recently_used", token_type="leonardo") is None
    )

    # refresher 推新 token：仍不复活（守卫按预期生效）
    tm.upsert_leonardo_token(_leo_jwt(aid, 7200), aid)
    assert _status(tm, "leo") == "exhausted"

    # 额度恢复后的余额刷新：这才是复活的正路
    epoch = tm.quota_epoch(aid)
    tm.set_credits_and_maybe_revive(
        "leo",
        {"total": 9000, "used": 0, "available": 9000,
         "available_until": None, "updated_at": int(time.time())},
        observed_quota_epoch=epoch,
    )
    assert _status(tm, "leo") == "active", "额度回来了就该能重新参与调度"
    assert (
        tm.get_available(strategy="least_recently_used", token_type="leonardo")
        is not None
    )


def test_leonardo_stale_credits_still_cannot_revive(make_tm):
    """乱序防护对 Leonardo 一样生效：查询期间又耗尽一次，这份余额不算数。"""
    tm = make_tm()
    aid = _seed_leonardo(tm, "active")
    epoch_at_request = tm.quota_epoch(aid)

    tm.report_account_exhausted(aid)

    tm.set_credits_and_maybe_revive(
        "leo",
        {"total": 9000, "used": 0, "available": 9000,
         "available_until": None, "updated_at": int(time.time())},
        observed_quota_epoch=epoch_at_request,
    )
    assert _status(tm, "leo") == "exhausted"
