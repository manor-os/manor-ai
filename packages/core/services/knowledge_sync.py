"""Synchronize user-visible filesystem changes into the Knowledge document index.

Filesystem remains the source for files/folders. The documents tables are the
user-facing Knowledge projection and are updated only for paths allowed by the
visibility policy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func as sa_func, select, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from packages.core.database import async_session
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder, VectorStatus
from packages.core.models.workspace import Workspace
from packages.core.services.document_service import StorageLimitExceeded, upsert_document_by_fs_path
from packages.core.services.file_type_detection import detect_file_type
from packages.core.services.knowledge_visibility import (
    is_storage_only_path,
    is_user_visible_folder_path,
    is_user_visible_path,
    normalize_rel_path,
)
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.tool_cache_version import bump_tool_cache_version

_AUTO_CREATE_FOLDER_SOURCES = {
    "manual",
    "upload",
    "startup_backfill",
    "ai_generated",
    "agent",
    "bash",
    "filesystem_reconcile",
}
_FINAL_ARTIFACT_SOURCES = {"ai_generated", "sandbox", "bash", "agent", "mcp", "elevenlabs"}


@dataclass(frozen=True)
class KnowledgeSyncResult:
    """Result of projecting a filesystem path into Knowledge."""

    synced: bool
    document_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class KnowledgeReconcileResult:
    """Summary from reconciling Knowledge documents against real filesystem."""

    scanned_files: int = 0
    synced_files: int = 0
    checked_documents: int = 0
    trashed_missing_documents: int = 0
    limited: bool = False


async def sync_file_to_knowledge(
    *,
    entity_id: str,
    abs_path: str,
    entity_root: str,
    source: str = "manual",
    created_by: str | None = None,
    force: bool | None = None,
    folder_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    tool_name: str | None = None,
) -> KnowledgeSyncResult:
    """Create/update a Document row for a visible filesystem file.

    ``force`` is an intent flag, not a permission override: hidden/system paths
    never sync even when force=True.
    """
    rel_path = normalize_rel_path(os.path.relpath(abs_path, entity_root))
    visible = is_user_visible_path(rel_path)
    should_sync = visible if force is None else bool(force) and visible
    if not should_sync:
        return KnowledgeSyncResult(False, reason="hidden" if not visible else "disabled")
    if not os.path.isfile(abs_path):
        return KnowledgeSyncResult(False, reason="not_file")

    stat = os.stat(abs_path)
    size = stat.st_size
    detected = detect_file_type(abs_path, declared_name=os.path.basename(rel_path))
    ext = detected.extension
    mime_type = detected.mime_type
    resolved_folder_id = folder_id
    if resolved_folder_id is None:
        rel_dir = os.path.dirname(rel_path)
        if rel_dir and is_user_visible_folder_path(rel_dir):
            if source in _AUTO_CREATE_FOLDER_SOURCES:
                resolved_folder_id = await ensure_folder_path(entity_id, rel_dir)
            else:
                resolved_folder_id = await find_folder_path(entity_id, rel_dir)

    # Reconcile re-projects files already on disk, so it must never be blocked
    # by the storage quota; every other source counts as adding to the KB.
    skip_storage_check = source == "filesystem_reconcile"
    async with async_session() as db:
        try:
            doc = await upsert_document_by_fs_path(
                db,
                entity_id,
                fs_path=rel_path,
                name=detected.display_name,
                file_size=size,
                file_type=ext,
                mime_type=mime_type,
                source=source,
                created_by=created_by,
                folder_id=resolved_folder_id,
                skip_storage_check=skip_storage_check,
            )
        except StorageLimitExceeded:
            # Over the plan limit: the file stays on disk but is not added to the
            # knowledge index. Callers (e.g. the generate_file tool) surface this.
            return KnowledgeSyncResult(False, reason="storage_limit")
        doc.metadata_ = _with_file_integrity(
            doc.metadata_,
            status="ok",
            mtime_ns=getattr(stat, "st_mtime_ns", None),
        )
        if resolved_folder_id is None and is_storage_only_path(rel_path):
            doc.folder_id = None
        if source in _FINAL_ARTIFACT_SOURCES or is_storage_only_path(rel_path):
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                artifact={
                    "role": "final",
                    "storage_scope": "artifact" if is_storage_only_path(rel_path) else None,
                },
            )
        if detected.mismatch:
            meta = dict(doc.metadata_ or {})
            meta["detected_file_type_mismatch"] = True
            meta["stored_filename"] = os.path.basename(rel_path)
            doc.metadata_ = meta
        if workspace_id:
            await _mark_document_workspace_origin(
                db,
                entity_id=entity_id,
                document_id=doc.id,
                workspace_id=workspace_id,
                source=source,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tool_name=tool_name,
            )
        await db.commit()
        await bump_tool_cache_version(entity_id, "documents")
        return KnowledgeSyncResult(True, document_id=getattr(doc, "id", None))


async def reconcile_entity_filesystem(
    *,
    entity_id: str,
    entity_root: str,
    source: str = "filesystem_reconcile",
    created_by: str | None = None,
    sync_files: bool = True,
    trash_missing: bool = True,
    max_files: int = 10_000,
) -> KnowledgeReconcileResult:
    """Make the Knowledge projection match the entity's real visible files.

    This is intentionally filesystem-led: visible files are upserted into
    ``documents`` and visible document rows whose ``fs_path`` no longer exists
    are soft-deleted. Metadata-only documents and remote ``file_url`` documents
    are left alone because they do not have a required local payload.
    """
    root = os.path.realpath(entity_root)
    if not entity_id or not root or not os.path.isdir(root):
        return KnowledgeReconcileResult()

    visible_files: set[str] = set()
    limited = False
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = normalize_rel_path(os.path.relpath(dirpath, root))
        visible_dirnames: list[str] = []
        for dirname in dirnames:
            child_rel = normalize_rel_path(os.path.join(rel_dir, dirname))
            if is_user_visible_path(child_rel):
                visible_dirnames.append(dirname)
        dirnames[:] = visible_dirnames

        for filename in filenames:
            rel_file = normalize_rel_path(os.path.relpath(os.path.join(dirpath, filename), root))
            if not is_user_visible_path(rel_file):
                continue
            visible_files.add(rel_file)
            if max_files > 0 and len(visible_files) >= max_files:
                limited = True
                dirnames[:] = []
                break
        if limited:
            break

    synced_files = 0
    if sync_files:
        for rel_file in sorted(visible_files):
            abs_path = os.path.join(root, rel_file)
            try:
                result = await sync_file_to_knowledge(
                    entity_id=entity_id,
                    abs_path=abs_path,
                    entity_root=root,
                    source=source,
                    created_by=created_by,
                    force=True,
                )
                if result.synced:
                    synced_files += 1
            except Exception:
                # Reconcile should be best-effort per file so one corrupt file
                # does not prevent stale DB rows from being cleaned up below.
                continue

    checked_documents = 0
    trashed_missing = 0
    if trash_missing:
        async with async_session() as db:
            docs = list((await db.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.fs_path.is_not(None),
                    Document.is_trashed == False,  # noqa: E712
                )
            )).scalars().all())
            checked_documents = len(docs)
            now = datetime.now(timezone.utc)
            for doc in docs:
                rel_path = normalize_rel_path(str(doc.fs_path or ""))
                if not rel_path or not is_user_visible_path(rel_path):
                    continue
                if getattr(doc, "file_url", None):
                    continue
                if rel_path in visible_files:
                    continue
                full_path = os.path.realpath(os.path.join(root, rel_path))
                try:
                    if os.path.commonpath([root, full_path]) == root and os.path.isfile(full_path):
                        continue
                except ValueError:
                    pass
                doc.is_trashed = True
                doc.trashed_at = now
                doc.metadata_ = _with_file_integrity(
                    doc.metadata_,
                    status="missing",
                    recoverable=False,
                )
                if doc.vector_status != VectorStatus.FAILED:
                    doc.vector_status = VectorStatus.FAILED
                trashed_missing += 1
            if trashed_missing:
                await db.commit()
                await bump_tool_cache_version(entity_id, "documents")

    return KnowledgeReconcileResult(
        scanned_files=len(visible_files),
        synced_files=synced_files,
        checked_documents=checked_documents,
        trashed_missing_documents=trashed_missing,
        limited=limited,
    )


async def bind_document_to_workspace(
    *,
    entity_id: str,
    document_id: str,
    workspace_id: str | None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    tool_name: str | None = None,
) -> str | None:
    """Mark an existing document as produced or used in a workspace.

    This is provenance only. User-facing workspace Knowledge membership is
    controlled by explicit workspace Knowledge Nets (DocumentGroup rows)
    created through the workspace Knowledge UI/API.
    """
    if not workspace_id:
        return None
    async with async_session() as db:
        doc_id = await _mark_document_workspace_origin(
            db,
            entity_id=entity_id,
            document_id=document_id,
            workspace_id=workspace_id,
            source=None,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name=tool_name,
        )
        await db.commit()
        if doc_id:
            await bump_tool_cache_version(entity_id, "documents")
        return doc_id


async def _mark_document_workspace_origin(
    db,
    *,
    entity_id: str,
    document_id: str,
    workspace_id: str,
    source: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    tool_name: str | None = None,
) -> str | None:
    doc = (await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.entity_id == entity_id,
        ).limit(1)
    )).scalar_one_or_none()
    if not doc:
        return None

    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if not workspace:
        return None

    doc.metadata_ = merge_document_metadata(
        doc.metadata_,
        origin={
            "workspace_id": workspace_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "tool_name": tool_name,
        },
        artifact={"role": "final"} if source in _FINAL_ARTIFACT_SOURCES else None,
    )
    await db.flush()
    return doc.id


async def ensure_folder_path(entity_id: str, rel_path: str) -> str | None:
    """Create/find the DocumentFolder chain for a visible relative directory."""
    rel_path = normalize_rel_path(rel_path)
    if not rel_path or not is_user_visible_folder_path(rel_path):
        return None

    parts = _path_parts(rel_path)
    if not parts:
        return None

    parent_id: str | None = None
    last_id: str | None = None
    async with async_session() as db:
        for name in parts:
            folder_id = await _find_folder_id_at_position(
                db,
                entity_id=entity_id,
                parent_id=parent_id,
                name=name,
            )
            if not folder_id:
                folder_id = generate_ulid()
                result = await db.execute(
                    pg_insert(DocumentFolder)
                    .values(
                        id=folder_id,
                        entity_id=entity_id,
                        name=name,
                        parent_id=parent_id,
                    )
                    .on_conflict_do_nothing()
                    .returning(DocumentFolder.id)
                )
                folder_id = result.scalar_one_or_none()
                if not folder_id:
                    folder_id = await _find_folder_id_at_position(
                        db,
                        entity_id=entity_id,
                        parent_id=parent_id,
                        name=name,
                    )
                if not folder_id:
                    raise RuntimeError(f"Could not create or find Knowledge folder: {rel_path}")
            parent_id = folder_id
            last_id = folder_id
        await db.commit()
    return last_id


async def _find_folder_id_at_position(
    db,
    *,
    entity_id: str,
    parent_id: str | None,
    name: str,
) -> str | None:
    result = await db.execute(
        select(DocumentFolder.id).where(
            DocumentFolder.entity_id == entity_id,
            DocumentFolder.name == name,
            DocumentFolder.parent_id == parent_id,
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def find_folder_path(entity_id: str, rel_path: str) -> str | None:
    """Find an existing DocumentFolder chain without creating missing folders."""
    rel_path = normalize_rel_path(rel_path)
    if not rel_path or not is_user_visible_folder_path(rel_path):
        return None

    parent_id: str | None = None
    found_id: str | None = None
    async with async_session() as db:
        for name in _path_parts(rel_path):
            result = await db.execute(
                select(DocumentFolder).where(
                    DocumentFolder.entity_id == entity_id,
                    DocumentFolder.name == name,
                    DocumentFolder.parent_id == parent_id,
                ).limit(1)
            )
            folder = result.scalar_one_or_none()
            if not folder:
                return None
            parent_id = folder.id
            found_id = folder.id
    return found_id


async def trash_path(entity_id: str, rel_path: str) -> bool:
    """Soft-delete documents matching a visible file or directory path."""
    rel_path = normalize_rel_path(rel_path)
    if not is_user_visible_path(rel_path):
        return False

    async with async_session() as db:
        await db.execute(
            sa_update(Document)
            .where(Document.entity_id == entity_id, Document.fs_path == rel_path)
            .values(is_trashed=True, trashed_at=datetime.now(timezone.utc))
        )
        prefix = rel_path.rstrip("/") + "/"
        await db.execute(
            sa_update(Document)
            .where(
                Document.entity_id == entity_id,
                Document.fs_path.like(prefix + "%"),
            )
            .values(is_trashed=True, trashed_at=datetime.now(timezone.utc))
        )
        await db.commit()
        await bump_tool_cache_version(entity_id, "documents")
    return True


async def move_path(entity_id: str, old_rel: str, new_rel: str) -> bool:
    """Move/rename the Knowledge projection for a file or directory path."""
    old_rel = normalize_rel_path(old_rel)
    new_rel = normalize_rel_path(new_rel)
    old_visible = is_user_visible_path(old_rel)
    new_visible = is_user_visible_path(new_rel)
    if not old_visible and not new_visible:
        return False
    if old_visible and not new_visible:
        return await trash_path(entity_id, old_rel)
    if not old_visible and new_visible:
        # The destination may become visible, but without the absolute file path
        # here we cannot safely create a fresh Document projection.
        return False

    await move_folder_path(entity_id, old_rel, new_rel)
    new_dir = os.path.dirname(new_rel)
    new_folder_id = await ensure_folder_path(entity_id, new_dir) if new_dir else None

    async with async_session() as db:
        await db.execute(
            sa_update(Document)
            .where(Document.entity_id == entity_id, Document.fs_path == old_rel)
            .values(
                fs_path=new_rel,
                name=os.path.basename(new_rel),
                folder_id=new_folder_id,
            )
        )
        old_prefix = old_rel.rstrip("/") + "/"
        new_prefix = new_rel.rstrip("/") + "/"
        await db.execute(
            sa_update(Document)
            .where(
                Document.entity_id == entity_id,
                Document.fs_path.like(old_prefix + "%"),
            )
            .values(
                fs_path=sa_func.concat(
                    new_prefix,
                    sa_func.substr(Document.fs_path, len(old_prefix) + 1),
                )
            )
        )
        await db.commit()
        await bump_tool_cache_version(entity_id, "documents")
    return True


async def copy_file_projection(entity_id: str, old_rel: str, new_rel: str) -> bool:
    """Duplicate a Document row when a visible indexed file is copied."""
    old_rel = normalize_rel_path(old_rel)
    new_rel = normalize_rel_path(new_rel)
    if not is_user_visible_path(new_rel):
        return False

    async with async_session() as db:
        result = await db.execute(
            select(Document).where(Document.entity_id == entity_id, Document.fs_path == old_rel)
        )
        src_doc = result.scalar_one_or_none()
        if not src_doc:
            return False
        folder_id = await find_folder_path(entity_id, os.path.dirname(new_rel))
        new_doc = Document(
            id=generate_ulid(),
            entity_id=entity_id,
            name=os.path.basename(new_rel),
            fs_path=new_rel,
            file_size=src_doc.file_size,
            file_type=src_doc.file_type,
            mime_type=src_doc.mime_type,
            source=src_doc.source,
            created_by=src_doc.created_by,
            folder_id=folder_id,
        )
        db.add(new_doc)
        await db.commit()
        await bump_tool_cache_version(entity_id, "documents")
    return True


async def move_folder_path(entity_id: str, old_rel: str, new_rel: str) -> bool:
    """Move/rename a DocumentFolder chain to match a filesystem mv."""
    old_parts = _path_parts(old_rel)
    new_parts = _path_parts(new_rel)
    if not old_parts or not new_parts:
        return False

    async with async_session() as db:
        chain: list[DocumentFolder] = []
        parent_id: str | None = None
        for name in old_parts:
            row = await db.execute(
                select(DocumentFolder).where(
                    DocumentFolder.entity_id == entity_id,
                    DocumentFolder.name == name,
                    DocumentFolder.parent_id == parent_id,
                ).limit(1)
            )
            folder = row.scalar_one_or_none()
            if not folder:
                return False
            chain.append(folder)
            parent_id = folder.id

        leaf = chain[-1]
        new_parent_parts = new_parts[:-1]
        new_parent_id = None
        if new_parent_parts:
            new_parent_id = await ensure_folder_path(entity_id, "/".join(new_parent_parts))
        leaf.parent_id = new_parent_id
        leaf.name = new_parts[-1]
        await db.commit()
    return True


def _path_parts(rel_path: str) -> list[str]:
    cleaned = normalize_rel_path(rel_path).strip("/")
    if not cleaned:
        return []
    return [p for p in cleaned.split("/") if p and p not in (".", "..")]


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
