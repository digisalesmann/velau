# The app's stats count only the bot's own trades, lifetime, labelled by
# strategy (main._bot_trades / main._history_payload).

import os

os.environ.pop("DATABASE_URL", None)

import main

TRADES = [
    {"contract_id": "1", "contract_type": "BINARY", "pnl": -10.0},
    {"contract_id": "2", "contract_type": "MULTIPLIER", "pnl": 25.0},
    {"contract_id": "3", "contract_type": "MULTIPLIER", "pnl": -5.0},   # manual: not the bot's
]


def test_only_bot_trades_counted_and_labelled():
    bot = main._bot_trades(TRADES, {"1", "2"})
    assert [t["contract_id"] for t in bot] == ["1", "2"]
    assert [t["strategy"] for t in bot] == ["15min_retired", "trend"]


def test_summary_is_exact_even_when_list_is_truncated():
    many = [{"contract_id": str(i), "pnl": 1.0 if i % 4 == 0 else -1.0} for i in range(1000)]
    p = main._history_payload(many)
    assert len(p["history"]) == main.HISTORY_LIST_LIMIT
    assert p["summary"]["total_trades"] == 1000 and p["summary"]["wins"] == 250
    assert p["summary"]["win_rate"] == 25.0 and p["summary"]["total_pnl"] == -500.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
