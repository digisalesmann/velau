"""
backtest/ablation.py — re-runs the full-year backtest with specific
confluence factors excluded, to test whether the factors that looked
inverted/flat in analyze.py's per-factor breakdown are actually hurting
the strategy or whether that breakdown was just noise.

Run: python3 -m backtest.ablation <factor1> [factor2 ...]
     python3 -m backtest.ablation f8_pivot
     python3 -m backtest.ablation f6_adx_di f7_bos f8_pivot
"""
import sys
import time
from backtest.engine import _load, run_backtest, _summarize, print_result, STARTING_BALANCE


def main():
    args = sys.argv[1:]
    min_conf = None
    call_only = False
    while args and args[0].startswith("--"):
        if args[0].startswith("--min-confluence="):
            min_conf = int(args[0].split("=", 1)[1])
        elif args[0] == "--call-only":
            call_only = True
        args = args[1:]
    factors = args
    if not factors:
        print("Usage: python3 -m backtest.ablation [--min-confluence=N] [--call-only] <factor1> [factor2 ...]")
        return

    m15 = _load("15m")
    h1  = _load("1h")
    h4  = _load("4h")

    label = "excl_" + "+".join(factors) + (f"_minconf{min_conf}" if min_conf else "") + ("_callonly" if call_only else "")
    t0 = time.time()
    trades, equity_curve = run_backtest(
        m15, h1, h4, label=label, exclude_factor=factors, min_confluence=min_conf,
        call_only=call_only,
    )
    print(f"[{label}] completed in {(time.time()-t0)/60:.1f} min")
    print_result(_summarize(trades, label, starting_balance=STARTING_BALANCE))


if __name__ == "__main__":
    main()
