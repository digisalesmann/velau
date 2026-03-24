@app.get("/ping")
async def ping():
    return {"status": "ok"}


from fastapi import FastAPI, HTTPException, Depends, status
from pydantic import BaseModel
from typing import Optional
import asyncio
from brokers.deriv_rest import DerivREST
from brokers.deriv_trading_service import DerivTradingService
from user_models import User, router as users_router, get_current_user

# Instantiate FastAPI app before any route decorators
app = FastAPI()
app.include_router(users_router)

class DashboardResponse(BaseModel):
    username: str
    bot_status: str
    balance: float
    currency: str = "USD"
    account_id: Optional[str] = None

@app.get("/dashboard", response_model=DashboardResponse, status_code=status.HTTP_200_OK)
async def get_dashboard(user=Depends(get_current_user)):
    try:
        deriv = DerivREST()
        account_info = deriv.get_account_info()
        balance = float(account_info.get("balance", 0))
        currency = account_info.get("currency", "USD")
        account_id = account_info.get("account_id")
        bot_status = "active"
        return DashboardResponse(
            username=user.username,
            bot_status=bot_status,
            balance=balance,
            currency=currency,
            account_id=account_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dashboard error: {e}")

class AuthResponse(BaseModel):
    success: bool
    message: str
    details: dict = None

class TickRequest(BaseModel):
    symbol: str = "frxXAUUSD"

class TradeRequest(BaseModel):
    contract_type: str
    amount: float
    duration: int
    symbol: str = "frxXAUUSD"

@app.post("/auth", response_model=AuthResponse)
async def authenticate():
    service = DerivTradingService()
    try:
        auth_response = await service.authenticate()
        await service.close()
        return AuthResponse(success=True, message="Authenticated", details=auth_response)
    except Exception as e:
        return AuthResponse(success=False, message=str(e))


@app.post("/ticks")
async def subscribe_ticks(req: TickRequest, user=Depends(get_current_user)):
    service = DerivTradingService()
    try:
        await service.authenticate()
        ticks = await service.subscribe_ticks(symbol=req.symbol)
        await service.close()
        return ticks
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trade")
async def place_trade(req: TradeRequest, user=Depends(get_current_user)):
    service = DerivTradingService()
    try:
        await service.authenticate()
        trade_result = await service.place_order(
            contract_type=req.contract_type,
            amount=req.amount,
            duration=req.duration,
            symbol=req.symbol
        )
        await service.close()
        return trade_result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
