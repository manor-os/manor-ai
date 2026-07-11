"""Document RAG — one tool, one job.

A single ``rag`` tool that semantically searches indexed documents for a
natural-language question. Optional ``workspace_id`` scopes the search to
documents bound to a specific workspace (everything else in the entity is
filtered out).

Indexing is NOT an agent concern — it happens automatically when documents
are uploaded or when files written via ``write_file`` are picked up by the
ingest watcher. There is no ``index_document`` tool.

Other legacy RAG tools (``search_knowledge``, ``search_documents``,
``list_documents``) have been retired. Agents who
need document metadata use ``manor({action: "list_documents"})``; agents
who need to produce final deliverable files can use ``generate_document_file``;
agents doing low-level file I/O should use ``write_file`` and let the watcher
index it.
"""
from __future__ import annotations

from typing import Any

from packages.core.ai.runtime import runtime_rag_action
from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_kwargs,
    runtime_tool_call_context_is_external_customer,
    runtime_tool_call_context_is_public_customer,
)


RAG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rag",
        "description": (
            "Semantically search the entity's indexed documents for a "
            "natural-language question. Returns ranked document excerpts.\n\n"
            "Use RAG when the answer lives inside uploaded files or "
            "knowledge-base documents. For literal file I/O on the "
            "filesystem, use read_file / glob_files / grep_files instead.\n\n"
            "Scope:\n"
            "  • No workspace_id → searches all documents in the entity.\n"
            "  • workspace_id=<id> → searches only documents bound to that "
            "workspace (via DocumentGroup.workspace_id).\n"
            "  • net_ids/group_ids → searches only those Knowledge Nets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The natural-language question to answer.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Optional — limit search to this workspace's documents.",
                },
                "net_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional — Knowledge Net ids to search. Alias: group_ids.",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional alias for net_ids.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max hits to return (default 5, max 20).",
                },
            },
            "required": ["question"],
        },
    },
}


async def _rag(entity_id: str, user_id: str = "", **kwargs: Any) -> str:
    """Semantic search across entity (or workspace-scoped) documents."""
    context = runtime_tool_call_context_from_kwargs(kwargs)
    params = {
        key: value
        for key, value in kwargs.items()
        if key not in {"conversation_id", "task_id"} and not str(key).startswith("_")
    }
    workspace_id = str(params.get("workspace_id") or context.workspace_id or "").strip()
    if workspace_id:
        params["workspace_id"] = workspace_id
    return await runtime_rag_action(
        entity_id=entity_id,
        user_id=user_id or context.user_id,
        workspace_id=workspace_id or context.workspace_id,
        client_visible_only=runtime_tool_call_context_is_external_customer(kwargs),
        public_agent_visible_only=runtime_tool_call_context_is_public_customer(kwargs),
        params=params,
    )


def get_tools() -> list[tuple[dict, callable]]:
    return [(RAG_SCHEMA, _rag)]
