"""Read-only pool discovery from Meteora's DLMM public API."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import urlencode

from .http import ProviderError, fetch_json
from .pool_data import PoolSnapshot


POOLS_URL = "https://dlmm.datapi.meteora.ag/pools"
OHLCV_URL = f"{POOLS_URL}/{{address}}/ohlcv"
SUPPORTED_TIMEFRAMES = frozenset({"5m", "30m", "1h", "2h", "4h", "12h", "24h"})

# Meteora accepts approximately 100 candles per OHLCV request.
# Use 90 intervals to leave a safety margin with inclusive endpoints.
MAX_CANDLE_INTERVALS_PER_REQUEST = 90


TIMEFRAME_SECONDS = {
    "5m": 5 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "24h": 24 * 60 * 60,
}


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass(frozen=True)
class MeteoraUniverse:
    """Bounds the number and minimum quality of pools discovered."""

    min_tvl_usd: float = 100_000.0
    max_pools: int = 100
    page_size: int = 100
    max_pages: int = 10
    include_blacklisted: bool = False
    history_days: int = 30
    history_timeframe: str = "1h"

    def __post_init__(self) -> None:
        if (
            not _finite(self.min_tvl_usd)
            or self.min_tvl_usd <= 0
        ):
            raise ValueError("min_tvl_usd must be positive and finite")

        if (
            isinstance(self.max_pools, bool)
            or not isinstance(self.max_pools, int)
            or self.max_pools <= 0
        ):
            raise ValueError("max_pools must be a positive integer")

        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= 1000
        ):
            raise ValueError("page_size must be between 1 and 1000")

        if (
            isinstance(self.max_pages, bool)
            or not isinstance(self.max_pages, int)
            or self.max_pages <= 0
        ):
            raise ValueError("max_pages must be a positive integer")

        if not isinstance(self.include_blacklisted, bool):
            raise ValueError("include_blacklisted must be a boolean")
        if (
            isinstance(self.history_days, bool)
            or not isinstance(self.history_days, int)
            or self.history_days < 1
        ):
            raise ValueError("history_days must be a positive integer")

        if self.history_timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"history_timeframe must be one of {sorted(SUPPORTED_TIMEFRAMES)}"
            )


class MeteoraDLMMProvider:
    """Fetch and normalize Meteora DLMM pools without signing transactions."""

    def __init__(
        self,
        universe: MeteoraUniverse | None = None,
        fetch: Callable[[str], Any] = fetch_json,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.universe = universe or MeteoraUniverse()
        self._fetch = fetch
        self._clock = clock
        self.last_report: dict[str, Any] = {}

    def _snapshot(self, record: dict[str, Any], as_of: float) -> PoolSnapshot | None:
        address = record.get("address")
        token_x = record.get("token_x")
        token_y = record.get("token_y")
        tvl = record.get("tvl")

        if (
            not isinstance(address, str)
            or not address.strip()
            or not isinstance(token_x, dict)
            or not isinstance(token_y, dict)
            or not _finite(tvl)
            or tvl < self.universe.min_tvl_usd
            or tvl <= 0
        ):
            return None

        if not self.universe.include_blacklisted:
            if record.get("is_blacklisted") is not False:
                return None

        address_x = token_x.get("address")
        address_y = token_y.get("address")
        symbol_x = token_x.get("symbol")
        symbol_y = token_y.get("symbol")
        price_x = token_x.get("price")
        price_y = token_y.get("price")

        if not all(
            isinstance(value, str) and value.strip()
            for value in (address_x, address_y, symbol_x, symbol_y)
        ):
            return None

        if (
            not _finite(price_x)
            or price_x <= 0
            or not _finite(price_y)
            or price_y <= 0
        ):
            return None

        try:
            return PoolSnapshot(
                pool_id=address,
                chain="solana",
                dex="meteora-dlmm",
                token0=symbol_x,
                token1=symbol_y,
                tvl_usd=float(tvl),
                price0_usd=float(price_x),
                price1_usd=float(price_y),
                token_addresses=(address_x, address_y),
                as_of=as_of,
                source="meteora-dlmm-api",
            )
        except (TypeError, ValueError):
            return None

    def _fetch_price_history(
        self,
        pool_id: str,
        start_time: int,
        end_time: int,
        on_request: Callable[[], None] | None = None,
    ) -> tuple[tuple[float, float], ...]:
        """Fetch and validate chunked OHLCV close-price history.

        Requests overlap at their boundaries because Meteora includes
        both endpoints. Identical boundary observations are deduplicated.

        Candle timestamps identify candle starts. Stored observations
        use candle-close timestamps. The first request is aligned down
        to the candle boundary so the candle containing start_time is
        available for close-time filtering.
        """
        timeframe = self.universe.history_timeframe
        interval = TIMEFRAME_SECONDS[timeframe]
        max_chunk_seconds = (
            MAX_CANDLE_INTERVALS_PER_REQUEST * interval
        )

        # Keyed by candle-open timestamp to detect boundary duplicates.
        closes_by_timestamp: dict[int, float] = {}

        # Meteora may return the candle containing the requested start.
        # Align down so that candle is inside the first request.
        chunk_start = (start_time // interval) * interval

        while chunk_start < end_time:
            chunk_end = min(
                chunk_start + max_chunk_seconds,
                end_time,
            )

            query = urlencode({
                "timeframe": timeframe,
                "start_time": chunk_start,
                "end_time": chunk_end,
            })

            url = f"{OHLCV_URL.format(address=pool_id)}?{query}"

            if on_request is not None:
                on_request()

            try:
                payload = self._fetch(url)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(
                    f"Meteora OHLCV request failed for pool {pool_id}: {exc}"
                ) from exc

            if not isinstance(payload, dict):
                raise ProviderError(
                    f"Meteora OHLCV response for {pool_id} must be an object"
                )

            echoed_timeframe = payload.get("timeframe")
            if (
                echoed_timeframe is not None
                and echoed_timeframe != timeframe
            ):
                raise ProviderError(
                    f"Meteora returned unexpected timeframe for {pool_id}: "
                    f"{echoed_timeframe!r}"
                )

            candles = payload.get("data")
            if not isinstance(candles, list):
                raise ProviderError(
                    f"Meteora OHLCV data for {pool_id} must be a list"
                )

            previous_timestamp: float | None = None

            for index, candle in enumerate(candles):
                if not isinstance(candle, dict):
                    raise ProviderError(
                        f"Invalid OHLCV candle {index} for pool {pool_id}"
                    )

                timestamp = candle.get("timestamp")
                close = candle.get("close")

                if not _finite(timestamp) or timestamp < 0:
                    raise ProviderError(
                        f"Invalid OHLCV timestamp at index {index} "
                        f"for {pool_id}"
                    )

                if not _finite(close) or close <= 0:
                    raise ProviderError(
                        f"Invalid OHLCV close at index {index} "
                        f"for {pool_id}"
                    )

                timestamp = float(timestamp)
                close = float(close)

                if not timestamp.is_integer():
                    raise ProviderError(
                        f"OHLCV timestamp must be an integer Unix second "
                        f"for {pool_id}"
                    )

                if timestamp < chunk_start or timestamp > chunk_end:
                    raise ProviderError(
                        f"OHLCV timestamp outside requested chunk "
                        f"for {pool_id}"
                    )

                if (
                    previous_timestamp is not None
                    and timestamp <= previous_timestamp
                ):
                    raise ProviderError(
                        f"OHLCV timestamps are not strictly increasing "
                        f"for {pool_id}"
                    )

                previous_timestamp = timestamp

                # Include only completed candles whose close falls
                # inside the original requested history window.
                candle_close_timestamp = timestamp + interval

                if not start_time < candle_close_timestamp <= end_time:
                    continue

                existing_close = closes_by_timestamp.get(int(timestamp))

                if existing_close is not None:
                    if existing_close != close:
                        raise ProviderError(
                            f"Conflicting OHLCV close for duplicate "
                            f"timestamp {int(timestamp)} in pool {pool_id}"
                        )
                    continue

                closes_by_timestamp[int(timestamp)] = close

            if chunk_end == end_time:
                break

            # Deliberately overlap one boundary candle.
            chunk_start = chunk_end

        return tuple(
            (
                float(timestamp + interval),
                closes_by_timestamp[timestamp],
            )
            for timestamp in sorted(closes_by_timestamp)
        )

    def _enrich_history(
        self,
        pools: list[PoolSnapshot],
        now: float,
        report: dict[str, Any],
    ) -> list[PoolSnapshot]:
        """Attach historical OHLCV close observations to discovered pools."""
        end_time = int(now)
        start_time = end_time - self.universe.history_days * 86400

        history_report: dict[str, Any] = {
            "timeframe": self.universe.history_timeframe,
            "requested_days": self.universe.history_days,
            "requests": 0,
            "pools_with_history": 0,
            "pools_without_history": 0,
            "observations_total": 0,
            "observations_min": 0,
            "observations_median": 0,
            "observations_max": 0,
            "failures": [],
        }

        enriched: list[PoolSnapshot] = []
        observation_counts: list[int] = []

        def record_request() -> None:
            history_report["requests"] += 1

        for pool in pools:
            try:
                observations = self._fetch_price_history(
                    pool.pool_id,
                    start_time,
                    end_time,
                    on_request=record_request,
                )
            except ProviderError as exc:
                history_report["pools_without_history"] += 1
                history_report["failures"].append(
                    {"pool_id": pool.pool_id, "error": str(exc)}
                )
                enriched.append(pool)
                continue

            if not observations:
                history_report["pools_without_history"] += 1
                enriched.append(pool)
                continue

            closes = tuple(price for _, price in observations)

            enriched.append(
                replace(
                    pool,
                    price_history=closes,
                    price_observations=observations,
                )
            )

            history_report["pools_with_history"] += 1
            history_report["observations_total"] += len(observations)
            observation_counts.append(len(observations))

        if observation_counts:
            history_report["observations_min"] = min(observation_counts)
            history_report["observations_median"] = statistics.median(
                observation_counts
            )
            history_report["observations_max"] = max(observation_counts)

        report["history"] = history_report
        return enriched

    def fetch_pools(self) -> list[PoolSnapshot]:
        now = self._clock()
        accepted: list[PoolSnapshot] = []
        seen: set[str] = set()

        report: dict[str, Any] = {
            "pages_reported_by_api": None,
            "pages_requested": 0,
            "pages_processed": 0,
            "pages_not_retrieved": None,
            "pools_examined": 0,
            "pools_accepted": 0,
            "excluded": {
                "invalid_record": 0,
                "blacklisted": 0,
                "below_minimum_tvl": 0,
                "invalid_token_data": 0,
                "duplicate": 0,
            },
            "truncated_by_max_pages": False,
            "truncated_by_max_pools": False,
        }

        # Publish this run's report immediately. A failed run must not leave
        # a previous successful report looking current.
        self.last_report = report

        total_pages: int | None = None
        total_pools: int | None = None
        stop_reason: str | None = None

        for page in range(1, self.universe.max_pages + 1):
            params = urlencode({
                "page": page,
                "page_size": self.universe.page_size,
            })
            report["pages_requested"] += 1
            payload = self._fetch(f"{POOLS_URL}?{params}")

            if not isinstance(payload, dict):
                raise ProviderError("Meteora response must be an object")

            rows = payload.get("data")
            pages = payload.get("pages")
            current_page = payload.get("current_page")
            response_page_size = payload.get("page_size")
            total = payload.get("total")

            if (
                not isinstance(rows, list)
                or isinstance(pages, bool)
                or not isinstance(pages, int)
                or pages < 0
                or isinstance(current_page, bool)
                or not isinstance(current_page, int)
                or isinstance(response_page_size, bool)
                or not isinstance(response_page_size, int)
                or response_page_size <= 0
                or isinstance(total, bool)
                or not isinstance(total, int)
                or total < 0
            ):
                raise ProviderError("Meteora response has an invalid pagination structure")

            if response_page_size != self.universe.page_size:
                raise ProviderError(
                    "Meteora returned an unexpected page size: "
                    f"requested {self.universe.page_size}, received {response_page_size}"
                )

            if len(rows) > response_page_size:
                raise ProviderError(
                    "Meteora returned more records than the requested page size"
                )

            if current_page != page:
                raise ProviderError(
                    f"Meteora pagination did not progress: requested {page}, received {current_page}"
                )

            if total_pages is None:
                total_pages = pages
            elif pages != total_pages:
                raise ProviderError("Meteora pagination changed during discovery")

            if total_pools is None:
                total_pools = total
            elif total != total_pools:
                raise ProviderError("Meteora total pool count changed during discovery")

            if page > pages:
                raise ProviderError("Meteora returned a page beyond its reported page count")

            report["pages_reported_by_api"] = total_pages
            report["pages_processed"] += 1
            report["pages_not_retrieved"] = max(
                total_pages - report["pages_processed"],
                0,
            )
            report["pools_examined"] += len(rows)

            for record in rows:
                if not isinstance(record, dict):
                    report["excluded"]["invalid_record"] += 1
                    continue

                address = record.get("address")

                if isinstance(address, str) and address in seen:
                    report["excluded"]["duplicate"] += 1
                    continue

                if not self.universe.include_blacklisted:
                    if record.get("is_blacklisted") is not False:
                        report["excluded"]["blacklisted"] += 1
                        continue

                tvl = record.get("tvl")
                if not _finite(tvl) or tvl <= 0:
                    report["excluded"]["invalid_record"] += 1
                    continue

                if tvl < self.universe.min_tvl_usd:
                    report["excluded"]["below_minimum_tvl"] += 1
                    continue

                snapshot = self._snapshot(record, now)

                if snapshot is None:
                    report["excluded"]["invalid_token_data"] += 1
                    continue

                if isinstance(address, str):
                    seen.add(address)

                accepted.append(snapshot)

                if len(accepted) >= self.universe.max_pools:
                    break

            if len(accepted) >= self.universe.max_pools:
                stop_reason = "max_pools"
                break

            if page >= pages:
                stop_reason = "api_final_page"
                break

            if not rows:
                raise ProviderError(
                    "Meteora returned an empty page before the reported final page"
                )

        else:
            stop_reason = "max_pages"

        if total_pages is not None:
            report["pages_not_retrieved"] = max(
                total_pages - report["pages_processed"],
                0,
            )

        report["truncated_by_max_pages"] = (
            stop_reason == "max_pages"
            and report["pages_not_retrieved"] > 0
        )
        report["truncated_by_max_pools"] = (
            stop_reason == "max_pools"
            and report["pages_not_retrieved"] > 0
        )

        report["pools_accepted"] = len(accepted)
        accepted = self._enrich_history(accepted, now, report)

        return accepted
