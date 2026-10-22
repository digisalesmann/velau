# Tests for the 1:1.5 Multiplier trade flow: sizing, close-by times, and the
# contract monitor (with Deriv, the DB and notifications faked out).

import asyncio
from datetime import datetime, timezone

import core.strategy_engine as se
from position_sizing import (
    plan_multiplier_trade, RR_RATIO, MIN_FEASIBLE_STAKE_TO_RISK, MULTIPLIER_CHOICES,
    COMMISSION_RATE_EST,
)


def _epoch(h, m):
    return int(datetime(2026, 10, 14, h, m, tzinfo=timezone.utc).timestamp())


def test_plan_multiplier_trade():
    for risk, atr in [(1.0, 4.7), (10.0, 8.14), (50.0, 13.0), (5.0, 19.9)]:
        plan = plan_multiplier_trade(risk, atr, 4185.0)
        assert plan["stop_loss"] == round(risk, 2)
        assert plan["take_profit"] == round(risk * RR_RATIO, 2)
        assert plan["multiplier"] in MULTIPLIER_CHOICES
        # Deriv's x100 minimum can't always leave the preferred 2x headroom;
        # the hard guarantee is the feasibility floor (else the plan raises).
        assert plan["stake"] >= MIN_FEASIBLE_STAKE_TO_RISK * risk - 0.01
        # Stop price distance (commission included) lands at ~1x ATR.
        comm = plan["notional"] * COMMISSION_RATE_EST
        stop_dist = (plan["stop_loss"] - comm) / plan["notional"] * 4185.0
        assert abs(stop_dist - atr) / atr < 0.02, (risk, atr, stop_dist)


def test_multiplier_close_by():
    # Mid-session entry: the max-hold timeout comes first.
    assert se.multiplier_close_by(_epoch(14, 10)) == _epoch(14, 10) + se.MULT_MAX_HOLD_MINUTES * 60
    # Late entry: the daily close comes first.
    assert se.multiplier_close_by(_epoch(19, 0)) == _epoch(20, 45)


class _FakeService:
    """Stands in for DerivTradingService; replays a scripted poc sequence."""
    pocs: list = []
    sold: list = []

    def __init__(self, *a, **kw):
        pass

    async def authenticate(self):
        pass

    async def close(self):
        pass

    async def get_contract(self, contract_id):
        return _FakeService.pocs.pop(0)

    async def sell_contract(self, contract_id):
        _FakeService.sold.append(contract_id)
        return {"sold_for": 9.0}


def _run_monitor(pocs, close_by):
    recorded = []
    se.DerivTradingService = _FakeService
    _FakeService.pocs, _FakeService.sold = list(pocs), []

    async def no_sleep(_):
        pass

    se.asyncio.sleep = no_sleep
    se.db.insert_trade_result = lambda cid, won, pnl, username=None: recorded.append((won, pnl))
    se.db.record_trade_settlement = lambda *a: {
        "daily_pnl": 0.0, "consecutive_losses": 0, "newly_tripped": False}
    se.notif.notify_user = lambda *a, **kw: None

    bot = se.XAUMasterStrategy()
    bot._open_contracts["123"] = "alice"
    assert bot._trade_in_progress
    asyncio.run(bot._monitor_contract("tok", "demo", "123", 25.0, "alice", close_by=close_by))
    assert not bot._trade_in_progress, "lock must release once the position settles"
    return recorded


def test_take_profit_recorded_as_win():
    # Deriv reports a take-profit close as status "sold", never "won".
    far_future = _epoch(23, 0) + 10 ** 8
    recorded = _run_monitor(
        [{"status": "open", "is_sold": 0},
         {"status": "sold", "is_sold": 1, "profit": "15.00"}],
        close_by=far_future,
    )
    assert recorded == [(True, 15.0)]
    assert _FakeService.sold == []


def test_stop_loss_recorded_as_loss():
    recorded = _run_monitor(
        [{"status": "sold", "is_sold": 1, "profit": -10}], close_by=_epoch(23, 0) + 10 ** 8)
    assert recorded == [(False, -10.0)]


def test_timeout_sells_at_market():
    already_due = 1  # close_by in the past
    recorded = _run_monitor(
        [{"status": "open", "is_sold": 0},
         {"status": "sold", "is_sold": 1, "profit": "-1.00"}],
        close_by=already_due,
    )
    assert _FakeService.sold == ["123"]
    assert recorded == [(False, -1.0)]


def test_lock_held_while_placing_or_open():
    bot = se.XAUMasterStrategy()
    assert not bot._trade_in_progress
    bot._trade_in_progress = True
    assert bot._trade_in_progress
    bot._open_contracts["9"] = "bob"
    bot._trade_in_progress = False  # placement finished, position still open
    assert bot._trade_in_progress
    bot._open_contracts.clear()
    assert not bot._trade_in_progress


def test_contract_ids_are_sent_to_deriv_as_integers():
    # trend_positions stores contract_id as TEXT, but Deriv rejects string
    # ids ("Input validation failed: contract_id"), found live 2026-10-22.
    from brokers.deriv_trading_service import DerivTradingService
    sent = []

    class FakeWS:
        async def send(self, msg):
            sent.append(msg)

    async def fake_wait(expected, timeout=20.0):
        return {expected: {"contract_id": 1, "sold_for": 1.0, "status": "sold"}}

    svc = DerivTradingService.__new__(DerivTradingService)
    svc.ws, svc._authorized, svc._wait_for = FakeWS(), True, fake_wait
    asyncio.run(svc.get_contract("17856120979"))
    asyncio.run(svc.sell_contract("17856120979"))
    assert sent[0]["contract_id"] == 17856120979 and isinstance(sent[0]["contract_id"], int)
    assert sent[1]["sell"] == 17856120979 and isinstance(sent[1]["sell"], int)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
