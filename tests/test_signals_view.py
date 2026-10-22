# /signals per-user view: one row per trend bar, executed per account.

import os
from datetime import datetime, timezone

os.environ.pop("DATABASE_URL", None)

import main

BAR = int(datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc).timestamp())


def _row(i, typ, hh, mm, executed=None, features=None):
    return {"id": i, "type": typ, "timestamp": datetime(2026, 10, 22, hh, mm), "executed": executed,
            "features_json": features, "reason": "r"}


ROWS = [  # newest first, as the DB returns them (the real rows of 2026-10-22)
    _row(6566, "SELL", 6, 35), _row(6565, "SELL", 6, 32), _row(6564, "SELL", 4, 0),
    _row(6563, "NEUTRAL", 0, 4),
]


def test_duplicates_collapse_and_executed_is_per_user():
    mine = main._signals_for_user(ROWS, {BAR: [{"sleeve": "S20x2"}, {"sleeve": "S20x3"}]}, 30)
    assert [r["id"] for r in mine] == [6566, 6563]
    assert mine[0]["executed"] is True and mine[0]["trades_opened"] == 2
    other = main._signals_for_user(ROWS, {}, 30)
    assert other[0]["executed"] is False and other[0]["trades_opened"] == 0


def test_bar_from_features_json_wins():
    r = _row(1, "BUY", 9, 0, features='{"bar_epoch": %d}' % BAR)
    assert main._signal_bar(r) == BAR


def test_legacy_confluence_signals_untouched():
    old = {"id": 5, "type": "BUY", "timestamp": datetime(2026, 8, 1, 12, 0), "executed": True,
           "features_json": None, "reason": "[4/4] x"}
    assert main._signals_for_user([old, dict(old, id=4)], {}, 30) == [old, dict(old, id=4)]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
