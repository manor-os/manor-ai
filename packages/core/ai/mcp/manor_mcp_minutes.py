"""First-party Manor Minutes MCP.

Bridges Manor agents to the Minutes meeting-notes service
(minutes.manorai.xyz): search meetings, read transcripts, summaries,
action items, and ask questions about a meeting.

Transport: Minutes exposes a stateless MCP-over-HTTP endpoint
(``/api/mcp``, JSON-RPC request/response). This module forwards
``tools/call`` requests with service credentials — the shared
``MEETING_NOTE_TAKER_API_KEY`` plus the acting entity's id — so every
query is tenant-scoped by the Minutes backend itself.

Config:
    MINUTES_MCP_URL             endpoint (default https://minutes.manorai.xyz/api/mcp/)
    MEETING_NOTE_TAKER_API_KEY  explicit shared service API key (optional)

When no explicit key is set, the service key is derived from the OAuth
client secret Manor already issued to the Minutes app
(``MANOR_OAUTH_CLIENT_<SLUG>_SECRET`` for the ``minutes-cloud`` client)
— the one secret both deployments already share — so the bridge works
with zero extra provisioning. Minutes accepts the same derivation.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
from typing import Any, Dict, List, Optional

import httpx

_DEFAULT_URL = "https://minutes.manorai.xyz/api/mcp/"
_TIMEOUT = 30.0

# Must match the Minutes backend's derivation exactly
# (backend/api/services/api_key_service.py in Meeting-note-taker).
_DERIVED_KEY_CONTEXT = b"manor-minutes-mcp-service-key-v1"
_MINUTES_CLIENT_ID_DEFAULT = "minutes-cloud"


def _resolve_service_key() -> Optional[str]:
    """Explicit MEETING_NOTE_TAKER_API_KEY, else a key derived from the
    Minutes OAuth client secret already present in Manor's environment."""
    explicit = os.getenv("MEETING_NOTE_TAKER_API_KEY", "").strip()
    if explicit:
        return explicit

    target_client_id = os.getenv("MINUTES_OAUTH_CLIENT_ID", _MINUTES_CLIENT_ID_DEFAULT)
    prefix, suffix = "MANOR_OAUTH_CLIENT_", "_SECRET"
    for key, secret in os.environ.items():
        if not key.startswith(prefix) or not key.endswith(suffix) or not secret:
            continue
        slug = key[len(prefix):-len(suffix)]
        if not slug:
            continue
        client_id = os.getenv(f"{prefix}{slug}_CLIENT_ID") or slug.lower().replace("_", "-")
        if client_id == target_client_id:
            return hmac.new(secret.encode(), _DERIVED_KEY_CONTEXT, hashlib.sha256).hexdigest()
    return None

_call_ctx_var: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "manor_minutes_mcp_call_ctx",
    default={},
)


def set_call_context(ctx: Dict[str, str]) -> None:
    _call_ctx_var.set({
        k: str(v) for k, v in (ctx or {}).items() if v is not None
    })


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
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    del bearer_token  # first-party: authenticated with the service API key

    if name not in _TOOLS:
        return _error(f"Unknown tool: {name}")

    api_key = _resolve_service_key()
    if not api_key:
        return _error(
            "Minutes is not configured: set MEETING_NOTE_TAKER_API_KEY on the "
            "Manor API service, or provision the minutes-cloud OAuth client "
            "secret (MANOR_OAUTH_CLIENT_MINUTES_CLOUD_SECRET) so the service "
            "key can be derived from it."
        )
    entity_id = _call_ctx_var.get().get("entity_id", "")
    if not entity_id:
        return _error("No entity context for this call — cannot scope meeting access.")

    url = os.getenv("MINUTES_MCP_URL", _DEFAULT_URL)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": api_key,
        "X-Entity-Id": entity_id,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return _error(f"Minutes service unreachable: {exc}")

    if resp.status_code == 401:
        return _error("Minutes rejected the service credentials (API key/entity).")
    if resp.status_code >= 400:
        return _error(f"Minutes returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        body = resp.json()
    except ValueError:
        return _error(f"Minutes returned a non-JSON response: {resp.text[:300]}")

    if body.get("error"):
        err = body["error"]
        return _error(f"Minutes RPC error {err.get('code')}: {err.get('message')}")

    result = body.get("result") or {}
    # The Minutes server already answers in the MCP envelope shape
    # ({content: [...], isError}); pass it through when well-formed.
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return {
            "content": result["content"],
            "isError": bool(result.get("isError")),
        }
    return _ok(result)


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
