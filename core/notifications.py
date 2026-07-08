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

import database as db

logger = logging.getLogger("Notifications")

FCM_PROJECT_ID = "velau-87edd"
FCM_URL = f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send"

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
# Persisted in the DB (see database.save_fcm_token) so tokens survive process
# restarts/redeploys — an in-memory store would silently drop every push
# until the app was reopened to re-register.

def register_token(token: str, username: str = ""):
    if not token:
        return
    db.save_fcm_token(username or "_anonymous", token)
    logger.info(f"📱 FCM registered for {username or '_anonymous'}")


def unregister_token(token: str, username: str = ""):
    db.delete_fcm_token(token)


# ── Send ──────────────────────────────────────────────────────────────────────

def _send_to(tokens: list[str], title: str, body: str, data: dict = None):
    """Send a push notification to a specific set of device tokens."""
    if not tokens:
        return

    access_token = _get_access_token()
    if not access_token:
        return

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    for token in tokens:
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
                    db.delete_fcm_token(token)
                logger.warning(f"FCM error for token {token[:12]}…: {err.get('message')}")
        except Exception as e:
            logger.warning(f"FCM send error: {e}")


def _send(title: str, body: str, data: dict = None):
    """Broadcast to all registered users (global events like session start)."""
    _send_to(db.get_fcm_tokens(), title, body, data)


def notify_user(username: str, title: str, body: str, data: dict = None):
    """Send notification to a specific user's devices only."""
    _send_to(db.get_fcm_tokens(username), title, body, data)


# ── Public helpers ────────────────────────────────────────────────────────────

def notify_trade_executed(direction: str, symbol: str, amount: float, score: int):
    action = "CALL (Buy)" if direction == "CALL" else "PUT (Sell)"
    _send(
        title=f"Trade Placed {action}",
        body=f"{symbol}  |  Stake ${amount:.2f}  |  Confluence {score}/7",
        data={"type": "trade_executed", "direction": direction, "symbol": symbol},
    )

def notify_trade_settled(contract_id: str, won: bool, pnl: float):
    result = "Won" if won else "Lost"
    sign   = "+" if pnl >= 0 else "-"
    _send(
        title=f"Trade {result}  {sign}${abs(pnl):.2f}",
        body=f"Contract {contract_id} has been settled.",
        data={"type": "trade_settled", "won": str(won), "pnl": str(pnl)},
    )

def notify_circuit_breaker(consecutive_losses: int):
    _send(
        title="Circuit Breaker Triggered",
        body=f"{consecutive_losses} consecutive losses, bot paused for today. Resumes at midnight UTC.",
        data={"type": "circuit_breaker"},
    )

def notify_session_start():
    _send(
        title="Trading Session Open",
        body="London/NY session active. Bot is scanning XAU/USD.",
        data={"type": "session_start"},
    )
