"""Pool data providers. A provider returns normalized :class:`PoolSnapshot` objects.

Only a simulation fixture ships here. A real provider must verify chain and pool
addresses, token identity and decimals, data integrity and the exact meaning of
the volume and APR windows before its output is trusted.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Protocol

from .pool_data import PoolSnapshot, load_snapshots


class PoolDataProvider(Protocol):
    def fetch_pools(self) -> list[PoolSnapshot]: ...


class FixturePoolProvider:
    """Serves the synthetic example file. With ``restamp`` the observation time is set to "now" on every
    fetch so simulation data never looks stale; the source label says so and the data is still synthetic."""

    def __init__(self, path: str | Path, restamp: bool = True, clock: Callable[[], float] = time.time) -> None:
        self._snapshots, self._meta = load_snapshots(path)
        self._restamp = restamp
        self._clock = clock

    def fetch_pools(self) -> list[PoolSnapshot]:
        if not self._restamp:
            return list(self._snapshots)
        now = self._clock()
        return [replace(s, as_of=now, source=f"{s.source} [timestamps refreshed for simulation]")
                for s in self._snapshots]
