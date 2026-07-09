"""
In-memory rate limiter — sufficient because this runs as a single Render
instance. If it ever runs multi-instance, this needs to move to a shared
store (Redis INCR + EXPIRE) since each process would otherwise count
attempts separately.
"""
import time
from collections import defaultdict
from fastapi import HTTPException


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str):
        """Raises 429 if `key` has hit the limit; otherwise records this attempt."""
        now = time.monotonic()
        hits = self._hits[key]
        hits[:] = [t for t in hits if now - t < self.window]
        if len(hits) >= self.max_attempts:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a few minutes and try again.",
            )
        hits.append(now)

    def reset(self, key: str):
        self._hits.pop(key, None)


login_limiter    = RateLimiter(max_attempts=8,  window_seconds=300)    # 8 tries / 5 min per username
register_limiter = RateLimiter(max_attempts=10, window_seconds=3600)   # 10 accounts / hour per IP
twofa_limiter    = RateLimiter(max_attempts=8,  window_seconds=300)    # 8 tries / 5 min per username
