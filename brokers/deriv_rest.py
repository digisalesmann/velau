"""
Deriv REST API — account info and OTP generation.
Base URL: https://api.derivws.com
Auth: Deriv-App-ID header + Authorization: Bearer <token>
"""
import requests
import logging
from requests.adapters import HTTPAdapter, Retry
from config import settings

BASE_URL = "https://api.derivws.com"
logger = logging.getLogger("DerivREST")


class DerivREST:
    def __init__(self, app_id: str = None, token: str = None):
        self.app_id = app_id or settings.DERIV_APP_ID
        self.token = token or settings.DERIV_API_TOKEN
        self.headers = {
            "Deriv-App-ID": self.app_id,
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def get_account_info(self) -> dict:
        """GET /trading/v1/options/accounts/me — returns account id, balance, currency."""
        url = f"{BASE_URL}/trading/v1/options/accounts/me"
        resp = self.session.get(url, headers=self.headers, timeout=10)
        resp.raise_for_status()
        logger.info("Fetched account info.")
        return resp.json()

    def generate_otp(self, account_id: str) -> dict:
        """POST /trading/v1/options/accounts/{accountId}/otp — returns OTP + WebSocket URL."""
        url = f"{BASE_URL}/trading/v1/options/accounts/{account_id}/otp"
        resp = self.session.post(url, headers=self.headers, timeout=10)
        resp.raise_for_status()
        logger.info("OTP generated.")
        return resp.json()