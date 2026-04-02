"""
XAU Master Strategy — with push notifications wired in.
"""
import asyncio
import logging
import pandas as pd
from datetime import datetime, timezone

from ta.trend      import EMAIndicator, MACD
from ta.momentum   import RSIIndicator, StochasticOscillator
from ta.volatility import AverageTrueRange

from news.news_pipeline            import get_news_and_sentiment
from brokers.deriv_trading_service import DerivTradingService
import database as db
import notifications as notif

logger = logging.getLogger("XAUStrategy")

ANALYSIS_SYMBOL        = "frxXAUUSD"
TRADE_AMOUNT           = 10.0
MAX_SAFE_ATR           = 18.0
MIN_CONFLUENCE         = 5
CANDLE_COUNT           = 300
LOOP_INTERVAL          = 300
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_DRAWDOWN_PCT = 10.0
SESSIONS               = [(7, 12), (12, 17)]


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
        self._last_session_notif = None  # track so we don't spam session-open notifs

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
        """Fire session-open notification once per session, not every cycle."""
        today = datetime.now(timezone.utc).date()
        hour  = datetime.now(timezone.utc).hour
        key   = (today, hour // 5)   # changes every 5h so covers London + NY
        if self._last_session_notif != key and self._in_trading_session():
            self._last_session_notif = key
            notif.notify_session_start()

    # ── DB helpers ─────────────────────────────────────────────────────────────
    def save_signal(self, sig_type, price, rsi, bias, reason, confluence_score=0):
        try:
            db.insert_signal(
                ANALYSIS_SYMBOL, sig_type, price, rsi, bias,
                f"[{confluence_score}/7] {reason}", confluence_score,
            )
        except Exception as e:
            logger.error(f"Signal DB error: {e}")

    def save_trade_result(self, contract_id: str, won: bool, pnl: float):
        try:
            db.insert_trade_result(contract_id, won, pnl)
        except Exception as e:
            logger.error(f"Trade result DB error: {e}")

    # ── Candles ────────────────────────────────────────────────────────────────
    async def _get_candles(self, service, gran=300, count=300) -> pd.DataFrame:
        raw = await service.get_candles(ANALYSIS_SYMBOL, count=count, granularity=gran)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["volume"] = df.get("volume", pd.Series([1.0] * len(df))).astype(float)
        return df

    # ── Indicators ─────────────────────────────────────────────────────────────
    def _compute(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 210:
            return pd.DataFrame()
        c, h, l = df["close"], df["high"], df["low"]
        df["EMA_50"]  = EMAIndicator(close=c, window=50).ema_indicator()
        df["EMA_200"] = EMAIndicator(close=c, window=200).ema_indicator()
        df["RSI_14"]  = RSIIndicator(close=c, window=14).rsi()
        macd          = MACD(close=c, window_slow=26, window_fast=12, window_sign=9)
        df["MACD_H"]  = macd.macd_diff()
        stoch         = StochasticOscillator(high=h, low=l, close=c, window=14, smooth_window=3)
        df["STOCH_K"] = stoch.stoch()
        df["STOCH_D"] = stoch.stoch_signal()
        df["ATR_14"]  = AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range()
        vm = df["volume"].rolling(20).mean()
        vs = df["volume"].rolling(20).std()
        df["VOL_Z"] = (df["volume"] - vm) / (vs + 1e-9)
        df.dropna(inplace=True)
        return df

    async def _get_1h_bias(self, service) -> str:
        try:
            df = await self._get_candles(service, gran=3600, count=220)
            if df.empty or len(df) < 210:
                return "neutral"
            df["EMA_50"]  = EMAIndicator(close=df["close"], window=50).ema_indicator()
            df["EMA_200"] = EMAIndicator(close=df["close"], window=200).ema_indicator()
            df.dropna(inplace=True)
            row = df.iloc[-1]
            if row["EMA_50"] > row["EMA_200"]: return "bullish"
            if row["EMA_50"] < row["EMA_200"]: return "bearish"
            return "neutral"
        except Exception as e:
            logger.warning(f"1H bias error: {e}")
            return "neutral"

    def _score(self, row, sentiment):
        bull, bear = [], []
        price, ema50, ema200 = row["close"], row["EMA_50"], row["EMA_200"]
        rsi, macd_h = row["RSI_14"], row["MACD_H"]
        sk, sd      = row["STOCH_K"], row["STOCH_D"]
        vol_z       = row["VOL_Z"]

        (bull if price > ema200 else bear).append(
            "Price above EMA 200" if price > ema200 else "Price below EMA 200")
        (bull if price > ema50 else bear).append(
            "Price above EMA 50" if price > ema50 else "Price below EMA 50")
        if rsi < 35:   bull.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > 65: bear.append(f"RSI overbought ({rsi:.1f})")
        (bull if macd_h > 0 else bear).append(
            "MACD bullish" if macd_h > 0 else "MACD bearish")
        if sk < 25 and sk > sd:   bull.append(f"Stoch oversold+cross ({sk:.1f})")
        elif sk > 75 and sk < sd: bear.append(f"Stoch overbought+cross ({sk:.1f})")
        if vol_z > 1.0:
            (bull if len(bull) >= len(bear) else bear).append(
                f"Volume spike (z={vol_z:.1f})")
        if sentiment == "Bullish":   bull.append("News sentiment Bullish")
        elif sentiment == "Bearish": bear.append("News sentiment Bearish")

        return len(bull), len(bear), bull, bear

    # ── Contract monitor ───────────────────────────────────────────────────────
    async def _monitor_contract(self, service, contract_id: str, stake: float):
        try:
            logger.info(f"👁  Monitoring {contract_id}")
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

                # ── Push notification: settlement ──────────────────────────
                notif.notify_trade_settled(contract_id, won, profit)

                if won:
                    self._consecutive_losses = 0
                    logger.info(f"✅ WON | profit={profit:+.2f}")
                else:
                    self._consecutive_losses += 1
                    logger.warning(f"❌ LOST | streak={self._consecutive_losses}")

                # ── Circuit breaker ────────────────────────────────────────
                if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    self._circuit_broken = True
                    notif.notify_circuit_breaker(self._consecutive_losses)
                    logger.error("🚨 CIRCUIT BREAKER triggered")

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
        service = DerivTradingService()
        try:
            await service.authenticate()

            try:
                info = await service.get_account_info()
                self._maybe_reset_daily(info.get("balance", 0.0))
            except Exception:
                pass

            self._maybe_notify_session_start()

            now = datetime.now(timezone.utc)
            logger.info(
                f"[{now:%H:%M:%S}] 🤖 session={'✅' if self._in_trading_session() else '❌'} "
                f"circuit={'🚨' if self._circuit_broken else '✅'} "
                f"lock={'🔒' if self._trade_in_progress else '🔓'}"
            )

            if self._circuit_broken:
                self.save_signal("NEUTRAL", 0, 0, "N/A",
                    f"Circuit breaker — {self._consecutive_losses} losses. Resumes tomorrow.", 0)
                return

            if self._trade_in_progress:
                logger.info("🔒 Position open — skipping")
                return

            if not self._in_trading_session():
                logger.info(f"😴 Outside session (UTC {now.hour}:00)")
                return

            try:
                _, sentiment_data = get_news_and_sentiment()
                market_bias = sentiment_data.get("overall", "Neutral")
            except Exception as e:
                logger.warning(f"Sentiment error: {e}")
                market_bias = "Neutral"

            h1_bias = await self._get_1h_bias(service)
            logger.info(f"📈 1H: {h1_bias}")

            df = await self._get_candles(service)
            df = self._compute(df)
            if df.empty:
                self.save_signal("NEUTRAL", 0, 0, market_bias, "Collecting data...", 0)
                return

            row   = df.iloc[-1]
            price = row["close"]
            rsi   = row["RSI_14"]
            atr   = row["ATR_14"]

            logger.info(
                f"📊 Price={price:.2f} RSI={rsi:.1f} ATR={atr:.2f} "
                f"EMA50={row['EMA_50']:.2f} EMA200={row['EMA_200']:.2f} "
                f"Bias={market_bias}"
            )

            if atr > MAX_SAFE_ATR:
                self.save_signal("NEUTRAL", price, rsi, market_bias,
                    f"Extreme volatility ATR={atr:.2f}", 0)
                return

            bull_score, bear_score, bull_r, bear_r = self._score(row, market_bias)
            logger.info(f"🔢 BULL={bull_score}/7 BEAR={bear_score}/7")

            direction = None
            reasons   = []
            score     = 0

            if bull_score >= MIN_CONFLUENCE and bull_score > bear_score:
                if h1_bias in ("bullish", "neutral"):
                    direction, reasons, score = "CALL", bull_r, bull_score
                else:
                    self.save_signal("NEUTRAL", price, rsi, market_bias,
                        f"5M bullish ({bull_score}/7) blocked — 1H bearish", bull_score)
                    return

            elif bear_score >= MIN_CONFLUENCE and bear_score > bull_score:
                if h1_bias in ("bearish", "neutral"):
                    direction, reasons, score = "PUT", bear_r, bear_score
                else:
                    self.save_signal("NEUTRAL", price, rsi, market_bias,
                        f"5M bearish ({bear_score}/7) blocked — 1H bullish", bear_score)
                    return

            if direction:
                reason = " | ".join(reasons)
                signal = "BUY" if direction == "CALL" else "SELL"
                self.save_signal(signal, price, rsi, market_bias, reason, score)

                logger.info(f"{'🟢' if direction=='CALL' else '🔴'} {signal} [{score}/7]")
                self._trade_in_progress = True

                result      = await service.place_order(direction, self.trade_amount)
                contract_id = result.get("buy", {}).get("contract_id", "unknown")
                logger.info(f"✅ Placed | contract_id={contract_id}")

                # ── Push notification: trade executed ──────────────────────
                notif.notify_trade_executed(direction, ANALYSIS_SYMBOL,
                                            self.trade_amount, score)

                monitor_svc = DerivTradingService()
                await monitor_svc.authenticate()
                asyncio.create_task(
                    self._monitor_contract(monitor_svc, contract_id, self.trade_amount)
                )
            else:
                top     = max(bull_score, bear_score)
                leaning = "bullish" if bull_score > bear_score else "bearish"
                top_r   = bull_r if bull_score > bear_score else bear_r
                self.save_signal("NEUTRAL", price, rsi, market_bias,
                    f"Leaning {leaning} ({top}/7) — need {MIN_CONFLUENCE}. "
                    + " | ".join(top_r), top)

        except Exception as e:
            import traceback
            logger.error(f"❌ Strategy error: {e}")
            traceback.print_exc()
            self._trade_in_progress = False
        finally:
            await service.close()

    async def start_bot_loop(self):
        self.is_running = True
        logger.info("🚀 Bot loop started")
        while self.is_running:
            await self.execute_trade_cycle()
            if self.is_running:
                logger.info(f"⏱  Next scan in {LOOP_INTERVAL}s")
                await asyncio.sleep(LOOP_INTERVAL)
        logger.info("🛑 Bot loop stopped")