"""
backtest/analyze.py — runs the full-year backtest once and breaks down win
rate by individual confluence factor, score level, direction, and hour of
day, to find out *why* the strategy backtests below breakeven rather than
just re-confirming that it does.

Run: python3 -m backtest.analyze
"""
import time
from backtest.engine import (
    _load, run_backtest, _summarize, print_result, FACTOR_NAMES, STARTING_BALANCE,
)


def factor_breakdown(trades: list):
    print("\n" + "=" * 60)
    print("WIN RATE BY FACTOR AGREEMENT (does this factor voting")
    print("with the trade's direction predict a win?)")
    print("=" * 60)
    for factor in FACTOR_NAMES:
        agree, disagree_or_neutral = [], []
        for t in trades:
            vote = t["votes"][factor]
            trade_side = "bull" if t["direction"] == "CALL" else "bear"
            (agree if vote == trade_side else disagree_or_neutral).append(t)
        for group_name, group in [("agrees", agree), ("neutral/against", disagree_or_neutral)]:
            n = len(group)
            wr = (sum(1 for t in group if t["won"]) / n * 100) if n else float("nan")
            print(f"  {factor:16s} {group_name:16s} n={n:5d}  win_rate={wr:5.1f}%")


def score_breakdown(trades: list):
    print("\n" + "=" * 60)
    print("WIN RATE BY CONFLUENCE SCORE LEVEL")
    print("=" * 60)
    scores = sorted(set(t["score"] for t in trades))
    for s in scores:
        group = [t for t in trades if t["score"] == s]
        n = len(group)
        wr = sum(1 for t in group if t["won"]) / n * 100
        print(f"  score={s}/4  n={n:5d}  win_rate={wr:5.1f}%")


def direction_breakdown(trades: list):
    print("\n" + "=" * 60)
    print("WIN RATE BY DIRECTION")
    print("=" * 60)
    for d in ("CALL", "PUT"):
        group = [t for t in trades if t["direction"] == d]
        n = len(group)
        if n == 0:
            continue
        wr = sum(1 for t in group if t["won"]) / n * 100
        print(f"  {d:5s} n={n:5d}  win_rate={wr:5.1f}%")


def hour_breakdown(trades: list):
    print("\n" + "=" * 60)
    print("WIN RATE BY HOUR OF DAY (UTC)")
    print("=" * 60)
    hours = sorted(set(t["time"].hour for t in trades))
    for h in hours:
        group = [t for t in trades if t["time"].hour == h]
        n = len(group)
        wr = sum(1 for t in group if t["won"]) / n * 100
        print(f"  {h:02d}:00-{h+1:02d}:00 UTC  n={n:5d}  win_rate={wr:5.1f}%")


def main():
    m15 = _load("15m")
    h1  = _load("1h")
    h4  = _load("4h")

    t0 = time.time()
    trades, equity_curve = run_backtest(m15, h1, h4, label="full_period")
    print(f"Backtest completed in {(time.time()-t0)/60:.1f} min | {len(trades)} trades")

    print_result(_summarize(trades, "full_period", starting_balance=STARTING_BALANCE))
    factor_breakdown(trades)
    score_breakdown(trades)
    direction_breakdown(trades)
    hour_breakdown(trades)

    # Persist for reuse by ablation runs / further analysis without re-scanning.
    import pickle
    with open("backtest/data/last_run_trades.pkl", "wb") as f:
        pickle.dump(trades, f)


if __name__ == "__main__":
    main()
