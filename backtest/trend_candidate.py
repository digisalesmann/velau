"""
backtest/trend_candidate.py — decides whether the one promising lead from
backtest/edge_research.py (F3, 4H swing trend) has real timing skill or is
just riding gold's rise, then gives it its single run on the TEST hold-out.

Locked BEFORE looking at TEST:
  - Candidate = equal-weight ensemble of all four long-only F3 variants
    (lookback 20/55 x stop 2/3 ATR). Using all four avoids picking the
    variant that happened to look best on VAL.
  - Costs = Deriv Multiplier, no swap (Deriv states gold Multipliers carry
    no overnight swap fees): $0.10 slippage + 0.026% commission.
  - Beta control = random long entries at the same frequency, with the same
    ATR stop and trailing exit, repeated N_RANDOM times. The candidate
    only shows timing skill if it beats most of those random runs.

Run: python3 -m backtest.trend_candidate            (as locked)
     python3 -m backtest.trend_candidate --regime   (+ daily 200-SMA regime
                                                    filter, added after the
                                                    BEAR period failed)
"""
import numpy as np
import pandas as pd

import backtest.edge_research as er

er.COSTS["deriv_mult_noswap"] = {"fixed": 0.10, "pct": 0.00026, "swap_long": 0.0, "swap_short": 0.0}
VENUE = "deriv_mult_noswap"
# Gold's 2011-2015 bear market (peak ~$1,900 in Sep 2011 to ~$1,050 in Dec
# 2015): every other period here is a bull market, so this is the test of
# whether the trend filter actually stands aside when gold falls.
er.PERIODS["BEAR"] = ("2011-09-01", "2016-01-01")
VARIANTS = [dict(lookback=lb, stop_mult=sm, longs_only=True) for lb in (20, 55) for sm in (2.0, 3.0)]
N_RANDOM = 300
SEED = 7


def daily_uptrend(h4):
    """Regime filter, the textbook one with no tuning: gold's previous daily
    close is above its 200-day simple moving average. Uses only completed
    days, so a 4H bar never sees its own day's close."""
    d = er.load("1d")
    sma = d["close"].rolling(200).mean()
    up = pd.Series((d["close"] > sma).to_numpy(), index=d["t"].dt.normalize())
    prev_day_up = up.shift(1)
    day = h4["t"].dt.normalize()
    return day.map(prev_day_up).fillna(False).astype(bool).to_numpy()


REGIME = None  # set in main() when --regime is passed
STOP_LIMIT = {}  # e.g. {"max_stop_pct": 0.0157, "stop_mode": "skip"}, set in main()


def candidate_trades(h4):
    """Each variant is a sleeve risking 1/len(VARIANTS) R per trade."""
    out = []
    for v in VARIANTS:
        out += [(t, r / len(VARIANTS)) for t, r in er.f3_swing_trend(h4, VENUE, regime_ok=REGIME, **STOP_LIMIT, **v)]
    return sorted(out)


def random_long_trades(h4, rng, stop_mult, entries_per_bar):
    """Random long entries with the F3 exit logic (ATR stop + chandelier trail)."""
    c = h4["close"].to_numpy(); a = er.atr(h4)
    out, k = [], 70
    while k < len(h4) - 1:
        if rng.random() >= entries_per_bar or (REGIME is not None and not REGIME[k]):
            k += 1
            continue
        risk = stop_mult * a[k]
        j, px, _ = er.simulate(h4, k, 1, c[k] - risk, None, k + 6 * 60, trail_atr=a, trail_mult=stop_mult)
        out.append((h4["t"].iloc[k], er.trade_r(h4, k, j, 1, c[k], px, risk, VENUE)))
        k = j + 1
    return out


def random_ensemble(h4, rng, freq):
    out = []
    for v in VARIANTS:
        p = freq[(v["lookback"], v["stop_mult"])]
        out += [(t, r / len(VARIANTS)) for t, r in random_long_trades(h4, rng, v["stop_mult"], p)]
    return sorted(out)


def total_r(trades, period):
    return sum(r for _, r in er.slice_trades(trades, period))


def main(regime: bool = False, stop_limit: dict | None = None):
    global REGIME, STOP_LIMIT
    STOP_LIMIT = stop_limit or {}
    if STOP_LIMIT:
        print(f"Deriv stop limit: {STOP_LIMIT}")
    h4 = er.load("4h")
    if regime:
        REGIME = daily_uptrend(h4)
        print(f"Regime filter ON: daily close > 200-day SMA ({REGIME.mean():.0%} of 4H bars eligible)")
    cand = candidate_trades(h4)

    # Calibrate each random sleeve's per-bar entry probability so it takes
    # about as many trades as its real variant (open positions block new
    # entries, so this can't be read off trades / bars directly).
    freq = {}
    for v in VARIANTS:
        target = len(er.f3_swing_trend(h4, VENUE, regime_ok=REGIME, **STOP_LIMIT, **v))
        lo, hi = 1e-4, 0.5
        for _ in range(12):
            mid = (lo * hi) ** 0.5
            n = np.mean([len(random_long_trades(h4, np.random.default_rng(s), v["stop_mult"], mid))
                         for s in range(3)])
            lo, hi = (mid, hi) if n < target else (lo, mid)
        freq[(v["lookback"], v["stop_mult"])] = (lo * hi) ** 0.5
        print(f"   {v}: {target} real trades, random entry prob {freq[(v['lookback'], v['stop_mult'])]:.4f}/bar")

    rng = np.random.default_rng(SEED)
    rand = {p: [] for p in ("BEAR", "DEV", "VAL", "TEST")}
    for _ in range(N_RANDOM):
        rt = random_ensemble(h4, rng, freq)
        for p in rand:
            rand[p].append(total_r(rt, p))

    print(f"Candidate: equal-weight ensemble of {len(VARIANTS)} long-only 4H trend variants, Deriv Multiplier costs, no swap\n")
    for p in ("BEAR", "DEV", "VAL", "TEST"):
        s = er.stats(er.slice_trades(cand, p))
        a, b = (pd.Timestamp(x) for x in er.PERIODS[p])
        yrs = (b - a).days / 365.25
        c_r = total_r(cand, p)
        rr = np.array(rand[p])
        pct = (rr < c_r).mean()
        gold = h4[(h4.t >= a) & (h4.t < b)].close
        print(f"[{p}] {er.PERIODS[p][0]} -> {er.PERIODS[p][1]}  (gold {gold.iloc[0]:.0f} -> {gold.iloc[-1]:.0f}, {gold.iloc[-1]/gold.iloc[0]-1:+.0%})")
        print(f"   candidate:     {c_r:+6.1f}R total ({c_r/yrs:+.1f}R/yr) | {s}")
        print(f"   random longs:  median {np.median(rr):+6.1f}R | 95th pct {np.percentile(rr, 95):+6.1f}R")
        print(f"   candidate beats {pct:.0%} of random-entry runs  -> {'TIMING SKILL' if pct >= 0.95 else 'not distinguishable from riding the trend'}\n")


if __name__ == "__main__":
    import sys
    limit = None
    for arg in sys.argv:
        if arg.startswith("--stop-limit="):          # e.g. --stop-limit=skip:0.0157
            mode, pct = arg.split("=", 1)[1].split(":")
            limit = {"stop_mode": mode, "max_stop_pct": float(pct)}
    main(regime="--regime" in sys.argv, stop_limit=limit)
