"""Historical DEX pool snapshot collection."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from .pool_data import PoolSnapshot
from .provider import PoolDataProvider
from .snapshot_store import SnapshotStore


@dataclass(frozen=True)
class CollectionResult:
    started_at: float
    finished_at: float
    fetched: int
    inserted: int
    duplicates: int
    failed: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(
                self.finished_at - self.started_at, 3
            ),
            "fetched": self.fetched,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "errors": list(self.errors),
        }


class PoolSnapshotCollector:
    """Collect provider observations without executing LP transactions."""

    def __init__(
        self,
        provider: PoolDataProvider,
        store: SnapshotStore,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.store = store
        self.clock = clock
        self.sleep = sleep

    def _timestamp(self) -> float:
        value = self.clock()

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(
                "Collector clock must return a finite timestamp"
            )

        return float(value)

    def collect_once(self) -> CollectionResult:
        started_at = self._timestamp()

        try:
            snapshots = self.provider.fetch_pools()
        except Exception as exc:
            finished_at = self._timestamp()

            return CollectionResult(
                started_at=started_at,
                finished_at=finished_at,
                fetched=0,
                inserted=0,
                duplicates=0,
                failed=1,
                errors=(
                    f"Provider fetch failed: {type(exc).__name__}: {exc}",
                ),
            )

        if not isinstance(snapshots, list):
            finished_at = self._timestamp()

            return CollectionResult(
                started_at=started_at,
                finished_at=finished_at,
                fetched=0,
                inserted=0,
                duplicates=0,
                failed=1,
                errors=("Provider returned a non-list result",),
            )

        inserted = 0
        duplicates = 0
        failed = 0
        errors: list[str] = []

        for index, snapshot in enumerate(snapshots):
            if not isinstance(snapshot, PoolSnapshot):
                failed += 1
                errors.append(
                    f"Observation {index}: expected PoolSnapshot, "
                    f"received {type(snapshot).__name__}"
                )
                continue

            try:
                saved = self.store.save(snapshot)

                if saved:
                    inserted += 1
                else:
                    duplicates += 1

            except Exception as exc:
                failed += 1
                errors.append(
                    f"Pool {snapshot.pool_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        finished_at = self._timestamp()

        return CollectionResult(
            started_at=started_at,
            finished_at=finished_at,
            fetched=len(snapshots),
            inserted=inserted,
            duplicates=duplicates,
            failed=failed,
            errors=tuple(errors),
        )

    def collect_forever(
        self,
        interval_seconds: float,
        max_cycles: int | None = None,
        on_result: Callable[[CollectionResult], None] | None = None,
    ) -> None:
        """Collect repeatedly.

        max_cycles=None runs until interrupted.
        """
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError(
                "interval_seconds must be positive and finite"
            )

        if max_cycles is not None:
            if (
                isinstance(max_cycles, bool)
                or not isinstance(max_cycles, int)
                or max_cycles < 1
            ):
                raise ValueError(
                    "max_cycles must be a positive integer or None"
                )

        cycles = 0

        while max_cycles is None or cycles < max_cycles:
            result = self.collect_once()

            if on_result is not None:
                on_result(result)

            cycles += 1

            if max_cycles is None or cycles < max_cycles:
                self.sleep(interval_seconds)
