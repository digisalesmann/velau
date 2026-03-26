import os
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Grab your API key from environment variables, or paste it directly here for testing
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "GET_A_FREE_KEY_FROM_NEWSAPI.ORG")

def get_news_and_sentiment():
    """
    Fetches live financial news specific to Gold (XAU) and macroeconomics,
    runs NLP sentiment analysis, and calculates market bias.
    """
    # 1. Targeted Query for XAU/USD Macro Factors
    # We track Gold, USD, Federal Reserve (interest rates), and Inflation
    query = "(Gold OR XAU OR 'Federal Reserve' OR Inflation OR USD)"
    url = f"https://newsapi.org/v2/everything?q={query}&language=en&sortBy=publishedAt&apiKey={NEWS_API_KEY}"
    
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        
        # Fallback if API fails or key is invalid
        if data.get("status") != "ok":
            print(f"News API Error: {data.get('message')}")
            return [], {"overall": "Neutral", "score": 0, "bullish_percent": 0, "bearish_percent": 0}
            
        articles = data.get("articles", [])[:20] # Grab the top 20 most recent articles
        
        if not articles:
            return [], {"overall": "Neutral", "score": 0, "bullish_percent": 0, "bearish_percent": 0}

        # 2. Run NLP Sentiment Analysis
        analyzer = SentimentIntensityAnalyzer()
        total_score = 0
        bullish_count = 0
        bearish_count = 0
        valid_articles = []
        
        for article in articles:
            # Combine headline and description for better context
            title = article.get("title") or ""
            desc = article.get("description") or ""
            text = f"{title}. {desc}"
            
            if not text.strip() or "[Removed]" in title:
                continue
                
            # VADER returns a compound score between -1 (Extreme Bearish) and +1 (Extreme Bullish)
            sentiment = analyzer.polarity_scores(text)
            compound = sentiment["compound"]
            total_score += compound
            
            if compound > 0.05:
                bullish_count += 1
            elif compound < -0.05:
                bearish_count += 1
                
            # Attach the individual score to the article for the frontend
            article["sentiment_score"] = compound
            valid_articles.append(article)

        # 3. Aggregate the Market Bias
        num_articles = len(valid_articles)
        if num_articles == 0:
            return [], {"overall": "Neutral", "score": 0, "bullish_percent": 0, "bearish_percent": 0}
            
        avg_score = total_score / num_articles
        
        # Define strict thresholds to prevent false signals
        if avg_score >= 0.15:
            overall = "Bullish"
        elif avg_score <= -0.15:
            overall = "Bearish"
        else:
            overall = "Neutral"
            
        sentiment_dict = {
            "overall": overall,
            "score": round(avg_score, 2),
            "bullish_percent": round((bullish_count / num_articles) * 100),
            "bearish_percent": round((bearish_count / num_articles) * 100)
        }
        
        return valid_articles, sentiment_dict
        
    except Exception as e:
        print(f"Pipeline Exception: {e}")
        # Fail gracefully so the app doesn't crash, just returns Neutral
        return [], {"overall": "Neutral", "score": 0, "bullish_percent": 0, "bearish_percent": 0}