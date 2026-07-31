import pytest

from core.mail_otp import extract_canva_otp, redeem_graph_token, MailOTPError


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_extract_otp_chinese_subject():
    assert extract_canva_otp("你的Canva可画验证码是100581") == "100581"


def test_extract_otp_english():
    assert extract_canva_otp("Your Canva verification code is 482913") == "482913"


def test_extract_otp_indonesian():
    assert extract_canva_otp("Kode Canva anda adalah 123456") == "123456"


def test_extract_otp_generic_code_colon():
    assert extract_canva_otp("Canva\nYour code: 654321") == "654321"


def test_extract_otp_none_when_no_code():
    assert extract_canva_otp("Welcome to Canva, let's get started") is None


def test_extract_otp_ignores_unrelated_numbers():
    # 无 canva/code/验证码 语境的 6 位数字不应误命中
    assert extract_canva_otp("Order #123456 has shipped") is None


def test_redeem_returns_access_and_rotated_refresh():
    calls = {}

    def fake_post(url, data=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        return _FakeResp(200, {"access_token": "AT", "refresh_token": "NEW_RT"})

    access, refresh = redeem_graph_token("CID", "OLD_RT", http_post=fake_post)
    assert access == "AT"
    assert refresh == "NEW_RT"
    assert calls["url"] == "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    assert calls["data"]["grant_type"] == "refresh_token"
    assert calls["data"]["scope"] == "https://graph.microsoft.com/.default"


def test_redeem_falls_back_to_old_refresh_when_not_rotated():
    def fake_post(url, data=None, timeout=None):
        return _FakeResp(200, {"access_token": "AT"})  # 无 refresh_token

    access, refresh = redeem_graph_token("CID", "OLD_RT", http_post=fake_post)
    assert access == "AT"
    assert refresh == "OLD_RT"


def test_redeem_raises_on_error_status():
    def fake_post(url, data=None, timeout=None):
        return _FakeResp(400, {"error": "invalid_grant"}, text='{"error":"invalid_grant"}')

    with pytest.raises(MailOTPError):
        redeem_graph_token("CID", "OLD_RT", http_post=fake_post)
