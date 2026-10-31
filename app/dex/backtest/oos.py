"""Decision-time out-of-sample evaluation for DEX LP pool selection.

The selector uses only snapshots and price observations available by the
cutoff. Portfolio performance is then measured from the final training
observation through the unseen testing observations.

This is a research model, not a reconstruction of realized historical LP
returns. Fee APR and execution costs remain assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import math

from app.dex.pool_data import PoolSnapshot, is_stable
from app.dex.selector import (
    PoolSelector,
    PoolSelectionError,
    SelectorConfig,
    SelectionResult,
)
from app.dex.snapshot_store import SnapshotStore

from .accounting import (
    ExecutionCostConfig,
    LPAccountingModel,
    NetPortfolioResult,
)
from .market_data import (
    HistoricalMarketData,
    HistoricalMarketDataLoader,
    PricePoint,
)
from .portfolio import LPPortfolioResult, V2LPPortfolioModel
from .splitting import (
    ChronologicalMarketDataSplitter,
    DataSplitError,
)


MICRO_USDT = 1_000_000


class OOSBacktestError(ValueError):
    """Invalid inputs or unrecoverable OOS evaluation state."""


@dataclass(frozen=True, slots=True)
class OOSPoolResult:
    """Out-of-sample result for one selected pool."""

    pool_id: str
    allocation_micro_usdt: int
    quote_price_usd: float
    initial_capital_quote: Decimal
    anchor_timestamp: float
    first_test_timestamp: float
    last_test_timestamp: float
    anchor_gap_hours: float
    fee_apr_assumption: float
    gross_portfolio: LPPortfolioResult
    net_portfolio: NetPortfolioResult


@dataclass(frozen=True, slots=True)
class OOSBacktestResult:
    """Complete selector decision and out-of-sample evaluation."""

    cutoff_timestamp: float
    capital_micro_usdt: int
    selection: SelectionResult
    pool_results: dict[str, OOSPoolResult]
    exclusions: dict[str, list[str]]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allocated_micro_usdt(self) -> int:
        return self.selection.allocation.allocated_minor

    @property
    def reserve_micro_usdt(self) -> int:
        return self.selection.allocation.reserve_minor

    def summary(self) -> dict:
        """Return a JSON-friendly summary without copying full time series."""
        pools = {}

        for pool_id, result in self.pool_results.items():
            first = result.net_portfolio.observations[0]
            last = result.net_portfolio.observations[-1]

            pools[pool_id] = {
                "allocation_micro_usdt": result.allocation_micro_usdt,
                "allocation_usdt": (
                    result.allocation_micro_usdt / MICRO_USDT
                ),
                "quote_price_usd": result.quote_price_usd,
                "initial_capital_quote": str(
                    result.initial_capital_quote
                ),
                "anchor_timestamp": result.anchor_timestamp,
                "first_test_timestamp": result.first_test_timestamp,
                "last_test_timestamp": result.last_test_timestamp,
                "anchor_gap_hours": result.anchor_gap_hours,
                "fee_apr_assumption": result.fee_apr_assumption,
                "gross_final_value_quote": str(
                    result.gross_portfolio.observations[-1].lp_value_quote
                ),
                "net_final_value_quote": str(last.net_lp_value_quote),
                "net_pnl_quote": str(last.net_lp_pnl_quote),
                "net_pnl_vs_hold_quote": str(
                    last.net_pnl_vs_hold_quote
                ),
                "cumulative_fee_quote": str(
                    last.cumulative_fee_quote
                ),
                "cumulative_cost_quote": str(
                    last.cumulative_cost_quote
                ),
                "test_observations": (
                    len(result.net_portfolio.observations) - 1
                ),
                "warnings": list(result.net_portfolio.warnings),
            }

        return {
            "cutoff_timestamp": self.cutoff_timestamp,
            "capital_micro_usdt": self.capital_micro_usdt,
            "capital_usdt": self.capital_micro_usdt / MICRO_USDT,
            "allocated_micro_usdt": self.allocated_micro_usdt,
            "reserve_micro_usdt": self.reserve_micro_usdt,
            "unallocated_micro_usdt": (
                self.selection.allocation.unallocated_minor
            ),
            "selected_pool_count": len(self.pool_results),
            "pools": pools,
            "exclusions": self.exclusions,
            "warnings": list(self.warnings),
            "selector_rationale": self.selection.allocation.rationale,
        }


class DEXOutOfSampleEvaluator:
    """Replay a selector decision at a cutoff and evaluate unseen prices.

    Only stablecoin token1 quotes are supported in this first version.
    The token1 USD price must exist in the decision-time snapshot.

    ``costs`` are denominated in token1 quote units. Use zero costs for a
    baseline run, or provide explicit scenario assumptions.
    """

    def __init__(
        self,
        store: SnapshotStore,
        selector_config: SelectorConfig | None = None,
        costs: ExecutionCostConfig | None = None,
        max_anchor_gap_hours: float = 48.0,
    ) -> None:
        if not isinstance(store, SnapshotStore):
            raise OOSBacktestError("store must be a SnapshotStore")

        if (
            isinstance(max_anchor_gap_hours, bool)
            or not isinstance(max_anchor_gap_hours, (int, float))
            or not math.isfinite(max_anchor_gap_hours)
            or max_anchor_gap_hours <= 0
        ):
            raise OOSBacktestError(
                "max_anchor_gap_hours must be finite and positive"
            )

        if costs is not None and not isinstance(
            costs, ExecutionCostConfig
        ):
            raise OOSBacktestError(
                "costs must be ExecutionCostConfig or None"
            )

        self.store = store
        self.selector_config = selector_config or SelectorConfig()
        self.costs = costs or ExecutionCostConfig()
        self.max_anchor_gap_hours = float(max_anchor_gap_hours)

        self.market_loader = HistoricalMarketDataLoader(store)
        self.splitter = ChronologicalMarketDataSplitter()
        self.portfolio_model = V2LPPortfolioModel()
        self.accounting_model = LPAccountingModel()

    @staticmethod
    def _validate_inputs(
        cutoff_timestamp: float,
        capital_micro_usdt: int,
    ) -> None:
        if (
            isinstance(cutoff_timestamp, bool)
            or not isinstance(cutoff_timestamp, (int, float))
            or not math.isfinite(cutoff_timestamp)
            or cutoff_timestamp < 0
        ):
            raise OOSBacktestError(
                "cutoff_timestamp must be a non-negative finite timestamp"
            )

        if (
            isinstance(capital_micro_usdt, bool)
            or not isinstance(capital_micro_usdt, int)
            or capital_micro_usdt <= 0
        ):
            raise OOSBacktestError(
                "capital_micro_usdt must be a positive integer"
            )

    def run(
        self,
        cutoff_timestamp: float,
        capital_micro_usdt: int,
    ) -> OOSBacktestResult:
        """Run a decision-time selection and out-of-sample evaluation."""

        self._validate_inputs(cutoff_timestamp, capital_micro_usdt)
        cutoff = float(cutoff_timestamp)

        # Only snapshots available by the decision cutoff may influence
        # pool selection.
        decision_snapshots = self.store.latest_all_as_of(cutoff)

        if not decision_snapshots:
            raise OOSBacktestError(
                "no pool snapshots exist at or before the cutoff"
            )

        usable_snapshots: list[PoolSnapshot] = []
        snapshot_by_id: dict[str, PoolSnapshot] = {}
        split_by_id = {}
        exclusions: dict[str, list[str]] = {}

        for snapshot in decision_snapshots:
            pool_id = snapshot.pool_id

            if not is_stable(snapshot.token1):
                exclusions[pool_id] = [
                    "unsupported quote token: token1 must be a stablecoin"
                ]
                continue

            if snapshot.price1_usd is None:
                exclusions[pool_id] = [
                    "missing decision-time token1 USD price"
                ]
                continue

            try:
                market_data = self.market_loader.load_pool(pool_id)
                split = self.splitter.split(market_data, cutoff)
            except (ValueError, DataSplitError) as exc:
                exclusions[pool_id] = [
                    f"historical market data unavailable: {exc}"
                ]
                continue

            # The training anchor must be sufficiently close to the
            # decision cutoff. Validate this before selector allocation.
            anchor = split.training.observations[-1]
            anchor_gap_hours = (
                cutoff - anchor.timestamp
            ) / 3600.0

            if anchor_gap_hours > self.max_anchor_gap_hours:
                exclusions[pool_id] = [
                    "training price anchor is too old: "
                    f"{anchor_gap_hours:.2f} hours before cutoff "
                    f"(maximum {self.max_anchor_gap_hours:.2f})"
                ]
                continue

            # Rebuild selector history from timestamped training data.
            # Never use un-timestamped snapshot price_history here.
            training_prices = tuple(
                point.price for point in split.training.observations
            )

            if not training_prices:
                exclusions[pool_id] = [
                    "no training price observations at cutoff"
                ]
                continue

            decision_snapshot = PoolSnapshot(
                **{
                    **snapshot.to_dict(),
                    "price_history": training_prices,
                }
            )

            usable_snapshots.append(decision_snapshot)
            snapshot_by_id[pool_id] = decision_snapshot
            split_by_id[pool_id] = split

        if not usable_snapshots:
            raise OOSBacktestError(
                "no pools have sufficient decision-time data; "
                f"exclusions: {exclusions}"
            )

        selector = PoolSelector(self.selector_config)
        selector.add_pools(usable_snapshots)

        try:
            selection = selector.select(
                capital_micro_usdt,
                now=cutoff,
            )
        except PoolSelectionError as exc:
            raise OOSBacktestError(
                f"selector could not make a decision: {exc}"
            ) from exc

        pool_results: dict[str, OOSPoolResult] = {}
        warnings = [
            "This is a modelled OOS evaluation, not realized historical LP performance.",
            "Selector TVL, volume, fee APR and token USD prices come from the latest stored snapshot at or before the cutoff.",
            "Fee income uses the selector's decision-time gross fee APR as a constant assumption; it is not historical realized fee income.",
            "Execution costs are explicit scenario assumptions.",
            "Only stablecoin token1 quotes are supported; stablecoin USD parity is not assumed.",
        ]

        evaluations_by_id = {
            item.pool_id: item
            for item in selection.evaluations
        }

        for pool_id, allocation_micro_usdt in (
            selection.allocation.allocations.items()
        ):
            snapshot = snapshot_by_id[pool_id]
            split = split_by_id[pool_id]

            anchor = split.training.observations[-1]
            first_test = split.testing.observations[0]
            last_test = split.testing.observations[-1]

            anchor_gap_hours = (
                cutoff - anchor.timestamp
            ) / 3600.0

            # Selector capital is USD-equivalent micro-USDT.
            # Convert to token1 units using the decision-time USD price.
            allocated_usd = (
                Decimal(allocation_micro_usdt) / Decimal(MICRO_USDT)
            )
            quote_price_usd = Decimal(str(snapshot.price1_usd))

            if quote_price_usd <= 0:
                # PoolSnapshot validation should already prevent this.
                # Keep the guard to protect the conversion boundary.
                raise OOSBacktestError(
                    f"{pool_id}: decision-time token1 USD price "
                    "must be positive"
                )

            capital_quote = allocated_usd / quote_price_usd

            # Entry is the final training observation. All subsequent
            # observations are strictly after the cutoff.
            anchored_data = HistoricalMarketData(
                pool_id=pool_id,
                token0=split.training.token0,
                token1=split.training.token1,
                observations=(anchor, *split.testing.observations),
            )

            gross = self.portfolio_model.run(
                anchored_data,
                capital_quote,
            )

            evaluation = evaluations_by_id[pool_id]

            # Use gross fee APR. Do not pass selector net_apr here:
            # gross portfolio IL is already modelled by the LP position.
            net = self.accounting_model.run(
                gross,
                fee_apr=evaluation.fee_apr,
                costs=self.costs,
            )

            pool_results[pool_id] = OOSPoolResult(
                pool_id=pool_id,
                allocation_micro_usdt=allocation_micro_usdt,
                quote_price_usd=float(quote_price_usd),
                initial_capital_quote=capital_quote,
                anchor_timestamp=anchor.timestamp,
                first_test_timestamp=first_test.timestamp,
                last_test_timestamp=last_test.timestamp,
                anchor_gap_hours=anchor_gap_hours,
                fee_apr_assumption=evaluation.fee_apr,
                gross_portfolio=gross,
                net_portfolio=net,
            )

        if not pool_results:
            warnings.append(
                "The selector did not allocate capital to any pool "
                "that could be evaluated."
            )

        return OOSBacktestResult(
            cutoff_timestamp=cutoff,
            capital_micro_usdt=capital_micro_usdt,
            selection=selection,
            pool_results=pool_results,
            exclusions=exclusions,
            warnings=tuple(warnings),
        )
