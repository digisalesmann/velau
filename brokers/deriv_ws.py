"""
Deriv WebSocket API integration (production-ready).
"""
import asyncio
import websockets
import json
import logging
from config import settings

logger = logging.getLogger("DerivWebSocket")

class DerivWebSocket:
    def __init__(self, ws_url: str = None, max_retries: int = 3):
        app_id = settings.DERIV_APP_ID
        self.ws_url = ws_url or f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
        self.connection = None
        self.max_retries = max_retries

    async def connect(self):
        attempt = 0
        while attempt < self.max_retries:
            try:
                self.connection = await websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                )
                logger.info(f"WebSocket connected to {self.ws_url}")
                return self.connection
            except Exception as e:
                attempt += 1
                logger.warning(f"WebSocket connection failed (attempt {attempt}): {e}")
                await asyncio.sleep(2 * attempt)
        raise ConnectionError("WebSocket connection failed after retries.")

    async def send(self, message: dict):
        if not self.connection:
            raise Exception("WebSocket not connected.")
        await self.connection.send(json.dumps(message))
        logger.debug(f"Sent: {message}")

    async def receive(self):
        if not self.connection:
            raise Exception("WebSocket not connected.")
        response = await self.connection.recv()
        data = json.loads(response)
        logger.debug(f"Received: {data}")
        return data

    async def close(self):
        if self.connection:
            await self.connection.close()
            self.connection = None
            logger.info("WebSocket connection closed.")