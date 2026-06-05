import hashlib
import json
import urllib.request
from datetime import datetime, timezone

import feedparser  # pip install feedparser

RSS_FEEDS = [
    "https://www.interfax.ru/rss.asp",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://www.rbc.ru/rss/",
]


def fetch_competitor_news(competitors: list[str], max_articles: int = 20) -> dict:
    """Скачивает RSS-ленты и фильтрует по ключевым словам-конкурентам."""
    all_items = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                all_items.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source": feed.feed.get("title", feed_url),
                })
        except Exception:
            continue

    # Фильтруем: только статьи, где упомянут хотя бы один конкурент
    matched = []
    for item in all_items:
        text = (item["title"] + " " + item["summary"]).lower()
        for comp in competitors:
            if comp.lower() in text:
                item["matched_competitor"] = comp
                matched.append(item)
                break

    # Берём последние N
    matched = matched[:max_articles]

    return {
        "total_found": len(matched),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "articles": matched,
    }
