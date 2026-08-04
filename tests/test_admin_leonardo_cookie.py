"""后台「导入 Leonardo Cookie」：走 admin 会话鉴权，与 refresh-key 接口共用同一份存储。

背景：此前 Leonardo cookie 只有 HTTP 接口（需 X-Leonardo-Refresh-Key），后台没有入口，
用户只能去粘 Adobe 的「Cookie 导入」，结果导入成没额度的 Adobe 游客号并提示刷新失败。
"""
import json

import pytest

from api.routes.leonardo_tokens import store_leonardo_cookie, read_leonardo_cookie_status

LEO_COOKIE = (
    "anonymous-id=x; "
    "__Secure-better-auth.session_token=abc.def; "
    "__Secure-better-auth.session_data.0=part0; "
    "__Secure-better-auth.session_data.1=part1"
)


@pytest.fixture
def cookie_dir(tmp_path, monkeypatch):
    import core.token_mgr as tm
    monkeypatch.setattr(tm, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_store_extracts_and_persists(cookie_dir):
    out = store_leonardo_cookie(LEO_COOKIE)
    assert len(out["fingerprint"]) == 64
    assert out["updated_at"] > 0

    saved = json.loads((cookie_dir / "leonardo_cookie.json").read_text())
    # 只保留 better-auth 三条，其余(如 anonymous-id)丢弃
    assert "anonymous-id" not in saved["cookie"]
    assert saved["cookie"].count("__Secure-better-auth") == 3
    assert saved["fingerprint"] == out["fingerprint"]


def test_store_rejects_non_leonardo_cookie(cookie_dir):
    with pytest.raises(ValueError):
        store_leonardo_cookie("k1=v1; k2=v2")


def test_status_reports_fingerprint_without_leaking_cookie(cookie_dir):
    assert read_leonardo_cookie_status()["uploaded"] is False
    stored = store_leonardo_cookie(LEO_COOKIE)
    st = read_leonardo_cookie_status()
    assert st["uploaded"] is True
    assert st["fingerprint"] == stored["fingerprint"]
    assert st["updated_at"] == stored["updated_at"]
    # 状态接口不得回传 cookie 明文（后台页面会展示它）
    assert "cookie" not in st


def test_same_cookie_is_idempotent_fingerprint(cookie_dir):
    a = store_leonardo_cookie(LEO_COOKIE)
    b = store_leonardo_cookie(LEO_COOKIE + "; extra=1")
    assert a["fingerprint"] == b["fingerprint"]  # 只按 better-auth 三条算
