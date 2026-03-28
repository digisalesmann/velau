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

        # Small delay — prevents Deriv's rate limiter from rejecting
        # rapid reconnects (common on Render's free tier cold starts)
        await asyncio.sleep(0.5)

        await self.ws.send({"authorize": self.token})
        response = await self.ws.receive()

        if response.get("error"):
            error_msg = response["error"].get("message", "Unknown auth error")
            error_code = response["error"].get("code", "")
            logger.error(f"Deriv auth failed [{error_code}]: {error_msg}")
            raise Exception(f"Deriv auth failed [{error_code}]: {error_msg}")

        self._authorized = True
        logger.info(f"✅ Deriv auth success: {response.get('authorize', {}).get('loginid')}")
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

        # --- FIXED: Deriv rejects durations outside their allowed range.
        # For frxXAUUSD binary options, minimum is 5 minutes.
        # Clamp to valid range: 5–60 minutes.
        VALID_MIN_DURATION = 5
        VALID_MAX_DURATION = 60
        safe_duration = max(VALID_MIN_DURATION, min(duration, VALID_MAX_DURATION))

        if safe_duration != duration:
            logger.warning(
                f"Duration {duration}m clamped to {safe_duration}m "
                f"(Deriv allows {VALID_MIN_DURATION}–{VALID_MAX_DURATION}m for {symbol})"
            )

        logger.info(
            f"📤 Sending proposal | type={contract_type} "
            f"amount={amount} duration={safe_duration}m symbol={symbol}"
        )

        await self.ws.send({
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": safe_duration,
            "duration_unit": "m",
            "symbol": symbol,
        })
        proposal = await self.ws.receive()

        if proposal.get("error"):
            error_msg = proposal["error"].get("message", "Proposal failed")
            error_code = proposal["error"].get("code", "")
            logger.error(f"Proposal error [{error_code}]: {error_msg}")
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
            raise Exception(f"Order error [{error_code}]: {error_msg}")

        contract_id = result.get("buy", {}).get("contract_id")
        logger.info(f"✅ Order placed successfully: contract_id={contract_id}")
        return result

    async def close(self):
        if self.ws:
            await self.ws.close()
        self._authorized = False