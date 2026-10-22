"""
backtest/risk_rules.py — what do per-trade risk % and a daily loss limit do
to the live trend strategy's results, with compounding?

Simulates in time order: each signal risks `risk_pct` of the CURRENT balance
(split across its 4 sleeves), a trade's P&L is its R x that dollar risk, and
when losses realised during a UTC day reach `daily_limit` of the day's
opening balance, no new trades open until the next day (open ones keep
running, as the live circuit breaker does).

Run: python3 -m backtest.risk_rules
"""
import pandas as pd

import backtest.edge_research as er
import backtest.regime_filters as rf
import backtest.trend_candidate as tc
import backtest.short_side as ss

START_BALANCE = 8247.18


def all_trades(h4, d):
    rows = []
    for rule, short in ((ss.UP, False), (ss.DOWN, True)):
        regime = rf.regime_array(h4, d, rule)
        for v in tc.VARIANTS:
            kw = dict(v, longs_only=not short, shorts_only=short)
            for t, r, te in er.f3_swing_trend(h4, tc.VENUE, regime_ok=regime, with_exit=True, **ss.STOP, **kw):
                rows.append((pd.Timestamp(t), r / len(tc.VARIANTS), pd.Timestamp(te)))
    df = pd.DataFrame(rows, columns=["entry", "R", "exit"])
    return df[df.entry >= "2011-01-01"].sort_values("entry").reset_index(drop=True)


def run(trades, risk_pct, daily_limit=None, cap_usd=None):
    bal = START_BALANCE
    events = [(r.entry, 1, i) for i, r in trades.iterrows()] + [(r.exit, 0, i) for i, r in trades.iterrows()]
    events.sort(key=lambda e: (e[0], e[1]))       # exits before entries at the same time
    taken, risk_usd, day, day_open, day_pnl, blocked = set(), {}, None, bal, 0.0, 0
    peak, max_dd, yearly = bal, 0.0, {}
    for t, is_entry, i in events:
        if t.date() != day:
            day, day_open, day_pnl = t.date(), bal, 0.0
        if is_entry:
            if daily_limit is not None and day_pnl <= -daily_limit * day_open:
                blocked += 1
                continue
            unit = bal * risk_pct
            if cap_usd is not None:
                unit = min(unit, cap_usd)
            risk_usd[i] = unit
            taken.add(i)
        elif i in taken:
            pnl = trades.R[i] * risk_usd[i]
            bal += pnl; day_pnl += pnl
            yearly.setdefault(t.year, [None, 0.0])
            peak = max(peak, bal); max_dd = max(max_dd, (peak - bal) / peak)
            yearly[t.year][1] = bal
    yrs = (trades.exit.max() - trades.entry.min()).days / 365.25
    ends = sorted((y, v[1]) for y, v in yearly.items())
    prev, ret = START_BALANCE, {}
    for y, end in ends:
        ret[y] = end / prev - 1; prev = end
    return {"final": bal, "cagr": (bal / START_BALANCE) ** (1 / yrs) - 1, "max_dd": max_dd,
            "blocked": blocked, "worst_year": min(ret.values()), "y2026": ret.get(2026), "losing_years": sum(v < 0 for v in ret.values())}


def main():
    h4 = er.load("4h"); d = rf.daily_features()
    trades = all_trades(h4, d)
    worst_day = trades.groupby(trades.exit.dt.date).R.sum().min()
    print(f"{len(trades)} trades 2011-2026 | worst single day realised: {worst_day:.2f}R")
    configs = [
        ("now: ~0.6% ($50 cap), 10% daily", 0.02, 0.10, 50.0),
        ("1% risk, no daily limit", 0.01, None, None),
        ("1% risk, 3% daily limit", 0.01, 0.03, None),
        ("2% risk, 3% daily limit", 0.02, 0.03, None),
    ]
    print(f"{'config':36} {'$8,247 ->':>12} {'per yr':>7} {'worst DD':>9} {'worst yr':>9} {'2026':>7} {'losing yrs':>10} {'entries blocked':>15}")
    for name, rp, dl, cap in configs:
        o = run(trades, rp, dl, cap)
        print(f"{name:36} {o['final']:>12,.0f} {o['cagr']:>+7.1%} {-o['max_dd']:>+9.1%} {o['worst_year']:>+9.1%} {o['y2026']:>+7.1%} {o['losing_years']:>10} {o['blocked']:>15}")


if __name__ == "__main__":
    main()
