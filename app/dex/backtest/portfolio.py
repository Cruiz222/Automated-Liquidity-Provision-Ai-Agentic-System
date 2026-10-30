"""Historical portfolio accounting for full-range V2 LP positions.

This is a research-only model. It excludes fees, gas, slippage,
deposits, withdrawals, and execution costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from app.dex.amm import evaluate_constant_product_position
from app.dex.backtest.market_data import HistoricalMarketData


class PortfolioModelError(ValueError):
    """Invalid input or state for the LP portfolio model."""


def _positive_decimal(value: Decimal | str | int | float, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PortfolioModelError(f"{field} must be a valid decimal") from None

    if not result.is_finite() or result <= 0:
        raise PortfolioModelError(f"{field} must be finite and positive")

    return result


@dataclass(frozen=True, slots=True)
class PortfolioPoint:
    """Portfolio valuation at one historical market observation."""

    timestamp: float
    price_quote_per_base: Decimal
    lp_amount_base: Decimal
    lp_amount_quote: Decimal
    lp_value_quote: Decimal
    hold_value_quote: Decimal
    impermanent_loss_fraction: Decimal
    impermanent_loss_pct: Decimal
    lp_pnl_quote: Decimal
    hold_pnl_quote: Decimal
    pnl_vs_hold_quote: Decimal


@dataclass(frozen=True, slots=True)
class LPPortfolioResult:
    """Complete historical valuation of one V2 LP position."""

    pool_id: str
    token0: str
    token1: str
    initial_timestamp: float
    initial_price_quote_per_base: Decimal
    initial_amount_base: Decimal
    initial_amount_quote: Decimal
    initial_capital_quote: Decimal
    observations: tuple[PortfolioPoint, ...]


class V2LPPortfolioModel:
    """Simulate a balanced, full-range V2 constant-product LP position."""

    def run(
        self,
        market_data: HistoricalMarketData,
        initial_capital_quote: Decimal | str | int | float,
    ) -> LPPortfolioResult:
        """Value a position from the first observation through the last.

        Initial capital is split equally by USD/quote value between
        token0 and token1. The first price observation is the entry price.

        All valuations use token1 as the quote currency.
        """

        if not isinstance(market_data, HistoricalMarketData):
            raise PortfolioModelError(
                "market_data must be HistoricalMarketData"
            )

        capital = _positive_decimal(
            initial_capital_quote,
            "initial_capital_quote",
        )

        first = market_data.observations[0]
        initial_price = _positive_decimal(
            first.price,
            "initial_price",
        )

        with localcontext() as ctx:
            ctx.prec = 40

            # Allocate half the initial capital to each token by value.
            initial_amount_base = capital / (Decimal(2) * initial_price)
            initial_amount_quote = capital / Decimal(2)

            # Constant-product invariant: x * y = k.
            invariant = initial_amount_base * initial_amount_quote

            points: list[PortfolioPoint] = []

            for observation in market_data.observations:
                price = _positive_decimal(
                    observation.price,
                    "observation price",
                )

                # Recalculate the LP's token composition at this price.
                current_amount_base = (invariant / price).sqrt()
                current_amount_quote = (invariant * price).sqrt()

                # Reuse the existing AMM calculator for valuation,
                # impermanent loss, and the hold benchmark.
                amm_result = evaluate_constant_product_position(
                    initial_amount_base=initial_amount_base,
                    initial_amount_quote=initial_amount_quote,
                    current_price_quote_per_base=price,
                )

                points.append(
                    PortfolioPoint(
                        timestamp=observation.timestamp,
                        price_quote_per_base=price,
                        lp_amount_base=current_amount_base,
                        lp_amount_quote=current_amount_quote,
                        lp_value_quote=amm_result.lp_value_before_fees_quote,
                        hold_value_quote=amm_result.hold_value_quote,
                        impermanent_loss_fraction=(
                            amm_result.impermanent_loss_fraction
                        ),
                        impermanent_loss_pct=amm_result.impermanent_loss_pct,
                        lp_pnl_quote=(
                            amm_result.lp_value_before_fees_quote - capital
                        ),
                        hold_pnl_quote=(
                            amm_result.hold_value_quote - capital
                        ),
                        pnl_vs_hold_quote=(
                            amm_result.lp_value_before_fees_quote
                            - amm_result.hold_value_quote
                        ),
                    )
                )

            return LPPortfolioResult(
                pool_id=market_data.pool_id,
                token0=market_data.token0,
                token1=market_data.token1,
                initial_timestamp=first.timestamp,
                initial_price_quote_per_base=initial_price,
                initial_amount_base=initial_amount_base,
                initial_amount_quote=initial_amount_quote,
                initial_capital_quote=capital,
                observations=tuple(points),
            )
