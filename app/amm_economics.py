"""Research-only constant-product AMM position and impermanent-loss calculator.

Amounts are token quantities, and prices/valuations are denominated in token1
(quote) per token0 (base). This is a simplified, fee-tier-agnostic v2-style model;
it is not suitable for concentrated-liquidity pools without a different model.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext


class AMMModelError(ValueError):
    """Invalid input for the simplified constant-product AMM model."""


def _positive(value: Decimal | str | int | float, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AMMModelError(f"{field} must be a valid decimal") from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise AMMModelError(f"{field} must be finite and {qualifier}")
    return result


@dataclass(frozen=True)
class AMMPositionResult:
    initial_price_quote_per_base: Decimal
    current_price_quote_per_base: Decimal
    price_ratio: Decimal
    initial_value_quote: Decimal
    hold_value_quote: Decimal
    lp_value_before_fees_quote: Decimal
    impermanent_loss_fraction: Decimal
    impermanent_loss_pct: Decimal
    accrued_fees_quote: Decimal
    gas_cost_quote: Decimal
    net_lp_value_quote: Decimal
    net_pnl_vs_initial_quote: Decimal
    net_pnl_vs_hold_quote: Decimal
    warnings: tuple[str, ...]

    def as_dict(self) -> dict:
        return {key: (str(value) if isinstance(value, Decimal) else list(value) if isinstance(value, tuple) else value)
                for key, value in self.__dict__.items()}


def impermanent_loss_fraction(price_ratio: Decimal | str | int | float) -> Decimal:
    """Return IL as a fraction versus holding, e.g. -0.057 for about -5.7%."""
    ratio = _positive(price_ratio, "price_ratio")
    with localcontext() as ctx:
        ctx.prec = 40
        root = ratio.sqrt()
        return (Decimal(2) * root / (Decimal(1) + ratio)) - Decimal(1)


def evaluate_constant_product_position(
    initial_amount_base: Decimal | str | int | float,
    initial_amount_quote: Decimal | str | int | float,
    current_price_quote_per_base: Decimal | str | int | float,
    accrued_fees_quote: Decimal | str | int | float = 0,
    gas_cost_quote: Decimal | str | int | float = 0,
) -> AMMPositionResult:
    """Estimate v2-style LP value, fees, gas and comparison with holding.

    Fee and gas inputs are denominated in the quote token. Price is quote-token
    units per one base token. No external price feed is consulted.
    """
    base = _positive(initial_amount_base, "initial_amount_base")
    quote = _positive(initial_amount_quote, "initial_amount_quote")
    current_price = _positive(current_price_quote_per_base, "current_price_quote_per_base")
    fees = _positive(accrued_fees_quote, "accrued_fees_quote", allow_zero=True)
    gas = _positive(gas_cost_quote, "gas_cost_quote", allow_zero=True)
    initial_price = quote / base
    ratio = current_price / initial_price
    with localcontext() as ctx:
        ctx.prec = 40
        invariant = base * quote
        current_base = (invariant / current_price).sqrt()
        current_quote = (invariant * current_price).sqrt()
        initial_value = base * initial_price + quote
        hold_value = base * current_price + quote
        lp_value = current_base * current_price + current_quote
        il_fraction = impermanent_loss_fraction(ratio)
        net_lp_value = lp_value + fees - gas
        warnings = (
            "Simplified constant-product (v2-style) model; not valid for concentrated-liquidity positions without adaptation.",
            "Assumes arbitrage restores pool price to the supplied current price and ignores fee-tier, range, MEV, taxes, and transaction-path effects.",
            "Outputs are scenario estimates, not a return forecast or investment advice.",
        )
        return AMMPositionResult(
            initial_price_quote_per_base=initial_price,
            current_price_quote_per_base=current_price,
            price_ratio=ratio,
            initial_value_quote=initial_value,
            hold_value_quote=hold_value,
            lp_value_before_fees_quote=lp_value,
            impermanent_loss_fraction=il_fraction,
            impermanent_loss_pct=il_fraction * Decimal(100),
            accrued_fees_quote=fees,
            gas_cost_quote=gas,
            net_lp_value_quote=net_lp_value,
            net_pnl_vs_initial_quote=net_lp_value - initial_value,
            net_pnl_vs_hold_quote=net_lp_value - hold_value,
            warnings=warnings,
        )
