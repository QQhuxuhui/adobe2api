"""账号调度：least_recently_used 策略 + 429 冷却。

背景：上游按「账号」限流，同一账号短时间内被连续使用就会 429。原来的 round_robin
是一个全局位置游标，在下面这些情况下会失准甚至完全塌缩，所以补了 LRU 和冷却窗口。
"""

import time

import pytest

from core.token_mgr import TokenManager

LRU = "least_recently_used"


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
            "error_until": r.get("error_until", 0),
            "last_used_at": r.get("last_used_at", 0),
            "type": r.get("type", "adobe"),
            "account_id": r.get("account_id", r["value"]),
        }
        for r in rows
    ]
    tm.save()
    return tm


def _adobe_pool(tm, n=3):
    return _seed(
        tm,
        [{"id": f"A{i}", "value": f"adobe-{i}", "account_id": f"acct-{i}"} for i in range(1, n + 1)],
    )


def test_lru_picks_the_longest_idle_account(make_tm):
    tm = _adobe_pool(make_tm(), 3)
    picks = [tm.get_available(strategy=LRU, token_type="adobe") for _ in range(6)]
    assert picks == ["adobe-1", "adobe-2", "adobe-3"] * 2


def test_lru_survives_mixed_adobe_and_leonardo_traffic(make_tm):
    """回归：共用的 round_robin 游标被两种类型轮流取模会锁死在一个账号上。

    3 个 adobe + 2 个 leonardo 交替取号时，round_robin 会让 adobe-1 吃掉 100% 流量。
    """
    tm = _seed(
        make_tm(),
        [{"id": f"A{i}", "value": f"adobe-{i}", "account_id": f"a{i}"} for i in range(1, 4)]
        + [
            {"id": f"L{i}", "value": f"leo-{i}", "account_id": f"l{i}", "type": "leonardo"}
            for i in range(1, 3)
        ],
    )

    seen = []
    for _ in range(30):
        seen.append(tm.get_available(strategy=LRU, token_type="adobe"))
        tm.get_available(strategy=LRU, token_type="leonardo")

    assert set(seen) == {"adobe-1", "adobe-2", "adobe-3"}
    assert max(seen.count(v) for v in set(seen)) == 10


def test_lru_is_account_granular_not_token_granular(make_tm):
    """一个账号可能有两行 token（手动导入 + 自动刷新），不能因此被连续选中。"""
    tm = _seed(
        make_tm(),
        [
            {"id": "t1", "value": "tokA_old", "account_id": "acct-A"},
            {"id": "t2", "value": "tokA_new", "account_id": "acct-A"},
            {"id": "t3", "value": "tokB", "account_id": "acct-B"},
        ],
    )
    of = {"tokA_old": "acct-A", "tokA_new": "acct-A", "tokB": "acct-B"}
    accounts = [of[tm.get_available(strategy=LRU, token_type="adobe")] for _ in range(6)]
    assert accounts == ["acct-A", "acct-B"] * 3


def test_lru_state_survives_restart(make_tm):
    """round_robin 的游标只在内存里，重启后第一个账号总是先挨打；LRU 落盘。"""
    tm = _adobe_pool(make_tm(), 3)
    assert tm.get_available(strategy=LRU, token_type="adobe") == "adobe-1"
    assert tm.get_available(strategy=LRU, token_type="adobe") == "adobe-2"

    rebooted = make_tm()
    assert rebooted.get_available(strategy=LRU, token_type="adobe") == "adobe-3"


def test_lru_not_skewed_when_a_token_leaves_and_returns(make_tm):
    """池子成员变化会让位置游标错位；LRU 按时间排序，不受影响。"""
    tm = _adobe_pool(make_tm(), 3)
    picks = []
    for i in range(9):
        if i == 2:
            tm.tokens[1]["status"] = "exhausted"
        if i == 5:
            tm.tokens[1]["status"] = "active"
        picks.append(tm.get_available(strategy=LRU, token_type="adobe"))

    # adobe-2 缺席期间不该被选中，回来之后要继续参与轮换
    assert picks[2:5].count("adobe-2") == 0
    assert "adobe-2" in picks[5:]


def test_rate_limited_account_is_benched_for_the_cooldown(make_tm):
    tm = _adobe_pool(make_tm(), 2)
    first = tm.get_available(strategy=LRU, token_type="adobe")

    assert tm.report_rate_limited(first, retry_after=30) == 30.0

    later = [tm.get_available(strategy=LRU, token_type="adobe") for _ in range(4)]
    assert first not in later


def test_cooldown_also_applies_to_round_robin(make_tm):
    """冷却和策略是正交的：选 round_robin 的人也该受保护。"""
    tm = _adobe_pool(make_tm(), 2)
    tm.report_rate_limited("adobe-1", retry_after=30)
    picks = [tm.get_available(strategy="round_robin", token_type="adobe") for _ in range(4)]
    assert picks == ["adobe-2"] * 4


def test_expired_cooldown_lets_the_account_back_in(make_tm):
    tm = _adobe_pool(make_tm(), 2)
    tm.report_rate_limited("adobe-1", retry_after=30)
    for t in tm.tokens:
        if t["value"] == "adobe-1":
            t["error_until"] = time.time() - 1

    picks = {tm.get_available(strategy=LRU, token_type="adobe") for _ in range(4)}
    assert "adobe-1" in picks


def test_all_cooling_down_still_returns_the_soonest_to_thaw(make_tm):
    """整池冷却时不能直接返回 None，否则服务会假死；交出最早解冻的那个。"""
    tm = _adobe_pool(make_tm(), 2)
    now = time.time()
    tm.tokens[0]["error_until"] = now + 300
    tm.tokens[1]["error_until"] = now + 30

    assert tm.get_available(strategy=LRU, token_type="adobe") == "adobe-2"


def test_cooldown_covers_every_row_of_the_same_account(make_tm):
    tm = _seed(
        make_tm(),
        [
            {"id": "t1", "value": "tokA_old", "account_id": "acct-A"},
            {"id": "t2", "value": "tokA_new", "account_id": "acct-A"},
            {"id": "t3", "value": "tokB", "account_id": "acct-B"},
        ],
    )
    tm.report_rate_limited("tokA_old", retry_after=30)
    picks = [tm.get_available(strategy=LRU, token_type="adobe") for _ in range(3)]
    assert picks == ["tokB"] * 3


def test_rate_limited_never_shortens_an_existing_cooldown(make_tm):
    tm = _adobe_pool(make_tm(), 2)
    tm.report_rate_limited("adobe-1", retry_after=300)
    before = [t["error_until"] for t in tm.tokens if t["value"] == "adobe-1"][0]

    tm.report_rate_limited("adobe-1", retry_after=5)
    after = [t["error_until"] for t in tm.tokens if t["value"] == "adobe-1"][0]
    assert after == before


def test_rate_limited_falls_back_to_configured_cooldown(make_tm, monkeypatch):
    from core.config_mgr import config_manager

    monkeypatch.setitem(config_manager.config, "rate_limit_cooldown_seconds", 45)
    tm = _adobe_pool(make_tm(), 2)
    assert tm.report_rate_limited("adobe-1") == 45.0


def test_zero_cooldown_disables_benching(make_tm, monkeypatch):
    from core.config_mgr import config_manager

    monkeypatch.setitem(config_manager.config, "rate_limit_cooldown_seconds", 0)
    tm = _adobe_pool(make_tm(), 2)
    assert tm.report_rate_limited("adobe-1") == 0.0
    assert all(float(t["error_until"]) == 0 for t in tm.tokens)


def test_rate_limited_ignores_unknown_token(make_tm):
    tm = _adobe_pool(make_tm(), 2)
    assert tm.report_rate_limited("not-in-pool", retry_after=30) == 0.0
    assert tm.report_rate_limited("", retry_after=30) == 0.0


def test_lru_alias_is_accepted(make_tm):
    tm = _adobe_pool(make_tm(), 2)
    assert tm.get_available(strategy="lru", token_type="adobe") == "adobe-1"
    assert tm.get_available(strategy="lru", token_type="adobe") == "adobe-2"


def test_unknown_strategy_still_falls_back_to_round_robin(make_tm):
    tm = _adobe_pool(make_tm(), 2)
    picks = [tm.get_available(strategy="nonsense", token_type="adobe") for _ in range(4)]
    assert picks == ["adobe-1", "adobe-2"] * 2


def test_round_robin_survives_interleaved_account_scoped_picks(make_tm):
    """回归：账号级选择（单行池）不能把全局 round_robin 游标夹回 0。

    get_available_for_account 传进来的池子只有一行，若写回时 % len(pool) 就会把
    全局游标钉死在 0，让普通流量永远只落到第一个账号。
    """
    tm = _adobe_pool(make_tm(), 5)
    picks = []
    for _ in range(5):
        picks.append(tm.get_available(strategy="round_robin", token_type="adobe"))
        tm.get_available_for_account("acct-3", strategy="round_robin")
    assert sorted(picks) == ["adobe-1", "adobe-2", "adobe-3", "adobe-4", "adobe-5"]


def test_account_scoped_rotation_does_not_touch_global_cursor(make_tm):
    tm = _adobe_pool(make_tm(), 3)
    before = tm._rr_index
    for _ in range(10):
        tm.get_available_for_account("acct-2", strategy="round_robin")
    assert tm._rr_index == before  # 账号内轮换只动自己的游标


def test_all_cooling_fallback_does_not_rewind_round_robin(make_tm):
    """整池冷却的兜底选择也不能把全局游标复位。"""
    tm = _adobe_pool(make_tm(), 3)
    tm.get_available(strategy="round_robin", token_type="adobe")  # 推进到 1
    now = time.time()
    for t in tm.tokens:
        t["error_until"] = now + 300  # 全部冷却
    tm.get_available(strategy="round_robin", token_type="adobe")  # 走兜底
    for t in tm.tokens:
        t["error_until"] = 0  # 解冻
    # 游标应继续前进，而不是从 adobe-1 重来
    assert tm.get_available(strategy="round_robin", token_type="adobe") != "adobe-1"


def test_retry_after_is_capped(make_tm):
    tm = _adobe_pool(make_tm(), 2)
    applied = tm.report_rate_limited("adobe-1", retry_after=99999)
    assert applied == TokenManager.MAX_COOLDOWN_SECONDS
    benched = [t for t in tm.tokens if t["value"] == "adobe-1"][0]
    assert benched["error_until"] <= time.time() + TokenManager.MAX_COOLDOWN_SECONDS + 1


def test_auto_refresh_preserves_active_cooldown(make_tm):
    """回归：自动刷新（15h 周期）不能抹掉正在生效的 429 冷却。"""
    from core.token_mgr import TokenManager as TM

    tm = make_tm()
    tm.upsert_auto_refresh_token("tokA", profile_id="p1", profile_email="a@x.com")
    tm.tokens[0]["account_id"] = "acct-A"
    tm.report_rate_limited("tokA", retry_after=300)
    cooling_until = tm.tokens[0]["error_until"]
    assert cooling_until > time.time()

    # 同一账号刷新出新 token 值
    tm.upsert_auto_refresh_token("tokA_new", profile_id="p1", profile_email="a@x.com")
    assert tm.tokens[0]["value"] == "tokA_new"
    assert tm.tokens[0]["error_until"] == cooling_until  # 冷却窗口保留


def test_auto_refresh_clears_expired_cooldown(make_tm):
    tm = make_tm()
    tm.upsert_auto_refresh_token("tokA", profile_id="p1", profile_email="a@x.com")
    tm.tokens[0]["error_until"] = time.time() - 1  # 已过期
    tm.upsert_auto_refresh_token("tokA_new", profile_id="p1", profile_email="a@x.com")
    assert tm.tokens[0]["error_until"] == 0


def test_get_available_for_account_respects_cooldown(make_tm):
    tm = _seed(
        make_tm(),
        [
            {"id": "t1", "value": "tokA", "account_id": "acct-A"},
            {"id": "t2", "value": "tokB", "account_id": "acct-B"},
        ],
    )
    assert tm.get_available_for_account("acct-A", strategy=LRU) == "tokA"

    tm.report_rate_limited("tokA", retry_after=30)
    # 该账号只有这一行且在冷却中：仍然交出它（总比无号可用好），但时间戳已更新
    assert tm.get_available_for_account("acct-A", strategy=LRU) == "tokA"
    assert float(tm.tokens[0]["last_used_at"]) > 0


def test_selection_stamps_last_used_on_the_whole_account(make_tm):
    tm = _seed(
        make_tm(),
        [
            {"id": "t1", "value": "tokA_old", "account_id": "acct-A"},
            {"id": "t2", "value": "tokA_new", "account_id": "acct-A"},
        ],
    )
    tm.get_available(strategy=LRU, token_type="adobe")
    assert all(float(t["last_used_at"]) > 0 for t in tm.tokens)


def test_legacy_pool_without_last_used_is_loadable(make_tm):
    """老的 tokens.json 没有 last_used_at，load() 要补默认值而不是炸掉。"""
    tm = make_tm()
    tm.tokens = [
        {"id": "t1", "value": "tokA", "status": "active", "fails": 0, "added_at": 0}
    ]
    tm.save()

    reloaded = make_tm()
    assert reloaded.tokens[0]["last_used_at"] == 0
    assert reloaded.get_available(strategy=LRU, token_type="adobe") == "tokA"
