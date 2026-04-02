from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import asyncio
import sqlite3
import traceback
from datetime import datetime, date
from contextlib import asynccontextmanager

from user_models import User, router as users_router, get_current_user, DB_PATH
from news.news_pipeline import get_news_and_sentiment
from core.strategy_engine import XAUMasterStrategy

# 1. INITIALIZE THE MASTER BOT
trading_bot = XAUMasterStrategy()
bot_task: Optional[asyncio.Task] = None


async def _delayed_bot_start(delay: int = 10):
    """
    Wait for the server to fully boot before starting the bot loop.
    On Render cold starts the network stack isn't ready immediately —
    starting the WebSocket bot before uvicorn finishes its startup
    causes recv() to get cancelled by the lifespan task runner.
    """
    print(f"⏳ Bot starts in {delay}s (waiting for server to settle)...")
    await asyncio.sleep(delay)
    await trading_bot.start_bot_loop()


# 2. LIFESPAN MANAGER
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_task
    bot_task = asyncio.create_task(_delayed_bot_start(delay=10))
    print("🚀 AI Trading Bot Engine queued (10s delay)")
    yield
    trading_bot.is_running = False
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    print("🛑 AI Trading Bot Engine stopped.")


# 3. INITIALIZE APP
app = FastAPI(lifespan=lifespan)
app.include_router(users_router)


# 4. MODELS
class NewsResponse(BaseModel):
    articles: list
    sentiment: dict

class DashboardResponse(BaseModel):
    username:         str
    bot_status:       str
    balance:          float
    currency:         str = "USD"
    account_id:       Optional[str] = None
    win_rate:         float = 0.0
    trades_today:     int = 0
    daily_pnl:        float = 0.0
    daily_pnl_percent: float = 0.0
    market_bias:      str = "Neutral"

class TickRequest(BaseModel):
    symbol: str = "frxXAUUSD"

class TradeRequest(BaseModel):
    contract_type: Optional[str] = None
    amount:        Optional[float] = None
    duration:      Optional[int] = None
    symbol:        str = "frxXAUUSD"
    action:        Optional[str] = "buy"


# 5. ENDPOINTS

@app.get("/")
async def root():
    return {"status": "ok", "bot_running": trading_bot.is_running}

@app.get("/bot/status")
async def get_bot_status(user=Depends(get_current_user)):
    return {"is_running": trading_bot.is_running}

@app.post("/bot/toggle")
async def toggle_bot(user=Depends(get_current_user)):
    global bot_task
    if trading_bot.is_running:
        trading_bot.is_running = False
        if bot_task:
            bot_task.cancel()
        return {"message": "Bot paused", "is_running": False}
    else:
        trading_bot.is_running = True
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

        today_trades = [
            t for t in trades_list
            if datetime.fromtimestamp(t.get("time", 0)).date() == date.today()
        ]
        trades_today_count = len(today_trades)
        daily_pnl  = sum(float(t.get("pnl", 0)) for t in today_trades)
        wins       = len([t for t in trades_list if float(t.get("pnl", 0)) > 0])
        win_rate   = (wins / len(trades_list) * 100) if trades_list else 0.0

        market_bias = "Neutral"
        try:
            conn   = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT bias FROM signals ORDER BY timestamp DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                market_bias = row[0]
            conn.close()
        except Exception:
            pass

        balance     = account_info.get("balance", 0.0)
        pnl_percent = (daily_pnl / balance * 100) if balance > 0 else 0.0

        return DashboardResponse(
            username=user.username,
            bot_status="active" if trading_bot.is_running else "paused",
            balance=balance,
            currency=account_info.get("currency", "USD"),
            account_id=account_info.get("account_id"),
            win_rate=round(win_rate, 1),
            trades_today=trades_today_count,
            daily_pnl=round(daily_pnl, 2),
            daily_pnl_percent=round(pnl_percent, 2),
            market_bias=market_bias,
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
        conn   = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT 30"
        )
        signals = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return {"signals": signals}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard/history")
async def get_history(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        history = await service.get_statement()
        return history
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
        ticks = await service.subscribe_ticks(symbol=req.symbol)
        return ticks
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
        symbols = await service.get_available_symbols()
        return {"count": len(symbols), "symbols": symbols}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/trade")
async def place_trade(req: TradeRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService

    if not req.contract_type or req.contract_type.upper() not in ("CALL", "PUT"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid contract_type '{req.contract_type}'. Must be CALL or PUT.",
        )
    if not req.amount or req.amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="amount must be a positive number.",
        )

    service = DerivTradingService()
    try:
        await service.authenticate()

        if req.action == "close":
            return {"status": "success", "message": "Position closed."}

        print(
            f"📲 Manual trade | type={req.contract_type} "
            f"amount={req.amount} symbol={req.symbol} "
            f"user={user.username}"
        )

        result = await service.place_order(
            contract_type=req.contract_type.upper(),
            amount=req.amount,
            duration=5,
            symbol=req.symbol,
        )

        print(f"✅ Manual trade placed: {result}")
        return result

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Trade failed: {str(e)}")
    finally:
        await service.close()