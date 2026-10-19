# Parity test: the live trend strategy's functions (core/trend_strategy.py),
# replayed bar by bar with only a recent window of candles the way the live
# bot sees them, must take the same trades as the backtest that validated the
# strategy (backtest/edge_research.f3_swing_trend + the daily stacked-average
# regime filter, close > SMA50 > SMA200, from backtest/regime_filters.py).
#
# Needs the gitignored history in backtest/data/hist_xau_{4h,1d}.csv (build
# with python3 -m backtest.build_hist); skipped if absent.

import os

import numpy as np
import pandas as pd

import core.trend_strategy as ts

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest", "data")
WINDOW = 600            # 4H bars the live bot fetches (2 batches)
START, END = "2023-01-01", "2026-10-01"


def _load():
    h4 = pd.read_csv(os.path.join(DATA, "hist_xau_4h.csv"))
    d1 = pd.read_csv(os.path.join(DATA, "hist_xau_1d.csv"))
    h4["t"] = pd.to_datetime(h4["epoch"], unit="s")
    d1["t"] = pd.to_datetime(d1["epoch"], unit="s")
    return h4, d1


def _replay_live(h4, d1, sleeve, k0, k1):
    """Entry epochs the live functions produce, with intrabar stop checks
    approximated by each bar's low (the live bot checks the spot price
    every 5 minutes)."""
    o, l = h4["open"].to_numpy(), h4["low"].to_numpy()
    ep = h4["epoch"].to_numpy()
    entries, pos, k = [], None, k0
    while k < k1:
        win = h4.iloc[max(0, k - WINDOW):k]          # bars closed before bar k
        if pos is not None:
            trail = ts.trailing_stop(win, pos["entry_ep"], pos["initial"], sleeve["stop_mult"], pos["trail"])
            pos["trail"] = trail
            held = ts.bars_held(h4.iloc[max(0, k - WINDOW):k + 1], pos["entry_ep"])
            if o[k] <= trail or l[k] <= trail or held >= ts.MAX_HOLD_BARS:
                pos = None
                k += 1                               # backtest re-enters from the next bar
                continue
            k += 1
            continue
        closed = h4.iloc[max(0, k - WINDOW):k + 1]   # bar k has just closed
        if ts.entry_signal(closed, sleeve["lookback"]) and ts.regime_up(d1, int(ep[k])):
            c = float(closed["close"].iloc[-1])
            dist = float(ts.stop_distance(ts.atr(closed)[-1:], np.array([c]), sleeve["stop_mult"])[0])
            pos = {"entry_ep": int(ep[k]), "initial": c - dist, "trail": None}
            entries.append(int(ep[k]))
        k += 1
    return entries


def _backtest_entries(h4, sleeve, k0, k1):
    import backtest.edge_research as er
    import backtest.regime_filters as rf
    er.COSTS["deriv_mult_noswap"] = {"fixed": 0.10, "pct": 0.00026, "swap_long": 0.0, "swap_short": 0.0}
    regime = rf.regime_array(h4, rf.daily_features(), rf.FILTERS["+ stacked_mas"])
    trades = er.f3_swing_trend(h4, "deriv_mult_noswap", lookback=sleeve["lookback"],
                               stop_mult=sleeve["stop_mult"], longs_only=True, regime_ok=regime,
                               max_stop_pct=ts.MAX_STOP_PCT, stop_mode="cap")
    lo, hi = h4["t"].iloc[k0], h4["t"].iloc[k1 - 1]
    return [int(pd.Timestamp(t).timestamp()) for t, _ in trades if lo <= t <= hi]


def test_live_matches_backtest():
    if not os.path.exists(os.path.join(DATA, "hist_xau_4h.csv")):
        print("history not built, skipping parity test")
        return
    h4, d1 = _load()
    # Load and window the backtest copy the same way edge_research does.
    h4["t"] = pd.to_datetime(h4["epoch"], unit="s")
    k0 = int(np.searchsorted(h4["epoch"].to_numpy(), pd.Timestamp(START).timestamp()))
    k1 = int(np.searchsorted(h4["epoch"].to_numpy(), pd.Timestamp(END).timestamp()))
    for sleeve in ts.SLEEVES:
        live = set(_replay_live(h4, d1, sleeve, k0, k1))
        bt = set(_backtest_entries(h4, sleeve, k0, k1))
        both = live & bt
        agree = len(both) / max(len(live | bt), 1)
        print(f"{sleeve['id']}: live {len(live)} entries, backtest {len(bt)}, identical {len(both)} "
              f"({agree:.1%} agreement)")
        assert agree >= 0.95, f"{sleeve['id']}: live and backtest disagree on {1 - agree:.1%} of entries"


if __name__ == "__main__":
    test_live_matches_backtest()
    print("test_live_matches_backtest passed.")
