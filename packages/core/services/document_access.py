"""Document visibility helpers used by API and runtime entrypoints."""
from __future__ import annotations

import asyncio
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
    """Keep stale local-file rows in Knowledge lists and mark them missing.

    The filesystem is the source of truth for rows with ``fs_path``. Background
    reconcile eventually repairs or records these. A read path must not
    permanently trash or hide documents because a mounted filesystem can be
    temporarily unavailable.
    """
    from packages.core.config import get_settings

    settings = get_settings()
    if not getattr(settings, "MANOR_FS_ENABLED", False):
        return documents
    fs_root = getattr(settings, "MANOR_FS_ROOT", "")

    # Resolve every path and stat it in one worker-thread batch. The mount is
    # a network filesystem: each realpath/isdir/isfile is a round-trip, and
    # doing them inline on the event loop stalled every request in the
    # process when a listing touched hundreds of rows.
    stat_docs = [
        document for document in documents
        if not getattr(document, "file_url", None) and getattr(document, "fs_path", None)
    ]

    def _stat_batch() -> dict[str, tuple[bool, str | None, bool]]:
        results: dict[str, tuple[bool, str | None, bool]] = {}
        root_is_dir: dict[str, bool] = {}
        for document in stat_docs:
            entity_root = os.path.realpath(
                os.path.join(fs_root, str(getattr(document, "entity_id", "")))
            )
            root_ok = root_is_dir.get(entity_root)
            if root_ok is None:
                root_ok = os.path.isdir(entity_root)
                root_is_dir[entity_root] = root_ok
            if not root_ok:
                results[document.id] = (False, None, False)
                continue
            full_path = _document_local_path(document, fs_root)
            is_file = bool(full_path and os.path.isfile(full_path))
            results[document.id] = (True, full_path, is_file)
        return results

    stats = await asyncio.to_thread(_stat_batch) if stat_docs else {}

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
                    recoverable=True,
                )
                mutated = True
                continue
            visible.append(document)
            continue

        root_ok, full_path, is_file = stats.get(document.id, (False, None, False))
        if not root_ok:
            document.metadata_ = _with_file_integrity(
                getattr(document, "metadata_", None),
                status="unavailable",
                fs_path=str(getattr(document, "fs_path", "") or ""),
                source="knowledge_list",
                recoverable=True,
            )
            mutated = True
            visible.append(document)
            continue

        if is_file:
            visible.append(document)
            continue

        if getattr(document, "vector_status", None) in _READABLE_LOCAL_SKIP_STATUSES:
            document.metadata_ = _with_file_integrity(
                getattr(document, "metadata_", None),
                status="pending",
                fs_path=str(getattr(document, "fs_path", "") or ""),
                source="knowledge_list",
                path=full_path,
                recoverable=True,
            )
            mutated = True
            visible.append(document)
            continue

        document.metadata_ = _with_file_integrity(
            getattr(document, "metadata_", None),
            status="missing" if full_path else "invalid_path",
            fs_path=str(getattr(document, "fs_path", "") or ""),
            source="knowledge_list",
            path=full_path,
            recoverable=True,
        )
        mutated = True
        visible.append(document)

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
        if str(resolved_role or "") not in _ENTITY_DOCUMENT_READ_ROLES:
            return False
        if visibility == Visibility.PUBLIC:
            # Public means public — a container does not claw that back.
            return True
        return await _workspace_containers_allow_read(
            db,
            document=document,
            entity_id=entity_id,
            user_id=user_id,
            role=resolved_role,
        )

    return False


async def _workspace_containers_allow_read(
    db: AsyncSession,
    *,
    document: Document,
    entity_id: str,
    user_id: str | None,
    role: str | None,
) -> bool:
    """Whether an entity-visible document is reachable through its workspaces.

    Filing a document into a workspace's knowledge net binds it to that
    workspace: the same container narrowing that
    :func:`_document_folder_path_is_readable` already applies to folders.
    Without this, a document in a ``members_only`` workspace stayed readable by
    the whole organization — the workspace hid itself but not its contents.

    A document in no workspace is unaffected, and one in several is readable if
    any of them is, so sharing into a second workspace never removes access.
    Workspaces set to ``entity_visible`` admit every member anyway, so this
    only bites where the workspace is genuinely restricted.
    """
    linked_workspace_ids = await document_workspace_ids(db, document)
    if not linked_workspace_ids:
        return True
    for linked_workspace_id in linked_workspace_ids:
        if await user_can_read_workspace_id(
            db,
            workspace_id=linked_workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=role,
        ):
            return True
    return False


class DocumentAccessContext:
    """Batched evaluator for one user's document/folder read access.

    ``user_can_read_document`` / ``user_can_read_folder`` issue several
    queries per row (subject ids, ancestor walks, grants, workspace links);
    listing endpoints calling them once per document turned into tens of
    thousands of round-trips on large entities. This context loads the same
    state up front — a fixed number of queries per request — and evaluates
    the identical rules in memory.

    It must stay behaviorally equivalent to the single-item helpers; parity
    is pinned by tests/test_document_access_batch.py. If you change one side,
    change the other.
    """

    def __init__(
        self,
        *,
        entity_id: str,
        user_id: str | None,
        role: str | None,
    ) -> None:
        self.entity_id = entity_id
        self.user_id = user_id
        self.role = role
        self.is_admin = is_entity_admin_role(role)
        self._folder_by_id: dict[str, DocumentFolder] = {}
        self._doc_caps: dict[str, set[str]] = {}
        self._folder_caps: dict[str, set[str]] = {}
        self._chain_cache: dict[str, list[str]] = {}
        self._ws_readable: dict[str, bool] = {}
        self._doc_group_links: dict[str, set[str]] = {}

    @classmethod
    async def load(
        cls,
        db: AsyncSession,
        *,
        entity_id: str,
        user_id: str | None = None,
        role: str | None = None,
    ) -> "DocumentAccessContext":
        resolved_role = await _resolve_user_role(
            db,
            user_id=user_id,
            entity_id=entity_id,
            role=role,
        )
        ctx = cls(entity_id=entity_id, user_id=user_id, role=resolved_role)
        # Admins and user-less (background) callers short-circuit to True in
        # the single-item helpers; no grant/folder state is ever consulted.
        if not user_id or ctx.is_admin:
            return ctx

        subject_ids = await _grant_subject_ids_for_user(
            db,
            entity_id=entity_id,
            user_id=user_id,
        )
        folders = (
            await db.execute(
                select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
            )
        ).scalars().all()
        ctx._folder_by_id = {folder.id: folder for folder in folders}

        if subject_ids:
            grants = (
                await db.execute(
                    select(ResourceGrant).where(
                        ResourceGrant.entity_id == entity_id,
                        ResourceGrant.subject_type == SubjectType.USER,
                        ResourceGrant.subject_id.in_(subject_ids),
                        ResourceGrant.status == GrantStatus.ACTIVE,
                        ResourceGrant.resource_type.in_(
                            [ResourceType.DOCUMENT, ResourceType.DOCUMENT_FOLDER]
                        ),
                    )
                )
            ).scalars().all()
            doc_rows: dict[str, list[ResourceGrant]] = {}
            folder_rows: dict[str, list[ResourceGrant]] = {}
            for grant in grants:
                target = (
                    doc_rows if grant.resource_type == ResourceType.DOCUMENT else folder_rows
                )
                target.setdefault(str(grant.resource_id), []).append(grant)
            ctx._doc_caps = {
                doc_id: _active_capabilities_from_grants(rows)
                for doc_id, rows in doc_rows.items()
            }
            ctx._folder_caps = {
                folder_id: _active_capabilities_from_grants(rows)
                for folder_id, rows in folder_rows.items()
            }
        return ctx

    # ── folder rules ─────────────────────────────────────────────────────

    def folder_chain_ids(self, folder_id: str | None) -> list[str]:
        """The folder and its ancestors, nearest first — `_folder_ancestor_ids`."""
        if not folder_id:
            return []
        cached = self._chain_cache.get(folder_id)
        if cached is not None:
            return cached
        ids: list[str] = []
        seen: set[str] = set()
        current_id: str | None = folder_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            folder = self._folder_by_id.get(current_id)
            if folder is None:
                break
            ids.append(folder.id)
            current_id = folder.parent_id
        self._chain_cache[folder_id] = ids
        return ids

    def folder_capabilities(self, folder_id: str | None) -> set[str]:
        """Active grant capabilities from the folder and its ancestors —
        ``folder_grant_capabilities_for_user``."""
        capabilities: set[str] = set()
        for chain_id in self.folder_chain_ids(folder_id):
            capabilities.update(self._folder_caps.get(chain_id, set()))
        return capabilities

    def can_read_folder(self, folder: DocumentFolder | None) -> bool:
        """In-memory ``user_can_read_folder``."""
        if not folder or folder.entity_id != self.entity_id:
            return False
        if not self.user_id:
            return True
        if self.is_admin:
            return True
        if getattr(folder, "owner_id", None) == self.user_id:
            return True
        if self.folder_capabilities(getattr(folder, "id", None)).intersection(
            _FOLDER_READ_CAPABILITIES
        ):
            return True
        visibility = getattr(folder, "visibility", None) or Visibility.ENTITY
        if visibility == Visibility.PRIVATE:
            return False
        if visibility in (Visibility.WORKSPACE, Visibility.ENTITY, Visibility.PUBLIC):
            return str(self.role or "") in _ENTITY_DOCUMENT_READ_ROLES
        return False

    def folder_path_readable(self, folder_id: str | None) -> bool:
        """In-memory ``_document_folder_path_is_readable``."""
        if not self.user_id or self.is_admin:
            return True
        for chain_id in self.folder_chain_ids(folder_id):
            if not self.can_read_folder(self._folder_by_id.get(chain_id)):
                return False
        return True

    # ── document rules ───────────────────────────────────────────────────

    async def preload_documents(self, db: AsyncSession, documents: list[Document]) -> None:
        """Load workspace-group links for a batch of documents in one query."""
        if not self.user_id or self.is_admin:
            return
        wanted = [
            document.id for document in documents
            if document.id not in self._doc_group_links
        ]
        if not wanted:
            return
        for doc_id in wanted:
            self._doc_group_links[doc_id] = set()
        rows = (
            await db.execute(
                select(DocumentGroupMember.document_id, DocumentGroup.workspace_id)
                .join(DocumentGroup, DocumentGroupMember.group_id == DocumentGroup.id)
                .where(
                    DocumentGroupMember.document_id.in_(wanted),
                    DocumentGroup.entity_id == self.entity_id,
                    DocumentGroup.workspace_id.isnot(None),
                )
            )
        ).all()
        for doc_id, workspace_id in rows:
            if workspace_id:
                self._doc_group_links[doc_id].add(str(workspace_id))

    def _workspace_ids_for(self, document: Document) -> set[str]:
        """``document_workspace_ids`` from the preloaded links + metadata."""
        workspace_ids = set(self._doc_group_links.get(document.id, set()))
        meta = document.metadata_ if isinstance(document.metadata_, dict) else {}
        origin = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
        for value in (origin.get("workspace_id"), meta.get("workspace_id")):
            if value:
                workspace_ids.add(str(value))
        return workspace_ids

    async def _workspace_readable(self, db: AsyncSession, workspace_id: str) -> bool:
        cached = self._ws_readable.get(workspace_id)
        if cached is None:
            cached = await user_can_read_workspace_id(
                db,
                workspace_id=workspace_id,
                entity_id=self.entity_id,
                user_id=self.user_id,
                role=self.role,
            )
            self._ws_readable[workspace_id] = cached
        return cached

    async def can_read_document(
        self,
        db: AsyncSession,
        document: Document | None,
        *,
        workspace_id: str | None = None,
        actor_type: str = "user",
    ) -> bool:
        """In-memory ``user_can_read_document``. Call ``preload_documents``
        on the batch first, or workspace links fall back to empty."""
        if not document or document.entity_id != self.entity_id:
            return False
        if not self.user_id:
            return True
        if self.is_admin:
            return True
        if document.owner_id and document.owner_id == self.user_id:
            return True
        if getattr(document, "quarantine_status", None) in _QUARANTINED_STATUSES:
            return False
        if getattr(document, "classification", None) == "restricted" and actor_type != "user":
            return False
        granted = set(self._doc_caps.get(document.id, set()))
        granted.update(self.folder_capabilities(document.folder_id))
        if granted.intersection(_DOCUMENT_READ_CAPABILITIES):
            return True
        if not self.folder_path_readable(document.folder_id):
            return False

        visibility = getattr(document, "visibility", None) or Visibility.ENTITY
        if visibility == Visibility.PRIVATE:
            return False

        if visibility == Visibility.WORKSPACE:
            if self.role == "client" and not getattr(document, "client_visible", False):
                return False
            linked_workspace_ids = self._workspace_ids_for(document)
            if workspace_id:
                return (
                    workspace_id in linked_workspace_ids
                    and await self._workspace_readable(db, workspace_id)
                )
            for linked_workspace_id in linked_workspace_ids:
                if await self._workspace_readable(db, linked_workspace_id):
                    return True
            return False

        if visibility in (Visibility.ENTITY, Visibility.PUBLIC):
            if self.role == "client":
                return bool(getattr(document, "client_visible", False))
            if str(self.role or "") not in _ENTITY_DOCUMENT_READ_ROLES:
                return False
            if visibility == Visibility.PUBLIC:
                return True
            linked_workspace_ids = self._workspace_ids_for(document)
            if not linked_workspace_ids:
                return True
            for linked_workspace_id in linked_workspace_ids:
                if await self._workspace_readable(db, linked_workspace_id):
                    return True
            return False

        return False

    async def effective_document_capabilities(
        self,
        db: AsyncSession,
        document: Document,
    ) -> set[str]:
        """In-memory ``effective_document_capabilities_for_user``."""
        if not self.user_id:
            return set()
        if self.is_admin or document.owner_id == self.user_id:
            return set(_DOCUMENT_OWNER_CAPABILITIES)
        granted = set(self._doc_caps.get(document.id, set()))
        granted.update(self.folder_capabilities(document.folder_id))
        if await self.can_read_document(db, document):
            granted.add(Capability.VIEW)
        return granted


async def unreadable_document_paths(
    db: AsyncSession,
    *,
    entity_id: str,
    rel_paths: "list[str]",
    user_id: str | None,
    role: str | None = None,
    actor_type: str = "user",
) -> set[str]:
    """Of ``rel_paths`` (entity-root-relative FS paths), return the normalized
    paths that map to a Knowledge ``Document`` the caller may NOT read.

    Knowledge documents live as real files under the entity FS root, so raw
    filesystem tools (agent ``read_file`` / ``list_files`` / ``grep`` / the
    ``/fs`` router) would otherwise serve a document's bytes without consulting
    ``Document.visibility``. This maps each path to its ``Document`` row and
    gates it through :func:`user_can_read_document`.

    Paths with no matching ``Document`` row (workspace artifacts, agent scratch,
    avatars) are never returned — they are not Knowledge documents, and their
    existing entity + hidden-path scoping applies. When ``user_id`` is falsy the
    read guard falls back to its legacy background-caller allowance, so nothing
    is blocked; callers that must fail closed should pass a real ``user_id``.
    """
    from packages.core.services.knowledge_visibility import normalize_rel_path

    wanted = {normalize_rel_path(p) for p in rel_paths if p}
    if not wanted:
        return set()
    rows = list((
        await db.execute(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path.in_(wanted),
                Document.is_trashed == False,  # noqa: E712
            )
        )
    ).scalars().all())
    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )
    await ctx.preload_documents(db, rows)
    blocked: set[str] = set()
    for doc in rows:
        if not await ctx.can_read_document(db, doc, actor_type=actor_type):
            blocked.add(normalize_rel_path(doc.fs_path))
    return blocked


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
    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )
    await ctx.preload_documents(db, candidates)
    visible: list[Document] = []
    for document in candidates:
        if await ctx.can_read_document(
            db,
            document,
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

    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        user_id=user_id,
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
        await ctx.preload_documents(db, docs)
        for document in docs:
            if await ctx.can_read_document(
                db,
                document,
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
    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        user_id=user_id,
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
        await ctx.preload_documents(db, docs)
        for document in docs:
            folder_id = getattr(document, "folder_id", None)
            if not folder_id or folder_id not in folder_ids:
                continue
            if await ctx.can_read_document(
                db,
                document,
                workspace_id=workspace_id,
                actor_type=actor_type,
            ):
                counts[folder_id] = counts.get(folder_id, 0) + 1
        offset += raw_batch_count
        if offset >= raw_total:
            break
    return counts
