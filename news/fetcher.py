# News fetching module

import requests
from config import settings

def fetch_latest_news():
    """Fetch latest news headlines for XAU/USD."""
    url = f'https://newsapi.org/v2/everything?q=gold+XAUUSD&apiKey={settings.NEWS_API_KEY}'
    response = requests.get(url)
    if response.status_code == 200:
        return response.json().get('articles', [])
    return []
