"""Gemini (web) MCP wrapper — use the user's Google AI / Gemini
Advanced subscription, not the API.

Drives gemini.google.com via the browser-runner sidecar. Same Google
session cookies as NotebookLM — exporting once gives access to both.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import _browser_runner


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "new_chat",
            "description": (
                "Start a fresh Gemini conversation. Uses the user's "
                "Google AI / Gemini Advanced subscription quota."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {
                        "type": "string",
                        "description": "Optional (e.g. '2.5 Pro', '2.5 Flash', '2.5 Deep Think').",
                    },
                },
            },
        },
        {
            "name": "continue_chat",
            "description": "Append a turn to an existing Gemini conversation.",
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
            "description": "List the user's recent Gemini conversations.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
    ]


async def call_tool(name: str, arguments: Dict[str, Any], bearer_token: str) -> Dict[str, Any]:
    return await _browser_runner.call_provider(
        provider="gemini_web",
        name=name,
        arguments=arguments,
        bearer_token=bearer_token,
        timeout_ms=180_000,
    )
