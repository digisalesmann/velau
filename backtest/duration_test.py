"""
backtest/duration_test.py — tests whether the same entry signal (current
production _score()/CALL-only logic, unchanged) performs better held for
longer than the current fixed 15-minute contract duration.

Run: python3 -m backtest.duration_test
"""
import time
from backtest.engine import _load, run_backtest, _summarize, print_result, STARTING_BALANCE

# hold_bars -> real duration, in 15-min-candle units
DURATIONS = {1: "15 min", 2: "30 min", 4: "60 min", 8: "120 min"}


def main():
    m15 = _load("15m")
    h1  = _load("1h")
    h4  = _load("4h")

    for hold_bars, label in DURATIONS.items():
        t0 = time.time()
        trades, equity_curve = run_backtest(m15, h1, h4, label=f"hold_{label}", hold_bars=hold_bars)
        elapsed = time.time() - t0
        print(f"\n[{label}] completed in {elapsed/60:.1f} min")
        print_result(_summarize(trades, f"hold_{label}", starting_balance=STARTING_BALANCE))


if __name__ == "__main__":
    main()
