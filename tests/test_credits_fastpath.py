"""调度侧的余额 fast-path。

已知没额度的账号在选号阶段就跳过，省掉「整次多图上传 → 撞 403 → 换号」的浪费。
定位是加速而不是裁决：任何不确定都放行，由真实的 403 分类兜底。

两个容易踩的点：
  - 这些解析器跑在持锁的选号热路径上，一次 float() 抛异常就能把选号打挂，
    所以脏数据（含 ISO 日期字符串、NaN、Inf）必须降级而不是抛。
  - 过滤要按账号整组做：同账号两行 token 的余额缓存不一定同步。
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
    now = time.time()
    tm.tokens = [
        {
            "id": r["id"],
            "value": r["value"],
            "status": r.get("status", "active"),
            "fails": 0,
            "added_at": 0,
            "error_until": 0,
            "last_used_at": r.get("last_used_at", 0),
            "type": r.get("type", "adobe"),
            "account_id": r.get("account_id", r["value"]),
            **{k: v for k, v in r.items() if k.startswith("credits_") or k == "quota_epoch"},
        }
        for r in rows
    ]
    for t in tm.tokens:
        t.setdefault("credits_updated_at", now)
    tm.save()
    return tm


def _fresh(available, **kw):
    """一份属于当前配额版本（epoch 0）的新鲜余额快照。"""
    row = {
        "credits_available": available,
        "credits_updated_at": time.time(),
        "credits_quota_epoch": 0,
        "credits_available_until": None,
    }
    row.update(kw)
    return row


def _two_accounts(make_tm, zero_kwargs):
    tm = make_tm()
    return _seed(
        tm,
        [
            {"id": "z", "value": "tok-zero", "account_id": "acct-zero", **zero_kwargs},
            {"id": "g", "value": "tok-good", "account_id": "acct-good", **_fresh(500)},
        ],
    )


def _picked(tm, n=6, strategy=LRU):
    return {tm.get_available(strategy=strategy) for _ in range(n)}


# --- 基本跳过 ---


def test_zero_credit_account_is_skipped(make_tm):
    tm = _two_accounts(make_tm, _fresh(0))
    assert _picked(tm) == {"tok-good"}


def test_positive_credit_account_is_kept(make_tm):
    tm = _two_accounts(make_tm, _fresh(10))
    assert _picked(tm) == {"tok-zero", "tok-good"}


def test_acquire_lease_also_skips(make_tm):
    """闸门路径（_universe_locked）同样生效。"""
    tm = _two_accounts(make_tm, _fresh(0))
    lease, reason = tm.acquire_lease(strategy=LRU)
    assert lease is not None and lease.token == "tok-good"
    tm.release_lease(lease)


def test_get_available_for_account_skips(make_tm):
    tm = _two_accounts(make_tm, _fresh(0))
    assert tm.get_available_for_account("acct-good") == "tok-good"


def test_never_empties_the_pool(make_tm):
    """所有账号都零余额时必须放行——fast-path 不能把池子清空。"""
    tm = make_tm()
    _seed(
        tm,
        [
            {"id": "z1", "value": "t1", "account_id": "a1", **_fresh(0)},
            {"id": "z2", "value": "t2", "account_id": "a2", **_fresh(0)},
        ],
    )
    assert tm.get_available(strategy=LRU) is not None
    lease, _ = tm.acquire_lease(strategy=LRU)
    assert lease is not None


# --- 不确定就放行 ---


def test_no_cache_is_allowed(make_tm):
    tm = _two_accounts(make_tm, {})
    assert "tok-zero" in _picked(tm)


def test_stale_cache_is_allowed(make_tm):
    """过期的零余额缓存不能把账号永久挡在池外。"""
    old = _fresh(0)
    old["credits_updated_at"] = time.time() - (TokenManager.CREDITS_FASTPATH_TTL + 60)
    tm = _two_accounts(make_tm, old)
    assert "tok-zero" in _picked(tm)


def test_expired_quota_window_is_allowed(make_tm):
    """额度周期已翻转 → 旧的 0 余额作废。"""
    rolled = _fresh(0, credits_available_until="2020-01-01T00:00:00Z")
    tm = _two_accounts(make_tm, rolled)
    assert "tok-zero" in _picked(tm)


def test_future_quota_window_still_skips(make_tm):
    future = _fresh(0, credits_available_until="2099-01-01T00:00:00Z")
    tm = _two_accounts(make_tm, future)
    assert _picked(tm) == {"tok-good"}


def test_snapshot_from_older_quota_epoch_is_ignored(make_tm):
    """快照属于旧配额版本 → 不能用它裁决当前状态。"""
    stale = _fresh(0)
    stale["credits_quota_epoch"] = 0
    stale["quota_epoch"] = 3  # 账号后来又耗尽过 3 次
    tm = _two_accounts(make_tm, stale)
    assert "tok-zero" in _picked(tm)


def test_legacy_rows_without_epoch_are_allowed(make_tm):
    """老数据没有 credits_quota_epoch，等下一次刷新写入版本后再参与过滤。"""
    legacy = _fresh(0)
    legacy.pop("credits_quota_epoch")
    tm = _two_accounts(make_tm, legacy)
    assert "tok-zero" in _picked(tm)


# --- 脏数据不得打挂选号 ---


@pytest.mark.parametrize(
    "until",
    ["2026-09-01T00:00:00Z", "2026-09-01T00:00:00", 1_900_000_000, "", None, "garbage", "NaN"],
)
def test_available_until_parsing_never_raises(make_tm, until):
    tm = _two_accounts(make_tm, _fresh(0, credits_available_until=until))
    assert tm.get_available(strategy=LRU) is not None


@pytest.mark.parametrize("bad", ["abc", float("nan"), float("inf"), None, ""])
def test_dirty_available_is_allowed_not_raised(make_tm, bad):
    tm = _two_accounts(make_tm, _fresh(bad))
    assert "tok-zero" in _picked(tm), "读不出余额就当不确定，放行"


@pytest.mark.parametrize("bad", ["abc", float("nan"), None])
def test_dirty_updated_at_is_allowed_not_raised(make_tm, bad):
    tm = _two_accounts(make_tm, _fresh(0, credits_updated_at=bad))
    assert "tok-zero" in _picked(tm)


@pytest.mark.parametrize("bad", ["abc", 1.5, None])
def test_dirty_epoch_is_allowed_not_raised(make_tm, bad):
    tm = _two_accounts(make_tm, _fresh(0, credits_quota_epoch=bad))
    assert tm.get_available(strategy=LRU) is not None


# --- 按账号聚合 ---


def test_account_filtered_as_a_whole(make_tm):
    """一行余额为 0、另一行没缓存：按行过滤会从没缓存那行漏过去。"""
    tm = make_tm()
    zero = _fresh(0)
    _seed(
        tm,
        [
            {"id": "z1", "value": "tok-zero-a", "account_id": "acct-zero", **zero},
            {"id": "z2", "value": "tok-zero-b", "account_id": "acct-zero"},
            {"id": "g", "value": "tok-good", "account_id": "acct-good", **_fresh(500)},
        ],
    )
    assert _picked(tm, n=8) == {"tok-good"}


def test_freshest_snapshot_wins_within_account(make_tm):
    """账号内以最新鲜的一份为准：旧的 0 不能盖过新的 >0。

    对照组 acct-good 是必需的：只有一个账号时 fast-path 即使把它滤掉，
    `kept or active` 的兜底也会把它交回来，测不出真实行为。
    """
    tm = make_tm()
    old_zero = _fresh(0)
    old_zero["credits_updated_at"] = time.time() - 120
    _seed(
        tm,
        [
            {"id": "x1", "value": "tok-x1", "account_id": "acct-x", **old_zero},
            {"id": "x2", "value": "tok-x2", "account_id": "acct-x", **_fresh(700)},
            {"id": "g", "value": "tok-good", "account_id": "acct-good", **_fresh(500)},
        ],
    )
    picked = _picked(tm, n=8)
    assert "tok-good" in picked
    assert picked & {"tok-x1", "tok-x2"}, "该账号最新余额是 700，不该被旧的 0 挡掉"


def test_stale_zero_beats_nothing_when_it_is_the_freshest(make_tm):
    """反向：账号内最新鲜的那份就是 0 时，照常跳过。"""
    tm = make_tm()
    old_positive = _fresh(900)
    old_positive["credits_updated_at"] = time.time() - 120
    _seed(
        tm,
        [
            {"id": "x1", "value": "tok-x1", "account_id": "acct-x", **old_positive},
            {"id": "x2", "value": "tok-x2", "account_id": "acct-x", **_fresh(0)},
            {"id": "g", "value": "tok-good", "account_id": "acct-good", **_fresh(500)},
        ],
    )
    assert _picked(tm, n=8) == {"tok-good"}


# --- 与出池/复活联动 ---


def test_revived_account_returns_to_pool(make_tm):
    tm = _two_accounts(make_tm, _fresh(0))
    tm.report_account_exhausted("acct-zero")
    assert _picked(tm) == {"tok-good"}

    epoch = tm.quota_epoch("acct-zero")
    tm.set_credits_and_maybe_revive(
        "z",
        {"total": 100, "used": 0, "available": 100, "available_until": None,
         "updated_at": int(time.time())},
        observed_quota_epoch=epoch,
    )
    assert "tok-zero" in _picked(tm), "余额回来后要能重新参与调度"
