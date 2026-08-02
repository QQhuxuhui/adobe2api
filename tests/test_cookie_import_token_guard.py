from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.refresh_mgr import RefreshManager


def make_manager() -> RefreshManager:
    # 守卫在 import_cookie 顶部触发，无需任何实例状态
    return object.__new__(RefreshManager)


# --- _looks_like_token 判定 ---

@pytest.mark.parametrize(
    "text",
    [
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2ln",
        "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2ln",
        "bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2ln",  # 大小写不敏感
        "  eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2ln  ",     # 首尾空白
    ],
)
def test_looks_like_token_true_for_bearer_and_bare_jwt(text):
    assert RefreshManager._looks_like_token(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "k1=v1; k2=v2",  # 普通 cookie 串
        "jmtwv82626981@hotmail.com|ukitu80634577|M.C546_BAY.0.U.MsaArtifacts",  # MSA 管道格式
        '[{"name":"a","value":"b"}]',  # 浏览器导出的 cookie 数组 JSON 文本
        "eyJonly.twoparts",  # 非三段
        "access_token=eyJa.eyJb.c; other=1",  # 含 token 的 cookie（仍是 cookie）
        "eyJfoo.bar; baz=1.qux",  # 三段但含 cookie 特征（; 与空格）
    ],
)
def test_looks_like_token_false_for_cookies_and_junk(text):
    assert RefreshManager._looks_like_token(text) is False


# --- 加固：来自对抗性验证的真实误判用例 ---

_ADOBE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhZG9iZSJ9.c2ln"  # 头段含 alg


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer " + _ADOBE_JWT,            # 抓包 header 整行
        "authorization: Bearer " + _ADOBE_JWT,            # 小写
        "Bearer " + _ADOBE_JWT + ";",                     # 复制带尾随分号
        "Bearer\t" + _ADOBE_JWT,                          # Bearer 后是 tab
        "Bearer Bearer " + _ADOBE_JWT,                    # 重复 Bearer
        "Bearer 1//0gK3xq9zRandomOpaqueRefreshNoDots",    # 显式 Bearer + 不透明 token
        '"' + _ADOBE_JWT + '"',                           # 从 JSON 值复制带引号
        _ADOBE_JWT + ".",                                 # 尾随点（四段）
        "​" + _ADOBE_JWT,                            # 前导零宽空格
        "Bearer　" + _ADOBE_JWT,                      # Bearer 后全角空格
        # 5 段 JWE（头段含 alg），Adobe/Leonardo 不用但仍是 token
        "eyJhbGciOiJSU0EtT0FFUCIsImVuYyI6IkEyNTZHQ00ifQ.a.b.c.d",
    ],
)
def test_looks_like_token_true_hardened(text):
    assert RefreshManager._looks_like_token(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Flask/itsdangerous 签名 cookie 值：eyJ+三段 base64url，但头段解出无 alg
        "eyJ1c2VyX2lkIjozOTksImNzcmYiOiJhYmMifQ.aZ7kxg.5nQ1p3r7T9vXwz0",
        "eyJ1c2VyIjoiYWxpY2UifQ.ZaBc1D.sig-with_dash",
    ],
)
def test_looks_like_token_false_for_signed_cookie_values(text):
    # 非 JWT 的 base64url 签名 cookie 值不应被误判为 token
    assert RefreshManager._looks_like_token(text) is False


# --- import_cookie 守卫 ---

def test_import_cookie_rejects_pasted_bearer():
    manager = make_manager()
    with pytest.raises(ValueError) as caught:
        manager.import_cookie("Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2ln")
    assert "添加 Token" in str(caught.value)


def test_import_cookie_rejects_bare_jwt():
    manager = make_manager()
    with pytest.raises(ValueError) as caught:
        manager.import_cookie("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2ln")
    assert "添加 Token" in str(caught.value)


def test_import_cookie_rejects_cookie_prefixed_bearer():
    # 锁定顺序：_cookie_string_from_input 先剥 "cookie:"，再由守卫识别 token
    manager = make_manager()
    with pytest.raises(ValueError) as caught:
        manager.import_cookie("cookie: Bearer " + _ADOBE_JWT)
    assert "添加 Token" in str(caught.value)


# --- 多行/多 token 粘贴（把多个 token 粘进 Cookie 框） ---

@pytest.mark.parametrize(
    "text",
    [
        _ADOBE_JWT + "\n" + _ADOBE_JWT,                  # 多行裸 JWT
        "Bearer " + _ADOBE_JWT + "\nBearer " + _ADOBE_JWT,  # 多行 Bearer
    ],
)
def test_looks_like_token_true_multiline_all_tokens(text):
    assert RefreshManager._looks_like_token(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "[\n  {\"name\": \"a\", \"value\": \"b\"},\n  {\"name\": \"c\", \"value\": \"d\"}\n]",  # 多行 JSON 导出
        "sess=eyJa.eyJb.c\ncsrf=xyz",  # 多行 name=value cookie
    ],
)
def test_looks_like_token_false_multiline_cookies(text):
    assert RefreshManager._looks_like_token(text) is False


# --- 正向：正常 Cookie 不被守卫误伤，import_cookie 照常建 profile ---

def test_import_cookie_allows_real_cookie(monkeypatch, tmp_path):
    import core.refresh_mgr as rm

    monkeypatch.setattr(rm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(rm, "PROFILE_FILE", tmp_path / "refresh_profile.json")
    manager = rm.RefreshManager()

    summary = manager.import_cookie("sessionid=abc123; csrf=xyz789", name="acct-A")

    assert summary["id"]
    assert summary["import_action"] == "created"
