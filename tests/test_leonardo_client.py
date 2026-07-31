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


from core.leonardo_client import TOKEN_BALANCE_QUERY, sum_credits, parse_token_balance


def test_sum_credits_includes_apicredit_and_stream():
    details = {"subscriptionTokens": 100, "paidTokens": 5, "rolloverTokens": 0,
               "apiCredit": 8500, "streamTokens": 3}
    assert sum_credits(details) == 8608


def test_sum_credits_ignores_missing_and_nonnumeric():
    assert sum_credits({"subscriptionTokens": 10, "paidTokens": None, "apiCredit": "x"}) == 10


def test_parse_token_balance_from_response():
    resp = {"data": {"user_details": [{"subscriptionTokens": 850, "apiCredit": 0}]}}
    assert parse_token_balance(resp) == 850


def test_parse_token_balance_empty_returns_none():
    assert parse_token_balance({"data": {"user_details": []}}) is None
    assert parse_token_balance({}) is None


def test_token_balance_query_shape():
    assert TOKEN_BALANCE_QUERY["operationName"] == "GetTokenBalance"
    assert "user_details" in TOKEN_BALANCE_QUERY["query"]
