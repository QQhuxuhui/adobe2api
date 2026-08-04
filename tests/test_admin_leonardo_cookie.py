"""后台「导入 Leonardo Cookie」：走 admin 会话鉴权，与 refresh-key 接口共用同一份存储。

背景：此前 Leonardo cookie 只有 HTTP 接口（需 X-Leonardo-Refresh-Key），后台没有入口，
用户只能去粘 Adobe 的「Cookie 导入」，结果导入成没额度的 Adobe 游客号并提示刷新失败。
"""
import json

import pytest

from api.routes.leonardo_tokens import (
    store_leonardo_cookie,
    read_leonardo_cookie_status,
    replace_leonardo_cookie,
    list_leonardo_cookies,
    remove_leonardo_cookie,
)

LEO_COOKIE = (
    "anonymous-id=x; "
    "__Secure-better-auth.session_token=abc.def; "
    "__Secure-better-auth.session_data.0=part0; "
    "__Secure-better-auth.session_data.1=part1"
)


def _leo_cookie(tok):
    return (
        f"__Secure-better-auth.session_token={tok}; "
        "__Secure-better-auth.session_data.0=p0; "
        "__Secure-better-auth.session_data.1=p1"
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
    assert out["count"] == 1

    saved = json.loads((cookie_dir / "leonardo_cookies.json").read_text())["cookies"]
    assert len(saved) == 1
    entry = saved[0]
    # 只保留 better-auth 三条，其余(如 anonymous-id)丢弃
    assert "anonymous-id" not in entry["cookie"]
    assert entry["cookie"].count("__Secure-better-auth") == 3
    assert entry["fingerprint"] == out["fingerprint"]


def test_store_rejects_non_leonardo_cookie(cookie_dir):
    with pytest.raises(ValueError):
        store_leonardo_cookie("k1=v1; k2=v2")


def test_multiple_accounts_accumulate(cookie_dir):
    # 导入三个不同账号的 cookie → 三条，互不覆盖
    a = store_leonardo_cookie(_leo_cookie("acctA"))
    b = store_leonardo_cookie(_leo_cookie("acctB"))
    c = store_leonardo_cookie(_leo_cookie("acctC"))
    assert (a["count"], b["count"], c["count"]) == (1, 2, 3)
    fps = {x["fingerprint"] for x in list_leonardo_cookies()}
    assert len(fps) == 3
    assert read_leonardo_cookie_status()["count"] == 3


def test_reimport_same_cookie_updates_not_duplicates(cookie_dir):
    store_leonardo_cookie(_leo_cookie("acctA"))
    out = store_leonardo_cookie(_leo_cookie("acctA"))  # 同一 cookie 再导
    assert out["count"] == 1  # 不重复


def test_remove_cookie_by_fingerprint(cookie_dir):
    a = store_leonardo_cookie(_leo_cookie("acctA"))
    store_leonardo_cookie(_leo_cookie("acctB"))
    out = remove_leonardo_cookie(a["fingerprint"])
    assert out["removed"] == 1 and out["count"] == 1
    fps = {c["fingerprint"] for c in list_leonardo_cookies()}
    assert a["fingerprint"] not in fps


def test_remove_cookie_by_prefix(cookie_dir):
    a = store_leonardo_cookie(_leo_cookie("acctA"))
    # 后台只展示前 12 位，支持前缀删除
    out = remove_leonardo_cookie(a["fingerprint"][:12])
    assert out["removed"] == 1 and out["count"] == 0


def test_remove_missing_cookie_is_noop(cookie_dir):
    store_leonardo_cookie(_leo_cookie("acctA"))
    out = remove_leonardo_cookie("deadbeef")
    assert out["removed"] == 0 and out["count"] == 1


def test_replace_rotated_cookie_targets_one_account(cookie_dir):
    a = store_leonardo_cookie(_leo_cookie("acctA"))
    store_leonardo_cookie(_leo_cookie("acctB"))
    # 轮换 A 的 cookie：只替换 A 那条，B 不动
    out = replace_leonardo_cookie(_leo_cookie("acctA_rotated"), a["fingerprint"])
    assert out["count"] == 2
    cookies = list_leonardo_cookies()
    assert a["fingerprint"] not in {c["fingerprint"] for c in cookies}  # 旧 A 指纹已换掉
    tokens = {c["cookie"] for c in cookies}
    assert any("acctA_rotated" in t for t in tokens)
    assert any("acctB" in t for t in tokens)


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
