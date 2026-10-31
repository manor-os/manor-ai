"""Verify a Workspace Knowledge media document and inspect its pinned bytes."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


class WorkspaceDocumentMediaError(ValueError):
    """The selected Knowledge media is not available to this Workspace actor."""


# A wrong expected_source_sha256 is the caller's to fix, not a missing file.
_STALE_SHA256_MESSAGE = (
    "The Knowledge media has changed since expected_source_sha256 was read, or that hash belongs to "
    "another file. Use the source_sha256 that read_file or probe_media returned for this exact "
    "document, or omit expected_source_sha256."
)


async def readable_knowledge_document_media_path(
    db: Any,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    document_id: str,
    tool_name: str,
    expected_path: str | None = None,
) -> str:
    """Check an exact Document in the actor's own active ordinary chat."""
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.task import Conversation
    from packages.core.services.ai_file_permissions import guard_ai_file_read_access
    from packages.core.services.file_refs import is_chat_upload_storage_path
    from packages.core.services.knowledge_visibility import normalize_rel_path

    unavailable = "The Knowledge media is unavailable or no longer readable."
    if not all((entity_id, user_id, conversation_id, document_id, tool_name)):
        raise WorkspaceDocumentMediaError(unavailable)
    conversation = await db.scalar(select(Conversation.id).where(
        Conversation.id == conversation_id,
        Conversation.entity_id == entity_id,
        Conversation.user_id == user_id,
        Conversation.workspace_id.is_(None),
        Conversation.scope == "channel",
        Conversation.status == "active",
    ))
    if not conversation:
        raise WorkspaceDocumentMediaError(unavailable)
    document = await db.scalar(select(Document).where(
        Document.id == document_id,
        Document.entity_id == entity_id,
        Document.is_trashed == False,  # noqa: E712
    ))
    raw_path = str(getattr(document, "fs_path", "") or "")
    path = normalize_rel_path(raw_path)
    if (
        not document or not path or path != raw_path or is_chat_upload_storage_path(path)
        or (expected_path is not None and path != expected_path)
    ):
        raise WorkspaceDocumentMediaError(unavailable)
    denied = await guard_ai_file_read_access(
        entity_id=entity_id, user_id=user_id, conversation_id=conversation_id,
        workspace_id=None, tool_name=tool_name, paths=[path],
    )
    if denied:
        raise WorkspaceDocumentMediaError(unavailable)
    return path


async def inspect_knowledge_document_media(
    reference: Any,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    workspace_id: str | None,
    tool_name: str,
    allowed_suffixes: set[str],
    max_bytes: int,
    expected_source_sha256: str | None = None,
    inspect_snapshot: Callable[[str], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pin an authorized ordinary-chat Document; Workspace sources stay separate."""
    if (
        not isinstance(reference, dict)
        or set(reference) != {"source_kind", "document_id"}
        or reference.get("source_kind") != "knowledge_document"
    ):
        raise WorkspaceDocumentMediaError("Use an exact Knowledge document reference.")
    document_id = str(reference.get("document_id") or "").strip()
    if workspace_id is not None or not all((entity_id, user_id, conversation_id, document_id, tool_name)):
        raise WorkspaceDocumentMediaError("Knowledge media requires the current user's ordinary chat.")
    from packages.core.database import async_session
    from packages.core.services.entity_fs import (
        EntityFilesystemError, EntityFilesystemStaleWriteError, canonical_entity_root, entity_filesystem_read_lock,
        is_fs_enabled, open_entity_file_snapshot,
    )

    if not is_fs_enabled():
        raise WorkspaceDocumentMediaError("The Knowledge filesystem is unavailable.")
    try:
        async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
            async with async_session() as db:
                path = await readable_knowledge_document_media_path(
                    db, entity_id=entity_id, user_id=user_id,
                    conversation_id=conversation_id, document_id=document_id,
                    tool_name=tool_name,
                )
            if Path(path).suffix.lower() not in allowed_suffixes:
                raise WorkspaceDocumentMediaError("The selected Knowledge document has an unsupported media type.")
            with open_entity_file_snapshot(entity_id, path, expected_content_sha256=expected_source_sha256) as snapshot:
                if not 0 < snapshot.stat.st_size <= max_bytes:
                    raise WorkspaceDocumentMediaError("The Knowledge media has an invalid size.")
                digest = snapshot.content_sha256
                if not digest:
                    def digest_file(descriptor_path: str) -> str:
                        with open(descriptor_path, "rb") as source:
                            value = hashlib.file_digest(source, "sha256").hexdigest()
                            source.seek(0)
                            return value
                    digest = await asyncio.to_thread(digest_file, snapshot.descriptor_path)
                if expected_source_sha256 is not None and digest != expected_source_sha256:
                    raise WorkspaceDocumentMediaError("The Knowledge media has changed.")
                details = inspect_snapshot(snapshot.descriptor_path) if inspect_snapshot else {}
                if inspect.isawaitable(details):
                    details = await details
                return {
                    "source_kind": "knowledge_document", "document_id": document_id,
                    "rel_path": path, "source_sha256": digest,
                    "size_bytes": snapshot.stat.st_size, **dict(details or {}),
                }
    except WorkspaceDocumentMediaError:
        raise
    except EntityFilesystemStaleWriteError as exc:
        raise WorkspaceDocumentMediaError(_STALE_SHA256_MESSAGE) from exc
    except (EntityFilesystemError, OSError) as exc:
        raise WorkspaceDocumentMediaError("The Knowledge media is unavailable or has changed.") from exc


async def inspect_workspace_document_media(
    reference: Any,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str | None,
    workspace_id: str,
    tool_name: str,
    allowed_suffixes: set[str],
    max_bytes: int,
    expected_source_sha256: str | None = None,
    inspect_snapshot: Callable[[str], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve exactly one readable Workspace document and pin its current bytes."""
    if (
        not isinstance(reference, dict)
        or set(reference) != {"source_kind", "document_id"}
        or reference.get("source_kind") != "workspace_document"
    ):
        raise WorkspaceDocumentMediaError("Use an exact Workspace Knowledge document reference.")
    document_id = str(reference.get("document_id") or "").strip()
    if not all((entity_id, user_id, workspace_id, tool_name, document_id)):
        raise WorkspaceDocumentMediaError("Workspace media requires an active Workspace and user.")

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Workspace
    from packages.core.services.ai_file_permissions import guard_ai_file_read_access, is_chat_upload_path
    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        EntityFilesystemStaleWriteError,
        canonical_entity_root,
        entity_filesystem_read_lock,
        is_fs_enabled,
        open_entity_file_snapshot,
    )
    from packages.core.services.knowledge_visibility import normalize_rel_path

    if not is_fs_enabled():
        raise WorkspaceDocumentMediaError("The Workspace Knowledge filesystem is unavailable.")
    async with async_session() as db:
        document = (await db.execute(select(Document).where(
            Document.id == document_id,
            Document.entity_id == entity_id,
            Document.is_trashed == False,  # noqa: E712
        ))).scalar_one_or_none()
        # Scheduled Workflows can read Knowledge without a Chat conversation.
        # A supplied conversation still has to belong to this active scope.
        conversation = (await db.execute(select(Conversation.id).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Conversation.status == "active",
        ))).scalar_one_or_none() if conversation_id else None
        workspace = (await db.execute(select(Workspace.id).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.status.in_(("active", "paused")),
            Workspace.deleted_at.is_(None),
        ))).scalar_one_or_none()
    path = normalize_rel_path(str(getattr(document, "fs_path", "") or ""))
    if (
        not document or (conversation_id and not conversation) or not workspace
        or not path or is_chat_upload_path(path)
    ):
        raise WorkspaceDocumentMediaError("The Workspace Knowledge media is unavailable or no longer readable.")
    if Path(path).suffix.lower() not in allowed_suffixes:
        raise WorkspaceDocumentMediaError("The selected Workspace document has an unsupported media type.")

    try:
        async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
            denied = await guard_ai_file_read_access(
                entity_id=entity_id, user_id=user_id, conversation_id=conversation_id,
                workspace_id=workspace_id, tool_name=tool_name, paths=[path],
            )
            if denied:
                raise WorkspaceDocumentMediaError("The Workspace Knowledge media is no longer readable.")
            with open_entity_file_snapshot(entity_id, path, expected_content_sha256=expected_source_sha256) as snapshot:
                if not 0 < snapshot.stat.st_size <= max_bytes:
                    raise WorkspaceDocumentMediaError("The Workspace Knowledge media has an invalid size.")
                digest = snapshot.content_sha256
                if not digest:
                    def digest_file(descriptor_path: str) -> str:
                        with open(descriptor_path, "rb") as source:
                            digest = hashlib.file_digest(source, "sha256").hexdigest()
                            # /dev/fd on macOS shares the snapshot's file offset.
                            source.seek(0)
                            return digest
                    digest = await asyncio.to_thread(digest_file, snapshot.descriptor_path)
                if expected_source_sha256 is not None and digest != expected_source_sha256:
                    raise WorkspaceDocumentMediaError("The Workspace Knowledge media has changed.")
                details = inspect_snapshot(snapshot.descriptor_path) if inspect_snapshot else {}
                if inspect.isawaitable(details):
                    details = await details
                return {
                    "source_kind": "workspace_document", "document_id": document_id,
                    "rel_path": path, "source_sha256": digest,
                    "size_bytes": snapshot.stat.st_size, **dict(details or {}),
                }
    except WorkspaceDocumentMediaError:
        raise
    except EntityFilesystemStaleWriteError as exc:
        raise WorkspaceDocumentMediaError(_STALE_SHA256_MESSAGE) from exc
    except (EntityFilesystemError, OSError) as exc:
        raise WorkspaceDocumentMediaError("The Workspace Knowledge media is unavailable or has changed.") from exc
