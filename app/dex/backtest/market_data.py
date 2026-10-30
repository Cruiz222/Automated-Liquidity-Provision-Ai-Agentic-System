"""Historical price data loading for DEX liquidity backtests."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.dex.snapshot_store import SnapshotStore


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One token0/token1 price observation at a Unix timestamp."""

    timestamp: float
    price: float

    def __post_init__(self) -> None:
        for name, value in (
            ("timestamp", self.timestamp),
            ("price", self.price),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be a finite number")

        if self.timestamp < 0:
            raise ValueError("timestamp cannot be negative")

        if self.price <= 0:
            raise ValueError("price must be positive")


@dataclass(frozen=True, slots=True)
class HistoricalMarketData:
    """Chronologically ordered historical prices for one pool."""

    pool_id: str
    token0: str
    token1: str
    observations: tuple[PricePoint, ...]

    def __post_init__(self) -> None:
        if not self.pool_id.strip():
            raise ValueError("pool_id is required")

        if not self.token0.strip() or not self.token1.strip():
            raise ValueError("token symbols are required")

        if not self.observations:
            raise ValueError("historical observations cannot be empty")

        previous_timestamp = -1.0

        for point in self.observations:
            if point.timestamp <= previous_timestamp:
                raise ValueError(
                    "observations must have strictly increasing timestamps"
                )

            previous_timestamp = point.timestamp


class HistoricalMarketDataLoader:
    """Build a clean price series from persisted pool snapshots.

    Snapshot collection timestamps (as_of) are not used as market
    observation timestamps. Each price observation retains its own
    original timestamp.
    """

    def __init__(self, store: SnapshotStore):
        self.store = store

    @staticmethod
    def _validate_bound(name: str, value: float | None) -> None:
        if value is None:
            return

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative finite timestamp")

    def load_pool(
        self,
        pool_id: str,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
    ) -> HistoricalMarketData:
        """Load and merge timestamped price observations for one pool.

        Time bounds are inclusive and apply to individual price observations,
        not snapshot collection timestamps.

        Overlapping observations are deduplicated. If the same timestamp
        contains conflicting prices, loading fails rather than silently
        choosing one.
        """

        if not isinstance(pool_id, str) or not pool_id.strip():
            raise ValueError("pool_id is required")

        self._validate_bound("start_timestamp", start_timestamp)
        self._validate_bound("end_timestamp", end_timestamp)

        if (
            start_timestamp is not None
            and end_timestamp is not None
            and start_timestamp > end_timestamp
        ):
            raise ValueError(
                "start_timestamp cannot be later than end_timestamp"
            )

        snapshots = self.store.history_snapshots(pool_id)

        if not snapshots:
            raise ValueError(f"no snapshots found for pool: {pool_id}")

        first = snapshots[0]
        token0 = first.token0
        token1 = first.token1

        observations: dict[float, float] = {}

        for snapshot in snapshots:
            if (
                snapshot.token0.upper() != token0.upper()
                or snapshot.token1.upper() != token1.upper()
            ):
                raise ValueError(
                    f"inconsistent token orientation for pool: {pool_id}"
                )

            for timestamp, price in snapshot.price_observations:
                if (
                    start_timestamp is not None
                    and timestamp < start_timestamp
                ):
                    continue

                if (
                    end_timestamp is not None
                    and timestamp > end_timestamp
                ):
                    continue

                existing = observations.get(timestamp)

                if existing is not None:
                    if not math.isclose(
                        existing,
                        price,
                        rel_tol=1e-12,
                        abs_tol=0.0,
                    ):
                        raise ValueError(
                            f"conflicting prices at timestamp {timestamp} "
                            f"for pool {pool_id}: {existing} vs {price}"
                        )

                    continue

                observations[timestamp] = price

        if not observations:
            raise ValueError(
                f"no price observations found for pool {pool_id} "
                "within the requested time range"
            )

        points = tuple(
            PricePoint(timestamp=timestamp, price=price)
            for timestamp, price in sorted(observations.items())
        )

        return HistoricalMarketData(
            pool_id=pool_id,
            token0=token0,
            token1=token1,
            observations=points,
        )