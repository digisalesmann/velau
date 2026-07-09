"""
Deriv WebSocket client — Python 3.14 safe.

Connection model (Deriv retired the legacy `wss://ws.binaryws.com` +
in-band `{"authorize": token}` flow):
  1. REST: GET  /trading/v1/options/accounts        -> list of accounts for this token
  2. REST: POST /trading/v1/options/accounts/{id}/otp -> single-use WebSocket URL
  3. Connect directly to that URL — the OTP in the query string authenticates
     the session, so no further auth frame is sent or expected. Every other
     message (balance, ticks_history, proposal, buy, ...) uses the exact same
     JSON protocol as before.

  OTPs are short-lived and single-use, so a fresh one is fetched on every
  connection attempt (including retries), not just the first.

Root cause of all timeout issues:
  Python 3.14 changed how CancelledError propagates through async generators.
  websockets uses an async generator internally for recv(). Both
  asyncio.wait_for() and asyncio.timeout() wrap their body in a task/scope
  that catches CancelledError — but when websockets propagates CancelledError
  upward through its generator, both wrappers misinterpret it as a timeout.

Solution:
  Wrap recv() in a plain asyncio.Task and use asyncio.wait() with a timeout.
  asyncio.wait() does NOT cancel the underlying task on timeout — it just
  stops waiting. We then cancel explicitly only if it actually timed out.
  This gives us real timeout behaviour without interfering with websockets'
  internal cancellation handling.
"""
import asyncio
import websockets
import json
import logging

from brokers.deriv_rest import DerivREST
from env_config import DERIV_APP_ID

logger = logging.getLogger("DerivWebSocket")


class DerivWebSocket:
    def __init__(self, token: str, app_id: str = None, max_retries: int = 7):
        self.app_id = app_id or DERIV_APP_ID
        if not self.app_id:
            raise ValueError(
                "DERIV_APP_ID missing — set it in Render environment variables."
            )
        if not token:
            raise ValueError("Deriv API token missing.")

        self.token       = token
        self.max_retries = max_retries
        self.connection  = None
        self.account_id   = None
        self.account_info = None

    def _fetch_ws_url(self) -> str:
        """Blocking REST calls (accounts -> OTP) run off the event loop via asyncio.to_thread."""
        rest = DerivREST(app_id=self.app_id, token=self.token)
        accounts = rest.get_accounts()
        if not accounts:
            raise ConnectionError("Deriv token has no accessible trading accounts.")

        active = [a for a in accounts if a.get("status") == "active"] or accounts
        chosen = next((a for a in active if a.get("account_type") == "real"), active[0])

        self.account_id   = chosen.get("account_id")
        self.account_info = chosen

        otp = rest.generate_otp(self.account_id)
        return otp["data"]["url"]

    async def connect(self):
        last_error = None
        for attempt in range(self.max_retries):
            try:
                ws_url = await asyncio.to_thread(self._fetch_ws_url)
                self.connection = await websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=10,
                    open_timeout=15,
                )
                logger.info(
                    f"✅ WebSocket connected (attempt {attempt + 1}) | "
                    f"account={self.account_id}"
                )
                return self.connection
            except Exception as e:
                last_error = e
                wait = min(2 ** (attempt + 1), 30)
                logger.warning(
                    f"WS attempt {attempt + 1}/{self.max_retries} failed: {e}. "
                    f"Retrying in {wait}s..."
                )
                await asyncio.sleep(wait)
        raise ConnectionError(
            f"WebSocket failed after {self.max_retries} attempts. Last: {last_error}"
        )

    async def send(self, message: dict):
        if not self.connection:
            raise Exception("WebSocket not connected.")
        await self.connection.send(json.dumps(message))
        logger.debug(f"→ {list(message.keys())}")

    async def receive(self, timeout: float = 20.0) -> dict:
        """
        Receive next WebSocket frame with a real timeout.

        Uses asyncio.wait() on a Task wrapping recv() — this is the only
        approach that correctly handles Python 3.14 + websockets because:

        1. The Task runs recv() in isolation from our timeout logic.
        2. asyncio.wait() with a timeout returns PENDING sets without
           cancelling the task automatically.
        3. We cancel explicitly only when we know it actually timed out.
        4. CancelledError from websockets' internal generator never
           leaks into our timeout handling.
        """
        if not self.connection:
            raise Exception("WebSocket not connected.")

        loop = asyncio.get_event_loop()
        recv_task = loop.create_task(self.connection.recv())

        try:
            done, pending = await asyncio.wait(
                {recv_task},
                timeout=timeout,
            )
        except asyncio.CancelledError:
            # Our own task was cancelled from outside — clean up and re-raise
            recv_task.cancel()
            raise

        if recv_task in pending:
            # Genuine timeout — cancel the recv task and raise
            recv_task.cancel()
            try:
                await recv_task
            except (asyncio.CancelledError, Exception):
                pass
            raise TimeoutError(
                f"WebSocket receive timed out after {timeout}s"
            )

        # Task completed — get result or propagate exception
        exc = recv_task.exception()
        if exc:
            raise exc

        raw = recv_task.result()
        msg = json.loads(raw)
        logger.debug(f"← msg_type={msg.get('msg_type', '?')}")
        return msg

    async def close(self):
        if self.connection:
            try:
                await self.connection.close()
            except Exception:
                pass
            self.connection = None
            logger.info("WebSocket closed.")