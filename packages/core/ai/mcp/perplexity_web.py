"""Perplexity (web) MCP wrapper — use the user's Pro subscription
instead of the API.

Why: Perplexity Pro gives unlimited Sonar Pro / Sonar Reasoning +
file uploads on the web. The API meters every call. For agents
running lots of research, the web path saves real money.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import _browser_runner


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "search",
            "description": (
                "Run a search-grounded query on Perplexity. Uses the "
                "user's Pro subscription. Returns the synthesized "
                "answer + cited source URLs."
            ),
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "focus": {
                        "type": "string",
                        "description": "Optional focus filter (Academic / Reddit / Writing / Wolfram / YouTube).",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model (Pro / Sonar Reasoning / o1 / GPT-4 / Claude).",
                    },
                },
            },
        },
        {
            "name": "follow_up",
            "description": "Append a follow-up query to an existing Perplexity thread.",
            "parameters": {
                "type": "object",
                "required": ["thread_id", "query"],
                "properties": {
                    "thread_id": {"type": "string"},
                    "query": {"type": "string"},
                },
            },
        },
    ]


async def call_tool(name: str, arguments: Dict[str, Any], bearer_token: str) -> Dict[str, Any]:
    return await _browser_runner.call_provider(
        provider="perplexity_web",
        name=name,
        arguments=arguments,
        bearer_token=bearer_token,
        timeout_ms=120_000,
    )
