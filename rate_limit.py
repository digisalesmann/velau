"""
In-memory rate limiter — sufficient because this runs as a single Render
instance. If it ever runs multi-instance, this needs to move to a shared
store (Redis INCR + EXPIRE) since each process would otherwise count
attempts separately.
"""
import threading
import time
from collections import defaultdict
from fastapi import HTTPException


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        # The auth routes run as sync handlers in FastAPI's thread pool, so
        # two attempts for the same key can arrive concurrently; without a
        # lock both could pass the limit check before either is recorded.
        self._lock = threading.Lock()

    def check(self, key: str):
        """Raises 429 if `key` has hit the limit; otherwise records this attempt."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            hits[:] = [t for t in hits if now - t < self.window]
            if len(hits) >= self.max_attempts:
                raise HTTPException(
                    status_code=429,
                    detail="Too many attempts. Please wait a few minutes and try again.",
                )
            hits.append(now)

    def reset(self, key: str):
        with self._lock:
            self._hits.pop(key, None)


login_limiter          = RateLimiter(max_attempts=8,  window_seconds=300)    # 8 tries / 5 min per username
register_limiter       = RateLimiter(max_attempts=10, window_seconds=3600)   # 10 accounts / hour per IP
twofa_limiter          = RateLimiter(max_attempts=8,  window_seconds=300)    # 8 tries / 5 min per username
forgot_password_limiter = RateLimiter(max_attempts=5, window_seconds=3600)   # 5 requests / hour per email
reset_password_limiter  = RateLimiter(max_attempts=5, window_seconds=900)    # 5 code guesses / 15 min per email
change_password_limiter = RateLimiter(max_attempts=8, window_seconds=300)    # 8 tries / 5 min per username — a
                                                                              # stolen/leaked JWT shouldn't be enough
                                                                              # to brute-force the real password
payment_proof_limiter  = RateLimiter(max_attempts=6, window_seconds=3600)    # 6 submissions / hour per username —
                                                                              # generous enough for a legitimate
                                                                              # resubmit-after-reject, not spam
