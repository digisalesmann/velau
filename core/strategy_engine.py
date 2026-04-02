"""
XAU Master Strategy — production-grade engine.

Safety systems added:
  1. Position lock       — only one contract open at a time
  2. Circuit breaker     — stops after 3 consecutive losses OR 10% daily drawdown
  3. Contract monitor    — subscribes to settlement, logs win/loss in real time
  4. Session filter      — only trades London + NY sessions (best XAU liquidity)
  5. Multi-timeframe     — 1H trend must agree with 5M signal

Pillars (7):
  1. EMA 200  — long-term trend
  2. EMA 50   — short-term trend
  3. RSI 14   — momentum extremes
  4. MACD     — momentum shift
  5. Stoch    — secondary overbought/oversold
  6. Volume   — spike confirmation
  7. Sentiment— NLP news bias
"""

import asyncio
import sqlite3
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta

from ta.trend      import EMAIndicator, MACD
from ta.momentum   import RSIIndicator, StochasticOscillator
from ta.volatility import AverageTrueRange

from news.news_pipeline            import get_news_and_sentiment
from brokers.deriv_trading_service import DerivTradingService

logger = logging.getLogger("XAUStrategy")


# ── Constants ──────────────────────────────────────────────────────────────────
ANALYSIS_SYMBOL   = "frxXAUUSD"
TRADE_AMOUNT      = 10.0
MAX_SAFE_ATR      = 18.0
MIN_CONFLUENCE    = 5          # out of 7 pillars
CANDLE_COUNT      = 300
LOOP_INTERVAL     = 300        # 5 minutes
DB_PATH           = "/tmp/users.db"

# Circuit breaker
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_DRAWDOWN_PCT = 10.0  # % of opening balance

# Session filter — UTC hours (XAU moves most in these windows)
SESSIONS = [
    (7, 12),   # London: 07:00–12:00 UTC
    (12, 17),  # New York: 12:00–17:00 UTC
]


class XAUMasterStrategy:
    def __init__(self):
        self.is_running          = False
        self.db_path             = DB_PATH
        self.trade_amount        = TRADE_AMOUNT

        # Safety state
        self._trade_in_progress  = False   # position lock
        self._consecutive_losses = 0       # circuit breaker counter
        self._circuit_broken     = False   # circuit breaker flag
        self._daily_pnl          = 0.0     # running P&L today
        self._opening_balance    = None    # set on first auth of the day
        self._last_reset_date    = None    # date of last daily reset

    # ── DAILY RESET ────────────────────────────────────────────────────────────
    def _maybe_reset_daily(self, balance: float):
        today = datetime.now(timezone.utc).date()
        if self._last_reset_date != today:
            self._last_reset_date    = today
            self._opening_balance    = balance
            self._daily_pnl          = 0.0
            self._consecutive_losses = 0
            self._circuit_broken     = False
            logger.info(
                f"📅 Daily reset | opening_balance={balance} | "
                f"circuit_breaker=OFF"
            )

    # ── SESSION FILTER ─────────────────────────────────────────────────────────
    def _in_trading_session(self) -> bool:
        hour = datetime.now(timezone.utc).hour
        for start, end in SESSIONS:
            if start <= hour < end:
                return True
        return False

    # ── DATABASE ───────────────────────────────────────────────────────────────
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

    def save_trade_result(self, contract_id: str, won: bool, pnl: float):
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # Upsert into trade_results table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id TEXT UNIQUE,
                    won         INTEGER,
                    pnl         REAL,
                    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                """
                INSERT OR REPLACE INTO trade_results
                  (contract_id, won, pnl)
                VALUES (?, ?, ?)
                """,
                (contract_id, 1 if won else 0, round(pnl, 2)),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB trade_result write error: {e}")

    # ── CANDLE FETCH ───────────────────────────────────────────────────────────
    async def _get_candles(
        self, service: DerivTradingService, gran: int = 300, count: int = 300
    ) -> pd.DataFrame:
        raw = await service.get_candles(ANALYSIS_SYMBOL, count=count, granularity=gran)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["volume"] = df.get("volume", pd.Series([1.0] * len(df))).astype(float)
        return df

    # ── INDICATORS ─────────────────────────────────────────────────────────────
    def _compute(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 210:
            return pd.DataFrame()

        c, h, l = df["close"], df["high"], df["low"]

        df["EMA_50"]   = EMAIndicator(close=c, window=50).ema_indicator()
        df["EMA_200"]  = EMAIndicator(close=c, window=200).ema_indicator()
        df["RSI_14"]   = RSIIndicator(close=c, window=14).rsi()

        macd           = MACD(close=c, window_slow=26, window_fast=12, window_sign=9)
        df["MACD_H"]   = macd.macd_diff()

        stoch          = StochasticOscillator(high=h, low=l, close=c, window=14, smooth_window=3)
        df["STOCH_K"]  = stoch.stoch()
        df["STOCH_D"]  = stoch.stoch_signal()

        df["ATR_14"]   = AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range()

        vm             = df["volume"].rolling(20).mean()
        vs             = df["volume"].rolling(20).std()
        df["VOL_Z"]    = (df["volume"] - vm) / (vs + 1e-9)

        df.dropna(inplace=True)
        return df

    # ── MULTI-TIMEFRAME: 1H TREND ──────────────────────────────────────────────
    async def _get_1h_bias(self, service: DerivTradingService) -> str:
        """
        Returns 'bullish', 'bearish', or 'neutral' based on 1H EMA 50/200.
        Used as a higher-timeframe gate — 5M signals must align with 1H trend.
        """
        try:
            df = await self._get_candles(service, gran=3600, count=220)
            if df.empty or len(df) < 210:
                return "neutral"
            df["EMA_50"]  = EMAIndicator(close=df["close"], window=50).ema_indicator()
            df["EMA_200"] = EMAIndicator(close=df["close"], window=200).ema_indicator()
            df.dropna(inplace=True)
            row = df.iloc[-1]
            if row["EMA_50"] > row["EMA_200"]:
                return "bullish"
            elif row["EMA_50"] < row["EMA_200"]:
                return "bearish"
            return "neutral"
        except Exception as e:
            logger.warning(f"1H bias fetch failed: {e}")
            return "neutral"

    # ── CONFLUENCE SCORER ──────────────────────────────────────────────────────
    def _score(self, row: pd.Series, sentiment: str) -> tuple:
        bull, bear = [], []

        price, ema50, ema200 = row["close"], row["EMA_50"], row["EMA_200"]
        rsi    = row["RSI_14"]
        macd_h = row["MACD_H"]
        sk, sd = row["STOCH_K"], row["STOCH_D"]
        vol_z  = row["VOL_Z"]

        # Pillar 1 — EMA 200
        (bull if price > ema200 else bear).append("Price above EMA 200" if price > ema200 else "Price below EMA 200")

        # Pillar 2 — EMA 50
        (bull if price > ema50 else bear).append("Price above EMA 50" if price > ema50 else "Price below EMA 50")

        # Pillar 3 — RSI
        if rsi < 35:
            bull.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > 65:
            bear.append(f"RSI overbought ({rsi:.1f})")

        # Pillar 4 — MACD histogram
        (bull if macd_h > 0 else bear).append(
            "MACD bullish" if macd_h > 0 else "MACD bearish"
        )

        # Pillar 5 — Stochastic
        if sk < 25 and sk > sd:
            bull.append(f"Stoch oversold+crossover ({sk:.1f})")
        elif sk > 75 and sk < sd:
            bear.append(f"Stoch overbought+crossunder ({sk:.1f})")

        # Pillar 6 — Volume spike
        if vol_z > 1.0:
            (bull if len(bull) >= len(bear) else bear).append(
                f"Volume spike (z={vol_z:.1f})"
            )

        # Pillar 7 — Sentiment
        if sentiment == "Bullish":
            bull.append("News sentiment Bullish")
        elif sentiment == "Bearish":
            bear.append("News sentiment Bearish")

        return len(bull), len(bear), bull, bear

    # ── CONTRACT SETTLEMENT MONITOR ────────────────────────────────────────────
    async def _monitor_contract(
        self,
        service: DerivTradingService,
        contract_id: str,
        stake: float,
    ):
        """
        Subscribe to contract updates and wait for settlement.
        Updates circuit breaker and daily P&L when the contract closes.
        Runs as a background task — does not block the next scan cycle.
        """
        try:
            logger.info(f"👁  Monitoring contract {contract_id}...")
            await service.ws.send({
                "proposal_open_contracts": 1,
                "contract_id": contract_id,
                "subscribe": 1,
            })

            # Wait up to 120s for the contract to settle (5 ticks is fast)
            deadline = asyncio.get_event_loop().time() + 120

            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await service.ws.receive(timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Contract {contract_id} monitor timeout")
                    break

                poc = msg.get("proposal_open_contracts") or msg.get("poc")
                if not poc:
                    continue

                status = poc.get("status", "")
                if status not in ("won", "lost", "sold"):
                    continue

                # Contract settled
                profit = float(poc.get("profit", 0))
                won    = status == "won"

                self._daily_pnl += profit
                self.save_trade_result(contract_id, won, profit)

                if won:
                    self._consecutive_losses = 0
                    logger.info(
                        f"✅ Contract {contract_id} WON | "
                        f"profit={profit:+.2f} | "
                        f"daily_pnl={self._daily_pnl:+.2f}"
                    )
                else:
                    self._consecutive_losses += 1
                    logger.warning(
                        f"❌ Contract {contract_id} LOST | "
                        f"profit={profit:+.2f} | "
                        f"consecutive_losses={self._consecutive_losses}"
                    )

                # Check circuit breaker
                if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    self._circuit_broken = True
                    logger.error(
                        f"🚨 CIRCUIT BREAKER: {self._consecutive_losses} consecutive losses. "
                        f"Bot paused for the day."
                    )

                if self._opening_balance and self._opening_balance > 0:
                    drawdown_pct = abs(self._daily_pnl) / self._opening_balance * 100
                    if self._daily_pnl < 0 and drawdown_pct >= MAX_DAILY_DRAWDOWN_PCT:
                        self._circuit_broken = True
                        logger.error(
                            f"🚨 CIRCUIT BREAKER: Daily drawdown {drawdown_pct:.1f}% "
                            f">= {MAX_DAILY_DRAWDOWN_PCT}%. Bot paused for the day."
                        )
                break

        except Exception as e:
            logger.error(f"Contract monitor error: {e}")
        finally:
            self._trade_in_progress = False
            logger.info("🔓 Position lock released")

    # ── MAIN TRADE CYCLE ───────────────────────────────────────────────────────
    async def execute_trade_cycle(self):
        service = DerivTradingService()
        monitor_task = None

        try:
            await service.authenticate()

            # ── Daily reset ───────────────────────────────────────────────────
            try:
                info = await service.get_account_info()
                self._maybe_reset_daily(info.get("balance", 0.0))
            except Exception:
                pass

            now_utc = datetime.now(timezone.utc)
            logger.info(
                f"[{now_utc:%H:%M:%S}] 🤖 Scanning | "
                f"session={'✅' if self._in_trading_session() else '❌'} | "
                f"circuit={'🚨 BROKEN' if self._circuit_broken else '✅'} | "
                f"locked={'🔒' if self._trade_in_progress else '🔓'}"
            )

            # ── Safety gates ──────────────────────────────────────────────────
            if self._circuit_broken:
                self.save_signal(
                    "NEUTRAL", 0, 0, "N/A",
                    f"Circuit breaker active — "
                    f"{self._consecutive_losses} losses or drawdown limit hit. "
                    f"Resumes tomorrow.", 0
                )
                logger.warning("🚨 Circuit breaker active — skipping cycle")
                return

            if self._trade_in_progress:
                logger.info("🔒 Position open — skipping cycle")
                return

            if not self._in_trading_session():
                logger.info(
                    f"😴 Outside trading session (UTC {now_utc.hour}:00) — "
                    f"next window: London 07:00 or NY 12:00"
                )
                return

            # ── Sentiment ─────────────────────────────────────────────────────
            try:
                _, sentiment_data = get_news_and_sentiment()
                market_bias = sentiment_data.get("overall", "Neutral")
            except Exception as e:
                logger.warning(f"Sentiment failed: {e}")
                market_bias = "Neutral"

            # ── Multi-timeframe: 1H bias ──────────────────────────────────────
            h1_bias = await self._get_1h_bias(service)
            logger.info(f"📈 1H bias: {h1_bias}")

            # ── 5M technicals ─────────────────────────────────────────────────
            df = await self._get_candles(service, gran=300, count=300)
            df = self._compute(df)

            if df.empty:
                logger.warning("⏳ Not enough candle data")
                self.save_signal("NEUTRAL", 0, 0, market_bias,
                                 "Collecting market data...", 0)
                return

            row   = df.iloc[-1]
            price = row["close"]
            rsi   = row["RSI_14"]
            atr   = row["ATR_14"]

            logger.info(
                f"📊 Price={price:.2f} | RSI={rsi:.1f} | ATR={atr:.2f} | "
                f"EMA50={row['EMA_50']:.2f} | EMA200={row['EMA_200']:.2f} | "
                f"MACD_H={row['MACD_H']:.4f} | Bias={market_bias}"
            )

            # ── Volatility gate ───────────────────────────────────────────────
            if atr > MAX_SAFE_ATR:
                reason = f"Extreme volatility (ATR {atr:.2f} > {MAX_SAFE_ATR})"
                self.save_signal("NEUTRAL", price, rsi, market_bias, reason, 0)
                logger.warning(f"⚠️  {reason}")
                return

            # ── Confluence score ──────────────────────────────────────────────
            bull_score, bear_score, bull_r, bear_r = self._score(row, market_bias)
            logger.info(f"🔢 BULL={bull_score}/7 | BEAR={bear_score}/7")

            # ── Decision + 1H gate ───────────────────────────────────────────
            direction = None
            reasons   = []
            score     = 0

            if bull_score >= MIN_CONFLUENCE and bull_score > bear_score:
                if h1_bias in ("bullish", "neutral"):
                    direction = "CALL"
                    reasons   = bull_r
                    score     = bull_score
                else:
                    reason = (
                        f"5M bullish ({bull_score}/7) but 1H trend is bearish — "
                        "skipping to avoid counter-trend trade"
                    )
                    self.save_signal("NEUTRAL", price, rsi, market_bias, reason, bull_score)
                    logger.info(f"⏳ 1H gate blocked CALL: {reason}")
                    return

            elif bear_score >= MIN_CONFLUENCE and bear_score > bull_score:
                if h1_bias in ("bearish", "neutral"):
                    direction = "PUT"
                    reasons   = bear_r
                    score     = bear_score
                else:
                    reason = (
                        f"5M bearish ({bear_score}/7) but 1H trend is bullish — "
                        "skipping to avoid counter-trend trade"
                    )
                    self.save_signal("NEUTRAL", price, rsi, market_bias, reason, bear_score)
                    logger.info(f"⏳ 1H gate blocked PUT: {reason}")
                    return

            # ── Execute ───────────────────────────────────────────────────────
            if direction:
                reason = " | ".join(reasons)
                signal = "BUY" if direction == "CALL" else "SELL"
                self.save_signal(signal, price, rsi, market_bias, reason, score)

                emoji = "🟢" if direction == "CALL" else "🔴"
                logger.info(
                    f"{emoji} {signal} [{score}/7] — executing {direction}"
                )

                self._trade_in_progress = True
                logger.info("🔒 Position lock engaged")

                result      = await service.place_order(direction, self.trade_amount)
                contract_id = result.get("buy", {}).get("contract_id", "unknown")
                logger.info(f"✅ {direction} placed | contract_id={contract_id}")

                # Hand off monitoring to background task
                # We create a fresh service for monitoring so the main
                # service can be closed normally in the finally block
                monitor_service = DerivTradingService()
                await monitor_service.authenticate()
                monitor_task = asyncio.create_task(
                    self._monitor_contract(monitor_service, contract_id, self.trade_amount)
                )

            else:
                top      = max(bull_score, bear_score)
                leaning  = "bullish" if bull_score > bear_score else "bearish"
                top_r    = bull_r if bull_score > bear_score else bear_r
                reason   = (
                    f"Leaning {leaning} ({top}/7) — need {MIN_CONFLUENCE}. "
                    + " | ".join(top_r)
                )
                self.save_signal("NEUTRAL", price, rsi, market_bias, reason, top)
                logger.info(f"⏳ Neutral: {reason}")

        except Exception as e:
            import traceback
            logger.error(f"❌ Strategy error: {e}")
            traceback.print_exc()
            self._trade_in_progress = False  # always release on error
        finally:
            await service.close()
            # monitor_task runs independently — don't cancel it here

    # ── BOT LOOP ───────────────────────────────────────────────────────────────
    async def start_bot_loop(self):
        self.is_running = True
        logger.info("🚀 Bot loop started")
        while self.is_running:
            await self.execute_trade_cycle()
            if self.is_running:
                logger.info(f"⏱  Next scan in {LOOP_INTERVAL}s")
                await asyncio.sleep(LOOP_INTERVAL)
        logger.info("🛑 Bot loop stopped")