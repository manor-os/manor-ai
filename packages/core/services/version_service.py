"""Version and trash services for documents."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.models.document_version import DocumentVersion
from packages.core.services.document_service import get_document


_TRASH_META_KEY = "original_fs_path_before_trash"


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
    return os.path.join(directory, f"{stem}_{int(time.time())}{ext}")


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

async def create_version(
    db: AsyncSession,
    document_id: str,
    entity_id: str,
    *,
    change_summary: str | None = None,
    created_by: str | None = None,
) -> DocumentVersion:
    """Create a version snapshot of the current document state."""
    doc = await get_document(db, document_id, entity_id)
    if not doc:
        raise ValueError("Document not found")

    # Get next version number
    result = await db.execute(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0))
        .where(DocumentVersion.document_id == document_id)
    )
    next_version = result.scalar() + 1

    version = DocumentVersion(
        id=generate_ulid(),
        document_id=document_id,
        version_number=next_version,
        name=doc.name,
        fs_path=doc.fs_path,
        file_size=doc.file_size,
        change_summary=change_summary or f"Version {next_version}",
        created_by=created_by,
    )
    db.add(version)
    await db.flush()
    return version


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
    db: AsyncSession, document_id: str, entity_id: str, trashed_by: str | None = None,
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
    await bump_tool_cache_version(entity_id, "documents")
    return True


async def restore_document(
    db: AsyncSession, document_id: str, entity_id: str,
) -> bool:
    """Restore document from trash."""
    # Query including trashed docs (get_document filters them out)
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.entity_id == entity_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc or not doc.is_trashed:
        return False
    _restore_file_from_hidden_trash(doc, entity_id)
    doc.is_trashed = False
    doc.trashed_at = None
    doc.trashed_by = None
    await db.flush()
    await bump_tool_cache_version(entity_id, "documents")
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
    """Permanently delete all trashed documents and their files. Returns count deleted."""
    import os
    from packages.core.config import get_settings

    # Collect fs_paths before deleting records so we can clean up files
    trashed = await db.execute(
        select(Document.fs_path).where(
            Document.entity_id == entity_id,
            Document.is_trashed == True,  # noqa: E712
        )
    )
    fs_paths = [row[0] for row in trashed if row[0]]

    result = await db.execute(
        delete(Document).where(
            Document.entity_id == entity_id,
            Document.is_trashed == True,  # noqa: E712
        )
    )
    await db.flush()
    if result.rowcount:
        await bump_tool_cache_version(entity_id, "documents")

    # Delete files from disk
    settings = get_settings()
    if settings.MANOR_FS_ENABLED and fs_paths:
        entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
        for fp in fs_paths:
            full = os.path.join(entity_root, fp)
            try:
                if os.path.isfile(full):
                    os.remove(full)
            except OSError:
                pass  # best-effort cleanup

    return result.rowcount
