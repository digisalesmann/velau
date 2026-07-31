"""
fx_rates.py — live USD → local-currency conversion for bank-transfer payment
methods, so users are quoted the exact amount to send instead of a stale or
manually-entered figure. Crypto methods don't need this — those are quoted
directly in a USD-pegged stablecoin.

Uses open.er-api.com's free, no-key endpoint (rates update ~once/day
upstream). Cached in-memory across all currencies with a soft TTL so
/subscription/create stays fast and we don't hit the API on every checkout —
same single-process assumption as rate_limit.py. If a refresh fails, we keep
serving the last known-good rates as long as they're under _MAX_STALE; past
that we return None so the caller fails closed rather than silently quoting
a possibly very wrong amount.
"""
import logging
import time
import requests

logger = logging.getLogger("FXRates")

BASE_URL = "https://open.er-api.com/v6/latest/USD"
_REFRESH_TTL = 6 * 3600   # try for a fresh quote after this long
_MAX_STALE   = 24 * 3600  # refuse to serve a rate older than this

_cache: dict = {"rates": None, "fetched_at": 0.0}


def _refresh() -> dict | None:
    try:
        resp = requests.get(BASE_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("rates")
        if data.get("result") != "success" or not rates:
            raise ValueError(f"unexpected response body: {data}")
        _cache["rates"] = rates
        _cache["fetched_at"] = time.monotonic()
        return rates
    except Exception as e:
        logger.warning(f"FX rate refresh failed: {e}")
        return None


def get_usd_rate(currency_code: str) -> float | None:
    """How many units of `currency_code` equal 1 USD, or None if no
    sufficiently-fresh rate is available. Never raises — callers decide how
    to handle unavailability."""
    if not currency_code:
        return None
    code = currency_code.strip().upper()
    if code == "USD":
        return 1.0

    age = time.monotonic() - _cache["fetched_at"]
    if _cache["rates"] is None or age > _REFRESH_TTL:
        if _refresh() is None and _cache["rates"] is not None and age > _MAX_STALE:
            return None

    if _cache["rates"] is None:
        return None
    return _cache["rates"].get(code)


def convert_from_usd(usd_amount: float, currency_code: str) -> float | None:
    """Converts a USD amount to `currency_code` at the live rate, rounded to
    2dp, or None if no rate is available."""
    rate = get_usd_rate(currency_code)
    if rate is None:
        return None
    return round(usd_amount * rate, 2)
