"""
backtest/macro_signals.py — does gold's fundamental relationship with the US
dollar and real interest rates give a *predictive* daily signal, beyond what
simply holding gold earns?

Data (Yahoo Finance daily closes, 2010+, in backtest/data/yf_*.csv):
  gold = GC=F, dxy = DX-Y.NYB (dollar index), tnx = ^TNX (10y yield),
  tip = TIP ETF (rises when real yields fall; FRED's DFII10 timed out).

Locked before running (16 configurations, none added afterwards):
  signals   M1 dollar falling, M2 TIP rising, M3 10y yield falling,
            M4 at least 2 of the 3
  lookback  20 or 60 trading days
  mode      long/flat or long/short
Signals use day t's close only; the position earns day t+1's return.
Costs: Deriv Multiplier (no swap): 0.026% commission + $0.10 per position
change. Periods match edge_research: DEV 2010-2022, VAL 2023-24, TEST 2025-26.

The test that matters is alpha: the strategy's daily return regressed on
gold's own daily return. Positive alpha with a t-stat >= 2 means real timing
skill; anything else is gold's own trend wearing a different hat.

Run: python3 -m backtest.macro_signals            (DEV + VAL)
     python3 -m backtest.macro_signals --final    (also TEST for VAL passers)
"""
import os
import sys
import itertools
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PERIODS = {"DEV": ("2010-01-01", "2023-01-01"), "VAL": ("2023-01-01", "2025-01-01"),
           "TEST": ("2025-01-01", "2026-10-01")}
COMMISSION, SLIPPAGE = 0.00026, 0.10


def load() -> pd.DataFrame:
    cols = {}
    for lab in ("gold", "dxy", "tnx", "tip"):
        s = pd.read_csv(os.path.join(DATA_DIR, f"yf_{lab}.csv"))
        cols[lab] = pd.Series(s["close"].to_numpy(), index=pd.to_datetime(s["epoch"], unit="s").dt.normalize())
    df = pd.DataFrame(cols).dropna()
    return df[~df.index.duplicated()]


def signal(df, kind, lb):
    up = lambda s: np.sign(s / s.shift(lb) - 1)
    votes = {"M1": -up(df.dxy), "M2": up(df.tip), "M3": -up(df.tnx)}
    if kind == "M4":
        return np.sign(votes["M1"] + votes["M2"] + votes["M3"])
    return votes[kind]


def run(df, kind, lb, mode):
    s = signal(df, kind, lb).fillna(0)
    pos = s.clip(lower=0) if mode == "long_flat" else s
    held = pos.shift(1).fillna(0)                       # decided at t, earns t+1
    ret = df.gold.pct_change().fillna(0)
    turns = pos.diff().abs().fillna(0).shift(1).fillna(0)
    cost = turns * (COMMISSION + SLIPPAGE / df.gold)
    return held * ret - cost, ret, held


def period_stats(net, gold_ret, held, period):
    a, b = PERIODS[period]
    n, g, h = net[a:b], gold_ret[a:b], held[a:b]
    X = np.column_stack([np.ones(len(g)), g.to_numpy()])
    coef, res, *_ = np.linalg.lstsq(X, n.to_numpy(), rcond=None)
    resid = n.to_numpy() - X @ coef
    se = np.sqrt(resid.var(ddof=2) * np.linalg.inv(X.T @ X)[0, 0])
    yrs = len(n) / 252
    return {
        "CAGR": f"{(1 + n).prod() ** (1 / yrs) - 1:+.1%}",
        "gold_CAGR": f"{(1 + g).prod() ** (1 / yrs) - 1:+.1%}",
        "sharpe": round(n.mean() / n.std() * np.sqrt(252), 2),
        "gold_sharpe": round(g.mean() / g.std() * np.sqrt(252), 2),
        "alpha/yr": f"{coef[0] * 252:+.1%}",
        "alpha_t": round(coef[0] / se, 2),
        "in_mkt": f"{(h != 0).mean():.0%}",
    }


def main(final=False):
    df = load()
    print(f"data {df.index[0].date()} -> {df.index[-1].date()}, {len(df)} days\n")
    for kind, lb, mode in itertools.product(("M1", "M2", "M3", "M4"), (20, 60), ("long_flat", "long_short")):
        net, g, h = run(df, kind, lb, mode)
        dev, val = period_stats(net, g, h, "DEV"), period_stats(net, g, h, "VAL")
        passed = dev["alpha_t"] >= 2 and val["alpha_t"] > 0
        print(f"{kind} lb={lb:<3} {mode:10}  DEV alpha {dev['alpha/yr']:>6} (t {dev['alpha_t']:>5}) sharpe {dev['sharpe']:>5} vs gold {dev['gold_sharpe']:>5}"
              f" | VAL alpha {val['alpha/yr']:>6} (t {val['alpha_t']:>5}) CAGR {val['CAGR']:>6} vs gold {val['gold_CAGR']:>6}"
              f"{'  <- PASS' if passed else ''}")
        if final and passed:
            print("      TEST:", period_stats(net, g, h, "TEST"))


if __name__ == "__main__":
    main(final="--final" in sys.argv)
