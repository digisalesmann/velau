"""
Deriv WebSocket API integration for real-time trading and market data (production-ready).
"""
import asyncio
import websockets
import json
import logging
from config import settings

logger = logging.getLogger("DerivWebSocket")
logging.basicConfig(level=settings.LOG_LEVEL)

class DerivWebSocket:
    def __init__(self, ws_url: str, max_retries: int = 3):
        self.ws_url = ws_url
        self.connection = None
        self.max_retries = max_retries

    async def connect(self):
        attempt = 0
        while attempt < self.max_retries:
            try:
                self.connection = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10)
                logger.info(f"WebSocket connected to {self.ws_url}")
                return self.connection
            except Exception as e:
                attempt += 1
                logger.warning(f"WebSocket connection failed (attempt {attempt}): {e}")
                await asyncio.sleep(2 * attempt)
        logger.error("WebSocket connection failed after retries.")
        raise ConnectionError("WebSocket connection failed after retries.")

    async def send(self, message: dict):
        if not self.connection:
            raise Exception("WebSocket not connected.")
        try:
            await self.connection.send(json.dumps(message))
            logger.debug(f"Sent message: {message}")
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            raise

    async def receive(self):
        if not self.connection:
            raise Exception("WebSocket not connected.")
        try:
            response = await self.connection.recv()
            logger.debug(f"Received message: {response}")
            return json.loads(response)
        except Exception as e:
            logger.error(f"Error receiving message: {e}")
            raise

    async def close(self):
        if self.connection:
            await self.connection.close()
            logger.info("WebSocket connection closed.")
            self.connection = None
