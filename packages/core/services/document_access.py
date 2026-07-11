"""Document visibility helpers used by API and runtime entrypoints."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Document, DocumentFolder, DocumentGroup, DocumentGroupMember, VectorStatus
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
    Visibility,
)
from packages.core.models.staff import Staff
from packages.core.models.user import User, UserMembership
from packages.core.services.workspace_access import (
    is_entity_admin_role,
    user_can_read_workspace_id,
)


_ENTITY_DOCUMENT_READ_ROLES = {"owner", "admin", "member", "viewer"}
_DOCUMENT_READ_CAPABILITIES = {
    Capability.VIEW,
    Capability.VIEW_REDACTED,
    Capability.COMMENT,
    Capability.EDIT,
    Capability.DOWNLOAD,
    Capability.PRINT,
    Capability.MANAGE_METADATA,
    Capability.SHARE_INTERNAL,
    Capability.SHARE_EXTERNAL,
    Capability.GRANT_ACCESS,
}
_FOLDER_READ_CAPABILITIES = {
    Capability.VIEW,
    Capability.VIEW_REDACTED,
    Capability.COMMENT,
    Capability.EDIT,
    Capability.UPLOAD_TO,
    Capability.DOWNLOAD,
    Capability.PRINT,
    Capability.MANAGE_METADATA,
    Capability.SHARE_INTERNAL,
    Capability.SHARE_EXTERNAL,
    Capability.GRANT_ACCESS,
}
_DOCUMENT_OWNER_CAPABILITIES = {
    Capability.VIEW,
    Capability.COMMENT,
    Capability.EDIT,
    Capability.DOWNLOAD,
    Capability.PRINT,
    Capability.MANAGE_METADATA,
    Capability.SHARE_INTERNAL,
    Capability.SHARE_EXTERNAL,
    Capability.RECLASSIFY,
    Capability.DELETE,
    Capability.GRANT_ACCESS,
    Capability.LEGAL_HOLD,
}
_QUARANTINED_STATUSES = {"quarantined", "rejected"}
_INTERNAL_FILTER_LIMIT = 2_000
_READABLE_LOCAL_SKIP_STATUSES = {VectorStatus.PROCESSING, VectorStatus.GENERATING}
_PLACEHOLDER_UNAVAILABLE_STATUSES = {VectorStatus.FAILED, VectorStatus.SKIPPED}


def _with_file_integrity(metadata: dict | None, **fields: object) -> dict:
    updated = dict(metadata or {}) if isinstance(metadata, dict) else {}
    integrity = dict(updated.get("file_integrity") or {})
    integrity.update(fields)
    integrity["checked_at"] = datetime.now(timezone.utc).isoformat()
    if fields.get("status") == "ok":
        integrity.pop("recoverable", None)
        integrity.pop("error", None)
    updated["file_integrity"] = integrity
    return updated


def _document_local_path(document: Document, fs_root: str) -> str | None:
    fs_path = str(getattr(document, "fs_path", "") or "")
    if not fs_path:
        return None
    root = os.path.realpath(os.path.join(fs_root, document.entity_id))
    if os.path.isabs(fs_path):
        full_path = os.path.realpath(fs_path)
    else:
        full_path = os.path.realpath(os.path.join(root, fs_path))

    try:
        if os.path.commonpath([root, full_path]) != root:
            return None
    except ValueError:
        return None
    return full_path


async def _filter_readable_local_documents(
    db: AsyncSession,
    documents: list[Document],
) -> list[Document]:
    """Hide stale local-file rows from Knowledge lists and mark them missing.

    The filesystem is the source of truth for rows with ``fs_path``. Background
    reconcile eventually cleans these up, but the user-facing list should not
    keep surfacing a row whose required local payload is already gone.
    """
    from packages.core.config import get_settings

    settings = get_settings()
    if not getattr(settings, "MANOR_FS_ENABLED", False):
        return documents
    fs_root = getattr(settings, "MANOR_FS_ROOT", "")

    visible: list[Document] = []
    mutated = False
    for document in documents:
        if getattr(document, "file_url", None):
            visible.append(document)
            continue
        if not getattr(document, "fs_path", None):
            meta = getattr(document, "metadata_", None)
            has_inline_content = isinstance(meta, dict) and any(
                isinstance(meta.get(key), str) and meta.get(key)
                for key in ("content", "content_text")
            )
            if (
                getattr(document, "vector_status", None) in _PLACEHOLDER_UNAVAILABLE_STATUSES
                and not has_inline_content
            ):
                document.metadata_ = _with_file_integrity(
                    meta,
                    status="unavailable",
                    source="knowledge_list",
                    recoverable=False,
                )
                document.is_trashed = True
                document.trashed_at = datetime.now(timezone.utc)
                mutated = True
                continue
            visible.append(document)
            continue
        if getattr(document, "vector_status", None) in _READABLE_LOCAL_SKIP_STATUSES:
            visible.append(document)
            continue

        entity_root = os.path.realpath(os.path.join(fs_root, str(getattr(document, "entity_id", ""))))
        if not os.path.isdir(entity_root):
            visible.append(document)
            continue

        full_path = _document_local_path(document, fs_root)
        if full_path and os.path.isfile(full_path):
            visible.append(document)
            continue

        document.metadata_ = _with_file_integrity(
            getattr(document, "metadata_", None),
            status="missing" if full_path else "invalid_path",
            fs_path=str(getattr(document, "fs_path", "") or ""),
            source="knowledge_list",
            path=full_path,
            recoverable=False,
        )
        document.vector_status = VectorStatus.FAILED
        document.is_trashed = True
        document.trashed_at = datetime.now(timezone.utc)
        mutated = True

    if mutated:
        await db.flush()
    return visible


def _expires_after_now(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    now = datetime.now(UTC)
    if expires_at.tzinfo is None:
        return expires_at > now.replace(tzinfo=None)
    return expires_at > now


async def _resolve_user_role(
    db: AsyncSession,
    *,
    user_id: str | None,
    entity_id: str,
    role: str | None,
) -> str | None:
    if role or not user_id:
        return role
    membership_role = (
        await db.execute(
            select(UserMembership.role).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == entity_id,
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if membership_role:
        return membership_role
    user = (
        await db.execute(
            select(User.role).where(
                User.id == user_id,
                User.entity_id == entity_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return user


async def document_workspace_ids(db: AsyncSession, document: Document) -> set[str]:
    workspace_ids: set[str] = set()
    rows = (
        await db.execute(
            select(DocumentGroup.workspace_id)
            .join(DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id)
            .where(
                DocumentGroupMember.document_id == document.id,
                DocumentGroup.entity_id == document.entity_id,
                DocumentGroup.workspace_id.isnot(None),
            )
        )
    ).scalars().all()
    workspace_ids.update(str(row) for row in rows if row)

    meta = document.metadata_ if isinstance(document.metadata_, dict) else {}
    origin = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
    for value in (origin.get("workspace_id"), meta.get("workspace_id")):
        if value:
            workspace_ids.add(str(value))
    return workspace_ids


async def _folder_ancestor_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
) -> list[str]:
    if not folder_id:
        return []
    ids: list[str] = []
    seen: set[str] = set()
    current_id = folder_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        row = (
            await db.execute(
                select(DocumentFolder.id, DocumentFolder.parent_id)
                .where(
                    DocumentFolder.id == current_id,
                    DocumentFolder.entity_id == entity_id,
                )
                .limit(1)
            )
        ).first()
        if not row:
            break
        ids.append(row.id)
        current_id = row.parent_id
    return ids


async def _grant_subject_ids_for_user(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
) -> set[str]:
    """IDs that may appear in user grants for this login.

    New grants should store ``User.id``. Older flows occasionally stored the
    linked ``Staff.id`` while still marking the grant as subject_type=user;
    include both so historical shares keep working.
    """
    if not user_id:
        return set()

    ids = {user_id}
    staff_ids = (
        await db.execute(
            select(Staff.id).where(
                Staff.entity_id == entity_id,
                Staff.user_id == user_id,
                Staff.status == "active",
                Staff.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    ids.update(str(staff_id) for staff_id in staff_ids if staff_id)
    return ids


async def _has_read_grant(
    db: AsyncSession,
    *,
    document: Document,
    user_id: str | None,
) -> bool:
    if not user_id:
        return False
    subject_ids = await _grant_subject_ids_for_user(
        db,
        entity_id=document.entity_id,
        user_id=user_id,
    )
    if not subject_ids:
        return False
    resource_filters = [
        and_(
            ResourceGrant.resource_type == ResourceType.DOCUMENT,
            ResourceGrant.resource_id == document.id,
        )
    ]
    for folder_id in await _folder_ancestor_ids(
        db,
        entity_id=document.entity_id,
        folder_id=document.folder_id,
    ):
        resource_filters.append(
            and_(
                ResourceGrant.resource_type == ResourceType.DOCUMENT_FOLDER,
                ResourceGrant.resource_id == folder_id,
            )
        )

    rows = (
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.entity_id == document.entity_id,
                ResourceGrant.subject_type == SubjectType.USER,
                ResourceGrant.subject_id.in_(subject_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
                or_(*resource_filters),
            )
        )
    ).scalars().all()
    for grant in rows:
        if not _expires_after_now(grant.expires_at):
            continue
        if _DOCUMENT_READ_CAPABILITIES.intersection(set(grant.capabilities or [])):
            return True
    return False


async def _document_grants_for_user(
    db: AsyncSession,
    *,
    document: Document,
    user_id: str | None,
) -> list[ResourceGrant]:
    if not user_id:
        return []
    subject_ids = await _grant_subject_ids_for_user(
        db,
        entity_id=document.entity_id,
        user_id=user_id,
    )
    if not subject_ids:
        return []
    resource_filters = [
        and_(
            ResourceGrant.resource_type == ResourceType.DOCUMENT,
            ResourceGrant.resource_id == document.id,
        )
    ]
    for folder_id in await _folder_ancestor_ids(
        db,
        entity_id=document.entity_id,
        folder_id=document.folder_id,
    ):
        resource_filters.append(
            and_(
                ResourceGrant.resource_type == ResourceType.DOCUMENT_FOLDER,
                ResourceGrant.resource_id == folder_id,
            )
        )
    return list((
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.entity_id == document.entity_id,
                ResourceGrant.subject_type == SubjectType.USER,
                ResourceGrant.subject_id.in_(subject_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
                or_(*resource_filters),
            )
        )
    ).scalars().all())


async def _folder_grants_for_user(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
    user_id: str | None,
) -> list[ResourceGrant]:
    if not folder_id or not user_id:
        return []
    subject_ids = await _grant_subject_ids_for_user(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
    if not subject_ids:
        return []
    resource_filters = [
        and_(
            ResourceGrant.resource_type == ResourceType.DOCUMENT_FOLDER,
            ResourceGrant.resource_id == ancestor_id,
        )
        for ancestor_id in await _folder_ancestor_ids(
            db,
            entity_id=entity_id,
            folder_id=folder_id,
        )
    ]
    if not resource_filters:
        return []
    return list((
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.entity_id == entity_id,
                ResourceGrant.subject_type == SubjectType.USER,
                ResourceGrant.subject_id.in_(subject_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
                or_(*resource_filters),
            )
        )
    ).scalars().all())


async def _document_folder_path_is_readable(
    db: AsyncSession,
    *,
    document: Document,
    user_id: str | None,
    role: str | None,
) -> bool:
    """A document cannot be surfaced through a folder path the user cannot see.

    Folder property cascades normally narrow child document visibility, but
    older rows or ``cascade=false`` updates can leave a document broader than
    its ancestor folder. Enforce the folder visibility ceiling at read time.
    """
    if not document.folder_id:
        return True
    folder_ids = await _folder_ancestor_ids(
        db,
        entity_id=document.entity_id,
        folder_id=document.folder_id,
    )
    if not folder_ids:
        return True
    folders = list((
        await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.entity_id == document.entity_id,
                DocumentFolder.id.in_(folder_ids),
            )
        )
    ).scalars().all())
    folder_by_id = {folder.id: folder for folder in folders}
    for folder_id in folder_ids:
        folder = folder_by_id.get(folder_id)
        if not await user_can_read_folder(
            db,
            folder,
            entity_id=document.entity_id,
            user_id=user_id,
            role=role,
        ):
            return False
    return True


def _active_capabilities_from_grants(rows: list[ResourceGrant]) -> set[str]:
    capabilities: set[str] = set()
    for grant in rows:
        if not _expires_after_now(grant.expires_at):
            continue
        capabilities.update(str(capability) for capability in (grant.capabilities or []))
    return capabilities


async def document_grant_capabilities_for_user(
    db: AsyncSession,
    *,
    document: Document,
    user_id: str | None,
) -> set[str]:
    """Return explicit document capabilities from direct and folder grants."""
    return _active_capabilities_from_grants(
        await _document_grants_for_user(db, document=document, user_id=user_id)
    )


async def folder_grant_capabilities_for_user(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
    user_id: str | None,
) -> set[str]:
    """Return explicit folder capabilities from the folder and its ancestors."""
    return _active_capabilities_from_grants(
        await _folder_grants_for_user(
            db,
            entity_id=entity_id,
            folder_id=folder_id,
            user_id=user_id,
        )
    )


async def user_has_document_capability(
    db: AsyncSession,
    *,
    document: Document | None,
    user_id: str | None,
    capabilities: set[str],
) -> bool:
    if not document or not user_id:
        return False
    granted = await document_grant_capabilities_for_user(
        db,
        document=document,
        user_id=user_id,
    )
    return bool(granted.intersection(capabilities))


async def user_has_folder_capability(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
    user_id: str | None,
    capabilities: set[str],
) -> bool:
    granted = await folder_grant_capabilities_for_user(
        db,
        entity_id=entity_id,
        folder_id=folder_id,
        user_id=user_id,
    )
    return bool(granted.intersection(capabilities))


async def user_can_read_folder(
    db: AsyncSession,
    folder: DocumentFolder | None,
    *,
    entity_id: str,
    user_id: str | None = None,
    role: str | None = None,
) -> bool:
    """Return whether a folder may be listed/opened by this user.

    Folder ACLs cascade down the folder tree via
    ``folder_grant_capabilities_for_user``. ``private`` folder visibility is
    therefore enforced here rather than left to the UI list filter.
    """
    if not folder or folder.entity_id != entity_id:
        return False

    # Preserve legacy/background callers that do not carry a user context.
    if not user_id:
        return True

    resolved_role = await _resolve_user_role(
        db,
        user_id=user_id,
        entity_id=entity_id,
        role=role,
    )
    if is_entity_admin_role(resolved_role):
        return True
    if getattr(folder, "owner_id", None) == user_id:
        return True

    granted = await folder_grant_capabilities_for_user(
        db,
        entity_id=entity_id,
        folder_id=getattr(folder, "id", None),
        user_id=user_id,
    )
    if granted.intersection(_FOLDER_READ_CAPABILITIES):
        return True

    visibility = getattr(folder, "visibility", None) or Visibility.ENTITY
    if visibility == Visibility.PRIVATE:
        return False

    # DocumentFolder does not currently store a workspace_id, so workspace
    # folder visibility cannot be resolved to a specific workspace membership
    # here. Keep the existing non-private folder behavior and rely on explicit
    # grants for stricter folder scopes.
    if visibility in (Visibility.WORKSPACE, Visibility.ENTITY, Visibility.PUBLIC):
        return str(resolved_role or "") in _ENTITY_DOCUMENT_READ_ROLES

    return False


async def effective_document_capabilities_for_user(
    db: AsyncSession,
    *,
    document: Document,
    user_id: str | None,
    role: str | None = None,
) -> set[str]:
    """Capabilities the current user effectively has on a document.

    This is used by API responses so the frontend can avoid guessing from
    owner_id alone. Entity/document owners are handled here; creator aliases
    are handled at route level where the full User identity is available.
    """
    if not user_id:
        return set()
    resolved_role = await _resolve_user_role(
        db,
        user_id=user_id,
        entity_id=document.entity_id,
        role=role,
    )
    if is_entity_admin_role(resolved_role) or document.owner_id == user_id:
        return set(_DOCUMENT_OWNER_CAPABILITIES)
    granted = await document_grant_capabilities_for_user(
        db,
        document=document,
        user_id=user_id,
    )
    if await user_can_read_document(
        db,
        document,
        entity_id=document.entity_id,
        user_id=user_id,
        role=resolved_role,
    ):
        granted.add(Capability.VIEW)
    return granted


async def user_can_read_document(
    db: AsyncSession,
    document: Document | None,
    *,
    entity_id: str,
    user_id: str | None = None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
) -> bool:
    if not document or document.entity_id != entity_id:
        return False

    # Preserve legacy/background callers that do not carry a user context.
    if not user_id:
        return True

    resolved_role = await _resolve_user_role(
        db,
        user_id=user_id,
        entity_id=entity_id,
        role=role,
    )
    if is_entity_admin_role(resolved_role):
        return True
    if document.owner_id and document.owner_id == user_id:
        return True
    if getattr(document, "quarantine_status", None) in _QUARANTINED_STATUSES:
        return False
    if getattr(document, "classification", None) == "restricted" and actor_type != "user":
        return False
    if await _has_read_grant(db, document=document, user_id=user_id):
        return True
    if not await _document_folder_path_is_readable(
        db,
        document=document,
        user_id=user_id,
        role=resolved_role,
    ):
        return False

    visibility = getattr(document, "visibility", None) or Visibility.ENTITY
    if visibility == Visibility.PRIVATE:
        return False

    if visibility == Visibility.WORKSPACE:
        if resolved_role == "client" and not getattr(document, "client_visible", False):
            return False
        linked_workspace_ids = await document_workspace_ids(db, document)
        if workspace_id:
            return (
                workspace_id in linked_workspace_ids
                and await user_can_read_workspace_id(
                    db,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    role=resolved_role,
                )
            )
        for linked_workspace_id in linked_workspace_ids:
            if await user_can_read_workspace_id(
                db,
                workspace_id=linked_workspace_id,
                entity_id=entity_id,
                user_id=user_id,
                role=resolved_role,
            ):
                return True
        return False

    if visibility in (Visibility.ENTITY, Visibility.PUBLIC):
        if resolved_role == "client":
            return bool(getattr(document, "client_visible", False))
        return str(resolved_role or "") in _ENTITY_DOCUMENT_READ_ROLES

    return False


async def document_is_client_visible(
    db: AsyncSession,
    document: Document | None,
    *,
    entity_id: str,
    workspace_id: str | None = None,
) -> bool:
    """True when a document may be surfaced to external/customer chats.

    This is intentionally stricter than legacy background-agent access:
    public chat visitors do not have a Manor user_id, so they must not inherit
    the old "system caller can read everything" behavior.
    """

    if not document or document.entity_id != entity_id:
        return False
    if not getattr(document, "client_visible", False):
        return False
    if getattr(document, "quarantine_status", None) in _QUARANTINED_STATUSES:
        return False
    if getattr(document, "classification", None) in {"confidential", "restricted"}:
        return False

    visibility = getattr(document, "visibility", None) or Visibility.ENTITY
    if visibility == Visibility.PRIVATE:
        return False
    if visibility == Visibility.WORKSPACE:
        linked_workspace_ids = await document_workspace_ids(db, document)
        return bool(linked_workspace_ids) if not workspace_id else workspace_id in linked_workspace_ids
    return visibility in (Visibility.ENTITY, Visibility.PUBLIC)


async def document_is_public_agent_visible(
    db: AsyncSession,
    document: Document | None,
    *,
    entity_id: str,
    workspace_id: str | None,
) -> bool:
    """True when a public agent chat may surface a document.

    Public chat is scoped to one workspace-bound agent. Entity-level
    ``client_visible`` is not enough here; the file must also belong to the
    current workspace.
    """

    workspace = str(workspace_id or "").strip()
    if not workspace:
        return False
    if not await document_is_client_visible(
        db,
        document,
        entity_id=entity_id,
        workspace_id=workspace,
    ):
        return False
    linked_workspace_ids = await document_workspace_ids(db, document)
    return workspace in linked_workspace_ids


async def get_visible_document(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    user_id: str | None = None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
) -> Document | None:
    from packages.core.services.document_service import get_document

    document = await get_document(db, doc_id, entity_id)
    if await user_can_read_document(
        db,
        document,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
        workspace_id=workspace_id,
        actor_type=actor_type,
    ):
        return document
    return None


async def list_visible_documents(
    db: AsyncSession,
    entity_id: str,
    *,
    user_id: str | None = None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
    name_search: str | None = None,
    folder_id: str | None = None,
    folder_ids: set[str] | None = None,
    include_generated_assets: bool = True,
    limit: int | None = 100,
    offset: int = 0,
) -> tuple[list[Document], int]:
    from packages.core.services.document_service import list_documents

    if not user_id:
        docs, total = await list_documents(
            db,
            entity_id,
            name_search=name_search,
            folder_id=folder_id,
            folder_ids=folder_ids,
            workspace_id=workspace_id,
            include_generated_assets=include_generated_assets,
            limit=limit,
            offset=offset,
        )
        readable = await _filter_readable_local_documents(db, docs)
        if len(readable) == len(docs):
            return readable, total
        return readable, max(0, total - (len(docs) - len(readable)))

    fetch_limit = None if limit is None else max(min(_INTERNAL_FILTER_LIMIT, max(limit + offset, limit, 100) * 4), limit)
    candidates, _ = await list_documents(
        db,
        entity_id,
        name_search=name_search,
        folder_id=folder_id,
        folder_ids=folder_ids,
        workspace_id=workspace_id,
        include_generated_assets=include_generated_assets,
        limit=fetch_limit,
        offset=0,
    )
    candidates = await _filter_readable_local_documents(db, candidates)
    resolved_role = await _resolve_user_role(
        db,
        user_id=user_id,
        entity_id=entity_id,
        role=role,
    )
    visible: list[Document] = []
    for document in candidates:
        if await user_can_read_document(
            db,
            document,
            entity_id=entity_id,
            user_id=user_id,
            role=resolved_role,
            workspace_id=workspace_id,
            actor_type=actor_type,
        ):
            visible.append(document)
    if limit is None:
        return visible[offset:], len(visible)
    return visible[offset : offset + limit], len(visible)


async def visible_storage_usage(
    db: AsyncSession,
    entity_id: str,
    *,
    user_id: str | None = None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
    name_search: str | None = None,
    folder_ids: set[str] | None = None,
    include_generated_assets: bool = True,
) -> tuple[int, int]:
    """Return size/count for documents visible to the current user."""
    from packages.core.services.document_service import list_documents

    resolved_role = await _resolve_user_role(
        db,
        user_id=user_id,
        entity_id=entity_id,
        role=role,
    )
    total_size = 0
    total_files = 0
    offset = 0
    batch_size = _INTERNAL_FILTER_LIMIT
    while True:
        docs, raw_total = await list_documents(
            db,
            entity_id,
            name_search=name_search,
            folder_ids=folder_ids,
            workspace_id=workspace_id,
            include_generated_assets=include_generated_assets,
            limit=batch_size,
            offset=offset,
        )
        if not docs:
            break
        raw_batch_count = len(docs)
        docs = await _filter_readable_local_documents(db, docs)
        for document in docs:
            if await user_can_read_document(
                db,
                document,
                entity_id=entity_id,
                user_id=user_id,
                role=resolved_role,
                workspace_id=workspace_id,
                actor_type=actor_type,
            ):
                total_files += 1
                total_size += int(getattr(document, "file_size", None) or 0)
        offset += raw_batch_count
        if offset >= raw_total:
            break
    return total_size, total_files


async def visible_document_counts_by_folder(
    db: AsyncSession,
    entity_id: str,
    *,
    folder_ids: set[str],
    user_id: str | None = None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
) -> dict[str, int]:
    """Return direct visible document counts keyed by folder_id."""
    from packages.core.services.document_service import list_documents

    if not folder_ids:
        return {}
    resolved_role = await _resolve_user_role(
        db,
        user_id=user_id,
        entity_id=entity_id,
        role=role,
    )
    counts: dict[str, int] = {}
    offset = 0
    batch_size = _INTERNAL_FILTER_LIMIT
    while True:
        docs, raw_total = await list_documents(
            db,
            entity_id,
            folder_ids=folder_ids,
            limit=batch_size,
            offset=offset,
        )
        if not docs:
            break
        raw_batch_count = len(docs)
        docs = await _filter_readable_local_documents(db, docs)
        for document in docs:
            folder_id = getattr(document, "folder_id", None)
            if not folder_id or folder_id not in folder_ids:
                continue
            if await user_can_read_document(
                db,
                document,
                entity_id=entity_id,
                user_id=user_id,
                role=resolved_role,
                workspace_id=workspace_id,
                actor_type=actor_type,
            ):
                counts[folder_id] = counts.get(folder_id, 0) + 1
        offset += raw_batch_count
        if offset >= raw_total:
            break
    return counts
