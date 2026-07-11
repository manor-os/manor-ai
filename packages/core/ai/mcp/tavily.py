"""Tavily MCP server — web search + content extraction tuned for AI agents.

Why Tavily over Manor's existing Serper search? Tavily returns
agent-friendly synthesized snippets and supports ``include_raw_content``
to inline article text in one call — saves the agent from having to
make a second fetch. Free tier covers 1000 calls/month, plenty for
demos.

Auth: bearer_token = the user's Tavily API key
(``tvly-...``), stored as an entity Integration with
provider="tavily" and credentials ``{"api_key": "tvly-..."}``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.tavily.com"
_TIMEOUT = 30.0
_MAX_PAYLOAD_CHARS = 10_000


# ── MCP protocol ────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "search",
            "description": (
                "Run a web search via Tavily. Optimized for AI agents: "
                "returns synthesized snippets, an optional 1-sentence "
                "answer, and (when requested) inline article text. "
                "Free tier: 1000 calls/month."
            ),
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {
                        "type": "integer",
                        "description": "1-20. Default 5.",
                    },
                    "search_depth": {
                        "type": "string",
                        "description": "'basic' (cheap, ~3 results) or 'advanced' (deeper, more credits). Default 'basic'.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "'general' (default), 'news' (fresh-only), 'finance'.",
                    },
                    "include_answer": {
                        "type": "boolean",
                        "description": "When true, Tavily synthesizes a single-sentence answer alongside results. Default true.",
                    },
                    "include_raw_content": {
                        "type": "boolean",
                        "description": "When true, each result includes the full article body. Useful for downstream summarization. Default false.",
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to these domains (e.g. ['producthunt.com']).",
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        {
            "name": "extract",
            "description": (
                "Extract clean article body from one or more URLs. Use "
                "after search() when you need the full text of "
                "specific results."
            ),
            "parameters": {
                "type": "object",
                "required": ["urls"],
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1-20 URLs to fetch.",
                    },
                    "include_images": {"type": "boolean"},
                },
            },
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    if not bearer_token:
        return _error(
            "Tavily API key is missing. Get one at "
            "https://app.tavily.com/home and add it under "
            "Integrations → Tavily."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown tavily tool: {name}")

    try:
        return _content(await handler(arguments, bearer_token))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"Tavily HTTP {exc.response.status_code}: {body}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tavily tool %s crashed", name)
        return _error(f"Tavily call failed: {exc}")


# ── Handlers ────────────────────────────────────────────────────────────────

async def _search(args: Dict[str, Any], api_key: str) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")

    body: Dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "max_results": int(args.get("max_results") or 5),
        "search_depth": args.get("search_depth") or "basic",
        "topic": args.get("topic") or "general",
        "include_answer": args.get("include_answer", True),
        "include_raw_content": bool(args.get("include_raw_content")),
    }
    if args.get("include_domains"):
        body["include_domains"] = list(args["include_domains"])
    if args.get("exclude_domains"):
        body["exclude_domains"] = list(args["exclude_domains"])

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(f"{_API}/search", json=body)
        r.raise_for_status()
        data = r.json()

    return _truncate(json.dumps({
        "query": query,
        "answer": data.get("answer"),
        "results": [
            {
                "title": x.get("title"),
                "url": x.get("url"),
                "score": x.get("score"),
                "snippet": (x.get("content") or "")[:600],
                "raw": (x.get("raw_content") or "")[:2000] if body["include_raw_content"] else None,
            }
            for x in (data.get("results") or [])
        ],
    }, ensure_ascii=False, indent=2))


async def _extract(args: Dict[str, Any], api_key: str) -> str:
    urls = args.get("urls") or []
    if not urls:
        raise ValueError("urls is required")

    body = {
        "api_key": api_key,
        "urls": list(urls),
        "include_images": bool(args.get("include_images")),
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(f"{_API}/extract", json=body)
        r.raise_for_status()
        data = r.json()

    return _truncate(json.dumps({
        "results": [
            {
                "url": x.get("url"),
                "raw_content": (x.get("raw_content") or "")[:3000],
                "images": x.get("images") or [],
            }
            for x in (data.get("results") or [])
        ],
        "failed": data.get("failed_results") or [],
    }, ensure_ascii=False, indent=2))


def _truncate(s: str) -> str:
    return s if len(s) <= _MAX_PAYLOAD_CHARS else s[:_MAX_PAYLOAD_CHARS] + "\n… (truncated)"


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


_HANDLERS = {
    "search": _search,
    "extract": _extract,
}
