"""
Centralized configuration for production and development environments.
Uses environment variables for sensitive data.
"""
import os
from dotenv import load_dotenv

load_dotenv()

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "")
DERIV_TOKEN = os.getenv("DERIV_TOKEN", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
