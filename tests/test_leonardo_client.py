import base64
import json
import time

from core.leonardo_client import (
    LeonardoError,
    decode_jwt_payload,
    token_exp,
    is_fresh_token,
    is_likely_leonardo_token,
)


def _jwt(payload: dict) -> str:
    def seg(d):
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return f"{seg({'alg':'none'})}.{seg(payload)}.sig"


def test_decode_and_exp():
    tok = _jwt({"exp": 1900000000, "iss": "https://cognito-idp.us-east-1.amazonaws.com/x"})
    assert decode_jwt_payload(tok)["exp"] == 1900000000
    assert token_exp(tok) == 1900000000


def test_decode_non_jwt_returns_empty():
    assert decode_jwt_payload("not-a-jwt") == {}
    assert token_exp("nope") == 0


def test_is_fresh_uses_injected_now():
    tok = _jwt({"exp": 1000})
    assert is_fresh_token(tok, now=lambda: 800) is True      # 1000 > 800+... no: 800+120=920 < 1000
    assert is_fresh_token(tok, now=lambda: 950) is False     # 950+120=1070 > 1000 -> not fresh
    assert is_fresh_token("no-dots", now=lambda: 0) is False


def test_is_fresh_true_when_no_exp():
    assert is_fresh_token(_jwt({"sub": "x"}), now=lambda: 0) is True


def test_is_likely_leonardo_token_cognito_signals():
    assert is_likely_leonardo_token(_jwt({"iss": "https://cognito-idp.us-east-1.amazonaws.com/x"})) is True
    assert is_likely_leonardo_token(_jwt({"token_use": "access"})) is True
    assert is_likely_leonardo_token(_jwt({"cognito:username": "u"})) is True
    assert is_likely_leonardo_token(_jwt({"foo": "bar"})) is False


def test_leonardo_error_is_exception():
    assert issubclass(LeonardoError, Exception)
