import os

# Logging level for the application
LOG_LEVEL = "INFO"

# Global settings for the trading bot
PAIR = 'XAUUSD'
NEWS_API_KEY = os.getenv('NEWS_API_KEY', '')
MT5_LOGIN = os.getenv('MT5_LOGIN', '')
MT5_PASSWORD = os.getenv('MT5_PASSWORD', '')
MT5_SERVER = os.getenv('MT5_SERVER', '')

# Deriv credentials — read from environment variables
DERIV_APP_ID = os.getenv('DERIV_APP_ID', '')
DERIV_API_TOKEN = os.getenv('DERIV_API_TOKEN', '')