"""Runtime-owned facades for Knowledge document tool actions."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any


RUNTIME_DOCUMENT_TOOL_CACHE_TTL_SECONDS = int(os.getenv("DOCUMENT_TOOL_CACHE_TTL_SECONDS", "900"))
RUNTIME_DOCUMENT_LIST_DEFAULT_LIMIT = 20
RUNTIME_DOCUMENT_LIST_MAX_LIMIT = 50
RUNTIME_DOCUMENT_CACHE_NAMESPACE = "documents"


def runtime_document_to_dict(doc: Any, *, detail: str = "summary") -> dict[str, Any]:
    data = {
        "id": doc.id,
        "name": doc.name,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
    }
    if detail == "details":
        data.update({
            "mime_type": getattr(doc, "mime_type", None),
            "source": getattr(doc, "source", None),
            "vector_status": getattr(doc, "vector_status", None),
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "folder_id": getattr(doc, "folder_id", None),
            "fs_path": getattr(doc, "fs_path", None),
        })
    return data


async def runtime_document_cache_key(action: str, entity_id: str, params: dict[str, Any]) -> str:
    from packages.core.services.tool_cache_version import get_tool_cache_version

    version = await get_tool_cache_version(entity_id, RUNTIME_DOCUMENT_CACHE_NAMESPACE)
    raw = json.dumps(params or {}, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"tool:documents:{entity_id}:v{version}:{action}:{digest}"


async def _runtime_get_cached_document_action(action: str, entity_id: str, params: dict[str, Any]) -> str | None:
    try:
        from packages.core.cache import cache

        cached = await cache.get(await runtime_document_cache_key(action, entity_id, params))
        return cached if isinstance(cached, str) else None
    except Exception:
        return None


async def _runtime_set_cached_document_action(action: str, entity_id: str, params: dict[str, Any], value: str) -> None:
    if RUNTIME_DOCUMENT_TOOL_CACHE_TTL_SECONDS <= 0:
        return
    try:
        from packages.core.cache import cache

        await cache.set(
            await runtime_document_cache_key(action, entity_id, params),
            value,
            ttl=RUNTIME_DOCUMENT_TOOL_CACHE_TTL_SECONDS,
        )
    except Exception:
        return


def _runtime_bounded_document_limit(value: Any, default: int = 20, maximum: int = 50) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _runtime_bounded_document_offset(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, min(parsed, 10_000))


async def runtime_search_documents_action(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    from packages.core.database import async_session
    from packages.core.services.document_access import list_visible_documents

    raw_params = dict(params or {})
    workspace_scope = str(raw_params.get("workspace_id") or workspace_id or "").strip() or None
    query = raw_params.get("query", "")
    limit = _runtime_bounded_document_limit(
        raw_params.get("limit"),
        default=RUNTIME_DOCUMENT_LIST_DEFAULT_LIMIT,
        maximum=RUNTIME_DOCUMENT_LIST_MAX_LIMIT,
    )
    detail = "details" if raw_params.get("detail") == "details" else "summary"
    cache_params = {
        "query": query,
        "limit": limit,
        "detail": detail,
        "user_id": user_id,
        "workspace_id": workspace_scope,
    }
    cached = await _runtime_get_cached_document_action("search_documents", entity_id, cache_params)
    if cached is not None:
        return cached

    async with async_session() as db:
        docs, total = await list_visible_documents(
            db,
            entity_id,
            user_id=user_id,
            workspace_id=workspace_scope,
            actor_type="agent",
            name_search=query,
            limit=limit,
        )

    result = json.dumps({
        "total": total,
        "count": len(docs),
        "has_more": total > len(docs),
        "documents": [runtime_document_to_dict(doc, detail=detail) for doc in docs],
    })
    await _runtime_set_cached_document_action("search_documents", entity_id, cache_params, result)
    return result


async def runtime_list_documents_action(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    from packages.core.database import async_session
    from packages.core.services.document_access import list_visible_documents

    raw_params = dict(params or {})
    workspace_scope = str(raw_params.get("workspace_id") or workspace_id or "").strip() or None
    limit = _runtime_bounded_document_limit(
        raw_params.get("limit"),
        default=RUNTIME_DOCUMENT_LIST_DEFAULT_LIMIT,
        maximum=RUNTIME_DOCUMENT_LIST_MAX_LIMIT,
    )
    offset = _runtime_bounded_document_offset(raw_params.get("offset"))
    detail = "details" if raw_params.get("detail") == "details" else "summary"
    cache_params = {
        "limit": limit,
        "offset": offset,
        "detail": detail,
        "user_id": user_id,
        "workspace_id": workspace_scope,
    }
    cached = await _runtime_get_cached_document_action("list_documents", entity_id, cache_params)
    if cached is not None:
        return cached

    async with async_session() as db:
        docs, total = await list_visible_documents(
            db,
            entity_id,
            user_id=user_id,
            workspace_id=workspace_scope,
            actor_type="agent",
            limit=limit,
            offset=offset,
        )

    result = json.dumps({
        "total": total,
        "count": len(docs),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(docs) if offset + len(docs) < total else None,
        "has_more": offset + len(docs) < total,
        "documents": [runtime_document_to_dict(doc, detail=detail) for doc in docs],
    })
    await _runtime_set_cached_document_action("list_documents", entity_id, cache_params, result)
    return result
