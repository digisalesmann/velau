"""
Deriv Trading Service — Python 3.14 compatible, Render-tuned timeouts.
"""
import asyncio
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

    # ── TRADE HISTORY ──────────────────────────────────────────────────────────
    SYMBOL_NAMES = {"frxXAUUSD": "Gold/USD"}
    PROFIT_TABLE_PAGE = 500
    PROFIT_TABLE_MAX_PAGES = 20   # 10,000 contracts: far beyond any real account

    async def get_statement(self) -> dict:
        """
        Every closed contract on the account, most recent first, from Deriv's
        profit_table (one row per contract), paged until exhausted.

        Replaces the old `statement` call with limit=100: that returned raw
        buy/sell transactions, two per contract, so it could never show more
        than ~50 trades. The dashboard's Total and Win Rate were computed from
        that window (49 of 105 trades on the demo account, 2026-10-22).
        """
        if not self._authorized:
            await self.authenticate()

        rows, offset = [], 0
        for _ in range(self.PROFIT_TABLE_MAX_PAGES):
            await self.ws.send({
                "profit_table": 1, "description": 1, "sort": "DESC",
                "limit": self.PROFIT_TABLE_PAGE, "offset": offset,
            })
            response = await self._wait_for("profit_table", timeout=30.0)
            if response.get("error"):
                raise Exception(response["error"].get("message", "Trade history fetch failed"))
            page = (response.get("profit_table") or {}).get("transactions", [])
            rows += page
            if len(page) < self.PROFIT_TABLE_PAGE:
                break
            offset += self.PROFIT_TABLE_PAGE

        trades = []
        for t in rows:
            buy_cost = float(t.get("buy_price") or 0)
            returned = float(t.get("sell_price") or 0)
            net_pnl  = returned - buy_cost
            shortcode = (t.get("shortcode") or t.get("contract_type") or "").upper()
            is_multiplier = shortcode.startswith("MULT")
            if shortcode.startswith("CALL"):
                direction = "CALL (BUY)"
            elif shortcode.startswith("PUT"):
                direction = "PUT (SELL)"
            elif shortcode.startswith("MULTUP"):
                direction = "BUY (Multiplier)"
            elif shortcode.startswith("MULTDOWN"):
                direction = "SELL (Multiplier)"
            else:
                direction = "Options"
            symbol = t.get("underlying_symbol") or ""
            trades.append({
                "symbol":        self.SYMBOL_NAMES.get(symbol, symbol or "Unknown"),
                "contract_id":   str(t.get("contract_id", "")),
                "type":          direction,
                "contract_type": "MULTIPLIER" if is_multiplier else "BINARY",
                "pnl":           round(net_pnl, 2),
                "buy_cost":      round(buy_cost, 2),
                "payout":        round(returned, 2),
                "time":          int(t.get("purchase_time") or 0),
                "sell_time":     int(t.get("sell_time") or 0),
                "won":           net_pnl > 0,
            })

        trades.sort(key=lambda x: x["time"], reverse=True)
        logger.info(f"Trade history: {len(trades)} closed contracts")
        return {"history": trades}

    # ── PLACE ORDER ────────────────────────────────────────────────────────────
    async def place_order(
        self,
        contract_type: str,
        amount: float,
        duration: int = CONTRACT_DURATION,
        duration_unit: str = CONTRACT_DURATION_UNIT,
        symbol: str = None,
        multiplier: int = None,
        stop_loss: float = None,
        take_profit: float = None,
    ) -> dict:
        """
        Rise/Fall (CALL/PUT): fixed-duration contract, `duration` applies.
        Multiplier (MULTUP/MULTDOWN): no expiry; `multiplier` is required and
        stop_loss/take_profit are net dollar P&L levels (commission included)
        that Deriv closes the contract at server-side.
        """
        if not self._authorized:
            await self.authenticate()

        exec_symbol = symbol or EXECUTION_SYMBOL
        is_multiplier = contract_type in ("MULTUP", "MULTDOWN")

        request = {
            "proposal":          1,
            "amount":            amount,
            "basis":             "stake",
            "contract_type":     contract_type,
            "currency":          "USD",
            "underlying_symbol": exec_symbol,
        }
        if is_multiplier:
            if not multiplier:
                raise ValueError("multiplier is required for MULTUP/MULTDOWN")
            request["multiplier"] = multiplier
            limit_order = {}
            if stop_loss is not None:
                limit_order["stop_loss"] = stop_loss
            if take_profit is not None:
                limit_order["take_profit"] = take_profit
            if limit_order:
                request["limit_order"] = limit_order
            logger.info(
                f"📤 Proposal | {contract_type} ${amount} x{multiplier} | "
                f"SL=${stop_loss} TP=${take_profit} | {exec_symbol}"
            )
        else:
            request["duration"]      = duration
            request["duration_unit"] = duration_unit
            logger.info(
                f"📤 Proposal | {contract_type} ${amount} | {duration}{duration_unit} | {exec_symbol}"
            )

        await self.ws.send(request)

        proposal = await self._wait_for("proposal", timeout=20.0)
        if proposal.get("error"):
            code = proposal["error"].get("code", "")
            msg  = proposal["error"].get("message", "Proposal failed")
            self._raise_trade_error(code, msg, "proposal")

        quote = proposal.get("proposal", {})
        proposal_id = quote.get("id")
        if not proposal_id:
            raise Exception(f"Proposal returned no ID: {proposal}")
        # ask_price/payout/commission may arrive as strings on the new API.
        # The payout actually quoted was never logged before, which is how a
        # 64.6% payout went unnoticed against a 75% assumption.
        if is_multiplier:
            logger.info(
                f"📋 Proposal OK: id={proposal_id} | ask=${quote.get('ask_price')} "
                f"commission=${quote.get('commission')}"
            )
        else:
            try:
                ratio = float(quote.get("payout")) / float(quote.get("ask_price")) - 1
                logger.info(f"📋 Proposal OK: id={proposal_id} | payout {ratio:.1%} of stake")
            except (TypeError, ValueError, ZeroDivisionError):
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

    # ── OPEN CONTRACTS ─────────────────────────────────────────────────────────
    @staticmethod
    def _contract_id(contract_id) -> int:
        """Deriv rejects a contract_id sent as a string ("Input validation
        failed: contract_id"), but trend_positions stores it as TEXT. Found
        live on 2026-10-22: the bot couldn't read, trail or close its first two
        positions until this cast. Always send the integer."""
        return int(str(contract_id).strip())

    async def get_contract(self, contract_id) -> dict:
        """Current state of one contract (proposal_open_contract, no subscription)."""
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"proposal_open_contract": 1, "contract_id": self._contract_id(contract_id)})
        msg = await self._wait_for("proposal_open_contract", timeout=20.0)
        if msg.get("error"):
            raise Exception(msg["error"].get("message", "proposal_open_contract failed"))
        return msg.get("proposal_open_contract") or {}

    async def get_open_contracts(self) -> list[dict]:
        """All currently open contracts on this account (portfolio)."""
        if not self._authorized:
            await self.authenticate()
        await self.ws.send({"portfolio": 1})
        msg = await self._wait_for("portfolio", timeout=20.0)
        if msg.get("error"):
            raise Exception(msg["error"].get("message", "portfolio failed"))
        return (msg.get("portfolio") or {}).get("contracts", [])

    async def sell_contract(self, contract_id, attempts: int = 5) -> dict:
        """Close an open contract at market (price 0 = accept any price).

        Deriv rejects a sell placed before the contract's entry tick with
        InvalidtoSell "Waiting for entry tick" (seen on a demo buy/sell on
        2026-10-19), so that one error is retried briefly."""
        if not self._authorized:
            await self.authenticate()
        for attempt in range(attempts):
            await self.ws.send({"sell": self._contract_id(contract_id), "price": 0})
            msg = await self._wait_for("sell", timeout=20.0)
            err = msg.get("error")
            if err and "entry tick" in err.get("message", "").lower() and attempt < attempts - 1:
                await asyncio.sleep(2)
                continue
            break
        if msg.get("error"):
            code = msg["error"].get("code", "")
            raise Exception(f"Sell failed [{code}]: {msg['error'].get('message', '')}")
        sold = msg.get("sell") or {}
        logger.info(f"💸 Sold {contract_id} for ${sold.get('sold_for')}")
        return sold

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