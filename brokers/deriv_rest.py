"""
Deriv REST API integration for account management and OTP generation (production-ready).
"""
import requests
import logging
from requests.adapters import HTTPAdapter, Retry
from config import settings

BASE_URL = "https://api.derivws.com"

logger = logging.getLogger("DerivREST")
logging.basicConfig(level=settings.LOG_LEVEL)

class DerivREST:
    def __init__(self, app_id: str = None, token: str = None):
        self.app_id = app_id or settings.DERIV_APP_ID
        self.token = token or settings.DERIV_TOKEN
        self.headers = {
            "Deriv-App-ID": self.app_id,
            "Authorization": f"Bearer {self.token}"
        }
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def get_account_info(self):
        url = f"{BASE_URL}/trading/v1/options/accounts/me"
        try:
            resp = self.session.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            logger.info("Fetched account info successfully.")
            return resp.json()
        except Exception as e:
            logger.error(f"Error fetching account info: {e}")
            raise

    def generate_otp(self, account_id: str):
        url = f"{BASE_URL}/trading/v1/options/accounts/{account_id}/otp"
        try:
            resp = self.session.post(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            logger.info("OTP generated successfully.")
            return resp.json()
        except Exception as e:
            logger.error(f"Error generating OTP: {e}")
            raise
