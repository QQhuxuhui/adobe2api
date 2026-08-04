"""Adobe「Cookie 导入」收到 Leonardo cookie 时必须明确拒绝并指路。

实际踩坑：把 Leonardo 的 better-auth cookie 粘进 Adobe 的 Cookie 导入框，
Adobe 流程会拿它去问 IMS，IMS 发一个**没有 user_id 的游客 token**（scope
AdobeID firefly_api openid），入池后查积分 403 → 界面只显示「刷新失败」，
用户完全看不出粘错了框。
"""
import pytest

from core.refresh_mgr import RefreshManager as R

LEO_COOKIE = (
    "anonymous-id=65be907b-a943-41a0-84c2-2216fcf17b04; "
    "__Secure-better-auth.session_token=rs5fu7IcvBj0.Fw%2Bkml%2B; "
    "__Secure-better-auth.session_data.0=eyJhbGciOiJkaXIifQ..abc; "
    "__Secure-better-auth.session_data.1=OUN7PigmZKFG"
)
ADOBE_COOKIE = "ftr_ncid=abc; ims_sid=eyJa.eyJb.sig; s_cc=true; gpv=adobe"


def test_detects_leonardo_cookie():
    assert R._looks_like_leonardo_cookie(LEO_COOKIE) is True


def test_plain_adobe_cookie_not_flagged():
    assert R._looks_like_leonardo_cookie(ADOBE_COOKIE) is False
    assert R._looks_like_leonardo_cookie("k1=v1; k2=v2") is False
    assert R._looks_like_leonardo_cookie("") is False


def test_import_cookie_rejects_leonardo_cookie_with_guidance():
    mgr = R.__new__(R)  # 不触发 __init__，只测校验分支
    with pytest.raises(ValueError) as ei:
        mgr.import_cookie(LEO_COOKIE)
    msg = str(ei.value)
    assert "Leonardo" in msg
    # 必须指出正确路径，而不是只说“失败”
    assert "leonardo" in msg.lower() and "cookie" in msg.lower()


def test_token_guard_still_works():
    # 原有的「粘了 Bearer」拦截不受影响
    mgr = R.__new__(R)
    with pytest.raises(ValueError) as ei:
        mgr.import_cookie("Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhIn0.sig")
    assert "token" in str(ei.value).lower()
