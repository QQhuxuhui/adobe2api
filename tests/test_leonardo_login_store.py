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
