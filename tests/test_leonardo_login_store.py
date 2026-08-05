import json, os, threading
import pytest
from api.routes.leonardo_login_store import LeonardoLoginStore

@pytest.fixture
def store(tmp_path):
    return LeonardoLoginStore(tmp_path / "leonardo_logins.json")

def test_empty_when_missing(store):
    assert store.list_for_refresher() == []

def test_save_is_atomic_and_0600(store, tmp_path):
    store._save({"logins": [{"id": "a", "email": "x@y.z", "password": "p", "credential_rev": 1}],
                 "yescaptcha_balance": None, "balance_at": None})
    f = tmp_path / "leonardo_logins.json"
    assert f.exists()
    assert (f.stat().st_mode & 0o777) == 0o600
    # 没有残留临时文件
    assert not list(tmp_path.glob(".leonardo_logins.json.*.tmp"))
    assert json.loads(f.read_text())["logins"][0]["email"] == "x@y.z"

def test_corrupt_file_refuses_overwrite(store, tmp_path):
    (tmp_path / "leonardo_logins.json").write_text("{ not json")
    with pytest.raises(RuntimeError):
        store.list_for_refresher()   # 读到损坏 → 抛错，绝不当空

def test_remove_by_id(store):
    store._save({"logins": [{"id": "a", "email": "e", "password": "p", "credential_rev": 1}],
                 "yescaptcha_balance": None, "balance_at": None})
    assert store.remove("a") == {"removed": 1, "count": 0}
    assert store.remove("missing") == {"removed": 0, "count": 0}

def test_concurrent_saves_do_not_corrupt(store):
    def worker(n):
        for _ in range(20):
            store._with_lock_append({"id": f"id{n}-{_}", "email": "e", "password": "p", "credential_rev": 1})
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(store.list_for_refresher()) == 100

def test_import_parses_first_colon_and_no_strip(store):
    out = store.import_lines("a@b.co:pa:ss word \nC@D.co : pw2\n")
    assert (out["added"], out["skipped"]) == (2, 0)
    rows = {r["email"]: r for r in store.list_for_refresher()}
    assert rows["a@b.co"]["password"] == "pa:ss word "   # 首冒号切分、密码不 strip、保留内部冒号与空格
    assert "c@d.co" in rows                               # email 规范化小写+strip

def test_import_skips_invalid_lines(store):
    out = store.import_lines("noColonHere\n:emptyEmail\nx@y.z:\n\n  \n")
    assert out["added"] == 0 and out["skipped"] == 5

def test_reimport_same_password_is_noop(store):
    store.import_lines("a@b.co:pw")
    out = store.import_lines("a@b.co:pw")
    assert out["added"] == 0 and out["updated"] == 0
    assert store.list_for_refresher()[0]["credential_rev"] == 1

def test_reimport_new_password_bumps_rev_and_resets(store):
    store.import_lines("a@b.co:pw1")
    out = store.import_lines("a@b.co:pw2")
    assert out["updated"] == 1
    row = store.list_for_refresher()[0]
    assert row["password"] == "pw2" and row["credential_rev"] == 2


def _id_rev(store):
    r = store.list_for_refresher()[0]
    return r["id"], r["credential_rev"]

def test_report_ok_clears_error_and_fail(store):
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    store.report(i, rev, "login_required", last_error_kind="password")
    store.report(i, rev, "ok")
    v = store.status_view()["logins"][0]
    assert v["status"] == "ok" and v["fail_count"] == 0 and v["last_error_kind"] is None

def test_report_login_required_increments(store):
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    store.report(i, rev, "login_required", last_error_kind="captcha")
    store.report(i, rev, "login_required", last_error_kind="captcha")
    v = store.status_view()["logins"][0]
    assert v["fail_count"] == 2 and v["last_error_kind"] == "captcha"

def test_stale_revision_rejected_but_balance_accepted(store):
    store.import_lines("a@b.co:pw1"); i, _ = _id_rev(store)
    store.import_lines("a@b.co:pw2")  # rev -> 2, pending
    out = store.report(i, 1, "ok", balance=42.0)  # 旧 rev
    assert out == {"updated": False, "reason": "stale_revision"}
    v = store.status_view()
    assert v["logins"][0]["status"] == "pending"        # 账号状态没被旧回报改
    assert v["yescaptcha_balance"] == 42.0              # 但余额收下了

def test_unknown_id_rejected(store):
    out = store.report("nope", 1, "ok")
    assert out == {"updated": False, "reason": "unknown_id"}

def test_status_view_has_thresholds_and_no_password(store):
    store.import_lines("a@b.co:pw")
    v = store.status_view()
    assert v["thresholds"] == {"fail_count": 3, "yescaptcha_balance": 1000}
    assert "password" not in v["logins"][0]

def test_fail_threshold_logged_only_on_crossing(store, caplog):
    import logging
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    with caplog.at_level(logging.WARNING, logger="leonardo_login"):
        store.report(i, rev, "login_required", last_error_kind="captcha")  # 1
        store.report(i, rev, "login_required", last_error_kind="captcha")  # 2
        store.report(i, rev, "login_required", last_error_kind="captcha")  # 3 -> 告警一次
        store.report(i, rev, "login_required", last_error_kind="captcha")  # 4 -> 不重复
    assert sum("连续登录失败" in r.message for r in caplog.records) == 1

def test_recover_logged_only_after_threshold(store, caplog):
    import logging
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    with caplog.at_level(logging.WARNING, logger="leonardo_login"):
        # 一两次失败后恢复 -> 不记恢复
        store.report(i, rev, "login_required", last_error_kind="captcha")
        store.report(i, rev, "ok")
        assert sum("恢复" in r.message for r in caplog.records) == 0
        # 达到阈值后恢复 -> 记一次
        store.report(i, rev, "login_required", last_error_kind="captcha")
        store.report(i, rev, "login_required", last_error_kind="captcha")
        store.report(i, rev, "login_required", last_error_kind="captcha")
        store.report(i, rev, "ok")
    assert sum("恢复" in r.message for r in caplog.records) == 1

def test_low_balance_logged_only_on_crossing(store, caplog):
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    import logging
    with caplog.at_level(logging.WARNING, logger="leonardo_login"):
        store.report(i, rev, "ok", balance=2000.0)   # 高，不告警
        store.report(i, rev, "ok", balance=500.0)    # 跌破 -> 告警一次
        store.report(i, rev, "ok", balance=400.0)    # 仍低 -> 不重复
    assert sum("余额" in r.message for r in caplog.records) == 1

def test_balance_recovery_and_none_noop(store, caplog):
    import logging
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    with caplog.at_level(logging.WARNING, logger="leonardo_login"):
        store.report(i, rev, "ok", balance=500.0)    # 首次即低 -> 告警(跌破)
        store.report(i, rev, "ok", balance=None)     # None 不改状态、不告警
        store.report(i, rev, "ok", balance=1500.0)   # 恢复 -> 告警
    msgs = [r.message for r in caplog.records if "余额" in r.message]
    assert len(msgs) == 2
    v = store.status_view()
    assert v["yescaptcha_balance"] == 1500.0 and isinstance(v["balance_at"], int)
