"""Document visibility helpers used by API and runtime entrypoints."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timezone

from sqlalchemy import and_, or_, select, tuple_
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
from packages.core.models.user import User
from packages.core.permissions import Permission, user_staff_role_assignment
from packages.core.services.actor_authorization import (
    AuthenticatedUserCredential,
    ResolvedUserActor,
    resolve_current_user_actor,
)
from packages.core.services.workspace_access import (
    ENTITY_WORKSPACE_READ_ROLES,
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
_CLASSIFICATION_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
_VISIBILITY_RANK = {"private": 0, "workspace": 1, "entity": 2, "public": 3}
_INTERNAL_FILTER_LIMIT = 2_000
_INTERNAL_QUERY_LIMIT = 500
_READABLE_LOCAL_SKIP_STATUSES = {VectorStatus.PROCESSING, VectorStatus.GENERATING}
_PLACEHOLDER_UNAVAILABLE_STATUSES = {VectorStatus.FAILED, VectorStatus.SKIPPED}


def _id_batches(values: set[str] | list[str], size: int = _INTERNAL_QUERY_LIMIT):
    ordered = list(values)
    for offset in range(0, len(ordered), size):
        yield set(ordered[offset:offset + size])


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


# Stat results for entity-filesystem document files, keyed by
# (fs_root, entity_id, fs_path). The mount is a network filesystem, so each
# realpath/isfile is a metadata round-trip; listings re-stat the same files on
# every request without this. Hits (file exists) stay valid for
# _STAT_CACHE_TTL; misses use the shorter negative TTL so a file that just
# landed (upload, repair, reconcile) stops being reported missing quickly.
# Integrity marks written from stale entries are recoverable by design.
_STAT_CACHE: dict[tuple[str, str, str], tuple[float, bool, str | None, bool]] = {}
_STAT_CACHE_TTL = 60.0
_STAT_CACHE_NEGATIVE_TTL = 5.0
_STAT_CACHE_MAX = 50_000
_STAT_CONCURRENCY = 8


@dataclass(frozen=True)
class ResourcePolicyLockScope:
    """Stable ownership/policy rows that must precede a resource row lock."""

    folder_ids: tuple[str, ...]
    workspace_ids: tuple[str, ...]


class ResourcePolicyMutationConflictError(RuntimeError):
    """A folder/document policy scope changed while mutation locks were acquired."""


def _clear_local_stat_cache() -> None:
    """Test hook."""
    _STAT_CACHE.clear()


async def _stat_local_documents(
    stat_docs: list[Document],
    fs_root: str,
) -> dict[str, tuple[bool, str | None, bool]]:
    """Resolve+stat document files off the event loop, through the TTL cache.

    Returns ``{document.id: (root_ok, full_path, is_file)}``.
    """
    import time

    now = time.monotonic()
    results: dict[str, tuple[bool, str | None, bool]] = {}
    misses: list[Document] = []
    for document in stat_docs:
        key = (fs_root, str(document.entity_id), str(document.fs_path))
        entry = _STAT_CACHE.get(key)
        if entry is not None and entry[0] > now:
            results[document.id] = entry[1:]
        else:
            misses.append(document)
    if not misses:
        return results

    def _stat_chunk(chunk: list[Document]) -> dict[str, tuple[bool, str | None, bool]]:
        chunk_results: dict[str, tuple[bool, str | None, bool]] = {}
        root_is_dir: dict[str, bool] = {}
        for document in chunk:
            entity_root = os.path.realpath(
                os.path.join(fs_root, str(getattr(document, "entity_id", "")))
            )
            root_ok = root_is_dir.get(entity_root)
            if root_ok is None:
                root_ok = os.path.isdir(entity_root)
                root_is_dir[entity_root] = root_ok
            if not root_ok:
                chunk_results[document.id] = (False, None, False)
                continue
            full_path = _document_local_path(document, fs_root)
            is_file = bool(full_path and os.path.isfile(full_path))
            chunk_results[document.id] = (True, full_path, is_file)
        return chunk_results

    chunks = [misses[i::_STAT_CONCURRENCY] for i in range(_STAT_CONCURRENCY)]
    chunks = [chunk for chunk in chunks if chunk]
    parts = await asyncio.gather(*[asyncio.to_thread(_stat_chunk, chunk) for chunk in chunks])
    fresh: dict[str, tuple[bool, str | None, bool]] = {}
    for part in parts:
        fresh.update(part)

    now = time.monotonic()
    for document in misses:
        entry = fresh.get(document.id)
        if entry is None:
            continue
        results[document.id] = entry
        ttl = _STAT_CACHE_TTL if (entry[0] and entry[2]) else _STAT_CACHE_NEGATIVE_TTL
        _STAT_CACHE[(fs_root, str(document.entity_id), str(document.fs_path))] = (now + ttl, *entry)

    if len(_STAT_CACHE) > _STAT_CACHE_MAX:
        expired = [key for key, entry in _STAT_CACHE.items() if entry[0] <= now]
        for key in expired:
            _STAT_CACHE.pop(key, None)
        while len(_STAT_CACHE) > _STAT_CACHE_MAX:
            _STAT_CACHE.pop(next(iter(_STAT_CACHE)), None)
    return results


async def _filter_readable_local_documents(
    db: AsyncSession,
    documents: list[Document],
    *,
    stat_files: bool = True,
) -> list[Document]:
    """Keep stale local-file rows in Knowledge lists and mark them missing.

    The filesystem is the source of truth for rows with ``fs_path``. Background
    reconcile eventually repairs or records these. A read path must not
    permanently trash or hide documents because a mounted filesystem can be
    temporarily unavailable.

    ``stat_files=False`` skips the filesystem entirely: rows with ``fs_path``
    are always kept visible regardless of stat outcome (the stats only feed
    the ``file_integrity`` metadata mark), so aggregation passes — folder
    counts, storage totals — get identical visibility without paying one
    network-filesystem round-trip per document.
    """
    from packages.core.config import get_settings

    settings = get_settings()
    if not getattr(settings, "MANOR_FS_ENABLED", False):
        return documents
    fs_root = getattr(settings, "MANOR_FS_ROOT", "")

    stat_docs = [
        document for document in documents
        if not getattr(document, "file_url", None) and getattr(document, "fs_path", None)
    ] if stat_files else []
    stats = await _stat_local_documents(stat_docs, fs_root) if stat_docs else {}

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

        if not stat_files:
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


async def _resolve_current_actor_for_entity(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
) -> ResolvedUserActor | None:
    return await resolve_current_user_actor(
        db,
        AuthenticatedUserCredential(
            user_id=str(user_id),
            entity_id=str(entity_id),
            token_version=None,
        )
    )


async def _resolve_user_role_and_document_read(
    db: AsyncSession,
    *,
    user_id: str | None,
    entity_id: str,
    role: str | None,
    _actor: ResolvedUserActor | None = None,
) -> tuple[str | None, bool, bool]:
    if not user_id:
        return role, True, True
    if _actor is not None:
        if _actor.user_id != str(user_id) or _actor.entity_id != str(entity_id):
            return None, False, False
        return (
            _actor.role,
            _actor.can_read_documents,
            _actor.can_read_workspaces,
        )
    has_staff_record, _, assigned_role, permissions = await user_staff_role_assignment(
        db,
        user_id,
        entity_id,
    )
    if has_staff_record:
        resolved_role = str(assigned_role or "").strip().lower() or None
        permission_keys = {str(permission) for permission in permissions}
        return (
            resolved_role,
            Permission.DOCS_READ.value in permission_keys,
            Permission.WORKSPACES_READ.value in permission_keys,
        )
    if role:
        resolved_role = role
    else:
        actor = await _resolve_current_actor_for_entity(
            db,
            user_id=user_id,
            entity_id=entity_id,
        )
        if actor is None:
            return None, False, False
        resolved_role = actor.role
    resolved_role = str(resolved_role or "").strip().lower() or None
    return (
        resolved_role,
        resolved_role in _ENTITY_DOCUMENT_READ_ROLES,
        resolved_role in ENTITY_WORKSPACE_READ_ROLES,
    )


async def _resolve_user_role(
    db: AsyncSession,
    *,
    user_id: str | None,
    entity_id: str,
    role: str | None,
) -> str | None:
    resolved_role, _, _ = await _resolve_user_role_and_document_read(
        db,
        user_id=user_id,
        entity_id=entity_id,
        role=role,
    )
    return resolved_role


async def document_workspace_ids_batched(
    db: AsyncSession,
    documents: list[Document],
) -> dict[str, set[str]]:
    """Resolve Workspace links for many Documents with bounded queries."""
    workspace_ids_by_document = {
        str(document.id): set(_document_metadata_workspace_ids(document))
        for document in documents
        if document is not None and document.id
    }
    document_ids_by_entity: dict[str, list[str]] = {}
    document_ids_by_physical_owner: dict[tuple[str, str], list[str]] = {}
    document_ids_by_folder: dict[tuple[str, str], set[str]] = {}
    for document in documents:
        if document is None or not document.id or not document.entity_id:
            continue
        entity_id = str(document.entity_id)
        document_id = str(document.id)
        document_ids_by_entity.setdefault(entity_id, []).append(document_id)
        folder_id = str(getattr(document, "folder_id", None) or "").strip()
        if folder_id:
            document_ids_by_folder.setdefault((entity_id, folder_id), set()).add(
                document_id
            )
        physical_artifact_folder_id = _document_physical_artifact_folder_id(document)
        if physical_artifact_folder_id:
            document_ids_by_physical_owner.setdefault(
                (entity_id, physical_artifact_folder_id),
                [],
            ).append(document_id)

    # Walk all referenced folder chains by depth, not by document.  This keeps
    # batch callers bounded while preserving the exact same ancestor-based
    # Workspace ownership rule as ``resolve_document_policy_lock_scope``.
    if document_ids_by_folder:
        from packages.core.models.workspace import Workspace

        for entity_id in sorted({key[0] for key in document_ids_by_folder}):
            owners_by_folder = {
                folder_id: set(document_ids)
                for (folder_entity_id, folder_id), document_ids
                in document_ids_by_folder.items()
                if folder_entity_id == entity_id
            }
            visited_by_document: dict[str, set[str]] = {}
            frontier = dict(owners_by_folder)
            while frontier:
                rows = []
                ordered_ids = sorted(frontier)
                for start in range(0, len(ordered_ids), 500):
                    rows.extend((await db.execute(
                        select(DocumentFolder.id, DocumentFolder.parent_id).where(
                            DocumentFolder.entity_id == entity_id,
                            DocumentFolder.id.in_(ordered_ids[start:start + 500]),
                        )
                    )).all())
                next_frontier: dict[str, set[str]] = {}
                for folder_id, parent_id in rows:
                    document_ids = frontier.get(str(folder_id), set())
                    owners_by_folder.setdefault(str(folder_id), set()).update(
                        document_ids
                    )
                    if not parent_id:
                        continue
                    parent_key = str(parent_id)
                    for document_id in document_ids:
                        visited = visited_by_document.setdefault(document_id, set())
                        if parent_key in visited:
                            continue
                        visited.add(parent_key)
                        next_frontier.setdefault(parent_key, set()).add(document_id)
                frontier = next_frontier

            ordered_folder_ids = sorted(owners_by_folder)
            for start in range(0, len(ordered_folder_ids), 500):
                workspace_rows = (await db.execute(
                    select(Workspace.id, Workspace.artifact_folder_id).where(
                        Workspace.entity_id == entity_id,
                        Workspace.artifact_folder_id.in_(
                            ordered_folder_ids[start:start + 500]
                        ),
                    )
                )).all()
                for workspace_id, artifact_folder_id in workspace_rows:
                    for document_id in owners_by_folder.get(
                        str(artifact_folder_id),
                        set(),
                    ):
                        workspace_ids_by_document.setdefault(
                            document_id,
                            set(),
                        ).add(str(workspace_id))
    for entity_id, document_ids in document_ids_by_entity.items():
        for start in range(0, len(document_ids), 500):
            rows = (await db.execute(
                select(DocumentGroupMember.document_id, DocumentGroup.workspace_id)
                .join(DocumentGroup, DocumentGroupMember.group_id == DocumentGroup.id)
                .where(
                    DocumentGroupMember.document_id.in_(document_ids[start:start + 500]),
                    DocumentGroup.entity_id == entity_id,
                    DocumentGroup.workspace_id.isnot(None),
                )
            )).all()
            for document_id, workspace_id in rows:
                if workspace_id:
                    workspace_ids_by_document.setdefault(
                        str(document_id),
                        set(),
                    ).add(str(workspace_id))
    if document_ids_by_physical_owner:
        from packages.core.models.workspace import Workspace

        artifact_folder_ids_by_entity: dict[str, set[str]] = {}
        for entity_id, artifact_folder_id in document_ids_by_physical_owner:
            artifact_folder_ids_by_entity.setdefault(entity_id, set()).add(
                artifact_folder_id
            )
        for entity_id, artifact_folder_ids in artifact_folder_ids_by_entity.items():
            ordered_ids = sorted(artifact_folder_ids)
            for start in range(0, len(ordered_ids), 500):
                workspace_rows = (await db.execute(
                    select(Workspace.id, Workspace.artifact_folder_id).where(
                        Workspace.entity_id == entity_id,
                        Workspace.artifact_folder_id.in_(
                            ordered_ids[start:start + 500]
                        ),
                    )
                )).all()
                for workspace_id, artifact_folder_id in workspace_rows:
                    for document_id in document_ids_by_physical_owner.get(
                        (entity_id, str(artifact_folder_id)),
                        [],
                    ):
                        workspace_ids_by_document.setdefault(document_id, set()).add(
                            str(workspace_id)
                        )
    return workspace_ids_by_document


async def document_workspace_ids(db: AsyncSession, document: Document) -> set[str]:
    workspace_ids_by_document = await document_workspace_ids_batched(db, [document])
    return workspace_ids_by_document.get(str(document.id), set())


def _document_metadata_workspace_ids(document: Document) -> set[str]:
    raw_meta = getattr(document, "metadata_", None)
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    origin = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
    return {
        str(value)
        for value in (origin.get("workspace_id"), meta.get("workspace_id"))
        if value
    }


def _document_physical_artifact_folder_id(document: Document) -> str | None:
    """Resolve the Workspace owner encoded in an entity-relative artifact path."""
    from packages.core.services.workspace_artifacts import (
        artifact_folder_id_from_entity_storage_path,
    )

    return artifact_folder_id_from_entity_storage_path(
        getattr(document, "fs_path", None)
    )


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


async def resolve_folder_policy_lock_scope(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
) -> ResourcePolicyLockScope:
    """Snapshot the rows that own one folder's lifecycle and policy."""
    ancestor_ids = tuple(await _folder_ancestor_ids(
        db,
        entity_id=entity_id,
        folder_id=folder_id,
    ))
    if not ancestor_ids:
        return ResourcePolicyLockScope(folder_ids=(), workspace_ids=())

    from packages.core.models.workspace import Workspace

    workspace_ids = tuple(str(workspace_id) for workspace_id in (await db.execute(
        select(Workspace.id)
        .where(
            Workspace.entity_id == entity_id,
            Workspace.artifact_folder_id.in_(ancestor_ids),
        )
        .order_by(Workspace.id)
    )).scalars())
    return ResourcePolicyLockScope(
        folder_ids=ancestor_ids,
        workspace_ids=workspace_ids,
    )


async def resolve_document_policy_lock_scope(
    db: AsyncSession,
    document: Document,
) -> ResourcePolicyLockScope:
    """Snapshot one document's folder and durable Workspace ownership."""
    folder_scope = await resolve_folder_policy_lock_scope(
        db,
        entity_id=document.entity_id,
        folder_id=getattr(document, "folder_id", None),
    )
    workspace_ids = set(folder_scope.workspace_ids)
    workspace_ids.update(_document_metadata_workspace_ids(document))
    physical_artifact_folder_id = _document_physical_artifact_folder_id(document)
    if physical_artifact_folder_id:
        from packages.core.models.workspace import Workspace

        workspace_ids.update(str(workspace_id) for workspace_id in (await db.execute(
            select(Workspace.id).where(
                Workspace.entity_id == document.entity_id,
                Workspace.artifact_folder_id == physical_artifact_folder_id,
            )
        )).scalars())
    return ResourcePolicyLockScope(
        folder_ids=folder_scope.folder_ids,
        workspace_ids=tuple(sorted(workspace_ids)),
    )


async def lock_workspace_policy_rows(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_ids: tuple[str, ...],
    read: bool = False,
):
    """Lock lifecycle owners in canonical ID order."""
    if not workspace_ids:
        return []

    from packages.core.models.workspace import Workspace

    return list((await db.execute(
        select(Workspace)
        .where(
            Workspace.entity_id == entity_id,
            Workspace.id.in_(workspace_ids),
        )
        .order_by(Workspace.id)
        .with_for_update(read=read)
        .execution_options(populate_existing=True)
    )).scalars())


async def lock_folder_policy_rows(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
    folder_ids: tuple[str, ...] | None = None,
    read: bool = False,
) -> list[DocumentFolder]:
    """Lock one folder policy chain in stable order and refresh its rows."""
    ancestor_ids = list(folder_ids) if folder_ids is not None else await _folder_ancestor_ids(
        db, entity_id=entity_id, folder_id=folder_id,
    )
    if not ancestor_ids:
        return []
    return list((await db.execute(
        select(DocumentFolder)
        .where(
            DocumentFolder.entity_id == entity_id,
            DocumentFolder.id.in_(ancestor_ids),
        )
        .order_by(DocumentFolder.id)
        .with_for_update(read=read)
        .execution_options(populate_existing=True)
    )).scalars())


async def lock_folder_policy_boundaries(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_ids: set[str],
) -> tuple[dict[str, DocumentFolder], list]:
    """Lock Workspace owners, folder chains, and targets in canonical order."""
    target_ids = tuple(sorted({str(folder_id) for folder_id in folder_ids if folder_id}))
    if not target_ids:
        return {}, []

    for _attempt in range(3):
        savepoint = await db.begin_nested()
        try:
            targets = list((await db.execute(
                select(DocumentFolder).where(
                    DocumentFolder.entity_id == entity_id,
                    DocumentFolder.id.in_(target_ids),
                ).execution_options(populate_existing=True)
            )).scalars())
            if {folder.id for folder in targets} != set(target_ids):
                raise ResourcePolicyMutationConflictError("Folder no longer exists")

            scopes = {
                folder.id: await resolve_folder_policy_lock_scope(
                    db,
                    entity_id=entity_id,
                    folder_id=folder.id,
                )
                for folder in targets
            }
            workspace_ids = tuple(sorted({
                workspace_id
                for scope in scopes.values()
                for workspace_id in scope.workspace_ids
            }))
            workspaces = await lock_workspace_policy_rows(
                db,
                entity_id=entity_id,
                workspace_ids=workspace_ids,
            )
            chain_ids = tuple(sorted({
                folder_id
                for scope in scopes.values()
                for folder_id in scope.folder_ids
            }))
            await lock_folder_policy_rows(
                db,
                entity_id=entity_id,
                folder_id=None,
                folder_ids=chain_ids,
            )
            locked_targets = list((await db.execute(
                select(DocumentFolder).where(
                    DocumentFolder.entity_id == entity_id,
                    DocumentFolder.id.in_(target_ids),
                ).execution_options(populate_existing=True)
            )).scalars())
            if {folder.id for folder in locked_targets} != set(target_ids):
                await savepoint.rollback()
                continue
            locked_scopes = {
                folder.id: await resolve_folder_policy_lock_scope(
                    db,
                    entity_id=entity_id,
                    folder_id=folder.id,
                )
                for folder in locked_targets
            }
            if locked_scopes != scopes:
                await savepoint.rollback()
                continue
            await savepoint.commit()
            return {folder.id: folder for folder in locked_targets}, workspaces
        except BaseException:
            if savepoint.is_active:
                await savepoint.rollback()
            raise

    raise ResourcePolicyMutationConflictError(
        "Folder policy changed during the write; retry the request"
    )


async def folder_is_owned_by_deleted_workspace(
    db: AsyncSession,
    folder: DocumentFolder,
    *,
    for_update: bool = False,
) -> bool:
    """Return whether ``folder`` is inside a soft-deleted Workspace tree.

    ``Workspace.artifact_folder_id`` is the durable ownership boundary for the
    whole folder subtree. ACLs and public share tokens remain persisted during
    the restore grace period, so every access surface must re-check this owner
    instead of treating those rows as independent entity resources.
    """
    if for_update:
        _folders, workspaces = await lock_folder_policy_boundaries(
            db,
            entity_id=folder.entity_id,
            folder_ids={folder.id},
        )
        return any(workspace.deleted_at is not None for workspace in workspaces)

    scope = await resolve_folder_policy_lock_scope(
        db,
        entity_id=folder.entity_id,
        folder_id=folder.id,
    )
    if not scope.workspace_ids:
        return False
    from packages.core.models.workspace import Workspace

    workspaces = list((await db.execute(
        select(Workspace).where(
            Workspace.entity_id == folder.entity_id,
            Workspace.id.in_(scope.workspace_ids),
        ).execution_options(populate_existing=True)
    )).scalars())
    return any(workspace.deleted_at is not None for workspace in workspaces)


async def document_is_owned_by_deleted_workspace(
    db: AsyncSession,
    document: Document,
    *,
    for_update: bool = False,
    lock_for_read: bool = False,
) -> bool:
    """Return whether a document's owning Workspace is soft-deleted."""
    if for_update and lock_for_read:
        raise ValueError("Choose either a write lock or a read lock")
    scope = await resolve_document_policy_lock_scope(db, document)
    if not scope.workspace_ids:
        return False
    if for_update or lock_for_read:
        workspaces = await lock_workspace_policy_rows(
            db,
            entity_id=document.entity_id,
            workspace_ids=scope.workspace_ids,
            read=lock_for_read,
        )
    else:
        from packages.core.models.workspace import Workspace

        workspaces = list((await db.execute(
            select(Workspace).where(
                Workspace.entity_id == document.entity_id,
                Workspace.id.in_(scope.workspace_ids),
            ).execution_options(populate_existing=True)
        )).scalars())
    return any(workspace.deleted_at is not None for workspace in workspaces)


async def _grant_subject_ids_for_user(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    _actor: ResolvedUserActor | None = None,
) -> set[str]:
    """IDs that may appear in user grants for this login.

    New grants should store ``User.id``. Older flows occasionally stored the
    linked ``Staff.id`` while still marking the grant as subject_type=user;
    include both so historical shares keep working.
    """
    if not user_id:
        return set()
    actor = _actor or await _resolve_current_actor_for_entity(
        db, user_id=user_id, entity_id=entity_id,
    )
    if actor is None or actor.user_id != str(user_id) or actor.entity_id != str(entity_id):
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
    allow_redacted: bool = True,
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
        readable = _DOCUMENT_READ_CAPABILITIES if allow_redacted else {Capability.VIEW}
        if readable.intersection(set(grant.capabilities or [])):
            return True
    return False


async def document_grants_for_user(
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


async def folder_grants_for_user(
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


async def user_can_read_folder_path(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
    user_id: str | None,
    role: str | None,
    allow_redacted: bool = True,
    include_grants: bool = True,
    _actor: ResolvedUserActor | None = None,
) -> bool:
    """Resolve visibility over the complete folder path, including ancestors.

    Folder property cascades normally narrow child document visibility, but
    older rows or ``cascade=false`` updates can leave a document broader than
    its ancestor folder. Enforce the folder visibility ceiling at read time.
    """
    if not folder_id:
        return True
    folder_ids = await _folder_ancestor_ids(
        db,
        entity_id=entity_id,
        folder_id=folder_id,
    )
    if not folder_ids:
        return True
    folders = list((
        await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.entity_id == entity_id,
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
            entity_id=entity_id,
            user_id=user_id,
            role=role,
            allow_redacted=allow_redacted,
            include_grants=include_grants,
            _actor=_actor,
        ):
            return False
    return True


def _merge_document_folder_policy(
    document: Document | DocumentFolder,
    folders: list[DocumentFolder],
) -> tuple[str, str, bool]:
    classification = str(getattr(document, "classification", None) or "internal")
    visibility = str(getattr(document, "visibility", None) or Visibility.ENTITY)
    client_visible = bool(getattr(document, "client_visible", False))
    for folder in folders:
        folder_classification = str(
            getattr(folder, "classification", None) or "internal"
        )
        if _CLASSIFICATION_RANK.get(folder_classification, 1) > _CLASSIFICATION_RANK.get(
            classification, 1
        ):
            classification = folder_classification
        folder_visibility = str(getattr(folder, "visibility", None) or Visibility.ENTITY)
        if _VISIBILITY_RANK.get(folder_visibility, 2) < _VISIBILITY_RANK.get(visibility, 2):
            visibility = folder_visibility
        if getattr(folder, "client_visible", None) is False:
            client_visible = False
    if classification in {"confidential", "restricted"}:
        client_visible = False
    return classification, visibility, client_visible


async def effective_document_folder_policy(
    db: AsyncSession,
    document: Document,
) -> tuple[str, str, bool]:
    """Return classification floor, visibility ceiling, and client flag.

    This read-time projection keeps legacy rows and ``cascade=false`` folder
    updates safe even before descendant rows are physically normalized.
    """
    if not getattr(document, "folder_id", None):
        return _merge_document_folder_policy(document, [])
    folder_ids = await _folder_ancestor_ids(
        db,
        entity_id=document.entity_id,
        folder_id=document.folder_id,
    )
    if not folder_ids:
        return _merge_document_folder_policy(document, [])
    folders = list((await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.entity_id == document.entity_id,
            DocumentFolder.id.in_(folder_ids),
        )
    )).scalars().all())
    return _merge_document_folder_policy(document, folders)


async def effective_folder_policy(
    db: AsyncSession,
    folder: DocumentFolder,
) -> tuple[str, str, bool]:
    """Return the effective policy for a folder and all of its ancestors."""
    folder_ids = await _folder_ancestor_ids(
        db,
        entity_id=folder.entity_id,
        folder_id=folder.id,
    )
    folders = list((await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.entity_id == folder.entity_id,
            DocumentFolder.id.in_(folder_ids),
        )
    )).scalars().all()) if folder_ids else []
    return _merge_document_folder_policy(folder, folders)


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
        await document_grants_for_user(db, document=document, user_id=user_id)
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
        await folder_grants_for_user(
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
    if await document_is_owned_by_deleted_workspace(db, document):
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
    if not folder_id:
        return False
    folder = (await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.id == folder_id,
            DocumentFolder.entity_id == entity_id,
        ).limit(1)
    )).scalar_one_or_none()
    if folder is None or await folder_is_owned_by_deleted_workspace(db, folder):
        return False
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
    allow_redacted: bool = True,
    include_grants: bool = True,
    _actor: ResolvedUserActor | None = None,
) -> bool:
    """Return whether a folder may be listed/opened by this user.

    Folder ACLs cascade down the folder tree via
    ``folder_grant_capabilities_for_user``. ``private`` folder visibility is
    therefore enforced here rather than left to the UI list filter.
    ``include_grants=False`` resolves independent visibility for delegation;
    a temporary folder grant must not become unlimited implicit authority.
    """
    if not folder or folder.entity_id != entity_id:
        return False

    if await folder_is_owned_by_deleted_workspace(db, folder):
        return False

    # Preserve legacy/background callers that do not carry a user context.
    if not user_id:
        return True

    actor = _actor or await _resolve_current_actor_for_entity(
        db, user_id=user_id, entity_id=entity_id,
    )
    if actor is None:
        return False
    (
        resolved_role,
        can_read_entity_documents,
        can_read_entity_workspaces,
    ) = (
        await _resolve_user_role_and_document_read(
            db,
            user_id=user_id,
            entity_id=entity_id,
            role=actor.role,
            _actor=actor,
        )
    )
    if is_entity_admin_role(resolved_role):
        return True
    if getattr(folder, "owner_id", None) == user_id:
        return True

    if include_grants:
        granted = await folder_grant_capabilities_for_user(
            db,
            entity_id=entity_id,
            folder_id=getattr(folder, "id", None),
            user_id=user_id,
        )
        readable = _FOLDER_READ_CAPABILITIES if allow_redacted else {Capability.VIEW}
        if granted.intersection(readable):
            return True

    visibility = getattr(folder, "visibility", None) or Visibility.ENTITY
    if visibility == Visibility.PRIVATE:
        return False

    if visibility == Visibility.WORKSPACE:
        # Workspace artifact folders use their stable DocumentFolder id as the
        # Workspace.artifact_folder_id. Descendants inherit that binding from
        # the first matching ancestor. Fail closed when no live Workspace owns
        # the folder path; ``workspace`` must never degrade to entity-wide.
        from packages.core.models.workspace import Workspace

        ancestor_ids = await _folder_ancestor_ids(
            db,
            entity_id=entity_id,
            folder_id=getattr(folder, "id", None),
        )
        workspace_ids = list((await db.execute(
            select(Workspace.id).where(
                Workspace.entity_id == entity_id,
                Workspace.artifact_folder_id.in_(ancestor_ids),
                Workspace.deleted_at.is_(None),
            )
        )).scalars().all()) if ancestor_ids else []
        for workspace_id in workspace_ids:
            if await user_can_read_workspace_id(
                db,
                workspace_id=str(workspace_id),
                entity_id=entity_id,
                user_id=user_id,
                role=resolved_role,
                can_read_entity_workspaces=can_read_entity_workspaces,
            ):
                return True
        return False

    if visibility in (Visibility.ENTITY, Visibility.PUBLIC):
        return can_read_entity_documents

    return False


async def effective_document_capabilities_for_user(
    db: AsyncSession,
    *,
    document: Document,
    user_id: str | None,
    role: str | None = None,
    _actor: ResolvedUserActor | None = None,
) -> set[str]:
    """Capabilities the current user effectively has on a document.

    This is used by API responses so the frontend can avoid guessing from
    owner_id alone. Entity/document owners are handled here; the route may
    additionally honor an immutable user id stored in legacy ``created_by``.
    """
    if not user_id:
        return set()
    if await document_is_owned_by_deleted_workspace(db, document):
        return set()
    actor = _actor or await _resolve_current_actor_for_entity(
        db, user_id=user_id, entity_id=document.entity_id,
    )
    if actor is None:
        return set()
    resolved_role = actor.role
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
        allow_redacted=False,
        _actor=actor,
    ):
        granted.add(Capability.VIEW)
    return granted


async def user_can_edit_document(
    db: AsyncSession,
    document: Document | None,
    *,
    user: User,
) -> bool:
    """Authoritative edit check for APIs that address a document by path.

    Ownership is based only on immutable user ids.  Mutable identity fields
    such as email and display name are audit labels, never authorization
    credentials.
    """
    if document is None or document.entity_id != user.entity_id:
        return False
    if await document_is_owned_by_deleted_workspace(db, document):
        return False
    actor = await _resolve_current_actor_for_entity(
        db,
        user_id=user.id,
        entity_id=user.entity_id,
    )
    if actor is None:
        return False
    resolved_role = actor.role
    if is_entity_admin_role(resolved_role) or document.owner_id == user.id:
        return True
    # Legacy rows sometimes stored the immutable user id in ``created_by``
    # before ``owner_id`` was introduced.  Email/display-name aliases are
    # intentionally excluded because both can be changed or reused.
    if document.created_by == user.id:
        return True
    capabilities = await effective_document_capabilities_for_user(
        db,
        document=document,
        user_id=user.id,
        role=resolved_role,
    )
    return Capability.EDIT in capabilities


async def user_can_share_document_externally(
    db: AsyncSession,
    document: Document | None,
    *,
    user: User,
) -> bool:
    """Return whether a user may expose a document outside the entity.

    External publication is deliberately stricter than editing. Creator
    aliases retain edit compatibility, but external sharing follows the
    canonical owner/admin or explicit capability contract used by document
    share links.
    """
    if document is None or document.entity_id != user.entity_id:
        return False
    if await document_is_owned_by_deleted_workspace(db, document):
        return False
    actor = await _resolve_current_actor_for_entity(
        db,
        user_id=user.id,
        entity_id=user.entity_id,
    )
    if actor is None:
        return False
    resolved_role = actor.role
    if is_entity_admin_role(resolved_role) or document.owner_id == user.id:
        return True
    return await user_has_document_capability(
        db,
        document=document,
        user_id=user.id,
        capabilities={Capability.SHARE_EXTERNAL, Capability.GRANT_ACCESS},
    )


async def user_can_read_document(
    db: AsyncSession,
    document: Document | None,
    *,
    entity_id: str,
    user_id: str | None = None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
    allow_redacted: bool = True,
    include_grants: bool = True,
    _actor: ResolvedUserActor | None = None,
) -> bool:
    """Resolve visibility; full content requires VIEW, not a metadata/action grant.

    Excluding grants also excludes inherited grants in the folder ceiling, so
    delegation can distinguish implicit access from expiring ACL authority.
    """
    if not document or document.entity_id != entity_id:
        return False

    if getattr(document, "quarantine_status", None) in _QUARANTINED_STATUSES:
        return False
    if getattr(document, "classification", None) == "restricted" and actor_type != "user":
        return False

    if await document_is_owned_by_deleted_workspace(db, document):
        return False

    # Preserve legacy/background callers that do not carry a user context.
    if not user_id:
        return actor_type == "user"

    actor = _actor or await _resolve_current_actor_for_entity(
        db, user_id=user_id, entity_id=entity_id,
    )
    if actor is None:
        return False

    effective_classification, _effective_visibility, effective_client_visible = (
        await effective_document_folder_policy(db, document)
    )
    if effective_classification == "restricted" and actor_type != "user":
        return False

    (
        resolved_role,
        can_read_entity_documents,
        can_read_entity_workspaces,
    ) = (
        await _resolve_user_role_and_document_read(
            db,
            user_id=user_id,
            entity_id=entity_id,
            role=actor.role,
            _actor=actor,
        )
    )
    if resolved_role == "client" and effective_classification in {
        "confidential",
        "restricted",
    }:
        return False
    if is_entity_admin_role(resolved_role):
        return True
    if document.owner_id and document.owner_id == user_id:
        return True
    if include_grants and await _has_read_grant(
        db, document=document, user_id=user_id, allow_redacted=allow_redacted,
    ):
        return True
    if not await user_can_read_folder_path(
        db,
        entity_id=document.entity_id,
        folder_id=document.folder_id,
        user_id=user_id,
        role=resolved_role,
        allow_redacted=allow_redacted,
        include_grants=include_grants,
        _actor=actor,
    ):
        return False

    visibility = getattr(document, "visibility", None) or Visibility.ENTITY
    if visibility == Visibility.PRIVATE:
        return False

    if visibility == Visibility.WORKSPACE:
        if resolved_role == "client" and not effective_client_visible:
            return False
        if resolved_role != "client" and not can_read_entity_documents:
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
                    can_read_entity_workspaces=can_read_entity_workspaces,
                )
            )
        for linked_workspace_id in linked_workspace_ids:
            if await user_can_read_workspace_id(
                db,
                workspace_id=linked_workspace_id,
                entity_id=entity_id,
                user_id=user_id,
                role=resolved_role,
                can_read_entity_workspaces=can_read_entity_workspaces,
            ):
                return True
        return False

    if visibility in (Visibility.ENTITY, Visibility.PUBLIC):
        if resolved_role == "client":
            return effective_client_visible
        if not can_read_entity_documents:
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
            can_read_entity_workspaces=can_read_entity_workspaces,
        )

    return False


async def _workspace_containers_allow_read(
    db: AsyncSession,
    *,
    document: Document,
    entity_id: str,
    user_id: str | None,
    role: str | None,
    can_read_entity_workspaces: bool,
) -> bool:
    """Whether an entity-visible document is reachable through its workspaces.

    Filing a document into a workspace's knowledge net binds it to that
    workspace: the same container narrowing that
    :func:`user_can_read_folder_path` already applies to folders.
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
            can_read_entity_workspaces=can_read_entity_workspaces,
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
        can_read_entity_documents: bool | None = None,
        can_read_entity_workspaces: bool | None = None,
        identity_active: bool = True,
    ) -> None:
        self.entity_id = entity_id
        self.user_id = user_id
        self.role = role
        self.identity_active = identity_active
        self.is_admin = is_entity_admin_role(role)
        self.can_read_entity_documents = (
            str(role or "") in _ENTITY_DOCUMENT_READ_ROLES
            if can_read_entity_documents is None
            else can_read_entity_documents
        )
        self.can_read_entity_workspaces = (
            str(role or "") in ENTITY_WORKSPACE_READ_ROLES
            if can_read_entity_workspaces is None
            else can_read_entity_workspaces
        )
        self._folder_by_id: dict[str, DocumentFolder] = {}
        self._doc_caps: dict[str, set[str]] = {}
        self._folder_caps: dict[str, set[str]] = {}
        self._chain_cache: dict[str, list[str]] = {}
        self._ws_readable: dict[str, bool] = {}
        self._workspace_by_artifact_folder_id: dict[str, str] = {}
        self._deleted_workspace_artifact_folder_ids: set[str] = set()
        self._doc_group_links: dict[str, set[str]] = {}
        self._deleted_provenance_workspace_ids: set[str] = set()

    @classmethod
    async def load(
        cls,
        db: AsyncSession,
        *,
        entity_id: str,
        user_id: str | None = None,
        role: str | None = None,
        folder_ids: set[str] | None = None,
        document_ids: set[str] | None = None,
        _actor: ResolvedUserActor | None = None,
    ) -> "DocumentAccessContext":
        actor = _actor or (
            await _resolve_current_actor_for_entity(
                db,
                user_id=user_id,
                entity_id=entity_id,
            )
            if user_id
            else None
        )
        identity_active = user_id is None or actor is not None
        if identity_active:
            (
                resolved_role,
                can_read_entity_documents,
                can_read_entity_workspaces,
            ) = await _resolve_user_role_and_document_read(
                db,
                user_id=user_id,
                entity_id=entity_id,
                role=actor.role if actor is not None else role,
                _actor=actor,
            )
        else:
            resolved_role = None
            can_read_entity_documents = False
            can_read_entity_workspaces = False
        ctx = cls(
            entity_id=entity_id,
            user_id=user_id,
            role=resolved_role,
            can_read_entity_documents=can_read_entity_documents,
            can_read_entity_workspaces=can_read_entity_workspaces,
            identity_active=identity_active,
        )
        # Every caller loads folder ownership because soft-deleted Workspace
        # trees are a lifecycle hard stop, including for user-less background
        # reads and entity admins. Inactive identities still fail closed before
        # any ACL evaluation and need no grant/folder state.
        if not identity_active:
            return ctx

        subject_ids = (
            set()
            if ctx.is_admin
            else await _grant_subject_ids_for_user(
                db,
                entity_id=entity_id,
                user_id=user_id,
                _actor=actor,
            )
        )
        if folder_ids is None:
            folders = (
                await db.execute(
                    select(DocumentFolder)
                    .where(DocumentFolder.entity_id == entity_id)
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
        else:
            folders = []
            pending = set(folder_ids)
            loaded: set[str] = set()
            while pending:
                batch = set(list(pending)[:500])
                pending.difference_update(batch)
                rows = list((await db.execute(
                    select(DocumentFolder)
                    .where(
                        DocumentFolder.entity_id == entity_id,
                        DocumentFolder.id.in_(batch),
                    )
                    .execution_options(populate_existing=True)
                )).scalars().all())
                folders.extend(rows)
                loaded.update(folder.id for folder in rows)
                pending.update(
                    folder.parent_id
                    for folder in rows
                    if folder.parent_id and folder.parent_id not in loaded
                )
        ctx._folder_by_id = {folder.id: folder for folder in folders}
        if folders:
            from packages.core.models.workspace import Workspace

            workspace_rows = []
            for artifact_folder_ids in _id_batches(set(ctx._folder_by_id)):
                workspace_rows.extend((await db.execute(
                    select(
                        Workspace.id,
                        Workspace.artifact_folder_id,
                        Workspace.deleted_at,
                    ).where(
                        Workspace.entity_id == entity_id,
                        Workspace.artifact_folder_id.in_(artifact_folder_ids),
                    )
                )).all())
            ctx._workspace_by_artifact_folder_id = {
                str(artifact_folder_id): str(workspace_id)
                for workspace_id, artifact_folder_id, deleted_at in workspace_rows
                if artifact_folder_id and deleted_at is None
            }
            ctx._deleted_workspace_artifact_folder_ids = {
                str(artifact_folder_id)
                for _workspace_id, artifact_folder_id, deleted_at in workspace_rows
                if artifact_folder_id and deleted_at is not None
            }

        if subject_ids:
            grant_query = select(ResourceGrant).where(
                ResourceGrant.entity_id == entity_id,
                ResourceGrant.subject_type == SubjectType.USER,
                ResourceGrant.subject_id.in_(subject_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
            if folder_ids is None and document_ids is None:
                grants = (
                    await db.execute(
                        grant_query.where(
                            ResourceGrant.resource_type.in_(
                                [ResourceType.DOCUMENT, ResourceType.DOCUMENT_FOLDER]
                            )
                        ).execution_options(populate_existing=True)
                    )
                ).scalars().all()
            else:
                grants = []
                scoped_resources = (
                    (ResourceType.DOCUMENT_FOLDER, set(ctx._folder_by_id)),
                    (ResourceType.DOCUMENT, set(document_ids or ())),
                )
                for resource_type, resource_ids in scoped_resources:
                    for resource_batch in _id_batches(resource_ids):
                        grants.extend((
                            await db.execute(
                                grant_query.where(
                                    ResourceGrant.resource_type == resource_type,
                                    ResourceGrant.resource_id.in_(resource_batch),
                                ).execution_options(populate_existing=True)
                            )
                        ).scalars().all())
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

    async def can_read_folder(
        self,
        db: AsyncSession,
        folder: DocumentFolder | None,
        *,
        allow_redacted: bool = True,
    ) -> bool:
        """In-memory ``user_can_read_folder``."""
        if not folder or folder.entity_id != self.entity_id:
            return False
        if set(self.folder_chain_ids(folder.id)).intersection(
            self._deleted_workspace_artifact_folder_ids
        ):
            return False
        if not self.user_id:
            return True
        if not self.identity_active:
            return False
        if self.is_admin:
            return True
        if getattr(folder, "owner_id", None) == self.user_id:
            return True
        if self.folder_capabilities(getattr(folder, "id", None)).intersection(
            _FOLDER_READ_CAPABILITIES if allow_redacted else {Capability.VIEW}
        ):
            return True
        visibility = getattr(folder, "visibility", None) or Visibility.ENTITY
        if visibility == Visibility.PRIVATE:
            return False
        if visibility == Visibility.WORKSPACE:
            for chain_id in self.folder_chain_ids(getattr(folder, "id", None)):
                workspace_id = self._workspace_by_artifact_folder_id.get(chain_id)
                if workspace_id and await self._workspace_readable(db, workspace_id):
                    return True
            return False
        if visibility in (Visibility.ENTITY, Visibility.PUBLIC):
            return self.can_read_entity_documents
        return False

    async def folder_path_readable(
        self,
        db: AsyncSession,
        folder_id: str | None,
        *,
        allow_redacted: bool = True,
    ) -> bool:
        """In-memory ``user_can_read_folder_path``."""
        if not self.identity_active:
            return False
        for chain_id in self.folder_chain_ids(folder_id):
            if not await self.can_read_folder(
                db, self._folder_by_id.get(chain_id), allow_redacted=allow_redacted,
            ):
                return False
        return True

    def folder_workspace_ids(self, folder_id: str | None) -> set[str]:
        """Active Workspace owners found along a folder's ancestry."""
        return {
            workspace_id
            for chain_id in self.folder_chain_ids(folder_id)
            if (
                workspace_id
                := self._workspace_by_artifact_folder_id.get(chain_id)
            )
        }

    def effective_document_policy(self, document: Document) -> tuple[str, str, bool]:
        folders = [
            self._folder_by_id[folder_id]
            for folder_id in self.folder_chain_ids(document.folder_id)
            if folder_id in self._folder_by_id
        ]
        return _merge_document_folder_policy(document, folders)

    # ── document rules ───────────────────────────────────────────────────

    async def preload_documents(self, db: AsyncSession, documents: list[Document]) -> None:
        """Load Workspace ownership and group links through bounded queries."""
        if not self.identity_active:
            return
        physical_artifact_folder_ids = {
            artifact_folder_id
            for document in documents
            if (
                artifact_folder_id := _document_physical_artifact_folder_id(document)
            )
        }
        provenance_workspace_ids = {
            workspace_id
            for document in documents
            for workspace_id in _document_metadata_workspace_ids(document)
        }
        if provenance_workspace_ids or physical_artifact_folder_ids:
            from packages.core.models.workspace import Workspace

            workspace_rows_by_id = {}
            for workspace_ids in _id_batches(provenance_workspace_ids):
                rows = (await db.execute(
                    select(
                        Workspace.id,
                        Workspace.artifact_folder_id,
                        Workspace.deleted_at,
                    ).where(
                        Workspace.entity_id == self.entity_id,
                        Workspace.id.in_(workspace_ids),
                    )
                )).all()
                workspace_rows_by_id.update({str(row.id): row for row in rows})
            for artifact_folder_ids in _id_batches(physical_artifact_folder_ids):
                rows = (await db.execute(
                    select(
                        Workspace.id,
                        Workspace.artifact_folder_id,
                        Workspace.deleted_at,
                    ).where(
                        Workspace.entity_id == self.entity_id,
                        Workspace.artifact_folder_id.in_(artifact_folder_ids),
                    )
                )).all()
                workspace_rows_by_id.update({str(row.id): row for row in rows})
            workspace_rows = workspace_rows_by_id.values()
            self._workspace_by_artifact_folder_id.update({
                str(artifact_folder_id): str(workspace_id)
                for workspace_id, artifact_folder_id, deleted_at in workspace_rows
                if artifact_folder_id and deleted_at is None
            })
            self._deleted_provenance_workspace_ids.update(
                str(workspace_id)
                for workspace_id, _artifact_folder_id, deleted_at in workspace_rows
                if str(workspace_id) in provenance_workspace_ids
                and deleted_at is not None
            )
            self._deleted_workspace_artifact_folder_ids.update(
                str(artifact_folder_id)
                for _workspace_id, artifact_folder_id, deleted_at in workspace_rows
                if artifact_folder_id and deleted_at is not None
            )
        if not self.user_id:
            return
        wanted = [
            document.id for document in documents
            if document.id not in self._doc_group_links
        ]
        if not wanted:
            return
        for doc_id in wanted:
            self._doc_group_links[doc_id] = set()
        for document_ids in _id_batches(wanted):
            rows = (
                await db.execute(
                    select(DocumentGroupMember.document_id, DocumentGroup.workspace_id)
                    .join(DocumentGroup, DocumentGroupMember.group_id == DocumentGroup.id)
                    .where(
                        DocumentGroupMember.document_id.in_(document_ids),
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
        workspace_ids.update(_document_metadata_workspace_ids(document))
        physical_artifact_folder_id = _document_physical_artifact_folder_id(document)
        if physical_artifact_folder_id:
            physical_workspace_id = self._workspace_by_artifact_folder_id.get(
                physical_artifact_folder_id
            )
            if physical_workspace_id:
                workspace_ids.add(physical_workspace_id)
        return workspace_ids

    def document_workspace_ids(self, document: Document) -> set[str]:
        """Return the Workspace scope loaded for a Document in this batch."""
        return self._workspace_ids_for(document)

    def _owned_by_deleted_workspace(self, document: Document) -> bool:
        physical_artifact_folder_id = _document_physical_artifact_folder_id(document)
        return bool(
            _document_metadata_workspace_ids(document).intersection(
                self._deleted_provenance_workspace_ids
            )
            or set(self.folder_chain_ids(document.folder_id)).intersection(
                self._deleted_workspace_artifact_folder_ids
            )
            or physical_artifact_folder_id
            in self._deleted_workspace_artifact_folder_ids
        )

    def document_owned_by_deleted_workspace(self, document: Document) -> bool:
        """Return whether the preloaded document belongs to a deleted Workspace."""
        return self._owned_by_deleted_workspace(document)

    async def _workspace_readable(self, db: AsyncSession, workspace_id: str) -> bool:
        if self.user_id and not self.identity_active:
            return False
        cached = self._ws_readable.get(workspace_id)
        if cached is None:
            cached = await user_can_read_workspace_id(
                db,
                workspace_id=workspace_id,
                entity_id=self.entity_id,
                user_id=self.user_id,
                role=self.role,
                can_read_entity_workspaces=self.can_read_entity_workspaces,
            )
            self._ws_readable[workspace_id] = cached
        return cached

    async def workspace_readable(self, db: AsyncSession, workspace_id: str) -> bool:
        """Expose the batch's canonical Workspace visibility decision."""
        return await self._workspace_readable(db, workspace_id)

    async def can_read_document(
        self,
        db: AsyncSession,
        document: Document | None,
        *,
        workspace_id: str | None = None,
        actor_type: str = "user",
        allow_redacted: bool = True,
    ) -> bool:
        """In-memory ``user_can_read_document``. Call ``preload_documents``
        on the batch first, or workspace links fall back to empty."""
        if not document or document.entity_id != self.entity_id:
            return False
        if getattr(document, "quarantine_status", None) in _QUARANTINED_STATUSES:
            return False
        if getattr(document, "classification", None) == "restricted" and actor_type != "user":
            return False
        if self._owned_by_deleted_workspace(document):
            return False
        if not self.user_id:
            return actor_type == "user"
        if not self.identity_active:
            return False
        effective_classification, _effective_visibility, effective_client_visible = (
            self.effective_document_policy(document)
        )
        if effective_classification == "restricted" and actor_type != "user":
            return False
        if self.role == "client" and effective_classification in {
            "confidential",
            "restricted",
        }:
            return False
        if self.is_admin:
            return True
        if document.owner_id and document.owner_id == self.user_id:
            return True
        granted = set(self._doc_caps.get(document.id, set()))
        granted.update(self.folder_capabilities(document.folder_id))
        readable = _DOCUMENT_READ_CAPABILITIES if allow_redacted else {Capability.VIEW}
        if granted.intersection(readable):
            return True
        if not await self.folder_path_readable(
            db, document.folder_id, allow_redacted=allow_redacted,
        ):
            return False

        visibility = getattr(document, "visibility", None) or Visibility.ENTITY
        if visibility == Visibility.PRIVATE:
            return False

        if visibility == Visibility.WORKSPACE:
            if self.role == "client" and not effective_client_visible:
                return False
            if self.role != "client" and not self.can_read_entity_documents:
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
                return effective_client_visible
            if not self.can_read_entity_documents:
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
        if self._owned_by_deleted_workspace(document):
            return set()
        if self.is_admin or document.owner_id == self.user_id:
            return set(_DOCUMENT_OWNER_CAPABILITIES)
        granted = set(self._doc_caps.get(document.id, set()))
        granted.update(self.folder_capabilities(document.folder_id))
        if await self.can_read_document(db, document, allow_redacted=False):
            granted.add(Capability.VIEW)
        return granted

    async def can_read_document_with_capability(
        self,
        db: AsyncSession,
        document: Document | None,
        *,
        required_capability: str,
        workspace_id: str | None = None,
        actor_type: str = "user",
    ) -> bool:
        """Apply one shared read + capability gate to a document mutation."""
        if not await self.can_read_document(
            db,
            document,
            workspace_id=workspace_id,
            actor_type=actor_type,
        ):
            return False
        capabilities = await self.effective_document_capabilities(db, document)
        return required_capability in capabilities


async def partition_documents_by_capability(
    db: AsyncSession,
    documents: list[Document],
    *,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
    required_capability: str,
    workspace_id: str | None = None,
    actor_type: str = "user",
) -> tuple[list[Document], list[Document]]:
    """Partition documents through the batched read + capability evaluator.

    Workspace document attachment has several entry points (API, folder
    snapshot, and agent runtime). They must all use the same capability
    semantics so a read-only document cannot be moved into a narrower
    Workspace audience through a less strict entry point.
    """
    if not documents:
        return [], []
    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )
    scoped_documents = [
        document
        for document in documents
        if document is not None and document.entity_id == entity_id
    ]
    await ctx.preload_documents(db, scoped_documents)
    allowed: list[Document] = []
    denied: list[Document] = []
    for document in documents:
        if document is None or document.entity_id != entity_id:
            denied.append(document)
            continue
        if await ctx.can_read_document_with_capability(
            db,
            document,
            required_capability=required_capability,
            workspace_id=workspace_id,
            actor_type=actor_type,
        ):
            allowed.append(document)
        else:
            denied.append(document)
    return allowed, denied


async def _document_folders_by_paths(
    db: AsyncSession,
    *,
    entity_id: str,
    paths: set[str],
) -> dict[str, DocumentFolder]:
    """Resolve exact logical folder paths without loading the entity tree."""
    path_parts = {
        path: tuple(part for part in path.split("/") if part)
        for path in paths
        if path
    }
    if not path_parts:
        return {}

    folder_by_prefix: dict[tuple[str, ...], DocumentFolder] = {}
    max_depth = max(len(parts) for parts in path_parts.values())
    for depth in range(max_depth):
        needed: dict[tuple[str | None, str], tuple[str, ...]] = {}
        for parts in path_parts.values():
            if len(parts) <= depth:
                continue
            parent_prefix = parts[:depth]
            if depth and parent_prefix not in folder_by_prefix:
                continue
            parent_id = (
                folder_by_prefix[parent_prefix].id if parent_prefix else None
            )
            needed[(parent_id, parts[depth])] = parts[: depth + 1]
        pairs = list(needed)
        for start in range(0, len(pairs), 200):
            batch = pairs[start:start + 200]
            root_names = [name for parent_id, name in batch if parent_id is None]
            child_pairs = [pair for pair in batch if pair[0] is not None]
            filters = []
            if root_names:
                filters.append(and_(
                    DocumentFolder.parent_id.is_(None),
                    DocumentFolder.name.in_(root_names),
                ))
            if child_pairs:
                filters.append(
                    tuple_(DocumentFolder.parent_id, DocumentFolder.name).in_(child_pairs)
                )
            if not filters:
                continue
            rows = (await db.execute(
                select(DocumentFolder).where(
                    DocumentFolder.entity_id == entity_id,
                    or_(*filters),
                )
            )).scalars().all()
            for folder in rows:
                prefix = needed.get((folder.parent_id, str(folder.name)))
                if prefix is not None:
                    folder_by_prefix[prefix] = folder

    return {
        path: folder_by_prefix[parts]
        for path, parts in path_parts.items()
        if parts in folder_by_prefix
    }


async def unreadable_document_paths(
    db: AsyncSession,
    *,
    entity_id: str,
    rel_paths: "list[str]",
    directory_paths: "list[str] | None" = None,
    user_id: str | None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
    _actor: ResolvedUserActor | None = None,
) -> set[str]:
    """Return entity-root-relative paths the actor may not read.

    Knowledge documents live as real files under the entity FS root, so raw
    filesystem tools (agent ``read_file`` / ``list_files`` / ``grep`` / the
    ``/fs`` router) would otherwise serve a document's bytes without consulting
    ``Document.visibility``. This maps each path to its ``Document`` row and
    gates it through :func:`user_can_read_document`.

    This is deliberately fail-closed for authenticated users and agents:
    unprojected bytes are not a side door around Knowledge ACLs. A path is
    readable only when it is an authorized active Document, belongs to an
    authorized Workspace artifact store, or resolves to an authorized
    DocumentFolder. User-less legacy system callers remain supported only when
    ``actor_type`` is ``user``; user-less agents are denied.
    """
    from packages.core.models.workspace import Workspace
    from packages.core.services.knowledge_visibility import normalize_rel_path
    from packages.core.services.workspace_artifacts import (
        artifact_folder_id_from_entity_storage_path,
    )

    wanted = {normalize_rel_path(p) for p in rel_paths if p}
    wanted_dirs = {normalize_rel_path(p) for p in (directory_paths or []) if p}
    current_workspace_id = str(workspace_id or "").strip() or None
    all_wanted = wanted | wanted_dirs
    if not all_wanted:
        return set()
    if not user_id and actor_type != "user":
        return all_wanted

    rows = list((
        await db.execute(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path.in_(wanted),
                Document.is_trashed == False,  # noqa: E712
            )
        )
    ).scalars().all())
    folders_by_path = await _document_folders_by_paths(
        db,
        entity_id=entity_id,
        paths=wanted_dirs,
    )
    relevant_folder_ids = {
        str(folder_id)
        for folder_id in (
            *(document.folder_id for document in rows),
            *(folder.id for folder in folders_by_path.values()),
        )
        if folder_id
    }
    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
        folder_ids=relevant_folder_ids,
        document_ids={str(document.id) for document in rows},
        _actor=_actor,
    )
    await ctx.preload_documents(db, rows)

    blocked: set[str] = set()
    documents_by_path: dict[str, list[Document]] = {}
    for document in rows:
        documents_by_path.setdefault(normalize_rel_path(document.fs_path), []).append(document)

    artifact_folder_ids = {
        folder_id
        for path in all_wanted
        if (folder_id := artifact_folder_id_from_entity_storage_path(path))
    }
    workspace_rows = (await db.execute(
        select(Workspace.id, Workspace.artifact_folder_id, Workspace.deleted_at).where(
            Workspace.entity_id == entity_id,
            Workspace.artifact_folder_id.in_(artifact_folder_ids),
        )
    )).all() if artifact_folder_ids else []
    workspace_by_artifact_folder_id = {
        str(artifact_folder_id): str(workspace_id)
        for workspace_id, artifact_folder_id, deleted_at in workspace_rows
        if artifact_folder_id and deleted_at is None
    }
    deleted_workspace_artifact_folder_ids = {
        str(artifact_folder_id)
        for _workspace_id, artifact_folder_id, deleted_at in workspace_rows
        if artifact_folder_id and deleted_at is not None
    }

    def _path_has_deleted_workspace_owner(
        path: str,
        documents: list[Document],
        folder: DocumentFolder | None,
    ) -> bool:
        artifact_folder_id = artifact_folder_id_from_entity_storage_path(path)
        return bool(
            artifact_folder_id in deleted_workspace_artifact_folder_ids
            or any(ctx._owned_by_deleted_workspace(document) for document in documents)
            or folder is not None
            and set(ctx.folder_chain_ids(folder.id)).intersection(
                ctx._deleted_workspace_artifact_folder_ids
            )
        )

    if not user_id:
        return {
            path
            for path in all_wanted
            if _path_has_deleted_workspace_owner(
                path,
                documents_by_path.get(path, []),
                folders_by_path.get(path),
            )
        }

    async def _workspace_scope_allows_path(
        path: str,
        documents: list[Document],
        folder: DocumentFolder | None,
    ) -> bool:
        if not current_workspace_id:
            return True
        if not await ctx._workspace_readable(db, current_workspace_id):
            return False

        artifact_folder_id = artifact_folder_id_from_entity_storage_path(path)
        if artifact_folder_id:
            if (
                workspace_by_artifact_folder_id.get(artifact_folder_id)
                != current_workspace_id
            ):
                return False

        linked_workspace_ids: set[str] = set()
        for document in documents:
            linked_workspace_ids.update(ctx._workspace_ids_for(document))
            for folder_id in ctx.folder_chain_ids(document.folder_id):
                linked_workspace_id = ctx._workspace_by_artifact_folder_id.get(folder_id)
                if linked_workspace_id:
                    linked_workspace_ids.add(linked_workspace_id)
        if folder is not None:
            for folder_id in ctx.folder_chain_ids(folder.id):
                linked_workspace_id = ctx._workspace_by_artifact_folder_id.get(folder_id)
                if linked_workspace_id:
                    linked_workspace_ids.add(linked_workspace_id)
        return not linked_workspace_ids or current_workspace_id in linked_workspace_ids

    for path in all_wanted:
        documents = documents_by_path.get(path, [])
        folder = folders_by_path.get(path)
        if _path_has_deleted_workspace_owner(path, documents, folder):
            blocked.add(path)
            continue
        if not await _workspace_scope_allows_path(path, documents, folder):
            blocked.add(path)
            continue
        if documents:
            for document in documents:
                if not await ctx.can_read_document(
                    db,
                    document,
                    workspace_id=current_workspace_id,
                    actor_type=actor_type,
                    allow_redacted=False,
                ):
                    blocked.add(path)
                    break
            continue

        # Entity admins may traverse legacy/unprojected entity files, but a
        # projected Document must still pass the actor-specific hard stops
        # above. In particular, an admin-backed agent is not a side door
        # around Restricted or quarantined Knowledge content.
        if ctx.is_admin:
            continue

        artifact_folder_id = artifact_folder_id_from_entity_storage_path(path)
        if artifact_folder_id:
            workspace_id = workspace_by_artifact_folder_id.get(artifact_folder_id)
            if not workspace_id or not await ctx._workspace_readable(db, workspace_id):
                blocked.add(path)
            continue

        if path not in wanted_dirs:
            # A readable logical folder authorizes traversing/listing that
            # directory, not arbitrary unprojected bytes found underneath it.
            blocked.add(path)
            continue
        if folder is None or not await ctx.folder_path_readable(db, folder.id):
            blocked.add(path)
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
    if getattr(document, "quarantine_status", None) in _QUARANTINED_STATUSES:
        return False
    if await document_is_owned_by_deleted_workspace(db, document):
        return False
    classification, visibility, client_visible = await effective_document_folder_policy(
        db, document
    )
    if not client_visible:
        return False
    if classification in {"confidential", "restricted"}:
        return False

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


async def public_agent_visible_document_ids_batched(
    db: AsyncSession,
    documents: list[Document],
    *,
    entity_id: str,
    workspace_id: str | None,
) -> set[str]:
    """Return public-agent-visible document ids with bounded policy queries."""

    workspace = str(workspace_id or "").strip()
    candidates = [
        document
        for document in documents
        if document is not None and document.entity_id == entity_id
    ]
    if not workspace or not candidates:
        return set()

    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        folder_ids={
            str(folder_id)
            for document in candidates
            if (folder_id := getattr(document, "folder_id", None))
        },
        document_ids={str(document.id) for document in candidates},
    )
    await ctx.preload_documents(db, candidates)
    workspace_ids_by_document = await document_workspace_ids_batched(db, candidates)

    visible: set[str] = set()
    for document in candidates:
        if getattr(document, "quarantine_status", None) in _QUARANTINED_STATUSES:
            continue
        if ctx.document_owned_by_deleted_workspace(document):
            continue
        classification, visibility, client_visible = ctx.effective_document_policy(document)
        if not client_visible or classification in {"confidential", "restricted"}:
            continue
        if visibility not in {Visibility.WORKSPACE, Visibility.ENTITY, Visibility.PUBLIC}:
            continue
        if workspace not in workspace_ids_by_document.get(str(document.id), set()):
            continue
        visible.add(str(document.id))
    return visible


async def get_visible_document(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    user_id: str | None = None,
    role: str | None = None,
    workspace_id: str | None = None,
    actor_type: str = "user",
    allow_redacted: bool = False,
) -> Document | None:
    """Load a full-content-readable document; metadata-only callers opt in to redacted access."""
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
        allow_redacted=allow_redacted,
    ):
        return document
    return None


async def _document_access_context_for_batch(
    db: AsyncSession,
    *,
    entity_id: str,
    documents: list[Document],
    user_id: str | None,
    role: str | None,
) -> DocumentAccessContext:
    """Load only the folder closure and direct grants needed by one batch."""
    ctx = await DocumentAccessContext.load(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
        folder_ids={
            str(folder_id)
            for document in documents
            if (folder_id := getattr(document, "folder_id", None))
        },
        document_ids={str(document.id) for document in documents},
    )
    await ctx.preload_documents(db, documents)
    return ctx


async def list_visible_document_ids_batched(
    db: AsyncSession,
    entity_id: str,
    *,
    folder_ids: set[str],
    user_id: str | None = None,
    role: str | None = None,
    actor_type: str = "user",
    required_capability: str | None = None,
    batch_size: int = 500,
) -> list[str]:
    """List all visible document ids in a folder set with bounded queries.

    Folder imports need a complete snapshot, but loading every ``Document``
    ORM object (and one giant ``IN`` clause) is unsafe for large Knowledge
    trees. This keeps both folder parameters and document batches bounded
    while reusing the same batched permission evaluator as normal listings.
    When ``required_capability`` is set, a visible document without that
    capability raises ``PermissionError`` before the caller begins writing.
    """
    from packages.core.services.document_service import list_documents

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    ordered_folder_ids = sorted(set(folder_ids))
    if not ordered_folder_ids:
        return []

    visible_ids: list[str] = []
    for folder_start in range(0, len(ordered_folder_ids), batch_size):
        folder_batch = set(
            ordered_folder_ids[folder_start : folder_start + batch_size]
        )
        offset = 0
        while True:
            candidates, raw_total = await list_documents(
                db,
                entity_id,
                folder_ids=folder_batch,
                limit=batch_size,
                offset=offset,
            )
            if not candidates:
                break
            raw_batch_count = len(candidates)
            candidates = await _filter_readable_local_documents(
                db,
                candidates,
                stat_files=False,
            )
            ctx = await _document_access_context_for_batch(
                db,
                entity_id=entity_id,
                documents=candidates,
                user_id=user_id,
                role=role,
            )
            for document in candidates:
                if await ctx.can_read_document(
                    db,
                    document,
                    actor_type=actor_type,
                ):
                    if required_capability and not await ctx.can_read_document_with_capability(
                        db,
                        document,
                        required_capability=required_capability,
                        actor_type=actor_type,
                    ):
                        raise PermissionError(
                            "A visible document lacks the required capability"
                        )
                    visible_ids.append(document.id)
            offset += raw_batch_count
            if offset >= raw_total:
                break
    return visible_ids


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

    if not user_id and actor_type != "user":
        return [], 0

    visible_page: list[Document] = []
    visible_total = 0
    raw_offset = 0
    while True:
        candidates, raw_total = await list_documents(
            db,
            entity_id,
            name_search=name_search,
            folder_id=folder_id,
            folder_ids=folder_ids,
            workspace_id=workspace_id,
            include_generated_assets=include_generated_assets,
            limit=_INTERNAL_FILTER_LIMIT,
            offset=raw_offset,
        )
        if not candidates:
            break
        raw_batch_count = len(candidates)
        # Keep permission filtering independent from storage availability.
        # Content/download endpoints validate bytes; listings use DB state.
        candidates = await _filter_readable_local_documents(
            db,
            candidates,
            stat_files=False,
        )
        ctx = await _document_access_context_for_batch(
            db,
            entity_id=entity_id,
            documents=candidates,
            user_id=user_id,
            role=role,
        )
        for document in candidates:
            if not await ctx.can_read_document(
                db,
                document,
                workspace_id=workspace_id,
                actor_type=actor_type,
            ):
                continue
            if visible_total >= offset and (
                limit is None or len(visible_page) < limit
            ):
                visible_page.append(document)
            visible_total += 1
        raw_offset += raw_batch_count
        if raw_offset >= raw_total:
            break
    return visible_page, visible_total


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
        docs = await _filter_readable_local_documents(db, docs, stat_files=False)
        ctx = await _document_access_context_for_batch(
            db,
            entity_id=entity_id,
            documents=docs,
            user_id=user_id,
            role=role,
        )
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
    authorize_batch: Callable[[list[Document]], Awaitable[list[Document]]] | None = None,
) -> dict[str, int]:
    """Return direct visible counts, scanning folders and Documents in batches."""
    from packages.core.services.document_service import list_documents

    if not folder_ids:
        return {}
    counts: dict[str, int] = {}
    counted_document_ids: set[str] = set()
    batch_size = _INTERNAL_FILTER_LIMIT
    for folder_batch in _id_batches(folder_ids):
        offset = 0
        while True:
            docs, raw_total = await list_documents(
                db,
                entity_id,
                folder_ids=folder_batch,
                limit=batch_size,
                offset=offset,
            )
            if not docs:
                break
            raw_batch_count = len(docs)
            docs = await _filter_readable_local_documents(db, docs, stat_files=False)
            if authorize_batch is not None:
                readable_documents = await authorize_batch(docs)
            else:
                ctx = await _document_access_context_for_batch(
                    db,
                    entity_id=entity_id,
                    documents=docs,
                    user_id=user_id,
                    role=role,
                )
                readable_documents = []
                for document in docs:
                    if await ctx.can_read_document(
                        db,
                        document,
                        workspace_id=workspace_id,
                        actor_type=actor_type,
                    ):
                        readable_documents.append(document)
            for document in readable_documents:
                folder_id = getattr(document, "folder_id", None)
                document_id = str(document.id)
                if (
                    document_id not in counted_document_ids
                    and folder_id
                    and str(folder_id) in folder_batch
                ):
                    counts[str(folder_id)] = counts.get(str(folder_id), 0) + 1
                    counted_document_ids.add(document_id)
            offset += raw_batch_count
            if offset >= raw_total:
                break
    return counts
