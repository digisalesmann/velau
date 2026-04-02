"""
Deriv Trading Service — Python 3.14 compatible, Render-tuned timeouts.
"""
import asyncio
import logging

from brokers.deriv_ws import DerivWebSocket
from env_config import DERIV_TOKEN

logger = logging.getLogger("DerivTradingService")

DEMO_EXECUTION_SYMBOL = "1HZ100V"
TICK_DURATION         = 5
DURATION_UNIT         = "t"


class DerivTradingService:
    def __init__(self, token: str = None):
        self.token       = token or DERIV_TOKEN
        self.ws          = DerivWebSocket()
        self._authorized = False

    # ── AUTH ───────────────────────────────────────────────────────────────────
    async def authenticate(self):
        await self.ws.connect()

        # Let the TCP handshake fully settle — critical on Render cold starts
        await asyncio.sleep(2.0)

        await self.ws.send({"authorize": self.token})

        for attempt in range(15):
            try:
                # 20s timeout — Render's network can be slow on first frame
                raw = await self.ws.receive(timeout=20.0)
            except TimeoutError:
                raise Exception(
                    "Deriv auth timed out — no response after 20s. "
                    "Check DERIV_TOKEN and DERIV_APP_ID env vars on Render."
                )
            except asyncio.CancelledError:
                raise

            msg_type = raw.get("msg_type", "")
            error    = raw.get("error")

            if error:
                code = error.get("code", "")
                msg  = error.get("message", "Unknown error")
                # WrongResponse on early frames = stale ping frame — skip
                if code == "WrongResponse" and attempt < 5:
                    logger.warning(f"Skipping stale frame {attempt}: [{code}] {msg}")
                    continue
                logger.error(f"Auth error [{code}]: {msg}")
                raise Exception(f"Deriv auth failed [{code}]: {msg}")

            if msg_type == "authorize" or raw.get("authorize"):
                self._authorized = True
                info     = raw.get("authorize", {})
                loginid  = info.get("loginid", "unknown")
                balance  = info.get("balance", "?")
                currency = info.get("currency", "")
                logger.info(
                    f"✅ Auth OK | account={loginid} balance={balance} {currency}"
                )
                return raw

            logger.debug(f"Skipping non-auth frame {attempt}: {msg_type}")

        raise Exception("Deriv auth failed: no authorize response after 15 frames.")

    # ── ACCOUNT ────────────────────────────────────────────────────────────────
    async def get_account_info(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"balance": 1})
        response = await self.ws.receive(timeout=20.0)
        if response.get("error"):
            raise Exception(response["error"].get("message", "Balance fetch failed"))
        data = response.get("balance", {})
        return {
            "balance":    data.get("balance", 0.0),
            "currency":   data.get("currency", "USD"),
            "account_id": data.get("loginid"),
        }

    # ── MARKET DATA ────────────────────────────────────────────────────────────
    async def subscribe_ticks(self, symbol: str = "frxXAUUSD") -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"ticks": symbol, "subscribe": 1})
        response = await self.ws.receive(timeout=20.0)
        if response.get("error"):
            raise Exception(response["error"].get("message", "Tick subscribe failed"))
        return response

    async def get_candles(
        self,
        symbol: str = "frxXAUUSD",
        count: int = 250,
        granularity: int = 300,
    ) -> list:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({
            "ticks_history":    symbol,
            "adjust_start_time": 1,
            "count":            count,
            "end":              "latest",
            "style":            "candles",
            "granularity":      granularity,
        })
        response = await self.ws.receive(timeout=30.0)
        if response.get("error"):
            raise Exception(response["error"].get("message", "Candle fetch failed"))
        return response.get("candles", [])

    async def get_available_symbols(self) -> list:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"active_symbols": "brief", "product_type": "basic"})
        response = await self.ws.receive(timeout=20.0)
        if response.get("error"):
            raise Exception(response["error"].get("message", "Symbol fetch failed"))
        return [
            {
                "symbol":       s.get("symbol"),
                "display_name": s.get("display_name"),
                "is_open":      s.get("exchange_is_open"),
            }
            for s in response.get("active_symbols", [])
            if any(x in s.get("symbol", "") for x in ["XAU", "frx", "R_", "1HZ", "BOOM", "CRASH"])
        ]

    # ── STATEMENT ──────────────────────────────────────────────────────────────
    async def get_statement(self) -> dict:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"statement": 1, "description": 1, "limit": 50})
        response = await self.ws.receive(timeout=20.0)
        if response.get("error"):
            raise Exception(response["error"].get("message", "Statement fetch failed"))
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

    # ── PLACE ORDER ────────────────────────────────────────────────────────────
    async def place_order(
        self,
        contract_type: str,
        amount: float,
        duration: int = TICK_DURATION,
        symbol: str = None,
    ) -> dict:
        if not self._authorized:
            await self.authenticate()

        exec_symbol = symbol or DEMO_EXECUTION_SYMBOL
        if "XAU" in exec_symbol or "frx" in exec_symbol:
            logger.warning(
                f"Redirecting {exec_symbol} → {DEMO_EXECUTION_SYMBOL} (not on demo)"
            )
            exec_symbol = DEMO_EXECUTION_SYMBOL

        logger.info(
            f"📤 Proposal | {contract_type} ${amount} | {TICK_DURATION}t | {exec_symbol}"
        )

        await self.ws.send({
            "proposal":      1,
            "amount":        amount,
            "basis":         "stake",
            "contract_type": contract_type,
            "currency":      "USD",
            "duration":      TICK_DURATION,
            "duration_unit": DURATION_UNIT,
            "symbol":        exec_symbol,
        })

        proposal = await self._wait_for("proposal", timeout=20.0)
        if proposal.get("error"):
            code = proposal["error"].get("code", "")
            msg  = proposal["error"].get("message", "Proposal failed")
            self._raise_trade_error(code, msg, "proposal")

        proposal_id = proposal.get("proposal", {}).get("id")
        if not proposal_id:
            raise Exception(f"Proposal returned no ID: {proposal}")
        logger.info(f"📋 Proposal OK: id={proposal_id}")

        await self.ws.send({"buy": proposal_id, "price": amount})
        result = await self._wait_for("buy", timeout=20.0)
        if result.get("error"):
            code = result["error"].get("code", "")
            msg  = result["error"].get("message", "Buy failed")
            self._raise_trade_error(code, msg, "buy")

        contract_id = result.get("buy", {}).get("contract_id")
        logger.info(f"✅ Order placed | contract_id={contract_id}")
        return result

    # ── HELPERS ────────────────────────────────────────────────────────────────
    async def _wait_for(
        self, expected: str, timeout: float = 20.0, max_frames: int = 10
    ) -> dict:
        for _ in range(max_frames):
            try:
                msg = await self.ws.receive(timeout=timeout)
            except TimeoutError:
                raise Exception(f"Timed out waiting for '{expected}' response.")
            if msg.get("msg_type") == expected or msg.get(expected) or msg.get("error"):
                return msg
            logger.debug(f"Skipping frame: {msg.get('msg_type')}")
        raise Exception(f"No '{expected}' response after {max_frames} frames.")

    @staticmethod
    def _raise_trade_error(code: str, msg: str, stage: str):
        hints = {
            "PermissionDenied":        "Token lacks Trade scope. Regenerate at app.deriv.com → API Token.",
            "AuthorizationRequired":   "Token expired. Regenerate your API token.",
            "OfferingsValidationError": f"Symbol/duration not offered at {stage}. Check DEMO_EXECUTION_SYMBOL.",
        }
        raise Exception(hints.get(code, f"Trade error [{code}] at {stage}: {msg}"))

    async def close(self):
        if self.ws:
            await self.ws.close()
        self._authorized = False