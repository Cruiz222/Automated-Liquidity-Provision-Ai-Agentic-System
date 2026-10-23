"""AI Pool Selection Agent for automated liquidity provision.

Evaluates liquidity pools based on risk-adjusted net returns, not simply
choosing the coin with the highest trading volume or most volatile price.

Supports three pool categories with initial policies as described in the
project roadmap:
  1. Stablecoin-stablecoin: Lower relative price-risk candidate, while
     checking depeg and contract risks
  2. Correlated volatile assets: Evaluate for moderate divergence risk
     and fee potential
  3. Uncorrelated volatile assets: Apply stricter risk limits and require
     stronger evidence that fees compensate for divergence risk
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .wallet_ledger import WalletLedger, InsufficientFunds
from .defi_milestone import ConversionQuotes, LiquidityPositions, QuoteExpired


class PoolCategory(Enum):
    """Pool risk categories with initial policy guidance."""

    STABLECOIN_STABLECOIN = "stablecoin-stablecoin"
    CORRELATED_VOLATILE = "correlated-volatile"
    UNCORRELATED_VOLATILE = "uncorrelated-volatile"


@dataclass
class PoolSpec:
    """Specifications for a liquidity pool."""

    pool_id: str
    category: PoolCategory
    base_token: str  # e.g., "ETH", "BTC"
    quote_token: str  # e.g., "USDT", "NGN"
    pool_name: str  # human-readable name

    # Pool characteristics (from market analysis)
    daily_volume_minor: int  # minor units (micro-USDT or kobo)
    spread_bps: Decimal  # current spread in basis points
    volatility_30d: Decimal  # 30-day price volatility (annualized % as Decimal)
    total_value_locked_minor: int  # TVL in minor units

    # Calculated metrics
    expected_fee_bps: Decimal = field(default=Decimal("50"))
    risk_score: float = field(default=0.0)  # 0.0 = lowest risk
    efficiency_score: Decimal = field(default=Decimal("0"))  # fee_return / risk

    # Capital allocation
    max_capital_minor: int = field(default=0)  # maximum capital to commit
    allocated_capital_minor: int = field(default=0)  # currently allocated


@dataclass
class PoolEvaluation:
    """Result of evaluating a liquidity pool."""

    pool: PoolSpec
    category: PoolCategory
    suitability_score: Decimal  # 0 to 1, higher is better
    recommended_capital: Decimal  # recommended capital in minor units
    risk_score: float = field(default=0.0)  # 0.0 = lowest risk
    risk_warnings: List[str] = field(default_factory=list)
    fee_estimate_yield: Decimal = Decimal("0")  # estimated annual fee yield
    il_risk: Decimal = Decimal("0")  # impermanent loss risk estimate


@dataclass
class CapitalAllocation:
    """Capital allocation result across selected pools."""

    total_capital_minor: int
    allocations: Dict[str, int]  # pool_id -> allocated capital in minor units
    remaining_capital_minor: int
    rationale: str


class PoolSelectionError(Exception):
    """Raised when pool selection cannot proceed."""

    pass


class InsufficientCapitalError(PoolSelectionError):
    """Raised when total requested capital exceeds available funds."""


# ════════════════════════════════════════════════════════════════
# Pool Evaluation Logic
# ════════════════════════════════════════════════════════════════

def evaluate_pool(spec: PoolSpec) -> PoolEvaluation:
    """Evaluate a liquidity pool and return suitability score.

    The evaluation considers:
    - Fee income potential (higher = better)
    - Risk factors (volatility, TVL, category-specific risks)
    - Impermanent loss risk
    - Liquidity depth

    Returns a PoolEvaluation with suitability score and recommendations.
    """
    pool = spec
    category = pool.category

    # 1. Fee yield estimation (annualized)
    # Assuming ~365 days of similar trading activity
    fee_yield = pool.expected_fee_bps / Decimal(10_000) * Decimal(str(
        pool.daily_volume_minor)) / Decimal(str(pool.total_value_locked_minor)) * Decimal(365)

    # 2. Risk scoring based on category
    risk_score = 0.0
    risk_warnings: List[str] = []

    if category == PoolCategory.STABLECOIN_STABLECOIN:
        # Lower risk: stable pairs should have minimal volatility
        if pool.volatility_30d > Decimal("50"):  # >50% annualized is high for stablecoins
            risk_warnings.append(
                "High volatility for stablecoin pair - check for depeg risk")
        risk_score = float(pool.volatility_30d) / 100.0  # normalize

    elif category == PoolCategory.CORRELATED_VOLATILE:
        # Moderate risk: correlated assets have divergence risk
        # BTC/ETH, etc. - fees can compensate for some divergence
        risk_score = float(pool.volatility_30d) / 200.0  # lower than uncorrelated
        if pool.volatility_30d > Decimal("100"):
            risk_warnings.append(
                "High volatility - ensure fee compensation for divergence risk")
        # Check spread impact
        if pool.spread_bps > Decimal("100"):
            risk_warnings.append(
                "Wide spread reduces effective fee income")

    elif category == PoolCategory.UNCORRELATED_VOLATILE:
        # Strict risk: uncorrelated assets (e.g., BTC/NGN) have highest divergence risk
        risk_score = float(pool.volatility_30d) / 100.0  # normalize fully
        if pool.volatility_30d > Decimal("150"):
            risk_warnings.append(
                "Very high volatility - strict risk limits apply")
        if pool.spread_bps > Decimal("150"):
            risk_warnings.append(
                "Very wide spread - fees may not compensate divergence risk")
        if pool.total_value_locked_minor < Decimal("100_000_000"):
            risk_warnings.append(
                "Low TVL - higher slippage and exit risk")

    # 3. Efficiency score = fee yield / risk (capped)
    if risk_score > 0:
        efficiency = fee_yield / Decimal(risk_score)
    else:
        efficiency = fee_yield

    # Cap efficiency at reasonable levels
    efficiency = min(efficiency, Decimal("0.5"))

    # 4. Impermanent loss risk estimate
    # Simplified: IL risk increases with volatility and spread
    il_risk = min(pool.volatility_30d / Decimal("200"), Decimal("1.0"))

    # 5. Suitability score combines efficiency and risk
    # Higher efficiency AND lower risk = better suitability
    suitability = efficiency / (Decimal(risk_score) + Decimal("0.1"))

    # Normalize to 0-1 range (rough)
    suitability_score = min(suitability, Decimal("1.0"))

    # 6. Recommend capital based on risk and TVL
    # More capital to low-risk, high-efficiency pools
    capital_fraction = suitability * (Decimal(str(pool.total_value_locked_minor)) /
                                       (Decimal(str(pool.total_value_locked_minor)) + Decimal("10_000_000")))

    recommended_capital = Decimal(str(spec.daily_volume_minor)) * \
        (Decimal("0.1") + capital_fraction * Decimal("0.9"))  # 10-100% of daily volume

    return PoolEvaluation(
        pool=pool,
        category=category,
        suitability_score=suitability_score,
        recommended_capital=recommended_capital,
        risk_warnings=risk_warnings,
        fee_estimate_yield=fee_yield,
        il_risk=il_risk,
    )


# ════════════════════════════════════════════════════════════════
# Pool Selector Agent
# ════════════════════════════════════════════════════════════════

class PoolSelector:
    """AI agent that selects and allocates capital across liquidity pools."""

    def __init__(
        self,
        ledger: WalletLedger,
        ngn_per_usdt: Decimal = Decimal("1500"),
        fee_bps: int = 50,
        max_inventory_usdt: int = 5_000_000,  # 5M USDT max exposure
    ):
        self.ledger = ledger
        self.conversion_quotes = ConversionQuotes(ledger, ngn_per_usdt, fee_bps)
        self.max_inventory_usdt = max_inventory_usdt

        # Category policies per the roadmap
        self.category_policies = {
            PoolCategory.STABLECOIN_STABLECOIN: {
                "max_risk_score": 0.3,
                "min_suitability": Decimal("0.3"),
                "capital_weight": Decimal("0.4"),  # 40% of capital goes here
                "il_tolerance": Decimal("0.2"),
            },
            PoolCategory.CORRELATED_VOLATILE: {
                "max_risk_score": 0.5,
                "min_suitability": Decimal("0.4"),
                "capital_weight": Decimal("0.4"),  # 40% of capital goes here
                "il_tolerance": Decimal("0.4"),
            },
            PoolCategory.UNCORRELATED_VOLATILE: {
                "max_risk_score": 0.7,
                "min_suitability": Decimal("0.5"),
                "capital_weight": Decimal("0.2"),  # 20% of capital goes here
                "il_tolerance": Decimal("0.6"),
            },
        }

    def add_pool(self, spec: PoolSpec) -> None:
        """Add a pool to the evaluation queue."""
        if not isinstance(spec.category, PoolCategory):
            raise ValueError(f"Invalid pool category: {spec.category}")
        # Store pool - in a full implementation, this would be in a registry
        setattr(self, f"_pool_{spec.pool_id}", spec)

    def select_pools(
        self,
        available_capital_minor: int,
        min_suitability: Optional[Decimal] = None,
    ) -> Tuple[List[PoolEvaluation], CapitalAllocation]:
        """Select optimal pools and allocate capital.

        Args:
            available_capital_minor: Capital available in minor units (micro-USDT)
            min_suitability: Minimum suitability score to consider a pool

        Returns:
            Tuple of (selected_evaluations, capital_allocation)
        """
        if min_suitability is None:
            min_suitability = Decimal("0.3")

        # Collect all known pools
        pools = self._gather_pools()

        # Evaluate each pool
        evaluations: List[PoolEvaluation] = []
        for spec in pools:
            eval_result = evaluate_pool(spec)

            # Apply category-specific filters
            policy = self.category_policies.get(eval_result.category, {})
            max_risk = Decimal(str(policy.get("max_risk_score", 0.7)))
            min_suit = Decimal(str(policy.get("min_suitability", "0.3")))

            # Filter by risk score
            if eval_result.risk_score > float(max_risk):
                continue

            # Filter by minimum suitability
            if eval_result.suitability_score < min_suit:
                continue

            evaluations.append(eval_result)

        if not evaluations:
            raise PoolSelectionError(
                "No pools meet the minimum suitability and risk criteria")

        # Sort by suitability score (highest first)
        evaluations.sort(key=lambda e: e.suitability_score, reverse=True)

        # Allocate capital using a proportional strategy
        # Give preference to higher suitability, but diversify across categories
        allocation = self._allocate_capital(
            evaluations, available_capital_minor
        )

        return evaluations, allocation

    def _gather_pools(self) -> List[PoolSpec]:
        """Gather all known pools. In production, this would query a registry."""

        # For now, return example pools based on the project's focus on NGN/USDT
        # In a full implementation, these would come from market data analysis
        pools: List[PoolSpec] = []

        # Stablecoin-stablecoin: USDT/NGN pool (the core focus)
        pools.append(PoolSpec(
            pool_id="usdt-ngn-pool",
            category=PoolCategory.STABLECOIN_STABLECOIN,
            base_token="NGN",
            quote_token="USDT",
            pool_name="USDT/NGN Stable Pair",
            daily_volume_minor=150_000_000,  # 150,000 NGN ≈ 100 USDT daily
            spread_bps=Decimal("20"),
            volatility_30d=Decimal("10"),
            total_value_locked_minor=5_000_000_000,  # 5B micro-USDT = 5K USDT
            expected_fee_bps=Decimal("30"),
            max_capital_minor=5_000_000_000,  # 5K USDT max capital
        ))

        # Correlated volatile: ETH/USDT
        pools.append(PoolSpec(
            pool_id="eth-usdt-pool",
            category=PoolCategory.CORRELATED_VOLATILE,
            base_token="ETH",
            quote_token="USDT",
            pool_name="ETH/USDT Correlated",
            daily_volume_minor=30_000_000,  # 30M micro-USDT daily
            spread_bps=Decimal("50"),
            volatility_30d=Decimal("80"),
            total_value_locked_minor=20_000_000_000,  # 20B micro-USDT
            expected_fee_bps=Decimal("80"),
            max_capital_minor=3_000_000_000,  # 3K USDT max capital
        ))

        # Uncorrelated volatile: BTC/USDT (less correlated in stress scenarios)
        pools.append(PoolSpec(
            pool_id="btc-usdt-pool",
            category=PoolCategory.UNCORRELATED_VOLATILE,
            base_token="BTC",
            quote_token="USDT",
            pool_name="BTC/USDT Uncorrelated",
            daily_volume_minor=10_000_000,  # 10M micro-USDT daily
            spread_bps=Decimal("100"),
            volatility_30d=Decimal("120"),
            total_value_locked_minor=15_000_000_000,  # 15B micro-USDT
            expected_fee_bps=Decimal("120"),
            max_capital_minor=2_000_000_000,  # 2K USDT max capital
        ))

        return pools

    def _allocate_capital(
        self,
        evaluations: List[PoolEvaluation],
        available_capital_minor: int,
    ) -> CapitalAllocation:
        """Allocate capital across selected pool evaluations."""

        total_allocated = 0
        allocations: Dict[str, int] = {}
        remaining = available_capital_minor

        # Separate by category for balanced allocation
        by_category: Dict[PoolCategory, List[PoolEvaluation]] = {}
        for eval_result in evaluations:
            cat = eval_result.category
            by_category.setdefault(cat, []).append(eval_result)

        # Priority order: stablecoin first (safer), then correlated, then uncorrelated
        category_order = [
            PoolCategory.STABLECOIN_STABLECOIN,
            PoolCategory.CORRELATED_VOLATILE,
            PoolCategory.UNCORRELATED_VOLATILE,
        ]

        for cat in category_order:
            if cat not in by_category:
                continue

            policy = self.category_policies[cat]
            capital_weight = policy["capital_weight"]
            max_risk = Decimal(str(policy["max_risk_score"]))

            # Distribute weighted capital among pools in this category
            category_capital = int(available_capital_minor * float(capital_weight))

            # Sort within category by suitability (highest first)
            category_pools = sorted(
                by_category[cat], key=lambda e: e.suitability_score, reverse=True
            )

            # Allocate proportionally based on suitability score
            total_suitability = sum(e.suitability_score for e in category_pools)

            if total_suitability == 0:
                # Equal split if no suitability differentiation
                pool_share = category_capital / len(category_pools) if category_pools else 0
            else:
                pool_share = None

            for eval_result in category_pools:
                pool_id = eval_result.pool.pool_id

                if total_suitability > 0:
                    # Proportional allocation
                    share = int(
                        category_capital
                        * float(eval_result.suitability_score / total_suitability)
                    )
                else:
                    # Equal split
                    share = int(category_capital / len(category_pools)) if category_pools else 0

                # Cap at remaining capital and pool's max
                share = min(share, remaining)
                share = min(share, eval_result.pool.max_capital_minor - eval_result.pool.allocated_capital_minor)

                if share <= 0:
                    continue

                allocations[pool_id] = share
                total_allocated += share
                remaining -= share

                # Track allocated capital in the pool spec
                # (in a full implementation, this would update the pool registry)

        return CapitalAllocation(
            total_capital_minor=available_capital_minor,
            allocations=allocations,
            remaining_capital_minor=remaining,
            rationale=self._generate_rationale(evaluations, allocations, available_capital_minor),
        )

    def _generate_rationale(
        self,
        evaluations: List[PoolEvaluation],
        allocations: Dict[str, int],
        total_capital: int,
    ) -> str:
        """Generate human-readable rationale for the allocation."""

        lines = [
            f"Pool allocation of {total_capital:,} micro-USDT across {len(evaluations)} selected pools:",
        ]

        for eval_result in evaluations:
            pool_id = eval_result.pool.pool_id
            allocated = allocations.get(pool_id, 0)
            if allocated > 0:
                lines.append(
                    f"  - {pool_id} ({eval_result.pool.pool_name}): "
                    f"{allocated:,} micro-USDT "
                    f"(suitability: {eval_result.suitability_score:.2f}, "
                    f"fee yield: {eval_result.fee_estimate_yield:.4f} IL risk: "
                    f"{eval_result.il_risk:.2f})"
                )

        if any(eval_result.risk_warnings for eval_result in evaluations):
            lines.append("")
            lines.append("Risk warnings:")
            for eval_result in evaluations:
                for warning in eval_result.risk_warnings:
                    lines.append(f"  • {eval_result.pool.pool_id}: {warning}")

        return "\n".join(lines)

    def execute_conversion_to_usdt(
        self,
        account: str,
        ngn_amount_minor: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Convert NGN to USDT for pool funding.

        Args:
            account: User account identifier
            ngn_amount_minor: NGN amount in kobo (1 NGN = 1 kobo)
            idempotency_key: Unique key for idempotent execution

        Returns:
            Execution result dict
        """
        # Create a conversion quote
        quote = self.conversion_quotes.create_quote(
            account=account,
            source_currency="NGN",
            target_currency="USDT",
            source_amount_minor=ngn_amount_minor,
        )

        # Execute the quote
        result = self.conversion_quotes.execute(
            quote["quote_id"],
            idempotency_key,
        )

        return result

    def open_liquidity_position(
        self,
        account: str,
        pool_id: str,
        asset: str,
        target_currency: str,
        amount_minor: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Open a liquidity position in a selected pool.

        Args:
            account: User account identifier
            pool_id: Pool identifier
            asset: The asset being provided (e.g., "ETH")
            target_currency: The currency being deposited (e.g., "USDT")
            amount_minor: Amount in minor units
            idempotency_key: Unique key for idempotent execution

        Returns:
            Position opening result
        """
        positions = LiquidityPositions(self.ledger)
        result = positions.open_position(
            account=account,
            pool_id=pool_id,
            asset=asset,
            target_currency=target_currency,
            amount_minor=amount_minor,
            idempotency_key=idempotency_key,
        )

        return result

    def get_portfolio_status(self, account: str) -> dict[str, Any]:
        """Get the current portfolio status for an account.

        Returns balances, open positions, and valuation.
        """
        balances = self.ledger.balances(account)

        # Get all positions for this account
        positions = LiquidityPositions(self.ledger)
        position_list = positions.list_positions(account)

        # Calculate total valuation
        total_valuation_usdt = balances.get("USDT", 0)
        total_valuation_ngn = balances.get("NGN", 0)

        # Convert NGN to USDT equivalent
        if total_valuation_ngn > 0:
            total_valuation_usdt += int(
                Decimal(str(total_valuation_ngn)) / Decimal("1500") * Decimal("1_000_000")
            )

        # Add position valuations
        for pos in position_list:
            pos_status = positions.get_position(pos["position_id"])
            # current_value + accrued fees
            pos_valuation = pos_status.get("current_value_minor", 0) + pos_status.get("accrued_fees_minor", 0)
            # Convert to USDT equivalent if not already
            if pos_status.get("target_currency") != "USDT":
                # Simplified conversion
                pos_valuation = int(
                    Decimal(str(pos_valuation)) / Decimal("1500") * Decimal("1_000_000")
                )

            total_valuation_usdt += pos_valuation

        return {
            "account": account,
            "balances": balances,
            "total_valuation_usdt_approx": total_valuation_usdt,
            "open_positions": len(position_list),
            "position_details": [
                {
                    "pool_id": p.get("pool_id"),
                    "asset": p.get("principal_minor"),
                    "status": p.get("status"),
                    "current_value_usdt": positions.get_position(p["position_id"]).get(
                        "current_value_minor", 0
                    ),
                }
                for p in position_list
            ],
        }


# ════════════════════════════════════════════════════════════════
# Convenience Functions
# ════════════════════════════════════════════════════════════════

def create_default_selector(
    ledger: WalletLedger,
    ngn_per_usdt: Decimal = Decimal("1500"),
    fee_bps: int = 50,
) -> PoolSelector:
    """Create a PoolSelector with default NGN/USDT-focused pools.

    Pre-configures pools relevant to the project's focus on NGN/USDT
    liquidity provision with three risk categories.
    """
    selector = PoolSelector(ledger, ngn_per_usdt, fee_bps)

    # The pools are auto-added in _gather_pools(), but we also
    # explicitly register them here for clarity
    return selector


def quick_evaluate_ngn_usdt_pools(
    ledger: WalletLedger,
    ngn_per_usdt: Decimal = Decimal("1500"),
) -> List[PoolEvaluation]:
    """Quick evaluation of NGN/USDT-relevant pools.

    Returns evaluated pools sorted by suitability for immediate use.
    """
    from .pool_selector import PoolSelector, PoolCategory, PoolSpec

    selector = PoolSelector(ledger, ngn_per_usdt)
    evaluations = selector.select_pools(available_capital_minor=10_000_000)  # 10M micro-USDT = 10 USDT

    # Return sorted by suitability
    return sorted(evaluations[0], key=lambda e: e.suitability_score, reverse=True)


if __name__ == "__main__":
    # Demonstration mode
    import sys

    print("=" * 60)
    print("AI Pool Selection Agent - Demonstration")
    print("=" * 60)

    # Note: This requires a database connection
    # For demo purposes, we'll show the evaluation logic
    from decimal import Decimal

    # Example pool specs (these would come from market analysis)
    example_pools = [
        PoolSpec(
            pool_id="usdt-ngn-pool",
            category=PoolCategory.STABLECOIN_STABLECOIN,
            base_token="NGN",
            quote_token="USDT",
            pool_name="USDT/NGN Stable Pair",
            daily_volume_minor=150_000_000,
            spread_bps=Decimal("20"),
            volatility_30d=Decimal("10"),
            total_value_locked_minor=5_000_000_000,
        ),
        PoolSpec(
            pool_id="eth-usdt-pool",
            category=PoolCategory.CORRELATED_VOLATILE,
            base_token="ETH",
            quote_token="USDT",
            pool_name="ETH/USDT Correlated",
            daily_volume_minor=30_000_000,
            spread_bps=Decimal("50"),
            volatility_30d=Decimal("80"),
            total_value_locked_minor=20_000_000_000,
        ),
        PoolSpec(
            pool_id="btc-usdt-pool",
            category=PoolCategory.UNCORRELATED_VOLATILE,
            base_token="BTC",
            quote_token="USDT",
            pool_name="BTC/USDT Uncorrelated",
            daily_volume_minor=10_000_000,
            spread_bps=Decimal("100"),
            volatility_30d=Decimal("120"),
            total_value_locked_minor=15_000_000_000,
        ),
    ]

    print("\nEvaluating pools:\n")
    for spec in example_pools:
        eval_result = evaluate_pool(spec)
        print(f"Pool: {spec.pool_id} ({spec.pool_name})")
        print(f"  Category: {spec.category.value}")
        print(f"  Suitability: {eval_result.suitability_score:.3f}")
        print(f"  Risk Score: {eval_result.risk_score:.3f}")
        print(f"  Fee Yield (annual): {eval_result.fee_estimate_yield:.4f}")
        print(f"  IL Risk: {eval_result.il_risk:.3f}")
        print(f"  Risk Warnings: {eval_result.risk_warnings or 'None'}")
        print(f"  Recommended Capital: {eval_result.recommended_capital:.0f} micro-USDT")
        print()

    # Demonstrate selection
    print("=" * 60)
    print("Capital Allocation (100 USDT = 10,000,000 micro-USDT):")
    print("=" * 60)

    from .pool_selector import PoolSelector, WalletLedger

    # Create a mock ledger for demo
    import tempfile
    import os

    tmpdir = tempfile.mkdtemp()
    demo_ledger = WalletLedger(os.path.join(tmpdir, "demo.sqlite3"))

    selector = PoolSelector(demo_ledger)
    selector._gather_pools()  # Populates example pools

    try:
        evaluations, allocation = selector.select_pools(
            available_capital_minor=10_000_000  # 10 USDT
        )

        print(f"Selected {len(evaluations)} pools for allocation")
        print(f"Total allocated: {sum(allocation.allocations.values()):,}/10,000,000 micro-USDT")
        print(f"Remaining: {allocation.remaining_capital_minor:,}")
        print()
        for pool_id, amount in sorted(allocation.allocations.items(), key=lambda x: -x[1]):
            print(f"  {pool_id}: {amount:,} micro-USDT ({amount/10_000:.2f} USDT)")
        print()
        print(f"Rationale:\n{allocation.rationale}")
    except PoolSelectionError as e:
        print(f"Selection error: {e}")

    print("\n" + "=" * 60)