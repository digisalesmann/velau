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
        await asyncio.sleep(2.0)
        await self.ws.send({"authorize": self.token})

        for attempt in range(15):
            try:
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
        """
        Fetch transaction history for binary options account.

        Deriv's statement API returns different fields for binary options
        vs CFD trades. For binary options (tick contracts on 1HZ100V):

        Raw transaction fields:
          action_type      — "buy" or "sell" (sell = contract settled)
          amount           — net P&L (negative for buy cost, positive for payout)
          balance_after    — account balance after transaction
          contract_id      — the contract ID
          display_name     — symbol display name e.g. "Volatility 100 (1s) Index"
          purchase_time    — epoch timestamp of purchase
          transaction_time — epoch timestamp of this transaction
          shortcode        — e.g. "CALL_1HZ100V_10.00_1234567_5T"

        We pair buy+sell transactions by contract_id to show net P&L per trade.
        """
        if not self._authorized:
            await self.authenticate()

        await self.ws.send({
            "statement": 1,
            "description": 1,
            "limit": 100,          # fetch more to get full pairs
        })
        response = await self.ws.receive(timeout=20.0)
        if response.get("error"):
            raise Exception(response["error"].get("message", "Statement fetch failed"))

        raw_txns = response.get("statement", {}).get("transactions", [])
        logger.info(f"Raw statement: {len(raw_txns)} transactions")

        # ── Pair buy/sell transactions by contract_id ──────────────────────────
        # Each binary options trade creates TWO transactions:
        #   1. Buy  — negative amount (cost of contract e.g. -$10)
        #   2. Sell — positive amount (payout e.g. +$17.50 win, +$0 loss)
        # We combine them to show net P&L per contract.

        contracts: dict = {}

        for tx in raw_txns:
            contract_id  = str(tx.get("contract_id", ""))
            action       = tx.get("action_type", "")
            amount       = float(tx.get("amount", 0))
            symbol       = tx.get("display_name", "Volatility 100 (1s) Index")
            tx_time      = tx.get("transaction_time", 0)
            purchase_time= tx.get("purchase_time", tx_time)
            shortcode    = tx.get("shortcode", "")

            if not contract_id or contract_id == "None":
                continue

            if contract_id not in contracts:
                contracts[contract_id] = {
                    "contract_id":   contract_id,
                    "symbol":        symbol,
                    "buy_amount":    0.0,
                    "sell_amount":   0.0,
                    "time":          purchase_time or tx_time,
                    "shortcode":     shortcode,
                    "settled":       False,
                }

            if action == "buy":
                contracts[contract_id]["buy_amount"]  = amount   # negative
                contracts[contract_id]["time"]        = tx_time
            elif action == "sell":
                contracts[contract_id]["sell_amount"] = amount   # positive
                contracts[contract_id]["settled"]     = True

        # ── Build trade list ───────────────────────────────────────────────────
        trades = []
        for cid, c in contracts.items():
            buy_cost = abs(c["buy_amount"])   # e.g. 10.00
            payout   = c["sell_amount"]        # e.g. 17.50 or 0.0
            net_pnl  = payout - buy_cost       # e.g. +7.50 or -10.00

            # Determine direction from shortcode (CALL_... or PUT_...)
            shortcode = c["shortcode"].upper()
            if shortcode.startswith("CALL"):
                direction = "CALL (BUY)"
            elif shortcode.startswith("PUT"):
                direction = "PUT (SELL)"
            else:
                direction = "Options"

            # Only show settled contracts
            if not c["settled"] and payout == 0.0:
                continue

            trades.append({
                "symbol":        c["symbol"],
                "contract_id":   cid,
                "type":          direction,
                "contract_type": "BINARY",
                "pnl":           round(net_pnl, 2),
                "buy_cost":      round(buy_cost, 2),
                "payout":        round(payout, 2),
                "time":          c["time"],
                "won":           net_pnl > 0,
            })

        # Sort by time descending (most recent first)
        trades.sort(key=lambda x: x["time"], reverse=True)
        logger.info(f"Parsed {len(trades)} completed contracts from {len(contracts)} total")

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
            "PermissionDenied":         "Token lacks Trade scope. Regenerate at app.deriv.com → API Token.",
            "AuthorizationRequired":    "Token expired. Regenerate your API token.",
            "OfferingsValidationError": f"Symbol/duration not offered at {stage}. Check DEMO_EXECUTION_SYMBOL.",
        }
        raise Exception(hints.get(code, f"Trade error [{code}] at {stage}: {msg}"))

    async def close(self):
        if self.ws:
            await self.ws.close()
        self._authorized = False