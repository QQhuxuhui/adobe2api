import pytest


def test_normalize_credit_price_accepts_null_zero_and_six_decimals():
    from core.credit_costs import normalize_credit_price

    assert normalize_credit_price(None) is None
    assert normalize_credit_price(0) == 0.0
    assert normalize_credit_price("0.001") == 0.001
    assert normalize_credit_price("12.345678") == 12.345678


def test_normalize_credit_price_accepts_large_finite_integer_values():
    from core.credit_costs import normalize_credit_price

    assert normalize_credit_price("1e100") == 1e100


@pytest.mark.parametrize("value", [-0.1, "nan", "inf", 0.0000001])
def test_normalize_credit_price_rejects_invalid_values(value):
    from core.credit_costs import normalize_credit_price

    with pytest.raises(ValueError):
        normalize_credit_price(value)


def test_calculate_credit_cost_uses_decimal_round_half_up():
    from core.credit_costs import calculate_credit_cost

    assert calculate_credit_cost("146", "0.001") == 0.146
    assert calculate_credit_cost("1", "0.0000005") == 0.000001


def test_calculate_credit_cost_returns_none_when_input_is_unknown():
    from core.credit_costs import calculate_credit_cost

    assert calculate_credit_cost(None, "0.001") is None
    assert calculate_credit_cost(10, None) is None


def test_calculate_credit_cost_handles_large_finite_values_without_raising():
    from core.credit_costs import calculate_credit_cost

    assert calculate_credit_cost("1e100", "1") == 1e100
    assert calculate_credit_cost("1e309", "1") is None


def test_snapshot_and_provider_selection_are_stable_after_config_change():
    from core.credit_costs import select_credit_price, snapshot_credit_prices

    config = {
        "leonardo_credit_price_cny": 0.001,
        "adobe_credit_price_cny": 0.002,
    }
    snapshot = snapshot_credit_prices(config.get)
    config["leonardo_credit_price_cny"] = 0.009

    assert select_credit_price(snapshot, "leonardo") == 0.001
    assert select_credit_price(snapshot, "adobe") == 0.002


def test_config_schema_exposes_both_credit_prices():
    from api.schemas import ConfigUpdateRequest

    assert "leonardo_credit_price_cny" in ConfigUpdateRequest.model_fields
    assert "adobe_credit_price_cny" in ConfigUpdateRequest.model_fields
    assert ConfigUpdateRequest(
        leonardo_credit_price_cny=None,
        adobe_credit_price_cny=0.002,
    ).adobe_credit_price_cny == 0.002


@pytest.mark.parametrize(
    "field,value",
    [
        ("leonardo_credit_price_cny", -1),
        ("adobe_credit_price_cny", 0.0000001),
        ("leonardo_credit_price_cny", float("nan")),
        ("adobe_credit_price_cny", True),
    ],
)
def test_config_schema_rejects_invalid_credit_prices(field, value):
    from api.schemas import ConfigUpdateRequest

    with pytest.raises(ValueError):
        ConfigUpdateRequest(**{field: value})
