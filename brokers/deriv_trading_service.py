"""
Unified Deriv trading service for live production use.
Handles REST OTP, WebSocket authentication, market data, and trading actions.
"""
import asyncio
import logging
from brokers.deriv_rest import DerivREST
from brokers.deriv_ws import DerivWebSocket
from config import settings

logger = logging.getLogger("DerivTradingService")

class DerivTradingService:
    def __init__(self, app_id=None, token=None, ws_url=None):
        self.rest = DerivREST(app_id, token)
        self.ws_url = ws_url or "wss://ws.derivws.com/websockets/v3?app_id=" + (app_id or settings.DERIV_APP_ID)
        self.ws = DerivWebSocket(self.ws_url)
        self.account_id = None
        self.otp = None

    async def authenticate(self):
        # Get account info and generate OTP
        info = self.rest.get_account_info()
        self.account_id = info.get("id") or info.get("account_id")
        if not self.account_id:
            logger.error("Account ID not found in account info.")
            raise Exception("Account ID not found.")
        otp_resp = self.rest.generate_otp(self.account_id)
        self.otp = otp_resp.get("otp")
        if not self.otp:
            logger.error("OTP not found in response.")
            raise Exception("OTP not found.")
        logger.info("OTP generated. Connecting WebSocket...")
        await self.ws.connect()
        # Authenticate WebSocket
        auth_msg = {"authorize": self.otp}
        await self.ws.send(auth_msg)
        auth_response = await self.ws.receive()
        if auth_response.get("error"):
            logger.error(f"WebSocket auth error: {auth_response['error']}")
            raise Exception(f"WebSocket auth error: {auth_response['error']}")
        logger.info("WebSocket authenticated.")
        return auth_response

    async def subscribe_ticks(self, symbol="frxXAUUSD"):
        # Subscribe to tick data for a symbol
        sub_msg = {"ticks_subscribe": symbol}
        await self.ws.send(sub_msg)
        return await self.ws.receive()

    async def place_order(self, contract_type, amount, duration, symbol="frxXAUUSD"):
        # Place a trade order (example for a simple contract)
        order_msg = {
            "buy": 1,
            "price": amount,
            "parameters": {
                "contract_type": contract_type,
                "symbol": symbol,
                "duration": duration,
                "duration_unit": "m"
            }
        }
        await self.ws.send(order_msg)
        return await self.ws.receive()

    async def close(self):
        await self.ws.close()
