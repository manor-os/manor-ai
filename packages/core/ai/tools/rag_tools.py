"""Document RAG — one tool, one job.

A single ``rag`` tool that semantically searches indexed documents for a
natural-language question. Optional ``workspace_id`` scopes the search to
documents bound to a specific workspace (everything else in the entity is
filtered out).

Indexing is NOT an agent concern — it happens automatically when documents
are uploaded or when files written via ``write_file`` are picked up by the
ingest watcher. There is no ``index_document`` tool.

``search_documents`` and ``list_documents`` remain metadata-only document
inventory tools; they are not RAG aliases and their filename matches are not
evidence of document contents. Agents who need document metadata use
``manor({action: "list_documents"})``; agents who need to produce final
deliverable files can use ``generate_document_file``; agents doing low-level
file I/O should use ``write_file`` and let the watcher index it.
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
            "Search indexed Knowledge document contents for facts, values, "
            "passages, summaries, comparisons, or calculations. Prefer RAG "
            "when intent mixes filename and content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Question requiring document-body evidence.",
                },
                "workspace_id": {
                    "type": "string",
                },
                "net_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "limit": {
                    "type": "integer",
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
