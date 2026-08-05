"""后台「Leonardo 登录账号」导入/状态/删除端点（走 admin 会话鉴权）。

沿用 test_admin_proxy 的 build_admin_router + TestClient 模式；单例 login_store
的落盘路径在测试里 monkeypatch 到 tmp_path，避免污染真实配置目录。
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routes.admin import build_admin_router


class FakeConfigManager:
    def get(self, key: str, default=None):
        return default

    def get_all(self) -> dict:
        return {}


def make_admin_client(*, authenticated: bool = True) -> TestClient:
    def require_admin_auth(request) -> None:
        if not authenticated:
            raise HTTPException(status_code=401, detail="Unauthorized")

    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=object(),
            config_manager=FakeConfigManager(),
            refresh_manager=object(),
            log_store=object(),
            error_store=object(),
            live_log_store=object(),
            require_admin_auth=require_admin_auth,
            is_admin_authenticated=lambda request: authenticated,
            apply_client_config=lambda: None,
            get_generated_storage_stats=lambda: {},
        )
    )
    return TestClient(api)


@pytest.fixture
def login_store_dir(tmp_path, monkeypatch):
    """把单例 login_store 的落盘路径指向 tmp，隔离测试。"""
    import api.routes.leonardo_login_store as mod

    monkeypatch.setattr(
        mod.login_store, "_path", tmp_path / "leonardo_logins.json"
    )
    return tmp_path


def test_import_status_delete_roundtrip(login_store_dir):
    client = make_admin_client()

    r = client.post("/api/v1/leonardo/login", json={"text": "a@b.co:pw\nbad line\n"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["added"] == 1
    assert body["skipped"] == 1

    st = client.get("/api/v1/leonardo/login/status").json()
    assert st["count"] == 1
    assert st["logins"][0]["email"] == "a@b.co"
    assert "password" not in st["logins"][0]
    assert st["thresholds"]["fail_count"] == 3

    lid = st["logins"][0]["id"]
    d1 = client.delete(f"/api/v1/leonardo/login/{lid}")
    assert d1.status_code == 200
    assert d1.json()["status"] == "ok"
    assert d1.json()["removed"] == 1

    d2 = client.delete(f"/api/v1/leonardo/login/{lid}")
    assert d2.status_code == 404


def test_import_rejects_oversize(login_store_dir):
    client = make_admin_client()
    r = client.post("/api/v1/leonardo/login", json={"text": "x" * 200_001})
    assert r.status_code == 422


def test_endpoints_require_admin_auth(login_store_dir):
    client = make_admin_client(authenticated=False)
    assert client.post("/api/v1/leonardo/login", json={"text": "a@b.co:pw"}).status_code == 401
    assert client.get("/api/v1/leonardo/login/status").status_code == 401
    assert client.delete("/api/v1/leonardo/login/whatever").status_code == 401
