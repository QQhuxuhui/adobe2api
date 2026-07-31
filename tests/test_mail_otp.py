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


from core.mail_otp import fetch_latest_canva_otp


def _msg(addr, subject, received, body=""):
    return {
        "from": {"emailAddress": {"address": addr}},
        "subject": subject,
        "bodyPreview": body,
        "receivedDateTime": received,
    }


def test_fetch_returns_newest_canva_otp():
    payload = {"value": [
        _msg("noreply@canva.com", "你的Canva可画验证码是100581", "2026-07-30T14:47:00Z"),
        _msg("news@e.adobe.com", "Welcome", "2026-07-30T13:00:00Z"),
    ]}

    def fake_get(url, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer AT"
        return _FakeResp(200, payload)

    otp, ts = fetch_latest_canva_otp("AT", http_get=fake_get)
    assert otp == "100581"
    assert ts > 0


def test_fetch_ignores_non_canva_and_returns_none():
    payload = {"value": [_msg("news@e.adobe.com", "Verification code is 999999", "2026-07-30T14:47:00Z")]}

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp(200, payload)

    assert fetch_latest_canva_otp("AT", http_get=fake_get) is None


def test_fetch_skips_messages_at_or_before_since_ts():
    # since_ts 设为该邮件时间之后，应过滤掉它
    payload = {"value": [_msg("noreply@canva.com", "验证码是100581", "2026-07-30T14:47:00Z")]}

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp(200, payload)

    future = 4102444800.0  # 2100-01-01
    assert fetch_latest_canva_otp("AT", since_ts=future, http_get=fake_get) is None


from core.mail_otp import get_otp


def test_get_otp_rotates_token_and_polls_until_found():
    rotated = []
    fetch_calls = {"n": 0}

    def fake_post(url, data=None, timeout=None):
        return _FakeResp(200, {"access_token": "AT", "refresh_token": "NEW_RT"})

    def fake_get(url, headers=None, timeout=None):
        fetch_calls["n"] += 1
        if fetch_calls["n"] < 2:
            return _FakeResp(200, {"value": []})           # 第一次：还没到验证码
        return _FakeResp(200, {"value": [
            {"from": {"emailAddress": {"address": "noreply@canva.com"}},
             "subject": "验证码是100581", "bodyPreview": "",
             "receivedDateTime": "2026-07-30T14:47:00Z"},
        ]})

    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])

    otp, refresh = get_otp(
        "CID", "OLD_RT",
        on_rotate=rotated.append,
        poll_interval=1, timeout=60,
        http_post=fake_post, http_get=fake_get,
        sleep=lambda _s: None,
        now=lambda: next(ticks),
    )
    assert otp == "100581"
    assert refresh == "NEW_RT"
    assert rotated == ["NEW_RT"]        # 轮换回调被调用
    assert fetch_calls["n"] == 2        # 轮询了两次


def test_get_otp_raises_on_timeout():
    def fake_post(url, data=None, timeout=None):
        return _FakeResp(200, {"access_token": "AT", "refresh_token": "OLD_RT"})

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp(200, {"value": []})

    ticks = iter([0.0, 100.0, 200.0])

    with pytest.raises(MailOTPError):
        get_otp(
            "CID", "OLD_RT",
            poll_interval=1, timeout=30,
            http_post=fake_post, http_get=fake_get,
            sleep=lambda _s: None,
            now=lambda: next(ticks),
        )


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("你的登录码是161153", "161153"),
        ("你的登陆码是654321", "654321"),
    ],
)
def test_extract_otp_chinese_login_code(subject, expected):
    assert extract_canva_otp(subject) == expected


def test_extract_otp_rejects_long_digit_run():
    assert extract_canva_otp("Canva code: 1611537") is None
