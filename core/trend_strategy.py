"""
core/trend_strategy.py — 4H trend-following on XAU/USD via Deriv Multipliers.

Replaces the 15-minute Rise/Fall confluence strategy, which lost money in 10
of the 11 years 2016-2026 when backtested on 10+ years of 1-minute history,
and lost 25.7% of everything it staked across 103 demo trades in Jul-Aug 2026.

The rules here are exactly the ones validated in backtest/trend_candidate.py
(run with --regime), and tests/test_trend_parity.py replays these functions
over the same history to confirm they take the same trades:

  8 independent sleeves: 4 long + 4 short, lookback 20/55 bars x stop 2/3 ATR
  (short sleeves mirror the long rules below: breakdown below the lowest
  low, stacked-DOWN averages, stop above price trailed down)
  Entry   a completed 4H candle closes above the highest high of the previous
          `lookback` candles, AND on the previous day's daily close the moving
          averages are stacked: close > 50-day SMA > 200-day SMA (regime
          filter; trades only when gold's short and long-term trends are up).
  Stop    stop_mult x ATR(14) below entry (capped at 0.75% of price, the
          widest Deriv Multipliers allow with a safety margin), then trailed
          the same distance below each completed 4H close; it only moves up.
  Exit    price trades at or below the trailing stop, or 360 bars (60 days).
  Size    a signal risks 1% of the balance (capped at $500,
          position_sizing.trend_risk_amount), split across its side's 4
          sleeves, each as a dollar stop_loss on a Multiplier with no
          take-profit. New trades stop for the day once realised losses
          reach 3% of the day's opening balance (TREND_MAX_DAILY_LOSS_PCT).

Backtest 2011-2026 (Deriv Multiplier commission, no swap: Deriv states gold
Multipliers carry no overnight fees; stops capped at 0.75%), R = per-trade
risk, from backtest/regime_filters.py:
  bull years +8.8R/yr   sideways years +1.5R/yr   bear years 0.0R/yr
  average +5.0R/yr, worst drawdown 11.3R, 4 losing years of 16
The stacked-average filter replaced "close > 200-day SMA" alone, which had
the same total return but a 21.9R worst drawdown, 7 losing years, and lost
money in sideways (-0.3R/yr) and bear (-1.0R/yr) years. Win rate is only
~30-55%: profits come from a few long trends, so losing streaks are normal.

Deriv's stop_loss is a fixed dollar loss and can't trail into profit, so the
bot enforces the trailing stop itself every cycle (selling at market) and
keeps Deriv's stop_loss at the initial stop as a server-side backstop for
when the bot is down. Open positions live in the trend_positions table, not
in memory, because they run for days or weeks across Render restarts.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import database as db
from brokers.deriv_trading_service import DerivTradingService
from position_sizing import trend_risk_amount, plan_multiplier_trade

logger = logging.getLogger("TrendStrategy")

SYMBOL          = "frxXAUUSD"
GRAN_4H         = 14400
GRAN_1D         = 86400
# Long sleeves ("L") buy breakouts in a stacked-up market; short sleeves ("S")
# are the exact mirror, selling breakdowns in a stacked-down market (added
# 2026-10-19, backtest/short_side.py). Each side's 4 sleeves risk 1/4 of the
# per-trade amount each, as backtested.
SLEEVES = [
    {"id": f"{p}{lb}x{int(sm)}", "lookback": lb, "stop_mult": sm, "side": side}
    for p, side in (("L", 1), ("S", -1))
    for lb in (20, 55)
    for sm in (2.0, 3.0)
]
SLEEVES_PER_SIDE = 4


def sleeve_side(sleeve_id: str) -> int:
    """+1 long, -1 short. Derived from the id so trend_positions needs no column."""
    return -1 if sleeve_id.startswith("S") else 1
ATR_PERIOD      = 14
REGIME_SMA_DAYS = 200
REGIME_FAST_SMA_DAYS = 50
MAX_HOLD_BARS   = 360   # 60 days of 4H bars, matching the backtest's time exit
# Deriv's smallest gold multiplier for real accounts is x100 (a demo buy on
# 2026-10-19 rejected x50, though the public quote endpoint accepts it), and
# x100 stops out after ~1% of price. Stops (initial and trailing) are capped
# at 0.75% of price, which keeps the stake ~1.29x the risk, above the 1.25x
# floor in plan_multiplier_trade. Backtested with this cap
# (backtest/trend_candidate.py --regime --stop-limit=cap:0.0075):
# 2011-15 bear -1.0R/yr, 2016-22 +4.8, 2023-24 +3.3, 2025-26 +22.0R/yr.
MAX_STOP_PCT    = 0.0075
# A trend system with a 30-54% win rate routinely strings together 3+ losses,
# so the confluence strategy's 3-loss daily circuit breaker would keep pausing
# it and skipping the very entries the backtest depended on. Drawdown-based
# protection (MAX_DAILY_DRAWDOWN_PCT) still applies unchanged.
TREND_MAX_CONSECUTIVE_LOSSES = 8
# Daily loss limit: once realised losses reach this % of the day's opening
# balance, no new trend trades open until midnight UTC (open positions keep
# being managed). In backtest/risk_rules.py the strategy's worst day was
# -2.1% at 1% risk, so this never triggered in 2011-2026: it's a safety net
# for abnormal days (crisis gaps through stops, a broker glitch, a bug).
TREND_MAX_DAILY_LOSS_PCT = 3.0


# ── Pure signal logic (shared with tests/test_trend_parity.py) ─────────────────
def atr(df: pd.DataFrame, n: int = ATR_PERIOD) -> np.ndarray:
    """Wilder ATR, identical to backtest/edge_research.atr."""
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def stop_distance(atr_values: np.ndarray, closes: np.ndarray, stop_mult: float) -> np.ndarray:
    """stop_mult x ATR, capped at MAX_STOP_PCT of price (see MAX_STOP_PCT)."""
    return np.minimum(stop_mult * atr_values, MAX_STOP_PCT * closes)


def closed_bars(df: pd.DataFrame, gran: int, now_epoch: int) -> pd.DataFrame:
    """Drop the still-forming candle: a bar is complete once epoch + gran <= now."""
    return df[df["epoch"] + gran <= now_epoch].reset_index(drop=True)


def entry_signal(h4_closed: pd.DataFrame, lookback: int, side: int = 1) -> bool:
    """Long: last completed 4H close above the highest high of the `lookback`
    bars before it. Short: close below the lowest low of those bars."""
    if len(h4_closed) < lookback + 1:
        return False
    close = float(h4_closed["close"].iloc[-1])
    if side > 0:
        return close > float(h4_closed["high"].to_numpy()[-1 - lookback:-1].max())
    return close < float(h4_closed["low"].to_numpy()[-1 - lookback:-1].min())


def regime(daily: pd.DataFrame, bar_epoch: int) -> int:
    """As of the 4H bar's day, the previous daily close's moving averages:
    +1 stacked up (close > SMA50 > SMA200), -1 stacked down (close < SMA50 <
    SMA200), 0 neither (mixed: no new trades on either side)."""
    day_start = bar_epoch - bar_epoch % GRAN_1D
    prior = daily[daily["epoch"] < day_start]
    if len(prior) < REGIME_SMA_DAYS:
        return 0
    closes = prior["close"].to_numpy()
    close = float(closes[-1])
    sma_fast = float(closes[-REGIME_FAST_SMA_DAYS:].mean())
    sma_slow = float(closes[-REGIME_SMA_DAYS:].mean())
    if close > sma_fast > sma_slow:
        return 1
    if close < sma_fast < sma_slow:
        return -1
    return 0


def regime_up(daily: pd.DataFrame, bar_epoch: int) -> bool:
    return regime(daily, bar_epoch) == 1


def trailing_stop(h4_closed: pd.DataFrame, entry_bar_epoch: int, initial_stop: float,
                  stop_mult: float, stored_trail: float | None = None, side: int = 1) -> float:
    """Long: initial stop raised to (close - distance) of every completed bar
    after entry, never lowered. Short: lowered to (close + distance), never
    raised. Never looser than a previously stored value either."""
    c = h4_closed["close"].to_numpy()
    dist = stop_distance(atr(h4_closed), c, stop_mult)
    after = (h4_closed["epoch"] > entry_bar_epoch).to_numpy()
    tighter = max if side > 0 else min
    stop = tighter(initial_stop, stored_trail if stored_trail is not None else initial_stop)
    if after.any():
        trail = (c[after] - dist[after]).max() if side > 0 else (c[after] + dist[after]).min()
        stop = tighter(stop, float(trail))
    return stop


def bars_held(h4_closed: pd.DataFrame, entry_bar_epoch: int) -> int:
    return int((h4_closed["epoch"] > entry_bar_epoch).sum())


# ── Live trading ───────────────────────────────────────────────────────────────
async def _fetch(service: DerivTradingService, gran: int, batches: int) -> pd.DataFrame:
    """Recent candles, stitching `batches` requests back in time (Deriv caps
    each ticks_history response well below what 60-day trails need)."""
    raw, end = [], "latest"
    for _ in range(batches):
        chunk = await service.get_candles(SYMBOL, count=1000, granularity=gran, end=end)
        if not chunk:
            break
        raw = chunk + raw
        end = int(chunk[0]["epoch"]) - gran
    df = pd.DataFrame(raw)
    if df.empty:
        return df
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["epoch"] = df["epoch"].astype(int)
    return df.drop_duplicates("epoch").sort_values("epoch").reset_index(drop=True)


class TrendTrader:
    def __init__(self, strategy):
        # The XAUMasterStrategy instance: reused for signal saving and the
        # shared settlement path (trade_results, circuit breaker, notifications).
        self.strategy = strategy
        self._last_bar_evaluated: int | None = None

    async def run_cycle(self, service: DerivTradingService, all_users: list[dict]):
        now = int(time.time())
        h4_all = await _fetch(service, GRAN_4H, batches=2)
        daily  = await _fetch(service, GRAN_1D, batches=1)
        if h4_all.empty or daily.empty:
            logger.warning("Trend: no candle data, skipping cycle")
            return
        h4    = closed_bars(h4_all, GRAN_4H, now)
        daily = closed_bars(daily, GRAN_1D, now)
        if len(h4) < max(s["lookback"] for s in SLEEVES) + ATR_PERIOD + 1:
            logger.warning(f"Trend: only {len(h4)} closed 4H bars, skipping cycle")
            return
        spot     = float(h4_all["close"].iloc[-1])  # latest price, from the forming candle
        bar      = h4.iloc[-1]
        bar_ep   = int(bar["epoch"])
        a_now    = float(atr(h4)[-1])
        trend    = regime(daily, bar_ep)
        new_bar  = bar_ep != self._last_bar_evaluated
        # A sleeve's signal only counts when the market is stacked its way.
        signals  = {s["id"]: trend == s["side"] and entry_signal(h4, s["lookback"], s["side"])
                    for s in SLEEVES}
        logger.info(
            f"📈 Trend | spot={spot:.2f} bar={datetime.fromtimestamp(bar_ep, timezone.utc):%m-%d %H:%M} "
            f"close={bar['close']:.2f} ATR={a_now:.2f} regime={ {1: 'UP', -1: 'DOWN', 0: 'mixed'}[trend] } "
            f"signals={[k for k, v in signals.items() if v] or 'none'}"
        )

        if new_bar:
            fired = [k for k, v in signals.items() if v]
            if fired:
                lookbacks = sorted({s["lookback"] for s in SLEEVES if s["id"] in fired}, reverse=True)
                days = round(lookbacks[0] * 4 / 24)
                if trend > 0:
                    self.strategy.save_signal(
                        "BUY", bar["close"], 0, "bullish",
                        f"Gold closed at a {days}-day high on the 4H chart | "
                        f"Gold's 50-day and 200-day averages both point up", None)
                else:
                    self.strategy.save_signal(
                        "SELL", bar["close"], 0, "bearish",
                        f"Gold closed at a {days}-day low on the 4H chart | "
                        f"Gold's 50-day and 200-day averages both point down", None)
            else:
                why = {1: "Uptrend, waiting for a 4H breakout",
                       -1: "Downtrend, waiting for a 4H breakdown",
                       0: "No clear trend, staying in cash"}[trend]
                self.strategy.save_signal("NEUTRAL", bar["close"], 0,
                                          {1: "bullish", -1: "bearish", 0: "neutral"}[trend], why, None)

        for u in all_users:
            try:
                await self._run_user(u, h4, spot, bar_ep, a_now, signals, new_bar, now)
            except Exception as e:
                logger.error(f"❌ Trend [{u['username']}] cycle error: {e}")

        self._last_bar_evaluated = bar_ep

    async def _run_user(self, u, h4, spot, bar_ep, a_now, signals, new_bar, now):
        username = u["username"]
        positions = db.get_open_trend_positions(username)
        eligible = u.get("bot_enabled", True) and not u.get("circuit_broken", False)
        want_entries = new_bar and eligible and any(signals.values())
        if not positions and not want_entries:
            return

        svc = DerivTradingService(token=u["deriv_token"],
                                  account_type=u.get("trade_account_type", "real"), max_retries=2)
        try:
            await svc.authenticate()
            info = await svc.get_account_info()
            balance = float(info.get("balance", 0.0))
            # Stamp the day's opening balance on ANY day the bot touches this
            # account (no-op once set). Trend positions often close days after
            # they open; stamping only on entry days would leave exit-only days
            # without an opening balance, so the daily loss limit could never
            # trip on exactly the days losses get realised.
            db.maybe_set_opening_balance(username, balance)
            open_sleeves = set()
            for p in positions:
                try:
                    closed = await self._manage_position(svc, p, h4, spot, now)
                except Exception as e:
                    # e.g. Deriv rejects a sell while the market is closed:
                    # keep the position, retry next cycle, manage the others.
                    logger.warning(f"⚠️  [{username}] {p['sleeve']} {p['contract_id']}: {e}")
                    closed = False
                if not closed:
                    open_sleeves.add(p["sleeve"])
            if want_entries:
                await self._enter(svc, username, balance, h4, bar_ep, a_now, signals, open_sleeves, now)
        finally:
            await svc.close()

    async def _manage_position(self, svc, p, h4, spot, now) -> bool:
        """Returns True if the position is now closed."""
        cid, username = p["contract_id"], p["username"]
        poc = await svc.get_contract(cid)
        sold = poc.get("is_sold") in (1, True, "1") or poc.get("status") in ("won", "lost", "sold")
        reason = "deriv_stop"  # closed server-side: Deriv's stop_loss backstop or stop-out
        if not sold:
            side = sleeve_side(p["sleeve"])
            stored = float(p["trail_stop"]) if p.get("trail_stop") is not None else None
            trail = trailing_stop(h4, int(p["entry_bar_epoch"]), float(p["initial_stop"]),
                                  float(p["stop_mult"]), stored, side)
            if stored is None or (trail > stored if side > 0 else trail < stored):
                db.set_trend_trail(cid, username, trail)
            held = bars_held(h4, int(p["entry_bar_epoch"]))
            if (spot <= trail) if side > 0 else (spot >= trail):
                reason = "trailing_stop"
            elif held >= MAX_HOLD_BARS:
                reason = "max_hold"
            else:
                logger.info(f"👁  [{username}] {p['sleeve']} {cid} open | spot={spot:.2f} trail={trail:.2f} bars={held}")
                return False
            logger.info(f"💸 [{username}] {p['sleeve']} {cid} exit ({reason}) | spot={spot:.2f} trail={trail:.2f}")
            await svc.sell_contract(cid)
            poc = await svc.get_contract(cid)
        profit = float(poc.get("profit", 0) or 0)
        db.close_trend_position(cid, username, now, profit, reason)
        await self.strategy._settle_contract(cid, profit, username, TREND_MAX_CONSECUTIVE_LOSSES,
                                             TREND_MAX_DAILY_LOSS_PCT)
        return True

    async def _enter(self, svc, username, balance, h4, bar_ep, a_now, signals, open_sleeves, now):
        # Fixed fractional sizing, as backtested: 1% of the current balance per
        # signal (capped, see position_sizing.trend_risk_amount), no recovery
        # step-down and no win-rate caution.
        total_risk = trend_risk_amount(balance)
        sleeve_risk = round(total_risk / SLEEVES_PER_SIDE, 2)
        entry = float(h4["close"].iloc[-1])
        for s in SLEEVES:
            if not signals[s["id"]] or s["id"] in open_sleeves:
                continue
            last_exit = db.get_last_trend_exit(username, s["id"])
            if last_exit is not None and last_exit >= bar_ep:
                continue  # exited during this bar; the backtest re-enters from the next bar
            dist = float(stop_distance(np.array([a_now]), np.array([entry]), s["stop_mult"])[0])
            try:
                # Plan with the (capped) stop distance expressed as 1 x "ATR".
                plan = plan_multiplier_trade(sleeve_risk, dist, entry, stop_atr_mult=1.0, rr=None)
            except ValueError as e:
                logger.warning(f"⛔ [{username}] {s['id']} skipped: {e}")
                continue
            if plan["stake"] > balance:
                logger.warning(f"⛔ [{username}] {s['id']} skipped: stake ${plan['stake']:.2f} > balance ${balance:.2f}")
                continue
            side = s["side"]
            result = await svc.place_order("MULTUP" if side > 0 else "MULTDOWN", plan["stake"],
                                           multiplier=plan["multiplier"], stop_loss=plan["stop_loss"])
            buy = result.get("buy", {})
            cid = str(buy.get("contract_id"))
            initial_stop = entry - side * dist
            try:
                db.insert_trend_position(cid, username, s["id"], bar_ep, entry, s["stop_mult"],
                                         initial_stop, plan["stop_loss"], plan["stake"],
                                         plan["multiplier"], int(buy.get("purchase_time") or now))
            except Exception as e:
                # The contract exists on Deriv but we can't track it: close it
                # rather than leave an unmanaged position running for weeks.
                logger.error(f"🚨 [{username}] could not record {cid} ({e}), selling it")
                await svc.sell_contract(cid)
                continue
            open_sleeves.add(s["id"])
            logger.info(f"✅ [{username}] {s['id']} {'BUY' if side > 0 else 'SELL'} x{plan['multiplier']} stake=${plan['stake']:.2f} "
                        f"risk=${plan['stop_loss']:.2f} stop={initial_stop:.2f} | contract={cid}")
            try:
                from core import notifications as notif
                await asyncio.to_thread(
                    notif.notify_user, username,
                    f"Trade Placed: Gold {'uptrend (buy)' if side > 0 else 'downtrend (sell)'}",
                    f"XAU/USD  |  Risk ${plan['stop_loss']:.2f}  |  Trailing stop, no fixed target",
                    {"type": "trade_executed", "direction": "CALL" if side > 0 else "PUT"},
                )
            except Exception as e:
                logger.warning(f"Trade-placed notification failed for {username}: {e}")
