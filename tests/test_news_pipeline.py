# Test for news ingestion and sentiment pipeline

from news.news_pipeline import get_news_and_sentiment

def test_news_and_sentiment():
    articles, sentiment = get_news_and_sentiment()
    assert isinstance(articles, list)
    assert isinstance(sentiment, dict)
    assert all(k in sentiment for k in ['bullish', 'bearish', 'neutral'])

if __name__ == "__main__":
    test_news_and_sentiment()
    print("News pipeline test passed.")
