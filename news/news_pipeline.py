# News ingestion and sentiment pipeline

from .fetcher import fetch_latest_news
from ai.sentiment import analyze_sentiment

def get_news_and_sentiment():
    """Fetch news and analyze sentiment for the trading pair."""
    articles = fetch_latest_news()
    sentiment = analyze_sentiment(articles)
    return articles, sentiment

if __name__ == "__main__":
    articles, sentiment = get_news_and_sentiment()
    print(f"Fetched {len(articles)} news articles.")
    print(f"Sentiment: {sentiment}")
