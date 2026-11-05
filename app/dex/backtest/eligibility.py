"""Read-only historical market-data eligibility assessment."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from statistics import median
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from app.dex.snapshot_store import SnapshotStore
from .market_data import HistoricalMarketDataLoader


class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"


class IdentityConfidence(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EligibilityConfig:
    """Screening thresholds for an hourly historical-data window."""

    expected_points: int = 720
    interval_seconds: int = 3600
    eligible_coverage_pct: float = 98.0
    review_coverage_pct: float = 90.0
    eligible_max_gap_hours: float = 2.0
    review_max_gap_hours: float = 6.0
    eligible_max_age_minutes: float = 90.0
    review_max_age_minutes: float = 360.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.expected_points, bool)
            or not isinstance(self.expected_points, int)
            or self.expected_points < 2
        ):
            raise ValueError("expected_points must be an integer >= 2")

        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, int)
            or self.interval_seconds <= 0
        ):
            raise ValueError("interval_seconds must be a positive integer")

        for name in (
            "eligible_coverage_pct",
            "review_coverage_pct",
            "eligible_max_gap_hours",
            "review_max_gap_hours",
            "eligible_max_age_minutes",
            "review_max_age_minutes",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative finite number")

        if (
            self.eligible_coverage_pct > 100
            or self.review_coverage_pct > 100
        ):
            raise ValueError("coverage percentages cannot exceed 100")

        if self.review_coverage_pct > self.eligible_coverage_pct:
            raise ValueError(
                "review_coverage_pct cannot exceed eligible_coverage_pct"
            )

        if self.eligible_max_gap_hours > self.review_max_gap_hours:
            raise ValueError(
                "eligible_max_gap_hours cannot exceed review_max_gap_hours"
            )

        if self.eligible_max_age_minutes > self.review_max_age_minutes:
            raise ValueError(
                "eligible_max_age_minutes cannot exceed review_max_age_minutes"
            )


@dataclass(frozen=True, slots=True)
class PoolEligibility:
    pool_id: str
    chain: str
    token0: str
    token1: str
    status: EligibilityStatus
    observation_count: int
    expected_points: int
    count_coverage_pct: float
    coverage_hours: float | None
    median_interval_seconds: float | None
    largest_gap_hours: float | None
    estimated_missing_intervals: int
    latest_observation_age_minutes: float | None
    market_key: str | None
    identity_confidence: IdentityConfidence
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketGroup:
    market_key: str
    chain: str
    token_pair: str
    identity_confidence: IdentityConfidence
    pool_ids: tuple[str, ...]
    is_single_pool_group: bool


@dataclass(frozen=True, slots=True)
class EligibilityReport:
    config: EligibilityConfig
    pools: tuple[PoolEligibility, ...]
    market_groups: tuple[MarketGroup, ...]

    @property
    def eligible_count(self) -> int:
        return sum(p.status is EligibilityStatus.ELIGIBLE for p in self.pools)

    @property
    def needs_review_count(self) -> int:
        return sum(p.status is EligibilityStatus.NEEDS_REVIEW for p in self.pools)

    @property
    def rejected_count(self) -> int:
        return sum(p.status is EligibilityStatus.REJECTED for p in self.pools)

    @property
    def market_group_count(self) -> int:
        """Number of distinct token-pair groups identified in the report."""
        return len(self.market_groups)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": {
                name: getattr(self.config, name)
                for name in self.config.__dataclass_fields__
            },
            "summary": {
                "pool_count": len(self.pools),
                "eligible_count": self.eligible_count,
                "needs_review_count": self.needs_review_count,
                "rejected_count": self.rejected_count,
                "market_group_count": self.market_group_count,
            },
            "pools": [
                {
                    **{
                        name: getattr(pool, name)
                        for name in pool.__dataclass_fields__
                        if name not in {
                            "status",
                            "identity_confidence",
                            "reasons",
                        }
                    },
                    "status": pool.status.value,
                    "identity_confidence": pool.identity_confidence.value,
                    "reasons": list(pool.reasons),
                }
                for pool in self.pools
            ],
            "market_groups": [
                {
                    "market_key": group.market_key,
                    "chain": group.chain,
                    "token_pair": group.token_pair,
                    "identity_confidence": group.identity_confidence.value,
                    "pool_ids": list(group.pool_ids),
                    "is_single_pool_group": group.is_single_pool_group,
                }
                for group in self.market_groups
            ],
        }


def _market_identity(snapshot) -> tuple[str | None, IdentityConfidence]:
    """Build a chain-aware, order-independent identity for a token pair."""

    chain = snapshot.chain.strip().lower()
    addresses = snapshot.token_addresses

    if len(addresses) == 2 and all(
        isinstance(address, str) and address.strip()
        for address in addresses
    ):
        identity = (
            chain,
            "address",
            *sorted(address.strip().lower() for address in addresses),
        )
        confidence = IdentityConfidence.HIGH
    elif snapshot.token0.strip() and snapshot.token1.strip():
        identity = (
            chain,
            "symbol",
            *sorted((snapshot.token0.strip().upper(),
                     snapshot.token1.strip().upper())),
        )
        confidence = IdentityConfidence.LOW
    else:
        return None, IdentityConfidence.UNKNOWN

    digest = hashlib.sha256("|".join(identity).encode("utf-8")).hexdigest()
    return digest, confidence


def _assess_pool(snapshot, loader, config) -> PoolEligibility:
    reasons: list[str] = []
    status = EligibilityStatus.ELIGIBLE

    window_seconds = (config.expected_points - 1) * config.interval_seconds
    start_timestamp = snapshot.as_of - window_seconds

    market_key, identity_confidence = _market_identity(snapshot)

    if identity_confidence is IdentityConfidence.LOW:
        reasons.append(
            "Token contract addresses unavailable; market identity uses symbols"
        )
        status = EligibilityStatus.NEEDS_REVIEW
    elif identity_confidence is IdentityConfidence.UNKNOWN:
        reasons.append("Market identity could not be established")
        status = EligibilityStatus.NEEDS_REVIEW

    try:
        market_data = loader.load_pool(
            snapshot.pool_id,
            start_timestamp=max(0.0, start_timestamp),
            end_timestamp=snapshot.as_of,
        )
    except (TypeError, ValueError) as exc:
        return PoolEligibility(
            pool_id=snapshot.pool_id,
            chain=snapshot.chain,
            token0=snapshot.token0,
            token1=snapshot.token1,
            status=EligibilityStatus.REJECTED,
            observation_count=0,
            expected_points=config.expected_points,
            count_coverage_pct=0.0,
            coverage_hours=None,
            median_interval_seconds=None,
            largest_gap_hours=None,
            estimated_missing_intervals=0,
            latest_observation_age_minutes=None,
            market_key=market_key,
            identity_confidence=identity_confidence,
            reasons=tuple(reasons + [f"Historical data validation failed: {exc}"]),
        )

    points = market_data.observations
    timestamps = [point.timestamp for point in points]
    intervals = [
        right - left
        for left, right in zip(timestamps, timestamps[1:])
    ]

    count = len(points)
    coverage_pct = min(100.0, count / config.expected_points * 100.0)
    coverage_hours = (
        (timestamps[-1] - timestamps[0]) / 3600.0
        if len(timestamps) >= 2
        else 0.0
    )
    median_interval = median(intervals) if intervals else None
    largest_gap = max(intervals) / 3600.0 if intervals else None

    missing_intervals = sum(
        max(0, round(gap / config.interval_seconds) - 1)
        for gap in intervals
        if gap > config.interval_seconds * 1.5
    )

    latest_age = (snapshot.as_of - timestamps[-1]) / 60.0

    if coverage_pct < config.review_coverage_pct:
        status = EligibilityStatus.REJECTED
        reasons.append(
            f"Observation coverage {coverage_pct:.2f}% is below "
            f"{config.review_coverage_pct:.2f}%"
        )
    elif coverage_pct < config.eligible_coverage_pct:
        if status is not EligibilityStatus.REJECTED:
            status = EligibilityStatus.NEEDS_REVIEW
        reasons.append(
            f"Observation coverage {coverage_pct:.2f}% is below "
            f"{config.eligible_coverage_pct:.2f}%"
        )

    if largest_gap is not None:
        if largest_gap > config.review_max_gap_hours:
            status = EligibilityStatus.REJECTED
            reasons.append(
                f"Largest observation gap {largest_gap:.2f}h exceeds "
                f"{config.review_max_gap_hours:.2f}h"
            )
        elif largest_gap > config.eligible_max_gap_hours:
            if status is not EligibilityStatus.REJECTED:
                status = EligibilityStatus.NEEDS_REVIEW
            reasons.append(
                f"Largest observation gap {largest_gap:.2f}h exceeds "
                f"{config.eligible_max_gap_hours:.2f}h"
            )

    if latest_age < 0:
        if status is not EligibilityStatus.REJECTED:
            status = EligibilityStatus.NEEDS_REVIEW
        reasons.append(
            "Latest observation is later than the snapshot collection timestamp"
        )
    elif latest_age > config.review_max_age_minutes:
        status = EligibilityStatus.REJECTED
        reasons.append(
            f"Latest observation age {latest_age:.2f}m exceeds "
            f"{config.review_max_age_minutes:.2f}m"
        )
    elif latest_age > config.eligible_max_age_minutes:
        if status is not EligibilityStatus.REJECTED:
            status = EligibilityStatus.NEEDS_REVIEW
        reasons.append(
            f"Latest observation age {latest_age:.2f}m exceeds "
            f"{config.eligible_max_age_minutes:.2f}m"
        )

    if not reasons:
        reasons.append("Passed configured hourly history quality checks")

    return PoolEligibility(
        pool_id=snapshot.pool_id,
        chain=snapshot.chain,
        token0=snapshot.token0,
        token1=snapshot.token1,
        status=status,
        observation_count=count,
        expected_points=config.expected_points,
        count_coverage_pct=coverage_pct,
        coverage_hours=coverage_hours,
        median_interval_seconds=median_interval,
        largest_gap_hours=largest_gap,
        estimated_missing_intervals=missing_intervals,
        latest_observation_age_minutes=latest_age,
        market_key=market_key,
        identity_confidence=identity_confidence,
        reasons=tuple(reasons),
    )


def build_eligibility_report(
    store: SnapshotStore,
    config: EligibilityConfig | None = None,
) -> EligibilityReport:
    """Assess each latest pool snapshot without changing stored data."""

    config = config or EligibilityConfig()
    loader = HistoricalMarketDataLoader(store)
    latest_snapshots = store.latest_all()

    pools = tuple(
        _assess_pool(snapshot, loader, config)
        for snapshot in latest_snapshots
    )

    grouped: dict[str, list[PoolEligibility]] = {}
    for pool in pools:
        if pool.market_key is not None:
            grouped.setdefault(pool.market_key, []).append(pool)

    market_groups = tuple(
        MarketGroup(
            market_key=key,
            chain=members[0].chain,
            token_pair="/".join(
                sorted((members[0].token0.upper(), members[0].token1.upper()))
            ),
            identity_confidence=(
                IdentityConfidence.HIGH
                if all(
                    pool.identity_confidence is IdentityConfidence.HIGH
                    for pool in members
                )
                else IdentityConfidence.LOW
            ),
            pool_ids=tuple(sorted(pool.pool_id for pool in members)),
            is_single_pool_group=len(members) == 1,
        )
        for key, members in sorted(grouped.items())
    )

    return EligibilityReport(
        config=config,
        pools=pools,
        market_groups=market_groups,
    )


def build_eligibility_report_from_database(
    database_path: str | Path,
    config: EligibilityConfig | None = None,
) -> EligibilityReport:
    """Open a snapshot database read-only and build its eligibility report."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    uri = path.as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")

        # SnapshotStore.__init__ creates schema objects; do not call it here.
        store = object.__new__(SnapshotStore)
        store.db = db
        store.lock = threading.RLock()
        store.keep_per_pool = None

        return build_eligibility_report(store, config)
    finally:
        db.close()
