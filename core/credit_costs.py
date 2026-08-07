"""Credit price snapshots and deterministic CNY cost calculations."""

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Callable, Mapping, Optional


_PRICE_QUANTUM = Decimal("0.000001")
_COST_QUANTUM = Decimal("0.000001")


def _required_precision(value: Decimal) -> int:
    """Provide enough context precision for large finite Decimal values."""
    _sign, digits, exponent = value.as_tuple()
    return max(
        28,
        len(digits) + max(exponent, 0) + 8,
        len(digits) + max(-exponent, 0) + 8,
    )


def normalize_credit_price(value) -> Optional[float]:
    """Normalize a configured per-credit price, or return ``None`` when unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("credit price must be a finite non-negative number")
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("credit price must be a finite non-negative number")
    if not price.is_finite() or price < 0:
        raise ValueError("credit price must be a finite non-negative number")
    try:
        with localcontext() as context:
            context.prec = _required_precision(price)
            normalized = price.quantize(_PRICE_QUANTUM)
    except InvalidOperation:
        raise ValueError("credit price must have at most 6 decimal places")
    if normalized != price:
        raise ValueError("credit price must have at most 6 decimal places")
    normalized_float = float(normalized)
    if not math.isfinite(normalized_float):
        raise ValueError("credit price must be finite")
    return normalized_float


def calculate_credit_cost(credits_used, unit_price) -> Optional[float]:
    """Return a six-decimal, round-half-up cost, or ``None`` for unknown input."""
    if credits_used is None or unit_price is None:
        return None
    try:
        credits = Decimal(str(credits_used))
        price = Decimal(str(unit_price))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if (
        not credits.is_finite()
        or credits < 0
        or not price.is_finite()
        or price < 0
    ):
        return None
    try:
        with localcontext() as context:
            context.prec = _required_precision(credits) + _required_precision(price)
            normalized = (credits * price).quantize(
                _COST_QUANTUM, rounding=ROUND_HALF_UP
            )
        normalized_float = float(normalized)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    return normalized_float if math.isfinite(normalized_float) else None


def snapshot_credit_prices(config_getter: Callable[[str, object], object]) -> dict[str, Optional[float]]:
    """Read both provider prices once for a request-scoped immutable snapshot."""
    def read_price(key: str) -> Optional[float]:
        try:
            return normalize_credit_price(config_getter(key, None))
        except (TypeError, ValueError):
            return None

    return {
        "leonardo": read_price("leonardo_credit_price_cny"),
        "adobe": read_price("adobe_credit_price_cny"),
    }


def select_credit_price(
    snapshot: Mapping[str, Optional[float]], credit_type: str
) -> Optional[float]:
    """Select the price for a provider from a previously captured snapshot."""
    provider = str(credit_type or "").strip().lower()
    return snapshot.get(provider)
