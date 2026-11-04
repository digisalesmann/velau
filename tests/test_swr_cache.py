# Stale-while-revalidate for Deriv display data (main._swr): cached data is
# returned at once and refreshed in the background, so screens don't wait on
# a Deriv login every 30 seconds.

import asyncio
import os
import time

os.environ.pop("DATABASE_URL", None)

import deriv_cache
import main


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_first_load_fetches_then_stale_is_served_instantly_and_refreshed():
    calls = []

    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.2)
        return {"n": len(calls)}

    async def scenario():
        key = f"swr-test-{time.monotonic()}"
        assert await main._swr(key, 0.05, fetch) == {"n": 1}       # nothing cached: waits
        await asyncio.sleep(0.1)                                   # now older than fresh TTL
        t0 = time.perf_counter()
        assert await main._swr(key, 0.05, fetch) == {"n": 1}       # stale served at once
        assert time.perf_counter() - t0 < 0.05
        await asyncio.sleep(0.3)                                   # background refresh lands
        assert deriv_cache.get_stale(key) == {"n": 2}
    _run(scenario())


def test_failure_with_nothing_cached_raises():
    async def fetch():
        raise RuntimeError("deriv down")

    async def scenario():
        try:
            await main._swr(f"swr-fail-{time.monotonic()}", 30, fetch)
        except RuntimeError:
            return
        raise AssertionError("expected the error")
    _run(scenario())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
