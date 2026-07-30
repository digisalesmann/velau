"""
backtest/engine.py — replays cached historical Deriv candles through the
REAL live scoring code (XAUMasterStrategy._compute_indicators / ._score /
._detect_bos, imported directly, not reimplemented) to see what the
strategy would actually have done, and simulates each qualifying signal's
real outcome and P&L using the bot's own position-sizing logic.

Honest scope — what this does NOT model (disclosed, not hidden):
  - No economic-calendar blackout (news.news_pipeline.get_economic_blackout)
    and no live news-sentiment overlay — neither is reconstructable
    historically without a paid historical news/calendar feed. Live
    trading skips more setups than this backtest will, around news events.
  - Payout assumed at a fixed 75% (the ratio the codebase's own
    position_sizing.py documents as its break-even reference), not
    re-derived from actual historical Deriv proposal quotes, which
    fluctuate slightly and aren't retrievable retroactively.
  - Only the strategy engine's own signal → one position at a time,
    matching the real `_trade_in_progress` lock — doesn't model multiple
    concurrent users/manual trades.

Run: python3 -m backtest.engine
"""
import os
import sys
import time
import logging
import datetime
import math
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The live strategy logs at INFO on every single candle evaluated (by design,
# for live observability) — across a year of 15-min bars that's tens of
# thousands of log lines and materially slows the backtest. Silence it here;
# nothing about the scoring logic itself changes.
logging.getLogger("XAUStrategy").setLevel(logging.WARNING)

from core.strategy_engine import (
    XAUMasterStrategy, MIN_CONFLUENCE, MAX_SAFE_ATR, SESSIONS, CALL_ONLY,
    REQUIRE_1H_CONFIRMATION,
)
from position_sizing import calculate_stake, WINS_TO_EXIT_RECOVERY, MIN_STAKE

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator

DATA_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOL     = "frxXAUUSD"
PAYOUT_PCT = 0.75   # documented break-even assumption in position_sizing.py
LOOKBACK   = 250    # matches live _get_candles count
HTF_COUNT  = 220    # matches live _get_1h_bias / _get_4h_bias count
STARTING_BALANCE = 100.0


def _load(label: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, f"{SYMBOL}_{label}.csv"))
    df.sort_values("epoch", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _in_session(epoch: int, sessions=None) -> bool:
    hour = datetime.datetime.fromtimestamp(epoch, datetime.UTC).hour
    return any(s <= hour < e for s, e in (sessions or SESSIONS))


def _h1_bias_asof(h1: pd.DataFrame, h1_epochs: np.ndarray, t_epoch: int) -> str:
    idx = np.searchsorted(h1_epochs, t_epoch - 3600, side="right")
    if idx < 110:
        return "neutral"
    window = h1.iloc[max(0, idx - HTF_COUNT):idx]
    if len(window) < 110:
        return "neutral"
    ema50  = EMAIndicator(close=window["close"], window=50).ema_indicator()
    ema100 = EMAIndicator(close=window["close"], window=100).ema_indicator()
    e50, e100 = ema50.iloc[-1], ema100.iloc[-1]
    if pd.isna(e50) or pd.isna(e100):
        return "neutral"
    return "bullish" if e50 > e100 else "bearish" if e50 < e100 else "neutral"


def _h4_bias_asof(h4: pd.DataFrame, h4_epochs: np.ndarray, t_epoch: int) -> str:
    idx = np.searchsorted(h4_epochs, t_epoch - 14400, side="right")
    if idx < 60:
        return "neutral"
    window = h4.iloc[max(0, idx - HTF_COUNT):idx]
    if len(window) < 60:
        return "neutral"
    ema50 = EMAIndicator(close=window["close"], window=50).ema_indicator()
    e50 = ema50.iloc[-1]
    price = window["close"].iloc[-1]
    if pd.isna(e50):
        return "neutral"
    return "bullish" if price > e50 else "bearish"


def _h1_rsi_macd_asof(h1: pd.DataFrame, h1_epochs: np.ndarray, t_epoch: int):
    """RSI14/MACD on the 1H timeframe as of t_epoch, using only fully-closed
    1H candles — mirrors the same RSI/MACD conditions _score() applies on
    15M, just on the higher timeframe, for testing multi-timeframe
    confirmation of the two factors backtesting confirmed carry signal."""
    idx = np.searchsorted(h1_epochs, t_epoch - 3600, side="right")
    if idx < 35:
        return None, None, None
    window = h1.iloc[max(0, idx - HTF_COUNT):idx]
    if len(window) < 35:
        return None, None, None
    rsi = RSIIndicator(close=window["close"], window=14).rsi().iloc[-1]
    macd = MACD(close=window["close"], window_slow=26, window_fast=12, window_sign=9)
    macd_h = macd.macd_diff().iloc[-1]
    macd_l = macd.macd().iloc[-1]
    if pd.isna(rsi) or pd.isna(macd_h) or pd.isna(macd_l):
        return None, None, None
    return float(rsi), float(macd_h), float(macd_l)


# f5_bb_pullback, f6_adx_di, f7_bos, and f8_pivot were dropped from this list
# once the ablation testing that used it confirmed they were noise/harmful
# and the real _score() no longer computes them — see core/strategy_engine.py.
FACTOR_NAMES = ["f1_4h_macro", "f2_1h_trend", "f3_rsi", "f4_macd"]


def _factor_votes(features: dict) -> dict:
    """Re-derives each confluence factor's individual vote ("bull"/"bear"/
    "neutral") from the raw values _score() already computed — mirrors
    _score()'s conditions exactly, just split apart per-factor instead of
    only exposing the summed count, so we can see which factors actually
    carry signal versus which just add noise."""
    h1_bias, h4_bias = features["h1_bias"], features["h4_bias"]
    votes = {}
    votes["f1_4h_macro"] = "bull" if h4_bias == "bullish" else "bear" if h4_bias == "bearish" else "neutral"
    votes["f2_1h_trend"] = "bull" if h1_bias == "bullish" else "bear" if h1_bias == "bearish" else "neutral"
    rsi = features["rsi"]
    votes["f3_rsi"] = "bull" if rsi > 55 else "bear" if rsi < 45 else "neutral"
    macd_h, macd_l = features["macd_h"], features["macd_l"]
    votes["f4_macd"] = "bull" if (macd_h > 0 and macd_l > 0) else "bear" if (macd_h < 0 and macd_l < 0) else "neutral"
    return votes


def run_backtest(m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame,
                  start_idx: int = None, end_idx: int = None, label: str = "full",
                  exclude_factor: str = None, min_confluence: int = None,
                  call_only: bool = None, hold_bars: int = 1,
                  require_1h_confirmation: bool = None, sessions=None) -> dict:
    strat = XAUMasterStrategy()
    required_score = min_confluence if min_confluence is not None else MIN_CONFLUENCE
    call_only = CALL_ONLY if call_only is None else call_only
    require_1h_confirmation = REQUIRE_1H_CONFIRMATION if require_1h_confirmation is None else require_1h_confirmation
    sessions = sessions if sessions is not None else SESSIONS
    h1_epochs = h1["epoch"].to_numpy()
    h4_epochs = h4["epoch"].to_numpy()

    start_idx = start_idx or LOOKBACK
    end_idx   = end_idx or (len(m15) - 2)  # need i+1 for settlement

    balance          = STARTING_BALANCE
    consecutive_wins = 0
    in_recovery      = False
    trades           = []
    equity_curve     = [balance]
    next_free_idx    = start_idx  # can't open a new position before this index

    i = start_idx
    while i <= end_idx:
        if i < next_free_idx:
            i += 1
            continue

        row_epoch = int(m15["epoch"].iloc[i])
        if not _in_session(row_epoch, sessions):
            i += 1
            continue

        window = m15.iloc[max(0, i - LOOKBACK + 1): i + 1].reset_index(drop=True)
        df = strat._compute_indicators(window.copy())
        if df.empty:
            i += 1
            continue

        atr = float(df.iloc[-1]["ATR_14"])
        if atr > MAX_SAFE_ATR:
            i += 1
            continue

        h1_bias = _h1_bias_asof(h1, h1_epochs, row_epoch)
        h4_bias = _h4_bias_asof(h4, h4_epochs, row_epoch)

        bull_score, bear_score, bull_r, bear_r, features = strat._score(df, h1_bias, h4_bias)
        votes = _factor_votes(features)

        # Ablation support: recompute the aggregate score with one or more
        # factors' contributions removed, without touching the real
        # _score() logic at all — lets us test "what if these factors
        # didn't exist" using the exact same scoring code for everything
        # else. Accepts a single factor name or a list of them.
        excluded = ([exclude_factor] if isinstance(exclude_factor, str) else exclude_factor) or []
        for fname in excluded:
            v = votes[fname]
            if v == "bull":
                bull_score -= 1
            elif v == "bear":
                bear_score -= 1

        biases_agree_bull = h4_bias == "bullish" and h1_bias in ("bullish", "neutral")
        biases_agree_bear = h4_bias == "bearish" and h1_bias in ("bearish", "neutral")

        direction, score = None, 0
        if bull_score >= required_score and bull_score > bear_score and biases_agree_bull:
            direction, score = "CALL", bull_score
        elif bear_score >= required_score and bear_score > bull_score and biases_agree_bear:
            direction, score = "PUT", bear_score

        if call_only and direction == "PUT":
            direction = None

        if direction is not None and require_1h_confirmation:
            h1_rsi, h1_macd_h, h1_macd_l = _h1_rsi_macd_asof(h1, h1_epochs, row_epoch)
            if h1_rsi is None:
                direction = None
            elif direction == "CALL" and not (h1_rsi > 55 and h1_macd_h > 0 and h1_macd_l > 0):
                direction = None
            elif direction == "PUT" and not (h1_rsi < 45 and h1_macd_h < 0 and h1_macd_l < 0):
                direction = None

        if direction is None:
            i += 1
            continue

        # Need `hold_bars` contiguous candles ahead to settle against — skip if
        # a data gap (weekend/holiday boundary) falls anywhere in that window.
        # Payout % is assumed constant across durations (no historical quotes
        # for other durations are retrievable) — a simplification, same as
        # the fixed 75% payout assumption already disclosed above.
        if i + hold_bars >= len(m15) or \
                int(m15["epoch"].iloc[i + hold_bars]) - row_epoch != hold_bars * 900:
            i += 1
            continue

        entry_price = float(m15["close"].iloc[i])
        exit_price  = float(m15["close"].iloc[i + hold_bars])
        won = (exit_price > entry_price) if direction == "CALL" else (exit_price < entry_price)

        win_rate = (sum(1 for t in trades if t["won"]) / len(trades)) if trades else 0.0
        stake, tier = calculate_stake(
            balance=balance, win_rate=win_rate, in_recovery=in_recovery,
            consecutive_wins=consecutive_wins, atr=atr, atr_ceiling=MAX_SAFE_ATR,
        )

        pnl = stake * PAYOUT_PCT if won else -stake
        balance += pnl
        balance = max(balance, 0.0)

        if won:
            consecutive_wins += 1
            if in_recovery and consecutive_wins >= WINS_TO_EXIT_RECOVERY:
                in_recovery = False
        else:
            consecutive_wins = 0
            in_recovery = True

        trades.append({
            "time": datetime.datetime.fromtimestamp(row_epoch, datetime.UTC),
            "direction": direction, "score": score, "stake": stake,
            "won": won, "pnl": pnl, "balance": balance, "tier": tier,
            "votes": votes,
        })
        equity_curve.append(balance)

        next_free_idx = i + hold_bars + 1  # position occupies bars i..i+hold_bars
        i += 1

    return trades, equity_curve


def _summarize(trades: list, label: str, starting_balance: float = None) -> dict:
    n = len(trades)
    if n == 0:
        return {"label": label, "trades": 0}

    # Local equity curve relative to this slice's own trades — for a
    # sub-period breakdown this shows the drawdown *within that window*,
    # not the absolute account balance (which depends on whatever came
    # before it in the single continuous run this slice was cut from).
    start = starting_balance if starting_balance is not None else 0.0
    equity_curve = [start]
    for t in trades:
        equity_curve.append(equity_curve[-1] + t["pnl"])

    wins = sum(1 for t in trades if t["won"])
    win_rate = wins / n
    total_pnl = sum(t["pnl"] for t in trades)
    avg_pnl = total_pnl / n
    returns = [t["pnl"] / max(t["stake"], MIN_STAKE) for t in trades]  # per-trade return on stake
    mean_ret = float(np.mean(returns))
    std_ret  = float(np.std(returns)) if n > 1 else 0.0
    sharpe_per_trade = (mean_ret / std_ret) if std_ret > 0 else 0.0
    # crude annualization assuming ~250 trading days, scaled by trades/day observed
    span_days = max((trades[-1]["time"] - trades[0]["time"]).total_seconds() / 86400, 1)
    trades_per_day = n / span_days
    sharpe_annualized = sharpe_per_trade * math.sqrt(trades_per_day * 250) if trades_per_day > 0 else 0.0

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return {
        "label": label,
        "trades": n,
        "win_rate_pct": round(win_rate * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(avg_pnl, 3),
        "final_balance": round(equity_curve[-1], 2),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "sharpe_annualized_approx": round(sharpe_annualized, 2),
        "span_days": round(span_days, 1),
        "trades_per_day": round(trades_per_day, 2),
        "first_trade": trades[0]["time"].isoformat(),
        "last_trade": trades[-1]["time"].isoformat(),
    }


def main():
    m15 = _load("15m")
    h1  = _load("1h")
    h4  = _load("4h")

    t0 = time.time()
    trades, equity_curve = run_backtest(m15, h1, h4, label="full_period")
    elapsed = time.time() - t0
    print(f"Full backtest over {len(m15)} 15m candles completed in {elapsed/60:.1f} min "
          f"({len(trades)} qualifying signals found)")

    print_result(_summarize(trades, "full_period", starting_balance=STARTING_BALANCE))

    # Split by trade count (not calendar midpoint) so each half carries equal
    # statistical weight — checks whether performance held up across time
    # rather than being driven entirely by one lucky/unlucky stretch.
    if len(trades) >= 10:
        mid = len(trades) // 2
        first, second = trades[:mid], trades[mid:]
        bal_before_second = first[-1]["balance"] if first else STARTING_BALANCE
        print_result(_summarize(first, "first_half_by_trade_count", starting_balance=STARTING_BALANCE))
        print_result(_summarize(second, "second_half_by_trade_count", starting_balance=bal_before_second))


def print_result(r: dict):
    print("=" * 60)
    if r.get("trades", 0) == 0:
        print(f"[{r['label']}] NO QUALIFYING TRADES")
        return
    for k, v in r.items():
        print(f"  {k:28s}: {v}")


if __name__ == "__main__":
    main()
