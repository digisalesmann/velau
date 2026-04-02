"""
Deriv WebSocket client — Python 3.14 compatible.

Fix: asyncio.wait_for() has a known interaction with websockets on Python 3.14
where CancelledError propagates incorrectly through the recv() coroutine,
causing spurious TimeoutError even when the connection is healthy.

Solution: use `async with asyncio.timeout(n)` (native since Python 3.11)
which handles cancellation correctly inside websockets' async generator.
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
        Receive next frame with timeout.

        Uses `async with asyncio.timeout()` instead of `asyncio.wait_for()`
        to avoid the CancelledError → TimeoutError mis-propagation in
        Python 3.14 + websockets.
        """
        if not self.connection:
            raise Exception("WebSocket not connected.")

        try:
            async with asyncio.timeout(timeout):
                raw = await self.connection.recv()
            msg = json.loads(raw)
            logger.debug(f"← msg_type={msg.get('msg_type', '?')}")
            return msg
        except TimeoutError:
            raise TimeoutError(
                f"WebSocket receive timed out after {timeout}s"
            )
        except asyncio.CancelledError:
            # Re-raise cancellation — don't swallow it
            raise
        except Exception as e:
            raise Exception(f"WebSocket receive error: {e}") from e

    async def close(self):
        if self.connection:
            try:
                await self.connection.close()
            except Exception:
                pass
            self.connection = None
            logger.info("WebSocket closed.")