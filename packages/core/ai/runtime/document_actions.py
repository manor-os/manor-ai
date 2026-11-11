"""Runtime-owned facades for Knowledge document tool actions."""

from __future__ import annotations

import glob
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from packages.core.services.generated_file_refs import canonical_file_markdown_link


RUNTIME_DOCUMENT_TOOL_CACHE_TTL_SECONDS = int(os.getenv("DOCUMENT_TOOL_CACHE_TTL_SECONDS", "900"))
RUNTIME_DOCUMENT_LIST_DEFAULT_LIMIT = 20
RUNTIME_DOCUMENT_LIST_MAX_LIMIT = 50
RUNTIME_DOCUMENT_CACHE_NAMESPACE = "documents"
RUNTIME_DOCUMENT_SCOPE_ALL_KNOWLEDGE = "all_knowledge"


def runtime_document_viewer_url(document_id: Any) -> str | None:
    """Return the canonical in-app route for a Knowledge document."""

    value = str(document_id or "").strip()
    if not value:
        return None
    return f"/viewer/{quote(value, safe='')}"


def runtime_document_markdown_link(name: Any, document_id: Any) -> str | None:
    """Return the exact Markdown form the assistant should copy to its answer."""

    viewer_url = runtime_document_viewer_url(document_id)
    if not viewer_url:
        return None
    return canonical_file_markdown_link(name or "Knowledge file", viewer_url)


def runtime_document_to_dict(doc: Any, *, detail: str = "summary") -> dict[str, Any]:
    viewer_url = runtime_document_viewer_url(getattr(doc, "id", None))
    data = {
        "document_id": doc.id,
        "name": doc.name,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
        # FileRef contract: a Document's one outward address.
        "open_url": viewer_url,
        "viewer_url": viewer_url,
        "markdown_link": runtime_document_markdown_link(
            getattr(doc, "name", None),
            getattr(doc, "id", None),
        ),
    }
    if detail == "details":
        created_at = getattr(doc, "created_at", None)
        data.update({
            "mime_type": getattr(doc, "mime_type", None),
            "source": getattr(doc, "source", None),
            "vector_status": getattr(doc, "vector_status", None),
            "created_at": created_at.isoformat() if created_at else None,
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


def _runtime_document_search_text(raw_params: dict[str, Any]) -> str:
    for key in ("query", "search", "name_search"):
        value = raw_params.get(key)
        if value:
            return str(value)
    return ""


def _runtime_document_folder_id(raw_params: dict[str, Any]) -> str | None:
    for key in ("folder_id", "folder"):
        value = raw_params.get(key)
        if value:
            return str(value)
    return None


def _runtime_document_want_details(raw_params: dict[str, Any]) -> bool:
    return str(raw_params.get("detail") or raw_params.get("details") or "summary").lower() in {
        "details", "detail", "full", "true", "1",
    }


def _runtime_document_string_list(value: Any, *, maximum: int = 50) -> list[str]:
    if value in (None, ""):
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("plural document filters must be arrays")
    normalized: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= maximum:
            break
    return normalized


def _runtime_document_optional_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed


def _runtime_document_query_filters(
    raw_params: dict[str, Any],
    *,
    workspace_id: str | None,
) -> dict[str, Any]:
    workspace_ids = _runtime_document_string_list(raw_params.get("workspace_ids"))
    folder_ids = _runtime_document_string_list(raw_params.get("folder_ids"))
    file_types = _runtime_document_string_list(raw_params.get("file_types"))
    sources = _runtime_document_string_list(raw_params.get("sources"))
    vector_statuses = _runtime_document_string_list(
        raw_params.get("vector_statuses")
    )
    file_size_min = _runtime_document_optional_int(
        raw_params.get("file_size_min"),
        "file_size_min",
    )
    file_size_max = _runtime_document_optional_int(
        raw_params.get("file_size_max"),
        "file_size_max",
    )
    if (
        file_size_min is not None
        and file_size_max is not None
        and file_size_min > file_size_max
    ):
        raise ValueError("file_size_min must be less than or equal to file_size_max")
    return {
        "workspace_id": workspace_id,
        # ``None`` means no filter. In particular, ``list_documents`` reserves
        # an explicit empty folder set to mean an intentionally empty scope.
        "workspace_ids": workspace_ids or None,
        "folder_ids": folder_ids or None,
        "file_types": file_types or None,
        "sources": sources or None,
        "vector_statuses": vector_statuses or None,
        "created_after": str(raw_params.get("created_after") or "") or None,
        "created_before": str(raw_params.get("created_before") or "") or None,
        "updated_after": str(raw_params.get("updated_after") or "") or None,
        "updated_before": str(raw_params.get("updated_before") or "") or None,
        "file_size_min": file_size_min,
        "file_size_max": file_size_max,
    }


@dataclass(frozen=True)
class _RuntimeDocumentScope:
    """Where one listing or search looks, given what the caller named."""

    workspace_id: str | None = None
    # The run reads beyond its Workspace: a person asked, or a folder was named.
    execution_workspace_id: str | None = None
    # The run's Workspace applied only because nothing else was named.
    default_workspace_id: str | None = None
    all_knowledge_refused: bool = False
    person_in_chat: bool = False

    def apply(self, filters: dict[str, Any]) -> dict[str, Any]:
        if self.execution_workspace_id:
            return {**filters, "execution_workspace_id": self.execution_workspace_id}
        return filters

    def cache_fields(self) -> dict[str, Any]:
        return {
            "person_in_chat": self.person_in_chat,
            "all_knowledge_refused": self.all_knowledge_refused,
        }


def _runtime_document_scope(
    raw_params: dict[str, Any],
    *,
    workspace_id: str | None,
    person_in_chat: bool,
    folder_id: str | None = None,
) -> _RuntimeDocumentScope:
    named_workspace = str(raw_params.get("workspace_id") or "").strip()
    run_workspace = str(workspace_id or "").strip()
    if not run_workspace or (named_workspace and named_workspace != run_workspace):
        return _RuntimeDocumentScope(workspace_id=named_workspace or None)
    if _runtime_document_string_list(raw_params.get("workspace_ids")):
        return _RuntimeDocumentScope(workspace_id=run_workspace)
    # The top level is not a named location: it is where the unrelated files are.
    folders = {*_runtime_document_string_list(raw_params.get("folder_ids"))}
    if folder_id is not None:
        folders.add(folder_id)
    names_folder = bool(folders) and not folders & {"", "root"}
    asks_all = str(raw_params.get("scope") or "").strip() == RUNTIME_DOCUMENT_SCOPE_ALL_KNOWLEDGE
    if names_folder or (asks_all and person_in_chat):
        return _RuntimeDocumentScope(execution_workspace_id=run_workspace)
    return _RuntimeDocumentScope(
        workspace_id=run_workspace,
        default_workspace_id=run_workspace,
        all_knowledge_refused=asks_all,
        person_in_chat=person_in_chat,
    )


def runtime_glob_names_location(pattern: str) -> bool:
    """A pattern starting with a literal name points somewhere; ``**/*.csv`` searches."""

    first = str(pattern or "").replace("\\", "/").split("/", 1)[0]
    return first not in {"", "."} and not glob.has_magic(first)


def runtime_workspace_search_scope_note(excluded: int = 0, *, person_in_chat: bool = False) -> str:
    """Tell an agent a search stopped at its Workspace, so "nothing found" is not read as "does not exist"."""

    note = (
        f"Search covered this Workspace only; {excluded} file(s) outside it were left out."
        if excluded
        else "Search covered this Workspace only."
    )
    if person_in_chat:
        note += (
            " If the person asks to look outside this Workspace, use search_documents "
            f"with scope='{RUNTIME_DOCUMENT_SCOPE_ALL_KNOWLEDGE}', or name the exact path."
        )
    return note


async def _runtime_documents_left_out(
    session: Any,
    *,
    entity_id: str,
    user_id: str | None,
    scope: _RuntimeDocumentScope,
    filters: dict[str, Any],
    query: str,
    in_scope_total: int,
    folder_id: str | None = None,
) -> int:
    """Readable documents matching this request that the default scope leaves out."""

    if not scope.default_workspace_id or not user_id:
        return 0
    from packages.core.services.document_access import list_visible_documents

    folder_filter = {"folder_id": folder_id} if folder_id is not None else {}
    _page, total = await list_visible_documents(
        session,
        entity_id,
        user_id=user_id,
        **{
            **filters,
            **folder_filter,
            "workspace_id": None,
            "execution_workspace_id": scope.default_workspace_id,
        },
        actor_type="agent",
        name_search=query or None,
        limit=0,
    )
    return max(total - in_scope_total, 0)


def _runtime_document_scope_note(
    scope: _RuntimeDocumentScope,
    *,
    left_out: int,
    found_any: bool,
    offset: int,
) -> dict[str, str]:
    if not scope.default_workspace_id or offset:
        return {}
    if not (left_out or not found_any or scope.all_knowledge_refused):
        return {}
    note = runtime_workspace_search_scope_note(left_out, person_in_chat=scope.person_in_chat)
    if scope.all_knowledge_refused:
        note += (
            f" scope='{RUNTIME_DOCUMENT_SCOPE_ALL_KNOWLEDGE}' applies only when a person "
            "asks for it in chat."
        )
    return {"scope_note": note}


async def runtime_search_documents_action(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: dict[str, Any] | None = None,
    db: Any = None,
    user_wording: str | None = None,
    person_in_chat: bool = False,
) -> str:
    from packages.core.database import async_session
    from packages.core.services.document_access import list_visible_documents

    raw_params = dict(params or {})
    try:
        scope = _runtime_document_scope(raw_params, workspace_id=workspace_id, person_in_chat=person_in_chat)
        filters = scope.apply(_runtime_document_query_filters(
            raw_params,
            workspace_id=scope.workspace_id,
        ))
    except ValueError as exc:
        return json.dumps({
            "error": "invalid_document_filters",
            "message": str(exc),
        })
    query = _runtime_document_search_text(raw_params)
    limit = _runtime_bounded_document_limit(
        raw_params.get("limit"),
        default=RUNTIME_DOCUMENT_LIST_DEFAULT_LIMIT,
        maximum=RUNTIME_DOCUMENT_LIST_MAX_LIMIT,
    )
    offset = _runtime_bounded_document_offset(raw_params.get("offset"))
    detail = "details" if _runtime_document_want_details(raw_params) else "summary"
    cache_params = {
        "query": query,
        "limit": limit,
        "offset": offset,
        "detail": detail,
        "user_id": user_id,
        "user_wording": user_wording or None,
        **filters,
        **scope.cache_fields(),
    }
    cached = await _runtime_get_cached_document_action("search_documents", entity_id, cache_params)
    if cached is not None:
        return cached

    async def search(session: Any) -> tuple[list[Any], int, dict[str, Any] | None, int]:
        docs, total = await list_visible_documents(
            session,
            entity_id,
            user_id=user_id,
            **filters,
            actor_type="agent",
            name_search=query or None,
            limit=limit,
            offset=offset,
        )
        left_out = 0 if offset else await _runtime_documents_left_out(
            session,
            entity_id=entity_id,
            user_id=user_id,
            scope=scope,
            filters=filters,
            query=query,
            in_scope_total=total,
        )
        content = None
        if query and offset == 0:
            # The text, not just the name: indexing is paused, so content is
            # read at query time (knowledge_content_search).
            from packages.core.services.knowledge_content_search import search_knowledge_content

            content = await search_knowledge_content(
                session, entity_id=entity_id, user_id=user_id, query=query,
                limit=limit, filters=filters, user_wording=user_wording,
            )
        return docs, total, content, left_out

    if db is not None:
        docs, total, content, left_out = await search(db)
    else:
        async with async_session() as session:
            docs, total, content, left_out = await search(session)

    payload: dict[str, Any] = {
        "total": total,
        "count": len(docs),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(docs) if offset + len(docs) < total else None,
        "has_more": offset + len(docs) < total,
        "documents": [runtime_document_to_dict(doc, detail=detail) for doc in docs],
    }
    if content is not None:
        payload["content_matches"] = [
            {**runtime_document_to_dict(match["doc"], detail=detail),
             "match_score": match["score"], "snippet": match["snippet"]}
            for match in content["matches"]
        ]
        payload["content_search"] = {"scanned": content["scanned"], "partial": content["partial"]}
    payload.update(_runtime_document_scope_note(
        scope,
        left_out=left_out,
        found_any=bool(docs or (content or {}).get("matches")),
        offset=offset,
    ))
    result = json.dumps(payload)
    await _runtime_set_cached_document_action("search_documents", entity_id, cache_params, result)
    return result


async def runtime_list_documents_action(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: dict[str, Any] | None = None,
    db: Any = None,
    person_in_chat: bool = False,
) -> str:
    from packages.core.database import async_session
    from packages.core.services.document_access import list_visible_documents

    raw_params = dict(params or {})
    folder_id = _runtime_document_folder_id(raw_params)
    try:
        scope = _runtime_document_scope(
            raw_params,
            workspace_id=workspace_id,
            person_in_chat=person_in_chat,
            folder_id=folder_id,
        )
        filters = scope.apply(_runtime_document_query_filters(
            raw_params,
            workspace_id=scope.workspace_id,
        ))
    except ValueError as exc:
        return json.dumps({
            "error": "invalid_document_filters",
            "message": str(exc),
        })
    query = _runtime_document_search_text(raw_params)
    limit = _runtime_bounded_document_limit(
        raw_params.get("limit"),
        default=RUNTIME_DOCUMENT_LIST_DEFAULT_LIMIT,
        maximum=RUNTIME_DOCUMENT_LIST_MAX_LIMIT,
    )
    offset = _runtime_bounded_document_offset(raw_params.get("offset"))
    detail = "details" if _runtime_document_want_details(raw_params) else "summary"
    cache_params = {
        "query": query,
        "folder_id": folder_id,
        "limit": limit,
        "offset": offset,
        "detail": detail,
        "user_id": user_id,
        **filters,
        **scope.cache_fields(),
    }
    cached = await _runtime_get_cached_document_action("list_documents", entity_id, cache_params)
    if cached is not None:
        return cached

    async def list_page(session: Any) -> tuple[list[Any], int, int]:
        docs, total = await list_visible_documents(
            session,
            entity_id,
            user_id=user_id,
            **filters,
            actor_type="agent",
            name_search=query or None,
            folder_id=folder_id,
            limit=limit,
            offset=offset,
        )
        left_out = 0 if offset else await _runtime_documents_left_out(
            session,
            entity_id=entity_id,
            user_id=user_id,
            scope=scope,
            filters=filters,
            query=query,
            in_scope_total=total,
            folder_id=folder_id,
        )
        return docs, total, left_out

    if db is not None:
        docs, total, left_out = await list_page(db)
    else:
        async with async_session() as session:
            docs, total, left_out = await list_page(session)

    result = json.dumps({
        "total": total,
        "count": len(docs),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(docs) if offset + len(docs) < total else None,
        "has_more": offset + len(docs) < total,
        "documents": [runtime_document_to_dict(doc, detail=detail) for doc in docs],
        **_runtime_document_scope_note(
            scope,
            left_out=left_out,
            found_any=bool(docs),
            offset=offset,
        ),
    })
    await _runtime_set_cached_document_action("list_documents", entity_id, cache_params, result)
    return result
