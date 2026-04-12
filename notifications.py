"""
notifications.py — Firebase Cloud Messaging push notifications (HTTP v1 API).

Uses a Firebase service account for OAuth2 — the legacy FCM API is disabled
for this project. Set FIREBASE_SERVICE_ACCOUNT_JSON on Render with the full
contents of the downloaded service account JSON file.
"""
import os
import json
import logging
import requests

logger = logging.getLogger("Notifications")

FCM_PROJECT_ID = "velau-87edd"
FCM_URL = f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send"

# In-memory token store. Tokens survive within a single process run.
# They are re-registered each time the Flutter app starts.
_device_tokens: set[str] = set()

# ── Credential helpers ────────────────────────────────────────────────────────

def _get_access_token() -> str | None:
    """Exchange service account credentials for a short-lived OAuth2 token."""
    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        logger.warning("FIREBASE_SERVICE_ACCOUNT_JSON not set — push notifications disabled")
        return None
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as g_requests

        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_json),
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )
        creds.refresh(g_requests.Request())
        return creds.token
    except Exception as e:
        logger.warning(f"FCM credential error: {e}")
        return None


# ── Token registration ────────────────────────────────────────────────────────

def register_token(token: str):
    if token:
        _device_tokens.add(token)
        logger.info(f"📱 FCM token registered ({len(_device_tokens)} total)")


def unregister_token(token: str):
    _device_tokens.discard(token)


# ── Send ──────────────────────────────────────────────────────────────────────

def _send(title: str, body: str, data: dict = None):
    """Send a push notification to all registered devices (one request each)."""
    if not _device_tokens:
        logger.debug("No registered devices, skipping push")
        return

    access_token = _get_access_token()
    if not access_token:
        return

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    stale = set()
    for token in list(_device_tokens):
        payload = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in (data or {}).items()},
                "android": {
                    "priority": "high",
                    "notification": {"channel_id": "trading_alerts", "sound": "default"},
                },
                "apns": {
                    "payload": {"aps": {"sound": "default"}},
                },
            }
        }
        try:
            resp = requests.post(FCM_URL, json=payload, headers=headers, timeout=8)
            result = resp.json()
            if resp.status_code == 200:
                logger.info(f"📲 Push sent: '{title}' → {token[:12]}…")
            else:
                err = result.get("error", {})
                if err.get("status") in ("UNREGISTERED", "INVALID_ARGUMENT"):
                    stale.add(token)
                logger.warning(f"FCM error for token {token[:12]}…: {err.get('message')}")
        except Exception as e:
            logger.warning(f"FCM send error: {e}")

    for t in stale:
        _device_tokens.discard(t)


# ── Public helpers ────────────────────────────────────────────────────────────

def notify_trade_executed(direction: str, symbol: str, amount: float, score: int):
    emoji  = "🟢" if direction == "CALL" else "🔴"
    action = "BUY (CALL)" if direction == "CALL" else "SELL (PUT)"
    _send(
        title=f"{emoji} Trade Executed {action}",
        body=f"{symbol} · ${amount:.2f} stake · Confluence {score}/7",
        data={"type": "trade_executed", "direction": direction, "symbol": symbol},
    )

def notify_trade_settled(contract_id: str, won: bool, pnl: float):
    emoji  = "✅" if won else "❌"
    result = "WON" if won else "LOST"
    _send(
        title=f"{emoji} Trade {result} · {'+' if pnl >= 0 else ''}{pnl:.2f} USD",
        body=f"Contract {contract_id} settled.",
        data={"type": "trade_settled", "won": str(won), "pnl": str(pnl)},
    )

def notify_circuit_breaker(consecutive_losses: int):
    _send(
        title="🚨 Circuit Breaker Triggered",
        body=f"{consecutive_losses} consecutive losses, bot paused for today. Resets at midnight UTC.",
        data={"type": "circuit_breaker"},
    )

def notify_session_start():
    _send(
        title="📈 Trading Session Open",
        body="London/NY session started, bot is now scanning XAU/USD.",
        data={"type": "session_start"},
    )
