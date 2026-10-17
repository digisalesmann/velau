"""
Tiny in-memory TTL cache for expensive Deriv-derived display data (balance,
trade history). Single Render instance — same reasoning as rate_limit.py.

Only for read-heavy display endpoints where a few seconds of staleness is
an acceptable trade for not re-authenticating with Deriv (REST + OTP +
WebSocket) on every screen load. Never used for anything trade-execution
related.
"""
import asyncio
import time

_store: dict[str, tuple[float, object]] = {}
_locks: dict[str, asyncio.Lock] = {}
_failed_at: dict[str, float] = {}

# After a failed live fetch, requests for the same key within this window go
# straight to the stale fallback instead of each retrying Deriv in turn.
FAILURE_COOLDOWN = 15.0


def get(key: str, ttl: float):
    entry = _store.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    return None


def get_stale(key: str):
    """Returns the last known value regardless of age, or None if never set.
    Used as a fallback when a live Deriv fetch fails — showing minutes-old
    data is better than showing zeros."""
    entry = _store.get(key)
    return entry[1] if entry else None


def set(key: str, value):
    _store[key] = (time.monotonic(), value)
    _failed_at.pop(key, None)


def lock(key: str) -> asyncio.Lock:
    """Single-flight: hold this while fetching `key` so concurrent requests
    for the same data (e.g. the app opening and firing /dashboard twice)
    share one Deriv login instead of each doing REST + OTP + WebSocket."""
    return _locks.setdefault(key, asyncio.Lock())


def mark_failed(key: str):
    _failed_at[key] = time.monotonic()


def recently_failed(key: str) -> bool:
    t = _failed_at.get(key)
    return t is not None and time.monotonic() - t < FAILURE_COOLDOWN
