"""
backtest/regime_filters.py — can a trend-strength filter keep the 4H trend
strategy out of sideways (and bear) markets?

The live strategy only enters when gold's previous daily close is above its
200-day SMA. That already keeps losses small in bear markets, but sideways
years still cost ~2R/yr: gold keeps poking above recent highs and falling
back. Four textbook trend-strength filters, each added ON TOP of that rule,
with standard parameters chosen before running (not tuned):

  sma200_rising   the 200-day SMA is higher than 20 trading days ago
  stacked_mas     close > SMA50 > SMA200
  adx_20          daily ADX(14) > 20 (a trend, not a range)
  efficiency_0.3  Kaufman efficiency ratio(20) > 0.3: the last 20 days'
                  net move is >30% of the total distance travelled

Same costs and Deriv stop cap as live (0.75%). Caveat: every period here
has been seen during earlier research, so an improvement is suggestive, not
proof; the forward demo test is the real check.

Run: python3 -m backtest.regime_filters
"""
import numpy as np
import pandas as pd
from ta.trend import ADXIndicator

import backtest.edge_research as er
import backtest.trend_candidate as tc

MAX_STOP_PCT = 0.0075
YEARS = range(2011, 2027)


def daily_features() -> pd.DataFrame:
    d = er.load("1d")
    c = d["close"]
    d["sma50"] = c.rolling(50).mean()
    d["sma200"] = c.rolling(200).mean()
    d["adx"] = ADXIndicator(d["high"], d["low"], c, window=14).adx()
    d["er20"] = (c - c.shift(20)).abs() / c.diff().abs().rolling(20).sum()
    return d


FILTERS = {
    "current (close>SMA200)": lambda d: d.close > d.sma200,
    "+ sma200_rising":        lambda d: (d.close > d.sma200) & (d.sma200 > d.sma200.shift(20)),
    "+ stacked_mas":          lambda d: (d.close > d.sma50) & (d.sma50 > d.sma200),
    "+ adx_20":               lambda d: (d.close > d.sma200) & (d.adx > 20),
    "+ efficiency_0.3":       lambda d: (d.close > d.sma200) & (d.er20 > 0.3),
}


def regime_array(h4: pd.DataFrame, d: pd.DataFrame, rule) -> np.ndarray:
    """Per 4H bar: the rule evaluated on the PREVIOUS completed day."""
    ok = pd.Series(rule(d).fillna(False).to_numpy(), index=d["t"].dt.normalize())
    prev = ok.shift(1)
    return h4["t"].dt.normalize().map(prev).fillna(False).astype(bool).to_numpy()


def market_type(chg: float) -> str:
    return "bear" if chg <= -0.08 else "sideways" if abs(chg) < 0.08 else "bull"


def main():
    h4 = er.load("4h")
    d = daily_features()
    yearly = d.groupby(d["t"].dt.year)["close"].agg(["first", "last"])
    kinds = {y: market_type(r["last"] / r["first"] - 1) for y, r in yearly.iterrows()}
    tc.STOP_LIMIT = {"max_stop_pct": MAX_STOP_PCT, "stop_mode": "cap"}

    rows = []
    for name, rule in FILTERS.items():
        tc.REGIME = regime_array(h4, d, rule)
        trades = pd.DataFrame(tc.candidate_trades(h4), columns=["t", "R"])
        trades = trades[pd.to_datetime(trades.t).dt.year.isin(YEARS)]
        trades["yr"] = pd.to_datetime(trades.t).dt.year
        by_year = trades.groupby("yr").R.sum().reindex(YEARS, fill_value=0.0)
        eq = trades.R.cumsum()
        row = {"filter": name, "trades/yr": round(len(trades) / len(YEARS), 0),
               "total R": round(by_year.sum(), 1),
               "max DD R": round(float((eq.cummax() - eq).max()), 1),
               "losing yrs": int((by_year < 0).sum())}
        for k in ("bull", "sideways", "bear"):
            ys = [y for y in YEARS if kinds.get(y) == k]
            row[f"{k} R/yr"] = round(by_year[ys].mean(), 1) if ys else None
        row["all R/yr"] = round(by_year.mean(), 1)
        rows.append(row)
        print(f"{name}: " + ", ".join(f"{y}:{v:+.1f}" for y, v in by_year.items()))

    print("\nyears by type:", {k: [y for y in YEARS if kinds.get(y) == k] for k in ("bull", "sideways", "bear")})
    print(pd.DataFrame(rows).set_index("filter").to_string())


if __name__ == "__main__":
    main()
