"""Leonardo 额度：页面显示「出图可用额度」+ 请求日志记录每张图的实际积分消耗。

背景（实测踩坑）：user_details 有 5 个额度字段，但**只有 subscriptionTokens/paidTokens/
rolloverTokens 能用于出图**；apiCredit / streamTokens 属于官方 API 通道，网页会话出图
用不到。旧实现把 5 个字段直接相加，账号只剩 41 可用时后台仍显示 20 万，严重误导。
"""
import pytest

from core.leonardo_client import (
    credit_breakdown,
    parse_generation_cost,
    parse_token_balance,
    sum_credits,
)

# 真实线上返回过的形状：出图只剩 41，另两条通道各 100000
REAL = {
    "subscriptionTokens": 41,
    "paidTokens": 0,
    "rolloverTokens": 0,
    "apiCredit": 100000,
    "streamTokens": 100000,
}


def test_sum_credits_counts_only_generation_usable():
    # 旧实现返回 200041（误导）；现在必须只算能出图的额度
    assert sum_credits(REAL) == 41


def test_sum_credits_includes_paid_and_rollover():
    assert sum_credits({"subscriptionTokens": 10, "paidTokens": 5, "rolloverTokens": 2}) == 17


def test_sum_credits_ignores_nonnumeric():
    assert sum_credits({"subscriptionTokens": 10, "paidTokens": None, "apiCredit": "x"}) == 10


def test_parse_token_balance_is_generation_usable():
    assert parse_token_balance({"data": {"user_details": [REAL]}}) == 41
    assert parse_token_balance({"data": {"user_details": []}}) is None


def test_credit_breakdown_separates_channels():
    b = credit_breakdown(REAL)
    assert b["available"] == 41  # 出图可用
    assert b["subscription_tokens"] == 41
    assert b["api_credit"] == 100000  # 另一通道，单列、不计入可用
    assert b["stream_tokens"] == 100000
    assert b["available"] != b["api_credit"] + b["subscription_tokens"]


# --- 每张图的积分消耗：Generate mutation 直接返回 apiCreditCost（精确值） ---

def test_parse_generation_cost():
    resp = {"data": {"generate": {"generationId": "g-1", "apiCreditCost": 250}}}
    assert parse_generation_cost(resp) == 250


def test_parse_generation_cost_missing_is_none():
    assert parse_generation_cost({"data": {"generate": {"generationId": "g-1"}}}) is None
    assert parse_generation_cost({}) is None


def test_generate_images_returns_credit_cost():
    from core.leonardo_generation import generate_images

    class _Client:
        def create_generation(self, token, prompt, model_id, aspect, quantity=1,
                              model_slug="nano-banana-2", on_cost=None, **kw):
            if on_cost:
                on_cost(250)          # 上游回报的精确单张成本
            return "gen-1"

        def wait_for_completion(self, token, gen_id, **kw):
            return {"success": True, "images": ["https://cdn/x.jpg"]}

    out = generate_images(_Client(), "tok", prompt="p", model_id="uuid",
                          model_slug="gpt-image-2", aspect_ratio="1:1")
    assert out["provider"]["credit_cost"] == 250


class _BalanceClient:
    """上游 apiCreditCost 恒为 null（实测如此）→ 只能用生成前后的余额差分测量。"""

    def __init__(self, balances):
        self._balances = list(balances)
        self.balance_calls = 0

    def get_credits(self, token, **kw):
        self.balance_calls += 1
        return self._balances.pop(0) if self._balances else None

    def create_generation(self, *a, **kw):
        return "gen-1"

    def wait_for_completion(self, token, gen_id, **kw):
        return {"success": True, "images": ["https://cdn/x.jpg"]}


def test_generate_images_measures_cost_by_balance_diff():
    from core.leonardo_generation import generate_images

    client = _BalanceClient([8500, 8250])  # 生成前 8500，生成后 8250
    out = generate_images(client, "tok", prompt="p", model_id="uuid",
                          model_slug="gemini-image-2", aspect_ratio="1:1")
    assert out["provider"]["credit_cost"] == 250
    assert out["provider"]["credit_cost_source"] == "measured"
    assert client.balance_calls == 2


def test_measured_cost_ignored_when_diff_not_positive():
    # 余额没变/变大（并发或刷新导致）→ 不记，宁可空着也不记错账
    from core.leonardo_generation import generate_images

    for balances in ([8500, 8500], [8500, 8600]):
        out = generate_images(_BalanceClient(balances), "tok", prompt="p",
                              model_id="uuid", aspect_ratio="1:1")
        assert out["provider"]["credit_cost"] is None


def test_upstream_cost_wins_over_balance_diff():
    # 上游若回报了 apiCreditCost，以它为准（精确），不用差分
    from core.leonardo_generation import generate_images

    class _C(_BalanceClient):
        def create_generation(self, *a, on_cost=None, **kw):
            if on_cost:
                on_cost(300)
            return "gen-1"

    out = generate_images(_C([8500, 8250]), "tok", prompt="p", model_id="uuid",
                          aspect_ratio="1:1")
    assert out["provider"]["credit_cost"] == 300
    assert out["provider"]["credit_cost_source"] == "upstream"


def test_generate_images_credit_cost_none_when_no_balance_api():
    from core.leonardo_generation import generate_images

    class _Client:
        def create_generation(self, *a, **kw):
            return "gen-1"

        def wait_for_completion(self, token, gen_id, **kw):
            return {"success": True, "images": ["https://cdn/x.jpg"]}

    out = generate_images(_Client(), "tok", prompt="p", model_id="uuid",
                          aspect_ratio="1:1")
    assert out["provider"]["credit_cost"] is None


def test_exact_upstream_cost_is_not_overwritten_by_estimator():
    # credits_tracker 的余额差分/估值不得覆盖上游回报的精确值
    from core.credits_tracker import CreditsTracker

    payload = {"id": "log-1", "credits_used": 250, "credits_source": "upstream"}
    merged = CreditsTracker._merge_credits(payload, 999, "estimated")
    assert merged["credits_used"] == 250
    assert merged["credits_source"] == "upstream"


def test_estimator_fills_when_no_exact_value():
    from core.credits_tracker import CreditsTracker

    payload = {"id": "log-1"}
    merged = CreditsTracker._merge_credits(payload, 12.5, "measured")
    assert merged["credits_used"] == 12.5
    assert merged["credits_source"] == "measured"
