"""Minutes MCP using the runtime-selected, encrypted OAuth account.

The Minutes service derives identity and tenancy from the opaque grant. Runtime
actor fields are never sent as an alternative authorization identity.
"""
from __future__ import annotations

import contextvars
import json
from typing import Any, Dict, List

import httpx

from packages.core.services.manor_integration_oauth import manor_mcp_endpoint

_TIMEOUT = 30.0
_call_ctx_var: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "manor_minutes_mcp_call_ctx", default={},
)


def set_call_context(ctx: Dict[str, str]) -> None:
    _call_ctx_var.set({k: str(v) for k, v in (ctx or {}).items() if v is not None})


def clear_call_context() -> None:
    _call_ctx_var.set({})


def list_tools() -> List[Dict[str, Any]]:
    return [{"name": name, **spec} for name, spec in _TOOLS.items()]


def _ok(data: Any) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)}],
        "isError": False,
    }


def _error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def call_tool(name: str, arguments: Dict[str, Any], bearer_token: str) -> Dict[str, Any]:
    if name not in _TOOLS:
        return _error(f"Unknown tool: {name}")
    context = _call_ctx_var.get()
    if not context.get("user_id") or not context.get("entity_id"):
        return _error("Minutes requires the current Manor user and Entity context.")
    if not bearer_token or not bearer_token.isascii() or any(char.isspace() for char in bearer_token):
        return _error("Connect Minutes with OAuth in Integrations before calling its tools.")
    try:
        url = manor_mcp_endpoint("manor_mcp_minutes") + "/"
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
                headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json, text/event-stream"},
            )
    except (httpx.HTTPError, ValueError):
        return _error("Minutes request failed; no automatic retry was performed.")
    if response.status_code in {401, 403}:
        return _error("Minutes rejected this connection or its permissions; review the connection in Integrations.")
    if not 200 <= response.status_code < 300:
        return _error(f"Minutes returned HTTP {response.status_code}; redirects are not followed.")
    try:
        body = response.json()
    except ValueError:
        return _error("Minutes returned an invalid JSON response.")
    if not isinstance(body, dict):
        return _error("Minutes returned an invalid response envelope.")
    if body.get("error"):
        return _error("Minutes could not complete this tool request.")
    result = body.get("result")
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return {"content": result["content"], "isError": bool(result.get("isError"))}
    return _error("Minutes returned an invalid tool result.")


_MEETING_ID_PARAM = {
    "type": "string",
    "description": "Meeting id, as returned by search_meetings or list_recent_meetings",
}

_TOOLS: Dict[str, Dict[str, Any]] = {
    "search_meetings": {
        "description": "Search the entity's meetings by title or transcript content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text to find in meeting titles and transcripts"},
                "limit": {"type": "integer", "description": "Maximum number of results (default 10, max 50)"},
            },
            "required": ["query"],
        },
    },
    "get_transcript": {
        "description": "Get the full transcript of a meeting.",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": _MEETING_ID_PARAM},
            "required": ["meeting_id"],
        },
    },
    "get_summary": {
        "description": "Get a meeting's AI summary, key points, and action items.",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": _MEETING_ID_PARAM},
            "required": ["meeting_id"],
        },
    },
    "list_recent_meetings": {
        "description": "List the entity's most recent meetings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of meetings (default 10)"},
            },
            "required": [],
        },
    },
    "get_action_items": {
        "description": "Get the action items recorded for a meeting.",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": _MEETING_ID_PARAM},
            "required": ["meeting_id"],
        },
    },
    "chat_with_meeting": {
        "description": "Ask a question about a specific meeting; answered from its transcript.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": _MEETING_ID_PARAM,
                "question": {"type": "string", "description": "The question to answer from the meeting content"},
            },
            "required": ["meeting_id", "question"],
        },
    },
    "get_meeting_details": {
        "description": "Get a meeting's metadata: title, time, duration, platform, status, participants.",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": _MEETING_ID_PARAM},
            "required": ["meeting_id"],
        },
    },
    "get_meeting_stats": {
        "description": "Get aggregate meeting statistics for the entity (counts, duration totals).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
}
