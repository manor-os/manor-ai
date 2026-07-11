"""Runtime-owned facade for document RAG actions."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def runtime_rag_action(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    client_visible_only: bool = False,
    public_agent_visible_only: bool = False,
    params: dict[str, Any] | None = None,
) -> str:
    """Semantic search across entity, workspace, or Knowledge Net documents."""

    raw_params = dict(params or {})
    question = (raw_params.get("question") or "").strip()
    if not question:
        return json.dumps({"error": "question is required"})

    limit = min(max(int(raw_params.get("limit") or 5), 1), 20)
    workspace_scope = str(raw_params.get("workspace_id") or workspace_id or "").strip() or None
    net_ids = _runtime_rag_id_list(raw_params.get("net_ids") or raw_params.get("group_ids"))

    from packages.core.database import async_session
    from packages.core.services.document_access import (
        document_is_client_visible,
        document_is_public_agent_visible,
        list_visible_documents,
    )
    from packages.core.services.embedding_service import hybrid_search

    async with async_session() as db:
        results: list[dict[str, Any]] = []

        try:
            results = await hybrid_search(
                db,
                entity_id,
                question,
                limit=limit,
                workspace_id=workspace_scope,
                group_ids=net_ids or None,
            )
        except TypeError:
            try:
                results = await hybrid_search(db, entity_id, question, limit=limit)
                if net_ids:
                    results = await _runtime_rag_filter_to_groups(db, results, entity_id, net_ids)
                elif workspace_scope:
                    results = await _runtime_rag_filter_to_workspace(db, results, workspace_scope)
            except Exception:
                logger.warning("hybrid_search failed", exc_info=True)
        except Exception:
            logger.warning("hybrid_search failed", exc_info=True)

        if results and client_visible_only:
            results = await _runtime_rag_filter_to_client_visible_documents(
                db,
                results,
                entity_id=entity_id,
                workspace_id=workspace_scope,
                public_agent_visible_only=public_agent_visible_only,
            )
        elif results:
            results = await _runtime_rag_filter_to_visible_documents(
                db,
                results,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_scope,
            )

        if not results:
            docs, _ = await list_visible_documents(
                db,
                entity_id,
                user_id=user_id,
                workspace_id=workspace_scope,
                actor_type="agent",
                name_search=question,
                limit=limit * 4 if (workspace_scope or net_ids) else limit,
            )
            fallback_results: list[dict[str, Any]] = []
            for doc in docs:
                if client_visible_only:
                    visible = (
                        await document_is_public_agent_visible(
                            db,
                            doc,
                            entity_id=entity_id,
                            workspace_id=workspace_scope,
                        )
                        if public_agent_visible_only
                        else await document_is_client_visible(
                            db,
                            doc,
                            entity_id=entity_id,
                            workspace_id=workspace_scope,
                        )
                    )
                    if not visible:
                        continue
                fallback_results.append({
                    "document_id": doc.id,
                    "name": doc.name,
                    "score": None,
                    "content_preview": doc.name,
                })
            if net_ids:
                fallback_results = await _runtime_rag_filter_to_groups(
                    db,
                    fallback_results,
                    entity_id,
                    net_ids,
                )
            elif workspace_scope:
                fallback_results = await _runtime_rag_filter_to_workspace(
                    db,
                    fallback_results,
                    workspace_scope,
                )
            results.extend(fallback_results[:limit])

    scope = "knowledge_net" if net_ids else "workspace" if workspace_scope else "entity"
    if not results:
        return json.dumps({
            "context": "",
            "sources": [],
            "source_count": 0,
            "scope": scope,
            "net_ids": net_ids,
            "message": f"No indexed content found for: {question!r}.",
        })

    context_parts = []
    sources = []
    for index, result in enumerate(results, 1):
        score_str = f" (relevance: {result['score']})" if result.get("score") else ""
        context_parts.append(
            f"[Document {index}: {result['name']}{score_str}]\n"
            f"{result.get('content_preview', '')}"
        )
        sources.append({"document_id": result["document_id"], "name": result["name"]})

    return json.dumps({
        "context": "\n\n---\n\n".join(context_parts),
        "sources": sources,
        "source_count": len(results),
        "scope": scope,
        "net_ids": net_ids,
    })


def _runtime_rag_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


async def _runtime_rag_filter_to_workspace(
    db: Any,
    results: list[dict[str, Any]],
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Client-side fallback filter for older hybrid_search signatures."""

    if not results:
        return []
    from sqlalchemy import select

    from packages.core.models.document import DocumentGroup, DocumentGroupMember

    doc_ids = [result["document_id"] for result in results if result.get("document_id")]
    if not doc_ids:
        return []
    groups = (await db.execute(
        select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id)
    )).scalars().all()
    group_ids = [
        group.id for group in groups
        if not (group.settings or {}).get("workspace_file_bucket")
    ]
    if not group_ids:
        return []
    rows = (await db.execute(
        select(DocumentGroupMember.document_id).where(
            DocumentGroupMember.group_id.in_(group_ids),
            DocumentGroupMember.document_id.in_(doc_ids),
        )
    )).scalars().all()
    allowed = set(rows)
    return [result for result in results if result["document_id"] in allowed]


async def _runtime_rag_filter_to_visible_documents(
    db: Any,
    results: list[dict[str, Any]],
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
) -> list[dict[str, Any]]:
    if not user_id or not results:
        return results

    from packages.core.services.document_access import user_can_read_document
    from packages.core.services.document_service import get_document

    visible: list[dict[str, Any]] = []
    for result in results:
        document_id = result.get("document_id")
        if not document_id:
            continue
        document = await get_document(db, document_id, entity_id)
        if await user_can_read_document(
            db,
            document,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            actor_type="agent",
        ):
            visible.append(result)
    return visible


async def _runtime_rag_filter_to_client_visible_documents(
    db: Any,
    results: list[dict[str, Any]],
    *,
    entity_id: str,
    workspace_id: str | None,
    public_agent_visible_only: bool = False,
) -> list[dict[str, Any]]:
    if not results:
        return []

    from packages.core.services.document_access import (
        document_is_client_visible,
        document_is_public_agent_visible,
    )
    from packages.core.services.document_service import get_document

    visible: list[dict[str, Any]] = []
    for result in results:
        document_id = result.get("document_id")
        if not document_id:
            continue
        document = await get_document(db, document_id, entity_id)
        allowed = (
            await document_is_public_agent_visible(
                db,
                document,
                entity_id=entity_id,
                workspace_id=workspace_id,
            )
            if public_agent_visible_only
            else await document_is_client_visible(
                db,
                document,
                entity_id=entity_id,
                workspace_id=workspace_id,
            )
        )
        if allowed:
            visible.append(result)
    return visible


async def _runtime_rag_filter_to_groups(
    db: Any,
    results: list[dict[str, Any]],
    entity_id: str,
    group_ids: list[str],
) -> list[dict[str, Any]]:
    """Client-side fallback filter for Knowledge Net scoped RAG."""

    if not results or not group_ids:
        return []
    from sqlalchemy import select

    from packages.core.models.document import DocumentGroup, DocumentGroupMember

    doc_ids = [result["document_id"] for result in results if result.get("document_id")]
    if not doc_ids:
        return []
    groups = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.id.in_(group_ids),
        )
    )).scalars().all()
    allowed_group_ids = [
        group.id for group in groups
        if not (group.settings or {}).get("workspace_file_bucket")
    ]
    if not allowed_group_ids:
        return []
    rows = (await db.execute(
        select(DocumentGroupMember.document_id).where(
            DocumentGroupMember.group_id.in_(allowed_group_ids),
            DocumentGroupMember.document_id.in_(doc_ids),
        )
    )).scalars().all()
    allowed = set(rows)
    return [result for result in results if result["document_id"] in allowed]
