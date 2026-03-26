import logging
from brokers.deriv_ws import DerivWebSocket
from env_config import DERIV_TOKEN

logger = logging.getLogger("DerivTradingService")

class DerivTradingService:
    def __init__(self, app_id: str = None, token: str = None):
        self.token = token or DERIV_TOKEN
        self.ws = DerivWebSocket()
        self._authorized = False

    async def authenticate(self):
        await self.ws.connect()
        await self.ws.send({"authorize": self.token})
        response = await self.ws.receive()

        if response.get("error"):
            error_msg = response["error"].get("message", "Unknown auth error")
            logger.error(f"Deriv auth failed: {error_msg}")
            raise Exception(f"Deriv auth failed: {error_msg}")

        self._authorized = True
        return response

    async def get_account_info(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"balance": 1})
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(response["error"].get("message", "Balance fetch failed"))
        balance_data = response.get("balance", {})
        return {
            "balance": balance_data.get("balance", 0.0),
            "currency": balance_data.get("currency", "USD"),
            "account_id": balance_data.get("loginid"),
        }

    async def subscribe_ticks(self, symbol: str = "frxXAUUSD") -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"ticks": symbol})
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(response["error"].get("message", "Tick fetch failed"))
        return response

    # --- NEW: Fetch Historical Candles for the Strategy Engine ---
    async def get_candles(self, symbol: str = "frxXAUUSD", count: int = 200, granularity: int = 300) -> list:
        """
        granularity=300 means 5-minute candles.
        count=200 fetches enough history to calculate the 200 EMA.
        """
        if not self._authorized:
            await self.authenticate()
            
        await self.ws.send({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "style": "candles",
            "granularity": granularity
        })
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(response["error"].get("message", "Candle fetch failed"))
            
        return response.get("candles", [])

    async def get_statement(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"statement": 1, "description": 1, "limit": 50})
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(response["error"].get("message", "History fetch failed"))
        trades = []
        for tx in response.get("statement", {}).get("transactions", []):
            trades.append({
                "symbol": tx.get("display_name"),
                "pnl": tx.get("amount"),
                "time": tx.get("transaction_time"),
                "type": tx.get("action_type"),
                "contract_type": tx.get("contract_id")
            })
        return {"history": trades}

    async def place_order(self, contract_type: str, amount: float, duration: int, symbol: str = "frxXAUUSD") -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": duration,
            "duration_unit": "m",
            "symbol": symbol,
        })
        proposal = await self.ws.receive()
        if proposal.get("error"):
            raise Exception(proposal["error"].get("message", "Proposal failed"))
        proposal_id = proposal.get("proposal", {}).get("id")
        await self.ws.send({"buy": proposal_id, "price": amount})
        result = await self.ws.receive()
        if result.get("error"):
            raise Exception(result["error"].get("message", "Order failed"))
        return result

    async def close(self):
        if self.ws:
            await self.ws.close()
        self._authorized = False