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
from urllib.parse import urlparse

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
    api_key = bearer_token.strip() if isinstance(bearer_token, str) else ""
    if not api_key:
        return _error(
            "Tavily API key is missing. Get one at "
            "https://app.tavily.com/home and add it under "
            "Integrations → Tavily."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown tavily tool: {name}")
    if not isinstance(arguments, dict):
        return _error("arguments must be an object")
    arguments = dict(arguments)

    try:
        _validate_arguments(name, arguments)
        return _content(await handler(arguments, api_key))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"Tavily HTTP {exc.response.status_code}: {body}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tavily tool %s crashed", name)
        return _error(f"Tavily call failed: {exc}")


# ── Handlers ────────────────────────────────────────────────────────────────

_STRING_ARGUMENTS = {
    "search": {"query", "search_depth", "topic"},
}
_BOOLEAN_ARGUMENTS = {
    "search": {"include_answer", "include_raw_content"},
    "extract": {"include_images"},
}
_ARRAY_ARGUMENTS = {
    "search": {"include_domains", "exclude_domains"},
    "extract": {"urls"},
}


def _validate_arguments(name: str, arguments: dict[str, Any]) -> None:
    """Reject malformed MCP arguments before constructing a provider request."""
    for field in _STRING_ARGUMENTS.get(name, ()):
        value = arguments.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} must be a string")

    for field in _BOOLEAN_ARGUMENTS.get(name, ()):
        value = arguments.get(field)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{field} must be a boolean")

    for field in _ARRAY_ARGUMENTS.get(name, ()):
        value = arguments.get(field)
        if value is None:
            continue
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{field} must be an array")
        if any(not isinstance(item, str) for item in value):
            raise ValueError(f"{field} items must be strings")

def _max_results(value: Any) -> int:
    if value is None or value == "":
        return 5
    if isinstance(value, bool):
        raise ValueError("max_results must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("max_results must be an integer")
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_results must be an integer") from exc
    if count < 1:
        raise ValueError("max_results must be at least 1")
    return min(count, 20)


def _domain_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array of domain strings")
    domains = [str(item).strip() for item in value]
    if any(not domain for domain in domains):
        raise ValueError(f"{field} must not contain empty domains")
    return domains


def _url_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("urls must be an array of URL strings")
    if not value:
        raise ValueError("urls is required")
    if len(value) > 20:
        raise ValueError("urls must contain at most 20 URLs")
    urls = [str(item).strip() for item in value]
    if any(not url for url in urls):
        raise ValueError("urls must not contain empty values")
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("urls must contain absolute HTTP(S) URLs")
    return urls


async def _search(args: Dict[str, Any], api_key: str) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")

    body: Dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "max_results": _max_results(args.get("max_results")),
        "search_depth": args.get("search_depth") or "basic",
        "topic": args.get("topic") or "general",
        "include_answer": args.get("include_answer", True),
        "include_raw_content": bool(args.get("include_raw_content")),
    }
    if args.get("include_domains") is not None:
        body["include_domains"] = _domain_list(
            args["include_domains"], field="include_domains",
        )
    if args.get("exclude_domains") is not None:
        body["exclude_domains"] = _domain_list(
            args["exclude_domains"], field="exclude_domains",
        )

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
    urls = _url_list(args.get("urls"))

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
