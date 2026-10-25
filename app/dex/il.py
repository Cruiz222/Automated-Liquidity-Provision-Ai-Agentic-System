"""Impermanent-loss (IL) math for AMM liquidity positions.

Analytics use floats; money stays in integer minor units elsewhere. Prices are
"token1 per token0".

* ``v2_il`` is the exact IL of a constant-product (full-range) position.
* ``v3_*`` give exact holdings/IL for a Uniswap-v3-style concentrated range.
* ``expected_v2_il`` is the expected IL cost over a horizon under zero-drift
  geometric Brownian motion (numerical expectation, not a closed-form shortcut).

IL is measured against simply holding the entry tokens; it excludes fees.
"""
from __future__ import annotations

import math


def _check_price(value: float, name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")


def v2_il(price_ratio: float) -> float:
    """IL (a value <= 0) after the price moves by ``price_ratio`` = new/entry. v2_il(2) = -5.72%."""
    _check_price(price_ratio, "price_ratio")
    return 2.0 * math.sqrt(price_ratio) / (1.0 + price_ratio) - 1.0


def v3_holdings(liquidity: float, price: float, lower: float, upper: float) -> tuple[float, float]:
    """Token0 and token1 amounts for ``liquidity`` in range [lower, upper] at ``price``."""
    _check_price(liquidity, "liquidity")
    _check_price(price, "price")
    _check_price(lower, "lower")
    _check_price(upper, "upper")
    if lower >= upper:
        raise ValueError("lower must be below upper")
    root_p, root_a, root_b = math.sqrt(price), math.sqrt(lower), math.sqrt(upper)
    if price <= lower:
        return liquidity * (1 / root_a - 1 / root_b), 0.0
    if price >= upper:
        return 0.0, liquidity * (root_b - root_a)
    return liquidity * (1 / root_p - 1 / root_b), liquidity * (root_p - root_a)


def v3_il(entry_price: float, exit_price: float, lower: float, upper: float) -> float:
    """IL of a concentrated position versus holding the entry tokens, valued in token1."""
    x0, y0 = v3_holdings(1.0, entry_price, lower, upper)
    x1, y1 = v3_holdings(1.0, exit_price, lower, upper)
    hold = x0 * exit_price + y0
    return (x1 * exit_price + y1) / hold - 1.0


def v3_capital_multiplier(price: float, lower: float, upper: float) -> float:
    """How much more liquidity per dollar the range provides than a full-range position at ``price``.

    Fee income per dollar scales by this factor while the price stays in range;
    IL and out-of-range time grow with it too.
    """
    x, y = v3_holdings(1.0, price, lower, upper)
    return 2.0 * math.sqrt(price) / (x * price + y)


def expected_v2_il(volatility: float, days: float, steps: int = 4001, z_max: float = 8.0) -> float:
    """Expected IL cost (positive fraction of position value) over ``days`` for annual ``volatility``.

    Price ratio r = exp(s*z - s^2/2), z ~ N(0,1), s = volatility*sqrt(days/365), so E[r] = 1.
    For small variance this approaches s^2/8.
    """
    if volatility < 0 or days < 0 or not math.isfinite(volatility) or not math.isfinite(days):
        raise ValueError("volatility and days must be finite and non-negative")
    if volatility == 0 or days == 0:
        return 0.0
    s = volatility * math.sqrt(days / 365.0)
    step = 2.0 * z_max / (steps - 1)
    weighted = norm = 0.0
    for i in range(steps):
        z = -z_max + i * step
        weight = math.exp(-0.5 * z * z)
        weighted += weight * -v2_il(math.exp(s * z - 0.5 * s * s))
        norm += weight
    return weighted / norm


def annualized_volatility(prices: list[float] | tuple[float, ...], periods_per_year: float = 365.0) -> float:
    """Annualized standard deviation of log returns of a price series (needs >= 3 points)."""
    if len(prices) < 3:
        raise ValueError("at least 3 prices are required")
    for price in prices:
        _check_price(price, "price")
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)
