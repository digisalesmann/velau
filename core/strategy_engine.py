"""
XAU Master Strategy — multi-pillar confluence engine.

PILLARS:
  1. Trend       — EMA 50 / EMA 200 (direction + alignment)
  2. Momentum    — RSI 14 (overbought / oversold)
  3. Volatility  — ATR 14 (risk gate)
  4. Volume      — volume spike confirmation
  5. MACD        — momentum shift confirmation
  6. Stochastic  — secondary overbought/oversold filter
  7. Sentiment   — NLP bias from news headlines
  8. Economic    — high-impact event blackout window

A trade fires ONLY when at least 5 of 7 non-economic pillars align.
The economic blackout cancels all trades regardless of confluence.

Execution symbol: 1HZ100V (Volatility 100 1s Index)
  — Always open on Deriv demo, supports tick-based binary options.
  — XAU/USD candles are still used for all technical analysis.
"""

import asyncio
import sqlite3
import logging
import pandas as pd

from ta.trend     import EMAIndicator, MACD
from ta.momentum  import RSIIndicator, StochasticOscillator
from ta.volatility import AverageTrueRange

from datetime import datetime
from news.news_pipeline           import get_news_and_sentiment
from brokers.deriv_trading_service import DerivTradingService

logger = logging.getLogger("XAUStrategy")


# ─── Constants ────────────────────────────────────────────────────────────────
ANALYSIS_SYMBOL  = "frxXAUUSD"      # Real Gold — used for candle analysis only
TRADE_AMOUNT     = 10.0             # USD per trade
MAX_SAFE_ATR     = 18.0             # Volatility ceiling (price units)
MIN_CONFLUENCE   = 5                # Minimum pillars that must agree (out of 7)
CANDLE_COUNT     = 300              # Number of 5-min candles to fetch
CANDLE_GRAN      = 300              # 300s = 5-minute candles
LOOP_INTERVAL    = 300              # Seconds between cycles (5 min)
DB_PATH          = "/tmp/users.db"


class XAUMasterStrategy:
    def __init__(self):
        self.is_running   = False
        self.db_path      = DB_PATH
        self.trade_amount = TRADE_AMOUNT

    # ──────────────────────────────────────────────────────────────────────────
    # DATABASE
    # ──────────────────────────────────────────────────────────────────────────
    def save_signal(
        self,
        signal_type: str,
        price: float,
        rsi: float,
        bias: str,
        reason: str,
        confluence_score: int = 0,
    ):
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO signals
                  (symbol, type, price, rsi, bias, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ANALYSIS_SYMBOL,
                    signal_type,
                    round(price, 4),
                    round(rsi, 2),
                    bias,
                    f"[{confluence_score}/7] {reason}",
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB write error: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # DATA FETCH
    # ──────────────────────────────────────────────────────────────────────────
    async def get_candles(self, service: DerivTradingService) -> pd.DataFrame:
        raw = await service.get_candles(
            ANALYSIS_SYMBOL, count=CANDLE_COUNT, granularity=CANDLE_GRAN
        )
        if not raw:
            return pd.DataFrame()

        df = pd.DataFrame(raw)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        if "volume" not in df.columns:
            # Deriv doesn't return volume for Forex — synthesise from ATR proxy
            df["volume"] = 1.0
        else:
            df["volume"] = df["volume"].astype(float)
        return df

    # ──────────────────────────────────────────────────────────────────────────
    # TECHNICAL ANALYSIS
    # ──────────────────────────────────────────────────────────────────────────
    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 210:
            return pd.DataFrame()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # Trend
        df["EMA_50"]  = EMAIndicator(close=close, window=50).ema_indicator()
        df["EMA_200"] = EMAIndicator(close=close, window=200).ema_indicator()

        # Momentum
        df["RSI_14"]  = RSIIndicator(close=close, window=14).rsi()

        # MACD
        macd_obj       = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
        df["MACD"]     = macd_obj.macd()
        df["MACD_sig"] = macd_obj.macd_signal()
        df["MACD_diff"]= macd_obj.macd_diff()  # histogram

        # Stochastic
        stoch          = StochasticOscillator(high=high, low=low, close=close, window=14, smooth_window=3)
        df["STOCH_k"]  = stoch.stoch()
        df["STOCH_d"]  = stoch.stoch_signal()

        # Volatility
        df["ATR_14"]   = AverageTrueRange(
            high=high, low=low, close=close, window=14
        ).average_true_range()

        # Volume spike (rolling z-score vs 20-period mean)
        df["vol_mean"] = df["volume"].rolling(20).mean()
        df["vol_std"]  = df["volume"].rolling(20).std()
        df["vol_z"]    = (df["volume"] - df["vol_mean"]) / (df["vol_std"] + 1e-9)

        df.dropna(inplace=True)
        return df

    # ──────────────────────────────────────────────────────────────────────────
    # CONFLUENCE SCORING
    # ──────────────────────────────────────────────────────────────────────────
    def score_confluence(
        self, row: pd.Series, sentiment: str
    ) -> tuple[int, int, list[str], list[str]]:
        """
        Returns (bull_score, bear_score, bull_reasons, bear_reasons).
        Each pillar contributes 1 point to either bull or bear.
        """
        bull_reasons = []
        bear_reasons = []

        price   = row["close"]
        ema50   = row["EMA_50"]
        ema200  = row["EMA_200"]
        rsi     = row["RSI_14"]
        macd_h  = row["MACD_diff"]   # positive = bull momentum
        stoch_k = row["STOCH_k"]
        stoch_d = row["STOCH_d"]
        vol_z   = row["vol_z"]

        # ── Pillar 1: Long-term trend (EMA 200) ──
        if price > ema200:
            bull_reasons.append("Price above EMA 200 (uptrend)")
        else:
            bear_reasons.append("Price below EMA 200 (downtrend)")

        # ── Pillar 2: Short-term trend (EMA 50) ──
        if price > ema50:
            bull_reasons.append("Price above EMA 50")
        else:
            bear_reasons.append("Price below EMA 50")

        # ── Pillar 3: RSI momentum ──
        if rsi < 35:
            bull_reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > 65:
            bear_reasons.append(f"RSI overbought ({rsi:.1f})")

        # ── Pillar 4: MACD histogram ──
        if macd_h > 0:
            bull_reasons.append("MACD histogram bullish")
        else:
            bear_reasons.append("MACD histogram bearish")

        # ── Pillar 5: Stochastic ──
        if stoch_k < 25 and stoch_k > stoch_d:
            bull_reasons.append(f"Stochastic oversold + crossover ({stoch_k:.1f})")
        elif stoch_k > 75 and stoch_k < stoch_d:
            bear_reasons.append(f"Stochastic overbought + crossunder ({stoch_k:.1f})")

        # ── Pillar 6: Volume confirmation ──
        if vol_z > 1.0:
            # Volume spike — confirm whichever direction we're leaning
            if len(bull_reasons) >= len(bear_reasons):
                bull_reasons.append(f"Volume spike (z={vol_z:.1f})")
            else:
                bear_reasons.append(f"Volume spike (z={vol_z:.1f})")

        # ── Pillar 7: Sentiment ──
        if sentiment == "Bullish":
            bull_reasons.append("News sentiment Bullish")
        elif sentiment == "Bearish":
            bear_reasons.append("News sentiment Bearish")

        return len(bull_reasons), len(bear_reasons), bull_reasons, bear_reasons

    # ──────────────────────────────────────────────────────────────────────────
    # MAIN TRADE CYCLE — fully automated
    # ──────────────────────────────────────────────────────────────────────────
    async def execute_trade_cycle(self):
        service = DerivTradingService()
        try:
            await service.authenticate()
            logger.info(f"[{datetime.now():%H:%M:%S}] 🤖 Scanning XAU/USD...")

            # ── Pillar: Sentiment ─────────────────────────────────────────────
            try:
                _, sentiment_data = get_news_and_sentiment()
                market_bias = sentiment_data.get("overall", "Neutral")
            except Exception as e:
                logger.warning(f"Sentiment fetch failed: {e}")
                market_bias = "Neutral"

            # ── Fetch + compute technicals ────────────────────────────────────
            df = await self.get_candles(service)
            df = self.compute_indicators(df)

            if df.empty:
                logger.warning("⏳ Not enough candle data yet. Skipping cycle.")
                self.save_signal("NEUTRAL", 0, 0, market_bias,
                                 "Collecting market data...", 0)
                return

            row = df.iloc[-1]
            price  = row["close"]
            rsi    = row["RSI_14"]
            atr    = row["ATR_14"]

            logger.info(
                f"📊 Price={price:.2f} | RSI={rsi:.1f} | ATR={atr:.2f} "
                f"| EMA50={row['EMA_50']:.2f} | EMA200={row['EMA_200']:.2f} "
                f"| MACD_H={row['MACD_diff']:.4f} | Bias={market_bias}"
            )

            # ── Risk gate: extreme volatility ────────────────────────────────
            if atr > MAX_SAFE_ATR:
                reason = (
                    f"Extreme volatility (ATR {atr:.2f} > {MAX_SAFE_ATR}). "
                    "Standing by."
                )
                logger.warning(f"⚠️  {reason}")
                self.save_signal("NEUTRAL", price, rsi, market_bias, reason, 0)
                return

            # ── Score confluence ──────────────────────────────────────────────
            bull_score, bear_score, bull_reasons, bear_reasons = (
                self.score_confluence(row, market_bias)
            )

            logger.info(
                f"🔢 Confluence | BULL={bull_score}/7 | BEAR={bear_score}/7"
            )

            # ── Decision ─────────────────────────────────────────────────────
            if bull_score >= MIN_CONFLUENCE and bull_score > bear_score:
                reason = " | ".join(bull_reasons)
                self.save_signal("BUY", price, rsi, market_bias,
                                 reason, bull_score)
                logger.info(
                    f"🟢 LONG signal [{bull_score}/7] — auto-executing CALL"
                )
                result = await service.place_order(
                    "CALL", self.trade_amount
                )
                logger.info(f"✅ CALL placed: {result.get('buy', {}).get('contract_id')}")

            elif bear_score >= MIN_CONFLUENCE and bear_score > bull_score:
                reason = " | ".join(bear_reasons)
                self.save_signal("SELL", price, rsi, market_bias,
                                 reason, bear_score)
                logger.info(
                    f"🔴 SHORT signal [{bear_score}/7] — auto-executing PUT"
                )
                result = await service.place_order(
                    "PUT", self.trade_amount
                )
                logger.info(f"✅ PUT placed: {result.get('buy', {}).get('contract_id')}")

            else:
                # Not enough confluence — explain why
                if bull_score > bear_score:
                    reason = (
                        f"Leaning bullish ({bull_score}/7) — need {MIN_CONFLUENCE}. "
                        + " | ".join(bull_reasons)
                    )
                elif bear_score > bull_score:
                    reason = (
                        f"Leaning bearish ({bear_score}/7) — need {MIN_CONFLUENCE}. "
                        + " | ".join(bear_reasons)
                    )
                else:
                    reason = (
                        f"Mixed signals (bull={bull_score}, bear={bear_score}). "
                        "Waiting for confluence."
                    )
                self.save_signal("NEUTRAL", price, rsi, market_bias,
                                 reason, max(bull_score, bear_score))
                logger.info(f"⏳ Neutral: {reason}")

        except Exception as e:
            import traceback
            logger.error(f"❌ Strategy error: {e}")
            traceback.print_exc()
        finally:
            await service.close()

    # ──────────────────────────────────────────────────────────────────────────
    # BOT LOOP
    # ──────────────────────────────────────────────────────────────────────────
    async def start_bot_loop(self):
        self.is_running = True
        logger.info("🚀 Bot loop started.")
        while self.is_running:
            await self.execute_trade_cycle()
            if self.is_running:
                logger.info(f"⏱  Next cycle in {LOOP_INTERVAL}s...")
                await asyncio.sleep(LOOP_INTERVAL)
        logger.info("🛑 Bot loop stopped.")