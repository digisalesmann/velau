"""
News & Sentiment Pipeline — multi-source XAU/USD intelligence.

Sources:
  1. NewsAPI — financial headlines (Gold, Fed, Inflation, USD)
  2. Forex Factory — high-impact economic event calendar (scraped)
     FF has no public API, so we scrape their JSON feed which they
     expose internally for their own calendar widget.

The pipeline returns:
  - articles: list of enriched article dicts
  - sentiment: {
        overall, score, bullish_percent, bearish_percent,
        high_impact_events: [...],   # events in next 4 hours
        blackout_active: bool        # True if trade should be blocked
    }
"""

import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger("NewsPipeline")

NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# Keywords that directly move XAU/USD
XAU_QUERY = (
    "(Gold OR XAU OR 'Federal Reserve' OR 'interest rates' "
    "OR Inflation OR CPI OR 'US Dollar' OR DXY OR 'safe haven' "
    "OR 'geopolitical' OR 'Jerome Powell')"
)

# High-impact Forex Factory event names that historically move Gold
HIGH_IMPACT_KEYWORDS = [
    "Non-Farm", "NFP", "CPI", "Federal Reserve", "FOMC", "Fed",
    "Interest Rate", "GDP", "Unemployment", "Retail Sales",
    "PPI", "PCE", "Powell", "Treasury",
]

# Block trades this many minutes before AND after a high-impact event
BLACKOUT_MINUTES_BEFORE = 30
BLACKOUT_MINUTES_AFTER  = 15


# ─── Forex Factory calendar scraper ──────────────────────────────────────────

def _fetch_forex_factory_events() -> list:
    """
    Scrape Forex Factory's calendar JSON (used by their own widget).
    Returns list of event dicts for today.
    Falls back to [] on any error — never crashes the pipeline.
    """
    try:
        # FF exposes their calendar as a JSON endpoint (no auth required)
        today = datetime.now(timezone.utc).strftime("%b%d.%Y").lower()
        url   = f"https://www.forexfactory.com/calendar.php?day={today}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html",
            "Referer": "https://www.forexfactory.com/",
        }

        resp = requests.get(url, headers=headers, timeout=8)

        # FF returns HTML, not JSON — we need their actual JSON API
        # They expose it at this endpoint used by their iOS app:
        ff_json_url = (
            "https://cdn-nfs.forexfactory.com/calendar.json"
        )
        resp2 = requests.get(ff_json_url, headers=headers, timeout=8)

        if resp2.status_code == 200:
            data = resp2.json()
            return data if isinstance(data, list) else []

        logger.warning(f"Forex Factory JSON returned {resp2.status_code}")
        return []

    except Exception as e:
        logger.warning(f"Forex Factory scrape failed: {e}")
        return []


def _parse_ff_events(raw_events: list) -> list:
    """
    Normalise FF event list → list of dicts with:
      name, currency, impact, datetime_utc
    Filter to USD high-impact events only.
    """
    parsed = []
    now_utc = datetime.now(timezone.utc)

    for ev in raw_events:
        try:
            currency = ev.get("currency", "")
            impact   = ev.get("impact", "").lower()
            name     = ev.get("name", ev.get("title", ""))

            if currency != "USD":
                continue
            if impact not in ("high", "medium"):
                continue

            # Parse time — FF uses "HH:MMam/pm ET" strings
            time_str = ev.get("time", "")
            date_str = ev.get("date", now_utc.strftime("%Y-%m-%d"))

            ev_dt = None
            for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%dT%H:%M:%S"):
                try:
                    ev_dt = datetime.strptime(
                        f"{date_str} {time_str}", fmt
                    ).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

            parsed.append({
                "name":         name,
                "currency":     currency,
                "impact":       impact,
                "datetime_utc": ev_dt,
                "raw_time":     time_str,
            })
        except Exception:
            continue

    return parsed


def get_high_impact_events() -> tuple[list, bool]:
    """
    Returns (events_within_window, blackout_active).
    blackout_active=True means a high-impact event is imminent/recent.
    """
    raw    = _fetch_forex_factory_events()
    events = _parse_ff_events(raw)
    now    = datetime.now(timezone.utc)

    upcoming = []
    blackout = False

    for ev in events:
        ev_dt = ev.get("datetime_utc")
        if ev_dt is None:
            continue

        minutes_until = (ev_dt - now).total_seconds() / 60
        minutes_since = (now - ev_dt).total_seconds() / 60

        # Is this event within our blackout window?
        is_imminent = 0 <= minutes_until <= BLACKOUT_MINUTES_BEFORE
        is_recent   = 0 <= minutes_since <= BLACKOUT_MINUTES_AFTER

        # Is it Gold-relevant?
        is_relevant = any(
            kw.lower() in ev["name"].lower()
            for kw in HIGH_IMPACT_KEYWORDS
        )

        if is_relevant and ev["impact"] == "high" and (is_imminent or is_recent):
            blackout = True

        if -60 <= (minutes_until or -minutes_since) <= 240:
            upcoming.append({
                **ev,
                "minutes_until": round(minutes_until, 0),
                "is_blackout":   is_relevant and (is_imminent or is_recent),
            })

    return upcoming, blackout


# ─── NewsAPI sentiment ────────────────────────────────────────────────────────

def get_news_and_sentiment() -> tuple[list, dict]:
    """
    Main pipeline entry point.
    Returns (articles, sentiment_dict).
    """
    analyzer    = SentimentIntensityAnalyzer()
    ff_events, blackout_active = get_high_impact_events()

    # ── NewsAPI fetch ─────────────────────────────────────────────────────────
    articles = []
    if NEWS_API_KEY:
        try:
            url = (
                f"https://newsapi.org/v2/everything"
                f"?q={XAU_QUERY}"
                f"&language=en"
                f"&sortBy=publishedAt"
                f"&pageSize=30"
                f"&apiKey={NEWS_API_KEY}"
            )
            resp = requests.get(url, timeout=10)
            data = resp.json()

            if data.get("status") == "ok":
                articles = data.get("articles", [])[:25]
            else:
                logger.warning(f"NewsAPI error: {data.get('message')}")

        except Exception as e:
            logger.warning(f"NewsAPI fetch failed: {e}")
    else:
        logger.warning("NEWS_API_KEY not set — skipping NewsAPI.")

    # ── Sentiment scoring ─────────────────────────────────────────────────────
    total_score    = 0.0
    bullish_count  = 0
    bearish_count  = 0
    valid_articles = []

    for article in articles:
        title = article.get("title") or ""
        desc  = article.get("description") or ""
        if not title.strip() or "[Removed]" in title:
            continue

        text     = f"{title}. {desc}"
        scores   = analyzer.polarity_scores(text)
        compound = scores["compound"]
        total_score += compound

        if compound > 0.05:
            bullish_count += 1
        elif compound < -0.05:
            bearish_count += 1

        article["sentiment_score"] = compound
        valid_articles.append(article)

    n = len(valid_articles)
    if n == 0:
        overall   = "Neutral"
        avg_score = 0.0
        bull_pct  = 0
        bear_pct  = 0
    else:
        avg_score = total_score / n
        bull_pct  = round((bullish_count / n) * 100)
        bear_pct  = round((bearish_count / n) * 100)

        # Thresholds — strict to avoid false signals
        if avg_score >= 0.15:
            overall = "Bullish"
        elif avg_score <= -0.15:
            overall = "Bearish"
        else:
            overall = "Neutral"

    # If a high-impact event blackout is active, override to Neutral
    # so the strategy engine doesn't fire into the news spike
    effective_bias = "Neutral" if blackout_active else overall

    sentiment_dict = {
        "overall":             effective_bias,
        "raw_overall":         overall,
        "score":               round(avg_score, 3),
        "bullish_percent":     bull_pct,
        "bearish_percent":     bear_pct,
        "high_impact_events":  ff_events,
        "blackout_active":     blackout_active,
        "articles_analyzed":   n,
    }

    logger.info(
        f"📰 Sentiment={effective_bias} ({avg_score:+.3f}) | "
        f"bull={bull_pct}% bear={bear_pct}% | "
        f"blackout={blackout_active} | events={len(ff_events)}"
    )

    return valid_articles, sentiment_dict