"""Document tools — search, list, and generate Knowledge documents."""
from __future__ import annotations

from typing import Any

from packages.core.ai.runtime import (
    runtime_document_cache_key,
    runtime_document_to_dict,
    runtime_generate_document_file,
    runtime_list_documents_action,
    runtime_search_documents_action,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SEARCH_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Search user-visible Knowledge documents by name or query text. "
            "Use this when the user asks what files/documents they can see, "
            "when resolving # references, or when searching uploaded / AI-created "
            "deliverables. Do not use raw filesystem tools for user-visible "
            "Knowledge lists."
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
            "filesystem inspection, never as the user's visible Knowledge list."
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

GENERATE_DOCUMENT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_document_file",
        "description": (
            "Generate a user-visible document file from text/Markdown content "
            "and save it to Knowledge. Use this for final deliverables such as "
            ".md, .txt, .csv, .json, .html, .docx, .pptx, or .pdf. For .pdf, "
            ".docx, and .pptx this renders a real binary document, not a text "
            "file with a misleading extension. For editable AI diagrams from a "
            "prompt, use generate_file(kind='diagram') so a valid .diagram.json "
            "canvas is created. For low-level filesystem writes or internal "
            "notes, use write_file instead. For complex PDF editing such as "
            "merge/split/forms/OCR/watermarks, use the pdf skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Visible filename or relative Knowledge path, e.g. "
                        "'融资/Manor AI intro.pdf' or 'notes/summary.md'. "
                        "Hidden/system paths such as .ai/** are rejected."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Source text or Markdown content to render/save.",
                },
                "file_type": {
                    "type": "string",
                    "description": (
                        "Output extension without dot when name has no extension "
                        "(default 'txt'). Supported: md, txt, csv, json, html, "
                        "diagram.json, docx, pptx, pdf."
                    ),
                },
                "approval_token": {
                    "type": "string",
                    "description": "One-time token returned after the user approves creating/updating a user-visible document.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional previous source_sha256; refuses overwrite if target file changed.",
                },
            },
            "required": ["name", "content"],
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

async def _search_documents(entity_id: str, **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    return await runtime_search_documents_action(
        entity_id=entity_id,
        user_id=kwargs.get("user_id") or runtime_context.user_id,
        workspace_id=kwargs.get("workspace_id") or runtime_context.workspace_id,
        params=kwargs,
    )


async def _list_documents(entity_id: str, **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    return await runtime_list_documents_action(
        entity_id=entity_id,
        user_id=kwargs.get("user_id") or runtime_context.user_id,
        workspace_id=kwargs.get("workspace_id") or runtime_context.workspace_id,
        params=kwargs,
    )


async def _generate_document_file(entity_id: str, **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    return await runtime_generate_document_file(
        entity_id=entity_id,
        user_id=kwargs.get("user_id") or runtime_context.user_id or "",
        conversation_id=kwargs.get("conversation_id") or runtime_context.conversation_id or "",
        name=kwargs.get("name") or "",
        content=kwargs.get("content") or "",
        file_type=kwargs.get("file_type") or "txt",
        approval_token=kwargs.get("approval_token"),
        expected_sha256=kwargs.get("expected_sha256"),
        workspace_id=kwargs.get("workspace_id") or runtime_context.workspace_id,
        task_id=kwargs.get("task_id") or runtime_context.task_id,
        agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (SEARCH_DOCUMENTS_SCHEMA, _search_documents),
        (LIST_DOCUMENTS_SCHEMA, _list_documents),
        (GENERATE_DOCUMENT_FILE_SCHEMA, _generate_document_file),
    ]
