"""
backtest/fetch_history.py — pulls and caches historical XAU/USD candles from
Deriv's own API (not a third-party price feed) so the backtest replays the
exact same quotes the live bot would have seen and traded against.

Walks `ticks_history` backward via the `end=` param, same mechanism already
used live in strategy_engine.py's `_get_candles` batch-stitching — just
repeated many more times to build up months of history instead of one extra
batch. Each response is capped by Deriv at roughly 550-770 candles
regardless of the requested `count`, so depth is a function of how many
batches you walk back, not the `count` param.

Run directly to (re)build the local cache:
    python3 -m backtest.fetch_history
"""
import asyncio
import datetime
import sys
import os
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.deriv_trading_service import DerivTradingService

SYMBOL     = "frxXAUUSD"
DATA_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GRANULARITIES = {900: "15m", 3600: "1h", 14400: "4h"}


async def fetch_granularity(svc: DerivTradingService, gran: int, target_days: int) -> pd.DataFrame:
    end = "latest"
    prev_oldest = None
    rows = []
    while True:
        raw = await svc.get_candles(SYMBOL, count=5000, granularity=gran, end=end)
        if not raw:
            break
        oldest = raw[0]["epoch"]
        if oldest == prev_oldest:
            break  # hit the server's actual retention wall
        rows.extend(raw)
        min_epoch = min(r["epoch"] for r in rows)
        max_epoch = max(r["epoch"] for r in rows)
        span_days = (max_epoch - min_epoch) / 86400
        if span_days >= target_days:
            break
        prev_oldest = oldest
        end = oldest - gran
        await asyncio.sleep(0.3)  # polite pacing — avoid hammering Deriv's API

    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df.drop_duplicates(subset=["epoch"], keep="last", inplace=True)
    df.sort_values("epoch", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


async def main(target_days: int = 365):
    os.makedirs(DATA_DIR, exist_ok=True)
    svc = DerivTradingService(max_retries=2)
    await svc.authenticate()
    try:
        for gran, label in GRANULARITIES.items():
            print(f"Fetching {label} candles (target {target_days} days)...")
            df = await fetch_granularity(svc, gran, target_days)
            path = os.path.join(DATA_DIR, f"{SYMBOL}_{label}.csv")
            df.to_csv(path, index=False)
            oldest = datetime.datetime.fromtimestamp(df["epoch"].min(), datetime.UTC)
            newest = datetime.datetime.fromtimestamp(df["epoch"].max(), datetime.UTC)
            print(f"  {label}: {len(df)} candles | {oldest} -> {newest} | saved to {path}")
    finally:
        await svc.close()


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    asyncio.run(main(days))
