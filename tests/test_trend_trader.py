# Behaviour tests for core/trend_strategy.TrendTrader's live cycle, with
# Deriv, the database and the clock faked out.

import asyncio
import types

import numpy as np
import pandas as pd

import core.trend_strategy as ts

H4, D1 = ts.GRAN_4H, ts.GRAN_1D
T0 = 1_780_000_000 - 1_780_000_000 % D1   # a UTC midnight


def _h4_frame(n=200, breakout=True, spot=None):
    """Gently rising 4H bars; the last closed bar breaks above every prior
    high when `breakout`. A forming bar follows it, closing at `spot`."""
    rng = np.random.default_rng(1)
    close = 4000 + np.cumsum(rng.normal(0.3, 4, n))
    high, low = close + 3, close - 3
    if breakout:
        close[-1] = high[:-1].max() + 10
        high[-1] = close[-1] + 1
    ep = T0 + np.arange(n) * H4
    df = pd.DataFrame({"epoch": ep, "open": close - 1, "high": high, "low": low, "close": close})
    forming = {"epoch": ep[-1] + H4, "open": close[-1], "high": close[-1],
               "low": close[-1], "close": close[-1] if spot is None else spot}
    return pd.concat([df, pd.DataFrame([forming])], ignore_index=True)


def _d1_frame(uptrend=True, end_epoch=None):
    days = 260
    ep = (end_epoch - end_epoch % D1) - np.arange(days, 0, -1) * D1
    close = np.linspace(3000, 4000, days) if uptrend else np.linspace(5000, 3500, days)
    return pd.DataFrame({"epoch": ep, "open": close, "high": close + 5, "low": close - 5, "close": close})


class FakeDeriv:
    orders, sold, pocs = [], [], {}

    def __init__(self, *a, **kw):
        pass

    async def authenticate(self):
        pass

    async def close(self):
        pass

    async def get_candles(self, symbol, count, granularity, end="latest"):
        if end != "latest":
            return []
        df = FakeDeriv.h4 if granularity == H4 else FakeDeriv.d1
        return df.to_dict("records")

    async def get_account_info(self):
        return {"balance": 1000.0}

    async def place_order(self, contract_type, amount, multiplier=None, stop_loss=None, take_profit=None, **kw):
        cid = f"C{len(FakeDeriv.orders) + 1}"
        FakeDeriv.orders.append({"type": contract_type, "stake": amount, "multiplier": multiplier,
                                 "stop_loss": stop_loss, "take_profit": take_profit, "cid": cid})
        return {"buy": {"contract_id": cid, "purchase_time": 0}}

    async def get_contract(self, cid):
        return FakeDeriv.pocs.get(cid, {"status": "open", "is_sold": 0, "profit": "0"})

    async def sell_contract(self, cid):
        FakeDeriv.sold.append(cid)
        FakeDeriv.pocs[cid] = {"status": "sold", "is_sold": 1, "profit": "-3.10"}
        return {"sold_for": 1.0}


class FakeDB:
    def __init__(self):
        self.rows = {}

    def install(self):
        ts.db.get_open_trend_positions = lambda u=None: [r for r in self.rows.values()
                                                         if r["status"] == "open" and (u is None or r["username"] == u)]
        ts.db.insert_trend_position = self.insert
        ts.db.close_trend_position = self.close
        ts.db.set_trend_trail = lambda cid, u, t: self.rows[cid].__setitem__("trail_stop", t)
        ts.db.get_last_trend_exit = lambda u, s: max([r["closed_at"] for r in self.rows.values()
                                                      if r["username"] == u and r["sleeve"] == s
                                                      and r["status"] == "closed"], default=None)
        ts.db.maybe_set_opening_balance = lambda *a: None

    def insert(self, cid, u, sleeve, bar, entry, sm, init, risk, stake, mult, opened):
        self.rows[cid] = dict(contract_id=cid, username=u, sleeve=sleeve, entry_bar_epoch=bar,
                              entry_price=entry, stop_mult=sm, initial_stop=init, trail_stop=None,
                              risk=risk, stake=stake, multiplier=mult, status="open", opened_at=opened)

    def close(self, cid, u, closed_at, pnl, reason):
        self.rows[cid].update(status="closed", closed_at=closed_at, pnl=pnl, exit_reason=reason)


class FakeStrategy:
    def __init__(self):
        self.signals, self.settled = [], []

    def save_signal(self, *a, **kw):
        self.signals.append(a)

    async def _settle_contract(self, cid, profit, username, max_losses):
        self.settled.append((cid, profit, username))


USER = {"username": "alice", "deriv_token": "tok", "trade_account_type": "demo",
        "bot_enabled": True, "circuit_broken": False}


def _setup(h4, uptrend=True):
    FakeDeriv.orders, FakeDeriv.sold, FakeDeriv.pocs = [], [], {}
    FakeDeriv.h4 = h4
    now = int(h4["epoch"].iloc[-1]) + 600          # 10 min into the forming bar
    FakeDeriv.d1 = _d1_frame(uptrend, now)
    ts.DerivTradingService = FakeDeriv
    ts.time = types.SimpleNamespace(time=lambda: now)
    db = FakeDB(); db.install()
    strat = FakeStrategy()
    return ts.TrendTrader(strat), strat, db


def test_breakout_in_uptrend_opens_all_sleeves():
    trader, strat, db = _setup(_h4_frame())
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    from position_sizing import calculate_stake
    sleeve_risk = round(calculate_stake(balance=1000.0)[0] / len(ts.SLEEVES), 2)
    assert len(FakeDeriv.orders) == len(ts.SLEEVES)
    for o in FakeDeriv.orders:
        assert o["type"] == "MULTUP" and o["take_profit"] is None
        assert o["stop_loss"] == sleeve_risk
    assert len(db.rows) == len(ts.SLEEVES)
    assert strat.signals[0][0] == "BUY"


def test_same_bar_never_entered_twice():
    trader, strat, db = _setup(_h4_frame())
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    trader2 = ts.TrendTrader(strat)                  # simulated restart: memory gone, DB kept
    asyncio.run(trader2.run_cycle(FakeDeriv(), [USER]))
    assert len(FakeDeriv.orders) == len(ts.SLEEVES)


def test_downtrend_regime_blocks_entries():
    trader, strat, db = _setup(_h4_frame(), uptrend=False)
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    assert FakeDeriv.orders == []
    assert strat.signals[0][0] == "NEUTRAL"


def test_trailing_stop_exit_sells_and_settles():
    trader, strat, db = _setup(_h4_frame())
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    # Price collapses below every sleeve's stop on the next cycle.
    FakeDeriv.h4 = _h4_frame(spot=3000.0)
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    assert sorted(FakeDeriv.sold) == sorted(r["contract_id"] for r in db.rows.values())
    assert all(r["status"] == "closed" and r["exit_reason"] == "trailing_stop" for r in db.rows.values())
    assert len(strat.settled) == len(ts.SLEEVES) and all(p < 0 for _, p, _ in strat.settled)


def test_deriv_side_close_is_recorded_without_selling():
    trader, strat, db = _setup(_h4_frame())
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    cid = next(iter(db.rows))
    FakeDeriv.pocs[cid] = {"status": "sold", "is_sold": 1, "profit": "-6.25"}
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    assert cid not in FakeDeriv.sold
    assert db.rows[cid]["exit_reason"] == "deriv_stop" and (cid, -6.25, "alice") in strat.settled


def test_paused_user_still_gets_exits_managed():
    trader, strat, db = _setup(_h4_frame())
    asyncio.run(trader.run_cycle(FakeDeriv(), [USER]))
    paused = {**USER, "bot_enabled": False}
    FakeDeriv.h4 = _h4_frame(spot=3000.0)
    asyncio.run(trader.run_cycle(FakeDeriv(), [paused]))
    assert len(FakeDeriv.sold) == len(ts.SLEEVES)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
