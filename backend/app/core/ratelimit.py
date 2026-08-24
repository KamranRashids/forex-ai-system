"""A small in-process sliding-window rate limiter.

Phase 1 scope: auth endpoints. The limiter is deliberately simple and
dependency-free. It is per-process (correct for the single-container v1
deployment); a shared Redis backend can replace ``_hits`` storage later
without changing call sites.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


class SlidingWindowLimiter:
    """Fixed-memory sliding window counter keyed by arbitrary strings."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: float = 60.0) -> RateLimitResult:
        """Record a hit for ``key`` and report whether it is within the limit."""
        now = self._clock()
        with self._lock:
            window = self._hits[key]
            cutoff = now - window_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= limit:
                retry_after = max(1, math.ceil(window[0] + window_seconds - now))
                return RateLimitResult(allowed=False, retry_after_seconds=retry_after)
            window.append(now)
            return RateLimitResult(allowed=True, retry_after_seconds=0)

    def reset(self, key: str | None = None) -> None:
        """Clear one key or the whole table (test/ops convenience)."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
