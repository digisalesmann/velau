"""
XAU Master Strategy — SMC engine with tiered position sizing.

Position sizing tiers:
  $0-50:    10% per trade  (survival mode)
  $50-200:   5% per trade  (growth mode)
  $200-1000: 3% per trade  (compound mode)
  $1000+:    2% per trade, $50 cap (preservation mode)

Recovery mode: after any loss, drops one tier until 2 consecutive wins.

Confluence threshold: 5/7 (BOS fixed to only score on FRESH breaks)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import pandas as pd
from datetime import datetime, timezone

from ta.trend      import EMAIndicator, MACD
from ta.momentum   import RSIIndicator
from ta.volatility import AverageTrueRange

from news.news_pipeline            import get_news_and_sentiment
from brokers.deriv_trading_service import DerivTradingService
from position_sizing               import (
    calculate_stake, get_sizing_context, WINS_TO_EXIT_RECOVERY
)
import database as db
from core import notifications as notif

logger = logging.getLogger("XAUStrategy")

ANALYSIS_SYMBOL        = "frxXAUUSD"
MAX_SAFE_ATR           = 18.0
MIN_CONFLUENCE         = 5
LOOP_INTERVAL          = 300
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_DRAWDOWN_PCT = 10.0
SESSIONS               = [(7, 12), (12, 17)]
SWING_LOOKBACK         = 10
FVG_MIN_SIZE_PCT       = 0.02


class XAUMasterStrategy:
    def __init__(self):
        self.is_running          = False
        self._trade_in_progress  = False
        self._consecutive_losses = 0
        self._consecutive_wins   = 0
        self._in_recovery        = False
        self._circuit_broken     = False
        self._daily_pnl          = 0.0
        self._opening_balance    = None
        self._last_reset_date    = None
        self._last_session_notif = None
        self._current_balance    = 0.0
        self._win_rate           = 0.0

    # ── State management ───────────────────────────────────────────────────────
    def _maybe_reset_daily(self, balance: float):
        today = datetime.now(timezone.utc).date()
        if self._last_reset_date != today:
            self._last_reset_date    = today
            self._opening_balance    = balance
            self._daily_pnl          = 0.0
            self._consecutive_losses = 0
            self._circuit_broken     = False
            ctx = get_sizing_context(balance, self._win_rate)
            logger.info(
                f"📅 Daily reset | balance=${balance:.2f} | "
                f"tier={ctx['tier']} | stake=${ctx['normal_stake']:.2f}"
            )

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

    def _refresh_win_rate(self):
        try:
            stats = db.get_trade_stats()
            self._win_rate = stats["win_rate"] / 100.0
        except Exception:
            self._win_rate = 0.0

    def _get_stake(self) -> tuple[float, str]:
        stake, tier = calculate_stake(
            balance          = self._current_balance,
            win_rate         = self._win_rate,
            in_recovery      = self._in_recovery,
            consecutive_wins = self._consecutive_wins,
        )
        return stake, tier

    # ── DB ─────────────────────────────────────────────────────────────────────
    def save_signal(self, sig_type, price, rsi, bias, reason, score=0):
        try:
            db.insert_signal(
                ANALYSIS_SYMBOL, sig_type,
                float(price), float(rsi), str(bias),
                f"[{int(score)}/7] {reason}", int(score),
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
        logger.info(f"📥 Candles gran={gran}s count={count}")
        raw = await service.get_candles(ANALYSIS_SYMBOL, count=count, granularity=gran)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df.reset_index(drop=True, inplace=True)
        logger.info(f"✅ {len(df)} candles")
        return df

    def _compute_standard(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 210:
            logger.warning(f"Not enough candles: {len(df)}")
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
        logger.info(f"✅ Indicators on {len(df)} rows")
        return df

    # ── SMC detectors ──────────────────────────────────────────────────────────
    def _detect_fvg(self, df):
        if len(df) < 10:
            return "", "No FVG"
        price  = float(df["close"].iloc[-1])
        recent = df.tail(15).reset_index(drop=True)
        for i in range(2, len(recent)):
            c0, c2 = recent.iloc[i-2], recent.iloc[i]
            if c2["low"] > c0["high"]:
                gap = c2["low"] - c0["high"]
                if gap / price >= FVG_MIN_SIZE_PCT:
                    if c0["high"] <= price <= c2["low"]:
                        return "bull", f"Bullish FVG ({c0['high']:.2f}-{c2['low']:.2f})"
            if c2["high"] < c0["low"]:
                gap = c0["low"] - c2["high"]
                if gap / price >= FVG_MIN_SIZE_PCT:
                    if c2["high"] <= price <= c0["low"]:
                        return "bear", f"Bearish FVG ({c2['high']:.2f}-{c0['low']:.2f})"
        return "", "No FVG"

    def _detect_market_structure(self, df):
        """
        Only scores on a FRESH break of structure this candle.
        Prevents the 'above midpoint' bug that scored every cycle in a trend.
        """
        if len(df) < SWING_LOOKBACK + 5:
            return "", "No BOS"
        recent        = df.tail(SWING_LOOKBACK + 5).reset_index(drop=True)
        cur           = float(recent["close"].iloc[-1])
        prev          = float(recent["close"].iloc[-2])
        lookback      = recent.iloc[:-2]
        swing_high    = float(lookback["high"].max())
        swing_low     = float(lookback["low"].min())

        if cur > swing_high and prev <= swing_high:
            return "bull", f"BOS: broke {swing_high:.2f}"
        if cur < swing_low and prev >= swing_low:
            return "bear", f"BOS: broke {swing_low:.2f}"
        return "", "No fresh BOS"

    def _detect_liquidity_sweep(self, df):
        if len(df) < SWING_LOOKBACK + 3:
            return "", "No sweep"
        ref   = df.iloc[-(SWING_LOOKBACK + 3):-3]
        last3 = df.tail(3).reset_index(drop=True)
        if ref.empty:
            return "", "No sweep"
        swing_high = float(ref["high"].max())
        swing_low  = float(ref["low"].min())
        for i in range(len(last3)):
            c = last3.iloc[i]
            if c["low"] < swing_low and c["close"] > swing_low:
                return "bull", f"Liq sweep below {swing_low:.2f}"
            if c["high"] > swing_high and c["close"] < swing_high:
                return "bear", f"Liq sweep above {swing_high:.2f}"
        return "", "No sweep"

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
            logger.info(f"📈 1H: {bias}")
            return bias
        except Exception as e:
            logger.warning(f"1H bias: {e}")
            return "neutral"

    def _score(self, df, sentiment):
        bull, bear = [], []
        row    = df.iloc[-1]
        price  = float(row["close"])
        ema50  = float(row["EMA_50"])
        ema200 = float(row["EMA_200"])
        rsi    = float(row["RSI_14"])
        macd_h = float(row["MACD_H"])

        # 1. EMA 200
        (bull if price > ema200 else bear).append(
            "Above EMA 200" if price > ema200 else "Below EMA 200")

        # 2. EMA 50
        (bull if price > ema50 else bear).append(
            "Above EMA 50" if price > ema50 else "Below EMA 50")

        # 3. RSI — only at genuine extremes
        if rsi > 65:   bear.append(f"RSI overbought ({rsi:.1f})")
        elif rsi < 35: bull.append(f"RSI oversold ({rsi:.1f})")

        # 4. MACD
        (bull if macd_h > 0 else bear).append(
            f"MACD bull" if macd_h > 0 else "MACD bear")

        # 5. FVG
        fvg_dir, fvg_desc = self._detect_fvg(df)
        if fvg_dir == "bull":   bull.append(fvg_desc)
        elif fvg_dir == "bear": bear.append(fvg_desc)
        logger.info(f"🔲 FVG: {fvg_desc}")

        # 6. BOS — fresh breaks only
        bos_dir, bos_desc = self._detect_market_structure(df)
        if bos_dir == "bull":   bull.append(bos_desc)
        elif bos_dir == "bear": bear.append(bos_desc)
        logger.info(f"🏗  BOS: {bos_desc}")

        # 7. Sentiment
        if sentiment == "Bullish":   bull.append("Sentiment bullish")
        elif sentiment == "Bearish": bear.append("Sentiment bearish")

        return len(bull), len(bear), bull, bear

    # ── Contract monitor ───────────────────────────────────────────────────────
    async def _monitor_contract(self, service, contract_id, stake):
        try:
            logger.info(f"👁  Monitoring {contract_id} | stake=${stake:.2f}")
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
                    self._consecutive_losses  = 0
                    self._consecutive_wins   += 1
                    if self._in_recovery and \
                            self._consecutive_wins >= WINS_TO_EXIT_RECOVERY:
                        self._in_recovery = False
                        logger.info(
                            f"✅ Exiting recovery after "
                            f"{self._consecutive_wins} wins"
                        )
                    logger.info(
                        f"✅ WON +${profit:.2f} | "
                        f"streak=+{self._consecutive_wins} | "
                        f"daily={self._daily_pnl:+.2f}"
                    )
                else:
                    self._consecutive_wins    = 0
                    self._consecutive_losses += 1
                    self._in_recovery         = True
                    logger.warning(
                        f"❌ LOST -${stake:.2f} | "
                        f"streak=-{self._consecutive_losses} | "
                        f"→ recovery (need {WINS_TO_EXIT_RECOVERY} wins)"
                    )

                if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    self._circuit_broken = True
                    notif.notify_circuit_breaker(self._consecutive_losses)
                    logger.error(
                        f"🚨 CIRCUIT BREAKER — "
                        f"{self._consecutive_losses} consecutive losses"
                    )

                if self._opening_balance and self._opening_balance > 0:
                    dd = abs(self._daily_pnl) / self._opening_balance * 100
                    if self._daily_pnl < 0 and dd >= MAX_DAILY_DRAWDOWN_PCT:
                        self._circuit_broken = True
                        notif.notify_circuit_breaker(self._consecutive_losses)
                        logger.error(f"🚨 CIRCUIT BREAKER — drawdown {dd:.1f}%")
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
            await service.authenticate()

            try:
                info = await service.get_account_info()
                self._current_balance = float(info.get("balance", 0.0))
                self._maybe_reset_daily(self._current_balance)
                self._refresh_win_rate()
                logger.info(
                    f"💰 ${self._current_balance:.2f} | "
                    f"wr={self._win_rate:.1%} | "
                    f"recovery={'⚠️' if self._in_recovery else '✅'}"
                )
            except Exception as e:
                logger.warning(f"Balance: {e}")

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
                return

            if not self._in_trading_session():
                logger.info(f"😴 Outside session (UTC {now_utc.hour}:00)")
                return

            try:
                _, sentiment_data = get_news_and_sentiment()
                market_bias = sentiment_data.get("overall", "Neutral")
                logger.info(f"📰 {market_bias}")
            except Exception as e:
                logger.warning(f"Sentiment: {e}")
                market_bias = "Neutral"

            h1_bias = await self._get_1h_bias(service)

            df = await self._get_candles(service, gran=300, count=300)
            df = self._compute_standard(df)
            if df.empty:
                self.save_signal("NEUTRAL", 0, 0, market_bias,
                    "Collecting data...", 0)
                return

            row   = df.iloc[-1]
            price = float(row["close"])
            rsi   = float(row["RSI_14"])
            atr   = float(row["ATR_14"])

            logger.info(
                f"📊 Price={price:.2f} RSI={rsi:.1f} ATR={atr:.2f} "
                f"EMA50={float(row['EMA_50']):.2f} "
                f"EMA200={float(row['EMA_200']):.2f} "
                f"MACD={float(row['MACD_H']):.5f}"
            )

            if atr > MAX_SAFE_ATR:
                self.save_signal("NEUTRAL", price, rsi, market_bias,
                    f"ATR too high ({atr:.2f})", 0)
                return

            bull_score, bear_score, bull_r, bear_r = self._score(df, market_bias)
            logger.info(
                f"🔢 BULL={bull_score}/7 BEAR={bear_score}/7 "
                f"need {MIN_CONFLUENCE}"
            )

            direction, reasons, score = None, [], 0

            if bull_score >= MIN_CONFLUENCE and bull_score > bear_score:
                if h1_bias in ("bullish", "neutral"):
                    direction, reasons, score = "CALL", bull_r, bull_score
                    logger.info(f"🟢 CALL [{score}/7]")
                else:
                    self.save_signal("NEUTRAL", price, rsi, market_bias,
                        f"5M bull ({bull_score}/7) blocked — 1H bearish",
                        bull_score)
                    return

            elif bear_score >= MIN_CONFLUENCE and bear_score > bull_score:
                if h1_bias in ("bearish", "neutral"):
                    direction, reasons, score = "PUT", bear_r, bear_score
                    logger.info(f"🔴 PUT [{score}/7]")
                else:
                    self.save_signal("NEUTRAL", price, rsi, market_bias,
                        f"5M bear ({bear_score}/7) blocked — 1H bullish",
                        bear_score)
                    return

            if direction:
                stake, tier = self._get_stake()
                signal = "BUY" if direction == "CALL" else "SELL"
                self.save_signal(signal, price, rsi, market_bias,
                    " | ".join(reasons), score)

                logger.info(
                    f"🚀 {direction} | ${stake:.2f} "
                    f"({stake/self._current_balance*100:.1f}% | {tier}) | "
                    f"confluence={score}/7"
                )
                self._trade_in_progress = True

                result      = await service.place_order(direction, stake)
                contract_id = result.get("buy", {}).get("contract_id", "unknown")
                logger.info(f"✅ Placed | {contract_id}")

                notif.notify_trade_executed(
                    direction, ANALYSIS_SYMBOL, stake, score
                )

                monitor_svc = DerivTradingService()
                await monitor_svc.authenticate()
                asyncio.create_task(
                    self._monitor_contract(monitor_svc, contract_id, stake)
                )
            else:
                top     = max(bull_score, bear_score)
                leaning = "bullish" if bull_score >= bear_score else "bearish"
                top_r   = bull_r if bull_score >= bear_score else bear_r
                self.save_signal(
                    "NEUTRAL", price, rsi, market_bias,
                    f"Leaning {leaning} ({top}/7), need {MIN_CONFLUENCE}. "
                    + " | ".join(top_r), top
                )
                logger.info(f"⏳ {leaning} {top}/7")

        except Exception as e:
            logger.error(f"❌ Cycle error: {e}")
            import traceback as tb
            tb.print_exc()
            self._trade_in_progress = False
        finally:
            logger.info("━━━ Cycle end ━━━")
            await service.close()

    async def start_bot_loop(self):
        self.is_running = True
        logger.info("🚀 Bot loop active")
        while self.is_running:
            try:
                await self.execute_trade_cycle()
            except Exception as e:
                logger.error(f"Loop error: {e}")
            if self.is_running:
                logger.info(f"⏱  Next in {LOOP_INTERVAL}s")
                await asyncio.sleep(LOOP_INTERVAL)
        logger.info("🛑 Stopped")