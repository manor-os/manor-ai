"""Web search tool — search the internet via configurable providers.

Provider priority:
  1. SEARCH_API_KEY set → use SEARCH_ENGINE (serper | tavily)
  2. No key → DuckDuckGo (free, no API key needed)
     a. ddgs package installed → real web search
     b. fallback → DuckDuckGo Instant Answer API (limited)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import warnings
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

try:
    from lxml import html as lxml_html
except ImportError:  # pragma: no cover - stripped OSS builds omit cloud XML deps
    lxml_html = None

from packages.core.services.web_fetch import fetch_url

logger = logging.getLogger(__name__)

# Optional: real DuckDuckGo web search (pip install ddgs)
_DDGS_AVAILABLE = False
try:
    from ddgs import DDGS
    _DDGS_AVAILABLE = True
except ImportError:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning)
            from duckduckgo_search import DDGS  # type: ignore
        _DDGS_AVAILABLE = True
    except ImportError:
        DDGS = None  # type: ignore

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Returns a list of "
            "results with titles, snippets, and URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 10).",
                },
            },
            "required": ["query"],
        },
    },
}

WEB_EVENT_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_event_search",
        "description": (
            "Find topic-relevant public events for a geographic location and date range. "
            "Returns structured events with titles, start/end times, venues, source sites, and URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Backward-compatible combined search query. Prefer location and topics.",
                },
                "location": {
                    "type": "string",
                    "description": "Geographic area only, without presentation or scheduling instructions.",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                    "description": "Required event interests expressed as one or more topic phrases.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Inclusive start date in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Inclusive end date in YYYY-MM-DD format (maximum 31 days after start).",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Maximum number of structured events to return (default 20, max 40).",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def _web_search(entity_id: str, **kwargs: Any) -> str:
    query = (kwargs.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    num_results = min(int(kwargs.get("num_results") or 5), 10)

    api_key = os.getenv("SEARCH_API_KEY", "")
    search_engine = os.getenv("SEARCH_ENGINE", "serper")  # serper | tavily

    try:
        if api_key:
            if search_engine == "tavily":
                return await _tavily_search(api_key, query, num_results)
            else:
                return await _serper_search(api_key, query, num_results)
        else:
            # Free fallback: DuckDuckGo (no API key required)
            return await _duckduckgo_search(query, num_results)
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return json.dumps({"error": f"Search failed: {e}"})


def _clean_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _parse_event_datetime(value: Any) -> datetime | None:
    raw = _clean_text(value, 80)
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        for format_string in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, format_string)
            except ValueError:
                continue
    return None


def _node_attr(node: Any, xpath: str, *attributes: str) -> str:
    matches = node.xpath(xpath)
    if not matches:
        return ""
    match = matches[0]
    if isinstance(match, str):
        return _clean_text(match)
    for attribute in attributes:
        value = match.get(attribute)
        if value:
            return _clean_text(value)
    return _clean_text(" ".join(match.itertext()))


def _event_intersects(
    start_at: datetime,
    end_at: datetime | None,
    range_start: date,
    range_end: date,
) -> bool:
    event_end = end_at or start_at
    return start_at.date() <= range_end and event_end.date() >= range_start


def _event_record(
    *,
    title: str,
    url: str,
    start_at: datetime,
    end_at: datetime | None,
    venue: str,
    summary: str,
    source_url: str,
    query: str,
) -> dict[str, Any]:
    resolved_url = urljoin(source_url, url) if url else source_url
    return {
        "title": _clean_text(title, 240),
        "url": resolved_url,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat() if end_at else None,
        "venue": _clean_text(venue, 160) or None,
        "summary": _clean_text(summary, 360) or None,
        "source": (urlparse(source_url).hostname or "").removeprefix("www."),
        "location_query": query,
    }


def _event_topics(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    topics: list[str] = []
    for item in value[:8]:
        topic = _clean_text(item, 80)
        if topic and topic.casefold() not in {existing.casefold() for existing in topics}:
            topics.append(topic)
    return topics


def _event_text_contains(text: str, phrase: str) -> bool:
    words = re.findall(r"[\w+#.-]+", phrase.casefold())
    if not words:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
    return re.search(pattern, text.casefold()) is not None


def _event_matches_topics(event: dict[str, Any], topics: list[str]) -> bool:
    if not topics:
        return True
    searchable = " ".join(
        str(event.get(field) or "")
        for field in ("title", "summary", "venue")
    )
    for topic in topics:
        if not _event_text_contains(searchable, topic):
            return False
    return True


_EVENT_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)


def _search_result_event(
    result: dict[str, Any],
    *,
    location: str,
    topics: list[str],
    range_start: date,
    range_end: date,
) -> dict[str, Any] | None:
    title = _clean_text(result.get("title"), 240)
    summary = _clean_text(result.get("snippet"), 360)
    url = str(result.get("url") or "")
    if not title or not url.startswith("https://"):
        return None
    evidence = f"{title} {summary}"
    topic_probe = {"title": title, "summary": summary, "venue": ""}
    if not _event_matches_topics(topic_probe, topics):
        return None
    date_match = re.search(
        rf"\b({_EVENT_MONTH_PATTERN})\s+(\d{{1,2}})"
        r"(?:\s*[\u2013\u2014-]\s*(\d{1,2}))?"
        r"(?:,?\s+(20\d{2}))?\b",
        evidence,
        flags=re.IGNORECASE,
    )
    if not date_match:
        return None
    month_text, start_day, end_day, year_text = date_match.groups()
    year = int(year_text or range_start.year)
    try:
        month = datetime.strptime(month_text[:3], "%b").month
        event_start = date(year, month, int(start_day))
        event_end = date(year, month, int(end_day or start_day))
    except ValueError:
        return None
    if event_end < range_start or event_start > range_end:
        return None
    return _event_record(
        title=title,
        url=url,
        start_at=datetime.combine(event_start, time.min),
        end_at=(
            datetime.combine(event_end, time.max)
            if event_end != event_start
            else None
        ),
        venue=location,
        summary=summary,
        source_url=url,
        query=location,
    )


def _extract_events_from_html(
    content: bytes,
    source_url: str,
    query: str,
    range_start: date,
    range_end: date,
) -> list[dict[str, Any]]:
    if lxml_html is None:
        return []
    try:
        document = lxml_html.fromstring(content)
    except (ValueError, TypeError):
        return []

    events: list[dict[str, Any]] = []
    for node in document.xpath("//*[@itemtype and contains(@itemtype, 'schema.org/Event')]"):
        title = _node_attr(node, ".//*[@itemprop='name']", "content")
        start_at = _parse_event_datetime(
            _node_attr(node, ".//*[@itemprop='startDate']", "content", "datetime")
        )
        end_at = _parse_event_datetime(
            _node_attr(node, ".//*[@itemprop='endDate']", "content", "datetime")
        )
        if not title or not start_at or not _event_intersects(start_at, end_at, range_start, range_end):
            continue
        events.append(
            _event_record(
                title=title,
                url=_node_attr(node, ".//*[@itemprop='url']", "href", "content")
                or node.get("data-permalink", ""),
                start_at=start_at,
                end_at=end_at,
                venue=_node_attr(
                    node,
                    ".//*[@itemprop='location']//*[@itemprop='name']",
                    "content",
                ),
                summary=_node_attr(node, ".//*[@itemprop='description']", "content"),
                source_url=source_url,
                query=query,
            )
        )

    for date_node in document.xpath("//*[@data-event-date]"):
        start_at = _parse_event_datetime(date_node.get("data-event-date"))
        end_at = _parse_event_datetime(date_node.get("data-event-date-end"))
        if not start_at or not _event_intersects(start_at, end_at, range_start, range_end):
            continue
        cards = date_node.xpath(
            "ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' post ') "
            "or contains(concat(' ', normalize-space(@class), ' '), ' event ')][1]"
        )
        card = cards[0] if cards else date_node
        links = card.xpath(
            ".//div[contains(concat(' ', normalize-space(@class), ' '), ' title ')]//a[1] "
            "| .//a[@rel='bookmark'][1] | .//h2//a[1] | .//h3//a[1] | .//h4//a[1]"
        )
        if not links:
            continue
        link = links[0]
        title = _clean_text(link.get("title") or " ".join(link.itertext()), 240)
        if not title:
            continue
        events.append(
            _event_record(
                title=title,
                url=link.get("href", ""),
                start_at=start_at,
                end_at=end_at,
                venue="",
                summary=_node_attr(card, ".//p[1]"),
                source_url=source_url,
                query=query,
            )
        )

    return events


async def _web_event_search(entity_id: str, **kwargs: Any) -> str:
    legacy_query = _clean_text(kwargs.get("query"), 240)
    location = _clean_text(kwargs.get("location"), 160) or legacy_query
    topics = _event_topics(kwargs.get("topics"))
    query = " ".join(part for part in (location, *topics) if part)
    try:
        range_start = date.fromisoformat(str(kwargs.get("start_date") or ""))
        range_end = date.fromisoformat(str(kwargs.get("end_date") or ""))
    except ValueError:
        return json.dumps({"error": "start_date and end_date must use YYYY-MM-DD"})
    if not location:
        return json.dumps({"error": "location or query is required"})
    if range_end < range_start or (range_end - range_start).days > 31:
        return json.dumps({"error": "end_date must be within 31 days after start_date"})
    num_results = max(1, min(int(kwargs.get("num_results") or 20), 40))
    date_window = (
        f"{range_start.strftime('%B')} {range_start.day} {range_start.year} "
        f"{range_end.strftime('%B')} {range_end.day} {range_end.year}"
    )
    search_queries = [
        f"{query} events {date_window}",
        f"site:eventbrite.com {query} {date_window}",
        f"site:lu.ma {query} {date_window}",
        f"{query} events calendar {range_start.strftime('%B %Y')}",
    ]
    search_payloads = await asyncio.gather(
        *(
            _web_search(entity_id, query=search_query, num_results=6)
            for search_query in search_queries
        )
    )
    search_results = [
        result
        for payload in search_payloads
        for result in json.loads(payload).get("results", [])
    ]
    source_urls = []
    for result in search_results:
        url = str(result.get("url") or "")
        if url.startswith("https://") and url not in source_urls:
            source_urls.append(url)
        if len(source_urls) >= 20:
            break

    async def fetch_events(url: str) -> list[dict[str, Any]]:
        try:
            fetched = await fetch_url(url, timeout=15, max_bytes=2_000_000)
            if "html" not in fetched.content_type.lower():
                return []
            return _extract_events_from_html(
                fetched.content,
                fetched.url,
                location,
                range_start,
                range_end,
            )
        except (httpx.HTTPError, ValueError):
            return []

    extracted = await asyncio.gather(*(fetch_events(url) for url in source_urls))
    if not any(extracted):
        fallback_payload = json.loads(
            await _web_search(
                entity_id,
                query=f"{query} upcoming events things to do",
                num_results=10,
            )
        )
        fallback_urls = []
        for result in fallback_payload.get("results", []):
            url = str(result.get("url") or "")
            if url.startswith("https://") and url not in source_urls and url not in fallback_urls:
                fallback_urls.append(url)
        search_results.extend(fallback_payload.get("results", []))
        source_urls.extend(fallback_urls)
        extracted.extend(await asyncio.gather(*(fetch_events(url) for url in fallback_urls)))
    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    search_result_events = [
        event
        for result in search_results
        if isinstance(result, dict)
        for event in [
            _search_result_event(
                result,
                location=location,
                topics=topics,
                range_start=range_start,
                range_end=range_end,
            )
        ]
        if event is not None
    ]
    for event in [*(item for group in extracted for item in group), *search_result_events]:
        key = (
            str(event.get("url") or ""),
            str(event.get("title") or "").lower(),
            str(event.get("start_at") or "")[:10],
        )
        deduplicated.setdefault(key, event)
    events = sorted(
        (
            event
            for event in deduplicated.values()
            if _event_matches_topics(event, topics)
        ),
        key=lambda event: (str(event.get("start_at") or ""), str(event.get("title") or "")),
    )[:num_results]
    return json.dumps(
        {
            "query": query,
            "location": location,
            "topics": topics,
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "events": events,
            "sources_checked": source_urls,
        }
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def _serper_search(api_key: str, query: str, num_results: int) -> str:
    """Search via Serper.dev Google Search API."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
        )
        resp.raise_for_status()
        data = resp.json()

    results = [
        {"title": item.get("title", ""), "url": item.get("link", ""), "snippet": item.get("snippet", "")}
        for item in data.get("organic", [])[:num_results]
    ]
    return json.dumps({"query": query, "results": results})


async def _tavily_search(api_key: str, query: str, num_results: int) -> str:
    """Search via Tavily Search API."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": num_results,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results = [
        {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("content", "")[:500]}
        for item in data.get("results", [])[:num_results]
    ]
    return json.dumps({"query": query, "results": results})


async def _duckduckgo_search(query: str, num_results: int) -> str:
    """Free DuckDuckGo search — real web search via ddgs, or Instant Answer API fallback."""
    # 1. Real search via ddgs package (pip install ddgs)
    if _DDGS_AVAILABLE and DDGS is not None:
        try:
            return await _ddgs_text_search(query, num_results)
        except Exception as e:
            logger.warning("DuckDuckGo ddgs search failed, trying Instant Answer API: %s", e)

    # 2. Fallback: DuckDuckGo Instant Answer API (limited but works without any package)
    return await _ddg_instant_answer(query, num_results)


async def _ddgs_text_search(query: str, num_results: int) -> str:
    """Real web search using ddgs package (run in thread since it's sync)."""
    def _search():
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning)
            return list(
                DDGS().text(
                    query,
                    region="wt-wt",
                    safesearch="moderate",
                    max_results=num_results,
                    backend="auto",
                )
            )

    raw = await asyncio.to_thread(_search)
    results = [
        {"title": r.get("title", ""), "url": r.get("href", r.get("url", "")), "snippet": r.get("body", "")}
        for r in raw
    ]
    return json.dumps({"query": query, "results": results[:num_results]})


async def _ddg_instant_answer(query: str, num_results: int) -> str:
    """DuckDuckGo Instant Answer API — no package needed, but limited results."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    if data.get("Abstract"):
        results.append({
            "title": data.get("Heading", query),
            "url": data.get("AbstractURL", ""),
            "snippet": data.get("Abstract"),
        })
    for topic in data.get("RelatedTopics", [])[:num_results - 1]:
        if isinstance(topic, dict) and topic.get("Text"):
            results.append({
                "title": topic.get("Text", "")[:80],
                "url": topic.get("FirstURL", ""),
                "snippet": topic.get("Text", ""),
            })

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "hint": "DuckDuckGo returned no results. Try a more specific query.",
        })

    return json.dumps({"query": query, "results": results[:num_results]})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (WEB_SEARCH_SCHEMA, _web_search),
        (WEB_EVENT_SEARCH_SCHEMA, _web_event_search),
    ]
