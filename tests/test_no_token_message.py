"""池里只有 Leonardo token、请求却需要 Adobe 时，报错必须说清楚是哪种情况。

实际踩坑：下游调 /v1/images/edits（图片编辑，只接了 Adobe）时返回
「No active tokens available in the pool」，用户据此以为是 cookie 没导入成功，
反复重导账号——错误信息本身把人带偏了。
"""
from core.token_mgr import TokenManager


class _Pool:
    def __init__(self, leonardo: bool, adobe: bool):
        self._leo, self._adobe = leonardo, adobe

    def has_active_token(self, token_type=None):
        if token_type == "leonardo":
            return self._leo
        if token_type == "adobe":
            return self._adobe
        return self._leo or self._adobe


def _msg(pool):
    from app import describe_missing_token

    return describe_missing_token(pool)


def test_leonardo_only_pool_explains_adobe_requirement():
    msg = _msg(_Pool(leonardo=True, adobe=False))
    assert "Leonardo" in msg and "Adobe" in msg
    # 必须点出是「该接口/模型需要 Adobe」，而不是笼统的没有 token
    assert "编辑" in msg or "需要 Adobe" in msg


def test_adobe_only_pool_explains_leonardo_requirement():
    msg = _msg(_Pool(leonardo=False, adobe=True))
    assert "Leonardo" in msg


def test_truly_empty_pool_keeps_plain_message():
    msg = _msg(_Pool(leonardo=False, adobe=False))
    assert "No active tokens available in the pool" == msg


def test_handles_manager_without_capability_probe():
    class _Old:
        pass

    assert _msg(_Old()) == "No active tokens available in the pool"


def test_real_token_manager_is_compatible():
    # 真实 TokenManager 具备 has_active_token，不会走兜底分支
    assert callable(getattr(TokenManager, "has_active_token", None))
