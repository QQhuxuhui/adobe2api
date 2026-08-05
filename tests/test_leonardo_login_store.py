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
