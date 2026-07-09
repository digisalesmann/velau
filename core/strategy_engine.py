"""
XAU/USD Master Strategy — multi-timeframe trend-following engine.

Timeframe stack:
  4H  candles → macro bias (bull/bear market)
  1H  candles → session trend direction
  15M candles → primary entry/exit signals

Confluence scoring (5/7 required):
  1. 4H EMA trend  — price vs EMA50 on 4H  [REQUIRED — must match direction]
  2. 1H EMA trend  — EMA50 vs EMA100 on 1H [REQUIRED — must match or be neutral]
  3. RSI momentum  — RSI14 > 55 (bull) / < 45 (bear) on 15M (conviction zone)
  4. MACD momentum — both histogram AND line above/below zero on 15M
  5. Bollinger Band — price pulls back below/above mid-band in trend direction
  6. ADX trend     — ADX > 25 + DI alignment confirms trending market
  7. BOS structure — fresh break of 20-candle swing high/low on 15M

Trade contract: frxXAUUSD CALL/PUT, 15-minute expiry (matches analysis candle).

Position sizing tiers:
  $0-50    → 10%   (survival)
  $50-200  →  5%   (growth)
  $200-1000→  3%   (compound)
  $1000+   →  2%, $50 cap (preservation)

Break-even win rate at 75% payout: 57.1%
Target: 60%+
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import pandas as pd
from datetime import datetime, timezone

from ta.trend      import EMAIndicator, MACD, ADXIndicator
from ta.momentum   import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from brokers.deriv_trading_service import DerivTradingService
from position_sizing               import (
    calculate_stake, WINS_TO_EXIT_RECOVERY
)
import database as db
from core import notifications as notif

logger = logging.getLogger("XAUStrategy")

ANALYSIS_SYMBOL        = "frxXAUUSD"
MIN_CONFLUENCE         = 5          # 5/7 — high-quality signals only
LOOP_INTERVAL          = 300        # 5-minute cycles
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_DRAWDOWN_PCT = 10.0
SESSIONS               = [(8, 17)]  # Skip volatile London open hour; NY close at 17
SWING_LOOKBACK         = 20         # 5-hour structure look-back on 15M candles
MAX_SAFE_ATR           = 20.0       # filter extreme volatility spikes
ADX_TREND_THRESHOLD    = 25         # industry standard: trending market threshold


class XAUMasterStrategy:
    def __init__(self):
        self.is_running          = False
        self._trade_in_progress  = False
        self._consecutive_wins   = 0
        self._in_recovery        = False
        self._last_session_notif = None
        self._current_balance    = 0.0
        self._win_rate           = 0.0
        self._sentiment_cache: dict = {}
        self._sentiment_cache_time: datetime | None = None

    def _in_trading_session(self) -> bool:
        hour = datetime.now(timezone.utc).hour
        return any(s <= hour < e for s, e in SESSIONS)

    def _get_cached_sentiment(self) -> dict:
        """Refresh news sentiment at most once every 30 minutes."""
        now = datetime.now(timezone.utc)
        age = (now - self._sentiment_cache_time).total_seconds() if self._sentiment_cache_time else 9999
        if age > 1800:
            try:
                from news.news_pipeline import get_news_and_sentiment
                _, s = get_news_and_sentiment()
                self._sentiment_cache      = s
                self._sentiment_cache_time = now
                logger.info(f"📰 Sentiment: {s['overall']} ({s['score']:+.3f}) | articles={s['articles_analyzed']}")
            except Exception as e:
                logger.warning(f"Sentiment refresh failed: {e}")
        return self._sentiment_cache

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

    # ── DB helpers ─────────────────────────────────────────────────────────────
    def save_signal(self, sig_type, price, rsi, bias, reason, score=0):
        try:
            db.insert_signal(
                ANALYSIS_SYMBOL, sig_type,
                float(price), float(rsi), str(bias),
                f"[{int(score)}/7] {reason}", int(score),
            )
        except Exception as e:
            logger.error(f"Signal DB error: {e}")

    def save_trade_result(self, contract_id, won, pnl, username: str = None):
        try:
            db.insert_trade_result(str(contract_id), bool(won), float(pnl),
                                   username=username or None)
        except Exception as e:
            logger.error(f"Trade result DB error: {e}")

    # ── Candle fetching ────────────────────────────────────────────────────────
    async def _get_candles(self, service, gran=900, count=250) -> pd.DataFrame:
        logger.info(f"📥 Candles gran={gran}s count={count}")
        raw = await service.get_candles(ANALYSIS_SYMBOL, count=count, granularity=gran)
        if not raw:
            return pd.DataFrame()

        # For 15M only: fetch a second batch ending just before the first one starts
        # so we get ~280 rows instead of Deriv's ~139 cap.
        if gran == 900 and len(raw) > 0:
            oldest_epoch = raw[0]['epoch']
            try:
                raw2 = await service.get_candles(
                    ANALYSIS_SYMBOL, count=count, granularity=gran,
                    end=oldest_epoch - gran,
                )
                if raw2:
                    raw = raw2 + raw
                    logger.info(f"🔗 Stitched: {len(raw2)}+{len(raw) - len(raw2)} candles")
            except Exception as e:
                logger.warning(f"Candle stitch batch 2 failed (using single batch): {e}")

        df = pd.DataFrame(raw)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df.drop_duplicates(subset=["epoch"], keep="last", inplace=True)
        df.sort_values("epoch", inplace=True)
        df.reset_index(drop=True, inplace=True)
        logger.info(f"✅ {len(df)} candles (gran={gran}s)")
        return df

    # ── Indicator computation ──────────────────────────────────────────────────
    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 110:
            logger.warning(f"Not enough candles: {len(df)}")
            return pd.DataFrame()
        c, h, l = df["close"], df["high"], df["low"]

        # Trend — EMA100 used (Deriv caps 15M history at ~139 rows)
        df["EMA_50"]  = EMAIndicator(close=c, window=50).ema_indicator()
        df["EMA_100"] = EMAIndicator(close=c, window=100).ema_indicator()

        # Momentum
        df["RSI_14"]  = RSIIndicator(close=c, window=14).rsi()
        macd          = MACD(close=c, window_slow=26, window_fast=12, window_sign=9)
        df["MACD_H"]  = macd.macd_diff()
        df["MACD_L"]  = macd.macd()      # MACD line (for zero-cross)

        # Volatility / range
        df["ATR_14"]  = AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range()
        bb = BollingerBands(close=c, window=20, window_dev=2)
        df["BB_MID"]  = bb.bollinger_mavg()
        df["BB_UP"]   = bb.bollinger_hband()
        df["BB_LO"]   = bb.bollinger_lband()
        df["BB_PCT"]  = bb.bollinger_pband()   # 0=lower band, 1=upper band

        # Trend strength
        adx = ADXIndicator(high=h, low=l, close=c, window=14)
        df["ADX"]     = adx.adx()
        df["DI_PLUS"] = adx.adx_pos()
        df["DI_MINUS"]= adx.adx_neg()

        df.dropna(inplace=True)
        df.reset_index(drop=True, inplace=True)
        logger.info(f"✅ Indicators on {len(df)} rows")
        return df

    # ── Multi-timeframe bias ───────────────────────────────────────────────────
    async def _get_4h_bias(self, service) -> str:
        """Macro trend: price vs EMA50 on 4H candles (Deriv returns ~155 4H bars)."""
        try:
            df = await self._get_candles(service, gran=14400, count=220)
            if df.empty or len(df) < 60:
                return "neutral"
            df["EMA_50"] = EMAIndicator(close=df["close"], window=50).ema_indicator()
            df.dropna(inplace=True)
            row  = df.iloc[-1]
            bias = "bullish" if row["close"] > row["EMA_50"] else "bearish"
            logger.info(f"📈 4H macro bias: {bias} | price={row['close']:.2f} EMA50={row['EMA_50']:.2f}")
            return bias
        except Exception as e:
            logger.warning(f"4H bias error: {e}")
            return "neutral"

    async def _get_1h_bias(self, service) -> str:
        """Session trend: EMA50 vs EMA100 on 1H candles (Deriv returns ~162 1H bars)."""
        try:
            df = await self._get_candles(service, gran=3600, count=220)
            if df.empty or len(df) < 110:
                return "neutral"
            df["EMA_50"]  = EMAIndicator(close=df["close"], window=50).ema_indicator()
            df["EMA_100"] = EMAIndicator(close=df["close"], window=100).ema_indicator()
            df.dropna(inplace=True)
            row  = df.iloc[-1]
            bias = "bullish" if row["EMA_50"] > row["EMA_100"] else \
                   "bearish" if row["EMA_50"] < row["EMA_100"] else "neutral"
            logger.info(f"📈 1H session bias: {bias} | EMA50={row['EMA_50']:.2f} EMA100={row['EMA_100']:.2f}")
            return bias
        except Exception as e:
            logger.warning(f"1H bias error: {e}")
            return "neutral"

    # ── Structure detection ────────────────────────────────────────────────────
    def _detect_bos(self, df):
        """Fresh break of structure on current 15M candle only."""
        if len(df) < SWING_LOOKBACK + 5:
            return "", "No BOS"
        recent     = df.tail(SWING_LOOKBACK + 5).reset_index(drop=True)
        cur        = float(recent["close"].iloc[-1])
        prev       = float(recent["close"].iloc[-2])
        lookback   = recent.iloc[:-2]
        swing_high = float(lookback["high"].max())
        swing_low  = float(lookback["low"].min())
        if cur > swing_high and prev <= swing_high:
            return "bull", f"BOS broke {swing_high:.2f}"
        if cur < swing_low and prev >= swing_low:
            return "bear", f"BOS broke {swing_low:.2f}"
        return "", "No fresh BOS"

    # ── Confluence scorer ──────────────────────────────────────────────────────
    def _score(self, df: pd.DataFrame, h1_bias: str, h4_bias: str) -> tuple:
        bull_r, bear_r = [], []
        row     = df.iloc[-1]
        price   = float(row["close"])
        ema50   = float(row["EMA_50"])
        ema100  = float(row["EMA_100"])
        rsi     = float(row["RSI_14"])
        macd_h  = float(row["MACD_H"])
        macd_l  = float(row["MACD_L"])
        adx     = float(row["ADX"])
        di_plus = float(row["DI_PLUS"])
        di_minus= float(row["DI_MINUS"])
        bb_pct  = float(row["BB_PCT"])
        bb_mid  = float(row["BB_MID"])

        logger.info(
            f"📊 Price={price:.2f} RSI={rsi:.1f} ADX={adx:.1f} "
            f"EMA50={ema50:.2f} EMA100={ema100:.2f} "
            f"MACD_H={macd_h:.4f} BB%={bb_pct:.2f}"
        )

        # 1. 4H macro bias (strong trend filter)
        if h4_bias == "bullish":
            bull_r.append(f"4H macro bullish (price > EMA50)")
        elif h4_bias == "bearish":
            bear_r.append(f"4H macro bearish (price < EMA50)")

        # 2. 1H session trend (EMA50 vs EMA100 cross)
        if h1_bias == "bullish":
            bull_r.append("1H EMA50 > EMA100 (uptrend)")
        elif h1_bias == "bearish":
            bear_r.append("1H EMA50 < EMA100 (downtrend)")

        # 3. RSI conviction zone — > 55 bullish, < 45 bearish (50-line is noise)
        if rsi > 55:
            bull_r.append(f"RSI {rsi:.1f} > 55 (bullish conviction)")
        elif rsi < 45:
            bear_r.append(f"RSI {rsi:.1f} < 45 (bearish conviction)")

        # 4. MACD — only score when both histogram AND signal line agree on same side of zero
        # Weak "histogram rising but below zero" signals removed — too much noise
        if macd_h > 0 and macd_l > 0:
            bull_r.append(f"MACD above zero, histogram positive")
        elif macd_h < 0 and macd_l < 0:
            bear_r.append(f"MACD below zero, histogram negative")

        # 5. Bollinger Band pullback-to-midband in trend direction only
        # BB extremes (oversold/overbought) removed — catch falling knives in binary options
        if bb_pct < 0.45 and h1_bias == "bullish":
            bull_r.append(f"Price below BB mid in uptrend (pullback entry)")
        elif bb_pct > 0.55 and h1_bias == "bearish":
            bear_r.append(f"Price above BB mid in downtrend (pullback entry)")

        # 6. ADX + DI direction — trend strength
        if adx > ADX_TREND_THRESHOLD:
            if di_plus > di_minus:
                bull_r.append(f"ADX {adx:.1f} trending, +DI > -DI (bullish pressure)")
            elif di_minus > di_plus:
                bear_r.append(f"ADX {adx:.1f} trending, -DI > +DI (bearish pressure)")
        else:
            logger.info(f"⚠️  ADX {adx:.1f} < {ADX_TREND_THRESHOLD} (choppy market, ADX factor neutral)")

        # 7. Fresh BOS structure break
        bos_dir, bos_desc = self._detect_bos(df)
        if bos_dir == "bull":
            bull_r.append(bos_desc)
        elif bos_dir == "bear":
            bear_r.append(bos_desc)
        logger.info(f"🏗  BOS: {bos_desc}")

        return len(bull_r), len(bear_r), bull_r, bear_r

    # ── Contract monitor ───────────────────────────────────────────────────────
    async def _monitor_contract(self, token: str, account_type: str, contract_id, stake, username: str = ""):
        """
        Poll Deriv every 90 s with a fresh connection until the contract settles.
        Polling is used instead of a long-lived subscription because Render's
        network drops WebSocket connections within seconds, making subscriptions
        unreliable and causing the position lock to be released prematurely.
        """
        logger.info(f"👁  Monitoring {contract_id} | stake=${stake:.2f}")
        deadline = asyncio.get_running_loop().time() + 1200  # 20-min cap
        poll_interval = 90  # seconds between polls — contract lasts 15 min

        try:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(poll_interval)

                svc = DerivTradingService(token=token, account_type=account_type)
                try:
                    await svc.authenticate()
                    await svc.ws.send({
                        "proposal_open_contracts": 1,
                        "contract_id": contract_id,
                    })
                    msg = await svc.ws.receive(timeout=20.0)
                except Exception as poll_err:
                    logger.warning(f"👁  Poll error (will retry): {poll_err}")
                    continue
                finally:
                    await svc.close()

                poc = msg.get("proposal_open_contracts") or msg.get("poc")
                if not poc:
                    logger.debug(f"👁  No poc in response, retrying…")
                    continue

                status = poc.get("status", "")
                logger.info(f"👁  Contract {contract_id} status={status!r}")

                if status not in ("won", "lost", "sold"):
                    continue  # still open — poll again

                profit = float(poc.get("profit", 0))
                won    = status == "won"
                self.save_trade_result(contract_id, won, profit, username=username)

                state = db.record_trade_settlement(
                    username, won, profit, MAX_CONSECUTIVE_LOSSES, MAX_DAILY_DRAWDOWN_PCT
                )

                if username:
                    notif.notify_user(
                        username,
                        f"Trade {'Won' if won else 'Lost'}  {'+' if won else '-'}${abs(profit):.2f}",
                        f"Contract {contract_id} settled.",
                        {"type": "trade_settled", "won": str(won), "pnl": str(profit)},
                    )
                else:
                    notif.notify_trade_settled(contract_id, won, profit)

                # Global stake-sizing counters — explicitly out of scope for
                # per-user isolation, unchanged logic.
                if won:
                    self._consecutive_wins += 1
                    if self._in_recovery and \
                            self._consecutive_wins >= WINS_TO_EXIT_RECOVERY:
                        self._in_recovery = False
                        logger.info(f"✅ Exiting recovery after {self._consecutive_wins} wins")
                    logger.info(
                        f"✅ [{username}] WON +${profit:.2f} | streak=+{self._consecutive_wins} | "
                        f"user_daily={state['daily_pnl']:+.2f}"
                    )
                else:
                    self._consecutive_wins = 0
                    self._in_recovery       = True
                    logger.warning(
                        f"❌ [{username}] LOST -${stake:.2f} | user_streak=-{state['consecutive_losses']} | "
                        f"→ recovery (need {WINS_TO_EXIT_RECOVERY} wins)"
                    )

                if state["newly_tripped"]:
                    logger.error(
                        f"🚨 [{username}] CIRCUIT BREAKER tripped | "
                        f"losses={state['consecutive_losses']} | daily={state['daily_pnl']:+.2f}"
                    )
                    body = (
                        f"{state['consecutive_losses']} consecutive losses or daily drawdown "
                        f"limit hit. Your bot has paused for today, resumes at midnight UTC."
                    )
                    if username:
                        notif.notify_user(
                            username, "Circuit Breaker Triggered", body,
                            {"type": "circuit_breaker"},
                        )
                    else:
                        notif.notify_circuit_breaker(state["consecutive_losses"])
                return  # settled — done

            logger.warning(f"👁  Contract {contract_id} did not settle within 20 min")

        except Exception as e:
            logger.error(f"Monitor fatal error: {e}")
        finally:
            self._trade_in_progress = False
            logger.info("🔓 Position lock released")

    # ── Main trade cycle ───────────────────────────────────────────────────────
    def _get_all_users(self) -> list[dict]:
        """
        Returns all users with a connected Deriv token.
        Falls back to a synthetic entry using DERIV_TOKEN env var.
        """
        from env_config import DERIV_TOKEN
        users = db.get_all_users_with_tokens()
        if users:
            return users
        if DERIV_TOKEN:
            return [{"username": "_server", "deriv_token": DERIV_TOKEN, "trade_account_type": "real"}]
        return []

    async def _place_for_user(self, username: str, token: str, account_type: str,
                               direction: str, score: int) -> bool:
        """Place a trade. Returns True if a monitor task was started, False on failure."""
        svc = DerivTradingService(token=token, account_type=account_type)
        try:
            await svc.authenticate()
            info    = await svc.get_account_info()
            balance = float(info.get("balance", 0.0))
            db.maybe_set_opening_balance(username, balance)
            stake, tier = calculate_stake(
                balance          = balance,
                win_rate         = self._win_rate,
                in_recovery      = self._in_recovery,
                consecutive_wins = self._consecutive_wins,
            )
            result      = await svc.place_order(direction, stake)
            contract_id = result.get("buy", {}).get("contract_id", "unknown")
            logger.info(f"✅ [{username}] {direction} ${stake:.2f} | contract={contract_id}")

            notif.notify_user(
                username,
                f"Trade Placed {'CALL ↑' if direction == 'CALL' else 'PUT ↓'}",
                f"XAU/USD  |  Stake ${stake:.2f}  |  Confluence {score}/7",
                {"type": "trade_executed", "direction": direction},
            )

            # Pass the token — monitor opens its own fresh connections per poll
            asyncio.create_task(
                self._monitor_contract(token, account_type, contract_id, stake, username)
            )
            return True
        except Exception as e:
            logger.error(f"❌ [{username}] trade failed: {e}")
            return False
        finally:
            await svc.close()

    async def _get_market_data_service(self, all_users: list[dict]):
        """
        Find one working account to use for cycle-wide market data.
        Candles/bias are symbol-level, not per-user, so any authenticated
        account works — scans in order with a short retry budget per
        account (2 instead of the default 7) so a single stale/broken
        token burns seconds, not ~2 minutes of the 5-minute cycle, before
        moving on to the next one.
        """
        for u in all_users:
            service = DerivTradingService(
                token=u["deriv_token"],
                account_type=u.get("trade_account_type", "real"),
                max_retries=2,
            )
            try:
                await service.authenticate()
                return service
            except Exception as e:
                logger.warning(f"⚠️  Market-data account [{u['username']}] unusable: {e}")
                await service.close()
        return None

    async def execute_trade_cycle(self):
        now_utc = datetime.now(timezone.utc)
        logger.info(f"━━━ Cycle {now_utc:%H:%M:%S} UTC ━━━")

        all_users = self._get_all_users()
        if not all_users:
            logger.warning("No connected Deriv accounts — skipping cycle")
            return

        # Cheap, DB-only daily reset for every connected user (no Deriv API
        # call) — must run for everyone, not just users who end up trading,
        # otherwise a circuit-broken user excluded from trading would never
        # get unstuck at UTC midnight.
        db.reset_daily_risk_flags(
            [u["username"] for u in all_users], now_utc.date().isoformat()
        )

        # Any authenticated account works for market data (candles/bias are
        # symbol-level, not per-user) — scan until one works so a single
        # broken/stale token can't block the whole cycle for every user.
        service = await self._get_market_data_service(all_users)
        if service is None:
            logger.error("❌ No usable Deriv account for market data — skipping cycle")
            return

        try:
            try:
                info = await service.get_account_info()
                self._current_balance = float(info.get("balance", 0.0))
                self._refresh_win_rate()
                logger.info(
                    f"💰 ${self._current_balance:.2f} | "
                    f"wr={self._win_rate:.1%} | "
                    f"recovery={'⚠️' if self._in_recovery else '✅'}"
                )
            except Exception as e:
                logger.warning(f"Balance fetch: {e}")

            self._maybe_notify_session_start()

            logger.info(
                f"🔍 session={'✅' if self._in_trading_session() else '❌'} "
                f"lock={'🔒' if self._trade_in_progress else '🔓'}"
            )

            if self._trade_in_progress:
                return

            if not self._in_trading_session():
                logger.info(f"😴 Outside session (UTC {now_utc.hour}:00) | Active: 07:00-17:00 UTC")
                return

            # ── Economic calendar blackout ─────────────────────────────────────────
            try:
                from news.news_pipeline import get_economic_blackout
                is_blackout, blackout_reason = get_economic_blackout(now_utc)
                if is_blackout:
                    logger.warning(f"📅 {blackout_reason} — skipping cycle")
                    self.save_signal("NEUTRAL", 0, 0, "N/A", blackout_reason, 0)
                    return
            except Exception as e:
                logger.warning(f"Calendar check failed: {e}")

            # ── Fetch all timeframes ───────────────────────────────────────────
            h4_bias = await self._get_4h_bias(service)
            h1_bias = await self._get_1h_bias(service)

            # Primary: 15M candles (250 bars = ~2.6 days)
            df = await self._get_candles(service, gran=900, count=250)
            df = self._compute_indicators(df)
            if df.empty:
                self.save_signal("NEUTRAL", 0, 0, "N/A", "Collecting market data...", 0)
                return

            row   = df.iloc[-1]
            price = float(row["close"])
            rsi   = float(row["RSI_14"])
            atr   = float(row["ATR_14"])
            adx   = float(row["ADX"])

            # ── Volatility gate ────────────────────────────────────────────────
            if atr > MAX_SAFE_ATR:
                self.save_signal("NEUTRAL", price, rsi, "N/A",
                    f"Extreme volatility (ATR {atr:.2f}) — waiting for calm", 0)
                logger.warning(f"⚠️  ATR {atr:.2f} > {MAX_SAFE_ATR}, skipping")
                return

            # ── Score confluence ───────────────────────────────────────────────
            bull_score, bear_score, bull_r, bear_r = self._score(df, h1_bias, h4_bias)
            logger.info(f"🔢 BULL={bull_score}/7 BEAR={bear_score}/7 need {MIN_CONFLUENCE}")

            # ── Hard bias filter — 4H macro trend must be directional ──────────
            # 4H must be strictly bullish/bearish (no neutral allowed — that's sideways).
            # 1H can be matching or neutral (lags 4H at session transitions).
            biases_agree_bull = h4_bias == "bullish" and h1_bias in ("bullish", "neutral")
            biases_agree_bear = h4_bias == "bearish" and h1_bias in ("bearish", "neutral")

            direction, reasons, score = None, [], 0

            if bull_score >= MIN_CONFLUENCE and bull_score > bear_score:
                if biases_agree_bull:
                    direction, reasons, score = "CALL", bull_r, bull_score
                    logger.info(f"🟢 CALL [{score}/7] | 4H={h4_bias} 1H={h1_bias}")
                else:
                    self.save_signal("NEUTRAL", price, rsi, h1_bias,
                        f"15M bull ({bull_score}/7) blocked by HTF bias (4H={h4_bias} 1H={h1_bias})",
                        bull_score)
                    return

            elif bear_score >= MIN_CONFLUENCE and bear_score > bull_score:
                if biases_agree_bear:
                    direction, reasons, score = "PUT", bear_r, bear_score
                    logger.info(f"🔴 PUT [{score}/7] | 4H={h4_bias} 1H={h1_bias}")
                else:
                    self.save_signal("NEUTRAL", price, rsi, h1_bias,
                        f"15M bear ({bear_score}/7) blocked by HTF bias (4H={h4_bias} 1H={h1_bias})",
                        bear_score)
                    return

            if direction:
                # ── News sentiment filter (before lock or signal save) ──────────────
                sentiment     = self._get_cached_sentiment()
                news_overall  = sentiment.get("overall", "Neutral")
                sentiment_min = MIN_CONFLUENCE + 1   # raise bar when news disagrees
                if direction == "CALL" and news_overall == "Bearish":
                    if score < sentiment_min:
                        logger.warning(f"⚠️  Bearish news opposes CALL ({score}/7 < {sentiment_min}) — skipping")
                        self.save_signal("NEUTRAL", price, rsi, h1_bias,
                            f"News sentiment Bearish — CALL needs {sentiment_min}/7, got {score}/7", score)
                        return
                    logger.info(f"📰 News Bearish but CALL has high confluence ({score}/7) — proceeding")
                elif direction == "PUT" and news_overall == "Bullish":
                    if score < sentiment_min:
                        logger.warning(f"⚠️  Bullish news opposes PUT ({score}/7 < {sentiment_min}) — skipping")
                        self.save_signal("NEUTRAL", price, rsi, h1_bias,
                            f"News sentiment Bullish — PUT needs {sentiment_min}/7, got {score}/7", score)
                        return
                    logger.info(f"📰 News Bullish but PUT has high confluence ({score}/7) — proceeding")
                elif news_overall != "Neutral":
                    logger.info(f"📰 Sentiment {news_overall} aligns with {direction}")

                # ── All filters passed — lock, save, and trade all accounts ────────
                signal_type = "BUY" if direction == "CALL" else "SELL"
                self.save_signal(signal_type, price, rsi, h1_bias,
                    " | ".join(reasons), score)

                eligible = [
                    u for u in all_users
                    if u.get("bot_enabled", True) and not u.get("circuit_broken", False)
                ]
                if not eligible:
                    logger.info(
                        f"⛔ 0/{len(all_users)} users eligible to trade "
                        f"(disabled or circuit-broken) — signal saved, no trades placed"
                    )
                    self._trade_in_progress = False
                    return

                logger.info(
                    f"🚀 {direction} | confluence={score}/7 | ADX={adx:.1f} | "
                    f"accounts={len(eligible)}/{len(all_users)}"
                )
                self._trade_in_progress = True

                results = await asyncio.gather(
                    *[self._place_for_user(
                        u["username"], u["deriv_token"],
                        u.get("trade_account_type", "real"), direction, score,
                      ) for u in eligible],
                    return_exceptions=True,
                )

                # If every placement failed (no monitor started), release lock now.
                # Otherwise the monitor's finally block will release it after settlement.
                if not any(r is True for r in results):
                    self._trade_in_progress = False
                    logger.warning("⚠️  All trade placements failed — lock released")
            else:
                top     = max(bull_score, bear_score)
                leaning = "bullish" if bull_score >= bear_score else "bearish"
                top_r   = bull_r if bull_score >= bear_score else bear_r
                self.save_signal(
                    "NEUTRAL", price, rsi, h1_bias,
                    f"Leaning {leaning} ({top}/7, need {MIN_CONFLUENCE}). "
                    + " | ".join(top_r[:3]), top
                )
                logger.info(f"⏳ Waiting — {leaning} {top}/7 (need {MIN_CONFLUENCE})")

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
                logger.info(f"⏱  Next cycle in {LOOP_INTERVAL}s")
                await asyncio.sleep(LOOP_INTERVAL)
        logger.info("🛑 Stopped")
