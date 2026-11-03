# App status shown on the dashboard: weekend market closure, per-user
# "trade open", and the funding minimums.

import os
import uuid
from datetime import datetime, timezone

os.environ.pop("DATABASE_URL", None)

import database as db
import main
import position_sizing as ps
from core import trend_strategy as ts
from core.strategy_engine import gold_weekend_reopen


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def test_weekend_closure_follows_new_york_time():
    assert gold_weekend_reopen(_utc("2026-11-06 21:30")) is None           # Fri, open until 22:00 UTC (EST)
    assert gold_weekend_reopen(_utc("2026-11-06 22:00")) == _utc("2026-11-08 22:00")
    assert gold_weekend_reopen(_utc("2026-11-07 12:00")) == _utc("2026-11-08 22:00")
    assert gold_weekend_reopen(_utc("2026-11-08 22:00")) is None
    assert gold_weekend_reopen(_utc("2026-07-10 21:00")) == _utc("2026-07-12 21:00")  # summer (EDT)
    assert gold_weekend_reopen(_utc("2026-11-04 03:00")) is None           # midweek


def test_trade_open_is_per_user():
    a, b = (f"st-{uuid.uuid4().hex[:8]}@example.com" for _ in range(2))
    db.insert_trend_position(f"tp-{a}", a, "S20x2", 1, 4000.0, 2.0, 4030.0, 20.0, 26.0, 100, 1)
    assert main._user_trade_open(a) is True
    assert main._user_trade_open(b) is False


def test_min_balance_matches_what_the_bot_can_size():
    price, atr = 4000.0, 30.0
    stop = min(2 * atr, ts.MAX_STOP_PCT * price)

    def tradable(balance):
        risk = ps.trend_risk_amount(balance) / ts.SLEEVES_PER_SIDE
        try:
            ps.plan_multiplier_trade(risk, atr, price, stop_atr_mult=stop / atr, rr=None)
            return True
        except ValueError:
            return False

    assert tradable(ps.MIN_TREND_BALANCE)
    assert not tradable(200)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
