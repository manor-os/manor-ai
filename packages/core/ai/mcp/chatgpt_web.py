"""ChatGPT (web) MCP wrapper — leverage user's Plus / Team subscription.

Drives chatgpt.com via the browser-runner sidecar. User pastes their
exported cookies once; agents call Claude on their behalf using the
subscription quota (no API spend).
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import _browser_runner


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "new_chat",
            "description": (
                "Start a fresh ChatGPT conversation. Uses the user's "
                "Plus/Team subscription quota — no OpenAI API spend. "
                "Returns chat_id + first response."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {
                        "type": "string",
                        "description": "Optional model (e.g. 'GPT-4o', 'o1', 'o3-mini').",
                    },
                },
            },
        },
        {
            "name": "continue_chat",
            "description": "Append a turn to an existing ChatGPT conversation.",
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
            "description": "List the user's recent ChatGPT conversations from the sidebar.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
    ]


async def call_tool(name: str, arguments: Dict[str, Any], bearer_token: str) -> Dict[str, Any]:
    return await _browser_runner.call_provider(
        provider="chatgpt_web",
        name=name,
        arguments=arguments,
        bearer_token=bearer_token,
        timeout_ms=240_000,
    )
