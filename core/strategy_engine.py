"""
XAU Master Strategy — production engine.
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
from . import notifications as notif

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
        df["volume"] = df.get("volume", pd.Series([1.0] * len(df))).astype(float)
        logger.info(f"✅ Got {len(df)} candles")
        return df

    # ── Indicators ─────────────────────────────────────────────────────────────
    def _compute(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 210:
            logger.warning(f"Not enough candles: {len(df)} (need 210)")
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
        logger.info(f"✅ Indicators computed on {len(df)} rows")
        return df

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
        if sk < 25 and sk > sd:   bull.append(f"Stoch oversold ({sk:.1f})")
        elif sk > 75 and sk < sd: bear.append(f"Stoch overbought ({sk:.1f})")
        if vol_z > 1.0:
            (bull if len(bull) >= len(bear) else bear).append(
                f"Volume spike (z={vol_z:.1f})")
        if sentiment == "Bullish":   bull.append("Sentiment bullish")
        elif sentiment == "Bearish": bear.append("Sentiment bearish")

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

            logger.info("📰 Fetching sentiment...")
            try:
                _, sentiment_data = get_news_and_sentiment()
                market_bias = sentiment_data.get("overall", "Neutral")
                logger.info(f"📰 Sentiment: {market_bias}")
            except Exception as e:
                logger.warning(f"Sentiment failed: {e}")
                market_bias = "Neutral"

            h1_bias = await self._get_1h_bias(service)

            df = await self._get_candles(service, gran=300, count=300)
            df = self._compute(df)
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

            if atr > MAX_SAFE_ATR:
                logger.warning(f"⚠️  ATR={atr:.2f} too high — skipping")
                self.save_signal("NEUTRAL", price, rsi, market_bias,
                    f"Volatility too high (ATR {atr:.2f})", 0)
                return

            bull_score, bear_score, bull_r, bear_r = self._score(row, market_bias)
            logger.info(f"🔢 BULL={bull_score}/7 | BEAR={bear_score}/7 | Need {MIN_CONFLUENCE}")

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
                notif.notify_trade_executed(direction, ANALYSIS_SYMBOL,
                                            self.trade_amount, score)
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