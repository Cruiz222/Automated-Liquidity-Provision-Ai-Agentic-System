"""Live pool data from DefiLlama's public APIs (read-only, no key).

* ``https://yields.llama.fi/pools``: pool list with TVL, base (fee) APY, volumes, token addresses, fee tier.
* ``https://coins.llama.fi/prices/current/{coins}`` and ``/chart/{coins}``: USD prices and configurable daily/hourly price history,
  with coins addressed as ``chain:0xaddress``.

Response shapes follow DefiLlama's published API documentation. The parsers are tolerant (rows that do not fit are
skipped) but the provider is strict about trust: a pool without a recent, confident price for both tokens carries
no USD price and, if volatile, no price history, so the selector excludes it instead of guessing. Run
``python3 -m app.feedcheck`` on a machine with internet access to confirm the live responses still parse.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote

from .http import ProviderError, fetch_json
from .pool_data import PoolSnapshot, parse_defillama_pools

POOLS_URL = "https://yields.llama.fi/pools"
CURRENT_URL = "https://coins.llama.fi/prices/current/{coins}"
CHART_URL = "https://coins.llama.fi/chart/{coins}?span={span}&period={period}"

# DefiLlama chain names -> the prefix its coins API expects.
CHAIN_PREFIX = {"ethereum": "ethereum", "arbitrum": "arbitrum", "base": "base", "polygon": "polygon",
                "optimism": "optimism", "bsc": "bsc", "avalanche": "avax"}
DEFAULT_CHAINS = ("ethereum", "arbitrum", "base", "polygon", "optimism")
# Two-token AMM projects whose pools the full-range model approximates. v3-style pools are flagged by the selector.
DEFAULT_PROJECTS = ("uniswap-v2", "uniswap-v3", "sushiswap", "aerodrome-v1", "aerodrome-slipstream", "pancakeswap-amm")
DEFAULT_TOKENS = frozenset({"USDC", "USDT", "DAI", "WETH", "ETH", "WBTC", "CBBTC", "STETH", "WSTETH", "CBETH", "RETH"})
MIN_PRICE_CONFIDENCE = 0.8
BATCH = 30                                   # coins per current-price request
CHART_BATCH = 2                                # chart endpoint rejects larger tested batches
CHART_WINDOW_MAX = 240

@dataclass(frozen=True)
class PoolUniverse:
    """Which pools may be considered at all. Everything else is ignored before scoring."""
    chains: tuple[str, ...] = DEFAULT_CHAINS
    projects: tuple[str, ...] = DEFAULT_PROJECTS
    tokens: frozenset[str] = DEFAULT_TOKENS
    min_tvl_usd: float = 5_000_000.0
    max_pools: int = 40
    history_days: int = 30
    history_period: str = "1d"
    history_points: int | None = None
    history_alignment_tolerance_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.history_days < 1:
            raise ValueError("history_days must be at least 1")
        if self.history_period not in {"1d", "1h"}:
            raise ValueError("history_period must be '1d' or '1h'")
        if self.history_points is not None and self.history_points < 3:
            raise ValueError("history_points must be at least 3")
        if (
            self.history_alignment_tolerance_seconds is not None
            and (
                isinstance(self.history_alignment_tolerance_seconds, bool)
                or not isinstance(self.history_alignment_tolerance_seconds, (int, float))
                or not math.isfinite(self.history_alignment_tolerance_seconds)
                or self.history_alignment_tolerance_seconds <= 0
            )
        ):
            raise ValueError("history alignment tolerance must be positive and finite")

    @property
    def effective_history_span(self) -> int:
        if self.history_points is not None:
            return self.history_points
        return self.history_days * 24 if self.history_period == "1h" else self.history_days

    @property
    def effective_alignment_tolerance_seconds(self) -> float:
        if self.history_alignment_tolerance_seconds is not None:
            return self.history_alignment_tolerance_seconds
        return 12.0 * 3600.0 if self.history_period == "1d" else 15.0 * 60.0


def _symbols(row: dict[str, Any]) -> list[str]:
    return [t.strip().upper() for t in str(row.get("symbol", "")).replace("/", "-").split("-") if t.strip()]


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class DefiLlamaPoolProvider:
    def __init__(self, universe: PoolUniverse | None = None, fetch: Callable[[str], Any] = fetch_json,
                 clock: Callable[[], float] = time.time) -> None:
        self.universe, self._fetch, self._clock = universe or PoolUniverse(), fetch, clock
        self.last_report: dict[str, Any] = {}
        self._chart_report: dict[str, Any] = {}

    # ---- universe filter -------------------------------------------------------
    def _eligible_rows(self, rows: Iterable[Any]) -> list[dict[str, Any]]:
        u, kept = self.universe, []
        for row in rows:
            if not isinstance(row, dict) or row.get("outlier") or row.get("exposure") == "single":
                continue
            if str(row.get("chain", "")).lower() not in u.chains or row.get("project") not in u.projects:
                continue
            symbols = _symbols(row)
            if len(symbols) != 2 or not all(s in u.tokens for s in symbols):
                continue
            tvl = row.get("tvlUsd")
            if _finite(tvl) and tvl >= u.min_tvl_usd:
                kept.append(row)
        kept.sort(key=lambda r: -r["tvlUsd"])
        return kept[: u.max_pools]

    # ---- prices -------------------------------------------------------------------
    @staticmethod
    def _coin_ids(snapshot: PoolSnapshot) -> list[str] | None:
        prefix = CHAIN_PREFIX.get(snapshot.chain.lower())
        if prefix is None or len(snapshot.token_addresses) != 2:
            return None
        return [f"{prefix}:{address}" for address in snapshot.token_addresses]

    def _batches(self, ids: list[str]) -> list[str]:
        unique = sorted(set(ids))
        return [",".join(quote(i, safe=":,") for i in unique[k:k + BATCH]) for k in range(0, len(unique), BATCH)]

    @staticmethod
    def _chart_batches(ids: list[str]) -> list[str]:
        """Build small, deterministic batches for the historical chart API."""
        unique = sorted({coin.lower() for coin in ids})
        return [
            ",".join(quote(coin, safe=":,") for coin in unique[k:k + CHART_BATCH])
            for k in range(0, len(unique), CHART_BATCH)
        ]

    def _current_prices(self, ids: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for batch in self._batches(ids):
            try:
                coins = self._fetch(CURRENT_URL.format(coins=batch)).get("coins", {})
            except (ProviderError, AttributeError):
                continue                          # missing prices exclude pools later; never guessed
            for coin, record in coins.items():
                if isinstance(record, dict) and _finite(record.get("price")) and record["price"] > 0 \
                        and _finite(record.get("confidence", 1.0)) and record.get("confidence", 1.0) >= MIN_PRICE_CONFIDENCE:
                    prices[coin.lower()] = float(record["price"])
        return prices

    def _charts(self, ids: list[str]) -> dict[str, list[tuple[float, float]]]:
        """Fetch historical coin prices in bounded windows.

        Windows are requested backwards using the earliest returned timestamp.
        Overlapping observations are deduplicated by timestamp. Conflicting
        prices for the same coin/timestamp are rejected.
        """
        target_points = self.universe.effective_history_span
        period = self.universe.history_period

        charts: dict[str, list[tuple[float, float]]] = {}
        requested = sorted({coin.lower() for coin in ids})

        report = {
            "requests": 0,
            "failures": [],
            "requested_coins": len(requested),
            "batch_size": CHART_BATCH,
            "retrieved_coins": 0,
            "incomplete_coins": {},
        }

        for batch in self._chart_batches(ids):
            batch_ids = [
                coin.lower()
                for coin in unquote(batch).split(",")
            ]

            # Timestamp -> price, independently for every coin.
            merged: dict[str, dict[float, float]] = {
                coin: {} for coin in batch_ids
            }

            end: int | None = None
            previous_earliest: float | None = None

            while True:
                remaining = max(
                    target_points - len(series)
                    for series in merged.values()
                )

                if remaining <= 0:
                    break

                # Allow one extra point on subsequent requests because the API
                # may include the previous window's boundary observation.
                span = min(
                    CHART_WINDOW_MAX,
                    remaining if end is None else remaining + 1,
                )

                url = CHART_URL.format(
                    coins=batch,
                    span=span,
                    period=period,
                )

                if end is not None:
                    url += f"&end={end}"

                report["requests"] += 1

                try:
                    payload = self._fetch(url)
                except (ProviderError, AttributeError) as exc:
                    report["failures"].append(
                        f"Chart request failed: {type(exc).__name__}: {exc}"
                    )
                    break

                coins = payload.get("coins") if isinstance(payload, dict) else None

                if not isinstance(coins, dict):
                    report["failures"].append(
                        "Chart response did not contain a coins mapping"
                    )
                    break

                window_timestamps: list[float] = []

                for coin, record in coins.items():
                    coin_id = coin.lower()

                    if coin_id not in merged:
                        continue

                    points = record.get("prices") if isinstance(record, dict) else None

                    if not isinstance(points, list):
                        continue

                    for point in points:
                        if not isinstance(point, dict):
                            continue

                        timestamp = point.get("timestamp")
                        price = point.get("price")

                        if (
                            not _finite(timestamp)
                            or not _finite(price)
                            or timestamp < 0
                            or price <= 0
                        ):
                            continue

                        timestamp = float(timestamp)
                        price = float(price)

                        existing = merged[coin_id].get(timestamp)

                        if existing is not None and existing != price:
                            raise ProviderError(
                                "Conflicting historical prices for "
                                f"{coin_id} at timestamp {timestamp}: "
                                f"{existing} versus {price}"
                            )

                        merged[coin_id][timestamp] = price
                        window_timestamps.append(timestamp)

                if not window_timestamps:
                    report["failures"].append(
                        f"Chart response contained no usable observations "
                        f"for batch beginning {batch_ids[0]}"
                    )
                    break

                earliest = min(window_timestamps)

                if (
                    previous_earliest is not None
                    and earliest >= previous_earliest
                ):
                    report["failures"].append(
                        f"Historical pagination made no backward progress "
                        f"for batch beginning {batch_ids[0]}"
                    )
                    break

                previous_earliest = earliest

                # The next request should cover the period before this window.
                end = math.floor(earliest) - 1

            for coin_id, observations in merged.items():
                if observations:
                    series = sorted(observations.items())
                    charts[coin_id] = series[-target_points:]

        report["retrieved_coins"] = sum(
            len(charts.get(coin, [])) >= target_points
            for coin in requested
        )

        report["incomplete_coins"] = {
            coin: len(charts.get(coin, []))
            for coin in requested
            if len(charts.get(coin, [])) < target_points
        }

        self._chart_report = report
        return charts

    @staticmethod
    def _ratio_observations(
        first: list[tuple[float, float]],
        second: list[tuple[float, float]],
        tolerance: float = 12 * 3600,
    ) -> list[tuple[float, float]]:
        """Align token charts one-to-one; never reuse a token1 observation."""
        out: list[tuple[float, float]] = []
        if not first or not second:
            return out

        j = 0
        for ts, price in first:
            if j >= len(second):
                break
            while (
                j + 1 < len(second)
                and abs(second[j + 1][0] - ts) < abs(second[j][0] - ts)
            ):
                j += 1
            if abs(second[j][0] - ts) <= tolerance:
                out.append((ts, price / second[j][1]))
                j += 1
        return out

    @staticmethod
    def _ratio_series(
        first: list[tuple[float, float]],
        second: list[tuple[float, float]],
        tolerance: float = 12 * 3600,
    ) -> list[float]:
        """Legacy price-only view of the timestamp-aligned token ratio series."""
        return [
            price
            for _, price in DefiLlamaPoolProvider._ratio_observations(first, second, tolerance)
        ]

    # ---- main entry ------------------------------------------------------------------
    def fetch_pools(self) -> list[PoolSnapshot]:
        payload = self._fetch(POOLS_URL)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("pool list response has no 'data' list")
        chosen = self._eligible_rows(rows)
        snapshots = parse_defillama_pools({"data": chosen}, as_of=self._clock(), min_tvl_usd=self.universe.min_tvl_usd)
        if not snapshots:
            raise ProviderError("no pool passed the universe filter; check the chain, project and token settings")
        ids = [i for s in snapshots if (pair := self._coin_ids(s)) for i in pair]
        prices, charts = self._current_prices(ids), self._charts(ids)
        out, priced, with_history = [], 0, 0
        for snapshot in snapshots:
            pair = self._coin_ids(snapshot)
            p0, p1 = (prices.get(pair[0].lower()), prices.get(pair[1].lower())) if pair else (None, None)
            observations: list[tuple[float, float]] = []
            if pair and pair[0].lower() in charts and pair[1].lower() in charts:
                observations = self._ratio_observations(
                    charts[pair[0].lower()],
                    charts[pair[1].lower()],
                    tolerance=self.universe.effective_alignment_tolerance_seconds,
                )
            history = tuple(price for _, price in observations)
            priced += bool(p0 and p1)
            with_history += len(history) >= 14
            out.append(replace(snapshot, price0_usd=p0, price1_usd=p1, price_history=history,
                               price_observations=tuple(observations),
                               source="defillama (yields.llama.fi, coins.llama.fi)"))
        seen = Counter(r.get("project") for r in rows if isinstance(r, dict) and str(r.get("chain", "")).lower() in self.universe.chains)
        history_counts = [len(pool.price_observations) for pool in out if pool.price_observations]
        history_coverage_hours = [
            (pool.price_observations[-1][0] - pool.price_observations[0][0]) / 3600.0
            for pool in out if len(pool.price_observations) >= 2
        ]
        history_intervals = [
            right[0] - left[0]
            for pool in out
            for left, right in zip(pool.price_observations, pool.price_observations[1:])
        ]
        self.last_report = {
            "rows": len(rows),
            "after_filter": len(chosen),
            "snapshots": len(out),
            "with_usd_prices": priced,
            "with_price_history": with_history,
            "history_period": self.universe.history_period,
            "history_span": self.universe.effective_history_span,
            "history_alignment_tolerance_seconds": self.universe.effective_alignment_tolerance_seconds,
            "history_observations_min": min(history_counts) if history_counts else 0,
            "history_observations_median": statistics.median(history_counts) if history_counts else 0,
            "history_observations_max": max(history_counts) if history_counts else 0,
            "history_coverage_hours_min": min(history_coverage_hours) if history_coverage_hours else 0.0,
            "history_coverage_hours_median": statistics.median(history_coverage_hours) if history_coverage_hours else 0.0,
            "history_interval_seconds_median": statistics.median(history_intervals) if history_intervals else None,
            "chart_retrieval": dict(self._chart_report),
            "projects_on_chosen_chains": dict(seen.most_common(15)),
        }
        return out
