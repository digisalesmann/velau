"""
Deriv WebSocket client — Python 3.14 safe.

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

from env_config import DERIV_APP_ID

logger = logging.getLogger("DerivWebSocket")


class DerivWebSocket:
    def __init__(self, ws_url: str = None, max_retries: int = 7):
        if ws_url:
            self.ws_url = ws_url
        else:
            if not DERIV_APP_ID:
                raise ValueError(
                    "DERIV_APP_ID missing — set it in Render environment variables."
                )
            self.ws_url = (
                f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
            )
        self.connection  = None
        self.max_retries = max_retries

    async def connect(self):
        last_error = None
        for attempt in range(self.max_retries):
            try:
                self.connection = await websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=10,
                    open_timeout=15,
                    additional_headers={
                        "User-Agent": "Mozilla/5.0",
                        "Origin":     "https://app.deriv.com",
                    },
                )
                logger.info(f"✅ WebSocket connected (attempt {attempt + 1})")
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