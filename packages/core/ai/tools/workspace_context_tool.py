"""workspace_search tool — lets the LLM query workspace state on demand.

Registered in tool_pool so it's available to any agent chatting in a
workspace context. The tool handler receives workspace_id from the
execution context (set by WorkspaceChat or EmbeddedChat).
"""
from __future__ import annotations

from typing import Any

WORKSPACE_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_search",
        "description": (
            "Search this workspace's goals, tasks, agents, knowledge, "
            "generated artifacts/files, execution plans, rules, runtime evidence, "
            "learning candidates, and decision history. Use when the "
            "user asks about workspace status, progress, specific items, "
            "or anything about what's happening in this workspace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for (keyword or question).",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "goals", "tasks", "agents", "knowledge", "artifacts",
                        "files", "generated_files", "plans", "rules", "history",
                        "runtime", "evidence", "learning", "all",
                    ],
                    "description": "Narrow search to a category. Default: all.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter tasks by status. Accepts canonical values like "
                        "'in_progress', 'pending', 'failed', 'completed' and aliases "
                        "like 'running', 'active', or '运行中'."
                    ),
                },
            },
            "required": [],
        },
    },
}


async def _workspace_search_handler(
    entity_id: str = "",
    workspace_id: str = "",
    **kwargs: Any,
) -> str:
    """Execute workspace_search through the Runtime Harness tool executor."""
    from packages.core.ai.runtime import runtime_workspace_search

    return await runtime_workspace_search(
        entity_id=entity_id,
        workspace_id=workspace_id,
        **kwargs,
    )


def get_tools():
    """Return tool schemas + handlers for registration."""
    return [
        (WORKSPACE_SEARCH_SCHEMA, _workspace_search_handler),
    ]
