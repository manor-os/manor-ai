"""Synchronize user-visible filesystem changes into the Knowledge document index.

Filesystem remains the source for files/folders. The documents tables are the
user-facing Knowledge projection and are updated only for paths allowed by the
visibility policy.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, select, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder, DocumentGroupMember, VectorStatus
from packages.core.models.permission import (
    Classification,
    GrantStatus,
    PendingStatus,
    ResourceGrant,
    ResourceGrantPending,
    ResourceType,
    Share,
    Visibility,
)
from packages.core.models.workspace import Workspace
from packages.core.services.document_service import (
    StorageLimitExceeded,
    upsert_document_by_fs_path_result,
)
from packages.core.services.file_type_detection import detect_file_type
from packages.core.services.knowledge_visibility import (
    is_storage_only_path,
    is_user_visible_folder_path,
    is_user_visible_path,
    normalize_rel_path,
)
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.tool_cache_version import bump_tool_cache_version

def _schedule_document_reembed(document_id: str | None) -> None:
    """Best-effort re-embed after a byte change; the periodic sweep of
    vector_status=='pending' is the durable fallback if the queue is down."""
    if not document_id:
        return
    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings

        process_document_embeddings.delay(document_id)
    except Exception:
        pass


async def _invalidate_document_previews(entity_root: str, document_id: str | None) -> None:
    if not document_id:
        return
    from packages.core.services.slide_renderer import invalidate_document_preview_versions

    await asyncio.to_thread(
        invalidate_document_preview_versions,
        entity_root,
        document_id,
    )


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


def _sql_like_literal(value: str) -> str:
    """Escape a filesystem path before using it as a SQL LIKE prefix."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class KnowledgeSyncResult:
    """Result of projecting a filesystem path into Knowledge."""

    synced: bool
    document_id: str | None = None
    reason: str | None = None
    content_changed: bool = False
    name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None
    fs_path: str | None = None
    created: bool = False


@dataclass(frozen=True)
class KnowledgeReconcileResult:
    """Summary from reconciling Knowledge documents against real filesystem."""

    scanned_files: int = 0
    synced_files: int = 0
    checked_documents: int = 0
    missing_documents: int = 0
    trashed_missing_documents: int = 0
    limited: bool = False


@asynccontextmanager
async def _knowledge_sync_session(db: AsyncSession | None):
    if db is not None:
        yield db, False
        return
    async with async_session() as owned_db:
        yield owned_db, True


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
    visibility: str | None = None,
    classification: str | None = None,
    client_visible: bool | None = None,
    expected_content_sha256: str | None = None,
    db: AsyncSession | None = None,
    commit: bool = False,
) -> KnowledgeSyncResult:
    """Create/update a Document row for a visible filesystem file.

    ``force`` is an intent flag, not a permission override: hidden/system paths
    never sync even when force=True.
    """
    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        canonical_entity_root,
        open_entity_file_snapshot,
    )

    canonical_root = canonical_entity_root(entity_id)
    supplied_root = os.path.abspath(entity_root)
    supplied_path = os.path.abspath(abs_path)
    if supplied_root != canonical_root or os.path.realpath(supplied_root) != canonical_root:
        return KnowledgeSyncResult(False, reason="entity_root_mismatch")
    try:
        if os.path.commonpath([canonical_root, supplied_path]) != canonical_root:
            return KnowledgeSyncResult(False, reason="path_escape")
    except ValueError:
        return KnowledgeSyncResult(False, reason="path_escape")
    rel_path = normalize_rel_path(os.path.relpath(supplied_path, canonical_root))
    visible = is_user_visible_path(rel_path)
    should_sync = visible if force is None else bool(force) and visible
    if not should_sync:
        return KnowledgeSyncResult(False, reason="hidden" if not visible else "disabled")
    try:
        with open_entity_file_snapshot(
            entity_id,
            rel_path,
            expected_resolved_path=supplied_path,
            expected_content_sha256=expected_content_sha256,
        ) as snapshot:
            file_stat = snapshot.stat
            detected = detect_file_type(
                snapshot.descriptor_path,
                declared_name=os.path.basename(rel_path),
            )
    except EntityFilesystemError:
        return KnowledgeSyncResult(False, reason="source_changed")
    size = file_stat.st_size
    ext = detected.extension
    mime_type = detected.mime_type
    if not workspace_id:
        from packages.core.services.workspace_artifacts import infer_workspace_id_from_storage_path
        workspace_id = await infer_workspace_id_from_storage_path(
            entity_id=entity_id,
            rel_path=rel_path,
        )
    resolved_folder_id = folder_id
    if resolved_folder_id is None:
        if workspace_id:
            from packages.core.services.workspace_artifacts import ensure_workspace_document_folder
            resolved_folder_id = await ensure_workspace_document_folder(
                entity_id=entity_id,
                workspace_id=workspace_id,
                rel_path=rel_path,
                db=db,
            )
        else:
            rel_dir = os.path.dirname(rel_path)
            if rel_dir and is_user_visible_folder_path(rel_dir):
                if source in _AUTO_CREATE_FOLDER_SOURCES:
                    resolved_folder_id = await ensure_folder_path(
                        entity_id,
                        rel_dir,
                        db=db,
                    )
                else:
                    resolved_folder_id = await find_folder_path(
                        entity_id,
                        rel_dir,
                        db=db,
                    )

    # Reconcile re-projects files already on disk, so it must never be blocked
    # by the storage quota; every other source counts as adding to the KB.
    skip_storage_check = source == "filesystem_reconcile"
    async with _knowledge_sync_session(db) as (sync_db, owns_session):
        try:
            upsert = await upsert_document_by_fs_path_result(
                sync_db,
                entity_id,
                fs_path=rel_path,
                name=detected.display_name,
                file_size=size,
                file_type=ext,
                mime_type=mime_type,
                source=source,
                created_by=created_by,
                owner_id=user_id,
                folder_id=resolved_folder_id,
                visibility=visibility,
                classification=classification,
                client_visible=client_visible,
                skip_storage_check=skip_storage_check,
                emit_created_event=False,
            )
            doc = upsert.document
        except StorageLimitExceeded:
            # Over the plan limit: the file stays on disk but is not added to the
            # knowledge index. Callers (e.g. the generate_file tool) surface this.
            return KnowledgeSyncResult(False, reason="storage_limit")
        if upsert.created:
            from packages.core.services.event_emitter import emit_in_session

            await emit_in_session(
                sync_db,
                entity_id,
                "document.uploaded",
                source="document_service",
                payload={"document_id": doc.id, "name": doc.name},
                workspace_id=workspace_id,
                deliver_after_commit=True,
            )
        # ── invalidate derived representations on a byte change ──
        # A file's content lives in ONE place (the filesystem). The Document
        # projection carries TWO derived copies — the pgvector embedding and a
        # legacy metadata.content_text — that RAG serves. If a write/edit
        # changed the bytes but we leave those derived copies alone, read-back
        # returns stale content forever (the indexer only re-embeds
        # vector_status=='pending', which nothing was setting on edit). Detect
        # the change by mtime_ns (every writer here goes through os.replace, so
        # mtime always advances) and mark the projection dirty + drop the
        # content_text fork + schedule re-embedding, so the change pipeline is
        # the single point that keeps every representation coherent.
        prior_integrity = {}
        if isinstance(doc.metadata_, dict):
            prior_integrity = doc.metadata_.get("file_integrity") or {}
        prior_mtime = prior_integrity.get("mtime_ns")
        new_mtime = getattr(file_stat, "st_mtime_ns", None)
        content_changed = prior_mtime != new_mtime  # None on first sync ⇒ changed

        doc.metadata_ = _with_file_integrity(
            doc.metadata_,
            status="ok",
            mtime_ns=new_mtime,
        )
        if content_changed:
            doc.vector_status = VectorStatus.PENDING
            stale_meta = dict(doc.metadata_ or {})
            stale_meta.pop("content_text", None)
            stale_meta.pop("content", None)
            doc.metadata_ = stale_meta
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
                sync_db,
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
        await sync_db.flush()
        if content_changed:
            await _invalidate_document_previews(entity_root, getattr(doc, "id", None))
        if owns_session or commit:
            await sync_db.commit()
            if content_changed:
                _schedule_document_reembed(doc.id)
            await bump_tool_cache_version(entity_id, "documents")
        return KnowledgeSyncResult(
            True,
            document_id=getattr(doc, "id", None),
            content_changed=content_changed,
            name=getattr(doc, "name", None),
            file_size=getattr(doc, "file_size", None),
            mime_type=getattr(doc, "mime_type", None),
            fs_path=getattr(doc, "fs_path", None),
            created=upsert.created,
        )


async def reconcile_entity_filesystem(
    *,
    entity_id: str,
    entity_root: str,
    source: str = "filesystem_reconcile",
    created_by: str | None = None,
    sync_files: bool = True,
    mark_missing: bool = True,
    max_files: int = 10_000,
) -> KnowledgeReconcileResult:
    """Make the Knowledge projection match the entity's real visible files.

    This is intentionally filesystem-led: visible files are upserted into
    ``documents`` and visible document rows whose ``fs_path`` no longer exists
    receive a recoverable integrity diagnostic. Reconciliation never trashes a
    row because mounted storage can be temporarily unavailable.
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
    missing_documents = 0
    if mark_missing and not limited:
        async with async_session() as db:
            docs = list((await db.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.fs_path.is_not(None),
                    Document.is_trashed == False,  # noqa: E712
                )
            )).scalars().all())
            checked_documents = len(docs)
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
                doc.metadata_ = _with_file_integrity(
                    doc.metadata_,
                    status="missing",
                    recoverable=True,
                )
                missing_documents += 1
            if missing_documents:
                await db.commit()
                await bump_tool_cache_version(entity_id, "documents")

    return KnowledgeReconcileResult(
        scanned_files=len(visible_files),
        synced_files=synced_files,
        checked_documents=checked_documents,
        missing_documents=missing_documents,
        trashed_missing_documents=0,
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


async def ensure_folder_path(
    entity_id: str,
    rel_path: str,
    *,
    owner_id: str | None = None,
    db: AsyncSession | None = None,
) -> str | None:
    """Create/find the DocumentFolder chain for a visible relative directory."""
    if db is not None:
        return await _ensure_folder_path_in_session(
            db,
            entity_id=entity_id,
            rel_path=rel_path,
            owner_id=owner_id,
        )
    async with async_session() as owned_db:
        last_id = await _ensure_folder_path_in_session(
            owned_db,
            entity_id=entity_id,
            rel_path=rel_path,
            owner_id=owner_id,
        )
        await owned_db.commit()
        return last_id


async def _ensure_folder_path_in_session(
    db,
    *,
    entity_id: str,
    rel_path: str,
    owner_id: str | None = None,
) -> str | None:
    rel_path = normalize_rel_path(rel_path)
    if not rel_path or not is_user_visible_folder_path(rel_path):
        return None

    parts = _path_parts(rel_path)
    if not parts:
        return None

    parent_id: str | None = None
    last_id: str | None = None
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
                    owner_id=owner_id,
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
    if owner_id and last_id:
        await db.execute(
            sa_update(DocumentFolder)
            .where(
                DocumentFolder.id == last_id,
                DocumentFolder.entity_id == entity_id,
                DocumentFolder.owner_id.is_(None),
            )
            .values(owner_id=owner_id)
        )
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


async def find_folder_path(
    entity_id: str,
    rel_path: str,
    *,
    db: AsyncSession | None = None,
) -> str | None:
    """Find an existing DocumentFolder chain without creating missing folders."""
    if db is not None:
        folder = await _find_folder_path_in_session(
            db,
            entity_id=entity_id,
            rel_path=rel_path,
        )
        return folder.id if folder else None
    async with async_session() as owned_db:
        folder = await _find_folder_path_in_session(
            owned_db,
            entity_id=entity_id,
            rel_path=rel_path,
        )
        return folder.id if folder else None


async def _find_folder_path_in_session(
    db,
    *,
    entity_id: str,
    rel_path: str,
) -> DocumentFolder | None:
    rel_path = normalize_rel_path(rel_path)
    if not rel_path or not is_user_visible_folder_path(rel_path):
        return None

    parent_id: str | None = None
    folder: DocumentFolder | None = None
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
    return folder


async def _folder_subtree_ids_in_session(
    db,
    *,
    entity_id: str,
    root_id: str,
) -> set[str]:
    rows = (await db.execute(
        select(DocumentFolder.id, DocumentFolder.parent_id).where(
            DocumentFolder.entity_id == entity_id,
        )
    )).all()
    children: dict[str | None, list[str]] = {}
    for folder_id, parent_id in rows:
        children.setdefault(parent_id, []).append(folder_id)
    found: set[str] = set()
    pending = [root_id]
    while pending:
        folder_id = pending.pop()
        if folder_id in found:
            continue
        found.add(folder_id)
        pending.extend(children.get(folder_id, []))
    return found


async def trash_path(
    entity_id: str,
    rel_path: str,
    *,
    is_directory: bool | None = None,
    db: AsyncSession | None = None,
    commit: bool = False,
) -> bool:
    """Retire projections for files removed through a raw filesystem path.

    These callers run after ``rm`` or an equivalent physical delete, so there
    is no hidden file for the normal Knowledge restore flow to recover. Clear
    ``fs_path`` and record the former path explicitly; otherwise Restore would
    reactivate a phantom row that claims a file which no longer exists.
    """
    rel_path = normalize_rel_path(rel_path)
    if not is_user_visible_path(rel_path):
        return False

    async with _knowledge_sync_session(db) as (sync_db, owns_session):
        prefix = rel_path.rstrip("/") + "/"
        documents = list((await sync_db.scalars(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.is_trashed.is_(False),
                (
                    (Document.fs_path == rel_path)
                    | Document.fs_path.like(
                        _sql_like_literal(prefix) + "%",
                        escape="\\",
                    )
                ),
            )
        )).all())
        folder = None
        if is_directory is not False:
            folder = await _find_folder_path_in_session(
                sync_db,
                entity_id=entity_id,
                rel_path=rel_path,
            )
        folder_ids = (
            await _folder_subtree_ids_in_session(
                sync_db,
                entity_id=entity_id,
                root_id=folder.id,
            )
            if folder is not None
            else set()
        )
        if not documents and not folder_ids:
            return False

        now = datetime.now(timezone.utc)
        for document in documents:
            metadata = dict(document.metadata_ or {})
            metadata["deleted_fs_path"] = document.fs_path
            metadata["restore_blocked_reason"] = "filesystem_path_deleted"
            document.metadata_ = metadata
            document.fs_path = None
            document.folder_id = None
            document.is_trashed = True
            document.trashed_at = now
            document.trashed_by = "system:filesystem-delete"
        if folder_ids:
            await sync_db.execute(
                sa_update(Document)
                .where(
                    Document.entity_id == entity_id,
                    Document.folder_id.in_(folder_ids),
                )
                .values(folder_id=None)
            )
            await sync_db.execute(
                sa_update(Workspace)
                .where(
                    Workspace.entity_id == entity_id,
                    Workspace.artifact_folder_id.in_(folder_ids),
                )
                .values(artifact_folder_id=None)
            )
            await sync_db.execute(
                sa_delete(DocumentFolder).where(
                    DocumentFolder.entity_id == entity_id,
                    DocumentFolder.id.in_(folder_ids),
                )
            )
        if owns_session or commit:
            await sync_db.commit()
            await bump_tool_cache_version(entity_id, "documents")
    return True


async def move_path(
    entity_id: str,
    old_rel: str,
    new_rel: str,
    *,
    db: AsyncSession | None = None,
    commit: bool = False,
) -> bool:
    """Move/rename the Knowledge projection for a file or directory path."""
    old_rel = normalize_rel_path(old_rel)
    new_rel = normalize_rel_path(new_rel)
    old_visible = is_user_visible_path(old_rel)
    new_visible = is_user_visible_path(new_rel)
    if not old_visible and not new_visible:
        return False
    if old_visible and not new_visible:
        return await trash_path(
            entity_id,
            old_rel,
            db=db,
            commit=commit,
        )
    if not old_visible and new_visible:
        # The destination may become visible, but without the absolute file path
        # here we cannot safely create a fresh Document projection.
        return False

    async with _knowledge_sync_session(db) as (sync_db, owns_session):
        folder_moved = await _move_folder_path_in_session(
            sync_db,
            entity_id=entity_id,
            old_rel=old_rel,
            new_rel=new_rel,
        )
        new_dir = os.path.dirname(new_rel)
        new_folder_id = (
            await _ensure_folder_path_in_session(
                sync_db,
                entity_id=entity_id,
                rel_path=new_dir,
            )
            if new_dir
            else None
        )
        old_prefix = old_rel.rstrip("/") + "/"
        new_prefix = new_rel.rstrip("/") + "/"
        source_documents = list((await sync_db.scalars(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.is_trashed.is_(False),
                (
                    (Document.fs_path == old_rel)
                    | Document.fs_path.like(
                        _sql_like_literal(old_prefix) + "%",
                        escape="\\",
                    )
                ),
            )
        )).all())
        if not source_documents and not folder_moved:
            return False

        replacements: dict[str, str] = {}
        for document in source_documents:
            current_path = str(document.fs_path or "")
            replacements[document.id] = (
                new_rel
                if current_path == old_rel
                else new_prefix + current_path[len(old_prefix):]
            )

        source_ids = {document.id for document in source_documents}
        destination_paths = set(replacements.values())
        collisions = list((await sync_db.scalars(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path.in_(sorted(destination_paths)),
                Document.is_trashed.is_(False),
                Document.id.not_in(source_ids),
            )
        )).all())
        now = datetime.now(timezone.utc)
        for collision in collisions:
            metadata = dict(collision.metadata_ or {})
            metadata["replaced_fs_path"] = collision.fs_path
            metadata["restore_blocked_reason"] = "filesystem_path_replaced"
            collision.metadata_ = metadata
            collision.fs_path = None
            collision.is_trashed = True
            collision.trashed_at = now
            collision.trashed_by = "system:filesystem-move-replaced"
        if collisions:
            await sync_db.flush()

        for document in source_documents:
            destination = replacements[document.id]
            document.fs_path = destination
            document.name = os.path.basename(destination)
            if str(document.fs_path or "") == new_rel:
                document.folder_id = new_folder_id
        if owns_session or commit:
            await sync_db.commit()
            await bump_tool_cache_version(entity_id, "documents")
    return True


async def copy_file_projection(
    entity_id: str,
    old_rel: str,
    new_rel: str,
    *,
    operation_id: str | None = None,
) -> bool:
    """Duplicate a Document row when a visible indexed file is copied."""
    old_rel = normalize_rel_path(old_rel)
    new_rel = normalize_rel_path(new_rel)
    if not is_user_visible_path(new_rel):
        return False

    async with async_session() as db:
        result = await db.execute(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path == old_rel,
                Document.is_trashed.is_(False),
            )
        )
        src_doc = result.scalar_one_or_none()
        if not src_doc:
            return False
        folder = await _find_folder_path_in_session(
            db,
            entity_id=entity_id,
            rel_path=os.path.dirname(new_rel),
        )
        folder_id = folder.id if folder else None
        existing = await db.scalar(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path == new_rel,
                Document.is_trashed.is_(False),
            )
        )
        if existing is not None and operation_id:
            existing_metadata = dict(existing.metadata_ or {})
            if (
                existing_metadata.get("filesystem_copy_operation_id") == operation_id
                and existing_metadata.get("filesystem_copy_source_document_id") == src_doc.id
            ):
                # Retry the same journal entry without replacing identity. Also
                # re-assert the source's restrictive controls in case an older
                # generic filesystem repair touched the destination row.
                existing.visibility = _most_restrictive(
                    existing.visibility,
                    src_doc.visibility,
                    (Visibility.PUBLIC, Visibility.ENTITY, Visibility.WORKSPACE, Visibility.PRIVATE),
                )
                existing.classification = _most_restrictive(
                    existing.classification,
                    src_doc.classification,
                    Classification.LEVELS,
                )
                existing.client_visible = bool(existing.client_visible and src_doc.client_visible)
                existing.pii_detected = bool(existing.pii_detected or src_doc.pii_detected)
                existing.quarantine_status = _most_restrictive(
                    existing.quarantine_status,
                    src_doc.quarantine_status,
                    ("clean", "pending_scan", "quarantined", "rejected"),
                )
                if src_doc.owner_id:
                    existing.owner_id = src_doc.owner_id
                await db.commit()
                await bump_tool_cache_version(entity_id, "documents")
                return True
        source_group_ids = list((await db.scalars(
            select(DocumentGroupMember.group_id).where(
                DocumentGroupMember.document_id == src_doc.id,
            )
        )).all())
        visibility = src_doc.visibility
        classification = src_doc.classification
        client_visible = src_doc.client_visible
        pii_detected = src_doc.pii_detected
        quarantine_status = src_doc.quarantine_status
        if existing is not None:
            visibility = _most_restrictive(
                existing.visibility,
                src_doc.visibility,
                (Visibility.PUBLIC, Visibility.ENTITY, Visibility.WORKSPACE, Visibility.PRIVATE),
            )
            classification = _most_restrictive(
                existing.classification,
                src_doc.classification,
                Classification.LEVELS,
            )
            client_visible = bool(existing.client_visible and src_doc.client_visible)
            pii_detected = bool(existing.pii_detected or src_doc.pii_detected)
            quarantine_status = _most_restrictive(
                existing.quarantine_status,
                src_doc.quarantine_status,
                ("clean", "pending_scan", "quarantined", "rejected"),
            )
            now = datetime.now(timezone.utc)
            replaced_metadata = dict(existing.metadata_ or {})
            replaced_metadata["replaced_fs_path"] = existing.fs_path
            replaced_metadata["restore_blocked_reason"] = "filesystem_path_replaced"
            existing.metadata_ = replaced_metadata
            existing.fs_path = None
            existing.folder_id = None
            existing.is_trashed = True
            existing.trashed_at = now
            existing.trashed_by = "system:filesystem-copy-replaced"
            await db.execute(
                sa_update(ResourceGrant)
                .where(
                    ResourceGrant.entity_id == entity_id,
                    ResourceGrant.resource_type == ResourceType.DOCUMENT,
                    ResourceGrant.resource_id == existing.id,
                    ResourceGrant.status == GrantStatus.ACTIVE,
                )
                .values(
                    status=GrantStatus.REVOKED,
                    revoked_at=now,
                    revoked_by="system:filesystem-copy",
                )
            )
            await db.execute(
                sa_update(ResourceGrantPending)
                .where(
                    ResourceGrantPending.entity_id == entity_id,
                    ResourceGrantPending.resource_type.in_((ResourceType.DOCUMENT, "share")),
                    ResourceGrantPending.resource_id == existing.id,
                    ResourceGrantPending.status == PendingStatus.PENDING,
                )
                .values(
                    status=PendingStatus.DENIED,
                    decided_at=now,
                    decision_note="Destination content was replaced by a filesystem copy",
                )
            )
            await db.execute(
                sa_update(Share)
                .where(
                    Share.entity_id == entity_id,
                    Share.resource_type == ResourceType.DOCUMENT,
                    Share.resource_id == existing.id,
                    Share.status == "active",
                )
                .values(
                    status="revoked",
                    revoked_at=now,
                    revoked_by="system:filesystem-copy",
                )
            )
            await db.execute(
                sa_delete(DocumentGroupMember).where(
                    DocumentGroupMember.document_id == existing.id,
                )
            )
            await db.flush()
        copied_metadata = dict(src_doc.metadata_ or {})
        if operation_id:
            copied_metadata.update({
                "filesystem_copy_operation_id": operation_id,
                "filesystem_copy_source_document_id": src_doc.id,
                "filesystem_copy_source_path": old_rel,
            })
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
            metadata_=copied_metadata,
            vector_status=VectorStatus.PENDING,
            visibility=visibility,
            classification=classification,
            owner_id=src_doc.owner_id,
            client_visible=client_visible,
            pii_detected=pii_detected,
            quarantine_status=quarantine_status,
        )
        db.add(new_doc)
        await db.flush()
        for group_id in source_group_ids:
            db.add(DocumentGroupMember(document_id=new_doc.id, group_id=group_id))
        await db.commit()
        await bump_tool_cache_version(entity_id, "documents")
    return True


async def move_folder_path(entity_id: str, old_rel: str, new_rel: str) -> bool:
    """Move/rename a DocumentFolder chain to match a filesystem mv."""
    async with async_session() as db:
        moved = await _move_folder_path_in_session(
            db,
            entity_id=entity_id,
            old_rel=old_rel,
            new_rel=new_rel,
        )
        if moved:
            await db.commit()
        return moved


async def _move_folder_path_in_session(
    db,
    *,
    entity_id: str,
    old_rel: str,
    new_rel: str,
) -> bool:
    old_parts = _path_parts(old_rel)
    new_parts = _path_parts(new_rel)
    if not old_parts or not new_parts:
        return False

    leaf = await _find_folder_path_in_session(
        db,
        entity_id=entity_id,
        rel_path=old_rel,
    )
    if leaf is None:
        return False
    new_parent_parts = new_parts[:-1]
    new_parent_id = None
    if new_parent_parts:
        new_parent_id = await _ensure_folder_path_in_session(
            db,
            entity_id=entity_id,
            rel_path="/".join(new_parent_parts),
        )
    leaf.parent_id = new_parent_id
    leaf.name = new_parts[-1]
    return True


def _path_parts(rel_path: str) -> list[str]:
    cleaned = normalize_rel_path(rel_path).strip("/")
    if not cleaned:
        return []
    return [p for p in cleaned.split("/") if p and p not in (".", "..")]


def _most_restrictive(left: str, right: str, ordered: tuple[str, ...]) -> str:
    def rank(value: str) -> int:
        try:
            return ordered.index(value)
        except ValueError:
            return len(ordered)

    return left if rank(left) >= rank(right) else right


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
