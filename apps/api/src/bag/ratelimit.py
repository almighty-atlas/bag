"""In-process sliding-window limiter for login failures.

State lives in the API process: enough to blunt online guessing for a
single-process self-hosted deployment. A reverse proxy can add address-level limits.
"""

import threading
import time
from collections import deque


class FailureLimiter:
    def __init__(self, max_failures: int, window_seconds: float) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._failures.setdefault(key, deque())
        while bucket and bucket[0] <= now - self.window:
            bucket.popleft()
        if not bucket:
            self._failures.pop(key, None)
        return bucket

    def retry_after(self, key: str, now: float | None = None) -> float | None:
        """Seconds until another attempt is allowed, or None when not limited."""
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._prune(key, now)
            if len(bucket) < self.max_failures:
                return None
            return max(1.0, bucket[0] + self.window - now)

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._prune(key, now)
            bucket.append(now)
            self._failures[key] = bucket
            if len(self._failures) > 10_000:
                # Bound memory under a flood of distinct keys; oldest buckets go first.
                for stale in list(self._failures)[:1000]:
                    self._failures.pop(stale, None)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
