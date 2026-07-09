import os
import json
import hmac
import base64
import hashlib
import logging
import asyncio
import traceback
from datetime import datetime, date
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger("Main")

from user_models import User, router as users_router, get_current_user
from news.news_pipeline import get_news_and_sentiment
from core.strategy_engine import XAUMasterStrategy
import database as db
from core import notifications as notif
import storage

trading_bot = XAUMasterStrategy()
bot_task: Optional[asyncio.Task] = None


async def _bot_runner(delay: int = 10):
    logger.info(f"⏳ Bot starts in {delay}s...")
    await asyncio.sleep(delay)
    if not db.get_global_bot_enabled():
        trading_bot.is_running = False
        logger.info("🤖 Bot startup skipped — globally paused (persisted state).")
        return
    logger.info("🤖 Bot loop starting now")
    try:
        await trading_bot.start_bot_loop()
    except asyncio.CancelledError:
        logger.info("Bot task cancelled cleanly")
    except Exception as e:
        logger.error(f"Bot loop fatal error: {e}")
        traceback.print_exc()


async def _db_keepalive():
    """Ping the database every 12 hours so Supabase never pauses."""
    while True:
        await asyncio.sleep(12 * 3600)
        try:
            db.fetchone("SELECT 1")
            logger.info("💓 DB keep-alive ping OK")
        except Exception as e:
            logger.warning(f"💓 DB keep-alive failed: {e}")


async def _server_keepalive():
    """
    Ping this server's own root endpoint every 10 minutes so Render's
    free tier never spins down due to inactivity.
    """
    import aiohttp
    url = os.getenv("APP_BASE_URL", "https://velau.onrender.com") + "/"
    await asyncio.sleep(60)          # let startup finish first
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    logger.debug(f"🏓 Self-ping {r.status}")
        except Exception as e:
            logger.debug(f"🏓 Self-ping failed: {e}")
        await asyncio.sleep(10 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_task
    logger.info("🚀 FastAPI startup — queueing bot")
    bot_task = asyncio.create_task(_bot_runner(delay=10))
    asyncio.create_task(_db_keepalive())
    asyncio.create_task(_server_keepalive())
    yield
    logger.info("🛑 FastAPI shutdown — stopping bot")
    trading_bot.is_running = False
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.include_router(users_router)


# ── Models ─────────────────────────────────────────────────────────────────────
class NewsResponse(BaseModel):
    articles: list
    sentiment: dict

class DashboardResponse(BaseModel):
    username:            str
    bot_status:          str
    balance:             float
    currency:            str = "USD"
    account_id:          Optional[str] = None
    win_rate:            float = 0.0
    trades_today:        int = 0
    total_trades:        int = 0
    daily_pnl:           float = 0.0
    daily_pnl_percent:   float = 0.0
    market_bias:         str = "Neutral"
    circuit_broken:      bool = False
    consecutive_losses:  int = 0
    trade_in_progress:   bool = False
    in_session:          bool = True
    deriv_connected:     bool = False
    display_name:        Optional[str] = None
    avatar_url:           Optional[str] = None
    global_bot_enabled:  bool = True
    user_bot_enabled:    bool = True
    is_admin:            bool = False

class DisplayNameRequest(BaseModel):
    display_name: str

class AvatarUploadRequest(BaseModel):
    image_base64: str
    content_type: str = "image/jpeg"

class TradeModeRequest(BaseModel):
    mode: str  # "demo" or "real"

class TickRequest(BaseModel):
    symbol: str = "frxXAUUSD"

class TradeRequest(BaseModel):
    contract_type: Optional[str] = None
    amount:        Optional[float] = None
    duration:      Optional[int] = None
    symbol:        str = "frxXAUUSD"
    action:        Optional[str] = "buy"

class FCMTokenRequest(BaseModel):
    token: str

class DerivConnectRequest(BaseModel):
    api_token: str

class SubscriptionCreateRequest(BaseModel):
    plan: str  # "monthly" | "yearly" | "lifetime"

class CandleRequest(BaseModel):
    symbol:      str = "frxXAUUSD"
    count:       int = 120
    granularity: int = 300


# ── Helpers ────────────────────────────────────────────────────────────────────
def _get_user_token(username: str) -> str:
    """
    Get Deriv token for this user.
    Falls back to the server-level DERIV_TOKEN env var for backwards
    compatibility (single-user mode / admin account).
    Raises 400 with 'no_deriv_account' when neither source has a token.
    """
    user_token = db.get_deriv_token(username)
    if user_token:
        return user_token
    from env_config import DERIV_TOKEN
    if DERIV_TOKEN:
        return DERIV_TOKEN
    raise HTTPException(status_code=400, detail="no_deriv_account")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    # HEAD support matters here: uptime monitors (UptimeRobot's default HTTP(s)
    # monitor, among others) send HEAD requests, not GET, to keep checks cheap.
    # A GET-only route 405s every HEAD check, making the monitor falsely
    # report "down" regardless of whether the server is actually healthy.
    return {
        "status":      "ok",
        "bot_running": trading_bot.is_running,
        "in_session":  trading_bot._in_trading_session(),
    }

@app.post("/notifications/register")
async def register_fcm(req: FCMTokenRequest, user=Depends(get_current_user)):
    notif.register_token(req.token, username=user.username)
    return {"status": "registered"}

@app.post("/notifications/unregister")
async def unregister_fcm(req: FCMTokenRequest, user=Depends(get_current_user)):
    notif.unregister_token(req.token, username=user.username)
    return {"status": "unregistered"}


# ── Deriv connection management ────────────────────────────────────────────────

@app.post("/deriv/connect")
async def connect_deriv(
    req: DerivConnectRequest,
    user=Depends(get_current_user)
):
    """
    Connect a user's personal Deriv API token.
    Validates the token by attempting authentication, then stores it.
    """
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService(token=req.api_token)
    try:
        await service.authenticate()
        info = await service.get_account_info()
        account_id = info.get("account_id", "")
        balance    = info.get("balance", 0.0)
        currency   = info.get("currency", "USD")

        # Validate it's an Options account (VRTC for demo, CR for real)
        if not account_id:
            raise HTTPException(
                status_code=400,
                detail="Could not read account ID. Check your token."
            )

        # Store token
        db.save_deriv_token(user.username, req.api_token, account_id)
        logger.info(
            f"{user.username} connected Deriv account "
            f"{account_id} (${balance} {currency})"
        )

        return {
            "status":     "connected",
            "account_id": account_id,
            "balance":    balance,
            "currency":   currency,
            "message":    f"Connected to {account_id}",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Token validation failed: {str(e)}"
        )
    finally:
        await service.close()


@app.post("/deriv/disconnect")
async def disconnect_deriv(user=Depends(get_current_user)):
    """Remove the user's stored Deriv token."""
    db.save_deriv_token(user.username, "", "")
    return {"status": "disconnected"}


@app.get("/deriv/status")
async def deriv_status(user=Depends(get_current_user)):
    """Check if user has a connected Deriv account."""
    token = db.get_deriv_token(user.username)
    if not token:
        return {"connected": False, "account_id": None, "balance": None}

    me = db.get_user(user.username) or {}
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService(token=token, account_type=me.get("trade_account_type") or "real")
    try:
        await service.authenticate()
        info = await service.get_account_info()
        return {
            "connected":  True,
            "account_id": info.get("account_id"),
            "balance":    info.get("balance"),
            "currency":   info.get("currency", "USD"),
            "trade_account_type": me.get("trade_account_type") or "real",
        }
    except Exception as e:
        return {
            "connected": False,
            "error":     str(e),
        }
    finally:
        await service.close()


# ── Bot control ────────────────────────────────────────────────────────────────

def _require_admin(user=Depends(get_current_user)):
    if not db.is_admin(user.username):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


@app.get("/bot/status")
async def get_bot_status(user=Depends(get_current_user)):
    me   = db.get_user(user.username) or {}
    risk = db.get_user_risk_state(user.username)
    return {
        "is_running":         trading_bot.is_running,
        "global_enabled":     db.get_global_bot_enabled(),
        "user_bot_enabled":   bool(me.get("bot_enabled", True)),
        "circuit_broken":     bool(risk["circuit_broken"]),
        "consecutive_losses": int(risk["consecutive_losses"]),
        "trade_in_progress":  trading_bot._trade_in_progress,
        "daily_pnl":          float(risk["daily_pnl"]),
        "in_session":         trading_bot._in_trading_session(),
        "trade_account_type": me.get("trade_account_type") or "real",
    }

@app.post("/bot/my-toggle")
async def toggle_my_bot(user=Depends(get_current_user)):
    me = db.get_user(user.username) or {}
    new_val = not bool(me.get("bot_enabled", True))
    db.set_user_bot_enabled(user.username, new_val)
    return {"user_bot_enabled": new_val}

@app.post("/account/trade-mode")
async def set_trade_mode(req: TradeModeRequest, user=Depends(get_current_user)):
    if req.mode not in ("demo", "real"):
        raise HTTPException(status_code=400, detail="mode must be 'demo' or 'real'.")
    db.set_trade_account_type(user.username, req.mode)
    return {"trade_account_type": req.mode}

@app.post("/bot/toggle")
async def toggle_bot(user=Depends(_require_admin)):
    global bot_task
    if trading_bot.is_running:
        trading_bot.is_running = False
        db.set_global_bot_enabled(False, updated_by=user.username)
        if bot_task:
            bot_task.cancel()
        return {"message": "Bot paused (platform-wide)", "is_running": False}
    else:
        db.set_global_bot_enabled(True, updated_by=user.username)
        trading_bot.is_running = True
        bot_task = asyncio.create_task(trading_bot.start_bot_loop())
        return {"message": "Bot started (platform-wide)", "is_running": True}


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(user=Depends(get_current_user)):
    """
    Profile/admin/bot-status data is DB-only and has nothing to do with
    Deriv connectivity — it must always be returned even if the broker
    connection is missing, disabled, or erroring. Only the Deriv-dependent
    fields (balance, trade history, win rate) degrade to defaults on
    failure instead of taking down the whole response. Previously a single
    Deriv auth failure (no token connected, or a disabled account) 500'd
    the entire endpoint, silently breaking profile display, admin-section
    visibility, and bot-status accuracy on screens that don't even touch
    Deriv data.
    """
    market_bias = db.get_latest_bias(username=user.username)
    deriv_token = db.get_deriv_token(user.username)
    profile     = db.get_user(user.username) or {}
    risk        = db.get_user_risk_state(user.username)
    is_admin    = db.is_admin(user.username)

    global_enabled   = db.get_global_bot_enabled()
    user_enabled     = bool(profile.get("bot_enabled", True))
    effective_active = trading_bot.is_running and global_enabled and user_enabled

    balance = 0.0
    currency = "USD"
    account_id = None
    win_rate = 0.0
    trades_today = 0
    total_trades = 0
    daily_pnl = 0.0
    pnl_percent = 0.0

    if deriv_token:
        from brokers.deriv_trading_service import DerivTradingService
        service = DerivTradingService(token=deriv_token)
        try:
            await service.authenticate()
            account_info = await service.get_account_info()
            history_data = await service.get_statement()
            trades_list  = history_data.get("history", [])

            balance    = account_info.get("balance", 0.0)
            currency   = account_info.get("currency", "USD")
            account_id = account_info.get("account_id")
            total_trades = len(trades_list)
            wins = len([t for t in trades_list if float(t.get("pnl", 0)) > 0])
            win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0.0

            today_trades = [
                t for t in trades_list
                if t.get("time") and
                datetime.fromtimestamp(int(t["time"])).date() == date.today()
            ]
            daily_pnl    = sum(float(t.get("pnl", 0)) for t in today_trades)
            pnl_percent  = (daily_pnl / balance * 100) if balance > 0 else 0.0
            trades_today = len(today_trades)
        except Exception as e:
            logger.warning(f"Dashboard: Deriv fetch failed for {user.username}: {e}")
        finally:
            await service.close()

    return DashboardResponse(
        username=user.username,
        bot_status="active" if effective_active else "paused",
        balance=balance,
        currency=currency,
        account_id=account_id,
        win_rate=win_rate,
        trades_today=trades_today,
        total_trades=total_trades,
        daily_pnl=round(daily_pnl, 2),
        daily_pnl_percent=round(pnl_percent, 2),
        market_bias=market_bias,
        circuit_broken=bool(risk["circuit_broken"]),
        consecutive_losses=int(risk["consecutive_losses"]),
        trade_in_progress=trading_bot._trade_in_progress,
        in_session=trading_bot._in_trading_session(),
        deriv_connected=bool(deriv_token),
        display_name=profile.get("display_name"),
        avatar_url=profile.get("avatar_url"),
        global_bot_enabled=global_enabled,
        user_bot_enabled=user_enabled,
        is_admin=is_admin,
    )


@app.post("/account/display-name")
async def set_display_name(req: DisplayNameRequest, user=Depends(get_current_user)):
    name = req.display_name.strip()
    if not name or len(name) > 40:
        raise HTTPException(
            status_code=400,
            detail="Display name must be between 1 and 40 characters.",
        )
    db.update_display_name(user.username, name)
    return {"display_name": name}


@app.post("/account/avatar")
async def upload_avatar(req: AvatarUploadRequest, user=Depends(get_current_user)):
    try:
        image_bytes = base64.b64decode(req.image_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image data.")

    if len(image_bytes) > 3 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 3MB.")

    try:
        avatar_url = storage.upload_avatar(user.username, image_bytes, req.content_type)
    except Exception as e:
        logger.warning(f"Avatar upload failed for {user.username}: {e}")
        raise HTTPException(status_code=502, detail="Avatar upload failed. Please try again.")

    db.update_avatar_url(user.username, avatar_url)
    return {"avatar_url": avatar_url}


@app.get("/open_contracts")
async def get_open_contracts(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    token   = _get_user_token(user.username)
    service = DerivTradingService(token=token)
    try:
        await service.authenticate()
        await service.ws.send({
            "proposal_open_contracts": 1,
            "subscribe": 0,
        })
        response = await service.ws.receive(timeout=20.0)
        if response.get("error"):
            return {"contracts": []}
        contracts = response.get("proposal_open_contracts", {})
        if not contracts:
            return {"contracts": []}
        open_list = []
        for cid, c in contracts.items():
            if c.get("is_expired") or c.get("is_settleable"):
                continue
            open_list.append({
                "contract_id":   cid,
                "symbol":        c.get("display_name", "Volatility 100 (1s)"),
                "contract_type": c.get("contract_type", ""),
                "buy_price":     c.get("buy_price", 0),
                "current_spot":  c.get("current_spot", 0),
                "profit":        c.get("profit", 0),
                "entry_spot":    c.get("entry_spot", 0),
            })
        return {"contracts": open_list}
    except Exception as e:
        return {"contracts": [], "error": str(e)}
    finally:
        await service.close()


@app.get("/news", response_model=NewsResponse)
async def get_news():
    try:
        articles, sentiment = get_news_and_sentiment()
        return NewsResponse(articles=articles, sentiment=sentiment)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"News error: {e}")

@app.get("/signals")
async def get_signals(user=Depends(get_current_user)):
    try:
        from datetime import timezone as tz
        now_utc  = datetime.now(tz.utc)
        hour_utc = now_utc.hour
        in_session = trading_bot._in_trading_session()

        # Minutes until next session opens (UTC 07:00)
        session_start_utc = 7
        if hour_utc < session_start_utc:
            mins_to_session = (session_start_utc - hour_utc) * 60 - now_utc.minute
        elif hour_utc >= 17:
            # After NY session — next is tomorrow London
            mins_to_session = (24 - hour_utc + session_start_utc) * 60 - now_utc.minute
        else:
            mins_to_session = 0  # in session

        return {
            "signals":         db.get_signals(limit=30, username=user.username),
            "in_session":      in_session,
            "session_hours":   "07:00-17:00 UTC (London + NY)",
            "mins_to_session": mins_to_session if not in_session else 0,
            "bot_running":     trading_bot.is_running,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard/history")
async def get_history(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    token   = _get_user_token(user.username)
    service = DerivTradingService(token=token)
    try:
        await service.authenticate()
        return await service.get_statement()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/ticks")
async def subscribe_ticks(req: TickRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    token   = _get_user_token(user.username)
    service = DerivTradingService(token=token)
    try:
        await service.authenticate()
        return await service.subscribe_ticks(symbol=req.symbol)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/candles")
async def get_candles(req: CandleRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    token   = _get_user_token(user.username)
    service = DerivTradingService(token=token)
    try:
        await service.authenticate()
        raw = await service.get_candles(
            symbol=req.symbol,
            count=min(req.count, 300),
            granularity=req.granularity,
        )
        return {"candles": raw}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/trade")
async def place_trade(req: TradeRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    if not req.contract_type or req.contract_type.upper() not in ("CALL", "PUT"):
        raise HTTPException(status_code=400, detail="Invalid contract_type.")
    if not req.amount or req.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive.")
    token   = _get_user_token(user.username)
    service = DerivTradingService(token=token)
    try:
        await service.authenticate()
        result = await service.place_order(
            contract_type=req.contract_type.upper(),
            amount=req.amount, duration=5, symbol=req.symbol,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Trade failed: {str(e)}")
    finally:
        await service.close()


# ── Subscription / payments ────────────────────────────────────────────────────

@app.get("/subscription/status")
async def get_subscription_status(user=Depends(get_current_user)):
    """Return the user's current subscription status."""
    admin = db.is_admin(user.username)
    if admin:
        return {"active": True, "plan": "admin", "is_admin": True}
    sub = db.get_active_subscription(user.username)
    if sub:
        return {
            "active":     True,
            "plan":       sub["plan"],
            "expires_at": sub.get("expires_at"),
            "is_admin":   False,
        }
    return {"active": False, "is_admin": False}


@app.get("/session")
async def get_session(user=Depends(get_current_user)):
    """
    Consolidated post-login info: 2FA status + subscription status in one
    round trip instead of two separate requests. The splash-screen biometric
    flow and the login screen both used to fire /2fa/status and
    /subscription/status as two calls (even when parallelized, that's still
    two full request/response round trips) — every extra request adds real
    latency on a cold Render start, where connection setup dominates.
    """
    tfa_data = db.get_totp_data(user.username)
    tfa_enabled = bool(tfa_data and tfa_data.get("totp_enabled"))

    admin = db.is_admin(user.username)
    if admin:
        subscription_active, plan = True, "admin"
    else:
        sub = db.get_active_subscription(user.username)
        subscription_active, plan = bool(sub), (sub["plan"] if sub else None)

    return {
        "tfa_enabled":         tfa_enabled,
        "subscription_active": subscription_active,
        "plan":                plan,
        "is_admin":            admin,
    }


@app.post("/subscription/create")
async def create_subscription(req: SubscriptionCreateRequest,
                              user=Depends(get_current_user)):
    """Create a crypto payment invoice for the requested plan."""
    from payments import create_payment, PLANS

    if req.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose monthly, yearly, or lifetime.")

    # Don't allow double-subscribing while still active
    existing = db.get_active_subscription(user.username)
    if existing:
        raise HTTPException(status_code=400, detail="You already have an active subscription.")

    try:
        payment = create_payment(req.plan, user.username)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Payment creation failed for {user.username}: {e}")
        raise HTTPException(status_code=500, detail="Could not create payment. Please try again.")

    db.create_pending_subscription(
        username=user.username,
        plan=req.plan,
        payment_id=str(payment["payment_id"]),
        pay_address=payment.get("pay_address", ""),
        pay_amount=float(payment.get("pay_amount", 0)),
        pay_currency=payment.get("pay_currency", ""),
        price_usd=float(payment.get("price_amount", 0)),
    )

    return {
        "payment_id":  str(payment["payment_id"]),
        "pay_address": payment["pay_address"],
        "pay_amount":  payment["pay_amount"],
        "pay_currency": payment["pay_currency"],
        "price_usd":   payment["price_amount"],
        "plan":        req.plan,
    }


@app.get("/subscription/poll/{payment_id}")
async def poll_payment(payment_id: str, user=Depends(get_current_user)):
    """
    Client polls this endpoint every ~15 s to detect payment confirmation.
    Returns {"status": "active"|"waiting"|"confirming"|...}.
    """
    from payments import get_payment_status, is_confirmed

    sub = db.get_subscription_by_payment(payment_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Payment not found.")

    if sub["status"] == "active":
        return {"status": "active", "plan": sub["plan"]}

    # Double-check with NOWPayments in case webhook was missed
    try:
        np = get_payment_status(payment_id)
        np_status = np.get("payment_status", "waiting")
        if is_confirmed(np_status):
            db.activate_subscription(payment_id, sub["plan"])
            logger.info(f"Subscription activated (poll) for {user.username} — plan {sub['plan']}")
            return {"status": "active", "plan": sub["plan"]}
        return {"status": np_status}
    except Exception as e:
        logger.warning(f"NOWPayments poll error: {e}")
        return {"status": sub["status"]}


class CancelPaymentRequest(BaseModel):
    payment_id: str

@app.post("/subscription/cancel")
async def cancel_subscription(req: CancelPaymentRequest, user=Depends(get_current_user)):
    """User-initiated cancel of a pending payment they no longer want to complete."""
    sub = db.get_subscription_by_payment(req.payment_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Payment not found.")
    if sub["username"] != user.username:
        raise HTTPException(status_code=403, detail="Not your payment.")
    if sub["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot cancel a {sub['status']} payment.")
    db.cancel_pending_subscription(req.payment_id, user.username)
    return {"ok": True}


@app.post("/subscription/webhook")
async def subscription_webhook(request: Request):
    """
    NOWPayments IPN webhook — called when a payment status changes.
    Verifies the HMAC-SHA512 signature if NOWPAYMENTS_IPN_SECRET is set.
    """
    import os
    body = await request.body()

    ipn_secret = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
    if ipn_secret:
        sig      = request.headers.get("x-nowpayments-sig", "")
        expected = hmac.new(ipn_secret.encode(), body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="Invalid IPN signature.")

    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON.")

    payment_id     = str(data.get("payment_id", ""))
    payment_status = data.get("payment_status", "")

    from payments import is_confirmed
    if is_confirmed(payment_status):
        sub = db.get_subscription_by_payment(payment_id)
        if sub and sub["status"] == "pending":
            db.activate_subscription(payment_id, sub["plan"])
            logger.info(f"Subscription activated (webhook) payment_id={payment_id}")

    return {"received": True}


# ── Admin endpoints ────────────────────────────────────────────────────────────

class AdminGrantRequest(BaseModel):
    username: str
    plan:     str  # monthly | yearly | lifetime

class AdminRevokeRequest(BaseModel):
    sub_id: int

class AdminSetAdminRequest(BaseModel):
    username: str
    value:    bool


@app.get("/admin/stats")
async def admin_stats(user=Depends(_require_admin)):
    return db.admin_get_stats()


@app.get("/admin/users")
async def admin_users(user=Depends(_require_admin)):
    return {"users": db.admin_get_users()}


@app.get("/admin/subscriptions")
async def admin_subscriptions(user=Depends(_require_admin)):
    return {"subscriptions": db.admin_get_subscriptions()}


@app.post("/admin/grant")
async def admin_grant(req: AdminGrantRequest, user=Depends(_require_admin)):
    from payments import PLANS
    if req.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan.")
    if not db.get_user(req.username):
        raise HTTPException(status_code=404, detail="User not found.")
    db.admin_grant_subscription(req.username, req.plan)
    logger.info(f"Admin {user.username} granted {req.plan} to {req.username}")
    return {"ok": True}


@app.post("/admin/revoke")
async def admin_revoke(req: AdminRevokeRequest, user=Depends(_require_admin)):
    db.admin_revoke_subscription(req.sub_id)
    logger.info(f"Admin {user.username} revoked subscription {req.sub_id}")
    return {"ok": True}


@app.post("/admin/set_admin")
async def admin_set_admin(req: AdminSetAdminRequest, user=Depends(_require_admin)):
    if not db.get_user(req.username):
        raise HTTPException(status_code=404, detail="User not found.")
    db.set_admin(req.username, req.value)
    return {"ok": True}


class AdminResetCircuitBreakerRequest(BaseModel):
    username: str


@app.post("/admin/reset_circuit_breaker")
async def admin_reset_circuit_breaker(
    req: AdminResetCircuitBreakerRequest, user=Depends(_require_admin)
):
    if not db.get_user(req.username):
        raise HTTPException(status_code=404, detail="User not found.")
    db.reset_user_circuit_breaker(req.username)
    logger.info(f"Admin {user.username} manually reset circuit breaker for {req.username}")
    return {"ok": True}


# ── 2FA endpoints ──────────────────────────────────────────────────────────────

class TwoFACodeRequest(BaseModel):
    code: str


@app.get("/2fa/status")
async def get_2fa_status(user=Depends(get_current_user)):
    data = db.get_totp_data(user.username)
    return {"enabled": bool(data and data.get("totp_enabled"))}


@app.get("/2fa/setup")
async def setup_2fa(user=Depends(get_current_user)):
    import pyotp
    data = db.get_totp_data(user.username)
    # Refuse to overwrite an already-active secret — user must disable first
    if data and data.get("totp_enabled"):
        raise HTTPException(
            status_code=400,
            detail="2FA is already enabled. Disable it before setting up a new authenticator."
        )
    secret = pyotp.random_base32()
    db.save_totp_secret(user.username, secret)
    uri = pyotp.TOTP(secret).provisioning_uri(user.username, issuer_name="Velau")
    return {"secret": secret, "uri": uri}


@app.post("/2fa/enable")
async def enable_2fa(req: TwoFACodeRequest, user=Depends(get_current_user)):
    import pyotp
    data = db.get_totp_data(user.username)
    if not data or not data.get("totp_secret"):
        raise HTTPException(status_code=400, detail="Run /2fa/setup first.")
    if not pyotp.TOTP(data["totp_secret"]).verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code.")
    db.enable_totp(user.username)
    return {"ok": True}


@app.post("/2fa/verify")
async def verify_2fa(req: TwoFACodeRequest, user=Depends(get_current_user)):
    import pyotp
    data = db.get_totp_data(user.username)
    if not data or not data.get("totp_enabled"):
        return {"valid": True}
    valid = pyotp.TOTP(data["totp_secret"]).verify(req.code, valid_window=1)
    return {"valid": valid}


@app.post("/2fa/disable")
async def disable_2fa(req: TwoFACodeRequest, user=Depends(get_current_user)):
    import pyotp
    data = db.get_totp_data(user.username)
    if not data or not data.get("totp_secret"):
        raise HTTPException(status_code=400, detail="2FA is not set up.")
    if not pyotp.TOTP(data["totp_secret"]).verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code.")
    db.disable_totp(user.username)
    return {"ok": True}