import logging
import asyncio
import traceback
from datetime import datetime, date
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends
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

trading_bot = XAUMasterStrategy()
bot_task: Optional[asyncio.Task] = None


async def _bot_runner(delay: int = 10):
    logger.info(f"⏳ Bot starts in {delay}s...")
    await asyncio.sleep(delay)
    logger.info("🤖 Bot loop starting now")
    try:
        await trading_bot.start_bot_loop()
    except asyncio.CancelledError:
        logger.info("Bot task cancelled cleanly")
    except Exception as e:
        logger.error(f"Bot loop fatal error: {e}")
        traceback.print_exc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_task
    logger.info("🚀 FastAPI startup — queueing bot")
    bot_task = asyncio.create_task(_bot_runner(delay=10))
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

@app.post("/notifications/register")
async def register_fcm(req: FCMTokenRequest, user=Depends(get_current_user)):
    notif.register_token(req.token)
    return {"status": "registered"}

@app.post("/notifications/unregister")
async def unregister_fcm(req: FCMTokenRequest, user=Depends(get_current_user)):
    notif.unregister_token(req.token)
    return {"status": "unregistered"}

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

@app.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        account_info = await service.get_account_info()
        history_data = await service.get_statement()
        trades_list  = history_data.get("history", [])

        balance = account_info.get("balance", 0.0)

        # ── Accurate stats from full statement ────────────────────────────────
        total_trades = len(trades_list)
        wins         = len([t for t in trades_list if float(t.get("pnl", 0)) > 0])
        win_rate     = round(wins / total_trades * 100, 1) if total_trades > 0 else 0.0

        # Today's trades and P&L
        today_trades = [
            t for t in trades_list
            if t.get("time") and
            datetime.fromtimestamp(int(t["time"])).date() == date.today()
        ]
        daily_pnl    = sum(float(t.get("pnl", 0)) for t in today_trades)
        pnl_percent  = (daily_pnl / balance * 100) if balance > 0 else 0.0

        market_bias  = db.get_latest_bias()

        return DashboardResponse(
            username=user.username,
            bot_status="active" if trading_bot.is_running else "paused",
            balance=balance,
            currency=account_info.get("currency", "USD"),
            account_id=account_info.get("account_id"),
            win_rate=win_rate,
            trades_today=len(today_trades),
            total_trades=total_trades,
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

@app.get("/open_contracts")
async def get_open_contracts(user=Depends(get_current_user)):
    """Returns currently open binary options contracts."""
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        await service.ws.send({
            "proposal_open_contracts": 1,
            "subscribe": 0,   # just fetch, don't subscribe
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
                "expiry_time":   c.get("expiry_time", 0),
            })
        return {"contracts": open_list}
    except Exception as e:
        traceback.print_exc()
        return {"contracts": [], "error": str(e)}
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
        return {"symbols": await service.get_available_symbols()}
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


# ── Candles endpoint (for chart screen) ───────────────────────────────────────
class CandleRequest(BaseModel):
    symbol:      str = "frxXAUUSD"
    count:       int = 120
    granularity: int = 300   # seconds: 60=1m, 300=5m, 900=15m, 3600=1h, 14400=4h

@app.post("/candles")
async def get_candles(req: CandleRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        raw = await service.get_candles(
            symbol=req.symbol,
            count=min(req.count, 300),
            granularity=req.granularity,
        )
        return {"candles": raw}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()