"""Claude.ai (web) MCP wrapper — leverage user's Anthropic subscription.

Drives claude.ai via the browser-runner sidecar. User exports their
Claude.ai cookies (Cookie-Editor → JSON) and pastes into Manor;
Manor then can have agents call Claude on their behalf without
spending API credits.

Auth: bearer_token = exported cookie JSON (Playwright storage_state
or Cookie-Editor list — both formats accepted).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from . import _browser_runner

logger = logging.getLogger(__name__)


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "new_chat",
            "description": (
                "Start a fresh Claude.ai conversation with a prompt. "
                "Returns the chat_id and Claude's first response. The "
                "user's Claude Pro/Max subscription is used."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {
                        "type": "string",
                        "description": "Optional model override (e.g. 'Opus 4.5'). Defaults to user's account default.",
                    },
                },
            },
        },
        {
            "name": "continue_chat",
            "description": (
                "Append a prompt to an existing Claude.ai conversation."
            ),
            "parameters": {
                "type": "object",
                "required": ["chat_id", "prompt"],
                "properties": {
                    "chat_id": {"type": "string"},
                    "prompt": {"type": "string"},
                },
            },
        },
        {
            "name": "list_chats",
            "description": "List the user's recent Claude.ai conversations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows. Default 20."},
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
            "Claude.ai cookies are missing. Export them from a browser "
            "where you're signed into Claude (Cookie-Editor → JSON), "
            "then paste into Integrations → Claude.ai."
        )
    storage_state = _browser_runner.parse_storage_state(bearer_token)
    if not storage_state:
        return _error(
            "Could not parse the Claude.ai cookies. Expected either "
            "Playwright storage_state JSON or a Cookie-Editor export."
        )

    try:
        resp = await _browser_runner.perform(
            provider="claude_ai_web",
            action=name,
            params=arguments,
            storage_state=storage_state,
            timeout_ms=240_000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("claude_ai_web tool %s crashed", name)
        return _error(f"Claude.ai call failed: {exc}")

    if not resp.get("ok"):
        return _error(resp.get("error") or "browser-runner returned non-ok")
    return _content(json.dumps(resp.get("result") or {}, ensure_ascii=False, indent=2))


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}
