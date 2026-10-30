"""Estimated LP fee and transaction-cost accounting.

Fee income is estimated from an explicitly supplied annual APR.
It is not historical realized fee income.

All monetary values are denominated in token1 (quote currency).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from app.dex.backtest.portfolio import LPPortfolioResult


SECONDS_PER_YEAR = Decimal("31536000")
BPS_DENOMINATOR = Decimal("10000")


class AccountingModelError(ValueError):
    """Invalid fee or transaction-cost accounting input."""


def _decimal(value, field: str, *, allow_zero: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AccountingModelError(
            f"{field} must be a valid decimal"
        ) from None

    if not result.is_finite():
        raise AccountingModelError(f"{field} must be finite")

    if result < 0 or (not allow_zero and result == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise AccountingModelError(
            f"{field} must be {qualifier}"
        )

    return result


@dataclass(frozen=True, slots=True)
class ExecutionCostConfig:
    """Explicit transaction-cost assumptions in quote currency."""

    entry_gas_quote: Decimal = Decimal("0")
    exit_gas_quote: Decimal = Decimal("0")
    entry_swap_bps: Decimal = Decimal("0")
    exit_swap_bps: Decimal = Decimal("0")

    def __post_init__(self):
        for name in (
            "entry_gas_quote",
            "exit_gas_quote",
            "entry_swap_bps",
            "exit_swap_bps",
        ):
            value = _decimal(getattr(self, name), name)

            if "swap_bps" in name and value >= BPS_DENOMINATOR:
                raise AccountingModelError(
                    f"{name} must be below 10000 basis points"
                )

            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class NetPortfolioPoint:
    """Net portfolio valuation at one historical observation."""

    timestamp: float
    gross_lp_value_quote: Decimal
    hold_value_quote: Decimal
    interval_fee_quote: Decimal
    cumulative_fee_quote: Decimal
    entry_cost_quote: Decimal
    exit_cost_quote: Decimal
    cumulative_cost_quote: Decimal
    net_lp_value_quote: Decimal
    net_lp_pnl_quote: Decimal
    net_pnl_vs_hold_quote: Decimal


@dataclass(frozen=True, slots=True)
class NetPortfolioResult:
    """Historical LP performance after estimated fees and costs."""

    pool_id: str
    token0: str
    token1: str
    fee_apr_assumption: Decimal
    initial_capital_quote: Decimal
    observations: tuple[NetPortfolioPoint, ...]
    fee_method: str
    warnings: tuple[str, ...]


class LPAccountingModel:
    """Apply estimated fees and explicit execution costs to gross LP results."""

    def run(
        self,
        portfolio: LPPortfolioResult,
        fee_apr: Decimal | str | int | float,
        costs: ExecutionCostConfig | None = None,
    ) -> NetPortfolioResult:
        if not isinstance(portfolio, LPPortfolioResult):
            raise AccountingModelError(
                "portfolio must be LPPortfolioResult"
            )

        if not portfolio.observations:
            raise AccountingModelError(
                "portfolio must contain observations"
            )

        apr = _decimal(fee_apr, "fee_apr")
        costs = costs or ExecutionCostConfig()

        if not isinstance(costs, ExecutionCostConfig):
            raise AccountingModelError(
                "costs must be ExecutionCostConfig"
            )

        with localcontext() as ctx:
            ctx.prec = 40

            capital = portfolio.initial_capital_quote

            entry_cost = (
                costs.entry_gas_quote
                + capital * costs.entry_swap_bps / BPS_DENOMINATOR
            )

            cumulative_fees = Decimal("0")
            cumulative_costs = Decimal("0")
            results = []

            previous = None
            last_index = len(portfolio.observations) - 1

            for index, point in enumerate(portfolio.observations):
                gross_value = point.lp_value_quote
                interval_fee = Decimal("0")

                if previous is not None:
                    elapsed = Decimal(
                        str(point.timestamp - previous.timestamp)
                    )

                    if elapsed <= 0:
                        raise AccountingModelError(
                            "portfolio timestamps must increase"
                        )

                    average_value = (
                        previous.lp_value_quote + gross_value
                    ) / Decimal("2")

                    interval_fee = (
                        average_value * apr * elapsed / SECONDS_PER_YEAR
                    )

                cumulative_fees += interval_fee

                current_entry_cost = (
                    entry_cost if index == 0 else Decimal("0")
                )

                current_exit_cost = Decimal("0")

                if index == last_index:
                    current_exit_cost = (
                        costs.exit_gas_quote
                        + gross_value
                        * costs.exit_swap_bps
                        / BPS_DENOMINATOR
                    )

                cumulative_costs += (
                    current_entry_cost + current_exit_cost
                )

                net_value = (
                    gross_value
                    + cumulative_fees
                    - cumulative_costs
                )

                results.append(
                    NetPortfolioPoint(
                        timestamp=point.timestamp,
                        gross_lp_value_quote=gross_value,
                        hold_value_quote=point.hold_value_quote,
                        interval_fee_quote=interval_fee,
                        cumulative_fee_quote=cumulative_fees,
                        entry_cost_quote=current_entry_cost,
                        exit_cost_quote=current_exit_cost,
                        cumulative_cost_quote=cumulative_costs,
                        net_lp_value_quote=net_value,
                        net_lp_pnl_quote=net_value - capital,
                        net_pnl_vs_hold_quote=(
                            net_value - point.hold_value_quote
                        ),
                    )
                )

                previous = point

            return NetPortfolioResult(
                pool_id=portfolio.pool_id,
                token0=portfolio.token0,
                token1=portfolio.token1,
                fee_apr_assumption=apr,
                initial_capital_quote=capital,
                observations=tuple(results),
                fee_method="constant_apr_trapezoidal_estimate",
                warnings=(
                    "Fee income is estimated using a constant assumed APR; it is not realized historical LP income.",
                    "Pool volume, actual liquidity share, fee collection events, and fee compounding are not reconstructed.",
                    "Gas and swap costs are explicit scenario inputs, not measured historical execution costs.",
                    "Exit costs are applied to the final observation, which is treated as the position close.",
                ),
            )
