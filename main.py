from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import asyncio
import traceback
from datetime import datetime, date
from contextlib import asynccontextmanager

from user_models import User, router as users_router, get_current_user
from news.news_pipeline import get_news_and_sentiment
from core.strategy_engine import XAUMasterStrategy
import database as db
import notifications as notif

trading_bot = XAUMasterStrategy()
bot_task: Optional[asyncio.Task] = None


async def _delayed_bot_start(delay: int = 10):
    print(f"⏳ Bot starts in {delay}s...")
    await asyncio.sleep(delay)
    await trading_bot.start_bot_loop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_task
    bot_task = asyncio.create_task(_delayed_bot_start(delay=10))
    print("🚀 AI Trading Bot Engine queued")
    yield
    trading_bot.is_running = False
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    print("🛑 Bot stopped.")


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
    daily_pnl:           float = 0.0
    daily_pnl_percent:   float = 0.0
    market_bias:         str = "Neutral"
    circuit_broken:      bool = False
    consecutive_losses:  int = 0
    trade_in_progress:   bool = False
    in_session:          bool = True

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


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status":         "ok",
        "bot_running":    trading_bot.is_running,
        "circuit_broken": trading_bot._circuit_broken,
        "in_session":     trading_bot._in_trading_session(),
    }

# ── Push notification registration ────────────────────────────────────────────
@app.post("/notifications/register")
async def register_fcm(req: FCMTokenRequest, user=Depends(get_current_user)):
    """Called by Flutter app on startup to register the device FCM token."""
    notif.register_token(req.token)
    return {"status": "registered"}

@app.post("/notifications/unregister")
async def unregister_fcm(req: FCMTokenRequest, user=Depends(get_current_user)):
    notif.unregister_token(req.token)
    return {"status": "unregistered"}

# ── Bot control ────────────────────────────────────────────────────────────────
@app.get("/bot/status")
async def get_bot_status(user=Depends(get_current_user)):
    return {
        "is_running":         trading_bot.is_running,
        "circuit_broken":     trading_bot._circuit_broken,
        "consecutive_losses": trading_bot._consecutive_losses,
        "trade_in_progress":  trading_bot._trade_in_progress,
        "daily_pnl":          trading_bot._daily_pnl,
        "in_session":         trading_bot._in_trading_session(),
    }

@app.post("/bot/toggle")
async def toggle_bot(user=Depends(get_current_user)):
    global bot_task
    if trading_bot.is_running:
        trading_bot.is_running = False
        if bot_task:
            bot_task.cancel()
        return {"message": "Bot paused", "is_running": False}
    else:
        trading_bot.is_running          = True
        trading_bot._circuit_broken     = False
        trading_bot._consecutive_losses = 0
        bot_task = asyncio.create_task(trading_bot.start_bot_loop())
        return {"message": "Bot started", "is_running": True}

# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        account_info = await service.get_account_info()
        history_data = await service.get_statement()
        trades_list  = history_data.get("history", [])

        today_trades = [
            t for t in trades_list
            if datetime.fromtimestamp(t.get("time", 0)).date() == date.today()
        ]
        daily_pnl   = sum(float(t.get("pnl", 0)) for t in today_trades)
        stats       = db.get_trade_stats()
        market_bias = db.get_latest_bias()
        balance     = account_info.get("balance", 0.0)
        pnl_percent = (daily_pnl / balance * 100) if balance > 0 else 0.0

        return DashboardResponse(
            username=user.username,
            bot_status="active" if trading_bot.is_running else "paused",
            balance=balance,
            currency=account_info.get("currency", "USD"),
            account_id=account_info.get("account_id"),
            win_rate=round(stats["win_rate"], 1),
            trades_today=len(today_trades),
            daily_pnl=round(daily_pnl, 2),
            daily_pnl_percent=round(pnl_percent, 2),
            market_bias=market_bias,
            circuit_broken=trading_bot._circuit_broken,
            consecutive_losses=trading_bot._consecutive_losses,
            trade_in_progress=trading_bot._trade_in_progress,
            in_session=trading_bot._in_trading_session(),
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Dashboard error: {e}")
    finally:
        await service.close()

@app.get("/news", response_model=NewsResponse)
async def get_news():
    try:
        articles, sentiment = get_news_and_sentiment()
        return NewsResponse(articles=articles, sentiment=sentiment)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"News error: {e}")

@app.get("/signals")
async def get_signals(user=Depends(get_current_user)):
    try:
        return {"signals": db.get_signals(limit=30)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard/history")
async def get_history(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        return await service.get_statement()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/ticks")
async def subscribe_ticks(req: TickRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        return await service.subscribe_ticks(symbol=req.symbol)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.get("/symbols")
async def get_symbols(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        return {"count": 0, "symbols": await service.get_available_symbols()}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/trade")
async def place_trade(req: TradeRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    if not req.contract_type or req.contract_type.upper() not in ("CALL", "PUT"):
        raise HTTPException(status_code=400,
            detail=f"Invalid contract_type '{req.contract_type}'.")
    if not req.amount or req.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive.")

    service = DerivTradingService()
    try:
        await service.authenticate()
        if req.action == "close":
            return {"status": "success", "message": "Position closed."}
        result = await service.place_order(
            contract_type=req.contract_type.upper(),
            amount=req.amount, duration=5, symbol=req.symbol,
        )
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Trade failed: {str(e)}")
    finally:
        await service.close()