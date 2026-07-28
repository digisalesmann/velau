"""
coingecko.py — resolves a crypto currency ticker to its official logo URL.

Uses CoinGecko's free public API (no key required, verified live). Only
called when an admin creates or updates a crypto payment method — a rare,
manual action — so this stays comfortably within CoinGecko's unauthenticated
rate limit; nothing in the regular user-facing checkout/review flow ever
calls this. Never raises: a lookup failure just means the payment method is
saved without a logo_url, and the mobile app falls back to a generic
colored ticker badge.
"""
import logging
import requests

logger = logging.getLogger("CoinGecko")

BASE_URL = "https://api.coingecko.com/api/v3"


def fetch_logo_url(symbol: str) -> str | None:
    """Looks up `symbol` (e.g. "USDT", "btc") and returns its logo image URL,
    or None if unrecognized or the lookup fails for any reason."""
    if not symbol or not symbol.strip():
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/coins/markets",
            params={"vs_currency": "usd", "symbols": symbol.strip().lower()},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        # Sorted by market cap by default — the first match is the
        # best-known coin for an ambiguous/shared ticker.
        return data[0].get("image")
    except Exception as e:
        logger.warning(f"CoinGecko lookup failed for '{symbol}': {e}")
        return None
