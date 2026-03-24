from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import traceback

# Safely import broker modules — don't crash if they fail
try:
    from brokers.deriv_rest import DerivREST
    from brokers.deriv_trading_service import DerivTradingService
    BROKERS_AVAILABLE = True
except Exception as e:
    print(f"WARNING: Broker modules failed to import: {e}")
    BROKERS_AVAILABLE = False

from user_models import User, router as users_router, get_current_user

app = FastAPI()
app.include_router(users_router)


@app.get("/ping")
async def ping():
    return {"status": "ok", "brokers_available": BROKERS_AVAILABLE}


class DashboardResponse(BaseModel):
    username: str
    bot_status: str
    balance: float
    currency: str = "USD"
    account_id: Optional[str] = None


@app.get("/dashboard", response_model=DashboardResponse, status_code=status.HTTP_200_OK)
async def get_dashboard(user=Depends(get_current_user)):
    if not BROKERS_AVAILABLE:
        # Return placeholder data if broker modules aren't available
        return DashboardResponse(
            username=user.username,
            bot_status="inactive",
            balance=0.0,
            currency="USD",
            account_id=None,
        )
    try:
        deriv = DerivREST()
        account_info = deriv.get_account_info()
        balance = float(account_info.get("balance", 0))
        currency = account_info.get("currency", "USD")
        account_id = account_info.get("account_id")
        return DashboardResponse(
            username=user.username,
            bot_status="active",
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
    if not BROKERS_AVAILABLE:
        return AuthResponse(success=False, message="Broker modules not available")
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        auth_response = await service.authenticate()
        await service.close()
        return AuthResponse(success=True, message="Authenticated", details=auth_response)
    except Exception as e:
        return AuthResponse(success=False, message=str(e))


@app.post("/ticks")
async def subscribe_ticks(req: TickRequest, user=Depends(get_current_user)):
    if not BROKERS_AVAILABLE:
        raise HTTPException(status_code=503, detail="Broker modules not available")
    from brokers.deriv_trading_service import DerivTradingService
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
    if not BROKERS_AVAILABLE:
        raise HTTPException(status_code=503, detail="Broker modules not available")
    from brokers.deriv_trading_service import DerivTradingService
    service = DerivTradingService()
    try:
        await service.authenticate()
        trade_result = await service.place_order(
            contract_type=req.contract_type,
            amount=req.amount,
            duration=req.duration,
            symbol=req.symbol,
        )
        await service.close()
        return trade_result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))