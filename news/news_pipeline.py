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


def get_economic_blackout(now_utc=None) -> tuple[bool, str]:
    """
    Returns (is_blackout, reason) if we are within the pre/post window
    of a high-impact USD event that drives gold (NFP, CPI, FOMC, PPI).

    Priority order:
      1. Finnhub economic calendar (if FINNHUB_API_KEY env var is set, free tier)
      2. Hardcoded NFP detection — first Friday of month at 13:30 UTC
    """
    from datetime import datetime, timezone as _tz
    if now_utc is None:
        now_utc = datetime.now(_tz.utc)

    # ── 1. Finnhub ─────────────────────────────────────────────────────────────
    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    if finnhub_key:
        try:
            today  = now_utc.strftime("%Y-%m-%d")
            resp   = requests.get(
                "https://finnhub.io/api/v1/calendar/economic",
                params={"from": today, "to": today, "token": finnhub_key},
                timeout=6,
            )
            events = resp.json().get("economicCalendar", [])
            # High-impact USD events that move gold
            GOLD_EVENTS = {
                "Nonfarm Payrolls", "CPI", "Core CPI", "PCE", "Core PCE",
                "FOMC", "Federal Funds Rate", "PPI", "Core PPI",
                "Retail Sales", "GDP", "ISM Manufacturing", "ISM Services",
                "Initial Jobless Claims", "Unemployment Rate",
            }
            for ev in events:
                if ev.get("impact", 0) < 3:      # only HIGH impact
                    continue
                if ev.get("country", "").upper() != "US":
                    continue
                ev_name = ev.get("event", "")
                if not any(g in ev_name for g in GOLD_EVENTS):
                    continue
                # Parse event time — Finnhub returns HH:MM or full ISO
                ev_time_str = ev.get("time", "")
                try:
                    if "T" in ev_time_str:
                        ev_dt = datetime.fromisoformat(ev_time_str.replace("Z", "+00:00"))
                    else:
                        # time is "HH:MM" on the date we queried
                        h, m = map(int, ev_time_str.split(":"))
                        ev_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
                    diff_min = (now_utc - ev_dt).total_seconds() / 60
                    if -35 <= diff_min <= 65:    # 35 min before → 65 min after
                        reason = f"High-impact event blackout: {ev_name} (Finnhub)"
                        logger.warning(f"📅 {reason}")
                        return True, reason
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Finnhub calendar fetch failed: {e}")

    # ── 2. Hardcoded NFP (first Friday of month, 13:30 UTC) ────────────────────
    weekday = now_utc.weekday()      # 0=Mon … 6=Sun
    day     = now_utc.day
    total_m = now_utc.hour * 60 + now_utc.minute

    if weekday == 4 and day <= 7:    # first Friday
        if 13 * 60 <= total_m <= 15 * 60:    # 13:00–15:00 UTC
            reason = "NFP blackout — first Friday 13:00-15:00 UTC"
            logger.warning(f"📅 {reason}")
            return True, reason

    # ── 3. Generic USD release window (Tue-Thu, 13:00-14:15 UTC, mid-month) ───
    # CPI, PPI, Retail Sales, PCE all typically print at 13:30 UTC Tue-Thu
    if weekday in (1, 2, 3) and 9 <= day <= 21:
        if 13 * 60 <= total_m <= 14 * 60 + 15:
            reason = "USD data window blackout (Tue-Thu 13:00-14:15 UTC, mid-month)"
            logger.warning(f"📅 {reason}")
            return True, reason

    return False, ""