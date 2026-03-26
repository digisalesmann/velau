from fastapi import FastAPI, HTTPException, Depends, status
from pydantic import BaseModel
from typing import Optional, List
import asyncio
from contextlib import asynccontextmanager

from user_models import User, router as users_router, get_current_user

# Import for news endpoint
from news.news_pipeline import get_news_and_sentiment

# Import the new Strategy Engine
from strategy_engine import XAUMasterStrategy

# 1. INITIALIZE THE MASTER BOT
trading_bot = XAUMasterStrategy()

# 2. LIFESPAN MANAGER (Starts the bot in the background on boot)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the autonomous scanning loop
    bot_task = asyncio.create_task(trading_bot.start_bot_loop())
    print("🚀 AI Trading Bot Engine Started in Background!")
    yield
    # Clean shutdown
    trading_bot.is_running = False
    bot_task.cancel()
    print("🛑 AI Trading Bot Engine Stopped.")

# 3. INITIALIZE APP WITH LIFESPAN
app = FastAPI(lifespan=lifespan)
app.include_router(users_router)


# 4. DEFINE ALL PYDANTIC MODELS
class NewsResponse(BaseModel):
    articles: list
    sentiment: dict

class DashboardResponse(BaseModel):
    username: str
    bot_status: str
    balance: float
    currency: str = "USD"
    account_id: Optional[str] = None

class TickRequest(BaseModel):
    symbol: str = "frxXAUUSD"

class TradeRequest(BaseModel):
    contract_type: Optional[str] = None
    amount: Optional[float] = None
    duration: Optional[int] = None
    symbol: str = "frxXAUUSD"
    action: Optional[str] = "buy" 


# 5. DEFINE ALL ENDPOINTS / ROUTES
@app.get("/")
async def root():
    return {"status": "ok", "bot_running": trading_bot.is_running}

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.get("/news", response_model=NewsResponse)
async def get_news():
    try:
        articles, sentiment = get_news_and_sentiment()
        return NewsResponse(articles=articles, sentiment=sentiment)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"News error: {e}")

@app.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        account_info = await service.get_account_info()
        return DashboardResponse(
            username=user.username,
            bot_status="active" if trading_bot.is_running else "paused",
            balance=account_info.get("balance", 0.0),
            currency=account_info.get("currency", "USD"),
            account_id=account_info.get("account_id"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dashboard error: {e}")
    finally:
        await service.close()

@app.get("/dashboard/history")
async def get_history(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        history = await service.get_statement()
        return history
    except Exception as e:
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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()

@app.post("/trade")
async def place_trade(req: TradeRequest, user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        
        # Branching logic for action types
        if req.action == "close":
            return {"status": "success", "message": f"Position on {req.symbol} closed."}
            
        result = await service.place_order(
            contract_type=req.contract_type,
            amount=req.amount,
            duration=req.duration,
            symbol=req.symbol,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await service.close()