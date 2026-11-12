"""Liquidity-pool snapshots: the data the pool selector evaluates.

A :class:`PoolSnapshot` is one point-in-time view of a DEX pool. Snapshots can
come from a JSON file (:func:`load_snapshots`) or from DefiLlama-style yield
rows (:func:`parse_defillama_pools`). Nothing here talks to the network.

Money-like fields are floats in USD because they feed analytics, not the
ledger. Ledger amounts remain integer micro-USDT.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

STABLE_SYMBOLS = frozenset({
    "USDT", "USDC", "DAI", "FDUSD", "PYUSD", "TUSD", "USDP", "FRAX", "LUSD", "GUSD", "USDE", "USDS", "CRVUSD",
})
TOKEN_GROUPS = {
    "ETH": frozenset({"ETH", "WETH", "STETH", "WSTETH", "CBETH", "RETH", "WEETH"}),
    "BTC": frozenset({"BTC", "WBTC", "CBBTC", "TBTC", "BTCB"}),
    "USD": STABLE_SYMBOLS,
}


class PoolCategory(Enum):
    STABLECOIN_STABLECOIN = "stablecoin-stablecoin"
    CORRELATED_VOLATILE = "correlated-volatile"
    UNCORRELATED_VOLATILE = "uncorrelated-volatile"


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def is_stable(symbol: str) -> bool:
    return normalize_symbol(symbol) in STABLE_SYMBOLS


def token_group(symbol: str) -> str | None:
    symbol = normalize_symbol(symbol)
    for group, members in TOKEN_GROUPS.items():
        if symbol in members:
            return group
    return None


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class PoolSnapshot:
    pool_id: str
    chain: str
    dex: str
    token0: str
    token1: str
    tvl_usd: float
    fee_tier_bps: float | None = None
    volume_usd_1d: float | None = None
    volume_usd_7d: float | None = None
    price_history: tuple[float, ...] = ()   # legacy price-only history; daily for older snapshots
    price_observations: tuple[tuple[float, float], ...] = ()
    # (unix timestamp, token0/token1 price), oldest first
    reported_fee_apr: float | None = None   # fraction (0.05 = 5%), from the data source
    correlation: float | None = None        # return correlation of the two tokens, if known
    audited: bool | None = None
    age_days: int | None = None
    price0_usd: float | None = None         # current USD price of token0 / token1 (None when unknown)
    price1_usd: float | None = None
    token_addresses: tuple[str, ...] = ()   # contract addresses of the two tokens, when the source gives them
    as_of: float = 0.0                      # unix seconds
    source: str = "unknown"
    price0_as_of: float | None = None
    price1_as_of: float | None = None
    history_interval_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.pool_id or not isinstance(self.pool_id, str):
            raise ValueError("pool_id is required")
        for name in ("chain", "dex", "token0", "token1"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not _finite(self.tvl_usd) or self.tvl_usd <= 0:
            raise ValueError("tvl_usd must be a positive number")
        if self.fee_tier_bps is not None and (not _finite(self.fee_tier_bps) or not 0 <= self.fee_tier_bps < 10_000):
            raise ValueError("fee_tier_bps must be in [0, 10000)")
        for name in ("volume_usd_1d", "volume_usd_7d", "reported_fee_apr"):
            value = getattr(self, name)
            if value is not None and (not _finite(value) or value < 0):
                raise ValueError(f"{name} must be a non-negative number")
        if self.correlation is not None and (not _finite(self.correlation) or not -1 <= self.correlation <= 1):
            raise ValueError("correlation must be in [-1, 1]")
        object.__setattr__(self, "price_history", tuple(self.price_history))

        observations = []
        for point in self.price_observations:
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise ValueError("each price_observations entry must be (timestamp, price)")
            observations.append((point[0], point[1]))

        object.__setattr__(self, "price_observations", tuple(observations))
        object.__setattr__(self, "token_addresses", tuple(self.token_addresses))
        for name in ("price0_usd", "price1_usd"):
            value = getattr(self, name)
            if value is not None and (not _finite(value) or value <= 0):
                raise ValueError(f"{name} must be a positive number")

        for name in ("price0_as_of", "price1_as_of"):
            value = getattr(self, name)
            if value is not None and (not _finite(value) or value < 0):
                raise ValueError(f"{name} must be a non-negative finite number")

        if self.history_interval_seconds is not None and (
            not _finite(self.history_interval_seconds)
            or self.history_interval_seconds <= 0
        ):
            raise ValueError("history_interval_seconds must be a positive finite number")
        if any(not _finite(p) or p <= 0 for p in self.price_history):
            raise ValueError("price_history must contain positive numbers")

        previous_timestamp = -1.0
        for timestamp, price in self.price_observations:
            if not _finite(timestamp) or timestamp < 0:
                raise ValueError("price observation timestamps must be non-negative numbers")
            if not _finite(price) or price <= 0:
                raise ValueError("price observation prices must be positive numbers")
            if timestamp <= previous_timestamp:
                raise ValueError("price observation timestamps must be strictly increasing")
            previous_timestamp = timestamp

        if not _finite(self.as_of) or self.as_of < 0:
            raise ValueError("as_of must be a non-negative unix timestamp")

    @property
    def symbols(self) -> tuple[str, str]:
        return normalize_symbol(self.token0), normalize_symbol(self.token1)

    @property
    def category(self) -> PoolCategory:
        a, b = self.symbols
        if is_stable(a) and is_stable(b):
            return PoolCategory.STABLECOIN_STABLECOIN
        group_a, group_b = token_group(a), token_group(b)
        if group_a is not None and group_a == group_b:
            return PoolCategory.CORRELATED_VOLATILE
        if self.correlation is not None and self.correlation >= 0.8:
            return PoolCategory.CORRELATED_VOLATILE
        return PoolCategory.UNCORRELATED_VOLATILE

    @property
    def latest_price(self) -> float | None:
        if self.price_observations:
            return self.price_observations[-1][1]
        return self.price_history[-1] if self.price_history else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["price_history"] = list(self.price_history)
        data["price_observations"] = [list(point) for point in self.price_observations]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PoolSnapshot":
        if not isinstance(data, dict):
            raise ValueError("snapshot must be an object")
        unknown = set(data) - set(_FIELDS)
        if unknown:
            raise ValueError(f"unknown snapshot fields: {sorted(unknown)}")
        return cls(**data)

    def daily_volume_usd(self) -> float | None:
        """Conservative daily volume: the lower of the latest day and the 7-day average."""
        candidates = []
        if self.volume_usd_1d is not None:
            candidates.append(self.volume_usd_1d)
        if self.volume_usd_7d is not None:
            candidates.append(self.volume_usd_7d / 7.0)
        return min(candidates) if candidates else None


def with_price_history(snapshot: PoolSnapshot, prices: Iterable[float]) -> PoolSnapshot:
    """Attach a price series (for sources that do not provide one, such as yield aggregators)."""
    return replace(snapshot, price_history=tuple(prices))


_FIELDS = ("pool_id", "chain", "dex", "token0", "token1", "tvl_usd", "fee_tier_bps", "volume_usd_1d",
           "volume_usd_7d", "price_history", "price_observations", "reported_fee_apr", "correlation", "audited", "age_days",
           "price0_usd", "price1_usd", "token_addresses", "as_of", "source",
           "price0_as_of", "price1_as_of", "history_interval_seconds")


def load_snapshots(path: str | Path) -> tuple[list[PoolSnapshot], dict[str, Any]]:
    """Load ``{"as_of":..., "source":..., "pools":[...]}``. Returns (snapshots, metadata)."""
    document = json.loads(Path(path).read_text())
    if not isinstance(document, dict) or not isinstance(document.get("pools"), list):
        raise ValueError("snapshot file must be an object with a 'pools' list")
    default_as_of = document.get("as_of", 0.0)
    default_source = document.get("source", "unknown")
    snapshots = []
    seen = set()
    for index, raw in enumerate(document["pools"]):
        if not isinstance(raw, dict):
            raise ValueError(f"pool #{index} must be an object")
        unknown = set(raw) - set(_FIELDS)
        if unknown:
            raise ValueError(f"pool #{index} has unknown fields: {sorted(unknown)}")
        data = {key: raw[key] for key in raw}
        data.setdefault("as_of", default_as_of)
        data.setdefault("source", default_source)
        snapshot = PoolSnapshot(**data)
        if snapshot.pool_id in seen:
            raise ValueError(f"duplicate pool_id: {snapshot.pool_id}")
        seen.add(snapshot.pool_id)
        snapshots.append(snapshot)
    return snapshots, {"as_of": default_as_of, "source": default_source}


_FEE_TIER = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse_defillama_pools(payload: dict[str, Any], as_of: float, min_tvl_usd: float = 0.0) -> list[PoolSnapshot]:
    """Convert a DefiLlama yields response (``{"data": [...]}``) into two-token pool snapshots.

    Only two-token pools are kept. Field names follow DefiLlama's public yields
    data (``pool``, ``chain``, ``project``, ``symbol``, ``tvlUsd``, ``apyBase``,
    ``volumeUsd1d``, ``volumeUsd7d``, ``poolMeta``); rows that do not parse are
    skipped. Verify against a live response before relying on it. The source
    gives no price history, so volatile pools need :func:`with_price_history`.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("payload must contain a 'data' list")
    snapshots = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pool_id = row.get("pool") or row.get("poolId")
        tokens = [t for t in re.split(r"[-/]", str(row.get("symbol", ""))) if t.strip()]
        tvl = row.get("tvlUsd")
        if not pool_id or len(tokens) != 2 or not _finite(tvl) or tvl <= 0 or tvl < min_tvl_usd:
            continue
        fee_match = _FEE_TIER.search(str(row.get("poolMeta") or ""))
        apy_candidates = [v for v in (row.get("apyBase"), row.get("apyBase7d")) if _finite(v) and v >= 0]
        apy_base = min(apy_candidates) if apy_candidates else None          # the lower of the 1-day and 7-day base APY
        addresses = row.get("underlyingTokens")
        addresses = tuple(a for a in addresses if isinstance(a, str)) if isinstance(addresses, list) else ()
        try:
            snapshots.append(PoolSnapshot(
                pool_id=str(pool_id), chain=str(row.get("chain", "")).lower() or "unknown",
                dex=str(row.get("project", "")) or "unknown", token0=tokens[0], token1=tokens[1],
                tvl_usd=float(tvl),
                fee_tier_bps=float(fee_match.group(1)) * 100 if fee_match else None,
                volume_usd_1d=row.get("volumeUsd1d") if _finite(row.get("volumeUsd1d")) else None,
                volume_usd_7d=row.get("volumeUsd7d") if _finite(row.get("volumeUsd7d")) else None,
                reported_fee_apr=apy_base / 100.0 if apy_base is not None else None,
                token_addresses=addresses if len(addresses) == 2 else (),
                as_of=as_of, source="defillama"))
        except ValueError:
            continue
    return snapshots
