"""
backtest/multiplier_test.py — compares the current 15-minute Rise/Fall
contract against the 1:1.5 Multiplier contract (stop_loss = risk,
take_profit = 1.5 x risk, see position_sizing.plan_multiplier_trade) on the
same entry signals (production _score()/CALL-only logic, unchanged).

Rise/Fall is shown at both the 75% payout the backtest has always assumed and
the 64.6% a live frxXAUUSD quote actually paid on 2026-10-16. Multiplier rows
vary the max-hold timeout and whether positions are force-closed at the
daily close time, to pick the live settings.

"R" below = P&L per trade as a multiple of the dollar amount risked.

Run: python3 -m backtest.multiplier_test
"""
import time
from collections import Counter

import numpy as np

import backtest.engine as engine
from backtest.engine import _load, run_backtest, _summarize, print_result, STARTING_BALANCE

LIVE_QUOTED_PAYOUT = 0.646

# (label, run_backtest kwargs)
CONFIGS = [
    ("risefall_75pct_assumed", dict(exit_mode="expiry"), 0.75),
    ("risefall_64.6pct_live_quote", dict(exit_mode="expiry"), LIVE_QUOTED_PAYOUT),
    ("mult_hold120_close2045", dict(exit_mode="multiplier", max_hold_bars=8), None),
    ("mult_hold240_close2045", dict(exit_mode="multiplier", max_hold_bars=16), None),
    ("mult_hold480_close2045", dict(exit_mode="multiplier", max_hold_bars=32), None),
    ("mult_hold480_no_daily_close", dict(exit_mode="multiplier", max_hold_bars=32,
                                         daily_close_utc_min=24 * 60), None),
]


def _r_stats(trades: list) -> dict:
    r = np.array([t["pnl"] / t["stake"] for t in trades])
    mid = len(r) // 2
    return {
        "avg_R_per_trade": round(float(r.mean()), 3),
        "avg_R_first_half": round(float(r[:mid].mean()), 3),
        "avg_R_second_half": round(float(r[mid:].mean()), 3),
        "worst_trade_R": round(float(r.min()), 2),
        "exits": dict(Counter(t["exit_reason"] for t in trades)),
    }


def main():
    m15 = _load("15m")
    h1  = _load("1h")
    h4  = _load("4h")

    for label, kwargs, payout in CONFIGS:
        if payout is not None:
            engine.PAYOUT_PCT = payout
        t0 = time.time()
        trades, _ = run_backtest(m15, h1, h4, label=label, **kwargs)
        print(f"\n[{label}] completed in {(time.time() - t0)/60:.1f} min")
        summary = _summarize(trades, label, starting_balance=STARTING_BALANCE)
        if trades:
            summary.update(_r_stats(trades))
        print_result(summary)


if __name__ == "__main__":
    main()
