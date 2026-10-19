"""
backtest/short_side.py — does a mirror-image SHORT side add to the live
long-only 4H trend strategy?

Short rule (fixed before running, the exact mirror of the long side):
  entry   a completed 4H close below the lowest low of the previous 20/55
          bars, only while the daily averages are stacked DOWN on the
          previous day: close < SMA50 < SMA200
  stop    stop_mult (2/3) x ATR above entry, capped at 0.75% of price,
          trailed down the same distance; Deriv MULTDOWN costs, no swap
Long side unchanged (stacked UP). Reports each side alone and combined, by
year and by market type, so the question answered is "does adding shorts
improve the whole", not just "do shorts make money".

Caveat: all of this history has been seen in earlier research, so treat a
positive result as a reason to forward-test on demo, not as proof.

Run: python3 -m backtest.short_side
"""
import pandas as pd

import backtest.edge_research as er
import backtest.regime_filters as rf
import backtest.trend_candidate as tc

YEARS = range(2011, 2027)
STOP = {"max_stop_pct": 0.0075, "stop_mode": "cap"}
UP = rf.FILTERS["+ stacked_mas"]
DOWN = lambda d: (d.close < d.sma50) & (d.sma50 < d.sma200)


def sleeves(h4, regime, short: bool):
    out = []
    for v in tc.VARIANTS:
        kw = dict(v, longs_only=not short, shorts_only=short)
        out += [(pd.Timestamp(t), r / len(tc.VARIANTS))
                for t, r in er.f3_swing_trend(h4, tc.VENUE, regime_ok=regime, **STOP, **kw)]
    return pd.DataFrame(out, columns=["t", "R"])


def summarize(df: pd.DataFrame, kinds: dict) -> dict:
    df = df[df.t.dt.year.isin(YEARS)]
    by_year = df.groupby(df.t.dt.year).R.sum().reindex(YEARS, fill_value=0.0)
    eq = df.sort_values("t").R.cumsum()
    out = {"trades/yr": round(len(df) / len(YEARS)), "win%": f"{(df.R > 0).mean():.0%}",
           "R/yr": round(by_year.mean(), 1), "max DD R": round(float((eq.cummax() - eq).max()), 1),
           "losing yrs": int((by_year < 0).sum())}
    for k in ("bull", "sideways", "bear"):
        ys = [y for y in YEARS if kinds.get(y) == k]
        out[f"{k}"] = round(by_year[ys].mean(), 1)
    return out, by_year


def main():
    h4 = er.load("4h"); d = rf.daily_features()
    yearly = d.groupby(d.t.dt.year).close.agg(["first", "last"])
    kinds = {y: rf.market_type(r["last"] / r["first"] - 1) for y, r in yearly.iterrows()}
    longs = sleeves(h4, rf.regime_array(h4, d, UP), short=False)
    shorts = sleeves(h4, rf.regime_array(h4, d, DOWN), short=True)
    both = pd.concat([longs, shorts])
    rows, years = {}, {}
    for name, df in (("long only (live)", longs), ("short only", shorts), ("long + short", both)):
        rows[name], years[name] = summarize(df, kinds)
    print(pd.DataFrame(rows).T.to_string())
    print("\nR by year:")
    yt = pd.DataFrame(years).round(1)
    yt["gold"] = [f"{yearly.loc[y, 'last'] / yearly.loc[y, 'first'] - 1:+.0%}" for y in yt.index]
    yt["type"] = [kinds[y] for y in yt.index]
    print(yt.to_string())


if __name__ == "__main__":
    main()
