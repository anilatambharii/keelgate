"""A per-key token bucket.

State is in this process. Behind several instances each enforces the limit separately, so the
effective limit is the limit times the instance count; put a shared limiter (Redis) here before
running more than one instance.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Final

from keelgate_cloud.errors import RateLimitedError

_MAX_TRACKED: Final = 100_000


class RateLimiter:
    """Allow ``per_minute`` requests a minute per key, with bursts up to the same number."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, key: str, per_minute: int) -> None:
        """Take one token or raise :class:`RateLimitedError`. ``0`` means unlimited-by-plan is
        not allowed: a plan with no allowance serves nobody.
        """
        now = self._clock()
        rate = per_minute / 60.0
        with self._lock:
            tokens, stamp = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - stamp) * rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                raise RateLimitedError("rate limit exceeded; retry shortly")
            self._buckets[key] = (tokens - 1.0, now)
            if len(self._buckets) > _MAX_TRACKED:
                self._buckets.pop(next(iter(self._buckets)))
