"""
News & Sentiment Pipeline — NewsAPI-based XAU/USD intelligence.

Forex Factory scraping has been removed — their CDN domain is blocked
on Render's free tier network. The economic blackout logic is preserved
but falls back gracefully to no-blackout when FF is unreachable.

Future: replace FF with investing.com scraping or a paid calendar API.
"""
import os
import logging
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger("NewsPipeline")

NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# Targeted query — covers all macro drivers of XAU/USD
XAU_QUERY = (
    "(Gold OR XAU OR 'Federal Reserve' OR 'interest rates' "
    "OR Inflation OR CPI OR 'US Dollar' OR DXY OR 'safe haven' "
    "OR geopolitical OR 'Jerome Powell')"
)


def get_news_and_sentiment() -> tuple[list, dict]:
    """
    Fetch headlines → run VADER sentiment → return (articles, sentiment_dict).

    sentiment_dict keys:
      overall            — "Bullish" | "Bearish" | "Neutral"
      score              — float, average compound VADER score
      bullish_percent    — int
      bearish_percent    — int
      blackout_active    — bool (always False until FF is re-integrated)
      high_impact_events — list (always [] until FF is re-integrated)
      articles_analyzed  — int
    """
    analyzer = SentimentIntensityAnalyzer()

    # ── Fetch headlines ────────────────────────────────────────────────────────
    raw_articles: list = []

    if not NEWS_API_KEY:
        logger.warning("NEWS_API_KEY not set — sentiment will be Neutral.")
    else:
        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q":        XAU_QUERY,
                    "language": "en",
                    "sortBy":   "publishedAt",
                    "pageSize": 30,
                    "apiKey":   NEWS_API_KEY,
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("status") == "ok":
                raw_articles = data.get("articles", [])[:25]
            else:
                logger.warning(f"NewsAPI: {data.get('message', 'unknown error')}")
        except Exception as e:
            logger.warning(f"NewsAPI fetch failed: {e}")

    # ── Sentiment scoring ──────────────────────────────────────────────────────
    total_score   = 0.0
    bullish_count = 0
    bearish_count = 0
    valid         = []

    for article in raw_articles:
        title = article.get("title") or ""
        desc  = article.get("description") or ""
        if not title.strip() or "[Removed]" in title:
            continue

        compound = analyzer.polarity_scores(f"{title}. {desc}")["compound"]
        total_score += compound

        if compound > 0.05:
            bullish_count += 1
        elif compound < -0.05:
            bearish_count += 1

        article["sentiment_score"] = compound
        valid.append(article)

    n = len(valid)

    if n == 0:
        overall   = "Neutral"
        avg_score = 0.0
        bull_pct  = 0
        bear_pct  = 0
    else:
        avg_score = total_score / n
        bull_pct  = round(bullish_count / n * 100)
        bear_pct  = round(bearish_count / n * 100)

        # Strict thresholds — avoid hair-trigger signals
        if avg_score >= 0.15:
            overall = "Bullish"
        elif avg_score <= -0.15:
            overall = "Bearish"
        else:
            overall = "Neutral"

    logger.info(
        f"📰 Sentiment={overall} ({avg_score:+.3f}) | "
        f"bull={bull_pct}% bear={bear_pct}% | articles={n}"
    )

    return valid, {
        "overall":            overall,
        "score":              round(avg_score, 3),
        "bullish_percent":    bull_pct,
        "bearish_percent":    bear_pct,
        "blackout_active":    False,   # re-enable when FF is replaced
        "high_impact_events": [],
        "articles_analyzed":  n,
    }