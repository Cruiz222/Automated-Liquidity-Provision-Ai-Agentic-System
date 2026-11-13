"""Small in-memory sliding-window rate limiter (per process; resets on restart)."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__("too many attempts; try again later")
        self.retry_after = max(1, int(retry_after + 0.999))


class RateLimiter:
    """Apply sliding-window limits with bounded memory.

    Expired keys are reclaimed; when all key slots are occupied by active keys,
    new keys fail closed rather than evicting another key's security history.
    """

    def __init__(self, max_events: int, window_seconds: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10_000) -> None:
        if max_events < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("invalid rate limiter configuration")
        self.max_events, self.window, self.clock, self.max_keys = max_events, window_seconds, clock, max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            return deque()
        while events and now - events[0] >= self.window:
            events.popleft()
        if not events:
            self._events.pop(key, None)
        return events

    def blocked_for(self, key: str) -> float:
        """Seconds until ``key`` may act again (0 when it may act now)."""
        with self._lock:
            now = self.clock()
            events = self._trim(key, now)
            return self.window - (now - events[0]) if len(events) >= self.max_events else 0.0

    def _record_locked(self, key: str, now: float) -> None:
        if key not in self._events:
            # Iterate over a stable snapshot because _trim() removes expired keys.
            for existing_key in tuple(self._events):
                self._trim(existing_key, now)
            if len(self._events) >= self.max_keys:
                # Never evict an active key: flooding fresh keys must not bypass limits.
                earliest_expiry = min(events[0] + self.window for events in self._events.values())
                raise RateLimited(max(0.0, earliest_expiry - now))
            self._events[key] = deque((now,))
            return
        self._events[key].append(now)

    def hit(self, key: str) -> None:
        """Record an event without checking its per-key threshold.

        Raises RateLimited if a new key cannot be admitted without evicting an
        active key and weakening that key's protection.
        """
        with self._lock:
            self._record_locked(key, self.clock())

    def check_and_hit(self, key: str) -> None:
        """Atomically enforce the limit and reserve one attempt."""
        with self._lock:
            now = self.clock()
            events = self._trim(key, now)
            if len(events) >= self.max_events:
                raise RateLimited(self.window - (now - events[0]))
            self._record_locked(key, now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
