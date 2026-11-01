"""Pool selection and capital allocation for DEX liquidity provision.

For every :class:`~app.pool_data.PoolSnapshot` the selector estimates

* fee APR  = fee tier x daily volume / TVL x 365 (cross-checked against any
  source-reported APR; the lower figure is used),
* expected IL cost over the holding horizon (:func:`app.il.expected_v2_il`),
* a depeg haircut for stablecoin pairs,
* net APR = fee APR - annualized IL - haircut, and a 0-1 risk score,

then applies per-category policy (stablecoin pairs first, correlated pairs with
moderate limits, uncorrelated pairs only if fees clearly out-earn expected IL),
sizes positions under pool, category and TVL-share caps, keeps a cash reserve,
and drops pools whose gas costs would eat the yield at the proposed size.

Scope: positions are modelled as full-range (constant-product style) exposure.
Concentrated-liquidity range management is not modelled here. Output is a
research recommendation based on the snapshots supplied; it is not investment
advice and its inputs are only as good as the data source.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .il import (
    annualized_volatility,
    annualized_volatility_from_observations,
    expected_v2_il,
)
from .pool_data import PoolCategory, PoolSnapshot

MICRO = 1_000_000


@dataclass(frozen=True)
class CategoryLimits:
    max_risk: float          # reject pools with a risk score above this
    min_net_apr: float       # reject pools whose net APR is below this
    min_coverage: float      # fee APR must be at least this multiple of annualized IL
    max_weight: float        # share of deployable capital this category may take


DEFAULT_CATEGORY_LIMITS = {
    PoolCategory.STABLECOIN_STABLECOIN: CategoryLimits(0.35, 0.015, 1.0, 0.60),
    PoolCategory.CORRELATED_VOLATILE: CategoryLimits(0.60, 0.05, 1.5, 0.30),
    PoolCategory.UNCORRELATED_VOLATILE: CategoryLimits(0.80, 0.12, 2.5, 0.15),
}
DEFAULT_GAS_USD = {"ethereum": 8.0, "arbitrum": 0.4, "base": 0.2, "polygon": 0.15, "bsc": 0.3, "optimism": 0.2}


@dataclass(frozen=True)
class SelectorConfig:
    horizon_days: int = 30
    max_snapshot_age_hours: float = 24.0
    min_tvl_usd: float = 1_000_000.0
    min_daily_volume_usd: float = 50_000.0
    min_history_points: int = 14
    min_history_days: float = 14.0
    max_history_gap_hours: float = 48.0
    assumed_stable_vol: float = 0.02
    reserve_fraction: float = 0.10
    max_pool_weight: float = 0.40
    max_pool_tvl_share: float = 0.01
    min_position_usd: float = 50.0
    depeg_max_haircut_apr: float = 0.02
    default_gas_usd: float = 3.0
    concentrated_fee_haircut: float = 0.5   # pool-wide fee APR overstates a full-range LP in v3-style pools
    gas_usd: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GAS_USD))
    category_limits: dict[PoolCategory, CategoryLimits] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_LIMITS))

    def __post_init__(self) -> None:
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.min_history_points < 3:
            raise ValueError("min_history_points must be at least 3")
        if (
            isinstance(self.min_history_days, bool)
            or not isinstance(self.min_history_days, (int, float))
            or not math.isfinite(self.min_history_days)
            or self.min_history_days <= 0
        ):
            raise ValueError("min_history_days must be positive and finite")
        if (
            isinstance(self.max_history_gap_hours, bool)
            or not isinstance(self.max_history_gap_hours, (int, float))
            or not math.isfinite(self.max_history_gap_hours)
            or self.max_history_gap_hours <= 0
        ):
            raise ValueError("max_history_gap_hours must be positive and finite")
        if not 0 <= self.reserve_fraction < 1:
            raise ValueError("reserve_fraction must be in [0, 1)")
        if not 0 < self.max_pool_weight <= 1 or not 0 < self.max_pool_tvl_share <= 1:
            raise ValueError("max_pool_weight and max_pool_tvl_share must be in (0, 1]")


class PoolSelectionError(Exception):
    pass


@dataclass
class PoolEvaluation:
    pool_id: str
    category: PoolCategory
    eligible: bool
    reasons: list[str] = field(default_factory=list)     # why it is ineligible
    warnings: list[str] = field(default_factory=list)
    chain: str = ""
    tokens: tuple[str, str] = ("", "")
    tvl_usd: float = 0.0
    latest_price: float | None = None
    price0_usd: float | None = None
    price1_usd: float | None = None
    daily_volume_usd: float | None = None
    volatility: float | None = None
    fee_apr: float = 0.0
    il_apr: float = 0.0
    depeg_haircut_apr: float = 0.0
    net_apr: float = 0.0
    coverage: float | None = None     # fee APR / IL APR; None when IL is ~0
    risk_score: float = 1.0
    score: float = 0.0                # net APR per unit of (0.1 + risk); drives allocation weights

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["category"] = self.category.value
        data["tokens"] = list(self.tokens)
        return data


@dataclass
class CapitalAllocation:
    total_capital_minor: int
    allocations: dict[str, int]        # pool_id -> micro-USDT
    reserve_minor: int                 # held back by policy
    unallocated_minor: int             # deployable but not placed (caps / no eligible pool)
    details: dict[str, dict[str, Any]]
    rationale: str

    @property
    def allocated_minor(self) -> int:
        return sum(self.allocations.values())


@dataclass
class SelectionResult:
    evaluations: list[PoolEvaluation]
    excluded: dict[str, list[str]]
    allocation: CapitalAllocation

    @property
    def selected(self) -> list[PoolEvaluation]:
        return [e for e in self.evaluations if e.pool_id in self.allocation.allocations]


class PoolSelector:
    def __init__(self, config: SelectorConfig | None = None) -> None:
        self.config = config or SelectorConfig()
        self._pools: dict[str, PoolSnapshot] = {}

    # ---- registry ---------------------------------------------------------

    def add_pool(self, snapshot: PoolSnapshot) -> None:
        if not isinstance(snapshot, PoolSnapshot):
            raise ValueError("add_pool expects a PoolSnapshot")
        self._pools[snapshot.pool_id] = snapshot

    def add_pools(self, snapshots: Iterable[PoolSnapshot]) -> None:
        for snapshot in snapshots:
            self.add_pool(snapshot)

    @property
    def pools(self) -> list[PoolSnapshot]:
        return [self._pools[key] for key in sorted(self._pools)]

    # ---- evaluation -------------------------------------------------------

    @staticmethod
    def _history_prices(pool: PoolSnapshot) -> tuple[float, ...]:
        """Return timestamped observations when available, else legacy prices."""
        if pool.price_observations:
            return tuple(price for _, price in pool.price_observations)
        return pool.price_history

    def _volatility(self, pool: PoolSnapshot, category: PoolCategory, reasons: list[str],
                    warnings: list[str]) -> float | None:
        cfg = self.config

        if pool.price_observations:
            observations = pool.price_observations
            coverage_days = (
                observations[-1][0] - observations[0][0]
            ) / 86400.0 if len(observations) >= 2 else 0.0

            if len(observations) < 3:
                history_error = (
                    f"insufficient timestamped price history "
                    f"({len(observations)} < 3 observations)"
                )
            elif coverage_days < cfg.min_history_days:
                history_error = (
                    f"insufficient price-history coverage "
                    f"({coverage_days:.2f} < {cfg.min_history_days:.2f} days)"
                )
            else:
                try:
                    measured = annualized_volatility_from_observations(
                        observations,
                        max_gap_seconds=cfg.max_history_gap_hours * 3600.0,
                    )
                except ValueError as exc:
                    history_error = f"invalid timestamped price history: {exc}"
                else:
                    return (
                        max(measured, cfg.assumed_stable_vol)
                        if category == PoolCategory.STABLECOIN_STABLECOIN
                        else measured
                    )
        elif len(pool.price_history) >= cfg.min_history_points:
            # Backward compatibility for old snapshots and fixtures whose
            # histories have no timestamps and are documented as daily closes.
            measured = annualized_volatility(pool.price_history)
            warnings.append("volatility uses legacy un-timestamped daily price history")
            return max(measured, cfg.assumed_stable_vol) if category == PoolCategory.STABLECOIN_STABLECOIN else measured
        else:
            history_error = (
                f"insufficient price history "
                f"({len(pool.price_history)} < {cfg.min_history_points} daily points)"
            )

        if category == PoolCategory.STABLECOIN_STABLECOIN and not pool.price_observations and not pool.price_history:
            warnings.append(f"no price history; assuming {cfg.assumed_stable_vol:.0%} annual volatility")
            return cfg.assumed_stable_vol

        reasons.append(history_error)
        return None

    def evaluate(self, pool: PoolSnapshot, now: float | None = None) -> PoolEvaluation:
        cfg = self.config
        now = time.time() if now is None else now
        category = pool.category
        limits = cfg.category_limits[category]
        reasons: list[str] = []
        warnings: list[str] = []
        evaluation = PoolEvaluation(pool_id=pool.pool_id, category=category, eligible=False, chain=pool.chain,
                                    tokens=(pool.token0, pool.token1), tvl_usd=pool.tvl_usd,
                                    latest_price=pool.latest_price, price0_usd=pool.price0_usd, price1_usd=pool.price1_usd)

        age_hours = (now - pool.as_of) / 3600.0
        if age_hours > cfg.max_snapshot_age_hours:
            reasons.append(f"stale snapshot ({age_hours:.0f}h old, limit {cfg.max_snapshot_age_hours:.0f}h)")
        elif age_hours < -0.1:
            reasons.append("snapshot timestamp is in the future")
        if pool.tvl_usd < cfg.min_tvl_usd:
            reasons.append(f"TVL ${pool.tvl_usd:,.0f} below minimum ${cfg.min_tvl_usd:,.0f}")

        volume = pool.daily_volume_usd()
        evaluation.daily_volume_usd = volume
        if volume is None:
            if pool.reported_fee_apr is None:
                reasons.append("no volume data")
            else:
                warnings.append("volume unknown; relying on the source's fee APR")
        elif volume < cfg.min_daily_volume_usd:
            reasons.append(f"daily volume ${volume:,.0f} below minimum ${cfg.min_daily_volume_usd:,.0f}")

        fee_estimates = []
        if pool.fee_tier_bps is not None and volume is not None:
            fee_estimates.append(pool.fee_tier_bps / 10_000.0 * volume / pool.tvl_usd * 365.0)
        if pool.reported_fee_apr is not None:
            fee_estimates.append(pool.reported_fee_apr)
        if not fee_estimates:
            reasons.append("no fee data (need fee tier with volume, or a reported fee APR)")
        elif len(fee_estimates) == 2 and min(fee_estimates) > 0 and max(fee_estimates) / min(fee_estimates) > 3:
            warnings.append("modelled and reported fee APR differ by more than 3x; using the lower")
        fee_apr = min(fee_estimates) if fee_estimates else 0.0
        dex = pool.dex.lower()
        if fee_apr and cfg.concentrated_fee_haircut < 1 and ("v3" in dex or "slipstream" in dex):
            fee_apr *= cfg.concentrated_fee_haircut
            warnings.append(f"concentrated-liquidity pool: fee APR cut by {1 - cfg.concentrated_fee_haircut:.0%} for a full-range position")

        vol = self._volatility(pool, category, reasons, warnings)
        evaluation.volatility = vol
        il_apr = 0.0
        if vol is not None:
            il_apr = expected_v2_il(vol, cfg.horizon_days) * 365.0 / cfg.horizon_days

        depeg_component = 0.0
        history_prices = self._history_prices(pool)
        if category == PoolCategory.STABLECOIN_STABLECOIN and history_prices:
            depeg_component = min(1.0, max(abs(p - 1.0) for p in history_prices) / 0.03)
            if depeg_component >= 0.5:
                warnings.append("relative price drifted more than 1.5% from parity in the history window")
        haircut = depeg_component * cfg.depeg_max_haircut_apr

        vol_component = min(1.0, (vol or 1.5) / 1.5)
        tvl_component = 1.0 - min(1.0, max(0.0, math.log10(pool.tvl_usd / 1_000_000.0)) / 3.0)
        risk = 0.45 * vol_component + 0.25 * tvl_component + 0.20 * depeg_component
        if pool.audited is False:
            risk += 0.15
            warnings.append("contract not audited")
        elif pool.audited is None:
            risk += 0.05
            warnings.append("audit status unknown")
        if pool.age_days is not None and pool.age_days < 90:
            risk += 0.10
            warnings.append(f"young pool ({pool.age_days} days)")
        risk = min(1.0, risk)

        net_apr = fee_apr - il_apr - haircut
        coverage = fee_apr / il_apr if il_apr > 1e-9 else None
        evaluation.fee_apr, evaluation.il_apr, evaluation.depeg_haircut_apr = fee_apr, il_apr, haircut
        evaluation.net_apr, evaluation.coverage, evaluation.risk_score = net_apr, coverage, risk

        if vol is not None and fee_estimates:
            if risk > limits.max_risk:
                reasons.append(f"risk score {risk:.2f} above {category.value} limit {limits.max_risk:.2f}")
            if net_apr < limits.min_net_apr:
                reasons.append(f"net APR {net_apr:.1%} below {category.value} minimum {limits.min_net_apr:.1%}")
            if coverage is not None and coverage < limits.min_coverage:
                reasons.append(f"fees cover only {coverage:.1f}x expected IL (need {limits.min_coverage:.1f}x)")
        evaluation.reasons, evaluation.warnings = reasons, warnings
        evaluation.eligible = not reasons
        evaluation.score = net_apr / (0.1 + risk) if evaluation.eligible else 0.0
        return evaluation

    # ---- allocation -------------------------------------------------------

    def _allocate(self, candidates: list[PoolEvaluation], deployable: int) -> dict[str, int]:
        """Score-weighted allocation with pool, category and TVL-share caps (water-filling)."""
        cfg = self.config
        by_id = {e.pool_id: e for e in candidates}
        cap_pool = {i: min(cfg.max_pool_weight * deployable, cfg.max_pool_tvl_share * e.tvl_usd * MICRO)
                    for i, e in by_id.items()}
        cap_cat = {cat: limits.max_weight * deployable for cat, limits in cfg.category_limits.items()}
        alloc = {i: 0.0 for i in by_id}
        used: dict[PoolCategory, float] = defaultdict(float)
        active = {i for i, e in by_id.items() if e.score > 0 and cap_pool[i] >= 1}
        remaining = float(deployable)
        for _ in range(100):
            if remaining < 1 or not active:
                break
            total_score = sum(by_id[i].score for i in active)
            give = {i: min(remaining * by_id[i].score / total_score, cap_pool[i] - alloc[i]) for i in active}
            groups: dict[PoolCategory, list[str]] = defaultdict(list)
            for i in active:
                groups[by_id[i].category].append(i)
            for category, ids in groups.items():
                room, wanted = max(cap_cat[category] - used[category], 0.0), sum(give[i] for i in ids)
                if wanted > room:
                    factor = room / wanted if wanted > 0 else 0.0
                    for i in ids:
                        give[i] *= factor
            given = sum(give.values())
            if given < 1:
                break
            for i, amount in give.items():
                alloc[i] += amount
                used[by_id[i].category] += amount
            remaining -= given
            active = {i for i in active
                      if cap_pool[i] - alloc[i] >= 1 and cap_cat[by_id[i].category] - used[by_id[i].category] >= 1}
        return {i: int(v) for i, v in alloc.items() if int(v) > 0}

    def gas_drag_apr(self, chain: str, position_usd: float) -> float:
        gas = self.config.gas_usd.get(chain.lower(), self.config.default_gas_usd)
        return 2.0 * gas / position_usd * 365.0 / self.config.horizon_days   # one entry + one exit

    def select(self, capital_minor: int, now: float | None = None) -> SelectionResult:
        """Evaluate every registered pool and allocate ``capital_minor`` (micro-USDT)."""
        if isinstance(capital_minor, bool) or not isinstance(capital_minor, int) or capital_minor <= 0:
            raise PoolSelectionError("capital_minor must be a positive integer (micro-USDT)")
        if not self._pools:
            raise PoolSelectionError("no pools registered")
        cfg = self.config
        evaluations = [self.evaluate(pool, now) for pool in self.pools]
        excluded = {e.pool_id: list(e.reasons) for e in evaluations if not e.eligible}
        by_id = {e.pool_id: e for e in evaluations}
        candidates = [e for e in evaluations if e.eligible]
        deployable = int(capital_minor * (1.0 - cfg.reserve_fraction))
        allocations: dict[str, int] = {}
        gas_drag: dict[str, float] = {}
        while candidates:
            allocations = self._allocate(candidates, deployable)
            dropped = False
            for e in list(candidates):
                amount = allocations.get(e.pool_id)
                if not amount:
                    continue
                usd = amount / MICRO
                drag = self.gas_drag_apr(e.chain, usd)
                limits = cfg.category_limits[e.category]
                reason = None
                if usd < cfg.min_position_usd:
                    reason = f"position ${usd:,.0f} below minimum ${cfg.min_position_usd:,.0f}"
                elif e.net_apr - drag < limits.min_net_apr:
                    reason = f"gas on {e.chain} would cost {drag:.1%} APR at ${usd:,.0f}, leaving {e.net_apr - drag:.1%}"
                if reason:
                    excluded[e.pool_id] = [reason]
                    e.eligible, e.reasons, e.score = False, [reason], 0.0
                    candidates.remove(e)
                    dropped = True
                    break      # re-allocate without this pool, then re-check
                gas_drag[e.pool_id] = drag
            if not dropped:
                break
        if not candidates:
            allocations = {}
        details = {
            pool_id: {
                "category": by_id[pool_id].category.value, "chain": by_id[pool_id].chain,
                "tokens": list(by_id[pool_id].tokens), "net_apr": by_id[pool_id].net_apr, "entry_price": by_id[pool_id].latest_price, "price0_usd": by_id[pool_id].price0_usd,
                "price1_usd": by_id[pool_id].price1_usd,
                "net_apr_after_gas": by_id[pool_id].net_apr - gas_drag.get(pool_id, 0.0),
                "risk_score": by_id[pool_id].risk_score,
                "share_of_pool_tvl": amount / MICRO / by_id[pool_id].tvl_usd,
            } for pool_id, amount in allocations.items()
        }
        reserve = capital_minor - deployable
        allocation = CapitalAllocation(
            total_capital_minor=capital_minor, allocations=allocations, reserve_minor=reserve,
            unallocated_minor=deployable - sum(allocations.values()), details=details,
            rationale=self._rationale(by_id, allocations, excluded, capital_minor, reserve, gas_drag))
        return SelectionResult(evaluations=evaluations, excluded=excluded, allocation=allocation)

    @staticmethod
    def _rationale(by_id: dict[str, PoolEvaluation], allocations: dict[str, int], excluded: dict[str, list[str]],
                   total: int, reserve: int, gas_drag: dict[str, float]) -> str:
        lines = [f"Capital {total / MICRO:,.2f} USDT: reserve {reserve / MICRO:,.2f} USDT, "
                 f"allocated {sum(allocations.values()) / MICRO:,.2f} USDT across {len(allocations)} pool(s)."]
        for pool_id, amount in sorted(allocations.items(), key=lambda kv: -kv[1]):
            e = by_id[pool_id]
            lines.append(f"- {pool_id} [{e.category.value}, {e.chain}] {amount / MICRO:,.2f} USDT: "
                         f"fee APR {e.fee_apr:.1%}, IL cost {e.il_apr:.1%}, net {e.net_apr:.1%} "
                         f"({e.net_apr - gas_drag.get(pool_id, 0.0):.1%} after gas), risk {e.risk_score:.2f}")
            lines.extend(f"    warning: {w}" for w in e.warnings)
        for pool_id, reasons in sorted(excluded.items()):
            lines.append(f"- excluded {pool_id}: " + "; ".join(reasons))
        return "\n".join(lines)
