"""
backtest/edge_research.py — structured search for a real, cost-surviving edge
on XAU/USD, run on 10+ years of 1-minute history (HistData.com, Jan 2016 -
Sep 2026, resampled to 15m/1h/4h/1d; validated against Deriv's own frxXAUUSD
15m candles: 94.3% bar-direction agreement, $0.34 median close difference).

Built because the original strategy was tuned on one bull-market year and
won 5/15 trades on data it had never seen. Guards against repeating that:

  1. Strategy families and their (small) parameter grids are fixed up front
     in FAMILIES below, not grown after looking at results.
  2. Three periods, used in order:
       DEV   2016-2022  parameters are chosen here, and only here
       VAL   2023-2024  the chosen parameters are checked once
       TEST  2025-2026  final hold-out, run only for families that pass VAL
  3. Every trade pays realistic costs, under two venues:
       cfd    MT5-style CFD: $0.45 round-trip spread/slippage + swap
       deriv  Deriv Multiplier: 0.026% of notional commission + $0.10 slippage
     plus overnight financing on positions held past 21:00 UTC.
  4. Conservative fills: entries at the signal bar's close, a bar touching
     both stop and target counts as the stop, gaps through the stop fill at
     the open.

Results are in R (multiples of the initial risk), so position sizing is out
of the picture and a 1R loss is the same everywhere.

Run: python3 -m backtest.edge_research            (DEV + VAL)
     python3 -m backtest.edge_research --final    (also TEST for VAL passers)
"""
import os
import sys
import math
import itertools
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

PERIODS = {
    "DEV":  ("2016-01-01", "2023-01-01"),
    "VAL":  ("2023-01-01", "2025-01-01"),
    "TEST": ("2025-01-01", "2026-10-01"),
}

COSTS = {
    "cfd":   {"fixed": 0.45, "pct": 0.0,     "swap_long": 0.05, "swap_short": 0.01},
    "deriv": {"fixed": 0.10, "pct": 0.00026, "swap_long": 0.05, "swap_short": 0.01},
}

# VAL pass bar: positive after costs with t-stat >= 1.5 on BOTH venues' cheaper
# one (cfd). Deliberately not a high bar; TEST is the real exam.
VAL_MIN_T = 1.5
MIN_TRADES = 30


# ── Data ───────────────────────────────────────────────────────────────────────
def load(label: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, f"hist_xau_{label}.csv"))
    df["t"] = pd.to_datetime(df["epoch"], unit="s")
    return df.reset_index(drop=True)


def atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


# ── Trade simulation ───────────────────────────────────────────────────────────
def simulate(df: pd.DataFrame, i: int, side: int, stop: float, target: float | None,
             last_idx: int, trail_atr: np.ndarray | None = None, trail_mult: float = 0.0):
    """Walk bars i+1..last_idx. Returns (exit_idx, exit_px, reason)."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    for j in range(i + 1, min(last_idx, len(df) - 1) + 1):
        if side > 0:
            if o[j] <= stop:
                return j, o[j], "stop_gap"
            if l[j] <= stop:
                return j, stop, "stop"
            if target is not None and o[j] >= target:
                return j, o[j], "target"
            if target is not None and h[j] >= target:
                return j, target, "target"
            if trail_atr is not None:
                stop = max(stop, c[j] - trail_mult * trail_atr[j])
        else:
            if o[j] >= stop:
                return j, o[j], "stop_gap"
            if h[j] >= stop:
                return j, stop, "stop"
            if target is not None and o[j] <= target:
                return j, o[j], "target"
            if target is not None and l[j] <= target:
                return j, target, "target"
            if trail_atr is not None:
                stop = min(stop, c[j] + trail_mult * trail_atr[j])
    j = min(last_idx, len(df) - 1)
    return j, c[j], "time"


def trade_r(df, i, j, side, entry, exit_px, risk_px, venue) -> float:
    cm = COSTS[venue]
    cost = cm["fixed"] + cm["pct"] * entry
    nights = max(0, (df["t"].iloc[j].normalize() - df["t"].iloc[i].normalize()).days)
    swap = (cm["swap_long"] if side > 0 else cm["swap_short"]) / 360 * entry * nights
    return (side * (exit_px - entry) - cost - swap) / risk_px


def min_risk_ok(entry: float, risk_px: float, venue: str) -> bool:
    """Skip trades whose stop is so tight that costs alone eat half the risk."""
    cm = COSTS[venue]
    return risk_px >= 2 * (cm["fixed"] + cm["pct"] * entry)


# ── Strategy families ──────────────────────────────────────────────────────────
def _day_groups(df):
    return df.groupby(df["t"].dt.normalize()).indices


def f1_london_breakout(m15, venue, stop_mode, rr, longs_only):
    """Asian range 00:00-07:00 UTC; enter on the first 15m close beyond it
    between 07:00 and 11:00 UTC; stop at the opposite side or the midpoint;
    target rr x risk; flat by 20:45 UTC."""
    hour = m15["t"].dt.hour.to_numpy(); minute = m15["t"].dt.minute.to_numpy()
    c = m15["close"].to_numpy(); hi = m15["high"].to_numpy(); lo = m15["low"].to_numpy()
    out = []
    for day, idx in _day_groups(m15).items():
        asia = [k for k in idx if hour[k] < 7]
        if len(asia) < 20:
            continue
        top, bot = hi[asia].max(), lo[asia].min()
        close_idx = [k for k in idx if hour[k] * 60 + minute[k] <= 20 * 60 + 30]
        if not close_idx:
            continue
        last = close_idx[-1]
        for k in idx:
            if not (7 <= hour[k] < 11):
                continue
            side = 1 if c[k] > top else -1 if c[k] < bot else 0
            if side == 0 or (longs_only and side < 0):
                continue
            stop = (bot if side > 0 else top) if stop_mode == "opposite" else (top + bot) / 2
            risk = abs(c[k] - stop)
            if risk <= 0 or not min_risk_ok(c[k], risk, venue):
                break
            target = c[k] + side * rr * risk
            j, px, why = simulate(m15, k, side, stop, target, last)
            out.append((m15["t"].iloc[k], trade_r(m15, k, j, side, c[k], px, risk, venue)))
            break
    return out


def f2_ny_breakout(m15, venue, stop_mode, rr, longs_only):
    """Pre-New-York range 07:00-08:30 New York time; enter on the first 15m
    close beyond it from 08:30 to 11:00 NY; same exits as F1."""
    ny = m15["t"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    mins = (ny.dt.hour * 60 + ny.dt.minute).to_numpy()
    day = ny.dt.normalize().dt.tz_localize(None)
    c = m15["close"].to_numpy(); hi = m15["high"].to_numpy(); lo = m15["low"].to_numpy()
    out = []
    for d, idx in m15.groupby(day).indices.items():
        pre = [k for k in idx if 7 * 60 <= mins[k] < 8 * 60 + 30]
        if len(pre) < 5:
            continue
        top, bot = hi[pre].max(), lo[pre].min()
        sess = [k for k in idx if mins[k] <= 16 * 60]
        if not sess:
            continue
        last = sess[-1]
        for k in idx:
            if not (8 * 60 + 30 <= mins[k] < 11 * 60):
                continue
            side = 1 if c[k] > top else -1 if c[k] < bot else 0
            if side == 0 or (longs_only and side < 0):
                continue
            stop = (bot if side > 0 else top) if stop_mode == "opposite" else (top + bot) / 2
            risk = abs(c[k] - stop)
            if risk <= 0 or not min_risk_ok(c[k], risk, venue):
                break
            target = c[k] + side * rr * risk
            j, px, why = simulate(m15, k, side, stop, target, last)
            out.append((m15["t"].iloc[k], trade_r(m15, k, j, side, c[k], px, risk, venue)))
            break
    return out


def f3_swing_trend(h4, venue, lookback, stop_mult, longs_only, regime_ok=None,
                   max_stop_pct=None, stop_mode="skip"):
    """4H Donchian breakout: enter when close > prior `lookback`-bar high (or
    < low); initial stop stop_mult x ATR(14, 4H), then an ATR chandelier
    trailing stop at the same multiple; no target, so winners can run for
    days. One position at a time. regime_ok (optional bool array per bar)
    blocks entries where it's False, e.g. a higher-timeframe trend filter.
    max_stop_pct models Deriv Multipliers' widest representable stop (their
    x50 minimum multiplier stops out at ~2% of price): stop_mode "skip" drops
    entries whose stop is wider, "cap" narrows the stop and trail to it."""
    c = h4["close"].to_numpy(); a = atr(h4)
    if max_stop_pct and stop_mode == "cap":
        a = np.minimum(a, max_stop_pct * c / stop_mult)
    hh = h4["high"].rolling(lookback).max().shift(1).to_numpy()
    ll = h4["low"].rolling(lookback).min().shift(1).to_numpy()
    out, k = [], lookback + 15
    while k < len(h4) - 1:
        side = 1 if c[k] > hh[k] else -1 if c[k] < ll[k] else 0
        if side == 0 or (longs_only and side < 0) or (regime_ok is not None and not regime_ok[k]):
            k += 1
            continue
        risk = stop_mult * a[k]
        if not min_risk_ok(c[k], risk, venue) or \
                (max_stop_pct and stop_mode == "skip" and risk / c[k] > max_stop_pct):
            k += 1
            continue
        stop = c[k] - side * risk
        j, px, why = simulate(h4, k, side, stop, None, k + 6 * 60, trail_atr=a, trail_mult=stop_mult)
        out.append((h4["t"].iloc[k], trade_r(h4, k, j, side, c[k], px, risk, venue)))
        k = j + 1
    return out


def f4_mean_reversion(h1, venue, rsi_th, trend_filter):
    """1H RSI(2) pullback: long when RSI2 < rsi_th (short when > 100-rsi_th);
    with trend_filter, only in the direction of the 200-bar SMA. Exit when
    price closes back through the 5-bar SMA, after 10 bars, or at a 2x ATR stop."""
    c = h1["close"].to_numpy(); a = atr(h1)
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.clip(d, 0, None)).ewm(alpha=1 / 2, adjust=False).mean()
    dn = pd.Series(np.clip(-d, 0, None)).ewm(alpha=1 / 2, adjust=False).mean()
    rsi2 = (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()
    sma200 = pd.Series(c).rolling(200).mean().to_numpy()
    sma5 = pd.Series(c).rolling(5).mean().to_numpy()
    out, k = [], 210
    o, h, l = (h1[x].to_numpy() for x in ("open", "high", "low"))
    while k < len(h1) - 11:
        side = 1 if rsi2[k] < rsi_th else -1 if rsi2[k] > 100 - rsi_th else 0
        if side and trend_filter and side * (c[k] - sma200[k]) <= 0:
            side = 0
        if side == 0:
            k += 1
            continue
        risk = 2 * a[k]
        if not min_risk_ok(c[k], risk, venue):
            k += 1
            continue
        stop, j, px = c[k] - side * risk, None, None
        for m in range(k + 1, k + 11):
            if (side > 0 and o[m] <= stop) or (side < 0 and o[m] >= stop):
                j, px = m, o[m]; break
            if (side > 0 and l[m] <= stop) or (side < 0 and h[m] >= stop):
                j, px = m, stop; break
            if side * (c[m] - sma5[m]) > 0:
                j, px = m, c[m]; break
        if j is None:
            j, px = k + 10, c[k + 10]
        out.append((h1["t"].iloc[k], trade_r(h1, k, j, side, c[k], px, risk, venue)))
        k = j + 1
    return out


def f5_intraday_momentum(m15, venue, mode, stop_mult):
    """London-session move (03:00-08:00 NY) predicts New York's (08:30-12:00 NY):
    mode 'momentum' trades in the London direction, 'reversal' against it.
    Stop stop_mult x ATR(14, 15M), time exit at 12:00 NY."""
    ny = m15["t"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    mins = (ny.dt.hour * 60 + ny.dt.minute).to_numpy()
    day = ny.dt.normalize().dt.tz_localize(None)
    c = m15["close"].to_numpy(); o = m15["open"].to_numpy(); a = atr(m15)
    out = []
    for d, idx in m15.groupby(day).indices.items():
        lon = [k for k in idx if 3 * 60 <= mins[k] < 8 * 60]
        ent = [k for k in idx if mins[k] == 8 * 60 + 15]   # bar closing 08:30 NY
        ext = [k for k in idx if mins[k] == 11 * 60 + 45]  # bar closing 12:00 NY
        if len(lon) < 15 or not ent or not ext:
            continue
        move = c[lon[-1]] - o[lon[0]]
        if abs(move) < 0.5 * a[lon[-1]]:
            continue
        side = int(np.sign(move)) * (1 if mode == "momentum" else -1)
        k = ent[0]; risk = stop_mult * a[k]
        if not min_risk_ok(c[k], risk, venue):
            continue
        j, px, why = simulate(m15, k, side, c[k] - side * risk, None, ext[0])
        out.append((m15["t"].iloc[k], trade_r(m15, k, j, side, c[k], px, risk, venue)))
    return out


FAMILIES = {
    "F1_london_breakout": (f1_london_breakout, "15m", dict(
        stop_mode=["opposite", "mid"], rr=[1.0, 1.5, 2.0], longs_only=[False, True])),
    "F2_ny_breakout": (f2_ny_breakout, "15m", dict(
        stop_mode=["opposite", "mid"], rr=[1.0, 1.5, 2.0], longs_only=[False, True])),
    "F3_swing_trend_4h": (f3_swing_trend, "4h", dict(
        lookback=[20, 55], stop_mult=[2.0, 3.0], longs_only=[False, True])),
    "F4_rsi2_mean_reversion_1h": (f4_mean_reversion, "1h", dict(
        rsi_th=[5, 10], trend_filter=[True, False])),
    "F5_intraday_momentum": (f5_intraday_momentum, "15m", dict(
        mode=["momentum", "reversal"], stop_mult=[2.0, 4.0])),
}


# ── Evaluation ─────────────────────────────────────────────────────────────────
def stats(trades) -> dict:
    if not trades:
        return {"n": 0}
    r = np.array([x[1] for x in trades])
    t = pd.Series(r, index=pd.DatetimeIndex([x[0] for x in trades]))
    daily = t.groupby(t.index.normalize()).sum()
    yrs = max((t.index[-1] - t.index[0]).days / 365.25, 0.5)
    eq = np.cumsum(r)
    return {
        "n": len(r),
        "per_yr": round(len(r) / yrs, 1),
        "win%": round(100 * (r > 0).mean(), 1),
        "avgR": round(r.mean(), 3),
        "t": round(r.mean() / r.std(ddof=1) * math.sqrt(len(r)), 2) if len(r) > 1 else 0.0,
        "PF": round(r[r > 0].sum() / max(-r[r < 0].sum(), 1e-9), 2),
        "R/yr": round(r.sum() / yrs, 1),
        "maxDD_R": round(float((np.maximum.accumulate(eq) - eq).max()), 1),
        "sharpe": round(daily.mean() / daily.std(ddof=1) * math.sqrt(252), 2) if len(daily) > 2 else 0.0,
    }


def slice_trades(trades, period):
    a, b = (pd.Timestamp(x) for x in PERIODS[period])
    return [x for x in trades if a <= x[0] < b]


def main(final: bool = False):
    data = {lab: load(lab) for lab in ("15m", "1h", "4h")}
    rows = []
    for fam, (fn, tf, grid) in FAMILIES.items():
        keys = list(grid)
        best = None
        cache = {}
        for combo in itertools.product(*grid.values()):
            params = dict(zip(keys, combo))
            trades = fn(data[tf], "cfd", **params)
            cache[combo] = trades
            s = stats(slice_trades(trades, "DEV"))
            if s["n"] >= MIN_TRADES and (best is None or s["t"] > best[1]["t"]):
                best = (combo, s)
        if best is None:
            print(f"{fam}: no combo with {MIN_TRADES}+ DEV trades")
            continue
        combo, dev = best
        params = dict(zip(keys, combo))
        for venue in ("cfd", "deriv"):
            trades = cache[combo] if venue == "cfd" else fn(data[tf], venue, **params)
            val = stats(slice_trades(trades, "VAL"))
            row = {"family": fam, "venue": venue, "params": params,
                   "DEV": stats(slice_trades(trades, "DEV")), "VAL": val}
            passed = val.get("n", 0) >= 10 and val.get("avgR", -1) > 0 and val.get("t", 0) >= VAL_MIN_T
            row["VAL_pass"] = passed
            if final and passed:
                row["TEST"] = stats(slice_trades(trades, "TEST"))
            rows.append(row)
    for r in rows:
        print("=" * 100)
        print(f"{r['family']}  [{r['venue']}]  params={r['params']}  VAL_pass={r['VAL_pass']}")
        for p in ("DEV", "VAL", "TEST"):
            if p in r:
                print(f"   {p:4}: {r[p]}")
    return rows


if __name__ == "__main__":
    main(final="--final" in sys.argv)
