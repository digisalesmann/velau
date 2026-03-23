# Market data utilities

import random

def get_market_data():
    """
    Fetch or simulate market data for XAU/USD.
    Returns: dict with price, volatility, session, etc.
    """
    # Placeholder: simulate data
    return {
        'price': 2000 + random.uniform(-10, 10),
        'volatility': random.uniform(5, 30),
        'session': random.choice(['Asia', 'London', 'New York']),
        'spread': random.uniform(0.1, 0.5)
    }
