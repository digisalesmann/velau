"""
backtest/mtf_confirm_test.py — tests requiring RSI+MACD to ALSO agree on
the 1H timeframe, not just 15M, alongside the current production
_score()/CALL-only logic. RSI and MACD are the only two factors
backtesting confirmed carry real signal — this tests a stricter,
higher-conviction version of exactly those two, rather than reintroducing
different indicators.

Run: python3 -m backtest.mtf_confirm_test
"""
import time
from backtest.engine import _load, run_backtest, _summarize, print_result, STARTING_BALANCE


def main():
    m15 = _load("15m")
    h1  = _load("1h")
    h4  = _load("4h")

    t0 = time.time()
    trades, equity_curve = run_backtest(
        m15, h1, h4, label="mtf_confirm", require_1h_confirmation=True,
    )
    print(f"[mtf_confirm] completed in {(time.time()-t0)/60:.1f} min")
    print_result(_summarize(trades, "mtf_confirm", starting_balance=STARTING_BALANCE))


if __name__ == "__main__":
    main()
