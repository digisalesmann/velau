"""
backtest/session_test.py — tests narrowing the trading session to the hours
that showed the strongest win rate in analyze.py's hour-of-day breakdown
(14:00-17:00 UTC, and 11:00-12:00 UTC), checked against both halves of the
year separately so a promising-looking aggregate isn't trusted on the
strength of one lucky stretch alone.

Run: python3 -m backtest.session_test
"""
import time
from backtest.engine import _load, run_backtest, _summarize, print_result, STARTING_BALANCE

CANDIDATE_SESSIONS = [(11, 12), (14, 17)]


def main():
    m15 = _load("15m")
    h1  = _load("1h")
    h4  = _load("4h")

    t0 = time.time()
    trades, equity_curve = run_backtest(
        m15, h1, h4, label="narrow_sessions", sessions=CANDIDATE_SESSIONS,
    )
    print(f"[narrow_sessions] completed in {(time.time()-t0)/60:.1f} min")
    print_result(_summarize(trades, "narrow_sessions", starting_balance=STARTING_BALANCE))

    if len(trades) >= 10:
        mid = len(trades) // 2
        first, second = trades[:mid], trades[mid:]
        bal_before_second = first[-1]["balance"] if first else STARTING_BALANCE
        print_result(_summarize(first, "narrow_first_half", starting_balance=STARTING_BALANCE))
        print_result(_summarize(second, "narrow_second_half", starting_balance=bal_before_second))


if __name__ == "__main__":
    main()
