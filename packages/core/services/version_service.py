"""Version and trash services for documents."""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.document_version import DocumentVersion
from packages.core.services.document_service import get_document, get_document_for_update
from packages.core.services.comment_service import delete_resource_comments


_TRASH_META_KEY = "original_fs_path_before_trash"


class DocumentRestoreConflict(ValueError):
    """Raised when a trashed projection cannot be restored safely."""


def _entity_root(entity_id: str) -> str | None:
    from packages.core.config import get_settings

    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        return None
    return os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))


def _safe_full_path(root: str, rel_path: str | None) -> str | None:
    if not rel_path:
        return None
    full = os.path.realpath(os.path.join(root, rel_path))
    if os.path.commonpath([root, full]) != root:
        return None
    return full


def _dedupe_dest(path: str) -> str:
    if not os.path.exists(path):
        return path
    directory = os.path.dirname(path)
    stem, ext = os.path.splitext(os.path.basename(path))
    timestamp = int(time.time())
    candidate = os.path.join(directory, f"{stem}_{timestamp}{ext}")
    sequence = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{timestamp}_{sequence}{ext}")
        sequence += 1
    return candidate


def _move_file_to_hidden_trash(doc: Document, entity_id: str) -> None:
    root = _entity_root(entity_id)
    if not root or not doc.fs_path:
        return
    src = _safe_full_path(root, doc.fs_path)
    if not src or not os.path.isfile(src):
        return

    original_rel = doc.fs_path
    trash_rel = os.path.join(".trash", "documents", doc.id, os.path.basename(original_rel))
    dst = _safe_full_path(root, trash_rel)
    if not dst:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)

    meta = dict(doc.metadata_ or {})
    meta.setdefault(_TRASH_META_KEY, original_rel)
    doc.metadata_ = meta
    doc.fs_path = trash_rel


def _restore_file_from_hidden_trash(doc: Document, entity_id: str) -> None:
    root = _entity_root(entity_id)
    meta = dict(doc.metadata_ or {})
    original_rel = meta.get(_TRASH_META_KEY)
    if not root or not doc.fs_path or not original_rel:
        return

    src = _safe_full_path(root, doc.fs_path)
    dst = _safe_full_path(root, original_rel)
    if not src or not dst or not os.path.isfile(src):
        return

    dst = _dedupe_dest(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    doc.fs_path = os.path.relpath(dst, root)
    meta.pop(_TRASH_META_KEY, None)
    doc.metadata_ = meta


# ── Versioning ──

def _snapshot_file(entity_id: str, source_path: str, snapshot_path: str) -> tuple[int, str]:
    from packages.core.services.entity_fs import (
        EntityFileWriteError,
        copy_entity_file_atomic,
        open_entity_file_snapshot,
        unlink_entity_file_entry,
    )

    def require_new(_rel_path: str, exists: bool, _full_path: str) -> None:
        if exists:
            raise EntityFileWriteError("Document version snapshot already exists")

    created = False
    try:
        with open_entity_file_snapshot(entity_id, source_path) as source:
            copy_entity_file_atomic(
                entity_id,
                snapshot_path,
                source.descriptor_path,
                source_fd=source.file_descriptor,
                expected_size=source.stat.st_size,
                allow_empty=True,
                write_precondition=require_new,
            )
            created = True
        with open_entity_file_snapshot(entity_id, snapshot_path) as snapshot:
            digest = hashlib.sha256()
            with open(snapshot.descriptor_path, "rb") as stored:
                for chunk in iter(lambda: stored.read(1024 * 1024), b""):
                    digest.update(chunk)
            return snapshot.stat.st_size, digest.hexdigest()
    except Exception:
        if created:
            unlink_entity_file_entry(entity_id, snapshot_path)
        raise


async def _version_workspace_id(db: AsyncSession, doc: Document, entity_id: str) -> str | None:
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import (
        artifact_folder_id_from_entity_storage_path,
        resolve_workspace_folder_binding,
    )

    origin = (doc.metadata_ or {}).get("origin")
    candidates = set()
    if isinstance(origin, dict) and origin.get("workspace_id"):
        candidates.add(str(origin["workspace_id"]))
    if doc.folder_id:
        binding = await resolve_workspace_folder_binding(
            db, entity_id=entity_id, folder_id=doc.folder_id,
        )
        if binding:
            candidates.add(binding.workspace_id)
    artifact_folder_id = artifact_folder_id_from_entity_storage_path(doc.fs_path)
    if artifact_folder_id:
        path_workspace_id = await db.scalar(
            select(Workspace.id).where(
                Workspace.entity_id == entity_id,
                Workspace.artifact_folder_id == artifact_folder_id,
                Workspace.deleted_at.is_(None),
            )
        )
        if path_workspace_id:
            candidates.add(path_workspace_id)
    return next(iter(candidates)) if len(candidates) == 1 else None


async def _create_version(
    db: AsyncSession,
    document_id: str,
    entity_id: str,
    change_summary: str | None,
    created_by: str | None,
) -> DocumentVersion:
    doc = await get_document(db, document_id, entity_id)
    if not doc:
        raise ValueError("Document not found")

    result = await db.execute(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0))
        .where(DocumentVersion.document_id == document_id)
    )
    next_version = result.scalar() + 1
    version_id = generate_ulid()
    snapshot_path = None
    file_size = doc.file_size
    content_sha256 = None
    if doc.fs_path:
        if _entity_root(entity_id) is None:
            raise ValueError("Document filesystem is not enabled")
        snapshot_path = f".versions/documents/{document_id}/{version_id}"
        file_size, content_sha256 = await asyncio.to_thread(
            _snapshot_file, entity_id, doc.fs_path, snapshot_path,
        )

    version = DocumentVersion(
        id=version_id,
        document_id=document_id,
        version_number=next_version,
        name=doc.name,
        fs_path=snapshot_path,
        file_size=file_size,
        snapshot_key=version_id if snapshot_path else None,
        content_sha256=content_sha256,
        workspace_id=await _version_workspace_id(db, doc, entity_id),
        change_summary=change_summary or f"Version {next_version}",
        created_by=created_by,
    )
    db.add(version)
    try:
        await db.flush()
    except Exception:
        if snapshot_path:
            from packages.core.services.entity_fs import unlink_entity_file_entry

            await asyncio.to_thread(unlink_entity_file_entry, entity_id, snapshot_path)
        raise
    return version

async def create_version(
    db: AsyncSession,
    document_id: str,
    entity_id: str,
    *,
    change_summary: str | None = None,
    created_by: str | None = None,
    filesystem_lock_held: bool = False,
) -> DocumentVersion:
    """Create a version snapshot of the current document state."""
    root = _entity_root(entity_id)
    if root and not filesystem_lock_held:
        from packages.core.services.entity_fs import entity_filesystem_mutation_lock

        async with entity_filesystem_mutation_lock(root):
            return await _create_version(db, document_id, entity_id, change_summary, created_by)
    return await _create_version(db, document_id, entity_id, change_summary, created_by)


async def list_versions(
    db: AsyncSession, document_id: str, entity_id: str,
) -> list[DocumentVersion]:
    """List all versions of a document, newest first."""
    # Verify the document belongs to this entity
    doc = await get_document(db, document_id, entity_id)
    if not doc:
        return []
    result = await db.execute(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
    )
    return list(result.scalars().all())


async def get_version(db: AsyncSession, version_id: str) -> DocumentVersion | None:
    """Get a single version by ID."""
    result = await db.execute(
        select(DocumentVersion).where(DocumentVersion.id == version_id)
    )
    return result.scalar_one_or_none()


# ── Trash ──

async def trash_document(
    db: AsyncSession,
    document_id: str,
    entity_id: str,
    trashed_by: str | None = None,
) -> bool:
    """Move document to trash (soft delete)."""
    doc = await get_document(db, document_id, entity_id)
    if not doc:
        return False
    _move_file_to_hidden_trash(doc, entity_id)
    doc.is_trashed = True
    doc.trashed_at = datetime.now(timezone.utc)
    doc.trashed_by = trashed_by
    await db.flush()
    return True


async def restore_document(
    db: AsyncSession,
    document_id: str,
    entity_id: str,
) -> bool:
    """Restore document from trash."""
    doc = await get_document_for_update(
        db,
        document_id,
        entity_id,
        include_trashed=True,
    )
    if not doc:
        return False
    restore_blocked_reason = dict(doc.metadata_ or {}).get("restore_blocked_reason")
    if restore_blocked_reason:
        raise DocumentRestoreConflict("This trashed document has no independently recoverable file")
    original_fs_path = dict(doc.metadata_ or {}).get(_TRASH_META_KEY)
    active_fs_path = original_fs_path or doc.fs_path
    if active_fs_path:
        active_document_id = await db.scalar(
            select(Document.id).where(
                Document.entity_id == entity_id,
                Document.fs_path == active_fs_path,
                Document.is_trashed.is_(False),
                Document.id != doc.id,
            )
        )
        if active_document_id is not None:
            raise DocumentRestoreConflict("A current document already uses this filesystem path")
    _restore_file_from_hidden_trash(doc, entity_id)
    doc.is_trashed = False
    doc.trashed_at = None
    doc.trashed_by = None
    await db.flush()
    return True


async def list_trash(db: AsyncSession, entity_id: str) -> list[Document]:
    """List all trashed documents for an entity."""
    result = await db.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.is_trashed == True,  # noqa: E712
        ).order_by(Document.trashed_at.desc())
    )
    return list(result.scalars().all())


async def empty_trash(db: AsyncSession, entity_id: str) -> int:
    """Permanently delete trashed rows, then clean their files."""
    candidate_ids = list((await db.execute(
        select(Document.id).where(
            Document.entity_id == entity_id,
            Document.is_trashed.is_(True),
        ).order_by(Document.id)
    )).scalars())
    document_ids = []
    for document_id in candidate_ids:
        document = await get_document_for_update(
            db,
            document_id,
            entity_id,
            include_trashed=True,
        )
        if document is not None:
            document_ids.append(document.id)
    if not document_ids:
        return 0

    # Collect fs_paths before deleting records so we can clean up files
    trashed = await db.execute(
        select(Document.id, Document.fs_path)
        .where(
            Document.entity_id == entity_id,
            Document.id.in_(document_ids),
        )
        .order_by(Document.id)
        .with_for_update()
    )
    trashed_rows = list(trashed)
    fs_paths = [row[1] for row in trashed_rows if row[1]]
    active_fs_paths: list[str] = []
    if fs_paths:
        active_paths = await db.execute(
            select(Document.fs_path).where(
                Document.entity_id == entity_id,
                Document.is_trashed.is_(False),
                Document.fs_path.is_not(None),
            )
        )
        active_fs_paths = [row[0] for row in active_paths if row[0]]

    entity_root = _entity_root(entity_id)
    source_cleanup_bases: set[str] = set()
    if entity_root:
        protected_files = {
            full
            for fp in active_fs_paths
            if (full := _safe_full_path(entity_root, fp)) is not None
        }
        source_cleanup_bases = {
            fp
            for fp in fs_paths
            if (
                (full := _safe_full_path(entity_root, fp)) is not None
                and full not in protected_files
            )
        }
    else:
        source_cleanup_bases = set(fs_paths) - set(active_fs_paths)

    await delete_resource_comments(db, entity_id, "document", document_ids)
    await db.execute(
        delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids))
    )
    result = await db.execute(
        delete(Document).where(
            Document.entity_id == entity_id,
            Document.id.in_(document_ids),
        )
    )
    from packages.core.services.workspace_artifact_purge import (
        ARTIFACT_CLEANUP_KIND_FILE,
        document_derived_file_cleanup_bases,
        document_derived_tree_cleanup_bases,
        document_version_snapshot_cleanup_bases,
        enqueue_artifact_cleanup_jobs,
    )

    cleanup_bases = (
        document_derived_tree_cleanup_bases(document_ids)
        | document_version_snapshot_cleanup_bases(document_ids)
    )
    file_cleanup_bases = (
        source_cleanup_bases
        | document_derived_file_cleanup_bases(document_ids)
    )
    await enqueue_artifact_cleanup_jobs(db, entity_id, cleanup_bases)
    await enqueue_artifact_cleanup_jobs(
        db,
        entity_id,
        file_cleanup_bases,
        target_kind=ARTIFACT_CLEANUP_KIND_FILE,
    )
    await db.flush()

    # The file cleanup is irreversible.  Make the database deletion durable
    # first so a failed commit can never leave live trash rows pointing at
    # files that were already removed.
    await db.commit()

    return result.rowcount
