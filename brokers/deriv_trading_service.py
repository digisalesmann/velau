"""
Deriv Trading Service — fixed auth handshake, demo-compatible symbols,
correct duration units, and full error surfacing.
"""
import asyncio
import logging

from brokers.deriv_ws import DerivWebSocket
from env_config import DERIV_TOKEN

logger = logging.getLogger("DerivTradingService")

# ─── Demo account tradeable symbols ───────────────────────────────────────────
# frxXAUUSD is NOT available on demo. We use Volatility 100 (1s) Index which
# is always open, always liquid, and supports tick-based binary options on demo.
DEMO_EXECUTION_SYMBOL = "1HZ100V"   # Volatility 100 (1s) Index
TICK_DURATION         = 5           # 5 ticks — minimum supported
DURATION_UNIT         = "t"         # "t" = ticks (correct for synthetic indices)


class DerivTradingService:
    def __init__(self, token: str = None):
        self.token = token or DERIV_TOKEN
        self.ws = DerivWebSocket()
        self._authorized = False

    # ──────────────────────────────────────────────────────────────────────────
    # AUTH — robust frame-skipping loop
    # ──────────────────────────────────────────────────────────────────────────
    async def authenticate(self):
        await self.ws.connect()

        # Deriv sends a connection/ping frame immediately on cold connects.
        # Wait briefly so the handshake settles before we send auth.
        await asyncio.sleep(0.5)

        await self.ws.send({"authorize": self.token})

        # Drain up to 10 frames looking for the authorize response.
        # Skip ping, connection, and any echo frames gracefully.
        for attempt in range(10):
            try:
                raw = await self.ws.receive(timeout=10.0)
            except asyncio.TimeoutError:
                raise Exception(
                    "Deriv auth timed out — no response from server."
                )

            msg_type = raw.get("msg_type", "")
            error    = raw.get("error")

            # ── Got an error frame ──
            if error:
                code = error.get("code", "")
                msg  = error.get("message", "Unknown error")

                # WrongResponse on the first frame almost always means Deriv
                # sent us a stale ping/connection frame. Skip it and retry.
                if code == "WrongResponse" and attempt < 3:
                    logger.warning(
                        f"Skipping stale frame (attempt {attempt}): "
                        f"[{code}] {msg}"
                    )
                    continue

                logger.error(f"Auth error [{code}]: {msg}")
                raise Exception(f"Deriv auth failed [{code}]: {msg}")

            # ── Got the authorize response ──
            if msg_type == "authorize" or raw.get("authorize"):
                self._authorized = True
                loginid = raw.get("authorize", {}).get("loginid", "unknown")
                balance = raw.get("authorize", {}).get("balance", "?")
                currency = raw.get("authorize", {}).get("currency", "")
                logger.info(
                    f"✅ Auth OK | account={loginid} "
                    f"balance={balance} {currency}"
                )
                return raw

            # ── Any other frame (ping, website_status, etc.) — skip ──
            logger.warning(
                f"Skipping non-auth frame (attempt {attempt}): "
                f"msg_type={msg_type}"
            )

        raise Exception(
            "Deriv auth failed: no authorize response after 10 frames."
        )

    # ──────────────────────────────────────────────────────────────────────────
    # ACCOUNT
    # ──────────────────────────────────────────────────────────────────────────
    async def get_account_info(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"balance": 1})
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Balance fetch failed")
            )
        data = response.get("balance", {})
        return {
            "balance":    data.get("balance", 0.0),
            "currency":   data.get("currency", "USD"),
            "account_id": data.get("loginid"),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # MARKET DATA  (still fetches real XAU candles for analysis)
    # ──────────────────────────────────────────────────────────────────────────
    async def subscribe_ticks(self, symbol: str = "frxXAUUSD") -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"ticks": symbol, "subscribe": 1})
        response = await self.ws.receive()
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Tick subscribe failed")
            )
        return response

    async def get_candles(
        self,
        symbol: str = "frxXAUUSD",
        count: int = 250,
        granularity: int = 300,
    ) -> list:
        """Fetch OHLC candles. granularity=300 → 5-min candles."""
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count":       count,
            "end":         "latest",
            "style":       "candles",
            "granularity": granularity,
        })
        response = await self.ws.receive(timeout=20.0)
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Candle fetch failed")
            )
        return response.get("candles", [])

    async def get_available_symbols(self) -> list:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({
            "active_symbols": "brief",
            "product_type":   "basic",
        })
        response = await self.ws.receive(timeout=15.0)
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Symbol fetch failed")
            )
        symbols = response.get("active_symbols", [])
        relevant = [
            {
                "symbol":       s.get("symbol"),
                "display_name": s.get("display_name"),
                "is_open":      s.get("exchange_is_open"),
            }
            for s in symbols
            if any(
                x in s.get("symbol", "")
                for x in ["XAU", "frx", "R_", "1HZ", "BOOM", "CRASH"]
            )
        ]
        return relevant

    # ──────────────────────────────────────────────────────────────────────────
    # STATEMENT
    # ──────────────────────────────────────────────────────────────────────────
    async def get_statement(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"statement": 1, "description": 1, "limit": 50})
        response = await self.ws.receive(timeout=15.0)
        if response.get("error"):
            raise Exception(
                response["error"].get("message", "Statement fetch failed")
            )
        trades = []
        for tx in response.get("statement", {}).get("transactions", []):
            trades.append({
                "symbol":        tx.get("display_name"),
                "pnl":           tx.get("amount"),
                "time":          tx.get("transaction_time"),
                "type":          tx.get("action_type"),
                "contract_type": tx.get("contract_id"),
            })
        return {"history": trades}

    # ──────────────────────────────────────────────────────────────────────────
    # PLACE ORDER — always uses DEMO_EXECUTION_SYMBOL on demo
    # ──────────────────────────────────────────────────────────────────────────
    async def place_order(
        self,
        contract_type: str,
        amount: float,
        duration: int = TICK_DURATION,
        symbol: str = None,
    ) -> dict:
        if not self._authorized:
            await self.authenticate()

        # On demo we MUST use a synthetic index — override any XAU symbol
        exec_symbol = symbol or DEMO_EXECUTION_SYMBOL
        if "XAU" in exec_symbol or "frx" in exec_symbol:
            logger.warning(
                f"Symbol {exec_symbol} is not available on demo. "
                f"Switching to {DEMO_EXECUTION_SYMBOL}."
            )
            exec_symbol = DEMO_EXECUTION_SYMBOL

        logger.info(
            f"📤 Proposal | type={contract_type} amount={amount} "
            f"duration={TICK_DURATION}t symbol={exec_symbol}"
        )

        # ── Step 1: Request proposal ──────────────────────────────────────────
        await self.ws.send({
            "proposal":       1,
            "amount":         amount,
            "basis":          "stake",
            "contract_type":  contract_type,
            "currency":       "USD",
            "duration":       TICK_DURATION,
            "duration_unit":  DURATION_UNIT,
            "symbol":         exec_symbol,
        })

        # Drain frames until we get the proposal response
        proposal = await self._wait_for_msg_type("proposal", timeout=15.0)

        if proposal.get("error"):
            code = proposal["error"].get("code", "")
            msg  = proposal["error"].get("message", "Proposal failed")
            logger.error(f"Proposal error [{code}]: {msg}")
            self._raise_trade_error(code, msg, "proposal")

        proposal_id = proposal.get("proposal", {}).get("id")
        if not proposal_id:
            raise Exception(
                f"Proposal returned no ID. Full response: {proposal}"
            )
        logger.info(f"📋 Proposal accepted: id={proposal_id}")

        # ── Step 2: Buy the contract ──────────────────────────────────────────
        await self.ws.send({"buy": proposal_id, "price": amount})
        result = await self._wait_for_msg_type("buy", timeout=15.0)

        if result.get("error"):
            code = result["error"].get("code", "")
            msg  = result["error"].get("message", "Buy failed")
            logger.error(f"Buy error [{code}]: {msg}")
            self._raise_trade_error(code, msg, "buy")

        contract_id = result.get("buy", {}).get("contract_id")
        logger.info(f"✅ Order placed | contract_id={contract_id}")
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────────────
    async def _wait_for_msg_type(
        self, expected: str, timeout: float = 15.0, max_frames: int = 10
    ) -> dict:
        """
        Read frames until we find one matching `expected` msg_type,
        or until we hit an error frame, or run out of attempts.
        """
        for _ in range(max_frames):
            try:
                msg = await self.ws.receive(timeout=timeout)
            except asyncio.TimeoutError:
                raise Exception(
                    f"Timed out waiting for '{expected}' response."
                )
            msg_type = msg.get("msg_type", "")
            if msg_type == expected or msg.get(expected) or msg.get("error"):
                return msg
            logger.debug(f"Skipping frame: msg_type={msg_type}")
        raise Exception(
            f"No '{expected}' response received after {max_frames} frames."
        )

    @staticmethod
    def _raise_trade_error(code: str, msg: str, stage: str):
        if code == "PermissionDenied":
            raise Exception(
                "PermissionDenied: Your API token doesn't have Trade scope. "
                "Go to app.deriv.com → API Token → create a new token with "
                "Read + Trade enabled."
            )
        if code == "AuthorizationRequired":
            raise Exception(
                "AuthorizationRequired: Token expired or missing Trade scope. "
                "Regenerate your API token."
            )
        if code == "OfferingsValidationError":
            raise Exception(
                f"OfferingsValidationError at {stage}: The symbol or duration "
                "is not available. Check DEMO_EXECUTION_SYMBOL in "
                "deriv_trading_service.py."
            )
        raise Exception(f"Trade error [{code}] at {stage}: {msg}")

    async def close(self):
        if self.ws:
            await self.ws.close()
        self._authorized = False