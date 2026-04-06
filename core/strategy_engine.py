"""
XAU Master Strategy — SMC-enhanced engine.

Replaced unreliable pillars:
  ❌ Volume Z-Score  — Deriv returns no real volume for Forex (all 1.0 synthetic)
  ❌ Stochastic      — redundant with RSI, rarely triggers alongside other conditions

Added Smart Money Concepts pillars:
  ✅ Fair Value Gap (FVG)        — 3-candle imbalance, price in unfilled gap = entry signal
  ✅ Market Structure (BOS)      — Break of Structure confirms trend continuation
  ✅ Liquidity Sweep             — price grabs beyond recent swing high/low then reverses

New threshold: 4/7 (down from 5/7) — SMC signals are more specific so lower bar needed.

7 Pillars:
  1. EMA 200        — long-term trend direction
  2. EMA 50         — short-term trend direction
  3. RSI 14         — momentum extreme (>65 bear, <35 bull)
  4. MACD histogram — momentum shift direction
  5. FVG            — fair value gap imbalance present
  6. BOS/CHoCH      — market structure break confirms direction
  7. Sentiment      — NLP news bias
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from ta.trend      import EMAIndicator, MACD
from ta.momentum   import RSIIndicator
from ta.volatility import AverageTrueRange

from news.news_pipeline            import get_news_and_sentiment
from brokers.deriv_trading_service import DerivTradingService
import database as db
import notifications as notif

logger = logging.getLogger("XAUStrategy")

ANALYSIS_SYMBOL        = "frxXAUUSD"
TRADE_AMOUNT           = 10.0
MAX_SAFE_ATR           = 18.0
MIN_CONFLUENCE         = 4          # 4/7 — SMC signals are specific enough
CANDLE_COUNT           = 300
LOOP_INTERVAL          = 300
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_DRAWDOWN_PCT = 10.0
SESSIONS               = [(7, 12), (12, 17)]

# SMC parameters
SWING_LOOKBACK   = 10   # candles to look back for swing highs/lows
FVG_MIN_SIZE_PCT = 0.02  # minimum FVG size as % of price (filters noise)


class XAUMasterStrategy:
    def __init__(self):
        self.is_running          = False
        self.trade_amount        = TRADE_AMOUNT
        self._trade_in_progress  = False
        self._consecutive_losses = 0
        self._circuit_broken     = False
        self._daily_pnl          = 0.0
        self._opening_balance    = None
        self._last_reset_date    = None
        self._last_session_notif = None

    def _maybe_reset_daily(self, balance: float):
        today = datetime.now(timezone.utc).date()
        if self._last_reset_date != today:
            self._last_reset_date    = today
            self._opening_balance    = balance
            self._daily_pnl          = 0.0
            self._consecutive_losses = 0
            self._circuit_broken     = False
            logger.info(f"📅 Daily reset | opening_balance={balance}")

    def _in_trading_session(self) -> bool:
        hour = datetime.now(timezone.utc).hour
        return any(s <= hour < e for s, e in SESSIONS)

    def _maybe_notify_session_start(self):
        today = datetime.now(timezone.utc).date()
        hour  = datetime.now(timezone.utc).hour
        key   = (today, hour // 5)
        if self._last_session_notif != key and self._in_trading_session():
            self._last_session_notif = key
            notif.notify_session_start()

    # ── DB ─────────────────────────────────────────────────────────────────────
    def save_signal(self, sig_type, price, rsi, bias, reason, confluence_score=0):
        try:
            db.insert_signal(
                ANALYSIS_SYMBOL, sig_type,
                float(price), float(rsi),
                str(bias),
                f"[{int(confluence_score)}/7] {reason}",
                int(confluence_score),
            )
        except Exception as e:
            logger.error(f"Signal DB error: {e}")

    def save_trade_result(self, contract_id, won, pnl):
        try:
            db.insert_trade_result(str(contract_id), bool(won), float(pnl))
        except Exception as e:
            logger.error(f"Trade result DB error: {e}")

    # ── Candles ────────────────────────────────────────────────────────────────
    async def _get_candles(self, service, gran=300, count=300) -> pd.DataFrame:
        logger.info(f"📥 Fetching candles gran={gran}s count={count}")
        raw = await service.get_candles(ANALYSIS_SYMBOL, count=count, granularity=gran)
        if not raw:
            logger.warning("No candles returned")
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df.reset_index(drop=True, inplace=True)
        logger.info(f"✅ Got {len(df)} candles")
        return df

    # ── Standard indicators ────────────────────────────────────────────────────
    def _compute_standard(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 210:
            logger.warning(f"Not enough candles: {len(df)} (need 210)")
            return pd.DataFrame()
        c, h, l = df["close"], df["high"], df["low"]
        df["EMA_50"]  = EMAIndicator(close=c, window=50).ema_indicator()
        df["EMA_200"] = EMAIndicator(close=c, window=200).ema_indicator()
        df["RSI_14"]  = RSIIndicator(close=c, window=14).rsi()
        macd          = MACD(close=c, window_slow=26, window_fast=12, window_sign=9)
        df["MACD_H"]  = macd.macd_diff()
        df["ATR_14"]  = AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range()
        df.dropna(inplace=True)
        df.reset_index(drop=True, inplace=True)
        logger.info(f"✅ Standard indicators on {len(df)} rows")
        return df

    # ── SMC: Fair Value Gap ────────────────────────────────────────────────────
    def _detect_fvg(self, df: pd.DataFrame) -> tuple[str, str]:
        """
        A Fair Value Gap (FVG) is a 3-candle imbalance:
        - Bullish FVG: candle[i].low > candle[i-2].high (gap up, unfilled)
        - Bearish FVG: candle[i].high < candle[i-2].low (gap down, unfilled)

        We check the last 10 candles for an FVG where current price is
        inside the gap (meaning it could fill it = high probability entry).

        Returns: (direction, description) where direction is 'bull', 'bear', or ''
        """
        if len(df) < 10:
            return "", ""

        current_price = float(df["close"].iloc[-1])
        recent = df.tail(15).reset_index(drop=True)

        for i in range(2, len(recent)):
            c0 = recent.iloc[i-2]  # first candle
            c2 = recent.iloc[i]    # third candle

            # Bullish FVG: gap between c0.high and c2.low
            if c2["low"] > c0["high"]:
                gap_size = c2["low"] - c0["high"]
                if gap_size / current_price >= FVG_MIN_SIZE_PCT:
                    # Price retraced into the gap = high probability buy
                    if c0["high"] <= current_price <= c2["low"]:
                        return "bull", f"Bullish FVG ({c0['high']:.2f}-{c2['low']:.2f})"

            # Bearish FVG: gap between c2.high and c0.low
            if c2["high"] < c0["low"]:
                gap_size = c0["low"] - c2["high"]
                if gap_size / current_price >= FVG_MIN_SIZE_PCT:
                    # Price retraced into the gap = high probability sell
                    if c2["high"] <= current_price <= c0["low"]:
                        return "bear", f"Bearish FVG ({c2['high']:.2f}-{c0['low']:.2f})"

        return "", "No FVG"

    # ── SMC: Market Structure (BOS / CHoCH) ────────────────────────────────────
    def _detect_market_structure(self, df: pd.DataFrame) -> tuple[str, str]:
        """
        Break of Structure (BOS): price breaks above last swing high (bullish)
        or below last swing low (bearish), confirming trend continuation.

        Change of Character (CHoCH): price breaks the OPPOSITE swing,
        signalling a potential reversal — we treat this as a weaker signal.

        We use the last SWING_LOOKBACK candles to find swing highs/lows.
        Returns: ('bull'/'bear'/'', description)
        """
        if len(df) < SWING_LOOKBACK + 5:
            return "", ""

        recent = df.tail(SWING_LOOKBACK + 5).reset_index(drop=True)
        current_close = float(recent["close"].iloc[-1])
        prev_close    = float(recent["close"].iloc[-2])

        # Find swing high and swing low in the lookback window (excluding last 2 candles)
        lookback = recent.iloc[:-2]
        swing_high = float(lookback["high"].max())
        swing_low  = float(lookback["low"].min())

        # BOS bullish: current candle closes above the swing high
        if current_close > swing_high and prev_close <= swing_high:
            return "bull", f"BOS bullish (broke {swing_high:.2f})"

        # BOS bearish: current candle closes below the swing low
        if current_close < swing_low and prev_close >= swing_low:
            return "bear", f"BOS bearish (broke {swing_low:.2f})"

        # Trend continuation without fresh break — check if we're in the right zone
        # If price is above swing high zone, still bullish structure
        mid = (swing_high + swing_low) / 2
        if current_close > mid:
            return "bull", f"Bullish structure (above midpoint {mid:.2f})"
        else:
            return "bear", f"Bearish structure (below midpoint {mid:.2f})"

    # ── SMC: Liquidity Sweep ───────────────────────────────────────────────────
    def _detect_liquidity_sweep(self, df: pd.DataFrame) -> tuple[str, str]:
        """
        A liquidity sweep occurs when price briefly exceeds a recent swing
        high/low (grabbing stop losses) and then immediately reverses.

        Pattern:
        - Bullish sweep: candle wick went BELOW recent swing low but closed ABOVE it
          (swept buy-side liquidity, now reversing up)
        - Bearish sweep: candle wick went ABOVE recent swing high but closed BELOW it
          (swept sell-side liquidity, now reversing down)

        This is one of the strongest SMC entry signals.
        Returns: ('bull'/'bear'/'', description)
        """
        if len(df) < SWING_LOOKBACK + 3:
            return "", ""

        # Get the swing reference from candles before the last 3
        reference = df.iloc[-(SWING_LOOKBACK + 3):-3]
        last3      = df.tail(3).reset_index(drop=True)

        if reference.empty:
            return "", ""

        swing_high = float(reference["high"].max())
        swing_low  = float(reference["low"].min())

        # Check last 3 candles for sweep pattern
        for i in range(len(last3)):
            candle = last3.iloc[i]

            # Bullish sweep: wick below swing low, closed above it
            if candle["low"] < swing_low and candle["close"] > swing_low:
                return "bull", f"Liquidity sweep below {swing_low:.2f} (bullish reversal)"

            # Bearish sweep: wick above swing high, closed below it
            if candle["high"] > swing_high and candle["close"] < swing_high:
                return "bear", f"Liquidity sweep above {swing_high:.2f} (bearish reversal)"

        return "", "No sweep"

    # ── 1H bias ────────────────────────────────────────────────────────────────
    async def _get_1h_bias(self, service) -> str:
        try:
            df = await self._get_candles(service, gran=3600, count=220)
            if df.empty or len(df) < 210:
                return "neutral"
            df["EMA_50"]  = EMAIndicator(close=df["close"], window=50).ema_indicator()
            df["EMA_200"] = EMAIndicator(close=df["close"], window=200).ema_indicator()
            df.dropna(inplace=True)
            row  = df.iloc[-1]
            bias = "bullish" if row["EMA_50"] > row["EMA_200"] else \
                   "bearish" if row["EMA_50"] < row["EMA_200"] else "neutral"
            logger.info(f"📈 1H bias: {bias}")
            return bias
        except Exception as e:
            logger.warning(f"1H bias error: {e}")
            return "neutral"

    # ── Confluence scorer ──────────────────────────────────────────────────────
    def _score(self, df: pd.DataFrame, sentiment: str) -> tuple:
        """
        7 pillars — returns (bull_score, bear_score, bull_reasons, bear_reasons)

        Pillar 1: EMA 200 trend
        Pillar 2: EMA 50 trend
        Pillar 3: RSI extreme
        Pillar 4: MACD histogram
        Pillar 5: Fair Value Gap
        Pillar 6: Market Structure (BOS)
        Pillar 7: News Sentiment
        """
        bull, bear = [], []
        row = df.iloc[-1]

        price  = float(row["close"])
        ema50  = float(row["EMA_50"])
        ema200 = float(row["EMA_200"])
        rsi    = float(row["RSI_14"])
        macd_h = float(row["MACD_H"])

        # ── Pillar 1: EMA 200 ──────────────────────────────────────────────────
        if price > ema200:
            bull.append(f"Price above EMA 200 ({ema200:.2f})")
        else:
            bear.append(f"Price below EMA 200 ({ema200:.2f})")

        # ── Pillar 2: EMA 50 ───────────────────────────────────────────────────
        if price > ema50:
            bull.append(f"Price above EMA 50 ({ema50:.2f})")
        else:
            bear.append(f"Price below EMA 50 ({ema50:.2f})")

        # ── Pillar 3: RSI ──────────────────────────────────────────────────────
        if rsi > 65:
            bear.append(f"RSI overbought ({rsi:.1f})")
        elif rsi < 35:
            bull.append(f"RSI oversold ({rsi:.1f})")
        # 35-65 = neutral, neither side scores

        # ── Pillar 4: MACD histogram ───────────────────────────────────────────
        if macd_h > 0:
            bull.append(f"MACD bullish ({macd_h:.3f})")
        else:
            bear.append(f"MACD bearish ({macd_h:.3f})")

        # ── Pillar 5: Fair Value Gap ───────────────────────────────────────────
        fvg_dir, fvg_desc = self._detect_fvg(df)
        if fvg_dir == "bull":
            bull.append(fvg_desc)
        elif fvg_dir == "bear":
            bear.append(fvg_desc)
        logger.info(f"🔲 FVG: {fvg_desc}")

        # ── Pillar 6: Market Structure ─────────────────────────────────────────
        bos_dir, bos_desc = self._detect_market_structure(df)
        if bos_dir == "bull":
            bull.append(bos_desc)
        elif bos_dir == "bear":
            bear.append(bos_desc)
        logger.info(f"🏗  BOS: {bos_desc}")

        # ── Pillar 7: Sentiment ────────────────────────────────────────────────
        if sentiment == "Bullish":
            bull.append("News sentiment Bullish")
        elif sentiment == "Bearish":
            bear.append("News sentiment Bearish")

        return len(bull), len(bear), bull, bear

    # ── Contract monitor ───────────────────────────────────────────────────────
    async def _monitor_contract(self, service, contract_id, stake):
        try:
            logger.info(f"👁  Monitoring contract {contract_id}")
            await service.ws.send({
                "proposal_open_contracts": 1,
                "contract_id": contract_id,
                "subscribe": 1,
            })
            deadline = asyncio.get_event_loop().time() + 120
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await service.ws.receive(timeout=30.0)
                except TimeoutError:
                    break
                poc = msg.get("proposal_open_contracts") or msg.get("poc")
                if not poc:
                    continue
                status = poc.get("status", "")
                if status not in ("won", "lost", "sold"):
                    continue
                profit = float(poc.get("profit", 0))
                won    = status == "won"
                self._daily_pnl += profit
                self.save_trade_result(contract_id, won, profit)
                notif.notify_trade_settled(contract_id, won, profit)
                if won:
                    self._consecutive_losses = 0
                    logger.info(f"✅ WON | profit={profit:+.2f}")
                else:
                    self._consecutive_losses += 1
                    logger.warning(f"❌ LOST | streak={self._consecutive_losses}")
                if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    self._circuit_broken = True
                    notif.notify_circuit_breaker(self._consecutive_losses)
                    logger.error("🚨 CIRCUIT BREAKER: 3 losses")
                if self._opening_balance and self._opening_balance > 0:
                    dd = abs(self._daily_pnl) / self._opening_balance * 100
                    if self._daily_pnl < 0 and dd >= MAX_DAILY_DRAWDOWN_PCT:
                        self._circuit_broken = True
                        notif.notify_circuit_breaker(self._consecutive_losses)
                        logger.error(f"🚨 CIRCUIT BREAKER: drawdown {dd:.1f}%")
                break
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        finally:
            self._trade_in_progress = False
            logger.info("🔓 Position lock released")

    # ── Main cycle ─────────────────────────────────────────────────────────────
    async def execute_trade_cycle(self):
        now_utc = datetime.now(timezone.utc)
        logger.info(f"━━━ Cycle {now_utc:%H:%M:%S} UTC ━━━")
        service = DerivTradingService()
        try:
            logger.info("🔑 Authenticating...")
            await service.authenticate()
            logger.info("✅ Auth OK")

            try:
                info    = await service.get_account_info()
                balance = info.get("balance", 0.0)
                logger.info(f"💰 Balance: {balance} {info.get('currency','USD')}")
                self._maybe_reset_daily(balance)
            except Exception as e:
                logger.warning(f"Balance fetch failed: {e}")

            self._maybe_notify_session_start()

            logger.info(
                f"🔍 session={'✅' if self._in_trading_session() else '❌'} "
                f"circuit={'🚨' if self._circuit_broken else '✅'} "
                f"lock={'🔒' if self._trade_in_progress else '🔓'}"
            )

            if self._circuit_broken:
                self.save_signal("NEUTRAL", 0, 0, "N/A",
                    "Circuit breaker active. Resumes tomorrow.", 0)
                return

            if self._trade_in_progress:
                logger.info("🔒 Position open — skipping")
                return

            if not self._in_trading_session():
                logger.info(
                    f"😴 Outside session (UTC {now_utc.hour}:00) "
                    f"| London 07:00-12:00 | NY 12:00-17:00"
                )
                return

            # Sentiment
            logger.info("📰 Fetching sentiment...")
            try:
                _, sentiment_data = get_news_and_sentiment()
                market_bias = sentiment_data.get("overall", "Neutral")
                logger.info(f"📰 Sentiment: {market_bias}")
            except Exception as e:
                logger.warning(f"Sentiment failed: {e}")
                market_bias = "Neutral"

            # 1H bias
            h1_bias = await self._get_1h_bias(service)

            # 5M candles + indicators
            df = await self._get_candles(service, gran=300, count=300)
            df = self._compute_standard(df)
            if df.empty:
                self.save_signal("NEUTRAL", 0, 0, market_bias, "Collecting data...", 0)
                return

            row   = df.iloc[-1]
            price = float(row["close"])
            rsi   = float(row["RSI_14"])
            atr   = float(row["ATR_14"])

            logger.info(
                f"📊 Price={price:.2f} | RSI={rsi:.1f} | ATR={atr:.2f} | "
                f"EMA50={float(row['EMA_50']):.2f} | EMA200={float(row['EMA_200']):.2f} | "
                f"MACD={float(row['MACD_H']):.5f} | Bias={market_bias}"
            )

            # Volatility gate
            if atr > MAX_SAFE_ATR:
                logger.warning(f"⚠️  ATR={atr:.2f} too high — skipping")
                self.save_signal("NEUTRAL", price, rsi, market_bias,
                    f"Volatility too high (ATR {atr:.2f})", 0)
                return

            # Score confluence
            bull_score, bear_score, bull_r, bear_r = self._score(df, market_bias)
            logger.info(
                f"🔢 BULL={bull_score}/7 | BEAR={bear_score}/7 | "
                f"Need {MIN_CONFLUENCE} to trade"
            )

            direction = None
            reasons   = []
            score     = 0

            if bull_score >= MIN_CONFLUENCE and bull_score > bear_score:
                if h1_bias in ("bullish", "neutral"):
                    direction, reasons, score = "CALL", bull_r, bull_score
                    logger.info(f"🟢 CALL signal [{score}/7]")
                else:
                    self.save_signal("NEUTRAL", price, rsi, market_bias,
                        f"5M bullish ({bull_score}/7) blocked by 1H downtrend", bull_score)
                    logger.info("⏳ Blocked by 1H bearish")
                    return

            elif bear_score >= MIN_CONFLUENCE and bear_score > bull_score:
                if h1_bias in ("bearish", "neutral"):
                    direction, reasons, score = "PUT", bear_r, bear_score
                    logger.info(f"🔴 PUT signal [{score}/7]")
                else:
                    self.save_signal("NEUTRAL", price, rsi, market_bias,
                        f"5M bearish ({bear_score}/7) blocked by 1H uptrend", bear_score)
                    logger.info("⏳ Blocked by 1H bullish")
                    return

            if direction:
                reason = " | ".join(reasons)
                signal = "BUY" if direction == "CALL" else "SELL"
                self.save_signal(signal, price, rsi, market_bias, reason, score)
                logger.info(f"🚀 Executing {direction} | stake=${self.trade_amount}")
                self._trade_in_progress = True
                result      = await service.place_order(direction, self.trade_amount)
                contract_id = result.get("buy", {}).get("contract_id", "unknown")
                logger.info(f"✅ {direction} placed | contract_id={contract_id}")
                notif.notify_trade_executed(
                    direction, ANALYSIS_SYMBOL, self.trade_amount, score
                )
                monitor_svc = DerivTradingService()
                await monitor_svc.authenticate()
                asyncio.create_task(
                    self._monitor_contract(monitor_svc, contract_id, self.trade_amount)
                )
            else:
                top     = max(bull_score, bear_score)
                leaning = "bullish" if bull_score >= bear_score else "bearish"
                top_r   = bull_r if bull_score >= bear_score else bear_r
                reason  = (
                    f"Leaning {leaning} ({top}/7), need {MIN_CONFLUENCE}. "
                    + " | ".join(top_r)
                )
                self.save_signal("NEUTRAL", price, rsi, market_bias, reason, top)
                logger.info(f"⏳ No trade: {leaning} {top}/7")

        except Exception as e:
            logger.error(f"❌ Cycle error: {e}")
            import traceback as tb
            tb.print_exc()
            self._trade_in_progress = False
        finally:
            logger.info("━━━ Cycle end ━━━")
            await service.close()

    # ── Bot loop ───────────────────────────────────────────────────────────────
    async def start_bot_loop(self):
        self.is_running = True
        logger.info("🚀 Bot loop active")
        while self.is_running:
            try:
                await self.execute_trade_cycle()
            except Exception as e:
                logger.error(f"Loop-level error (continuing): {e}")
            if self.is_running:
                logger.info(f"⏱  Next cycle in {LOOP_INTERVAL}s")
                await asyncio.sleep(LOOP_INTERVAL)
        logger.info("🛑 Bot loop stopped")