"""
Deriv WebSocket client — connects to OTP-authenticated WebSocket URL.
"""
import asyncio
import websockets
import json
import logging

# FIX: Import your App ID from the renamed config file
from env_config import DERIV_APP_ID

logger = logging.getLogger("DerivWebSocket")

class DerivWebSocket:
    def __init__(self, ws_url: str = None, max_retries: int = 3):
        # FIX: Build the correct Deriv WebSocket URL if one wasn't explicitly provided
        if ws_url:
            self.ws_url = ws_url
        else:
            if not DERIV_APP_ID:
                raise ValueError("DERIV_APP_ID is missing! Please check your Render environment variables or env_config file.")
            
            # The standard Deriv WebSocket endpoint
            self.ws_url = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
            
        self.connection = None
        self.max_retries = max_retries

    async def connect(self):
        attempt = 0
        while attempt < self.max_retries:
            try:
                # Adding standard headers can sometimes help bypass aggressive firewalls
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

    async def receive(self):
        if not self.connection:
            raise Exception("WebSocket not connected.")
        response = await self.connection.recv()
        return json.loads(response)

    async def close(self):
        if self.connection:
            await self.connection.close()
            self.connection = None
            logger.info("WebSocket closed.")