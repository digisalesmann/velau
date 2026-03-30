import logging
import asyncio
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

        # Allow WebSocket handshake to fully settle before sending auth.
        # Deriv sometimes sends a ping/connection frame first on cold connects.
        await asyncio.sleep(1.0)

        await self.ws.send({"authorize": self.token})

        # Loop until we get the actual authorize response —
        # skip any ping, echo, or unrelated frames Deriv sends first.
        response = None
        for _ in range(5):
            raw = await self.ws.receive()
            msg_type = raw.get("msg_type", "")
            if msg_type == "authorize" or raw.get("authorize") or raw.get("error"):
                response = raw
                break
            logger.warning(f"Skipping non-auth frame: {raw}")

        if response is None:
            raise Exception(
                "Deriv auth failed: No authorize response received after 5 attempts."
            )

        if response.get("error"):
            error_msg = response["error"].get("message", "Unknown auth error")
            error_code = response["error"].get("code", "")
            logger.error(f"Deriv auth failed [{error_code}]: {error_msg}")
            raise Exception(f"Deriv auth failed [{error_code}]: {error_msg}")

        self._authorized = True
        loginid = response.get("authorize", {}).get("loginid", "unknown")
        logger.info(f"✅ Deriv auth success: {loginid}")
        return response

    async def get_account_info(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"balance": 1})
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Balance fetch failed")
            )
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
            raise Exception(
                response["error"].get("message", "Tick fetch failed")
            )
        return response

    async def get_candles(
        self,
        symbol: str = "frxXAUUSD",
        count: int = 250,
        granularity: int = 300,
    ) -> list:
        """granularity=300 = 5-minute candles."""
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "style": "candles",
            "granularity": granularity,
        })
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Candle fetch failed")
            )
        return response.get("candles", [])

    async def get_available_symbols(self) -> list:
        """Returns symbols tradeable on this account.
        Hit GET /symbols with a valid JWT to see what your account supports.
        """
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({
            "active_symbols": "brief",
            "product_type": "basic",
        })
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Symbol fetch failed")
            )
        symbols = response.get("active_symbols", [])
        relevant = [
            {
                "symbol": s.get("symbol"),
                "display_name": s.get("display_name"),
                "is_open": s.get("exchange_is_open"),
            }
            for s in symbols
            if "XAU" in s.get("symbol", "").upper()
            or "frx" in s.get("symbol", "")
            or "R_" in s.get("symbol", "")
            or "1HZ" in s.get("symbol", "")
        ]
        return relevant

    async def get_statement(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"statement": 1, "description": 1, "limit": 50})
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "History fetch failed")
            )
        trades = []
        for tx in response.get("statement", {}).get("transactions", []):
            trades.append({
                "symbol": tx.get("display_name"),
                "pnl": tx.get("amount"),
                "time": tx.get("transaction_time"),
                "type": tx.get("action_type"),
                "contract_type": tx.get("contract_id"),
            })
        return {"history": trades}

    async def place_order(
        self,
        contract_type: str,
        amount: float,
        duration: int,
        symbol: str = "frxXAUUSD",
    ) -> dict:
        if not self._authorized:
            await self.authenticate()

        # VRW demo accounts use tick-based duration.
        # "t" (ticks) is universally supported across all Deriv demo accounts.
        DURATION_UNIT = "t"
        TICK_DURATION = 5

        logger.info(
            f"📤 Sending proposal | type={contract_type} "
            f"amount={amount} duration={TICK_DURATION}t symbol={symbol}"
        )

        await self.ws.send({
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": TICK_DURATION,
            "duration_unit": DURATION_UNIT,
            "symbol": symbol,
        })
        proposal = await self.ws.receive()

        if proposal.get("error"):
            error_msg = proposal["error"].get("message", "Proposal failed")
            error_code = proposal["error"].get("code", "")
            logger.error(f"Proposal error [{error_code}]: {error_msg}")
            if error_code == "PermissionDenied":
                raise Exception(
                    "PermissionDenied: Your API token does not have permission "
                    "to trade binary options. Please generate a new token from "
                    "your Deriv Options account at app.deriv.com."
                )
            if error_code == "AuthorizationRequired":
                raise Exception(
                    "AuthorizationRequired: Token is invalid or expired. "
                    "Please reconnect your Deriv API in Account settings."
                )
            raise Exception(f"Proposal error [{error_code}]: {error_msg}")

        proposal_id = proposal.get("proposal", {}).get("id")
        if not proposal_id:
            raise Exception("Proposal returned no ID — cannot place order.")

        logger.info(f"📋 Proposal accepted: id={proposal_id}")

        await self.ws.send({"buy": proposal_id, "price": amount})
        result = await self.ws.receive()

        if result.get("error"):
            error_msg = result["error"].get("message", "Order failed")
            error_code = result["error"].get("code", "")
            logger.error(f"Buy error [{error_code}]: {error_msg}")
            if error_code == "PermissionDenied":
                raise Exception(
                    "PermissionDenied: Your Deriv account cannot place binary "
                    "option orders. Visit app.deriv.com → Trader's Hub → Options "
                    "to create an Options account and generate a new API token."
                )
            if error_code == "AuthorizationRequired":
                raise Exception(
                    "AuthorizationRequired: Token expired or missing Trade "
                    "scope. Regenerate your API token with Trade permission."
                )
            raise Exception(f"Order error [{error_code}]: {error_msg}")

        contract_id = result.get("buy", {}).get("contract_id")
        logger.info(f"✅ Order placed: contract_id={contract_id}")
        return result

    async def close(self):
        if self.ws:
            await self.ws.close()
        self._authorized = False