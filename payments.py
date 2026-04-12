"""
payments.py — NOWPayments crypto payment integration.

Sign up at nowpayments.io to get your API key and IPN secret.
Set these env vars on Render:
  NOWPAYMENTS_API_KEY     — your API key
  NOWPAYMENTS_IPN_SECRET  — your IPN secret (for webhook signature verification)
  APP_BASE_URL            — e.g. https://velau.onrender.com
"""
import os
import time
import logging
import requests

logger = logging.getLogger("Payments")

NOWPAYMENTS_API_KEY    = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
APP_BASE_URL           = os.getenv("APP_BASE_URL", "https://velau.onrender.com")

BASE_URL = "https://api.nowpayments.io/v1"

# ── Pricing ───────────────────────────────────────────────────────────────────

PLANS: dict[str, dict] = {
    "monthly": {
        "usd":   149.90,
        "label": "Monthly",
        "days":  30,
    },
    "yearly": {
        "usd":   1699.00,
        "label": "Yearly",
        "days":  365,
    },
    "lifetime": {
        "usd":   7800.00,
        "label": "Lifetime",
        "days":  None,  # never expires
    },
}

# Default crypto: USDT on TRON (TRC-20) — low fees, widely accepted.
# NOWPayments currency code: "usdttrc20"
DEFAULT_CRYPTO = "usdttrc20"

# NOWPayments payment statuses that mean the payment is complete.
CONFIRMED_STATUSES = {"finished", "confirmed"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers() -> dict:
    if not NOWPAYMENTS_API_KEY:
        raise RuntimeError("NOWPAYMENTS_API_KEY is not configured")
    return {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}


# ── Public API ────────────────────────────────────────────────────────────────

def create_payment(plan: str, username: str) -> dict:
    """
    Create a NOWPayments payment invoice.

    Returns the full NOWPayments response dict which includes:
      payment_id, pay_address, pay_amount, pay_currency, price_amount, payment_status
    """
    if plan not in PLANS:
        raise ValueError(f"Unknown plan: {plan}")

    price_usd = PLANS[plan]["usd"]
    order_id  = f"{username}_{plan}_{int(time.time())}"

    payload = {
        "price_amount":      price_usd,
        "price_currency":    "usd",
        "pay_currency":      DEFAULT_CRYPTO,
        "order_id":          order_id,
        "order_description": f"Velau {PLANS[plan]['label']} Subscription",
        "ipn_callback_url":  f"{APP_BASE_URL}/subscription/webhook",
    }

    try:
        resp = requests.post(
            f"{BASE_URL}/payment",
            json=payload,
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        logger.error(f"NOWPayments create_payment HTTP error: {e.response.text}")
        raise
    except Exception as e:
        logger.error(f"NOWPayments create_payment error: {e}")
        raise


def get_payment_status(payment_id: str) -> dict:
    """
    Fetch the current status of a payment from NOWPayments.

    Relevant field: payment_status
      "waiting" | "confirming" | "confirmed" | "sending" |
      "partially_paid" | "finished" | "failed" | "refunded" | "expired"
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/payment/{payment_id}",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"NOWPayments get_payment_status error: {e}")
        raise


def is_confirmed(payment_status: str) -> bool:
    return payment_status in CONFIRMED_STATUSES
