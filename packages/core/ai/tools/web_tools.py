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
import warnings
from typing import Any

import httpx

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
    ]
