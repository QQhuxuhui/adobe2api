"""账号在飞占用锁 + 并发闸门 + 排队机制。

核心不变量：
- 任一账号同时在飞的请求数不超过 max_inflight_per_account。
- 并发请求数 > 账号数时，多出来的排队等待，有账号释放立刻顶上；不丢不卡死。
- 队列满/超时/池子空各自返回明确原因，且绝不泄漏占用计数。
"""

import threading
import time

import pytest

from core.token_mgr import (
    TokenManager,
    TokenLease,
    LEASE_OK,
    LEASE_NO_TOKEN,
    LEASE_QUEUE_FULL,
    LEASE_TIMEOUT,
)


@pytest.fixture
def make_tm(tmp_path, monkeypatch):
    import core.token_mgr as tm_mod

    monkeypatch.setattr(tm_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tm_mod, "DATA_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(tm_mod, "LEGACY_DATA_FILE", tmp_path / "tokens_legacy.json")
    return TokenManager


def _cfg(monkeypatch, **over):
    from core.config_mgr import config_manager

    base = {
        "concurrency_gate_enabled": True,
        "max_inflight_per_account": 1,
        "account_queue_size": 100,
        "account_queue_timeout_seconds": 25,
    }
    base.update(over)
    for k, v in base.items():
        monkeypatch.setitem(config_manager.config, k, v)


def _pool(TM, n):
    tm = TM()
    tm.tokens = [
        {
            "id": "A%d" % i,
            "value": "adobe-%d" % i,
            "status": "active",
            "fails": 0,
            "added_at": 0,
            "error_until": 0,
            "last_used_at": 0,
            "type": "adobe",
            "account_id": "acct-%d" % i,
        }
        for i in range(1, n + 1)
    ]
    tm.save()
    return tm


def test_single_lease_then_release(make_tm, monkeypatch):
    _cfg(monkeypatch)
    tm = _pool(make_tm, 2)
    lease, reason = tm.acquire_lease(token_type="adobe")
    assert reason == LEASE_OK and isinstance(lease, TokenLease)
    assert tm.inflight_snapshot().get(lease.account_key) == 1
    tm.release_lease(lease)
    assert tm.inflight_snapshot().get(lease.account_key, 0) == 0


def test_never_two_inflight_on_one_account(make_tm, monkeypatch):
    """两次占用同一空闲账号是不允许的：第二次应拿到另一个账号。"""
    _cfg(monkeypatch)
    tm = _pool(make_tm, 2)
    l1, _ = tm.acquire_lease(token_type="adobe")
    l2, _ = tm.acquire_lease(token_type="adobe")
    assert l1.account_key != l2.account_key
    assert set(tm.inflight_snapshot().values()) == {1}


def test_queue_full_fails_fast(make_tm, monkeypatch):
    _cfg(monkeypatch, account_queue_size=0)  # 不排队
    tm = _pool(make_tm, 1)
    held, _ = tm.acquire_lease(token_type="adobe")  # 占满唯一账号
    lease, reason = tm.acquire_lease(token_type="adobe", deadline=None)
    assert lease is None and reason == LEASE_QUEUE_FULL
    tm.release_lease(held)


def test_queue_timeout(make_tm, monkeypatch):
    _cfg(monkeypatch, account_queue_size=10, account_queue_timeout_seconds=1)
    tm = _pool(make_tm, 1)
    held, _ = tm.acquire_lease(token_type="adobe")
    t0 = time.monotonic()
    lease, reason = tm.acquire_lease(token_type="adobe")
    waited = time.monotonic() - t0
    assert lease is None and reason == LEASE_TIMEOUT
    assert 0.8 <= waited <= 4.0  # 大约等了 1s
    tm.release_lease(held)


def test_release_hands_off_to_waiter(make_tm, monkeypatch):
    """有账号释放，排队者立刻拿到——这是本功能的关键路径。"""
    _cfg(monkeypatch, account_queue_size=10, account_queue_timeout_seconds=20)
    tm = _pool(make_tm, 1)
    held, _ = tm.acquire_lease(token_type="adobe")

    got = {}

    def waiter():
        t0 = time.monotonic()
        lease, reason = tm.acquire_lease(token_type="adobe")
        got["reason"] = reason
        got["lease"] = lease
        got["waited"] = time.monotonic() - t0

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.5)  # 确认它在排队
    assert got == {}
    tm.release_lease(held)  # 释放 -> 排队者应立刻醒来拿到
    th.join(timeout=5)
    assert got["reason"] == LEASE_OK
    assert got["lease"] is not None
    assert got["waited"] < 2.0  # 立刻顶上，不是等超时


def test_no_token_when_pool_empty(make_tm, monkeypatch):
    _cfg(monkeypatch)
    tm = _pool(make_tm, 0)
    lease, reason = tm.acquire_lease(token_type="adobe")
    assert lease is None and reason == LEASE_NO_TOKEN


def test_no_token_when_all_excluded(make_tm, monkeypatch):
    """本请求把所有账号都试过了，应立即返回 no_token，而不是排队等自己。"""
    _cfg(monkeypatch)
    tm = _pool(make_tm, 2)
    lease, reason = tm.acquire_lease(
        token_type="adobe", exclude_accounts={"acct-1", "acct-2"}
    )
    assert lease is None and reason == LEASE_NO_TOKEN


def test_cooling_account_is_waited_for_then_thaws(make_tm, monkeypatch):
    _cfg(monkeypatch, account_queue_size=10, account_queue_timeout_seconds=20)
    tm = _pool(make_tm, 1)
    tm.tokens[0]["error_until"] = time.time() + 1  # 冷却 1s
    t0 = time.monotonic()
    lease, reason = tm.acquire_lease(token_type="adobe")
    waited = time.monotonic() - t0
    assert reason == LEASE_OK
    assert 0.8 <= waited <= 4.0  # 等到解冻

    tm.release_lease(lease)


def test_gate_disabled_never_blocks(make_tm, monkeypatch):
    """闸门关：即使账号占满也不排队，直接给号（空租约），保持旧行为。"""
    _cfg(monkeypatch, concurrency_gate_enabled=False)
    tm = _pool(make_tm, 1)
    l1, r1 = tm.acquire_lease(token_type="adobe")
    l2, r2 = tm.acquire_lease(token_type="adobe")  # 不该阻塞
    assert r1 == LEASE_OK and r2 == LEASE_OK
    assert l1.leased is False and l2.leased is False
    assert tm.inflight_snapshot() == {}  # 关闸不计占用
    tm.release_lease(l1)  # 空操作
    tm.release_lease(l2)


def test_gate_status_reports_live_numbers(make_tm, monkeypatch):
    _cfg(monkeypatch, max_inflight_per_account=1)
    tm = _pool(make_tm, 3)
    # 一个占用、一个冷却，剩一个空闲
    l1, _ = tm.acquire_lease(token_type="adobe")
    tm.report_rate_limited("adobe-2", retry_after=30)

    st = tm.gate_status()
    assert st["gate_enabled"] is True
    assert st["max_inflight_per_account"] == 1
    a = st["adobe"]
    assert a["accounts"] == 3
    assert a["inflight_total"] == 1
    assert a["cooling"] == 1
    assert a["ready"] == 1  # 3 - 1忙 - 1冷却
    assert a["capacity"] == 3
    assert len(a["busy_accounts"]) == 1 and a["busy_accounts"][0]["inflight"] == 1
    assert st["waiters"] == 0
    tm.release_lease(l1)
    assert tm.gate_status()["adobe"]["inflight_total"] == 0


def test_stress_more_threads_than_accounts(make_tm, monkeypatch):
    """压力测试：40 个线程抢 8 个账号，每个持有一小会儿再释放。
    验证：任一时刻单账号在飞≤1，全部完成，无死锁，结束后占用清零。"""
    _cfg(monkeypatch, max_inflight_per_account=1, account_queue_size=200,
         account_queue_timeout_seconds=20)
    tm = _pool(make_tm, 8)

    max_seen = {}          # account_key -> 观察到的最大并发
    lock = threading.Lock()
    live = {}              # account_key -> 当前并发
    results = []

    def worker(i):
        lease, reason = tm.acquire_lease(token_type="adobe")
        if reason != LEASE_OK:
            results.append(reason)
            return
        k = lease.account_key
        with lock:
            live[k] = live.get(k, 0) + 1
            max_seen[k] = max(max_seen.get(k, 0), live[k])
        time.sleep(0.02)  # 模拟出图占用
        with lock:
            live[k] -= 1
        tm.release_lease(lease)
        results.append(LEASE_OK)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert all(not t.is_alive() for t in threads), "有线程卡死（死锁）"
    assert results.count(LEASE_OK) == 40, "有请求没成功: %r" % results
    # 关键不变量：任何账号从未被两个请求同时占用
    assert max(max_seen.values()) == 1, "同账号并发超过 1: %r" % max_seen
    # 收尾：占用计数全部归零，无泄漏
    assert tm.inflight_snapshot() == {} or set(tm.inflight_snapshot().values()) == set()


def test_save_failure_does_not_leak_inflight(make_tm, monkeypatch):
    """回归：占用登记落盘失败时，绝不能把 in-flight 计数永久加上去（否则账号被悄悄抹掉）。"""
    _cfg(monkeypatch)
    tm = _pool(make_tm, 2)

    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(tm, "save", boom)
    with pytest.raises(RuntimeError):
        tm.acquire_lease(token_type="adobe")
    # save 抛了 -> 没拿到租约，也没漏占用
    assert tm.inflight_snapshot() == {}


def test_stress_max_inflight_two(make_tm, monkeypatch):
    """每账号允许 2 个在飞：验证上限被尊重（≤2），不会到 3。"""
    _cfg(monkeypatch, max_inflight_per_account=2, account_queue_size=200,
         account_queue_timeout_seconds=20)
    tm = _pool(make_tm, 3)  # 3 账号 × 2 = 峰值 6 并发

    max_seen = {}
    live = {}
    lock = threading.Lock()

    def worker():
        lease, reason = tm.acquire_lease(token_type="adobe")
        assert reason == LEASE_OK
        k = lease.account_key
        with lock:
            live[k] = live.get(k, 0) + 1
            max_seen[k] = max(max_seen.get(k, 0), live[k])
        time.sleep(0.02)
        with lock:
            live[k] -= 1
        tm.release_lease(lease)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(not t.is_alive() for t in threads)
    assert max(max_seen.values()) <= 2, "超过 max_inflight=2: %r" % max_seen
    assert tm.inflight_snapshot() == {}
