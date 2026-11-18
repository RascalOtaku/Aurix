# src/rate_limiter.py
"""Generic in-memory rate limiter — sliding window, keyed by IP."""

import threading
import time
from typing import Dict, List


class RateLimiter:
    """Sliding-window rate limiter.

    Usage:
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        if not limiter.check(ip):
            raise HTTPException(429, "Too many requests")
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._log: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = max(window_seconds * 2, 120)

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        with self._lock:
            self._maybe_cleanup(now)
            timestamps = self._log.get(key, [])
            cutoff = now - self.window
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self._log[key] = timestamps
                return False
            timestamps.append(now)
            self._log[key] = timestamps
            return True

    def blocked(self, key: str) -> bool:
        """True if `key` is at its limit, WITHOUT recording a request. Pair with `hit()` to count only failures
        (failed logins), so a legitimate user's successful requests never use up the budget."""
        now = time.monotonic()
        with self._lock:
            self._maybe_cleanup(now)
            cutoff = now - self.window
            timestamps = [t for t in self._log.get(key, []) if t > cutoff]
            self._log[key] = timestamps
            return len(timestamps) >= self.max_requests

    def hit(self, key: str) -> None:
        """Record one event for `key` (see `blocked`)."""
        now = time.monotonic()
        with self._lock:
            self._log.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._log.pop(key, None)

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically purge stale entries."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff = now - self.window
        stale = [k for k, v in self._log.items() if not v or v[-1] <= cutoff]
        for k in stale:
            del self._log[k]
