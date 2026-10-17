"""
backtest/build_hist.py — builds backtest/data/hist_xau_{15m,1h,4h,1d}.csv from
HistData.com's free XAU/USD 1-minute ASCII zips (one per year; one per month
for the current year), downloaded manually or by script into a folder.

HistData timestamps are New York local time WITH daylight saving (their docs
say fixed EST, but aligning against Deriv's own frxXAUUSD candles only
matches at 94%+ when DST is applied, and drops to ~52% in summer without it).

Run: python3 -m backtest.build_hist <folder-with-zips>
"""
import glob
import io
import os
import sys
import zipfile
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def main(src: str):
    frames = []
    for z in sorted(glob.glob(os.path.join(src, "*.zip"))):
        with zipfile.ZipFile(z) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv") and "/" not in n and ".." not in n]
            frames.append(pd.read_csv(io.BytesIO(zf.read(names[0])), sep=";", header=None,
                                      names=["ts", "open", "high", "low", "close", "vol"]))
    m1 = pd.concat(frames)
    t = pd.to_datetime(m1.ts, format="%Y%m%d %H%M%S").dt.tz_localize(
        "America/New_York", ambiguous="NaT", nonexistent="NaT")
    m1 = m1.assign(t=t.dt.tz_convert("UTC").dt.tz_localize(None)).dropna(subset=["t"])
    m1 = m1.drop_duplicates("t").set_index("t").sort_index()[["open", "high", "low", "close"]]
    print(f"1m bars: {len(m1)}  {m1.index[0]} -> {m1.index[-1]}")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    for rule, label in (("15min", "15m"), ("1h", "1h"), ("4h", "4h"), ("1D", "1d")):
        bars = m1.resample(rule).agg(agg).dropna()
        bars.insert(0, "epoch", (bars.index - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1))
        bars.to_csv(os.path.join(DATA_DIR, f"hist_xau_{label}.csv"), index=False)
        print(f"  {label}: {len(bars)} bars")


if __name__ == "__main__":
    main(sys.argv[1])
