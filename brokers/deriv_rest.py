"""
Deriv REST API — account info and OTP generation.
Base URL: https://api.derivws.com
Auth: Deriv-App-ID header + Authorization: Bearer <token>
"""
import requests
import logging
from requests.adapters import HTTPAdapter, Retry

BASE_URL = "https://api.derivws.com"
logger = logging.getLogger("DerivREST")


class DerivREST:
    def __init__(self, app_id: str, token: str):
        self.app_id = app_id
        self.token = token
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

    def _raise_with_body(self, resp):
        """requests' raise_for_status() drops the response body — Deriv's error
        messages ("Invalid or expired token" vs "Deriv-App-ID header is
        required" vs "Invalid token format") are the only way to tell auth
        failure modes apart, so surface them explicitly."""
        if resp.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{resp.status_code} error for {resp.url}: {resp.text[:300]}",
                response=resp,
            )

    def get_accounts(self) -> list:
        """GET /trading/v1/options/accounts — returns all accounts (demo + real) for this token."""
        url = f"{BASE_URL}/trading/v1/options/accounts"
        resp = self.session.get(url, headers=self.headers, timeout=10)
        self._raise_with_body(resp)
        logger.info("Fetched account list.")
        return resp.json().get("data", [])

    def generate_otp(self, account_id: str) -> dict:
        """POST /trading/v1/options/accounts/{accountId}/otp — returns OTP + WebSocket URL."""
        url = f"{BASE_URL}/trading/v1/options/accounts/{account_id}/otp"
        resp = self.session.post(url, headers=self.headers, timeout=10)
        self._raise_with_body(resp)
        logger.info("OTP generated.")
        return resp.json()