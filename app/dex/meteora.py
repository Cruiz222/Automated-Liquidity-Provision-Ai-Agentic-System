"""Read-only pool discovery from Meteora's DLMM public API."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

from .http import ProviderError, fetch_json
from .pool_data import PoolSnapshot


POOLS_URL = "https://dlmm.datapi.meteora.ag/pools"


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

    def fetch_pools(self) -> list[PoolSnapshot]:
        now = self._clock()
        accepted: list[PoolSnapshot] = []
        seen: set[str] = set()

        report: dict[str, Any] = {
            "pages_requested": 0,
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
        }

        total_pages: int | None = None
        total_pools: int | None = None

        for page in range(1, self.universe.max_pages + 1):
            params = urlencode({
                "page": page,
                "page_size": self.universe.page_size,
            })
            payload = self._fetch(f"{POOLS_URL}?{params}")
            report["pages_requested"] += 1

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
                break

            if page >= pages:
                break

            if not rows:
                raise ProviderError(
                    "Meteora returned an empty page before the reported final page"
                )

        else:
            report["truncated_by_max_pages"] = (
                total_pages is not None
                and self.universe.max_pages < total_pages
            )

        report["pools_accepted"] = len(accepted)
        self.last_report = report

        return accepted
