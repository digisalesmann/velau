"""
notifications.py — Firebase Cloud Messaging push notifications.

Sends push notifications to the user's device when:
  - A trade is auto-executed (BUY/SELL)
  - Circuit breaker trips
  - Contract settles (won/lost)

Requires FCM_SERVER_KEY env var set on Render.
Uses the legacy FCM HTTP API (v1 requires OAuth2 — legacy is simpler
for a single-app setup and still fully supported).
"""
import os
import logging
import requests

logger = logging.getLogger("Notifications")

FCM_URL     = "https://fcm.googleapis.com/fcm/send"
SERVER_KEY  = os.getenv("FCM_SERVER_KEY", "")

# In-memory store of device tokens registered by the app.
# In a multi-user production system this would be in the DB.
# For now, the app sends its token to /notifications/register
# and we keep it in memory (survives bot restarts within the same process).
_device_tokens: set[str] = set()


def register_token(token: str):
    """Called when the Flutter app sends its FCM token to the backend."""
    if token:
        _device_tokens.add(token)
        logger.info(f"📱 FCM token registered ({len(_device_tokens)} total)")


def unregister_token(token: str):
    _device_tokens.discard(token)


def _send(title: str, body: str, data: dict = None):
    """
    Send a push notification to all registered devices.
    Fails silently — a notification failure should never crash the bot.
    """
    if not SERVER_KEY:
        logger.warning("FCM_SERVER_KEY not set — push notifications disabled")
        return

    if not _device_tokens:
        logger.debug("No registered devices — skipping push")
        return

    payload = {
        "registration_ids": list(_device_tokens),
        "notification": {
            "title": title,
            "body":  body,
            "sound": "default",
        },
        "data":     data or {},
        "priority": "high",
        "android": {
            "priority": "high",
            "notification": {"channel_id": "trading_alerts"},
        },
    }

    try:
        resp = requests.post(
            FCM_URL,
            json=payload,
            headers={
                "Authorization": f"key={SERVER_KEY}",
                "Content-Type":  "application/json",
            },
            timeout=8,
        )
        result = resp.json()
        if result.get("failure", 0) > 0:
            # Remove tokens that are no longer valid
            for i, r in enumerate(result.get("results", [])):
                if r.get("error") in ("NotRegistered", "InvalidRegistration"):
                    tokens = list(_device_tokens)
                    if i < len(tokens):
                        _device_tokens.discard(tokens[i])
            logger.warning(f"FCM partial failure: {result}")
        else:
            logger.info(f"📲 Push sent: '{title}' → {len(_device_tokens)} device(s)")
    except Exception as e:
        logger.warning(f"FCM send error: {e}")


# ── Public notification helpers ────────────────────────────────────────────────

def notify_trade_executed(direction: str, symbol: str, amount: float, score: int):
    emoji  = "🟢" if direction == "CALL" else "🔴"
    action = "BUY (CALL)" if direction == "CALL" else "SELL (PUT)"
    _send(
        title=f"{emoji} Trade Executed — {action}",
        body=f"{symbol} · ${amount:.2f} stake · Confluence {score}/7",
        data={"type": "trade_executed", "direction": direction, "symbol": symbol},
    )

def notify_trade_settled(contract_id: str, won: bool, pnl: float):
    emoji = "✅" if won else "❌"
    result = "WON" if won else "LOST"
    _send(
        title=f"{emoji} Trade {result} · {'+' if pnl >= 0 else ''}{pnl:.2f} USD",
        body=f"Contract {contract_id} settled.",
        data={"type": "trade_settled", "won": str(won), "pnl": str(pnl)},
    )

def notify_circuit_breaker(consecutive_losses: int):
    _send(
        title="🚨 Circuit Breaker Triggered",
        body=f"{consecutive_losses} consecutive losses — bot paused for today. Resets at midnight UTC.",
        data={"type": "circuit_breaker"},
    )

def notify_session_start():
    _send(
        title="📈 Trading Session Open",
        body="London/NY session started — bot is now scanning XAU/USD.",
        data={"type": "session_start"},
    )