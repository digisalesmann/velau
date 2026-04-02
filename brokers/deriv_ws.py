"""
Deriv WebSocket client — robust connection with auth race-condition fix.
"""
import asyncio
import websockets
import json
import logging

from env_config import DERIV_APP_ID

logger = logging.getLogger("DerivWebSocket")


class DerivWebSocket:
    def __init__(self, ws_url: str = None, max_retries: int = 5):
        if ws_url:
            self.ws_url = ws_url
        else:
            if not DERIV_APP_ID:
                raise ValueError(
                    "DERIV_APP_ID is missing! Check your environment variables."
                )
            self.ws_url = (
                f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
            )

        self.connection = None
        self.max_retries = max_retries

    async def connect(self):
        attempt = 0
        last_error = None

        while attempt < self.max_retries:
            try:
                self.connection = await websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=10,
                    # Extra headers help bypass some proxy rejections
                    additional_headers={
                        "User-Agent": "Mozilla/5.0",
                        "Origin": "https://app.deriv.com",
                    },
                )
                logger.info(f"✅ WebSocket connected (attempt {attempt + 1})")
                return self.connection

            except Exception as e:
                attempt += 1
                last_error = e
                wait = 2 ** attempt  # exponential back-off: 2, 4, 8, 16, 32s
                logger.warning(
                    f"WebSocket connection failed (attempt {attempt}): {e}. "
                    f"Retrying in {wait}s..."
                )
                await asyncio.sleep(wait)

        raise ConnectionError(
            f"WebSocket failed after {self.max_retries} attempts. "
            f"Last error: {last_error}"
        )

    async def send(self, message: dict):
        if not self.connection:
            raise Exception("WebSocket not connected. Call connect() first.")
        await self.connection.send(json.dumps(message))
        logger.debug(f"→ Sent: {list(message.keys())}")

    async def receive(self, timeout: float = 15.0) -> dict:
        """
        Receive with timeout so we never hang forever waiting for a frame.
        Raises asyncio.TimeoutError if nothing arrives within `timeout` seconds.
        """
        if not self.connection:
            raise Exception("WebSocket not connected. Call connect() first.")
        raw = await asyncio.wait_for(self.connection.recv(), timeout=timeout)
        msg = json.loads(raw)
        logger.debug(f"← Recv msg_type={msg.get('msg_type', '?')}")
        return msg

    async def close(self):
        if self.connection:
            try:
                await self.connection.close()
            except Exception:
                pass
            self.connection = None
            logger.info("WebSocket closed.")