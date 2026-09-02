"""Document tools — search and list Knowledge documents."""
from __future__ import annotations

from typing import Any

from packages.core.ai.runtime import (
    runtime_document_cache_key,
    runtime_document_to_dict,
    runtime_list_documents_action,
    runtime_search_documents_action,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SEARCH_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Search user-visible Knowledge documents by filename and metadata. "
            "Use this when the user asks what files/documents they can see, "
            "when resolving # references, or when searching uploaded / AI-created "
            "deliverables. This tool does not retrieve document-body evidence. "
            "For facts, values, passages, summaries, comparisons, or calculations "
            "from inside documents, use rag. If the intent is mixed or uncertain, "
            "prefer rag; never infer document contents from a filename match. Do "
            "not use raw filesystem tools for user-visible Knowledge lists. Each "
            "result includes a markdown_link; copy it verbatim when mentioning a file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text to match against document names.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20).",
                },
                "detail": {
                    "type": "string",
                    "enum": ["summary", "details"],
                    "description": "summary returns minimal fields (default); details includes extra metadata.",
                },
            },
            "required": ["query"],
        },
    },
}

LIST_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_documents",
        "description": (
            "List user-visible Knowledge documents for the current entity. "
            "This is the user-facing document list: uploaded files, manually "
            "created files, and AI-created deliverables that are visible in "
            "Knowledge. Hidden/system paths, trash, sandbox output, and internal "
            "filesystem files are excluded. Use list_files only for internal "
            "filesystem inspection, never as the user's visible Knowledge list. "
            "Each result includes a markdown_link; copy it verbatim when mentioning a file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                },
                "detail": {
                    "type": "string",
                    "enum": ["summary", "details"],
                    "description": "summary returns minimal fields (default); details includes extra metadata.",
                },
            },
            "required": [],
        },
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc_to_dict(doc, *, detail: str = "summary") -> dict:
    return runtime_document_to_dict(doc, detail=detail)


async def _cache_key(action: str, entity_id: str, params: dict[str, Any]) -> str:
    return await runtime_document_cache_key(action, entity_id, params)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _search_documents(entity_id: str, user_id: str = "", **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_handler(kwargs, user_id=user_id)
    return await runtime_search_documents_action(
        entity_id=entity_id,
        user_id=runtime_context.user_id,
        workspace_id=runtime_context.workspace_id,
        params=kwargs,
    )


async def _list_documents(entity_id: str, user_id: str = "", **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_handler(kwargs, user_id=user_id)
    return await runtime_list_documents_action(
        entity_id=entity_id,
        user_id=runtime_context.user_id,
        workspace_id=runtime_context.workspace_id,
        params=kwargs,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (SEARCH_DOCUMENTS_SCHEMA, _search_documents),
        (LIST_DOCUMENTS_SCHEMA, _list_documents),
    ]
