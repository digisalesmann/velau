"""
XAU/USD Master Strategy — multi-timeframe trend-following engine.

Timeframe stack:
  4H  candles → macro bias (bull/bear market)
  1H  candles → session trend direction
  15M candles → primary entry/exit signals

Confluence scoring (4/4 required — unanimous):
  1. 4H EMA trend  — price vs EMA50 on 4H  [REQUIRED — must match direction]
  2. 1H EMA trend  — EMA50 vs EMA100 on 1H [REQUIRED — must match or be neutral]
  3. RSI momentum  — RSI14 > 55 (bull) / < 45 (bear) on 15M (conviction zone)
  4. MACD momentum — both histogram AND line above/below zero on 15M

Bollinger-pullback, ADX/DI, BOS structure, and pivot-point factors were
removed after backtesting a full year of real Deriv frxXAUUSD data
(backtest/engine.py) showed each one was either noise (flat win rate
whether it agreed with the trade or not) or actively counterproductive
(win rate *lower* when it agreed) — see backtest/data/ and the ablation
results for the full breakdown. Only RSI and MACD showed a real positive
correlation between agreement and winning, hence unanimous 4/4 rather than
a partial-majority score.

CALL-only: backtesting also showed a large, consistent asymmetry — CALL
signals won 51.1% vs PUT at 43.5% over the same year — so PUT signals are
not taken at all for now (see the CALL-only gate in execute_trade_cycle).

Two further refinements, layered on top of the above:
  - Session hours narrowed to 11:00-12:00 and 14:00-17:00 UTC only, after
    an hour-by-hour breakdown showed 09:00-10:00 and 12:00-13:00 UTC
    backtesting well below break-even (45-46%) while these hours ran
    57-60%.
  - RSI and MACD (the two factors with real signal) must ALSO agree on the
    1H timeframe, not just 15M — a stricter, higher-conviction version of
    exactly the two things already confirmed to work, rather than adding
    a new indicator type.

Combined, these backtested at 62.3% win rate (231 trades over the year),
profitable in BOTH halves of the year independently (64.3% / 60.3%) — the
first configuration tested that crossed the 57.1% break-even line, and did
so consistently rather than in one lucky stretch. Caveat, stated plainly:
this was the best-performing configuration found after testing several
hypotheses (contract duration, multi-timeframe confirmation, session
narrowing, and their combination) — that "best of several tested" framing
means this looks somewhat better than its true underlying edge by
construction, and 231 trades is a smaller sample than the ~1300 the
un-narrowed version traded. Treat this as the best evidence available, not
as proven — trade frequency is also much lower now (roughly 1 trade every
1.5 days) as a direct consequence.

Trade contract (USE_MULTIPLIERS): frxXAUUSD Multiplier (MULTUP for CALL
signals) with stop_loss = risk and take_profit = 1.5 x risk, a fixed 1:1.5
risk-to-reward ratio. The stop sits ~1x ATR(15M) from entry. Sold at market
after MULT_MAX_HOLD_MINUTES or at MULT_DAILY_CLOSE_UTC_MIN if neither level is
hit first. Replaced the 15-minute Rise/Fall contract, whose payout Deriv sets
(a live quote on 2026-10-16 paid 64.6%, so 1:0.65, not the 75% assumed).
Validated in backtest/multiplier_test.py.

Position sizing tiers:
  $0-50    → 10%   (survival)
  $50-200  →  5%   (growth)
  $200-1000→  3%   (compound)
  $1000+   →  2%, $50 cap (preservation)

Break-even win rate at 1:1.5: 40.0%
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import logging
import time
import pandas as pd
from datetime import datetime, timedelta, timezone

from ta.trend      import EMAIndicator, MACD
from ta.momentum   import RSIIndicator
from ta.volatility import AverageTrueRange

from brokers.deriv_trading_service import DerivTradingService, EXECUTION_SYMBOL
from env_config                    import DERIV_APP_ID
from position_sizing               import (
    calculate_stake, WINS_TO_EXIT_RECOVERY, plan_multiplier_trade,
)
import database as db
from core import notifications as notif
from core.trend_strategy import TrendTrader

logger = logging.getLogger("XAUStrategy")

ANALYSIS_SYMBOL        = "frxXAUUSD"
MIN_CONFLUENCE         = 4          # 4/4 — unanimous among the 4 factors backtesting
                                    # confirmed actually carry signal (see module docstring)
CALL_ONLY              = True       # backtesting found PUT signals unprofitable; see docstring
LOOP_INTERVAL          = 300        # 5-minute cycles
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_DRAWDOWN_PCT = 10.0
SESSIONS               = [(11, 12), (14, 17)]  # backtested hours only — see docstring;
                                    # 09-10 and 12-13 UTC backtested well below break-even
MAX_SAFE_ATR           = 20.0       # filter extreme volatility spikes
REQUIRE_1H_CONFIRMATION = True      # RSI/MACD must also agree on 1H, not just 15M — see docstring

# Which strategy the bot runs:
#   "trend_4h"       core/trend_strategy.py: 4H trend-following on Multipliers,
#                    the only strategy that held up out of sample (see there)
#   "confluence_15m" the original 15-minute Rise/Fall strategy in this file,
#                    kept for reference: lost money in 10 of 11 years 2016-2026
TRADING_STRATEGY = "trend_4h"

# Trade contract: Multiplier with stop_loss = risk, take_profit = 1.5 x risk
# (fixed 1:1.5 risk-to-reward; see position_sizing.plan_multiplier_trade).
# False falls back to the old 15-minute Rise/Fall contract.
USE_MULTIPLIERS          = False    # the 1:1.5 version backtested ~break-even after
                                    # commission (negative in the recent half), so off
MULT_MAX_HOLD_MINUTES    = 240      # sell at market if neither stop nor target hit by then
MULT_DAILY_CLOSE_UTC_MIN = 20 * 60 + 45  # also sell by 20:45 UTC, before gold's
                                    # daily break / weekend close, so no position
                                    # is ever held through a price gap
MULT_POLL_INTERVAL       = 180      # seconds between monitor polls on a multiplier
                                    # (Deriv enforces SL/TP itself; polling only
                                    # records the result and enforces the timeout)
RECONCILE_EVERY_CYCLES   = 6        # re-check every account's open multiplier
                                    # positions every ~30 min (and on startup) so a
                                    # restart never orphans an open position


def multiplier_close_by(purchase_epoch: int) -> int:
    """Epoch by which a multiplier bought at purchase_epoch must be sold:
    the earlier of the max-hold timeout and the next daily close time."""
    bought   = datetime.fromtimestamp(purchase_epoch, timezone.utc)
    daily    = bought.replace(hour=MULT_DAILY_CLOSE_UTC_MIN // 60,
                              minute=MULT_DAILY_CLOSE_UTC_MIN % 60, second=0, microsecond=0)
    if daily <= bought:
        daily += timedelta(days=1)
    return min(purchase_epoch + MULT_MAX_HOLD_MINUTES * 60, int(daily.timestamp()))


def get_session_status() -> dict:
    """Single source of truth for session-status display — used by both
    /dashboard and /signals so the UI never drifts from SESSIONS again the
    way the old hardcoded "London open (07:00 UTC)" copy did."""
    now_utc = datetime.now(timezone.utc)
    if TRADING_STRATEGY == "trend_4h":
        # The trend strategy has no session windows: it evaluates every 4H
        # candle whenever the gold market is open.
        return {"in_session": True, "session_hours": "Around the clock", "mins_to_session": 0}
    in_session = any(s <= now_utc.hour < e for s, e in SESSIONS)
    session_hours = ", ".join(f"{s:02d}:00-{e:02d}:00" for s, e in SESSIONS) + " UTC"

    if in_session:
        mins_to_session = 0
    else:
        now_minutes = now_utc.hour * 60 + now_utc.minute
        starts_today = [s * 60 for s, _ in SESSIONS if s * 60 > now_minutes]
        if starts_today:
            mins_to_session = min(starts_today) - now_minutes
        else:
            mins_to_session = (24 * 60 - now_minutes) + min(s for s, _ in SESSIONS) * 60

    return {
        "in_session": in_session,
        "session_hours": session_hours,
        "mins_to_session": mins_to_session,
    }


class XAUMasterStrategy:
    def __init__(self):
        self.is_running          = False
        self._placing            = False  # a signal's orders are being placed right now
        self._open_contracts: dict = {}   # bot contract_id -> username, until settled
        self._cycle_count        = 0
        self._trend              = TrendTrader(self)
        self._consecutive_wins   = 0
        self._in_recovery        = False
        self._last_session_notif = None
        self._current_balance    = 0.0
        self._win_rate           = 0.0
        self._trade_count        = 0
        self._sentiment_cache: dict = {}
        self._sentiment_cache_time: datetime | None = None

    @property
    def _trade_in_progress(self) -> bool:
        """Position lock: held while orders are being placed and until EVERY
        bot position from the last signal has settled. Multiplier positions
        can stay open for hours, so this can't be released by whichever
        user's monitor happens to finish first (the old behaviour), or a new
        signal would stack a second position on top of still-open ones."""
        return self._placing or bool(self._open_contracts)

    @_trade_in_progress.setter
    def _trade_in_progress(self, value: bool):
        self._placing = bool(value)

    def _in_trading_session(self) -> bool:
        if TRADING_STRATEGY == "trend_4h":
            return True
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

    async def _maybe_notify_session_start(self):
        today = datetime.now(timezone.utc).date()
        hour  = datetime.now(timezone.utc).hour
        key   = (today, hour // 5)
        if self._last_session_notif != key and self._in_trading_session():
            self._last_session_notif = key
            # notif.notify_session_start broadcasts to every registered
            # device via a blocking `requests` call per token — offload it
            # so a slow/large broadcast doesn't stall the event loop and
            # every concurrent HTTP request the whole app is serving.
            try:
                await asyncio.to_thread(notif.notify_session_start)
            except Exception as e:
                logger.warning(f"notify_session_start failed: {e}")

    def _refresh_win_rate(self):
        try:
            stats = db.get_trade_stats()
            self._win_rate = stats["win_rate"] / 100.0
            self._trade_count = int(stats.get("total_trades", 0))
        except Exception:
            self._win_rate = 0.0
            self._trade_count = 0

    def _get_stake(self) -> tuple[float, str]:
        stake, tier = calculate_stake(
            balance          = self._current_balance,
            win_rate         = self._win_rate,
            in_recovery      = self._in_recovery,
            consecutive_wins = self._consecutive_wins,
            trade_count      = self._trade_count,
        )
        return stake, tier

    # ── DB helpers ─────────────────────────────────────────────────────────────
    def save_signal(self, sig_type, price, rsi, bias, reason, score=0, features: dict = None) -> int | None:
        try:
            return db.insert_signal(
                ANALYSIS_SYMBOL, sig_type,
                float(price), float(rsi), str(bias),
                # score=None: no confluence score applies (trend strategy),
                # so no "[n/4]" prefix for the app to render as one.
                reason if score is None else f"[{int(score)}/4] {reason}", int(score or 0),
                features_json=json.dumps(features) if features else None,
            )
        except Exception as e:
            logger.error(f"Signal DB error: {e}")
            return None

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

        # Volatility / range — still used for the volatility gate and
        # vol-scaled stake sizing, even though it's no longer a scoring factor
        df["ATR_14"]  = AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range()

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

    async def _get_1h_momentum(self, service) -> tuple[float | None, float | None, float | None]:
        """RSI14/MACD on 1H candles — only called to confirm an
        already-qualifying 15M signal also has momentum agreement one
        timeframe up (see module docstring); returns (None, None, None)
        on any failure so the caller can treat that as unconfirmed."""
        try:
            df = await self._get_candles(service, gran=3600, count=220)
            if df.empty or len(df) < 35:
                return None, None, None
            df["RSI_14"] = RSIIndicator(close=df["close"], window=14).rsi()
            macd = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
            df["MACD_H"] = macd.macd_diff()
            df["MACD_L"] = macd.macd()
            df.dropna(inplace=True)
            if df.empty:
                return None, None, None
            row = df.iloc[-1]
            return float(row["RSI_14"]), float(row["MACD_H"]), float(row["MACD_L"])
        except Exception as e:
            logger.warning(f"1H momentum error: {e}")
            return None, None, None

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

        logger.info(
            f"📊 Price={price:.2f} RSI={rsi:.1f} "
            f"EMA50={ema50:.2f} EMA100={ema100:.2f} MACD_H={macd_h:.4f}"
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

        features = {
            "price": price, "ema50": ema50, "ema100": ema100, "rsi": rsi,
            "macd_h": macd_h, "macd_l": macd_l,
            "h1_bias": h1_bias, "h4_bias": h4_bias,
        }

        return len(bull_r), len(bear_r), bull_r, bear_r, features

    # ── Contract monitor ───────────────────────────────────────────────────────
    async def _monitor_contract(self, token: str, account_type: str, contract_id, stake,
                                username: str = "", close_by: int | None = None):
        """
        Poll Deriv with a fresh connection until the contract settles.
        Polling is used instead of a long-lived subscription because Render's
        network drops WebSocket connections within seconds, making subscriptions
        unreliable and causing the position lock to be released prematurely.

        close_by (multipliers only): epoch at which the position is sold at
        market if Deriv hasn't already closed it at its stop-loss/take-profit.
        Without it the contract is a fixed-expiry Rise/Fall, watched for 20 min.
        """
        loop = asyncio.get_running_loop()
        if close_by:
            deadline      = loop.time() + max(0, close_by - time.time()) + 900  # 15-min grace to settle the sale
            poll_interval = MULT_POLL_INTERVAL
        else:
            deadline      = loop.time() + 1200  # 20-min cap
            poll_interval = 90  # seconds between polls — contract lasts 15 min
        logger.info(
            f"👁  Monitoring {contract_id} | stake=${float(stake):.2f}"
            + (f" | close by {datetime.fromtimestamp(close_by, timezone.utc):%H:%M} UTC" if close_by else "")
        )
        sell_attempted = False

        try:
            while loop.time() < deadline:
                wait = poll_interval
                if close_by and not sell_attempted:
                    wait = max(5, min(poll_interval, close_by - time.time()))
                await asyncio.sleep(wait)

                svc = DerivTradingService(token=token, account_type=account_type)
                try:
                    await svc.authenticate()
                    poc = await svc.get_contract(contract_id)
                    is_settled = poc.get("is_sold") in (1, True, "1") or \
                        poc.get("status", "") in ("won", "lost", "sold")
                    if not is_settled and close_by and time.time() >= close_by:
                        sell_attempted = True
                        logger.info(f"⏰ {contract_id} reached its max hold / daily close, selling at market")
                        await svc.sell_contract(contract_id)
                        poc = await svc.get_contract(contract_id)
                except Exception as poll_err:
                    logger.warning(f"👁  Poll error (will retry): {poll_err}")
                    continue
                finally:
                    await svc.close()

                if not poc:
                    logger.debug(f"👁  No poc in response, retrying…")
                    continue

                status = poc.get("status", "")
                logger.info(f"👁  Contract {contract_id} status={status!r}")

                if not (poc.get("is_sold") in (1, True, "1") or status in ("won", "lost", "sold")):
                    continue  # still open — poll again

                # Multipliers closed at their stop-loss/take-profit (or sold on
                # timeout) report status "sold", never "won", so win/loss has
                # to come from the realised profit, not the status string.
                await self._settle_contract(contract_id, float(poc.get("profit", 0)), username,
                                            MAX_CONSECUTIVE_LOSSES)
                return  # settled — done

            # A multiplier that couldn't be confirmed sold is still picked up
            # again by _reconcile_open_positions on the next reconcile pass.
            logger.warning(f"👁  Contract {contract_id} did not settle before the monitor deadline")

        except Exception as e:
            logger.error(f"Monitor fatal error: {e}")
        finally:
            # Only bot positions are in _open_contracts; a manual /trade
            # contract monitored here never touches the bot's position lock.
            self._open_contracts.pop(str(contract_id), None)
            if not self._open_contracts:
                logger.info("🔓 Position lock released")

    async def _settle_contract(self, contract_id, profit: float, username: str,
                               max_consecutive_losses: int,
                               max_daily_drawdown_pct: float = MAX_DAILY_DRAWDOWN_PCT):
        """Record a closed contract: trade_results row, per-user risk state
        (circuit breaker), win/loss notification, and the global stake-sizing
        streak counters. Shared by the contract monitor and the trend strategy."""
        won = profit > 0
        self.save_trade_result(contract_id, won, profit, username=username)

        state = db.record_trade_settlement(
            username, won, profit, max_consecutive_losses, max_daily_drawdown_pct
        )

        try:
            if username:
                await asyncio.to_thread(
                    notif.notify_user,
                    username,
                    f"Trade {'Won' if won else 'Lost'}  {'+' if won else '-'}${abs(profit):.2f}",
                    f"Contract {contract_id} settled.",
                    {"type": "trade_settled", "won": str(won), "pnl": str(profit)},
                )
            else:
                await asyncio.to_thread(notif.notify_trade_settled, contract_id, won, profit)
        except Exception as e:
            logger.warning(f"Trade-settled notification failed for contract {contract_id}: {e}")

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
                f"❌ [{username}] LOST -${abs(profit):.2f} | user_streak=-{state['consecutive_losses']} | "
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
            try:
                if username:
                    await asyncio.to_thread(
                        notif.notify_user,
                        username, "Circuit Breaker Triggered", body,
                        {"type": "circuit_breaker"},
                    )
                else:
                    await asyncio.to_thread(notif.notify_circuit_breaker, state["consecutive_losses"])
            except Exception as e:
                logger.warning(f"Circuit-breaker notification failed for {username}: {e}")

    async def _reconcile_open_positions(self, all_users: list[dict]):
        """
        Restart safety for multipliers. Unlike a 15-minute Rise/Fall, a
        multiplier never expires on its own, and monitors only live in this
        process's memory. After a Render restart/redeploy, any position the
        bot opened would otherwise go unwatched: no max-hold/daily-close
        sale, no recorded result, and no lock, so a new signal would open a
        second position on top of it. This finds the bot's own open
        multipliers on every account and resumes monitoring them.
        """
        try:
            app_id = int(DERIV_APP_ID)
        except (TypeError, ValueError):
            app_id = None

        for u in all_users:
            svc = DerivTradingService(
                token=u["deriv_token"], account_type=u.get("trade_account_type", "real"),
                max_retries=2,
            )
            try:
                await svc.authenticate()
                contracts = await svc.get_open_contracts()
            except Exception as e:
                logger.warning(f"🔁 Reconcile [{u['username']}] failed: {e}")
                continue
            finally:
                await svc.close()

            for c in contracts:
                cid = str(c.get("contract_id", ""))
                if not cid or cid in self._open_contracts:
                    continue
                if c.get("contract_type") not in ("MULTUP", "MULTDOWN"):
                    continue
                if c.get("underlying_symbol", c.get("symbol")) != EXECUTION_SYMBOL:
                    continue
                # Only contracts this app opened, never a user's own manual
                # multiplier trades placed through Deriv directly.
                if app_id is not None and c.get("app_id") not in (None, app_id):
                    continue
                purchase = int(c.get("purchase_time") or c.get("date_start") or time.time())
                close_by = multiplier_close_by(purchase)
                logger.warning(
                    f"🔁 [{u['username']}] adopting unmonitored open multiplier {cid} "
                    f"(bought {datetime.fromtimestamp(purchase, timezone.utc):%Y-%m-%d %H:%M} UTC)"
                )
                self._open_contracts[cid] = u["username"]
                asyncio.create_task(self._monitor_contract(
                    u["deriv_token"], u.get("trade_account_type", "real"), cid,
                    float(c.get("buy_price", 0) or 0), u["username"], close_by=close_by,
                ))

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
                               direction: str, score: int, atr: float = None,
                               price: float = None) -> bool:
        """Place a trade. Returns True if a monitor task was started, False on failure."""
        svc = DerivTradingService(token=token, account_type=account_type)
        try:
            await svc.authenticate()
            info    = await svc.get_account_info()
            balance = float(info.get("balance", 0.0))
            db.maybe_set_opening_balance(username, balance)
            # With multipliers this is the dollar RISK (loss at the stop), not
            # the Deriv stake — see position_sizing.plan_multiplier_trade.
            stake, tier = calculate_stake(
                balance          = balance,
                win_rate         = self._win_rate,
                in_recovery      = self._in_recovery,
                consecutive_wins = self._consecutive_wins,
                atr              = atr,
                atr_ceiling      = MAX_SAFE_ATR,
                trade_count      = self._trade_count,
            )
            close_by = None
            if USE_MULTIPLIERS:
                if not atr or not price:
                    raise ValueError("ATR and price are required to size a multiplier trade")
                plan = plan_multiplier_trade(stake, atr, price)
                if plan["stake"] > balance:
                    raise ValueError(
                        f"balance ${balance:.2f} below the ${plan['stake']:.2f} multiplier stake needed"
                    )
                result = await svc.place_order(
                    "MULTUP" if direction == "CALL" else "MULTDOWN", plan["stake"],
                    multiplier=plan["multiplier"],
                    stop_loss=plan["stop_loss"], take_profit=plan["take_profit"],
                )
                buy        = result.get("buy", {})
                purchase   = int(buy.get("purchase_time") or buy.get("start_time") or time.time())
                close_by   = multiplier_close_by(purchase)
                deriv_stake = plan["stake"]
                summary    = f"Risk ${plan['stop_loss']:.2f}  |  Target +${plan['take_profit']:.2f}"
            else:
                result      = await svc.place_order(direction, stake)
                deriv_stake = stake
                summary     = f"Stake ${stake:.2f}"
            contract_id = str(result.get("buy", {}).get("contract_id", "unknown"))
            logger.info(f"✅ [{username}] {direction} {summary} | contract={contract_id}")
            self._open_contracts[contract_id] = username

            try:
                await asyncio.to_thread(
                    notif.notify_user,
                    username,
                    f"Trade Placed {'CALL ↑' if direction == 'CALL' else 'PUT ↓'}",
                    f"XAU/USD  |  {summary}  |  Confluence {score}/4",
                    {"type": "trade_executed", "direction": direction},
                )
            except Exception as e:
                logger.warning(f"Trade-placed notification failed for {username}: {e}")

            # Pass the token — monitor opens its own fresh connections per poll
            asyncio.create_task(
                self._monitor_contract(token, account_type, contract_id, deriv_stake,
                                       username, close_by=close_by)
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

        # Startup, then every ~30 min: re-attach monitors to any open bot
        # multiplier positions this process isn't watching (e.g. after a
        # redeploy) before deciding whether the position lock is free. Only
        # for the confluence strategy's short multipliers: it would otherwise
        # adopt the trend strategy's week-long positions and sell them after
        # MULT_MAX_HOLD_MINUTES. Trend positions are tracked in the DB instead.
        if TRADING_STRATEGY == "confluence_15m" and USE_MULTIPLIERS and \
                self._cycle_count % RECONCILE_EVERY_CYCLES == 0:
            await self._reconcile_open_positions(all_users)
        self._cycle_count += 1

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

            if TRADING_STRATEGY == "trend_4h":
                await self._trend.run_cycle(service, all_users)
                return

            await self._maybe_notify_session_start()

            logger.info(
                f"🔍 session={'✅' if self._in_trading_session() else '❌'} "
                f"lock={'🔒' if self._trade_in_progress else '🔓'}"
            )

            if self._trade_in_progress:
                return

            if not self._in_trading_session():
                s_start, s_end = SESSIONS[0]
                logger.info(f"😴 Outside session (UTC {now_utc.hour}:00) | Active: {s_start:02d}:00-{s_end:02d}:00 UTC")
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

            # ── Volatility gate ────────────────────────────────────────────────
            if atr > MAX_SAFE_ATR:
                self.save_signal("NEUTRAL", price, rsi, "N/A",
                    f"Extreme volatility (ATR {atr:.2f}) — waiting for calm", 0)
                logger.warning(f"⚠️  ATR {atr:.2f} > {MAX_SAFE_ATR}, skipping")
                return

            # ── Score confluence ───────────────────────────────────────────────
            bull_score, bear_score, bull_r, bear_r, features = self._score(df, h1_bias, h4_bias)
            features["atr"] = atr
            logger.info(f"🔢 BULL={bull_score}/4 BEAR={bear_score}/4 need {MIN_CONFLUENCE}")

            # ── Hard bias filter — 4H macro trend must be directional ──────────
            # 4H must be strictly bullish/bearish (no neutral allowed — that's sideways).
            # 1H can be matching or neutral (lags 4H at session transitions).
            biases_agree_bull = h4_bias == "bullish" and h1_bias in ("bullish", "neutral")
            biases_agree_bear = h4_bias == "bearish" and h1_bias in ("bearish", "neutral")

            direction, reasons, score = None, [], 0

            if bull_score >= MIN_CONFLUENCE and bull_score > bear_score:
                if biases_agree_bull:
                    direction, reasons, score = "CALL", bull_r, bull_score
                    logger.info(f"🟢 CALL [{score}/4] | 4H={h4_bias} 1H={h1_bias}")
                else:
                    self.save_signal("NEUTRAL", price, rsi, h1_bias,
                        f"15M bull ({bull_score}/4) blocked by HTF bias (4H={h4_bias} 1H={h1_bias})",
                        bull_score, features)
                    return

            elif bear_score >= MIN_CONFLUENCE and bear_score > bull_score:
                if biases_agree_bear:
                    direction, reasons, score = "PUT", bear_r, bear_score
                    logger.info(f"🔴 PUT [{score}/4] | 4H={h4_bias} 1H={h1_bias}")
                else:
                    self.save_signal("NEUTRAL", price, rsi, h1_bias,
                        f"15M bear ({bear_score}/4) blocked by HTF bias (4H={h4_bias} 1H={h1_bias})",
                        bear_score, features)
                    return

            if direction == "PUT" and CALL_ONLY:
                logger.info(f"⛔ PUT [{score}/4] skipped — CALL-only mode")
                self.save_signal("NEUTRAL", price, rsi, h1_bias,
                    f"PUT ({score}/4) skipped — CALL-only mode (backtested PUT win rate below break-even)",
                    score, features)
                return

            if direction and REQUIRE_1H_CONFIRMATION:
                h1_rsi, h1_macd_h, h1_macd_l = await self._get_1h_momentum(service)
                confirmed = h1_rsi is not None and (
                    (direction == "CALL" and h1_rsi > 55 and h1_macd_h > 0 and h1_macd_l > 0) or
                    (direction == "PUT" and h1_rsi < 45 and h1_macd_h < 0 and h1_macd_l < 0)
                )
                if not confirmed:
                    logger.info(f"⛔ {direction} [{score}/4] lacks 1H RSI/MACD confirmation — skipping")
                    self.save_signal("NEUTRAL", price, rsi, h1_bias,
                        f"{direction} ({score}/4) lacks 1H RSI/MACD confirmation", score, features)
                    return

            if direction:
                # ── News sentiment filter (before lock or signal save) ──────────────
                # Confluence is already unanimous (score is always MIN_CONFLUENCE, the
                # max possible with 4 factors) — there's no higher bar left to clear,
                # so outright news disagreement blocks the trade rather than the old
                # "raise the bar" escalation, which no longer has room to raise into.
                sentiment    = self._get_cached_sentiment()
                news_overall = sentiment.get("overall", "Neutral")
                if direction == "CALL" and news_overall == "Bearish":
                    logger.warning(f"⚠️  Bearish news opposes CALL [{score}/4] — skipping")
                    self.save_signal("NEUTRAL", price, rsi, h1_bias,
                        f"News sentiment Bearish opposes CALL ({score}/4)", score, features)
                    return
                elif direction == "PUT" and news_overall == "Bullish":
                    logger.warning(f"⚠️  Bullish news opposes PUT [{score}/4] — skipping")
                    self.save_signal("NEUTRAL", price, rsi, h1_bias,
                        f"News sentiment Bullish opposes PUT ({score}/4)", score, features)
                    return
                elif news_overall != "Neutral":
                    logger.info(f"📰 Sentiment {news_overall} aligns with {direction}")

                # ── All filters passed — lock, save, and trade all accounts ────────
                signal_type = "BUY" if direction == "CALL" else "SELL"
                # id used below to record whether this signal actually resulted in
                # an executed trade — "qualified" and "executed" are not the same
                # thing, and the mobile app's badge must reflect the latter.
                signal_id = self.save_signal(signal_type, price, rsi, h1_bias,
                    " | ".join(reasons), score, features)

                eligible = [
                    u for u in all_users
                    if u.get("bot_enabled", True) and not u.get("circuit_broken", False)
                ]
                if not eligible:
                    logger.info(
                        f"⛔ 0/{len(all_users)} users eligible to trade "
                        f"(disabled or circuit-broken) — signal saved, no trades placed"
                    )
                    if signal_id:
                        db.update_signal_executed(signal_id, False)
                    self._trade_in_progress = False
                    return

                logger.info(
                    f"🚀 {direction} | confluence={score}/4 | "
                    f"accounts={len(eligible)}/{len(all_users)}"
                )
                self._trade_in_progress = True

                results = await asyncio.gather(
                    *[self._place_for_user(
                        u["username"], u["deriv_token"],
                        u.get("trade_account_type", "real"), direction, score, atr, price,
                      ) for u in eligible],
                    return_exceptions=True,
                )

                executed = any(r is True for r in results)
                if signal_id:
                    db.update_signal_executed(signal_id, executed)

                # Placement is done. The lock now stays held only by the open
                # positions in _open_contracts, each released by its monitor.
                self._trade_in_progress = False
                if not executed:
                    logger.warning("⚠️  All trade placements failed — lock released")
            else:
                top     = max(bull_score, bear_score)
                leaning = "bullish" if bull_score >= bear_score else "bearish"
                top_r   = bull_r if bull_score >= bear_score else bear_r
                self.save_signal(
                    "NEUTRAL", price, rsi, h1_bias,
                    f"Leaning {leaning} ({top}/4, need {MIN_CONFLUENCE}). "
                    + " | ".join(top_r[:3]), top, features
                )
                logger.info(f"⏳ Waiting — {leaning} {top}/4 (need {MIN_CONFLUENCE})")

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
