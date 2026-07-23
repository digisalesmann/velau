"""
Deriv Trading Service — Python 3.14 compatible, Render-tuned timeouts.
"""
import logging

from brokers.deriv_ws import DerivWebSocket
from env_config import DERIV_TOKEN

logger = logging.getLogger("DerivTradingService")

# Trade on the same asset we analyse so TA is relevant.
# Binary options on frxXAUUSD are available on both demo and live Deriv accounts.
EXECUTION_SYMBOL = "frxXAUUSD"
CONTRACT_DURATION      = 15   # minutes — matches the 15-minute analysis candles
CONTRACT_DURATION_UNIT = "m"


class DerivTradingService:
    def __init__(self, token: str = None, account_type: str = "real", max_retries: int = 7):
        self.token       = token or DERIV_TOKEN
        self.ws          = DerivWebSocket(token=self.token, account_type=account_type, max_retries=max_retries)
        self._authorized = False

    # ── AUTH ───────────────────────────────────────────────────────────────────
    async def authenticate(self):
        """
        The WebSocket URL itself is single-use and OTP-authenticated
        (see brokers/deriv_ws.py), so a successful connect() means the
        session is already authorized — no in-band {"authorize": token}
        handshake exists on the new API.
        """
        try:
            await self.ws.connect()
        except ConnectionError as e:
            raise Exception(
                f"Deriv auth failed: {e}. Check DERIV_TOKEN and DERIV_APP_ID env vars."
            )

        self._authorized = True
        info = self.ws.account_info or {}
        logger.info(
            f"✅ Auth OK | account={info.get('account_id', 'unknown')} "
            f"balance={info.get('balance', '?')} {info.get('currency', '')}"
        )
        return info

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
        end: str | int = "latest",
    ) -> list:
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({
            "ticks_history":    symbol,
            "adjust_start_time": 1,
            "count":            count,
            "end":              end,
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
        await self.ws.send({"active_symbols": "brief"})
        response = await self.ws.receive(timeout=20.0)
        if response.get("error"):
            raise Exception(response["error"].get("message", "Symbol fetch failed"))
        return [
            {
                "symbol":       s.get("underlying_symbol"),
                "display_name": s.get("underlying_symbol_name"),
                "is_open":      s.get("exchange_is_open"),
            }
            for s in response.get("active_symbols", [])
            if any(x in s.get("underlying_symbol", "") for x in ["XAU", "frx", "R_", "1HZ", "BOOM", "CRASH"])
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
        duration: int = CONTRACT_DURATION,
        duration_unit: str = CONTRACT_DURATION_UNIT,
        symbol: str = None,
    ) -> dict:
        if not self._authorized:
            await self.authenticate()

        exec_symbol = symbol or EXECUTION_SYMBOL

        logger.info(
            f"📤 Proposal | {contract_type} ${amount} | {duration}{duration_unit} | {exec_symbol}"
        )

        await self.ws.send({
            "proposal":         1,
            "amount":           amount,
            "basis":            "stake",
            "contract_type":    contract_type,
            "currency":         "USD",
            "duration":         duration,
            "duration_unit":    duration_unit,
            "underlying_symbol": exec_symbol,
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