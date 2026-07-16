"""Fetch recent news for user-configured dashboard modules."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import logging
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)

_GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_CACHE_TTL_SECONDS = 10 * 60
_news_cache: dict[tuple[str, str, int, int], tuple[float, list[dict[str, Any]]]] = {}
_news_cache_lock = asyncio.Lock()


def _language_for_locale(locale: str | None) -> str:
    normalized = (locale or "en").lower()
    if normalized.startswith("zh"):
        return "Chinese"
    if normalized.startswith("es"):
        return "Spanish"
    if normalized.startswith("fr"):
        return "French"
    if normalized.startswith("de"):
        return "German"
    if normalized.startswith("ja"):
        return "Japanese"
    if normalized.startswith("ko"):
        return "Korean"
    return "English"


def _published_at(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return raw


def _normalize_articles(payload: object, limit: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
        return []

    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for raw in payload["articles"]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not title or not url.startswith(("https://", "http://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(
            {
                "id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:20],
                "title": title[:300],
                "url": url,
                "source": str(raw.get("domain") or "").strip()[:120] or None,
                "published_at": _published_at(raw.get("seendate")),
                "language": str(raw.get("language") or "").strip()[:40] or None,
            }
        )
        if len(items) >= limit:
            break
    return items


async def get_dashboard_news(
    *,
    query: str | None,
    days: int,
    limit: int,
    locale: str | None,
) -> list[dict[str, Any]]:
    """Return a small, cached list of recent articles from GDELT DOC 2.0."""

    clean_query = (query or "").strip()[:120]
    language = _language_for_locale(locale)
    search_days = min(days, 90)
    cache_key = (clean_query.casefold(), language, search_days, limit)
    now = time.monotonic()

    cached = _news_cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return [dict(item) for item in cached[1]]

    language_filter = f"sourcelang:{language}"
    gdelt_query = f"({clean_query}) {language_filter}" if clean_query else language_filter
    params = {
        "query": gdelt_query,
        "mode": "artlist",
        "maxrecords": min(50, max(limit * 2, limit)),
        "timespan": "24h" if search_days == 1 else f"{search_days}d",
        "sort": "datedesc",
        "format": "json",
    }

    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": "ManorAI-Dashboard/1.0"},
        ) as client:
            response = await client.get(_GDELT_DOC_URL, params=params)
            response.raise_for_status()
            items = _normalize_articles(response.json(), limit)
    except Exception as exc:
        logger.warning("Dashboard news request failed: %s", exc)
        return [dict(item) for item in cached[1]] if cached else []

    async with _news_cache_lock:
        _news_cache[cache_key] = (now, items)
        if len(_news_cache) > 128:
            oldest_key = min(_news_cache, key=lambda key: _news_cache[key][0])
            _news_cache.pop(oldest_key, None)
    return [dict(item) for item in items]
