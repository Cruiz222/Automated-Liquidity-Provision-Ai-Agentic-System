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
    """Allow at most ``max_events`` per ``window_seconds`` for each key. Memory is bounded by ``max_keys``."""

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

    def hit(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            if key not in self._events and len(self._events) >= self.max_keys:
                for stale in [k for k in self._events if not self._trim(k, now)]:
                    self._events.pop(stale, None)
                if len(self._events) >= self.max_keys:     # still full: drop the oldest-active key
                    self._events.pop(min(self._events, key=lambda k: self._events[k][-1]), None)
            self._events.setdefault(key, deque()).append(now)

    def check_and_hit(self, key: str) -> None:
        retry = self.blocked_for(key)
        if retry > 0:
            raise RateLimited(retry)
        self.hit(key)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
