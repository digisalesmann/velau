"""
Deriv trading service — production WebSocket-only implementation.
"""
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
        """Connect to Deriv WebSocket and authorize with API token."""
        await self.ws.connect()
        await self.ws.send({"authorize": self.token})
        response = await self.ws.receive()

        if response.get("error"):
            error_msg = response["error"].get("message", "Unknown auth error")
            logger.error(f"Deriv auth failed: {error_msg}")
            raise Exception(f"Deriv auth failed: {error_msg}")

        self._authorized = True
        logger.info(f"Deriv authenticated. Account: {response.get('authorize', {}).get('loginid')}")
        return response

    async def get_account_info(self) -> dict:
        """Get balance and account details."""
        if not self._authorized:
            await self.authenticate()

        # Balance request also simplified for validation safety
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
        """Get latest tick for a symbol (single snapshot)."""
        if not self._authorized:
            await self.authenticate()

        # FIX: Removed "subscribe": 0 to satisfy Deriv's strict validation
        await self.ws.send({"ticks": symbol})
        response = await self.ws.receive()

        if response.get("error"):
            raise Exception(response["error"].get("message", "Tick fetch failed"))

        return response

    async def place_order(self, contract_type: str, amount: float, duration: int, symbol: str = "frxXAUUSD") -> dict:
        """Buy a contract on Deriv."""
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