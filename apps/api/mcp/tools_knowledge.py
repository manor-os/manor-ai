"""Read a Workspace's Knowledge: list its collections, search documents, read one.

Small text documents come back inline. Anything larger, and every binary file
(PDF, Office, images), goes through a transfer slot (see ``transfers``): the
agent downloads it with the returned curl command instead of receiving bytes
in a tool result.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from apps.api.mcp import transfers
from apps.api.mcp.registry import Tool, ToolContext, ToolError, boolean, require, string
from apps.api.mcp.tools_workspace import _shell_quote

_DOCS = "/api/v1/documents"

DEFAULT_INLINE_BYTES = 100_000
MAX_INLINE_BYTES = 500_000

_TEXT_MIME_TYPES = frozenset({
    "application/json", "application/ld+json", "application/x-ndjson", "application/xml",
    "application/yaml", "application/x-yaml", "application/toml", "application/javascript",
    "application/x-sh", "application/sql",
})
_TEXT_FILE_TYPES = frozenset({
    "md", "markdown", "txt", "text", "csv", "tsv", "json", "jsonl", "ndjson", "yaml", "yml",
    "toml", "ini", "xml", "html", "htm", "log", "sql", "py", "js", "ts", "sh", "css", "rst",
})
_DOCUMENT_KEYS = ("id", "name", "display_path", "folder_id", "file_type", "mime_type",
                  "file_size", "source", "vector_status", "updated_at")
_GROUP_KEYS = ("id", "name", "kind", "purpose", "is_default_collection", "document_count",
               "folders", "documents")


def _document(row: dict) -> dict:
    return {key: row.get(key) for key in _DOCUMENT_KEYS if key in row}


def is_text_document(document: dict) -> bool:
    mime = str(document.get("mime_type") or "").split(";")[0].strip().lower()
    if mime.startswith("text/") or mime in _TEXT_MIME_TYPES or mime.endswith(("+json", "+xml")):
        return True
    file_type = str(document.get("file_type") or "").lower().lstrip(".")
    if not file_type:
        file_type = os.path.splitext(str(document.get("name") or ""))[1].lower().lstrip(".")
    return file_type in _TEXT_FILE_TYPES


async def list_knowledge(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    groups = await ctx.api.get(f"/api/v1/workspaces/{ws_id}/documents", initialize=False)
    return {"collections": [{key: group.get(key) for key in _GROUP_KEYS} for group in groups or []]}


async def search_knowledge(ctx: ToolContext, args: dict) -> Any:
    query = str(require(args, "query")).strip()
    limit = min(max(int(args.get("limit") or 20), 1), 100)
    kinds = [str(kind) for kind in (args.get("kinds") or [])]
    page = await ctx.api.get(
        _DOCS, search=query, workspace_id=args.get("workspace_id"),
        folder_id=args.get("folder_id"), limit=limit,
        **({"kind": kinds} if kinds else {}),
        **({"results_only": "true"} if args.get("results_only") else {}),
        include_usage="false",
    )
    return {"total": page.get("total"), "documents": [_document(row) for row in page.get("items") or []]}


async def search_knowledge_content(ctx: ToolContext, args: dict) -> Any:
    query = str(require(args, "query")).strip()
    limit = min(max(int(args.get("limit") or 10), 1), 50)
    result = await ctx.api.get(
        f"{_DOCS}/content-search", q=query, workspace_id=args.get("workspace_id"),
        folder_id=args.get("folder_id"), limit=limit,
    )
    return {
        "matches": [
            {"document": _document(item.get("document") or {}), "score": item.get("score"),
             "snippet": item.get("snippet")}
            for item in result.get("items") or []
        ],
        "scanned": result.get("scanned"),
        "partial": result.get("partial"),
    }


async def _download_slot(ctx: ToolContext, document: dict, data: bytes, content_type: str) -> dict:
    filename = os.path.basename(str(document.get("name") or "")) or f"{document.get('id')}.bin"
    slot = await transfers.create(
        kind=transfers.DOWNLOAD, user_id=ctx.user_id, entity_id=ctx.entity_id,
        filename=filename, content_type=content_type, data=data,
    )
    url = ctx.transfer_url(slot.id)
    return {
        "document": _document(document),
        "download_url": url,
        "command": f"curl -sS --fail -o {_shell_quote(filename)} {_shell_quote(url)}",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "expires_in_seconds": transfers.TRANSFER_TTL_SECONDS,
    }


async def read_knowledge_document(ctx: ToolContext, args: dict) -> Any:
    doc_id = require(args, "document_id")
    inline_limit = min(int(args.get("max_inline_bytes") or DEFAULT_INLINE_BYTES), MAX_INLINE_BYTES)
    document = await ctx.api.get(f"{_DOCS}/{doc_id}")
    size = document.get("file_size")
    if isinstance(size, int) and size > transfers.MAX_TRANSFER_BYTES:
        raise ToolError(
            f"{document.get('name')} is {size} bytes, over the "
            f"{transfers.MAX_TRANSFER_BYTES // (1024 * 1024)} MiB transfer limit; open it in Manor",
        )
    as_file = bool(args.get("as_file"))
    if is_text_document(document) and not (as_file or (isinstance(size, int) and size > inline_limit)):
        content = (await ctx.api.get(f"{_DOCS}/{doc_id}/content")).get("content") or ""
        data = content.encode("utf-8")
        if len(data) <= inline_limit:
            return {"document": _document(document), "content": content, "bytes": len(data)}
        return await _download_slot(ctx, document, data, "text/plain; charset=utf-8")
    data, content_type = await ctx.api.download(f"{_DOCS}/{doc_id}/download")
    return await _download_slot(ctx, document, data, document.get("mime_type") or content_type)


_WORKSPACE_ID = string("Workspace id (from list_workspaces).")

TOOLS = [
    Tool("list_knowledge", "List Workspace Knowledge",
         "A Workspace's Knowledge: each collection (the default collection, other Knowledge "
         "Nets, and files generated by its tasks and agents) with its folders and documents "
         "(id, name, type, size, indexing status). Read one with read_knowledge_document.",
         list_knowledge, properties={"workspace_id": _WORKSPACE_ID},
         required=("workspace_id",), read_only=True, idempotent=True),
    Tool("search_knowledge", "Search Knowledge",
         "Find Knowledge documents this user can read whose name, path, file type, source or "
         "metadata match 'query'. To search what documents say, use search_knowledge_content. "
         "Scope it to one Workspace or folder.",
         search_knowledge,
         properties={"query": string("Words to match, e.g. pricing or .pdf."),
                     "workspace_id": string("Only documents in this Workspace's Knowledge."),
                     "folder_id": string("Only documents in this folder and its subfolders."),
                     "kinds": {"type": "array", "items": {"enum": ["audio", "video", "image", "document"]},
                               "description": "Only these media kinds, e.g. [\"audio\"] for songs."},
                     "results_only": {"type": "boolean", "default": False,
                                      "description": "Leave out files made on the way to a result "
                                                     "(QA frames, edit recipes, timelines)."},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
         required=("query",), read_only=True, idempotent=True),
    Tool("search_knowledge_content", "Search Knowledge content",
         "Full-text search of the Knowledge documents this user can read: documents whose "
         "text matches 'query' (words, numbers, Chinese phrases), best first, each with a "
         "score and the snippet that matched. Uploaded images, video and audio match on "
         "their automatic description, on-screen text and transcript. Scans the newest visible documents within a "
         "time budget; 'partial' says it stopped before covering them all, so narrow it to "
         "a Workspace or folder. Read a whole match with read_knowledge_document.",
         search_knowledge_content,
         properties={"query": string("What the documents say, e.g. annual plan price."),
                     "workspace_id": string("Only documents in this Workspace's Knowledge."),
                     "folder_id": string("Only documents in this folder and its subfolders."),
                     "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
         required=("query",), read_only=True, idempotent=True),
    Tool("read_knowledge_document", "Read Knowledge document",
         "Read one Knowledge document. Text up to max_inline_bytes comes back as 'content'; "
         "larger text and binary files (PDF, Office, images) return a 30-minute download URL "
         "and a curl command that saves the file under its name.",
         read_knowledge_document,
         properties={"document_id": string("Document id (from list_knowledge or a search)."),
                     "max_inline_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_INLINE_BYTES,
                                          "default": DEFAULT_INLINE_BYTES,
                                          "description": "Largest text returned inline."},
                     "as_file": boolean("Always return a download, even for small text.", False)},
         required=("document_id",), read_only=True, idempotent=True),
]
