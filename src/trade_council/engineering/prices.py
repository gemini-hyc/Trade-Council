"""Price conversion between analysis and executable trading conventions."""

from __future__ import annotations


def raw_to_adjusted(raw_price: float, adj_factor_at_price: float, adj_factor_asof: float) -> float:
    """Convert an unadjusted on-screen price to the as-of adjusted series."""

    if adj_factor_asof == 0:
        raise ValueError("adj_factor_asof must not be zero")
    return raw_price * adj_factor_at_price / adj_factor_asof


def adjusted_to_raw(
    adjusted_price: float, adj_factor_at_price: float, adj_factor_asof: float
) -> float:
    """Convert an as-of adjusted level back to the executable raw price."""

    if adj_factor_at_price == 0:
        raise ValueError("adj_factor_at_price must not be zero")
    return adjusted_price * adj_factor_asof / adj_factor_at_price

