import os
import uuid
import base64
import logging
import asyncio
import traceback
from datetime import datetime, date
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger("Main")

from user_models import User, router as users_router, get_current_user
from rate_limit import twofa_limiter
from news.news_pipeline import get_news_and_sentiment
from core.strategy_engine import XAUMasterStrategy
import database as db
import deriv_cache
from core import notifications as notif
import storage

trading_bot = XAUMasterStrategy()
bot_task: Optional[asyncio.Task] = None

# Short-TTL cache for expensive Deriv-derived display data — balance/history
# only, never anything trade-execution related. See deriv_cache.py.
DASHBOARD_CACHE_TTL = 30
HISTORY_CACHE_TTL   = 30


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

from fastapi.staticfiles import StaticFiles
# Serves static/velau-logo.png at /static/velau-logo.png — used as a stable,
# publicly hosted logo URL for outbound emails (SendGrid, etc.), since email
# clients need a real HTTPS image URL, not a bundled app asset.
app.mount("/static", StaticFiles(directory="static"), name="static")


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
    payment_method_id: int

class SubmitProofRequest(BaseModel):
    payment_id: str
    reference: str
    image_base64: Optional[str] = None
    content_type: str = "image/jpeg"

class CandleRequest(BaseModel):
    symbol:      str = "frxXAUUSD"
    count:       int = 120
    granularity: int = 300


# ── Helpers ────────────────────────────────────────────────────────────────────
def _get_user_deriv_context(username: str) -> tuple[str, str]:
    """
    Get this user's own connected Deriv token and their demo/real preference.
    Raises 400 with 'no_deriv_account' if they haven't connected one —
    deliberately no server-level fallback: these endpoints (including
    /trade) must never let one user act on another account's money.
    """
    me = db.get_user(username) or {}
    token = me.get("deriv_token")
    if not token:
        raise HTTPException(status_code=400, detail="no_deriv_account")
    return token, (me.get("trade_account_type") or "real")


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
    service = DerivTradingService(token=req.api_token, max_retries=2)
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
    service = DerivTradingService(
        token=token, account_type=me.get("trade_account_type") or "real", max_retries=2
    )
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
        account_type = profile.get("trade_account_type") or "real"
        cache_key = f"dashboard:{user.username}:{account_type}"
        cached = deriv_cache.get(cache_key, DASHBOARD_CACHE_TTL)

        if cached is not None:
            balance, currency, account_id = cached["balance"], cached["currency"], cached["account_id"]
            win_rate, total_trades   = cached["win_rate"], cached["total_trades"]
            trades_today, daily_pnl = cached["trades_today"], cached["daily_pnl"]
            pnl_percent = cached["pnl_percent"]
        else:
            from brokers.deriv_trading_service import DerivTradingService
            # Single attempt, no retry — this is a screen someone's waiting
            # on. A retry-with-backoff here can itself take ~30s (open_timeout
            # + backoff + open_timeout again), which blows past the mobile
            # app's own 20s request timeout — so the client hits its timeout
            # and shows a hard error before this function ever gets to fall
            # back to the stale cache below. Failing fast lets that fallback
            # actually reach the user instead of racing (and losing) against
            # the client's own timeout. See deriv_ws.py's default of 7 for
            # why a higher retry budget is right for unattended jobs but
            # wrong here.
            service = DerivTradingService(token=deriv_token, account_type=account_type, max_retries=1)
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

                deriv_cache.set(cache_key, {
                    "balance": balance, "currency": currency, "account_id": account_id,
                    "win_rate": win_rate, "total_trades": total_trades,
                    "trades_today": trades_today, "daily_pnl": daily_pnl,
                    "pnl_percent": pnl_percent,
                })
            except Exception as e:
                logger.warning(f"Dashboard: Deriv fetch failed for {user.username}: {e}")
                # Minutes-old real data beats showing zeros on a transient blip.
                stale = deriv_cache.get_stale(cache_key)
                if stale is not None:
                    balance, currency, account_id = stale["balance"], stale["currency"], stale["account_id"]
                    win_rate, total_trades   = stale["win_rate"], stale["total_trades"]
                    trades_today, daily_pnl = stale["trades_today"], stale["daily_pnl"]
                    pnl_percent = stale["pnl_percent"]
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
    token, account_type = _get_user_deriv_context(user.username)
    service = DerivTradingService(token=token, account_type=account_type, max_retries=1)
    try:
        await service.authenticate()
        # Manual trades can stack on top of (or alongside) an automated one,
        # so this account is no longer guaranteed to have at most one open
        # contract — querying proposal_open_contract without a contract_id
        # silently returns only one of them. portfolio lists every open
        # contract_id; each is then fetched individually for its live
        # price/profit, which portfolio itself doesn't include.
        await service.ws.send({"portfolio": 1})
        portfolio_resp = await service.ws.receive(timeout=20.0)
        if portfolio_resp.get("error"):
            return {"contracts": []}
        contract_ids = [
            c.get("contract_id")
            for c in portfolio_resp.get("portfolio", {}).get("contracts", [])
            if c.get("contract_id")
        ]

        open_list = []
        for cid in contract_ids:
            try:
                await service.ws.send({"proposal_open_contract": 1, "contract_id": cid})
                response = await service.ws.receive(timeout=20.0)
            except Exception:
                continue
            if response.get("error"):
                continue
            c = response.get("proposal_open_contract")
            if not c or c.get("is_expired") or c.get("is_settleable"):
                continue
            open_list.append({
                "contract_id":   c.get("contract_id"),
                "symbol":        c.get("underlying_symbol", "frxXAUUSD"),
                "contract_type": c.get("contract_type", ""),
                "buy_price":     c.get("buy_price", 0),
                "current_spot":  c.get("current_spot", 0),
                "profit":        c.get("profit", 0),
                "entry_spot":    c.get("entry_spot", 0),
                "payout":        c.get("payout", 0),
                "date_start":    c.get("date_start"),
                "date_expiry":   c.get("date_expiry"),
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
        from core.strategy_engine import SESSIONS
        now_utc  = datetime.now(tz.utc)
        hour_utc = now_utc.hour
        in_session = trading_bot._in_trading_session()

        # Single source of truth for the trading window — must match SESSIONS
        # in core/strategy_engine.py, which is what actually gates the bot.
        session_start_utc, session_end_utc = SESSIONS[0]
        if hour_utc < session_start_utc:
            mins_to_session = (session_start_utc - hour_utc) * 60 - now_utc.minute
        elif hour_utc >= session_end_utc:
            # After NY session — next is tomorrow London
            mins_to_session = (24 - hour_utc + session_start_utc) * 60 - now_utc.minute
        else:
            mins_to_session = 0  # in session

        return {
            "signals":         db.get_signals(limit=30, username=user.username),
            "in_session":      in_session,
            "session_hours":   f"{session_start_utc:02d}:00-{session_end_utc:02d}:00 UTC (London + NY)",
            "mins_to_session": mins_to_session if not in_session else 0,
            "bot_running":     trading_bot.is_running,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard/history")
async def get_history(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    token, account_type = _get_user_deriv_context(user.username)

    cache_key = f"history:{user.username}:{account_type}"
    cached = deriv_cache.get(cache_key, HISTORY_CACHE_TTL)
    if cached is not None:
        return cached

    # Single attempt, no retry — see the /dashboard endpoint's comment above
    # for why a retry-with-backoff here defeats the stale-cache fallback by
    # outrunning the mobile client's own request timeout.
    service = DerivTradingService(token=token, account_type=account_type, max_retries=1)
    try:
        await service.authenticate()
        result = await service.get_statement()
        deriv_cache.set(cache_key, result)
        return result
    except Exception as e:
        # Minutes-old history beats a hard failure on a transient blip.
        stale = deriv_cache.get_stale(cache_key)
        if stale is not None:
            return stale
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/ticks")
async def subscribe_ticks(req: TickRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    token, account_type = _get_user_deriv_context(user.username)
    service = DerivTradingService(token=token, account_type=account_type, max_retries=2)
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
    token, account_type = _get_user_deriv_context(user.username)
    service = DerivTradingService(token=token, account_type=account_type, max_retries=2)
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
    from position_sizing import MAX_STAKE

    if not req.contract_type or req.contract_type.upper() not in ("CALL", "PUT"):
        raise HTTPException(status_code=400, detail="Invalid contract_type.")
    if not req.amount or req.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive.")
    if req.amount > MAX_STAKE:
        raise HTTPException(status_code=400, detail=f"Stake cannot exceed ${MAX_STAKE:.2f}.")

    # Manual trades must respect the same circuit breaker the bot enforces
    # on itself — otherwise a tripped user could route around it by hand.
    risk = db.get_user_risk_state(user.username)
    if risk["circuit_broken"]:
        raise HTTPException(
            status_code=403,
            detail="Trading is paused for today after hitting your risk limit. Resumes at midnight UTC.",
        )

    token, account_type = _get_user_deriv_context(user.username)
    service = DerivTradingService(token=token, account_type=account_type, max_retries=2)
    try:
        await service.authenticate()
        result = await service.place_order(
            contract_type=req.contract_type.upper(),
            amount=req.amount, duration=5, symbol=req.symbol,
        )
        # Hand off to the bot's own settlement path so this trade counts
        # toward daily P&L / consecutive-loss tracking exactly like an
        # automated one — monitor opens its own fresh connections per poll.
        contract_id = result.get("buy", {}).get("contract_id")
        if contract_id:
            asyncio.create_task(
                trading_bot._monitor_contract(
                    token, account_type, contract_id, req.amount, user.username
                )
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


@app.get("/subscription/methods")
async def list_payment_methods(user=Depends(get_current_user)):
    """Enabled payment methods for the checkout picker."""
    return {"methods": db.get_payment_methods(enabled_only=True)}


@app.post("/subscription/create")
async def create_subscription(req: SubscriptionCreateRequest,
                              user=Depends(get_current_user)):
    """Create a pending order against an admin-configured payment method.
    No external processor — the user pays externally and submits proof for
    admin review via /subscription/proof."""
    from payments import PLANS

    if req.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose monthly, yearly, or lifetime.")

    if db.get_active_subscription(user.username):
        raise HTTPException(status_code=400, detail="You already have an active subscription.")

    if db.get_pending_subscription_for_user(user.username):
        raise HTTPException(
            status_code=400,
            detail="You already have a payment in progress. Cancel it or wait for review.",
        )

    method = db.get_payment_method(req.payment_method_id)
    if not method:
        raise HTTPException(status_code=404, detail="Payment method not found.")
    if not method["enabled"]:
        raise HTTPException(status_code=400, detail="This payment method is no longer available.")

    price_usd = PLANS[req.plan]["usd"]
    payment_id = uuid.uuid4().hex

    if method["type"] == "crypto":
        pay_address  = method["crypto_address"] or ""
        pay_currency = f'{method["crypto_currency"]} ({method["crypto_network"]})'
    else:  # bank_transfer
        lines = [f'Bank: {method["bank_name"]}', f'Account Name: {method["bank_account_name"]}',
                  f'Account Number: {method["bank_account_number"]}']
        if method.get("bank_routing_number"):
            lines.append(f'Routing Number: {method["bank_routing_number"]}')
        if method.get("bank_swift"):
            lines.append(f'SWIFT: {method["bank_swift"]}')
        if method.get("bank_iban"):
            lines.append(f'IBAN: {method["bank_iban"]}')
        pay_address  = "\n".join(lines)
        pay_currency = f'Bank Transfer ({method["bank_currency"]})'

    db.create_pending_subscription(
        username=user.username,
        plan=req.plan,
        payment_id=payment_id,
        payment_method_id=method["id"],
        method_type=method["type"],
        pay_address=pay_address,
        pay_amount=price_usd,
        pay_currency=pay_currency,
        price_usd=price_usd,
    )

    return {
        "payment_id":   payment_id,
        "plan":         req.plan,
        "price_usd":    price_usd,
        "pay_amount":   price_usd,
        "pay_currency": pay_currency,
        "pay_address":  pay_address,
        "method_type":  method["type"],
        "instructions": method.get("instructions"),
    }


@app.get("/subscription/poll/{payment_id}")
async def poll_payment(payment_id: str, user=Depends(get_current_user)):
    """Client polls this every ~15s while a payment is in progress. Returns
    the order's own status — 'pending'|'pending_review'|'active'|'rejected'|'cancelled'."""
    sub = db.get_subscription_by_payment(payment_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Payment not found.")
    if sub["username"] != user.username and not db.is_admin(user.username):
        raise HTTPException(status_code=403, detail="Not your payment.")

    return {
        "status":           sub["status"],
        "plan":             sub["plan"],
        "expires_at":       sub.get("expires_at"),
        "rejection_reason": sub.get("rejection_reason"),
    }


class CancelPaymentRequest(BaseModel):
    payment_id: str

@app.post("/subscription/cancel")
async def cancel_subscription(req: CancelPaymentRequest, user=Depends(get_current_user)):
    """User-initiated cancel of a pending payment they no longer want to complete.
    Deliberately only works while status=='pending' — once proof is submitted
    (status=='pending_review'), only admin approve/reject should resolve it."""
    sub = db.get_subscription_by_payment(req.payment_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Payment not found.")
    if sub["username"] != user.username:
        raise HTTPException(status_code=403, detail="Not your payment.")
    if sub["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot cancel a {sub['status']} payment.")
    db.cancel_pending_subscription(req.payment_id, user.username)
    return {"ok": True}


@app.post("/subscription/proof")
async def submit_payment_proof(req: SubmitProofRequest, user=Depends(get_current_user)):
    """User submits a tx hash/reference (+ optional screenshot) for admin review."""
    from rate_limit import payment_proof_limiter
    payment_proof_limiter.check(user.username.lower())

    sub = db.get_subscription_by_payment(req.payment_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Payment not found.")
    if sub["username"] != user.username:
        raise HTTPException(status_code=403, detail="Not your payment.")
    if sub["status"] not in ("pending", "rejected"):
        raise HTTPException(status_code=400, detail=f"Cannot submit proof for a {sub['status']} payment.")
    if not req.reference.strip():
        raise HTTPException(status_code=400, detail="A payment reference is required.")

    image_url = None
    if req.image_base64:
        try:
            image_bytes = base64.b64decode(req.image_base64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid image data.")
        if len(image_bytes) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image must be under 5MB.")
        try:
            image_url = storage.upload_payment_proof(req.payment_id, image_bytes, req.content_type)
        except Exception as e:
            logger.warning(f"Proof upload failed for {req.payment_id}: {e}")
            raise HTTPException(status_code=502, detail="Screenshot upload failed. Please try again.")

    db.submit_payment_proof(req.payment_id, req.reference.strip(), image_url)

    notif.notify_admins(
        "New Payment Submitted for Review",
        f"{user.username} · {sub['plan'].capitalize()} Plan · ${sub['price_usd']:.2f}",
        {"type": "payment_review", "payment_id": req.payment_id},
    )

    return {"status": "pending_review"}


@app.get("/subscription/proof_image/{payment_id}")
async def get_payment_proof_image(payment_id: str, user=Depends(get_current_user)):
    """Authorized proxy for a proof screenshot — the storage bucket is private."""
    sub = db.get_subscription_by_payment(payment_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Payment not found.")
    if sub["username"] != user.username and not db.is_admin(user.username):
        raise HTTPException(status_code=403, detail="Not your payment.")
    if not sub.get("proof_image_url"):
        raise HTTPException(status_code=404, detail="No screenshot was submitted for this payment.")

    try:
        image_bytes, content_type = storage.get_payment_proof_bytes(sub["proof_image_url"])
    except Exception as e:
        logger.warning(f"Proof retrieval failed for {payment_id}: {e}")
        raise HTTPException(status_code=502, detail="Could not retrieve screenshot.")

    return Response(content=image_bytes, media_type=content_type)


# ── Admin endpoints ────────────────────────────────────────────────────────────

class AdminGrantRequest(BaseModel):
    username: str
    plan:     str  # monthly | yearly | lifetime

class AdminRevokeRequest(BaseModel):
    sub_id: int

class AdminSetAdminRequest(BaseModel):
    username: str
    value:    bool

class AdminPaymentMethodRequest(BaseModel):
    type:  str  # "crypto" | "bank_transfer"
    label: str
    enabled: bool = True
    sort_order: int = 0
    instructions: Optional[str] = None
    crypto_address:  Optional[str] = None
    crypto_network:  Optional[str] = None
    crypto_currency: Optional[str] = None
    bank_name:           Optional[str] = None
    bank_account_name:   Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_routing_number: Optional[str] = None
    bank_swift:          Optional[str] = None
    bank_iban:           Optional[str] = None
    bank_currency:       Optional[str] = None

class AdminPaymentMethodUpdateRequest(AdminPaymentMethodRequest):
    id: int

class AdminPaymentMethodDeleteRequest(BaseModel):
    id: int

class AdminSubActionRequest(BaseModel):
    sub_id: int

class AdminSubRejectRequest(BaseModel):
    sub_id: int
    reason: str


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


def _validate_payment_method_fields(req: AdminPaymentMethodRequest):
    if req.type not in ("crypto", "bank_transfer"):
        raise HTTPException(status_code=400, detail="type must be 'crypto' or 'bank_transfer'.")
    if not req.label.strip():
        raise HTTPException(status_code=400, detail="label is required.")
    if req.type == "crypto":
        if not req.crypto_address or not req.crypto_currency:
            raise HTTPException(status_code=400, detail="crypto_address and crypto_currency are required.")
    else:
        if not req.bank_name or not req.bank_account_name or not req.bank_account_number:
            raise HTTPException(
                status_code=400,
                detail="bank_name, bank_account_name, and bank_account_number are required.",
            )


@app.get("/admin/payment_methods")
async def admin_list_payment_methods(user=Depends(_require_admin)):
    return {"methods": db.get_payment_methods(enabled_only=False)}


@app.post("/admin/payment_methods/create")
async def admin_create_payment_method(req: AdminPaymentMethodRequest, user=Depends(_require_admin)):
    _validate_payment_method_fields(req)
    method_id = db.create_payment_method(
        type=req.type, label=req.label, enabled=req.enabled, sort_order=req.sort_order,
        instructions=req.instructions,
        crypto_address=req.crypto_address, crypto_network=req.crypto_network, crypto_currency=req.crypto_currency,
        bank_name=req.bank_name, bank_account_name=req.bank_account_name, bank_account_number=req.bank_account_number,
        bank_routing_number=req.bank_routing_number, bank_swift=req.bank_swift, bank_iban=req.bank_iban,
        bank_currency=req.bank_currency,
    )
    return {"id": method_id}


@app.post("/admin/payment_methods/update")
async def admin_update_payment_method(req: AdminPaymentMethodUpdateRequest, user=Depends(_require_admin)):
    if not db.get_payment_method(req.id):
        raise HTTPException(status_code=404, detail="Payment method not found.")
    _validate_payment_method_fields(req)
    db.update_payment_method(
        req.id, type=req.type, label=req.label, enabled=req.enabled, sort_order=req.sort_order,
        instructions=req.instructions,
        crypto_address=req.crypto_address, crypto_network=req.crypto_network, crypto_currency=req.crypto_currency,
        bank_name=req.bank_name, bank_account_name=req.bank_account_name, bank_account_number=req.bank_account_number,
        bank_routing_number=req.bank_routing_number, bank_swift=req.bank_swift, bank_iban=req.bank_iban,
        bank_currency=req.bank_currency,
    )
    return {"ok": True}


@app.post("/admin/payment_methods/delete")
async def admin_delete_payment_method(req: AdminPaymentMethodDeleteRequest, user=Depends(_require_admin)):
    db.delete_payment_method(req.id)
    return {"ok": True}


@app.post("/admin/subscriptions/approve")
async def admin_approve_subscription(req: AdminSubActionRequest, user=Depends(_require_admin)):
    sub = db.get_subscription_by_id(req.sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")
    if sub["status"] != "pending_review":
        raise HTTPException(status_code=400, detail=f"Cannot approve a {sub['status']} payment.")
    db.admin_approve_subscription(req.sub_id, sub["plan"], user.username)
    logger.info(f"Admin {user.username} approved subscription {req.sub_id} ({sub['username']}, {sub['plan']})")
    notif.notify_user(
        sub["username"], "Subscription Activated",
        f"Your {sub['plan']} subscription is now active. Welcome to Velau.",
        {"type": "subscription_approved"},
    )
    return {"ok": True}


@app.post("/admin/subscriptions/reject")
async def admin_reject_subscription(req: AdminSubRejectRequest, user=Depends(_require_admin)):
    sub = db.get_subscription_by_id(req.sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")
    if sub["status"] != "pending_review":
        raise HTTPException(status_code=400, detail=f"Cannot reject a {sub['status']} payment.")
    if not req.reason.strip():
        raise HTTPException(status_code=400, detail="A rejection reason is required.")
    db.admin_reject_subscription(req.sub_id, user.username, req.reason.strip())
    logger.info(f"Admin {user.username} rejected subscription {req.sub_id} ({sub['username']}): {req.reason}")
    notif.notify_user(
        sub["username"], "Payment Rejected",
        req.reason.strip(),
        {"type": "subscription_rejected"},
    )
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
    twofa_limiter.check(user.username)
    data = db.get_totp_data(user.username)
    if not data or not data.get("totp_secret"):
        raise HTTPException(status_code=400, detail="Run /2fa/setup first.")
    if not pyotp.TOTP(data["totp_secret"]).verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code.")
    twofa_limiter.reset(user.username)
    db.enable_totp(user.username)
    return {"ok": True}


@app.post("/2fa/verify")
async def verify_2fa(req: TwoFACodeRequest, user=Depends(get_current_user)):
    import pyotp
    data = db.get_totp_data(user.username)
    if not data or not data.get("totp_enabled"):
        return {"valid": True}
    twofa_limiter.check(user.username)
    valid = pyotp.TOTP(data["totp_secret"]).verify(req.code, valid_window=1)
    if valid:
        twofa_limiter.reset(user.username)
    return {"valid": valid}


@app.post("/2fa/disable")
async def disable_2fa(req: TwoFACodeRequest, user=Depends(get_current_user)):
    import pyotp
    twofa_limiter.check(user.username)
    data = db.get_totp_data(user.username)
    if not data or not data.get("totp_secret"):
        raise HTTPException(status_code=400, detail="2FA is not set up.")
    if not pyotp.TOTP(data["totp_secret"]).verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code.")
    twofa_limiter.reset(user.username)
    db.disable_totp(user.username)
    return {"ok": True}