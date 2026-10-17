# Request-handling concurrency: concurrent identical Deriv-backed requests
# share one Deriv login (single-flight), failures fall back to the stale cache
# without every waiter retrying, and a token's account list is cached.

import asyncio
import os
import time

os.environ.pop("DATABASE_URL", None)

import httpx

import database as db
import deriv_cache
import main
import brokers.deriv_trading_service as dts
import brokers.deriv_ws as dws
from user_models import create_access_token

USER = "concurrency_user@test.local"


class FakeDeriv:
    logins = 0
    fail = False

    def __init__(self, *a, **kw):
        pass

    async def authenticate(self):
        FakeDeriv.logins += 1
        await asyncio.sleep(1.0)          # a slow Deriv login
        if FakeDeriv.fail:
            raise ConnectionError("Deriv down")

    async def get_account_info(self):
        return {"balance": 100.0, "currency": "USD", "account_id": "DOT1"}

    async def get_statement(self):
        return {"history": []}

    async def close(self):
        pass


def _setup():
    if not db.get_user(USER):
        db.create_user(USER, "x")
    db.save_deriv_token(USER, "tok", "DOT1")
    dts.DerivTradingService = FakeDeriv
    deriv_cache._store.clear(); deriv_cache._failed_at.clear(); deriv_cache._locks.clear()
    FakeDeriv.logins, FakeDeriv.fail = 0, False
    return {"Authorization": f"Bearer {create_access_token({'sub': USER})}"}


async def _fire(paths, headers):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
        return await asyncio.gather(*[c.get(p, headers=headers) for p in paths])


def test_concurrent_requests_share_one_login():
    h = _setup()
    t0 = time.time()
    rs = asyncio.run(_fire(["/dashboard/history"] * 3 + ["/dashboard"] * 2, h))
    assert all(r.status_code == 200 for r in rs), [r.status_code for r in rs]
    assert FakeDeriv.logins == 2, f"{FakeDeriv.logins} logins; expected 1 per cache key"
    assert time.time() - t0 < 3


def test_failure_falls_back_to_stale_without_every_waiter_retrying():
    h = _setup()
    asyncio.run(_fire(["/dashboard/history"], h))          # warm the cache
    deriv_cache._store["history:" + USER + ":" + "real"] = (0.0, {"history": ["old"]})  # expire it
    for k in list(deriv_cache._store):
        deriv_cache._store[k] = (0.0, deriv_cache._store[k][1])
    FakeDeriv.logins, FakeDeriv.fail = 0, True
    rs = asyncio.run(_fire(["/dashboard/history"] * 4, h))
    assert all(r.status_code == 200 for r in rs)            # stale data, not an error
    assert FakeDeriv.logins == 1, f"{FakeDeriv.logins} logins while Deriv is down; expected 1"


def test_account_list_cached_and_dropped_on_failure():
    calls = {"accounts": 0}

    class FakeREST:
        def __init__(self, app_id, token):
            pass

        def get_accounts(self):
            calls["accounts"] += 1
            return [{"account_id": "DOT1", "account_type": "demo", "status": "active"}]

        def generate_otp(self, account_id):
            if calls.get("fail_otp"):
                raise RuntimeError("401 Invalid or expired token")
            return {"data": {"url": "wss://x"}}

    dws.DerivREST = FakeREST
    dws._accounts_cache.clear()
    ws = dws.DerivWebSocket(token="tok-a", app_id="1", account_type="demo")
    ws._fetch_ws_url(); ws._fetch_ws_url()
    assert calls["accounts"] == 1                            # second login used the cache
    calls["fail_otp"] = True
    try:
        ws._fetch_ws_url()
    except RuntimeError:
        pass
    calls["fail_otp"] = False
    ws._fetch_ws_url()
    assert calls["accounts"] == 2                            # failure dropped the cache entry


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
