"""In-memory fixed-window rate limiter.

Adequate for the single-instance M0 service (ADR-003: one process). If the service ever
scales out, this moves to a shared store — until then, in-memory is correct and simple.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max = max_events
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt for ``key``; return False once the window is saturated."""
        now = time.monotonic() if now is None else now
        bucket = self._events[key]
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True
