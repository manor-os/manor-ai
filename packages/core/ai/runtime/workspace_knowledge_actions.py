"""Runtime-owned workspace Knowledge Net action facade."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import delete as sa_delete, func, select

logger = logging.getLogger(__name__)

_WORKSPACE_GROUP_DEFAULT_KIND = "workspace_collection"
_WORKSPACE_GROUP_FOLDER_KIND = "knowledge_net"
_WORKSPACE_DEFAULT_COLLECTION_NAME = "Workspace Knowledge"


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _as_clean_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


async def _load_workspace(db: Any, *, entity_id: str, workspace_id: str) -> Any | None:
    from packages.core.models.workspace import Workspace

    return (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()


def _workspace_group_settings(group: Any) -> dict[str, Any]:
    return dict(getattr(group, "settings", None) or {})


def _workspace_group_kind(group: Any) -> str:
    settings = _workspace_group_settings(group)
    if settings.get("workspace_file_bucket"):
        return "workspace_files"
    if settings.get("default_collection"):
        return _WORKSPACE_GROUP_DEFAULT_KIND
    kind = str(settings.get("kind") or _WORKSPACE_GROUP_FOLDER_KIND)
    return _WORKSPACE_GROUP_FOLDER_KIND if kind == "knowledge_folder" else kind


def _is_workspace_default_collection(group: Any) -> bool:
    settings = _workspace_group_settings(group)
    return bool(settings.get("default_collection")) or _workspace_group_kind(group) == _WORKSPACE_GROUP_DEFAULT_KIND


async def _ensure_workspace_default_knowledge_group(db: Any, *, entity_id: str, workspace_id: str) -> Any:
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import DocumentGroup

    groups = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        )
    )).scalars().all()
    for group in groups:
        if (group.settings or {}).get("workspace_file_bucket"):
            continue
        if _is_workspace_default_collection(group):
            return group

    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=_WORKSPACE_DEFAULT_COLLECTION_NAME,
        settings={
            "kind": _WORKSPACE_GROUP_DEFAULT_KIND,
            "default_collection": True,
            "purpose": "General workspace knowledge available to agents.",
            "user_manageable": True,
        },
    )
    db.add(group)
    await db.flush()
    return group


async def _resolve_workspace_knowledge_group(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str,
    group_id: str | None = None,
    group_name: str | None = None,
    create_if_missing: bool = False,
    purpose: str | None = None,
) -> tuple[Any | None, bool]:
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import DocumentGroup

    clean_id = str(group_id or "").strip()
    if clean_id:
        group = (await db.execute(
            select(DocumentGroup).where(
                DocumentGroup.id == clean_id,
                DocumentGroup.entity_id == entity_id,
                DocumentGroup.workspace_id == workspace_id,
            )
        )).scalar_one_or_none()
        if group and not (group.settings or {}).get("workspace_file_bucket"):
            return group, False
        return None, False

    clean_name = str(group_name or "").strip()
    if not clean_name:
        group = await _ensure_workspace_default_knowledge_group(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        return group, False

    group = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
            func.lower(DocumentGroup.name) == clean_name.lower(),
        )
    )).scalar_one_or_none()
    if group and not (group.settings or {}).get("workspace_file_bucket"):
        return group, False
    if not create_if_missing:
        return None, False

    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=clean_name,
        settings={
            "kind": _WORKSPACE_GROUP_FOLDER_KIND,
            "purpose": str(purpose or "").strip(),
            "user_manageable": True,
        },
    )
    db.add(group)
    await db.flush()
    return group, True


async def _resolve_visible_documents(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    document_ids: Any = None,
    document_names: Any = None,
) -> tuple[list[Any], dict[str, Any]]:
    from packages.core.services.document_access import get_visible_document, list_visible_documents

    resolved: list[Any] = []
    seen_ids: set[str] = set()
    missing: list[str] = []
    ambiguous: list[dict[str, Any]] = []

    for doc_id in _as_clean_list(document_ids):
        doc = await get_visible_document(
            db,
            doc_id,
            entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            actor_type="agent",
        )
        if not doc:
            missing.append(doc_id)
            continue
        if doc.id not in seen_ids:
            seen_ids.add(doc.id)
            resolved.append(doc)

    for name in _as_clean_list(document_names):
        candidates, _ = await list_visible_documents(
            db,
            entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            actor_type="agent",
            name_search=name,
            include_generated_assets=True,
            limit=20,
        )
        exact = [
            doc for doc in candidates
            if str(doc.name or "").strip().lower() == name.strip().lower()
        ]
        candidates = exact or candidates
        if not candidates:
            missing.append(name)
            continue
        if len(candidates) > 1:
            ambiguous.append({
                "query": name,
                "candidates": [
                    {
                        "id": doc.id,
                        "name": doc.name,
                        "file_type": doc.file_type,
                        "source": doc.source,
                    }
                    for doc in candidates[:10]
                ],
            })
            continue
        doc = candidates[0]
        if doc.id not in seen_ids:
            seen_ids.add(doc.id)
            resolved.append(doc)

    return resolved, {"missing": missing, "ambiguous": ambiguous}


def _workspace_knowledge_policy(workspace: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    operating_model = dict(getattr(workspace, "operating_model", None) or {})
    knowledge = dict(operating_model.get("knowledge") or {})
    knowledge.setdefault("retrieval_mode", "auto")
    knowledge.setdefault("auto_search", True)
    knowledge.setdefault("citation_required", True)
    knowledge.setdefault("strict_mode", False)
    knowledge.setdefault("default_group_ids", [])
    knowledge.setdefault("group_purposes", {})
    return operating_model, knowledge


def _set_workspace_knowledge_policy(workspace: Any, knowledge: dict[str, Any]) -> dict[str, Any]:
    operating_model = dict(getattr(workspace, "operating_model", None) or {})
    operating_model["knowledge"] = knowledge
    workspace.operating_model = operating_model
    return operating_model


def _set_group_default(knowledge: dict[str, Any], group_id: str, use_by_default: Any) -> None:
    if use_by_default is None:
        return
    default_ids = _as_clean_list(knowledge.get("default_group_ids"))
    if bool(use_by_default):
        if group_id not in default_ids:
            default_ids.append(group_id)
    else:
        default_ids = [gid for gid in default_ids if gid != group_id]
    knowledge["default_group_ids"] = default_ids


def _set_group_purpose(knowledge: dict[str, Any], group_id: str, purpose: str | None) -> None:
    if purpose is None:
        return
    purposes = dict(knowledge.get("group_purposes") or {})
    purposes[group_id] = str(purpose or "").strip()
    knowledge["group_purposes"] = purposes


async def _workspace_group_to_dict(
    db: Any,
    group: Any,
    *,
    include_documents: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    from packages.core.models.document import Document, DocumentGroupMember

    docs: list[dict[str, Any]] = []
    count = (await db.execute(
        select(func.count(DocumentGroupMember.document_id))
        .join(Document, Document.id == DocumentGroupMember.document_id)
        .where(
            DocumentGroupMember.group_id == group.id,
            Document.entity_id == group.entity_id,
            Document.is_trashed == False,  # noqa: E712
        )
    )).scalar_one()
    if include_documents:
        rows = (await db.execute(
            select(
                Document.id,
                Document.name,
                Document.file_type,
                Document.file_size,
                Document.vector_status,
                Document.source,
            )
            .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
            .where(
                DocumentGroupMember.group_id == group.id,
                Document.entity_id == group.entity_id,
                Document.is_trashed == False,  # noqa: E712
            )
            .order_by(Document.created_at.desc())
            .limit(max(1, min(int(limit or 20), 100)))
        )).all()
        docs = [
            {
                "id": row.id,
                "name": row.name,
                "file_type": row.file_type,
                "file_size": row.file_size,
                "vector_status": row.vector_status,
                "source": row.source,
            }
            for row in rows
        ]

    return {
        "id": group.id,
        "workspace_id": group.workspace_id,
        "name": group.name,
        "kind": _workspace_group_kind(group),
        "network_type": "workspace" if group.workspace_id else "global",
        "scope": "workspace" if group.workspace_id else "global",
        "is_knowledge_net": not bool((_workspace_group_settings(group)).get("workspace_file_bucket")),
        "purpose": str((_workspace_group_settings(group)).get("purpose") or ""),
        "is_default_collection": _is_workspace_default_collection(group),
        "document_count": int(count or 0),
        "documents": docs,
    }


async def runtime_workspace_list_knowledge_action(
    *,
    entity_id: str,
    workspace_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.database import async_session
        from packages.core.models.document import DocumentGroup

        raw_params = dict(params or {})
        include_documents = raw_params.get("include_documents")
        include_documents = True if include_documents is None else bool(include_documents)
        try:
            limit = max(1, min(int(raw_params.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20

        async with async_session() as db:
            workspace = await _load_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
            if not workspace:
                return _dumps({"error": "workspace not found"})
            groups = list((await db.execute(
                select(DocumentGroup).where(
                    DocumentGroup.entity_id == entity_id,
                    DocumentGroup.workspace_id == workspace_id,
                ).order_by(DocumentGroup.created_at.asc())
            )).scalars().all())
            groups = [
                group for group in groups
                if not (group.settings or {}).get("workspace_file_bucket")
            ]
            operating_model, knowledge = _workspace_knowledge_policy(workspace)
            default_ids = set(_as_clean_list(knowledge.get("default_group_ids")))
            payload = []
            for group in groups:
                data = await _workspace_group_to_dict(
                    db,
                    group,
                    include_documents=include_documents,
                    limit=limit,
                )
                data["use_by_default"] = group.id in default_ids
                payload.append(data)

        return _dumps({
            "workspace_id": workspace_id,
            "knowledge_policy": operating_model.get("knowledge") or {},
            "groups": payload,
            "message": (
                "Use workspace_add_knowledge_documents to attach existing documents. "
                "Use workspace_create_knowledge_folder to create optional Knowledge Nets."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_list_knowledge failed")
        return _dumps({"error": f"failed to list workspace knowledge: {exc}"})


async def runtime_workspace_create_knowledge_folder_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    raw_params = dict(params or {})
    name = str(raw_params.get("name") or "").strip()
    if not name:
        return _dumps({"error": "name is required"})
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.database import async_session
        from packages.core.services.tool_cache_version import bump_tool_cache_version
        from packages.core.services.workspace_service import record_activity
        from packages.core.workspace_chat import context as chat_context

        async with async_session() as db:
            workspace = await _load_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
            if not workspace:
                return _dumps({"error": "workspace not found"})
            group, created = await _resolve_workspace_knowledge_group(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                group_name=name,
                create_if_missing=True,
                purpose=raw_params.get("purpose"),
            )
            if not group:
                return _dumps({"error": "failed to create workspace Knowledge Net"})

            purpose = str(raw_params.get("purpose") or "").strip()
            if purpose:
                settings = _workspace_group_settings(group)
                settings["purpose"] = purpose
                settings.setdefault("kind", _WORKSPACE_GROUP_FOLDER_KIND)
                settings["user_manageable"] = True
                group.settings = settings
            _operating_model, knowledge = _workspace_knowledge_policy(workspace)
            _set_group_default(knowledge, group.id, raw_params.get("use_by_default"))
            _set_group_purpose(knowledge, group.id, purpose if purpose else None)
            _set_workspace_knowledge_policy(workspace, knowledge)
            await record_activity(
                db,
                workspace_id,
                entity_id,
                event_type="workspace_agent.knowledge_net_created" if created else "workspace_agent.knowledge_net_reused",
                summary=f"Workspace Agent {'created' if created else 'reused'} Knowledge Net: {group.name}",
                details={"group_id": group.id, "name": group.name, "purpose": purpose, "created": created},
                user_id=user_id or None,
            )
            await db.commit()
            await bump_tool_cache_version(entity_id, "documents")
            chat_context.invalidate(workspace_id)
            async with async_session() as fresh_db:
                fresh_group = await _resolve_workspace_knowledge_group(
                    fresh_db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    group_id=group.id,
                )
                group_obj = fresh_group[0] if isinstance(fresh_group, tuple) else None
                group_payload = (
                    await _workspace_group_to_dict(fresh_db, group_obj, include_documents=True)
                    if group_obj else {"id": group.id}
                )

        return _dumps({
            "created": created,
            "group": group_payload,
            "workspace_id": workspace_id,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_create_knowledge_folder failed")
        return _dumps({"error": f"failed to create workspace Knowledge Net: {exc}"})


async def runtime_workspace_add_knowledge_documents_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.database import async_session
        from packages.core.services.document_service import add_document_to_group
        from packages.core.services.tool_cache_version import bump_tool_cache_version
        from packages.core.services.workspace_service import record_activity
        from packages.core.workspace_chat import context as chat_context

        raw_params = dict(params or {})
        async with async_session() as db:
            workspace = await _load_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
            if not workspace:
                return _dumps({"error": "workspace not found"})

            group, group_created = await _resolve_workspace_knowledge_group(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                group_id=raw_params.get("group_id"),
                group_name=raw_params.get("group_name"),
                create_if_missing=bool(raw_params.get("create_group_if_missing")),
            )
            if not group:
                return _dumps({
                    "error": "workspace knowledge group not found",
                    "message": "Provide a valid group_id/group_name or set create_group_if_missing=true.",
                })

            docs, resolution = await _resolve_visible_documents(
                db,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                document_ids=raw_params.get("document_ids"),
                document_names=raw_params.get("document_names") or raw_params.get("names"),
            )
            if not docs:
                return _dumps({
                    "error": "no_documents_resolved",
                    **resolution,
                    "message": "Call workspace_list_knowledge or search documents, then retry with exact ids.",
                })

            added_docs: list[dict[str, Any]] = []
            skipped_docs: list[dict[str, Any]] = []
            for doc in docs:
                added = await add_document_to_group(db, doc.id, group.id, entity_id=entity_id)
                target = {"id": doc.id, "name": doc.name, "file_type": doc.file_type}
                if added:
                    added_docs.append(target)
                else:
                    skipped_docs.append(target)

            _operating_model, knowledge = _workspace_knowledge_policy(workspace)
            _set_group_default(knowledge, group.id, raw_params.get("use_by_default"))
            _set_workspace_knowledge_policy(workspace, knowledge)
            await record_activity(
                db,
                workspace_id,
                entity_id,
                event_type="workspace_agent.knowledge_documents_added",
                summary=f"Workspace Agent attached {len(added_docs)} document(s) to {group.name}",
                details={
                    "group_id": group.id,
                    "group_created": group_created,
                    "added": added_docs,
                    "skipped": skipped_docs,
                    **resolution,
                },
                user_id=user_id or None,
            )
            await db.commit()
            await bump_tool_cache_version(entity_id, "documents")
            chat_context.invalidate(workspace_id)
            group_payload = await _workspace_group_to_dict(db, group, include_documents=True)

        return _dumps({
            "updated": True,
            "workspace_id": workspace_id,
            "group": group_payload,
            "group_created": group_created,
            "added": added_docs,
            "skipped": skipped_docs,
            **resolution,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_add_knowledge_documents failed")
        return _dumps({"error": f"failed to add workspace knowledge documents: {exc}"})


async def runtime_workspace_remove_knowledge_document_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.database import async_session
        from packages.core.models.document import DocumentGroup, DocumentGroupMember
        from packages.core.services.tool_cache_version import bump_tool_cache_version
        from packages.core.services.workspace_service import record_activity
        from packages.core.workspace_chat import context as chat_context

        raw_params = dict(params or {})
        async with async_session() as db:
            workspace = await _load_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
            if not workspace:
                return _dumps({"error": "workspace not found"})
            docs, resolution = await _resolve_visible_documents(
                db,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                document_ids=[raw_params.get("document_id")] if raw_params.get("document_id") else [],
                document_names=[raw_params.get("document_name")] if raw_params.get("document_name") else [],
            )
            if len(docs) != 1:
                return _dumps({
                    "error": "document_not_resolved",
                    **resolution,
                    "message": "Provide one exact document_id or unambiguous document_name.",
                })
            doc = docs[0]

            groups: list[Any] = []
            if raw_params.get("group_id") or raw_params.get("group_name"):
                group, _created = await _resolve_workspace_knowledge_group(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    group_id=raw_params.get("group_id"),
                    group_name=raw_params.get("group_name"),
                    create_if_missing=False,
                )
                if not group:
                    return _dumps({"error": "workspace knowledge group not found"})
                groups = [group]
            else:
                groups = list((await db.execute(
                    select(DocumentGroup).where(
                        DocumentGroup.entity_id == entity_id,
                        DocumentGroup.workspace_id == workspace_id,
                    )
                )).scalars().all())
                groups = [
                    group for group in groups
                    if not (group.settings or {}).get("workspace_file_bucket")
                ]

            group_ids = [group.id for group in groups]
            if not group_ids:
                return _dumps({"error": "no workspace knowledge groups found"})

            result = await db.execute(
                sa_delete(DocumentGroupMember).where(
                    DocumentGroupMember.document_id == doc.id,
                    DocumentGroupMember.group_id.in_(group_ids),
                )
            )
            removed = int(result.rowcount or 0)
            await record_activity(
                db,
                workspace_id,
                entity_id,
                event_type="workspace_agent.knowledge_document_removed",
                summary=f"Workspace Agent detached {doc.name} from workspace knowledge",
                details={"document_id": doc.id, "document_name": doc.name, "group_ids": group_ids, "removed": removed},
                user_id=user_id or None,
            )
            await db.commit()
            if removed:
                await bump_tool_cache_version(entity_id, "documents")
                chat_context.invalidate(workspace_id)

        return _dumps({
            "updated": True,
            "removed": removed,
            "document": {"id": doc.id, "name": doc.name},
            "group_ids": group_ids,
            "message": "Document was detached from workspace knowledge; the Knowledge document itself was not deleted.",
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_remove_knowledge_document failed")
        return _dumps({"error": f"failed to remove workspace knowledge document: {exc}"})


async def runtime_workspace_update_knowledge_policy_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.database import async_session
        from packages.core.services.tool_cache_version import bump_tool_cache_version
        from packages.core.services.workspace_service import record_activity
        from packages.core.workspace_chat import context as chat_context

        raw_params = dict(params or {})
        async with async_session() as db:
            workspace = await _load_workspace(db, entity_id=entity_id, workspace_id=workspace_id)
            if not workspace:
                return _dumps({"error": "workspace not found"})

            _operating_model, knowledge = _workspace_knowledge_policy(workspace)
            changed: dict[str, Any] = {}
            for key in ("auto_search", "citation_required", "strict_mode"):
                value = _optional_bool(raw_params.get(key))
                if value is not None:
                    knowledge[key] = value
                    changed[key] = value
            retrieval_mode = str(raw_params.get("retrieval_mode") or "").strip().lower()
            if retrieval_mode:
                if retrieval_mode not in {"auto", "manual", "strict"}:
                    return _dumps({"error": "retrieval_mode must be one of: auto, manual, strict"})
                knowledge["retrieval_mode"] = retrieval_mode
                changed["retrieval_mode"] = retrieval_mode

            group_payload = None
            group = None
            if raw_params.get("group_id") or raw_params.get("group_name"):
                group, _created = await _resolve_workspace_knowledge_group(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    group_id=raw_params.get("group_id"),
                    group_name=raw_params.get("group_name"),
                    create_if_missing=False,
                )
                if not group:
                    return _dumps({"error": "workspace knowledge group not found"})
                settings = _workspace_group_settings(group)
                new_name = str(raw_params.get("name") or "").strip()
                if new_name:
                    group.name = new_name
                    changed["group_name"] = new_name
                if raw_params.get("purpose") is not None:
                    purpose = str(raw_params.get("purpose") or "").strip()
                    settings["purpose"] = purpose
                    group.settings = settings
                    _set_group_purpose(knowledge, group.id, purpose)
                    changed["group_purpose"] = purpose
                if raw_params.get("use_by_default") is not None:
                    _set_group_default(knowledge, group.id, raw_params.get("use_by_default"))
                    changed["use_by_default"] = bool(raw_params.get("use_by_default"))

            _set_workspace_knowledge_policy(workspace, knowledge)
            await record_activity(
                db,
                workspace_id,
                entity_id,
                event_type="workspace_agent.knowledge_policy_updated",
                summary="Workspace Agent updated knowledge policy",
                details={"changed": changed, "group_id": getattr(group, "id", None)},
                user_id=user_id or None,
            )
            await db.commit()
            await bump_tool_cache_version(entity_id, "documents")
            chat_context.invalidate(workspace_id)
            if group:
                await db.refresh(group)
                group_payload = await _workspace_group_to_dict(db, group, include_documents=True)

        return _dumps({
            "updated": True,
            "workspace_id": workspace_id,
            "knowledge_policy": knowledge,
            "group": group_payload,
            "changed": changed,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_update_knowledge_policy failed")
        return _dumps({"error": f"failed to update workspace knowledge policy: {exc}"})
