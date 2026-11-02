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

SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0


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
    """Annualized sample standard deviation of equally spaced log returns."""
    if len(prices) < 3:
        raise ValueError("at least 3 prices are required")
    if (
        isinstance(periods_per_year, bool)
        or not isinstance(periods_per_year, (int, float))
        or not math.isfinite(periods_per_year)
        or periods_per_year <= 0
    ):
        raise ValueError("periods_per_year must be a positive finite number")
    for price in prices:
        _check_price(price, "price")
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def annualized_volatility_from_observations(
    observations: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    max_gap_seconds: float = 48.0 * 60.0 * 60.0,
) -> float:
    """Estimate annualized volatility from timestamped (timestamp, price) observations.

    Uses realized quadratic variation per unit of elapsed time:

        sqrt(sum(log_return ** 2) / sum(elapsed_seconds) * seconds_per_year)

    Unlike :func:`annualized_volatility`, this does not assume equally spaced
    samples and does not subtract the sample mean from returns. That is the
    standard realized-variance estimator under a zero-drift diffusion model.

    Every adjacent interval must be no longer than ``max_gap_seconds``.
    Excessive gaps are rejected rather than treated as ordinary hourly/daily
    returns or silently ignored, since doing either can materially bias risk.
    At least two valid returns (three observations) are required.
    """
    if (
        isinstance(max_gap_seconds, bool)
        or not isinstance(max_gap_seconds, (int, float))
        or not math.isfinite(max_gap_seconds)
        or max_gap_seconds <= 0
    ):
        raise ValueError("max_gap_seconds must be a positive finite number")
    if len(observations) < 3:
        raise ValueError("at least 3 timestamped observations are required")

    previous_timestamp: float | None = None
    previous_price: float | None = None
    squared_returns = 0.0
    elapsed_seconds = 0.0

    for point in observations:
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise ValueError("each observation must be a (timestamp, price) pair")
        timestamp, price = point
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            raise ValueError("observation timestamps must be non-negative finite numbers")
        _check_price(price, "observation price")

        if previous_timestamp is not None and previous_price is not None:
            gap = timestamp - previous_timestamp
            if gap <= 0:
                raise ValueError("observation timestamps must be strictly increasing")
            if gap > max_gap_seconds:
                raise ValueError(
                    f"observation gap {gap:.0f}s exceeds maximum {max_gap_seconds:.0f}s"
                )
            log_return = math.log(price / previous_price)
            squared_returns += log_return * log_return
            elapsed_seconds += gap

        previous_timestamp = float(timestamp)
        previous_price = float(price)

    if elapsed_seconds <= 0:
        raise ValueError("timestamped observations contain no elapsed time")

    variance_per_second = squared_returns / elapsed_seconds
    return math.sqrt(variance_per_second * SECONDS_PER_YEAR)
