"""Document service — CRUD, groups, file metadata."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional

from sqlalchemy import String, and_, func, not_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.document_groups import WorkspaceDocumentGroupKind
from packages.core.models.base import generate_ulid
from packages.core.models.document import (
    Document,
    DocumentFolder,
    DocumentGroup,
    DocumentGroupMember,
    VectorStatus,
)
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.comment_service import delete_resource_comments
from packages.core.services.entity_fs import (
    EditorWriteIntentStatus,
    EntityFilesystemStaleWriteError,
    SYSTEM_DIRS,
    SYSTEM_FILES,
)
from packages.core.services.knowledge_visibility import HIDDEN_PREFIXES, is_user_visible_path, normalize_rel_path
from packages.core.services.tool_cache_version import bump_tool_cache_version

logger = logging.getLogger(__name__)


async def _invalidate_document_preview_versions(
    entity_root: str,
    document_id: str,
) -> None:
    from packages.core.services.slide_renderer import invalidate_document_preview_versions

    await asyncio.to_thread(
        invalidate_document_preview_versions,
        entity_root,
        document_id,
    )


class StorageLimitExceeded(Exception):
    """Raised when adding a document would exceed the entity's plan storage.

    Carries the same fields as the plan gate's 402 detail so the HTTP layer and
    runtime tools can surface a consistent "upgrade to add more" message.
    """

    def __init__(self, message: str, *, plan: str = "", limit: float | None = None, current: float | None = None):
        super().__init__(message)
        self.message = message
        self.plan = plan
        self.limit = limit
        self.current = current


class DocumentMutationConflictError(RuntimeError):
    """The document's lifecycle or policy scope changed during admission."""


@dataclass(frozen=True)
class DocumentFileSaveResult:
    document: Document
    replayed: bool


@dataclass(frozen=True)
class DocumentUpsertResult:
    """Document projection plus whether this transaction created its row."""

    document: Document
    created: bool


_METADATA_EDITOR_WRITE_FENCE_KEY = "_editor_write_fence"
_MAX_METADATA_EDITOR_WRITE_FENCE_SESSIONS = 64
_DOCUMENT_CONTENT_WRITE_INTENT_PREFIX = b"manor-document-content-v1\0"


def _document_content_write_intent(content: str) -> bytes:
    """Return a stable editor intent independent of generated OOXML bytes."""
    return _DOCUMENT_CONTENT_WRITE_INTENT_PREFIX + content.encode("utf-8")


def _compatible_legacy_document_content_digests(
    content: str,
) -> frozenset[str]:
    return frozenset({hashlib.sha256(content.encode("utf-8")).hexdigest()})


def _metadata_editor_write_session(
    metadata: dict | None,
    session_id: str,
) -> dict | None:
    raw_fence = (metadata or {}).get(_METADATA_EDITOR_WRITE_FENCE_KEY)
    raw_sessions = raw_fence.get("sessions") if isinstance(raw_fence, dict) else None
    previous = raw_sessions.get(session_id) if isinstance(raw_sessions, dict) else None
    return previous if isinstance(previous, dict) else None


def _claim_metadata_editor_write_intent(
    metadata: dict | None,
    *,
    session_id: str,
    sequence: int,
    content: bytes | str,
    compatible_legacy_digests: frozenset[str] = frozenset(),
) -> tuple[EditorWriteIntentStatus, dict]:
    """Claim an editor write inside the document row's locked metadata.

    The caller persists the returned metadata in the same transaction as the
    content and version. Consequently a committed fence can be used both as a
    per-session high-water mark and as the durable retry receipt.
    """
    next_metadata = dict(metadata or {})
    raw_fence = next_metadata.get(_METADATA_EDITOR_WRITE_FENCE_KEY)
    raw_sessions = raw_fence.get("sessions") if isinstance(raw_fence, dict) else None
    sessions = dict(raw_sessions) if isinstance(raw_sessions, dict) else {}
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    content_digest = hashlib.sha256(content_bytes).hexdigest()
    previous = sessions.get(session_id)

    if isinstance(previous, dict):
        previous_sequence = previous.get("sequence")
        if isinstance(previous_sequence, int):
            if sequence < previous_sequence:
                return EditorWriteIntentStatus.STALE, next_metadata
            if sequence == previous_sequence:
                previous_digest = previous.get("content_digest")
                legacy_digest_matches = (
                    previous.get("digest_scheme") is None
                    and isinstance(previous_digest, str)
                    and previous_digest in compatible_legacy_digests
                )
                if previous_digest != content_digest and not legacy_digest_matches:
                    return EditorWriteIntentStatus.STALE, next_metadata
                if previous.get("committed") is True:
                    return EditorWriteIntentStatus.REPLAYED, next_metadata

    sessions[session_id] = {
        "sequence": sequence,
        "content_digest": content_digest,
        "committed": True,
        "updated_at": time.time_ns(),
    }
    if len(sessions) > _MAX_METADATA_EDITOR_WRITE_FENCE_SESSIONS:
        sessions = dict(sorted(
            sessions.items(),
            key=lambda item: item[1].get("updated_at", 0)
            if isinstance(item[1], dict) else 0,
            reverse=True,
        )[:_MAX_METADATA_EDITOR_WRITE_FENCE_SESSIONS])
    next_metadata[_METADATA_EDITOR_WRITE_FENCE_KEY] = {"sessions": sessions}
    return EditorWriteIntentStatus.CLAIMED, next_metadata


async def _repair_entity_editor_write_receipt(
    entity_root: str,
    rel_path: str,
    session_id: str,
    sequence: int,
    content_digest: str,
    *,
    document_id: str,
) -> None:
    """Mirror the DB-backed save receipt into the cross-entry path fence."""
    from packages.core.services.entity_fs import (
        mark_entity_editor_write_intent_digest_committed,
    )

    try:
        await asyncio.to_thread(
            mark_entity_editor_write_intent_digest_committed,
            entity_root,
            rel_path,
            session_id,
            sequence,
            content_digest,
            receipt={"document_id": document_id},
        )
    except Exception:
        # The document row stores the authoritative receipt in the same commit
        # as the version. A failed mirror is repaired by an identical retry and
        # must not turn an already-committed save into a duplicate side effect.
        logger.warning(
            "Could not mirror committed editor receipt for document %s",
            document_id,
            exc_info=True,
        )


async def _enforce_storage_limit(db: AsyncSession, entity_id: str) -> None:
    """Raise :class:`StorageLimitExceeded` if the entity is at/over its plan's
    knowledge-base storage limit. No-op in OSS/self-hosted (plan gate allows)."""
    from packages.core.services.plan_gate import check

    gate = await check(db, entity_id, "storage_mb")
    if not gate.allowed:
        raise StorageLimitExceeded(
            gate.message or "Knowledge base storage limit reached. Upgrade for more.",
            plan=gate.plan, limit=gate.limit, current=gate.current,
        )


# ── Documents ──

def _visible_fs_path_clause():
    hidden_checks = [
        Document.fs_path.ilike(".%"),
        Document.fs_path.ilike("%/.%"),
    ]
    for prefix in HIDDEN_PREFIXES:
        bare = prefix.rstrip("/")
        hidden_checks.append(Document.fs_path == bare)
        hidden_checks.append(Document.fs_path.ilike(f"{prefix}%"))
    for name in SYSTEM_FILES:
        hidden_checks.append(Document.fs_path == name)
        hidden_checks.append(Document.fs_path.ilike(f"%/{name}"))
    for dirname in SYSTEM_DIRS:
        hidden_checks.append(Document.fs_path == dirname)
        hidden_checks.append(Document.fs_path.ilike(f"{dirname}/%"))
        hidden_checks.append(Document.fs_path.ilike(f"%/{dirname}/%"))
    return or_(Document.fs_path.is_(None), not_(or_(*hidden_checks)))

async def list_documents(
    db: AsyncSession, entity_id: str, *,
    name_search: str | None = None,
    folder_id: str | None = None,
    folder_ids: set[str] | None = None,
    workspace_id: str | None = None,
    include_generated_assets: bool = True,
    limit: int | None = 100, offset: int = 0,
) -> tuple[list[Document], int]:
    conditions = _document_scope_conditions(
        entity_id,
        name_search=name_search,
        workspace_id=workspace_id,
        include_generated_assets=include_generated_assets,
    )
    q = select(Document).where(*conditions)
    count_q = select(func.count()).select_from(Document).where(*conditions)
    if folder_ids is not None:
        if not folder_ids:
            return [], 0
        q = q.where(Document.folder_id.in_(folder_ids))
        count_q = count_q.where(Document.folder_id.in_(folder_ids))
    elif folder_id is not None:
        # "root" or empty string means documents with no folder
        if folder_id == "" or folder_id == "root":
            q = q.where(Document.folder_id.is_(None))
            count_q = count_q.where(Document.folder_id.is_(None))
        else:
            q = q.where(Document.folder_id == folder_id)
            count_q = count_q.where(Document.folder_id == folder_id)
    # OFFSET pagination needs a total order. ``created_at`` is not unique
    # (bulk imports commonly share one timestamp), so use the immutable id as
    # a deterministic tie-breaker to prevent skipped or duplicated rows.
    q = q.order_by(Document.created_at.desc(), Document.id.desc())
    if limit is not None:
        q = q.limit(limit).offset(offset)
    elif offset:
        q = q.offset(offset)
    result = await db.execute(q)
    count_result = await db.execute(count_q)
    return list(result.scalars().all()), count_result.scalar_one()


def _document_scope_conditions(
    entity_id: str, *,
    name_search: str | None = None,
    workspace_id: str | None = None,
    include_generated_assets: bool = True,
) -> list:
    """Shared WHERE clauses for the visible-document scope.

    Everything that defines *which* documents are in view — entity, not
    trashed, not hidden, generated-asset and workspace filters, name search —
    minus the per-folder filter and pagination. Used by both the paginated
    listing and the storage-usage aggregate so the two always agree.
    """
    conditions = [
        Document.entity_id == entity_id,
        Document.is_trashed == False,  # noqa: E712
        _visible_fs_path_clause(),
    ]
    if not include_generated_assets:
        generated_media = and_(
            Document.source.in_(("ai_generated", "sandbox", "bash", "agent", "elevenlabs", "mcp")),
            or_(
                Document.mime_type.ilike("image/%"),
                Document.mime_type.ilike("video/%"),
                Document.mime_type.ilike("audio/%"),
                Document.file_type.in_(("png", "jpg", "jpeg", "webp", "gif", "mp4", "mov", "webm", "mp3", "wav", "m4a")),
            ),
        )
        conditions.append(not_(generated_media))
    if name_search:
        search = f"%{name_search}%"
        conditions.append(or_(
            Document.name.ilike(search),
            Document.fs_path.ilike(search),
            Document.file_type.ilike(search),
            Document.mime_type.ilike(search),
            Document.source.ilike(search),
            Document.metadata_.cast(String).ilike(search),
        ))
    if workspace_id:
        group_membership = (
            select(DocumentGroupMember.document_id)
            .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
            .where(
                DocumentGroupMember.document_id == Document.id,
                DocumentGroup.entity_id == entity_id,
                DocumentGroup.workspace_id == workspace_id,
            )
            .exists()
        )
        conditions.append(or_(
            group_membership,
            Document.metadata_["origin"]["workspace_id"].astext == workspace_id,
        ))
    return conditions


async def storage_usage(
    db: AsyncSession, entity_id: str, *,
    name_search: str | None = None,
    folder_ids: set[str] | None = None,
    workspace_id: str | None = None,
    include_generated_assets: bool = True,
) -> tuple[int, int]:
    """Total ``(size_bytes, file_count)`` for visible documents in scope.

    Unlike :func:`list_documents`, this is **not** capped to a single folder
    level or page: when ``folder_ids`` is given it sums every document in that
    set of folders (the caller passes a folder plus its descendants), and when
    it is ``None`` it covers the whole scope (the entire knowledge base, or a
    workspace). This is what the Knowledge Base header should show — the size of
    everything under the current location, including nested folders.
    """
    conditions = _document_scope_conditions(
        entity_id,
        name_search=name_search,
        workspace_id=workspace_id,
        include_generated_assets=include_generated_assets,
    )
    q = select(
        func.coalesce(func.sum(Document.file_size), 0),
        func.count(),
    ).select_from(Document).where(*conditions)
    if folder_ids is not None:
        if not folder_ids:
            return 0, 0
        q = q.where(Document.folder_id.in_(folder_ids))
    row = (await db.execute(q)).one()
    return int(row[0] or 0), int(row[1] or 0)


async def get_document(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    for_filesystem_mutation: bool = False,
    mutation_authorizer: (
        Callable[[AsyncSession, Document], Awaitable[None]] | None
    ) = None,
) -> Optional[Document]:
    if for_filesystem_mutation:
        return await _get_document_for_filesystem_mutation(
            db,
            doc_id,
            entity_id,
            mutation_authorizer=mutation_authorizer,
        )
    result = await db.execute(
        select(Document).where(
            Document.id == doc_id,
            Document.entity_id == entity_id,
            Document.is_trashed == False,  # noqa: E712
            _visible_fs_path_clause(),
        )
    )
    return result.scalar_one_or_none()


async def _get_document_for_filesystem_mutation(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    mutation_authorizer: (
        Callable[[AsyncSession, Document], Awaitable[None]] | None
    ),
) -> Optional[Document]:
    """Lock lifecycle/policy owners and authorize immediately before a write."""

    return await _get_document_for_policy_mutation(
        db,
        doc_id,
        entity_id,
        is_trashed=False,
        require_visible_path=True,
        mutation_authorizer=mutation_authorizer,
    )


async def _get_document_for_policy_mutation(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    is_trashed: bool,
    require_visible_path: bool,
    mutation_authorizer: (
        Callable[[AsyncSession, Document], Awaitable[None]] | None
    ) = None,
) -> Optional[Document]:
    """Lock Workspace/folder policy before one Document mutation."""

    from packages.core.services.document_access import (
        lock_folder_policy_rows,
        lock_workspace_policy_rows,
        resolve_document_policy_lock_scope,
    )

    conditions = [
        Document.id == doc_id,
        Document.entity_id == entity_id,
        Document.is_trashed.is_(is_trashed),
    ]
    if require_visible_path:
        conditions.append(_visible_fs_path_clause())
    base_stmt = select(Document).where(*conditions)
    for _attempt in range(3):
        savepoint = await db.begin_nested()
        try:
            document = (await db.execute(base_stmt)).scalar_one_or_none()
            if document is None:
                await savepoint.commit()
                return None

            scope = await resolve_document_policy_lock_scope(db, document)
            workspaces = await lock_workspace_policy_rows(
                db,
                entity_id=entity_id,
                workspace_ids=scope.workspace_ids,
            )
            if any(workspace.deleted_at is not None for workspace in workspaces):
                await savepoint.commit()
                return None
            await lock_folder_policy_rows(
                db,
                entity_id=entity_id,
                folder_id=document.folder_id,
                folder_ids=scope.folder_ids,
            )
            document = (await db.execute(
                base_stmt.with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if document is None:
                await savepoint.commit()
                return None
            if await resolve_document_policy_lock_scope(db, document) != scope:
                await savepoint.rollback()
                continue
            if mutation_authorizer is not None:
                await mutation_authorizer(db, document)
            await savepoint.commit()
            return document
        except BaseException:
            if savepoint.is_active:
                await savepoint.rollback()
            raise

    raise DocumentMutationConflictError(
        "Document policy changed during the write; retry the request"
    )


async def get_document_for_update(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    include_trashed: bool = False,
) -> Optional[Document]:
    """Return one lifecycle-visible document under the mutation lock."""
    return await _get_document_for_policy_mutation(
        db,
        doc_id,
        entity_id,
        is_trashed=include_trashed,
        require_visible_path=not include_trashed,
    )


async def _folder_default_visibility(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str,
    owner_id: str | None,
) -> str:
    """Visibility a new document should take from the folder it lands in.

    A ``private`` folder is only meaningful if what you put in it is private
    too, so the folder's own band is inherited as-is. A missing folder falls
    back to the loose-document rule rather than silently widening to the whole
    entity.
    """
    folder_visibility = (
        await db.execute(
            select(DocumentFolder.visibility).where(
                DocumentFolder.id == folder_id,
                DocumentFolder.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if folder_visibility:
        return str(folder_visibility)
    return "private" if owner_id is not None else "entity"


async def create_document(
    db: AsyncSession, entity_id: str, *,
    name: str, fs_path: str | None = None, file_url: str | None = None,
    file_size: int | None = None, file_type: str | None = None,
    mime_type: str | None = None, source: str = "upload",
    created_by: str | None = None, folder_id: str | None = None,
    metadata: dict | None = None,
    # ── Permission-v1 fields (see docs/PERMISSIONS_DESIGN_ZH.md §13) ────
    visibility: str | None = None,
    classification: str | None = None,
    client_visible: bool | None = None,
    owner_id: str | None = None,
    upload_idempotency_key: str | None = None,
    upload_request_fingerprint: str | None = None,
    # When True, skip the plan storage-limit check. Used for bookkeeping that
    # re-projects files already on disk (e.g. filesystem reconcile), which must
    # not be blocked just because the entity is over its quota.
    skip_storage_check: bool = False,
    emit_created_event: bool = True,
) -> Document:
    # Every new knowledge-base document funnels through here (direct creates and
    # the new-row branch of upsert_document_by_fs_path), so this is the single
    # chokepoint that enforces the storage limit across ALL add paths — uploads,
    # AI drafts, URL/Drive imports, and agent/sandbox-generated files alike.
    if not skip_storage_check:
        await _enforce_storage_limit(db, entity_id)
    doc = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        fs_path=fs_path,
        file_url=file_url,
        file_size=file_size,
        file_type=file_type,
        mime_type=mime_type,
        source=source,
        created_by=created_by,
        folder_id=folder_id,
        upload_idempotency_key=upload_idempotency_key,
        upload_request_fingerprint=upload_request_fingerprint,
    )
    if visibility is not None:
        doc.visibility = visibility
    elif folder_id:
        # Inherit the folder's visibility. Filing a document used to make it
        # *more* public than leaving it loose — a document dropped at the root
        # became private, but the same document moved into a folder defaulted
        # to entity-wide, regardless of how restricted that folder was.
        doc.visibility = await _folder_default_visibility(
            db, entity_id=entity_id, folder_id=folder_id, owner_id=owner_id,
        )
    elif owner_id is not None:
        doc.visibility = "private"
    if classification is not None:
        doc.classification = classification
    if client_visible is not None:
        doc.client_visible = client_visible
    if owner_id is not None:
        doc.owner_id = owner_id
    if metadata:
        doc.metadata_ = merge_document_metadata(metadata)
    db.add(doc)
    await db.flush()

    if emit_created_event:
        from packages.core.services.event_emitter import emit_in_session

        await emit_in_session(
            db,
            entity_id,
            "document.uploaded",
            source="document_service",
            payload={"document_id": doc.id, "name": name},
            deliver_after_commit=True,
        )
    await bump_tool_cache_version(entity_id, "documents")

    return doc


async def upsert_document_by_fs_path_result(
    db: AsyncSession,
    entity_id: str,
    *,
    fs_path: str,
    name: str,
    file_size: int | None = None,
    file_type: str | None = None,
    mime_type: str | None = None,
    source: str = "manual",
    created_by: str | None = None,
    owner_id: str | None = None,
    folder_id: str | None = None,
    visibility: str | None = None,
    classification: str | None = None,
    client_visible: bool | None = None,
    skip_storage_check: bool = False,
    emit_created_event: bool = True,
) -> DocumentUpsertResult:
    """Idempotent upsert by (entity_id, fs_path).

    Updating an existing projection never counts as "adding" storage, so the
    limit is only enforced on the new-row branch (and skippable for reconcile).
    """
    # Serialize same-path projection writers in PostgreSQL. This prevents a
    # loser from observing no row, colliding with the partial unique index, and
    # causing the filesystem router to roll back a path another writer owns.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(CAST(:document_path_key AS text), 0))"
            ),
            {"document_path_key": f"document-path:{entity_id}:{fs_path}"},
        )

    async def find_existing() -> Document | None:
        result = await db.execute(
            select(Document)
            .where(
                Document.entity_id == entity_id,
                Document.fs_path == fs_path,
            )
            .order_by(
                Document.is_trashed.asc(),
                func.coalesce(Document.updated_at, Document.created_at).desc(),
                Document.created_at.desc(),
                Document.id.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_existing(doc: Document) -> Document:
        doc.name = name
        doc.file_size = file_size
        doc.file_type = file_type
        doc.mime_type = mime_type
        # A filesystem write can recreate a path that was previously soft
        # deleted. In that case the filesystem is the source of truth again, so
        # revive the Knowledge projection instead of leaving it hidden in Trash.
        doc.is_trashed = False
        doc.trashed_at = None
        doc.trashed_by = None
        if folder_id is not None:
            doc.folder_id = folder_id
        if not doc.source:
            doc.source = source
        if created_by and not doc.created_by:
            doc.created_by = created_by
        # Filesystem projection updates describe who performed the sync, not an
        # ownership transfer. Preserve an established owner so a collaborator
        # with EDIT access cannot become the owner merely by changing bytes.
        if owner_id is not None and doc.owner_id is None:
            doc.owner_id = owner_id
        if visibility is not None:
            doc.visibility = visibility
        if classification is not None:
            doc.classification = classification
        if client_visible is not None:
            doc.client_visible = client_visible
        await bump_tool_cache_version(entity_id, "documents")
        return doc

    doc = await find_existing()
    if doc:
        return DocumentUpsertResult(
            document=await update_existing(doc),
            created=False,
        )

    try:
        async with db.begin_nested():
            created_document = await create_document(
                db,
                entity_id,
                name=name,
                fs_path=fs_path,
                file_size=file_size,
                file_type=file_type,
                mime_type=mime_type,
                source=source,
                created_by=created_by,
                owner_id=owner_id,
                folder_id=folder_id,
                visibility=visibility,
                classification=classification,
                client_visible=client_visible,
                skip_storage_check=skip_storage_check,
                emit_created_event=emit_created_event,
            )
            return DocumentUpsertResult(
                document=created_document,
                created=True,
            )
    except IntegrityError:
        # Non-PostgreSQL deployments do not have the advisory lock above. If a
        # concurrent writer won the insert race, the savepoint keeps the outer
        # transaction usable so this call can resolve to that canonical row.
        doc = await find_existing()
        if doc is None:
            raise
        return DocumentUpsertResult(
            document=await update_existing(doc),
            created=False,
        )


async def upsert_document_by_fs_path(
    db: AsyncSession,
    entity_id: str,
    *,
    fs_path: str,
    name: str,
    file_size: int | None = None,
    file_type: str | None = None,
    mime_type: str | None = None,
    source: str = "manual",
    created_by: str | None = None,
    owner_id: str | None = None,
    folder_id: str | None = None,
    visibility: str | None = None,
    classification: str | None = None,
    client_visible: bool | None = None,
    skip_storage_check: bool = False,
) -> Document:
    """Compatibility wrapper for callers that only need the projected row."""
    result = await upsert_document_by_fs_path_result(
        db,
        entity_id,
        fs_path=fs_path,
        name=name,
        file_size=file_size,
        file_type=file_type,
        mime_type=mime_type,
        source=source,
        created_by=created_by,
        owner_id=owner_id,
        folder_id=folder_id,
        visibility=visibility,
        classification=classification,
        client_visible=client_visible,
        skip_storage_check=skip_storage_check,
    )
    return result.document


async def rename_document(db: AsyncSession, doc_id: str, entity_id: str, new_name: str) -> Optional[Document]:
    """Rename a document. Returns the updated document or None if not found."""
    doc = await get_document(db, doc_id, entity_id)
    if not doc:
        return None
    doc.name = new_name
    await db.flush()
    await bump_tool_cache_version(entity_id, "documents")
    return doc


async def delete_document(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    include_trashed: bool = False,
) -> bool:
    doc = await get_document_for_update(
        db,
        doc_id,
        entity_id,
        include_trashed=include_trashed,
    )
    if not doc:
        return False
    await delete_resource_comments(db, entity_id, "document", [doc.id])
    await db.delete(doc)
    await db.flush()
    await bump_tool_cache_version(entity_id, "documents")
    return True


# ── Document Groups ──

async def list_groups(db: AsyncSession, entity_id: str) -> list[DocumentGroup]:
    result = await db.execute(
        select(DocumentGroup).where(DocumentGroup.entity_id == entity_id)
    )
    return list(result.scalars().all())


async def create_group(db: AsyncSession, entity_id: str, *, name: str, workspace_id: str | None = None) -> DocumentGroup:
    group = DocumentGroup(
        id=generate_ulid(), entity_id=entity_id,
        name=name, workspace_id=workspace_id,
    )
    db.add(group)
    await db.flush()
    return group


async def create_workspace_knowledge_group(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    name: str,
    purpose: str = "",
    kind: str = WorkspaceDocumentGroupKind.KNOWLEDGE_NET.value,
) -> DocumentGroup:
    """Create one ordinary Workspace Knowledge group with canonical settings."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Knowledge Net name is required")
    clean_kind = str(kind or WorkspaceDocumentGroupKind.KNOWLEDGE_NET).strip()
    if clean_kind == WorkspaceDocumentGroupKind.LEGACY_KNOWLEDGE_FOLDER:
        clean_kind = WorkspaceDocumentGroupKind.KNOWLEDGE_NET.value
    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=clean_name,
        settings={
            "kind": clean_kind or WorkspaceDocumentGroupKind.KNOWLEDGE_NET.value,
            "scope": "workspace",
            "purpose": str(purpose or "").strip(),
            "user_manageable": True,
        },
    )
    db.add(group)
    await db.flush()
    return group


async def mark_workspace_knowledge_changed(
    entity_id: str,
    workspace_id: str,
) -> None:
    """Invalidate both Knowledge tools and Workspace chat context."""
    await bump_tool_cache_version(entity_id, "documents")
    from packages.core.workspace_chat.context import invalidate

    invalidate(workspace_id)


async def trigger_reindex(db: AsyncSession, entity_id: str) -> int:
    """Reset all documents to pending and trigger re-indexing."""
    from packages.core.services.reusable_resource_locks import (
        lock_reusable_resource_lifecycle,
    )
    from packages.core.services.document_access import (
        document_workspace_ids_batched,
    )
    from packages.core.models.workspace import Workspace

    # Workspace soft-delete/restore takes this same entity lifecycle fence
    # before changing deleted_at, so the active-owner snapshot and UPDATE are
    # one linearized operation.
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    candidates = list((await db.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.vector_status != VectorStatus.PENDING,
            Document.is_trashed.is_(False),
            _visible_fs_path_clause(),
        )
    )).scalars())
    workspace_ids_by_document = await document_workspace_ids_batched(db, candidates)
    workspace_ids = {
        workspace_id
        for owned_workspace_ids in workspace_ids_by_document.values()
        for workspace_id in owned_workspace_ids
    }
    deleted_workspace_ids = set()
    if workspace_ids:
        deleted_workspace_ids = set((await db.execute(
            select(Workspace.id).where(
                Workspace.entity_id == entity_id,
                Workspace.id.in_(workspace_ids),
                Workspace.deleted_at.is_not(None),
            )
        )).scalars())
    active_ids = [
        document.id
        for document in candidates
        if not (
            workspace_ids_by_document.get(str(document.id), set())
            & deleted_workspace_ids
        )
    ]
    if not active_ids:
        return 0
    result = await db.execute(
        update(Document)
        .where(
            Document.entity_id == entity_id,
            Document.id.in_(active_ids),
        )
        .values(vector_status=VectorStatus.PENDING)
    )
    await db.flush()
    if result.rowcount:
        await bump_tool_cache_version(entity_id, "documents")
    return result.rowcount


_GROUP_MEMBER_INSERT_BATCH_SIZE = 500


async def add_documents_to_group(
    db: AsyncSession,
    doc_ids: list[str],
    group_id: str,
    *,
    entity_id: str | None = None,
) -> int:
    """Attach documents idempotently and return the number actually inserted.

    The composite primary key on ``DocumentGroupMember`` is the concurrency
    boundary. Using a database conflict-ignore insert keeps a simultaneous
    single-document add and recursive folder import from turning a harmless
    duplicate into a failed transaction.
    """
    wanted = sorted({str(doc_id).strip() for doc_id in doc_ids if doc_id})
    if not wanted:
        return 0

    if entity_id:
        group = (await db.execute(
            select(DocumentGroup.id).where(
                DocumentGroup.id == group_id,
                DocumentGroup.entity_id == entity_id,
            )
        )).scalar_one_or_none()
        if not group:
            return 0

    # Serialize sharing with Blueprint undo's ownership check/deletion. There
    # is no membership FK, so revalidate under the Document lock even for
    # trusted internal callers that omit entity_id.
    valid_ids: list[str] = []
    for start in range(0, len(wanted), _GROUP_MEMBER_INSERT_BATCH_SIZE):
        batch = wanted[start : start + _GROUP_MEMBER_INSERT_BATCH_SIZE]
        rows = set((await db.execute(
            select(Document.id).where(
                Document.id.in_(batch),
                Document.is_trashed.is_(False),
                *([Document.entity_id == entity_id] if entity_id else []),
            ).order_by(Document.id).with_for_update()
        )).scalars().all())
        valid_ids.extend(doc_id for doc_id in batch if doc_id in rows)
    wanted = valid_ids
    if not wanted:
        return 0

    dialect_name = db.get_bind().dialect.name
    inserted = 0
    for start in range(0, len(wanted), _GROUP_MEMBER_INSERT_BATCH_SIZE):
        batch = wanted[start : start + _GROUP_MEMBER_INSERT_BATCH_SIZE]
        values = [
            {"document_id": document_id, "group_id": group_id}
            for document_id in batch
        ]
        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert

            statement = (
                dialect_insert(DocumentGroupMember)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        DocumentGroupMember.document_id,
                        DocumentGroupMember.group_id,
                    ]
                )
                .returning(DocumentGroupMember.document_id)
            )
            inserted += len((await db.execute(statement)).scalars().all())
            continue
        if dialect_name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert

            statement = (
                dialect_insert(DocumentGroupMember)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        DocumentGroupMember.document_id,
                        DocumentGroupMember.group_id,
                    ]
                )
                .returning(DocumentGroupMember.document_id)
            )
            inserted += len((await db.execute(statement)).scalars().all())
            continue

        for document_id in batch:
            try:
                async with db.begin_nested():
                    db.add(DocumentGroupMember(
                        document_id=document_id,
                        group_id=group_id,
                    ))
                    await db.flush()
                inserted += 1
            except IntegrityError:
                continue
    return inserted


async def add_document_to_group(
    db: AsyncSession,
    doc_id: str,
    group_id: str,
    *,
    entity_id: str | None = None,
) -> bool:
    return bool(await add_documents_to_group(
        db,
        [doc_id],
        group_id,
        entity_id=entity_id,
    ))


# ── Content read/write ──

def _metadata_text_content(doc: Document) -> str | None:
    """Return legacy inline document text stored before FS projection existed."""
    meta = doc.metadata_ if isinstance(doc.metadata_, dict) else {}
    for key in ("content", "content_text"):
        value = meta.get(key)
        if isinstance(value, str):
            return value
    return None


class _EditorHtmlToDocgenText(HTMLParser):
    """Convert lightweight contentEditable HTML into docgen markdown-ish text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._inline_marks: list[str] = []
        self._skip_depth = 0

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if self._skip_depth:
            self._skip_depth += 1
            return
        attrs_dict = {str(k).lower(): str(v) for k, v in attrs}
        if attrs_dict.get("data-docx-page-break") == "true":
            self._newline()
            self.parts.append("[[PAGE_BREAK]]")
            self._newline()
            self._skip_depth = 1
            return
        if tag in {"p", "div", "section", "article", "blockquote"}:
            self._newline()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._newline()
            level = int(tag[1])
            self.parts.append("#" * level + " ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self._newline()
            self.parts.append("- ")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
            self._inline_marks.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
            self._inline_marks.append("*")
        elif tag == "u":
            self.parts.append("[[U]]")
            self._inline_marks.append("[[/U]]")
        elif tag in {"s", "strike", "del"}:
            self.parts.append("[[S]]")
            self._inline_marks.append("[[/S]]")
        elif tag == "span":
            style = attrs_dict.get("style", "").lower()
            closers: list[str] = []
            if "font-weight" in style and ("bold" in style or "700" in style or "800" in style or "900" in style):
                self.parts.append("**")
                closers.append("**")
            if "font-style" in style and "italic" in style:
                self.parts.append("*")
                closers.append("*")
            if "text-decoration" in style and "underline" in style:
                self.parts.append("[[U]]")
                closers.append("[[/U]]")
            if "text-decoration" in style and ("line-through" in style or "strike" in style):
                self.parts.append("[[S]]")
                closers.append("[[/S]]")
            self._inline_marks.append("".join(reversed(closers)))
        elif tag in {"td", "th"}:
            if self.parts and not self.parts[-1].endswith(("| ", "\n")):
                self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in {"strong", "b", "em", "i", "u", "s", "strike", "del", "span"} and self._inline_marks:
            closing = self._inline_marks.pop()
            if closing:
                self.parts.append(closing)
        elif tag in {"p", "div", "section", "article", "blockquote", "li"}:
            self._newline()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self._newline()
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = []
        for line in raw.splitlines():
            cleaned = " ".join(line.split())
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines).strip()


def _editor_html_to_docgen_text(content: str) -> str:
    if "<" not in content or ">" not in content:
        return content
    parser = _EditorHtmlToDocgenText()
    parser.feed(content)
    parser.close()
    return parser.text() or content


def _allocate_document_fs_path(doc: Document, entity_root: str) -> str:
    """Allocate a visible relative path for a legacy metadata-only document."""
    raw_name = normalize_rel_path(doc.name or "")
    filename = os.path.basename(raw_name) or f"document-{doc.id}.txt"
    if "." not in filename and doc.file_type:
        filename = f"{filename}.{str(doc.file_type).lstrip('.')}"
    if not is_user_visible_path(filename):
        ext = f".{str(doc.file_type).lstrip('.')}" if doc.file_type else ".txt"
        filename = f"document-{doc.id}{ext}"

    os.makedirs(entity_root, exist_ok=True)
    root_norm = os.path.normpath(entity_root)
    target = os.path.normpath(os.path.join(entity_root, filename))
    if os.path.commonpath([root_norm, target]) != root_norm:
        raise ValueError("Document path escaped entity root")
    if os.path.exists(target):
        base, ext = os.path.splitext(filename)
        target = os.path.join(entity_root, f"{base}_{doc.id[:8]}{ext}")
    return os.path.relpath(target, entity_root)

async def get_document_content(
    db: AsyncSession, document_id: str, entity_id: str, *, allow_filesystem: bool = True,
) -> str | None:
    """Read content, optionally restricting fallback to the authorized DB snapshot."""
    doc = await get_document(db, document_id, entity_id)
    if not doc:
        return None
    from packages.core.services.stickman_topic_ledger import (
        render_topic_ledger_markdown,
        topic_ledger_workspace_id_for_document,
    )
    ledger_workspace_id = topic_ledger_workspace_id_for_document(doc)
    if ledger_workspace_id:
        return await render_topic_ledger_markdown(
            entity_id=entity_id,
            workspace_id=ledger_workspace_id,
        )
    if not doc.fs_path or not allow_filesystem:
        return _metadata_text_content(doc)
    from packages.core.config import get_settings
    fs_root = get_settings().MANOR_FS_ROOT
    full_path = os.path.join(fs_root, doc.entity_id, doc.fs_path)

    def _read():
        if not os.path.isfile(full_path):
            return None
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    content = await asyncio.to_thread(_read)
    if content is not None:
        return content
    return _metadata_text_content(doc)


def _is_office_binary_document(doc: Document) -> bool:
    from packages.core.services.office_editing import OfficeConversionFactory

    return OfficeConversionFactory.resolve_format(
        doc.name or "",
        source_format=doc.file_type,
        source_mime=doc.mime_type,
    ) is not None


def _require_text_content_save(doc: Document) -> None:
    if _is_office_binary_document(doc):
        raise ValueError(
            "Office documents must be saved as complete binary files",
        )


async def save_document_content(
    db: AsyncSession, document_id: str, entity_id: str, content: str,
    *,
    created_by: str | None = None,
    save_session_id: str | None = None,
    save_sequence: int | None = None,
    mutation_authorizer: (
        Callable[[AsyncSession, Document], Awaitable[None]] | None
    ) = None,
) -> bool:
    """Save text content to the filesystem and create a version."""
    from packages.core.config import get_settings
    settings = get_settings()
    if (save_session_id is None) != (save_sequence is None):
        raise ValueError("save_session_id and save_sequence must be provided together")
    intent_content = _document_content_write_intent(content)

    if not settings.MANOR_FS_ENABLED:
        doc = await get_document(
            db,
            document_id,
            entity_id,
            for_filesystem_mutation=True,
            mutation_authorizer=mutation_authorizer,
        )
        if not doc:
            return False
        _require_text_content_save(doc)
        if doc.fs_path:
            raise ValueError("Filesystem storage is not enabled")
        meta = dict(doc.metadata_ or {})
        if save_session_id is not None and save_sequence is not None:
            claim_status, meta = _claim_metadata_editor_write_intent(
                meta,
                session_id=save_session_id,
                sequence=save_sequence,
                content=intent_content,
                compatible_legacy_digests=(
                    _compatible_legacy_document_content_digests(content)
                ),
            )
            if claim_status is EditorWriteIntentStatus.STALE:
                raise EntityFilesystemStaleWriteError(
                    "A newer save already exists for this file",
                )
            if claim_status is EditorWriteIntentStatus.REPLAYED:
                return True
        meta["content"] = content
        doc.metadata_ = meta
        doc.file_size = len(content.encode("utf-8"))
        await db.flush()
        from packages.core.services.version_service import create_version
        await create_version(db, document_id, entity_id, change_summary="Edited", created_by=created_by)
        await db.commit()
        await bump_tool_cache_version(entity_id, "documents")
        return True

    fs_root = settings.MANOR_FS_ROOT
    entity_root = os.path.join(fs_root, entity_id)
    from packages.core.services.entity_fs import (
        assert_entity_filesystem_ready,
        claim_entity_editor_write_intent,
        entity_filesystem_mutation_lock,
        finish_entity_filesystem_mutation,
        write_entity_file_atomic,
    )
    assert_entity_filesystem_ready()

    async with entity_filesystem_mutation_lock(entity_root):
        doc = await get_document(
            db,
            document_id,
            entity_id,
            for_filesystem_mutation=True,
            mutation_authorizer=mutation_authorizer,
        )
        if not doc:
            return False
        _require_text_content_save(doc)
        if not doc.fs_path:
            doc.fs_path = _allocate_document_fs_path(doc, entity_root)
        intent_digest = hashlib.sha256(intent_content).hexdigest()
        persisted_bytes = content.encode("utf-8")
        pending_metadata: dict | None = None
        path_receipt_replayed = False

        if save_session_id is not None and save_sequence is not None:
            compatible_legacy_digests = set(
                _compatible_legacy_document_content_digests(content)
            )
            previous = _metadata_editor_write_session(
                doc.metadata_,
                save_session_id,
            )
            previous_digest = previous.get("content_digest") if previous else None
            metadata_status, metadata = _claim_metadata_editor_write_intent(
                doc.metadata_,
                session_id=save_session_id,
                sequence=save_sequence,
                content=intent_content,
                compatible_legacy_digests=frozenset(compatible_legacy_digests),
            )
            if metadata_status is EditorWriteIntentStatus.STALE:
                raise EntityFilesystemStaleWriteError(
                    "A newer save already exists for this file",
                )
            if metadata_status is EditorWriteIntentStatus.REPLAYED:
                if isinstance(previous_digest, str):
                    await _repair_entity_editor_write_receipt(
                        entity_root,
                        doc.fs_path,
                        save_session_id,
                        save_sequence,
                        previous_digest,
                        document_id=document_id,
                    )
                return True
            pending_metadata = metadata

        if save_session_id is not None and save_sequence is not None:
            claim_result = await asyncio.to_thread(
                claim_entity_editor_write_intent,
                entity_root,
                doc.fs_path,
                save_session_id,
                save_sequence,
                intent_content,
            )
            if claim_result.status is EditorWriteIntentStatus.STALE:
                raise EntityFilesystemStaleWriteError(
                    "A newer save already exists for this file",
                )
            if claim_result.status is EditorWriteIntentStatus.REPLAYED:
                path_receipt_replayed = True

        if pending_metadata is not None:
            doc.metadata_ = pending_metadata

        if path_receipt_replayed:
            await db.flush()
            await db.commit()
            await bump_tool_cache_version(entity_id, "documents")
            return True

        doc.file_size = len(persisted_bytes)

        async def persist_and_commit() -> bool:
            await asyncio.to_thread(
                write_entity_file_atomic,
                doc.entity_id,
                doc.fs_path,
                persisted_bytes,
                expected_size=len(persisted_bytes),
                allow_empty=True,
            )

            doc.updated_at = datetime.now(timezone.utc)
            await db.flush()
            from packages.core.services.version_service import create_version
            await create_version(
                db,
                document_id,
                entity_id,
                change_summary="Edited",
                created_by=created_by,
            )
            await db.commit()
            await _invalidate_document_preview_versions(entity_root, document_id)
            await bump_tool_cache_version(entity_id, "documents")
            if save_session_id is not None and save_sequence is not None:
                await _repair_entity_editor_write_receipt(
                    entity_root,
                    doc.fs_path,
                    save_session_id,
                    save_sequence,
                    intent_digest,
                    document_id=document_id,
                )
            return True

        return await finish_entity_filesystem_mutation(persist_and_commit())


async def save_document_file(
    db: AsyncSession,
    document_id: str,
    entity_id: str,
    file_bytes: bytes,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    created_by: str | None = None,
    save_session_id: str | None = None,
    save_sequence: int | None = None,
    expected_source_sha256: str | None = None,
    mutation_authorizer: (
        Callable[[AsyncSession, Document], Awaitable[None]] | None
    ) = None,
) -> DocumentFileSaveResult | None:
    """Replace a document's binary file content on disk."""
    from packages.core.config import get_settings
    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        raise ValueError("Filesystem storage is not enabled")
    if (save_session_id is None) != (save_sequence is None):
        raise ValueError("save_session_id and save_sequence must be provided together")
    expected_source_digest = str(expected_source_sha256 or "").strip().lower()
    if expected_source_digest and (
        len(expected_source_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_source_digest)
    ):
        raise ValueError("expected_source_sha256 must be a 64-character SHA-256 digest")

    from packages.core.services.office_editing import OfficeConversionFactory

    office_format = OfficeConversionFactory.format_from_mime(mime_type)
    office_extension = (
        office_format.value
        if office_format is not None and office_format.is_editable_ooxml
        else None
    )

    entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
    from packages.core.services.entity_fs import (
        EditorWriteIntentStatus,
        EntityFilesystemStaleWriteError,
        assert_entity_filesystem_ready,
        claim_entity_editor_write_intent,
        entity_filesystem_mutation_lock,
        finish_entity_filesystem_mutation,
        open_entity_file_snapshot,
        write_entity_file_atomic,
    )
    assert_entity_filesystem_ready()

    async with entity_filesystem_mutation_lock(entity_root):
        doc = await get_document(
            db,
            document_id,
            entity_id,
            for_filesystem_mutation=True,
            mutation_authorizer=mutation_authorizer,
        )
        if not doc:
            return None
        if not doc.fs_path:
            if filename:
                raw_name = normalize_rel_path(filename)
                visible_name = os.path.basename(raw_name)
                if visible_name and is_user_visible_path(visible_name):
                    doc.name = visible_name
            doc.fs_path = _allocate_document_fs_path(doc, entity_root)

        replaced_legacy_fs_path: str | None = None
        conversion_spec = OfficeConversionFactory.conversion_spec(
            doc.name or "",
            source_format=doc.file_type,
            source_mime=doc.mime_type,
        )
        if (
            office_format is not None
            and conversion_spec is not None
            and conversion_spec.target_format is office_format
        ):
            requested_name = conversion_spec.editable_filename(doc.name or "document")
            if is_user_visible_path(requested_name):
                old_fs_path = doc.fs_path
                target_directory = os.path.dirname(old_fs_path)
                target_fs_path = normalize_rel_path(os.path.join(target_directory, requested_name))
                target_full_path = os.path.realpath(os.path.join(entity_root, target_fs_path))
                root = os.path.realpath(entity_root)
                if os.path.commonpath([root, target_full_path]) != root:
                    raise ValueError("Document path escaped entity root")
                if target_fs_path != old_fs_path and os.path.exists(target_full_path):
                    stem, suffix = os.path.splitext(requested_name)
                    target_fs_path = normalize_rel_path(os.path.join(
                        target_directory,
                        f"{stem}_{doc.id[:8]}{suffix}",
                    ))
                    collision_index = 2
                    while os.path.exists(os.path.join(entity_root, target_fs_path)):
                        target_fs_path = normalize_rel_path(os.path.join(
                            target_directory,
                            f"{stem}_{doc.id[:8]}_{collision_index}{suffix}",
                        ))
                        collision_index += 1
                if target_fs_path != old_fs_path:
                    replaced_legacy_fs_path = old_fs_path
                    doc.fs_path = target_fs_path
                doc.name = requested_name

        full_path = os.path.realpath(os.path.join(entity_root, doc.fs_path))
        root = os.path.realpath(entity_root)
        if os.path.commonpath([root, full_path]) != root:
            raise ValueError("Document path escaped entity root")
        expected_source_version = None
        if expected_source_digest:
            accepted_source_digests = [expected_source_digest]
            if save_session_id is not None and save_sequence is not None:
                previous_session = _metadata_editor_write_session(
                    doc.metadata_,
                    save_session_id,
                )
                previous_sequence = (
                    previous_session.get("sequence")
                    if isinstance(previous_session, dict)
                    else None
                )
                previous_digest = (
                    previous_session.get("content_digest")
                    if isinstance(previous_session, dict)
                    else None
                )
                if (
                    previous_session
                    and previous_session.get("committed") is True
                    and isinstance(previous_sequence, int)
                    and previous_sequence <= save_sequence
                    and isinstance(previous_digest, str)
                    and previous_digest not in accepted_source_digests
                ):
                    accepted_source_digests.append(previous_digest)

            stale_error: EntityFilesystemStaleWriteError | None = None
            source_fs_path = replaced_legacy_fs_path or doc.fs_path
            for accepted_digest in accepted_source_digests:
                try:
                    with open_entity_file_snapshot(
                        entity_id,
                        source_fs_path,
                        expected_content_sha256=accepted_digest,
                    ) as source_snapshot:
                        # A legacy Office save validates the old binary source,
                        # then writes a newly allocated OOXML path. The entity
                        # mutation lock fences that two-path transition; a
                        # same-path save also carries the version into the
                        # atomic writer to close the validation/write gap.
                        if source_fs_path == doc.fs_path:
                            expected_source_version = source_snapshot.version
                    stale_error = None
                    break
                except EntityFilesystemStaleWriteError as exc:
                    stale_error = exc
            if stale_error is not None:
                raise EntityFilesystemStaleWriteError(
                    "The document changed after this editor snapshot was loaded",
                ) from stale_error
        file_digest = hashlib.sha256(file_bytes).hexdigest()
        if save_session_id is not None and save_sequence is not None:
            claim_result = await asyncio.to_thread(
                claim_entity_editor_write_intent,
                entity_root,
                doc.fs_path,
                save_session_id,
                save_sequence,
                file_bytes,
            )
            if claim_result.status is EditorWriteIntentStatus.STALE:
                raise EntityFilesystemStaleWriteError(
                    "A newer save already exists for this file",
                )
            if claim_result.status is EditorWriteIntentStatus.REPLAYED:
                return DocumentFileSaveResult(document=doc, replayed=True)
            metadata_status, metadata = _claim_metadata_editor_write_intent(
                doc.metadata_,
                session_id=save_session_id,
                sequence=save_sequence,
                content=file_bytes,
            )
            if metadata_status is EditorWriteIntentStatus.STALE:
                raise EntityFilesystemStaleWriteError(
                    "A newer save already exists for this file",
                )
            if metadata_status is EditorWriteIntentStatus.REPLAYED:
                await _repair_entity_editor_write_receipt(
                    entity_root,
                    doc.fs_path,
                    save_session_id,
                    save_sequence,
                    file_digest,
                    document_id=document_id,
                )
                return DocumentFileSaveResult(document=doc, replayed=True)
            doc.metadata_ = metadata

        async def persist_and_commit() -> DocumentFileSaveResult:
            await asyncio.to_thread(
                write_entity_file_atomic,
                doc.entity_id,
                doc.fs_path,
                file_bytes,
                expected_size=len(file_bytes),
                allow_empty=True,
                expected_source_version=expected_source_version,
            )

            ext = office_extension or os.path.splitext(doc.name or filename or "")[1].lstrip(".").lower()
            doc.file_size = len(file_bytes)
            doc.file_type = ext or doc.file_type
            doc.mime_type = mime_type or doc.mime_type or "application/octet-stream"
            doc.vector_status = VectorStatus.PENDING
            doc.updated_at = datetime.now(timezone.utc)

            await db.flush()
            from packages.core.services.version_service import create_version
            await create_version(
                db,
                document_id,
                entity_id,
                change_summary="Edited file",
                created_by=created_by,
            )
            await db.commit()
            await _invalidate_document_preview_versions(entity_root, document_id)
            if replaced_legacy_fs_path:
                replaced_full_path = os.path.realpath(os.path.join(entity_root, replaced_legacy_fs_path))
                if os.path.commonpath([root, replaced_full_path]) == root:
                    try:
                        os.unlink(replaced_full_path)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.warning(
                            "Could not remove replaced legacy Office file %s",
                            replaced_legacy_fs_path,
                            exc_info=True,
                        )
            await bump_tool_cache_version(entity_id, "documents")
            if save_session_id is not None and save_sequence is not None:
                await _repair_entity_editor_write_receipt(
                    entity_root,
                    doc.fs_path,
                    save_session_id,
                    save_sequence,
                    file_digest,
                    document_id=document_id,
                )
            return DocumentFileSaveResult(document=doc, replayed=False)

        return await finish_entity_filesystem_mutation(persist_and_commit())
