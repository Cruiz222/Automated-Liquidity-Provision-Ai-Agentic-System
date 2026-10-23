"""Validated, timestamped DEX pool data contracts and a local SQLite snapshot store.

This milestone intentionally ships no live provider. FixturePoolProvider makes
integration deterministic while a future adapter can implement PoolDataProvider.
Never treat fixture data as current market data.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Protocol


class PoolDataError(ValueError):
    """Pool data is malformed, stale, or otherwise unsafe to use."""


def _decimal(value: object, field: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PoolDataError(f"{field} must be a valid decimal") from None
    if not result.is_finite() or result < minimum:
        raise PoolDataError(f"{field} must be finite and at least {minimum}")
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PoolDataError("observed_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PoolDataError("observed_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PoolDataError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PoolSnapshot:
    pool_id: str
    chain: str
    dex: str
    token0: str
    token1: str
    tvl_usd: Decimal
    volume_24h_usd: Decimal
    fee_apr_pct: Decimal
    observed_at: datetime
    source: str
    category: str = "uncorrelated_volatile"
    volatility_pct: Decimal = Decimal("0")
    depeg_risk_pct: Decimal = Decimal("0")
    contract_risk_pct: Decimal = Decimal("0")
    correlation_risk_pct: Decimal = Decimal("50")
    gas_cost_usd: Decimal = Decimal("0")

    @classmethod
    def from_dict(cls, data: dict) -> "PoolSnapshot":
        if not isinstance(data, dict):
            raise PoolDataError("pool snapshot must be an object")
        required = ("pool_id", "chain", "dex", "token0", "token1", "tvl_usd",
                    "volume_24h_usd", "fee_apr_pct", "observed_at", "source")
        missing = [field for field in required if field not in data]
        if missing:
            raise PoolDataError("missing required fields: " + ", ".join(missing))
        values = dict(data)
        for field in ("pool_id", "chain", "dex", "token0", "token1", "source"):
            if not isinstance(values[field], str) or not values[field].strip():
                raise PoolDataError(f"{field} must be a non-empty string")
            values[field] = values[field].strip()
        if values["token0"].upper() == values["token1"].upper():
            raise PoolDataError("pool tokens must differ")
        numeric_fields = {
            "tvl_usd": Decimal("0.00000001"), "volume_24h_usd": Decimal("0"),
            "fee_apr_pct": Decimal("0"), "volatility_pct": Decimal("0"),
            "depeg_risk_pct": Decimal("0"), "contract_risk_pct": Decimal("0"),
            "correlation_risk_pct": Decimal("0"), "gas_cost_usd": Decimal("0"),
        }
        for field, minimum in numeric_fields.items():
            if field in values:
                values[field] = _decimal(values[field], field, minimum=minimum)
            else:
                values[field] = {"volatility_pct": Decimal("0"), "depeg_risk_pct": Decimal("0"),
                                 "contract_risk_pct": Decimal("0"), "correlation_risk_pct": Decimal("50"),
                                 "gas_cost_usd": Decimal("0")}[field]
        for field in ("depeg_risk_pct", "contract_risk_pct", "correlation_risk_pct"):
            if values[field] > 100:
                raise PoolDataError(f"{field} must be between 0 and 100")
        if values["fee_apr_pct"] > 100_000 or values["volatility_pct"] > 100_000:
            raise PoolDataError("APR or volatility exceeds supported bounds")
        category = values.get("category", "uncorrelated_volatile")
        if category not in {"stable_stable", "correlated_volatile", "uncorrelated_volatile"}:
            raise PoolDataError("unsupported pool category")
        values["category"] = category
        values["observed_at"] = _timestamp(values["observed_at"])
        return cls(**{field: values[field] for field in cls.__dataclass_fields__ if field in values})

    def validate_freshness(self, *, now: datetime | None = None, max_age_seconds: int = 300) -> None:
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 0:
            raise PoolDataError("max_age_seconds must be a non-negative integer")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise PoolDataError("now must include a timezone")
        age = (current.astimezone(timezone.utc) - self.observed_at).total_seconds()
        if age < -30:
            raise PoolDataError("pool snapshot timestamp is too far in the future")
        if age > max_age_seconds:
            raise PoolDataError(f"pool snapshot is stale ({int(age)} seconds old; max {max_age_seconds})")

    def to_advisor_candidate(self):
        """Convert to the M3 advisor input; freshness must be checked separately."""
        from .dex_pool_advisor import PoolCandidate
        return PoolCandidate(
            pool_id=self.pool_id, chain=self.chain, dex=self.dex, token0=self.token0,
            token1=self.token1, tvl_usd=self.tvl_usd, volume_24h_usd=self.volume_24h_usd,
            fee_apr_pct=self.fee_apr_pct, volatility_pct=self.volatility_pct,
            depeg_risk_pct=self.depeg_risk_pct, contract_risk_pct=self.contract_risk_pct,
            correlation_risk_pct=self.correlation_risk_pct, gas_cost_usd=self.gas_cost_usd,
            category=self.category,
        )

    def to_json_dict(self) -> dict:
        result = asdict(self)
        result["observed_at"] = self.observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        for key, value in result.items():
            if isinstance(value, Decimal):
                result[key] = str(value)
        return result


class PoolDataProvider(Protocol):
    def fetch_pools(self) -> list[PoolSnapshot]:
        """Return normalized snapshots. Implementations must preserve source timestamps."""


class FixturePoolProvider:
    """Deterministic provider for tests and local development only."""
    def __init__(self, pools: Iterable[PoolSnapshot]):
        self._pools = list(pools)

    def fetch_pools(self) -> list[PoolSnapshot]:
        return list(self._pools)


class PoolSnapshotStore:
    """Append-only local store for normalized snapshots; not a live data source."""
    def __init__(self, database: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(database))
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS dex_pool_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_id TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(pool_id, observed_at, source)
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_pool_snapshot_latest ON dex_pool_snapshots(pool_id, snapshot_id DESC)")
        self.db.commit()

    def save(self, snapshot: PoolSnapshot) -> bool:
        payload = json.dumps(snapshot.to_json_dict(), sort_keys=True, separators=(",", ":"))
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO dex_pool_snapshots(pool_id, observed_at, source, payload_json) VALUES (?, ?, ?, ?)",
            (snapshot.pool_id, snapshot.observed_at.isoformat(), snapshot.source, payload),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def latest(self, pool_id: str | None = None, limit: int = 100) -> list[PoolSnapshot]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise PoolDataError("limit must be an integer from 1 to 10000")
        if pool_id is None:
            rows = self.db.execute("SELECT payload_json FROM dex_pool_snapshots ORDER BY snapshot_id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self.db.execute("SELECT payload_json FROM dex_pool_snapshots WHERE pool_id = ? ORDER BY snapshot_id DESC LIMIT ?", (pool_id, limit)).fetchall()
        return [PoolSnapshot.from_dict(json.loads(row["payload_json"])) for row in rows]

    def close(self) -> None:
        self.db.close()


def screen_fresh_pools(provider: PoolDataProvider, *, now: datetime | None = None,
                       max_age_seconds: int = 300) -> dict:
    """Fetch, freshness-check, and score current snapshots with the M3 advisor.

    Stale/malformed candidates are reported separately and never passed to the
    advisor. Provider exceptions are allowed to propagate to the caller.
    """
    from .dex_pool_advisor import evaluate_pool
    accepted, rejected = [], []
    for snapshot in provider.fetch_pools():
        try:
            snapshot.validate_freshness(now=now, max_age_seconds=max_age_seconds)
            accepted.append(evaluate_pool(snapshot.to_advisor_candidate()))
        except (PoolDataError, ValueError) as exc:
            rejected.append({"pool_id": getattr(snapshot, "pool_id", None), "reason": str(exc)})
    accepted.sort(key=lambda item: (not item["eligible"], -item["metrics"]["score"], item["pool_id"]))
    return {"mode": "screening_only", "evaluated_count": len(accepted),
            "rejected_count": len(rejected), "pools": accepted, "rejected": rejected,
            "disclaimer": "Scoring is heuristic; no transaction was prepared or submitted."}
