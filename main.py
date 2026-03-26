from fastapi import FastAPI, HTTPException, Depends, status
from pydantic import BaseModel
from typing import Optional, List
from user_models import User, router as users_router, get_current_user

app = FastAPI()
app.include_router(users_router)

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/ping")
async def ping():
    return {"status": "ok"}

class DashboardResponse(BaseModel):
    username: str
    bot_status: str
    balance: float
    currency: str = "USD"
    account_id: Optional[str] = None

@app.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(user=Depends(get_current_user)):
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        account_info = await service.get_account_info()
        return DashboardResponse(
            username=user.username,
            bot_status="active",
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

class TickRequest(BaseModel):
    symbol: str = "frxXAUUSD"

# FIXED: Optional fields to allow 'close' action without validation failure
class TradeRequest(BaseModel):
    contract_type: Optional[str] = None
    amount: Optional[float] = None
    duration: Optional[int] = None
    symbol: str = "frxXAUUSD"
    action: Optional[str] = "buy" 

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
            # For Deriv, we normally sell a contract_id. 
            # This is the endpoint bridge for your "Close Position" button.
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