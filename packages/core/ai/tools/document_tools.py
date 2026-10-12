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

DOCUMENT_QUERY_FILTER_PROPERTIES = {
    "workspace_ids": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 50,
        "uniqueItems": True,
        "description": "Match documents associated with any listed Workspace (OR).",
    },
    "folder_ids": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 50,
        "uniqueItems": True,
        "description": "Match any listed folder ID; use 'root' for top level (OR).",
    },
    "file_types": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 30,
        "uniqueItems": True,
        "description": "Match any file extension/type (OR).",
    },
    "sources": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 30,
        "uniqueItems": True,
        "description": "Match any document source (OR).",
    },
    "vector_statuses": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 10,
        "uniqueItems": True,
        "description": "Match any indexing status (OR).",
    },
    "file_size_min": {
        "type": "integer",
        "minimum": 0,
        "description": "Inclusive byte-size lower bound.",
    },
    "file_size_max": {
        "type": "integer",
        "minimum": 0,
        "description": "Inclusive byte-size upper bound.",
    },
    **{
        field: {
            "type": "string",
            "format": "date-time",
            "description": f"Inclusive {label} bound.",
        }
        for field, label in (
            ("created_after", "created-at lower"),
            ("created_before", "created-at upper"),
            ("updated_after", "updated-at lower"),
            ("updated_before", "updated-at upper"),
        )
    },
}

SEARCH_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Search user-visible Knowledge documents by file type, source, vector status, "
            "Workspace, folder, size, and created or updated date in one call. "
            "Plural filters are ORed; different filters and inclusive ranges are ANDed. "
            "Filtering happens before pagination and total is exact. "
            "Use this when the user asks what files/documents they can see, "
            "when resolving # references, or when searching uploaded / AI-created "
            "deliverables. This tool does not retrieve document-body evidence "
            "and never infers document contents from a filename match. Do "
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
                **DOCUMENT_QUERY_FILTER_PROPERTIES,
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Filtered-result offset (default 0).",
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
            "List user-visible Knowledge documents by file type, source, vector status, "
            "Workspace, folder, size, and created or updated date in one call. Plural "
            "filters are ORed; different filters and inclusive ranges are ANDed. "
            "Filtering happens before pagination and total is exact. "
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
                "query": {
                    "type": "string",
                    "description": "Optional filename and metadata search.",
                },
                **DOCUMENT_QUERY_FILTER_PROPERTIES,
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
