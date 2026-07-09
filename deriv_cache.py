"""
Tiny in-memory TTL cache for expensive Deriv-derived display data (balance,
trade history). Single Render instance — same reasoning as rate_limit.py.

Only for read-heavy display endpoints where a few seconds of staleness is
an acceptable trade for not re-authenticating with Deriv (REST + OTP +
WebSocket) on every screen load. Never used for anything trade-execution
related.
"""
import time

_store: dict[str, tuple[float, object]] = {}


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
