"""
scripts/trade_audit.py — read-only trade-performance audit against the live
production database.

Deliberately uses a SEPARATE, narrowly-scoped Postgres role
(TRADING_READONLY_DATABASE_URL) rather than the app's own DATABASE_URL —
that role only has SELECT on trade_results/signals (see the setup notes
this script was introduced with for the exact `CREATE ROLE` SQL), so this
tool is physically incapable of writing to the database or reading
sensitive tables (users' password hashes, live Deriv broker tokens,
payment/subscription records) even if misused.

The connection string is read from .env and never printed or logged.

Run:
  python3 -m scripts.trade_audit                  # summary + last 50 trades
  python3 -m scripts.trade_audit --limit 200       # more trades
  python3 -m scripts.trade_audit --since 2026-08-01
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

READONLY_URL = os.getenv("TRADING_READONLY_DATABASE_URL", "")

# The current strategy (unanimous 4-factor confluence, CALL-only, narrowed
# session, 1H confirmation) went live on this date — the default audit
# window starts here so old pre-overhaul trades don't dilute the comparison
# against the 62.3% backtested win rate.
STRATEGY_LIVE_SINCE = "2026-07-30"


def _connect():
    if not READONLY_URL:
        print(
            "TRADING_READONLY_DATABASE_URL is not set in .env.\n"
            "This should be a dedicated read-only Postgres role's connection "
            "string, not the app's own DATABASE_URL — see the setup steps "
            "this script was introduced with."
        )
        sys.exit(1)
    import psycopg2
    import psycopg2.extras

    return psycopg2.connect(READONLY_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def summary(conn, since: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN won THEN 1 ELSE 0 END) AS wins,
                ROUND(100.0 * SUM(CASE WHEN won THEN 1 ELSE 0 END)
                      / NULLIF(COUNT(*), 0), 1) AS win_rate_pct,
                ROUND(SUM(pnl)::numeric, 2) AS total_pnl,
                ROUND(AVG(pnl)::numeric, 3) AS avg_pnl_per_trade,
                MIN(timestamp) AS first_trade,
                MAX(timestamp) AS last_trade
            FROM trade_results
            WHERE timestamp >= %s
            """,
            (since,),
        )
        return cur.fetchone()


def recent_trades(conn, limit: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, contract_id, won, pnl, symbol, username, timestamp
            FROM trade_results
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=STRATEGY_LIVE_SINCE,
                         help=f"Only summarize trades on/after this date (default: {STRATEGY_LIVE_SINCE}).")
    parser.add_argument("--limit", type=int, default=50,
                         help="How many recent individual trades to list (default: 50).")
    args = parser.parse_args()

    conn = _connect()
    try:
        s = summary(conn, args.since)
        print("=" * 64)
        print(f"LIVE TRADE SUMMARY since {args.since}")
        print("=" * 64)
        if not s or not s["total_trades"]:
            print("No trades recorded in this window.")
        else:
            for k, v in s.items():
                print(f"  {k:20s}: {v}")

        print()
        print("=" * 64)
        print(f"LAST {args.limit} TRADES")
        print("=" * 64)
        trades = recent_trades(conn, args.limit)
        if not trades:
            print("No trades found.")
        for t in trades:
            outcome = "WIN " if t["won"] else "LOSS"
            pnl = t["pnl"] if t["pnl"] is not None else 0.0
            print(
                f"  {t['timestamp']}  {outcome}  pnl={pnl:+.2f}  "
                f"{t['symbol']}  user={t['username']}"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
