"""Document endpoints — CRUD, groups, file upload/download."""
from __future__ import annotations

import asyncio
import heapq
import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.models.document import Document, DocumentFolder, DocumentGroup, VectorStatus
from packages.core.config import get_settings
from packages.core.services.document_service import (
    DocumentMutationConflictError,
    create_document, delete_document,
    rename_document,
    list_groups, create_group, add_document_to_group, add_documents_to_group,
    create_workspace_knowledge_group, mark_workspace_knowledge_changed,
    trigger_reindex,
    get_document_content, get_document_for_update,
    save_document_content, save_document_file,
    upsert_document_by_fs_path,
)
from packages.core.services.document_access import (
    DocumentAccessContext,
    ResourcePolicyMutationConflictError,
    document_is_owned_by_deleted_workspace,
    effective_document_capabilities_for_user,
    folder_grant_capabilities_for_user,
    get_visible_document,
    list_visible_documents,
    lock_folder_policy_boundaries,
    partition_documents_by_capability,
    user_has_document_capability,
    user_has_folder_capability,
    user_can_read_folder,
    visible_document_counts_by_folder,
    visible_storage_usage,
)
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    readable_workspace_ids_for_user,
    user_can_manage_workspace,
)
from packages.core.services.version_service import (
    create_version, list_versions,
    DocumentRestoreConflict,
    trash_document, restore_document, list_trash, empty_trash,
)
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.entity_fs import (
    EntityFilesystemBusyError,
    EntityFilesystemError,
    EntityFilesystemStaleWriteError,
)
from packages.core.services.document_ai_draft import generate_document_ai_draft_content
from packages.core.services.knowledge_hot_cache import (
    cache_document_blob,
    cache_document_blob_from_path,
    cache_document_text,
    document_hot_cache_key,
    get_cached_document_blob,
    get_cached_document_text,
)
from packages.core.services.stickman_topic_ledger import (
    topic_ledger_workspace_id_for_document,
)
from packages.core.services.actor_authorization import AuthenticatedUserCredential
from packages.core.services.permission_gate import (
    DocumentDownloadNotAllowed,
    ResourcePermissionGate,
)
from packages.core.ai.runtime import runtime_text_completion_platform_configured
from apps.api.deps import enforce_plan_resource, get_current_user, require_plan
from apps.api.errors import CodedError
from apps.api.file_responses import EntitySnapshotFileResponse, entity_filesystem_read_boundary
from packages.core.models.permission import Capability
from packages.core.permissions import (
    Permission,
    check_effective_user_permission,
    effective_user_has_permission,
    user_is_effective_entity_admin,
)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
settings = get_settings()
logger = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STALE_FILE_INTEGRITY_STATUSES = {"missing", "invalid_path", "unavailable", "error"}
USER_ROOT_DOCUMENT_DEFAULT_VISIBILITY = "private"
AI_DRAFT_CANCELLED_BY_TRASH = "ai_draft_generation_cancelled_by_trash"


def _is_unrecoverable_ai_draft(doc: Document) -> bool:
    metadata = doc.metadata_ if isinstance(doc.metadata_, dict) else {}
    return (
        doc.source == "ai-draft"
        and not doc.fs_path
        and not metadata.get("content_text")
    )


class DocumentResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    fs_path: str | None = None
    display_path: str | None = None
    file_size: int | None = None
    file_type: str | None = None
    mime_type: str | None = None
    source: str = "upload"
    vector_status: str = VectorStatus.PENDING
    indexing_progress: dict | None = None
    created_by: str | None = None
    folder_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # ── Permission-v1 fields (see docs/PERMISSIONS_DESIGN_ZH.md §13) ─────
    visibility: str | None = None
    classification: str | None = None
    owner_id: str | None = None
    client_visible: bool | None = None
    pii_detected: bool | None = None
    quarantine_status: str | None = None
    editor_recipe_document_id: str | None = None
    editor_recipe_path: str | None = None
    editor_recipe_name: str | None = None
    current_user_capabilities: list[str] = Field(default_factory=list)


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    # Recursive totals for the current location — include files nested in
    # subfolders, not just the direct children on this page.
    total_files: int = 0
    total_size: int = 0
    # Entity-wide plan storage status, so the UI can warn/disable "add" actions
    # before the user hits a 402. ``storage_limit_mb`` is null when unlimited.
    storage_used_mb: float | None = None
    storage_limit_mb: float | None = None


class DocumentIndexingStatusResponse(BaseModel):
    """Small polling payload for Knowledge indexing progress."""

    id: str
    vector_status: str
    indexing_progress: dict | None = None


class DocumentGroupResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    workspace_id: str | None = None


class DocumentVersionResponse(BaseModel):
    id: str
    document_id: str
    version_number: int
    name: str
    fs_path: str | None = None
    file_size: int | None = None
    change_summary: str | None = None
    created_by: str | None = None
    created_at: str | None = None


class CreateVersionRequest(BaseModel):
    change_summary: str | None = None


class CreateGroupRequest(BaseModel):
    name: str
    workspace_id: str | None = None


class SaveContentRequest(BaseModel):
    content: str
    save_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    save_sequence: int | None = Field(default=None, ge=1, le=9_007_199_254_740_991)

    @model_validator(mode="after")
    def validate_save_intent(self):
        if (self.save_session_id is None) != (self.save_sequence is None):
            raise ValueError("save_session_id and save_sequence must be provided together")
        return self


class RenameDocumentRequest(BaseModel):
    name: str


_DOCUMENT_CAPABILITY_ORDER = [
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
    Capability.RECLASSIFY,
    Capability.DELETE,
    Capability.GRANT_ACCESS,
]
_DOCUMENT_OWNER_CAPABILITIES = set(_DOCUMENT_CAPABILITY_ORDER) - {Capability.UPLOAD_TO}
_FOLDER_OWNER_CAPABILITIES = set(_DOCUMENT_CAPABILITY_ORDER)


def _ordered_capabilities(capabilities: set[str]) -> list[str]:
    ordered = [capability for capability in _DOCUMENT_CAPABILITY_ORDER if capability in capabilities]
    ordered.extend(sorted(capability for capability in capabilities if capability not in set(_DOCUMENT_CAPABILITY_ORDER)))
    return ordered


def _doc_resp(
    d,
    *,
    current_user_capabilities: set[str] | None = None,
    display_path: str | None = None,
) -> DocumentResponse:
    meta = d.metadata_ if hasattr(d, "metadata_") else None
    # Mutation handlers may return an ORM instance after a commit, where
    # ``updated_at`` is expired. Reading through ``__dict__`` avoids triggering
    # async lazy-loading while still exposing the value for fully loaded rows.
    loaded_updated_at = getattr(d, "__dict__", {}).get("updated_at")
    indexing = meta.get("indexing") if isinstance(meta, dict) else None
    artifact_meta = meta.get("artifact") if isinstance(meta, dict) else None
    generation_meta = meta.get("generation") if isinstance(meta, dict) else None
    artifact_meta = artifact_meta if isinstance(artifact_meta, dict) else {}
    generation_meta = generation_meta if isinstance(generation_meta, dict) else {}
    return DocumentResponse(
        id=d.id, entity_id=d.entity_id, name=d.name,
        fs_path=d.fs_path, display_path=display_path or d.fs_path, file_size=d.file_size,
        file_type=d.file_type, mime_type=d.mime_type,
        source=d.source, vector_status=d.vector_status,
        indexing_progress=indexing,
        created_by=d.created_by, folder_id=d.folder_id,
        created_at=d.created_at.isoformat() if d.created_at else None,
        updated_at=loaded_updated_at.isoformat() if loaded_updated_at else None,
        # ── Permission-v1 fields ─────────────────────────────────────────
        visibility=getattr(d, "visibility", None),
        classification=getattr(d, "classification", None),
        owner_id=getattr(d, "owner_id", None),
        client_visible=getattr(d, "client_visible", None),
        pii_detected=getattr(d, "pii_detected", None),
        quarantine_status=getattr(d, "quarantine_status", None),
        editor_recipe_document_id=artifact_meta.get("editor_recipe_document_id") or generation_meta.get("editor_recipe_document_id"),
        editor_recipe_path=artifact_meta.get("editor_recipe_path") or generation_meta.get("editor_recipe_path"),
        editor_recipe_name=artifact_meta.get("editor_recipe_name") or generation_meta.get("editor_recipe_name"),
        current_user_capabilities=_ordered_capabilities(current_user_capabilities or set()),
    )


async def _doc_resp_for_user(
    db: AsyncSession,
    d,
    user: User,
    access_ctx: DocumentAccessContext | None = None,
) -> DocumentResponse:
    if access_ctx is not None:
        capabilities = await access_ctx.effective_document_capabilities(db, d)
        can_manage = (
            access_ctx.is_admin
            or getattr(d, "owner_id", None) == user.id
            or getattr(d, "created_by", None) == user.id
        )
    else:
        capabilities = await effective_document_capabilities_for_user(
            db,
            document=d,
            user_id=user.id,
            role=user.role,
        )
        can_manage = await _can_manage_document(db, user, d)
    if can_manage:
        capabilities.update(_DOCUMENT_OWNER_CAPABILITIES)
    display_path = None
    from packages.core.services.workspace_artifacts import (
        artifact_folder_id_from_entity_storage_path,
        workspace_artifact_display_path,
    )
    artifact_folder_id = artifact_folder_id_from_entity_storage_path(
        getattr(d, "fs_path", None)
    )
    if artifact_folder_id:
        from packages.core.models.document import DocumentFolder

        folder_name = (await db.execute(
            select(DocumentFolder.name).where(
                DocumentFolder.id == artifact_folder_id,
                DocumentFolder.entity_id == d.entity_id,
            ).limit(1)
        )).scalar_one_or_none()
        display_path = workspace_artifact_display_path(
            d.fs_path,
            artifact_folder_id=artifact_folder_id,
            display_folder_name=folder_name,
        )
    return _doc_resp(
        d,
        current_user_capabilities=capabilities,
        display_path=display_path,
    )


async def _can_manage_document(db: AsyncSession, user: User, doc) -> bool:
    if await user_is_effective_entity_admin(db, user):
        return True
    if getattr(doc, "owner_id", None) == user.id:
        return True
    # Historical rows may store the immutable user id in created_by. Mutable
    # email/display-name audit labels must never grant authorization.
    return getattr(doc, "created_by", None) == user.id


async def _can_use_document_capability(
    db: AsyncSession,
    user: User,
    doc,
    capabilities: set[str],
) -> bool:
    if await document_is_owned_by_deleted_workspace(db, doc):
        return False
    if await _can_manage_document(db, user, doc):
        return True
    return await user_has_document_capability(
        db,
        document=doc,
        user_id=user.id,
        capabilities=capabilities,
    )


async def _can_use_document_capability_from_context(
    db: AsyncSession,
    user: User,
    doc,
    capabilities: set[str],
    access_ctx: DocumentAccessContext,
) -> bool:
    """Batched equivalent of ``_can_use_document_capability`` for lists."""
    if access_ctx.document_owned_by_deleted_workspace(doc):
        return False
    if (
        access_ctx.is_admin
        or getattr(doc, "owner_id", None) == user.id
        or getattr(doc, "created_by", None) == user.id
    ):
        return True
    granted = await access_ctx.effective_document_capabilities(db, doc)
    return bool(granted.intersection(capabilities))


async def _require_document_capability(
    db: AsyncSession,
    user: User,
    doc,
    capabilities: set[str],
    message: str,
) -> None:
    if await document_is_owned_by_deleted_workspace(db, doc, for_update=True):
        raise HTTPException(404, "Document not found")
    if not await _can_use_document_capability(db, user, doc, capabilities):
        raise HTTPException(403, message)


def _document_mutation_authorizer(
    user: User,
    capabilities: set[str],
    message: str,
):
    """Build the final authorization check run under document mutation locks."""

    async def authorize(db: AsyncSession, document) -> None:
        await _require_document_capability(
            db,
            user,
            document,
            capabilities,
            message,
        )

    return authorize


async def _can_delete_document(
    db: AsyncSession,
    user: User,
    doc,
) -> bool:
    if await effective_user_has_permission(db, user, Permission.DOCS_DELETE):
        return True
    return await _can_use_document_capability(
        db,
        user,
        doc,
        {Capability.DELETE},
    )


async def _require_document_delete(
    db: AsyncSession,
    user: User,
    doc,
    message: str,
) -> None:
    if await document_is_owned_by_deleted_workspace(db, doc, for_update=True):
        raise HTTPException(404, "Document not found")
    if not await _can_delete_document(db, user, doc):
        raise HTTPException(403, message)


async def _require_document_upload(
    db: AsyncSession,
    user: User,
) -> None:
    await check_effective_user_permission(db, user, Permission.DOCS_UPLOAD)


async def _can_manage_folder(
    db: AsyncSession,
    user: User,
    folder,
) -> bool:
    if await user_is_effective_entity_admin(db, user):
        return True
    return bool(getattr(folder, "owner_id", None) == user.id)


async def _require_folder_manager(
    db: AsyncSession,
    user: User,
    folder,
) -> None:
    try:
        locked_folders, workspaces = await lock_folder_policy_boundaries(
            db,
            entity_id=user.entity_id,
            folder_ids={folder.id},
        )
    except ResourcePolicyMutationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    if any(workspace.deleted_at is not None for workspace in workspaces):
        raise HTTPException(404, "Folder not found")
    if not await _can_manage_folder(db, user, locked_folders[folder.id]):
        raise HTTPException(403, "Only an owner/admin or the folder owner can modify this folder")


async def _require_folder_managers(
    db: AsyncSession,
    user: User,
    folders: list,
) -> dict[str, object]:
    try:
        locked_folders, workspaces = await lock_folder_policy_boundaries(
            db,
            entity_id=user.entity_id,
            folder_ids={folder.id for folder in folders},
        )
    except ResourcePolicyMutationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    if any(workspace.deleted_at is not None for workspace in workspaces):
        raise HTTPException(404, "Folder not found")
    for folder in locked_folders.values():
        if not await _can_manage_folder(db, user, folder):
            raise HTTPException(
                403,
                "Only an owner/admin or the folder owner can modify this folder",
            )
    return locked_folders


async def _can_use_folder_capability(
    db: AsyncSession,
    user: User,
    folder,
    capabilities: set[str],
) -> bool:
    if await _can_manage_folder(db, user, folder):
        return True
    return await user_has_folder_capability(
        db,
        entity_id=user.entity_id,
        folder_id=getattr(folder, "id", None),
        user_id=user.id,
        capabilities=capabilities,
    )


async def _require_folder_capability(
    db: AsyncSession,
    user: User,
    folder,
    capabilities: set[str],
    message: str,
) -> None:
    try:
        locked_folders, workspaces = await lock_folder_policy_boundaries(
            db,
            entity_id=user.entity_id,
            folder_ids={folder.id},
        )
    except ResourcePolicyMutationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    if any(workspace.deleted_at is not None for workspace in workspaces):
        raise HTTPException(404, "Folder not found")
    if not await _can_use_folder_capability(
        db,
        user,
        locked_folders[folder.id],
        capabilities,
    ):
        raise HTTPException(403, message)


async def _require_document_group_manager(
    db: AsyncSession,
    user: User,
    group_id: str,
) -> DocumentGroup:
    """Lock a Group and its active Workspace before changing membership."""
    group = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.id == group_id,
            DocumentGroup.entity_id == user.entity_id,
        ).limit(1)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Document group not found")
    workspace_id = str(group.workspace_id or "").strip()
    if workspace_id:
        workspace = await lock_workspace_access_boundary(
            db,
            workspace_id=workspace_id,
            entity_id=user.entity_id,
        )
        if workspace is None or workspace.deleted_at is not None:
            raise HTTPException(404, "Document group not found")
        if not await user_can_manage_workspace(
            db,
            workspace_id=workspace_id,
            user_id=user.id,
            entity_role=user.role,
        ):
            raise HTTPException(403, "Only Workspace owners/admins can modify this group")
    locked_group = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.id == group_id,
            DocumentGroup.entity_id == user.entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked_group is None or str(locked_group.workspace_id or "").strip() != workspace_id:
        raise HTTPException(409, "Document group changed during the request; please retry")
    return locked_group


def _mark_document_file_available(doc, *, source: str) -> bool:
    meta = doc.metadata_ if isinstance(getattr(doc, "metadata_", None), dict) else {}
    integrity = meta.get("file_integrity")
    if not isinstance(integrity, dict):
        return False

    status = str(integrity.get("status") or "").lower()
    if status not in STALE_FILE_INTEGRITY_STATUSES and integrity.get("recoverable") is not False:
        return False

    updated_meta = dict(meta)
    updated_integrity = dict(integrity)
    updated_integrity.update(
        {
            "status": "ok",
            "resolved_from": source,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    updated_integrity.pop("recoverable", None)
    updated_integrity.pop("error", None)
    updated_meta["file_integrity"] = updated_integrity
    doc.metadata_ = updated_meta
    if doc.vector_status == VectorStatus.FAILED:
        doc.vector_status = VectorStatus.PENDING
    return True


def _safe_visible_filename(raw_name: str | None, default: str = "upload") -> str:
    """Return a basename that is safe to expose as a Knowledge document."""
    from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

    raw = normalize_rel_path(raw_name or default)
    if not raw or raw.startswith("../") or "/../" in f"/{raw}/":
        raise HTTPException(400, "Invalid filename")
    filename = os.path.basename(raw) or default
    if not is_user_visible_path(filename):
        raise HTTPException(400, "Cannot use hidden/system filename")
    return filename


def _safe_file_extension(raw_ext: str | None, default: str = "md") -> str:
    ext = (raw_ext or default).strip().lstrip(".").lower() or default
    if "/" in ext or "\\" in ext:
        raise HTTPException(400, "Invalid file type")
    return ext


_VIDEO_THUMB_EXTENSIONS = {"mp4", "webm", "ogg", "mov", "avi", "mkv"}
_VIDEO_THUMB_MIME_PREFIXES = ("video/",)
_IMAGE_THUMB_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}
_AUDIO_STREAM_EXTENSIONS = {"mp3", "wav", "ogg", "aac", "flac", "m4a", "wma"}


def _is_video_document(doc) -> bool:
    ext = (getattr(doc, "file_type", None) or os.path.splitext(getattr(doc, "name", "") or "")[1].lstrip(".")).lower()
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return ext in _VIDEO_THUMB_EXTENSIONS or any(mime.startswith(prefix) for prefix in _VIDEO_THUMB_MIME_PREFIXES)


def _is_image_document(doc) -> bool:
    ext = (getattr(doc, "file_type", None) or os.path.splitext(getattr(doc, "name", "") or "")[1].lstrip(".")).lower()
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return ext in _IMAGE_THUMB_EXTENSIONS or mime.startswith("image/") and mime != "image/svg+xml"


def _download_is_hot_cacheable(doc) -> bool:
    """Keep byte-range media on FileResponse/streaming paths."""
    ext = (getattr(doc, "file_type", None) or os.path.splitext(getattr(doc, "name", "") or "")[1].lstrip(".")).lower()
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return not (
        ext in _VIDEO_THUMB_EXTENSIONS
        or ext in _AUDIO_STREAM_EXTENSIONS
        or mime.startswith("video/")
        or mime.startswith("audio/")
    )


_GENERIC_DOCUMENT_FILE_TYPES = {"file", "document", "spreadsheet", "presentation"}


def _document_ext(doc) -> str:
    name_ext = os.path.splitext(getattr(doc, "name", "") or "")[1].lstrip(".").lower()
    file_type = (getattr(doc, "file_type", None) or "").lstrip(".").lower()
    if file_type and file_type not in _GENERIC_DOCUMENT_FILE_TYPES:
        return file_type
    return name_ext or file_type


def _is_pptx_document(doc) -> bool:
    ext = _document_ext(doc)
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return ext in {"pptx", "ppt", "dps"} or mime in {
        PPTX_MIME,
        "application/vnd.ms-powerpoint",
    }


def _presentation_source_format(doc) -> str:
    """Prefer durable format metadata over a later display-name extension."""
    file_type = (getattr(doc, "file_type", None) or "").strip().lstrip(".").lower()
    if file_type in {"pptx", "ppt", "dps"}:
        return file_type
    name_ext = os.path.splitext(getattr(doc, "name", "") or "")[1].lstrip(".").lower()
    if name_ext in {"pptx", "ppt", "dps"}:
        return name_ext
    mime = (getattr(doc, "mime_type", None) or "").lower()
    if mime == PPTX_MIME:
        return "pptx"
    if mime == "application/vnd.ms-powerpoint":
        return "ppt"
    return file_type or name_ext


def _stream_open_file(handle):
    """Stream an already-open cache file and always release its descriptor."""
    try:
        while chunk := handle.read(64 * 1024):
            yield chunk
    finally:
        handle.close()


@dataclass(frozen=True)
class _DocumentSourceSnapshot:
    path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def _capture_document_source_snapshot(source_path: str) -> _DocumentSourceSnapshot:
    resolved_path = os.path.realpath(source_path)
    metadata = os.stat(resolved_path)
    return _DocumentSourceSnapshot(
        path=resolved_path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _document_source_sha256(source_path: str) -> str:
    digest = hashlib.sha256()
    with open(source_path, "rb") as source_file:
        while chunk := source_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _document_source_matches(
    snapshot: _DocumentSourceSnapshot,
    current_source_path: str | None,
) -> bool:
    if not current_source_path or os.path.realpath(current_source_path) != snapshot.path:
        return False
    try:
        return _capture_document_source_snapshot(current_source_path) == snapshot
    except OSError:
        return False


async def _run_thread_to_completion(function, *args):
    """Do not tear down temporary inputs while their worker still uses them."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


def _copy_open_file_to_path(source, target_path: str) -> None:
    source.seek(0)
    with open(target_path, "xb") as target:
        shutil.copyfileobj(source, target, length=64 * 1024)


def _publish_rendered_preview_version(
    cache_dir: str,
    paths: list[str],
    source_path: str,
) -> None:
    if not paths:
        raise RuntimeError("Office preview produced no pages")
    version = Path(paths[0]).parent.name
    from packages.core.services.slide_renderer import publish_current_preview_version

    publish_current_preview_version(cache_dir, version, source_path)


def _is_docx_document(doc) -> bool:
    ext = _document_ext(doc)
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return ext in {"docx", "doc", "wps"} or mime in {
        DOCX_MIME,
        "application/msword",
    }


def _entity_root(entity_id: str) -> str:
    return os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))


@asynccontextmanager
async def _document_filesystem_mutation(entity_id: str):
    """Keep file bytes and their committed Document ACL behind one boundary."""
    from packages.core.services.entity_fs import entity_filesystem_mutation_lock

    try:
        async with entity_filesystem_mutation_lock(_entity_root(entity_id)):
            yield
    except EntityFilesystemBusyError as exc:
        raise HTTPException(
            status_code=423,
            detail="Entity filesystem is busy with another mutation; retry shortly",
        ) from exc


async def _finish_document_filesystem_mutation(operation, *, release_result=None):
    from packages.core.services.entity_fs import finish_entity_filesystem_mutation

    return await finish_entity_filesystem_mutation(
        operation,
        release_result=release_result,
    )


def _release_document_preview_result(result, release_result) -> None:
    if result is None or release_result is None:
        return
    try:
        release_result(result)
    except Exception:
        logger.exception("Document preview result could not be released")


async def _document_preview_is_still_visible(
    db: AsyncSession,
    *,
    doc_id: str,
    entity_id: str,
    user_id: str,
    user_role: str,
) -> bool:
    """Recheck access and durably remove a cache recreated after deletion."""
    await db.rollback()
    current_doc = await get_visible_document(
        db,
        doc_id,
        entity_id,
        user_id=user_id,
        role=user_role,
    )
    if current_doc is not None:
        await db.rollback()
        return True

    document_exists = (await db.execute(
        select(Document.id).where(
            Document.id == doc_id,
            Document.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if document_exists is not None:
        # The document still exists but is now trashed or inaccessible. Do not
        # delete caches that remain valid for another authorized user.
        await db.rollback()
        return False

    from packages.core.services.workspace_artifact_purge import (
        document_derived_tree_cleanup_bases,
        drain_workspace_artifact_purge_jobs,
        enqueue_artifact_cleanup_jobs,
    )

    cleanup_bases = document_derived_tree_cleanup_bases({doc_id})
    await enqueue_artifact_cleanup_jobs(db, entity_id, cleanup_bases)
    await db.commit()
    await drain_workspace_artifact_purge_jobs(
        db,
        limit=len(cleanup_bases),
        entity_id=entity_id,
        storage_bases=cleanup_bases,
    )
    return False


async def _finish_document_preview(
    operation,
    *,
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    user_id: str,
    user_role: str,
    release_result=None,
    source_snapshot: _DocumentSourceSnapshot | None = None,
    accept_result=None,
):
    """Finish a renderer without an entity lock, then close its deletion race."""

    async def render_and_revalidate():
        result = None
        error: BaseException | None = None
        try:
            result = await operation
        except BaseException as exc:
            error = exc

        if error is None and source_snapshot is not None:
            try:
                async with _document_filesystem_mutation(entity_id):
                    current_doc = await get_visible_document(
                        db,
                        doc_id,
                        entity_id,
                        user_id=user_id,
                        role=user_role,
                    )
                    if current_doc is not None:
                        current_source_path = _document_full_path(current_doc, entity_id)
                        if not await asyncio.to_thread(
                            _document_source_matches,
                            source_snapshot,
                            current_source_path,
                        ):
                            raise HTTPException(
                                409,
                                {
                                    "code": "document_source_changed",
                                    "message": "Document changed while its preview was being prepared",
                                },
                            )
                        if accept_result is not None:
                            await asyncio.to_thread(accept_result, result)
                    await db.rollback()
            except BaseException:
                _release_document_preview_result(result, release_result)
                await _rollback_database_best_effort(db)
                raise

        try:
            still_visible = await _document_preview_is_still_visible(
                db,
                doc_id=doc_id,
                entity_id=entity_id,
                user_id=user_id,
                user_role=user_role,
            )
        except BaseException:
            _release_document_preview_result(result, release_result)
            raise

        if not still_visible:
            _release_document_preview_result(result, release_result)
            raise HTTPException(404, "Document not found")
        if error is not None:
            raise error
        return result

    # A renderer publishes content-addressed cache files. If the request is
    # cancelled, finish the render and post-check so a concurrent permanent
    # deletion cannot leave a newly recreated derived cache behind.
    return await _finish_document_filesystem_mutation(
        render_and_revalidate(),
        release_result=(
            (lambda result: _release_document_preview_result(result, release_result))
            if release_result is not None
            else None
        ),
    )


async def _rollback_database_best_effort(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        logger.exception("Document database rollback failed")


_DOCUMENT_CACHE_INVALIDATION_TIMEOUT_SECONDS = 2.0
_DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TIMEOUT_SECONDS = 30.0
_DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _consume_cache_invalidation_result(task: asyncio.Task) -> None:
    _DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TASKS.discard(task)
    try:
        task.result()
    except BaseException:
        pass


async def _finish_detached_cache_invalidation(task: asyncio.Task) -> None:
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise
    except Exception:
        pass


def _detach_cache_invalidation(task: asyncio.Task) -> None:
    if task.done():
        _consume_cache_invalidation_result(task)
        return
    waiter = asyncio.create_task(_finish_detached_cache_invalidation(task))
    _DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TASKS.add(waiter)
    waiter.add_done_callback(_consume_cache_invalidation_result)


async def _invalidate_committed_document_cache(entity_id: str) -> None:
    from packages.core.services.tool_cache_version import bump_tool_cache_version

    task = asyncio.create_task(bump_tool_cache_version(entity_id, "documents"))
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_DOCUMENT_CACHE_INVALIDATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _detach_cache_invalidation(task)
        logger.warning(
            "Committed document cache invalidation timed out for entity %s",
            entity_id,
        )
    except asyncio.CancelledError:
        _detach_cache_invalidation(task)
        raise
    except Exception:
        logger.warning(
            "Committed document cache invalidation failed for entity %s",
            entity_id,
            exc_info=True,
        )


async def _dispatch_document_embeddings_and_invalidate_cache(
    document_id: str,
    entity_id: str,
) -> None:
    """Dispatch committed embedding work before publishing the cache change."""
    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings

        process_document_embeddings.delay(document_id)
    except Exception:
        logger.warning(
            "Failed to dispatch document embedding task for %s",
            document_id,
            exc_info=True,
        )
    await _invalidate_committed_document_cache(entity_id)


async def _commit_document_and_dispatch_embeddings(
    db: AsyncSession,
    document_id: str,
    entity_id: str,
) -> None:
    """Commit a document before dispatching its embedding work."""
    await db.commit()
    await _run_committed_document_side_effects(document_id, entity_id)


async def _run_committed_document_side_effects(
    document_id: str,
    entity_id: str,
) -> None:
    """Run post-commit work without turning a durable write into a failure."""
    try:
        from packages.core.services.slide_renderer import invalidate_document_preview_versions

        await asyncio.to_thread(
            invalidate_document_preview_versions,
            _entity_root(entity_id),
            document_id,
        )
    except Exception:
        logger.warning(
            "Committed document preview invalidation failed for %s",
            document_id,
            exc_info=True,
        )
    try:
        await _dispatch_document_embeddings_and_invalidate_cache(
            document_id,
            entity_id,
        )
    except Exception:
        logger.warning(
            "Committed document post-processing failed for %s",
            document_id,
            exc_info=True,
        )


async def _load_committed_document_upload(
    *,
    document_id: str,
    entity_id: str,
    owner_id: str,
    idempotency_key: str | None,
    request_fingerprint: str,
    require_source_file: bool,
) -> Document | None:
    """Use a fresh transaction to resolve an ambiguous upload commit."""
    from packages.core import database as database_module

    async with database_module.async_session() as receipt_db:
        if idempotency_key is not None:
            document = await _find_idempotent_document_upload(
                receipt_db,
                entity_id=entity_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
            )
        else:
            document = await receipt_db.scalar(
                select(Document).where(
                    Document.id == document_id,
                    Document.entity_id == entity_id,
                    Document.owner_id == owner_id,
                )
            )
        if document is None:
            return None
        if idempotency_key is not None:
            await _validate_idempotent_document_upload(
                receipt_db,
                document,
                request_fingerprint=request_fingerprint,
            )
        if require_source_file:
            source_path = _document_full_path(document, entity_id)
            if source_path is None or not await asyncio.to_thread(os.path.isfile, source_path):
                logger.error(
                    "Committed upload %s has no readable source file",
                    document.id,
                )
                return None
        receipt_db.expunge(document)
        return document


class _DocumentUploadCommitUncertain(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=503,
            detail={
                "code": "document_upload_commit_uncertain",
                "message": "Upload completion is still being reconciled; retry with the same Idempotency-Key",
            },
        )


async def _commit_document_upload_with_reconciliation(
    db: AsyncSession,
    document: Document,
    *,
    entity_id: str,
    owner_id: str,
    idempotency_key: str | None,
    request_fingerprint: str,
    require_source_file: bool,
) -> Document:
    """Commit once, proving durable success before any upload cleanup."""
    committed_document = document
    document_id = document.id
    try:
        await db.commit()
    except IntegrityError:
        # Constraint failures are a definite database rejection. The caller
        # owns the normal idempotency-race lookup and source-file cleanup.
        await _rollback_database_best_effort(db)
        raise
    except Exception as commit_error:
        await _rollback_database_best_effort(db)
        reconciled = None
        # A successful COMMIT can become visible to a fresh connection shortly
        # after its acknowledgement is lost. A single negative read is not
        # proof of rollback, so use a small bounded visibility window before
        # reporting the outcome as ambiguous.
        for delay_seconds in (0.0, 0.05, 0.1, 0.2, 0.4):
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            try:
                reconciled = await _load_committed_document_upload(
                    document_id=document_id,
                    entity_id=entity_id,
                    owner_id=owner_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    require_source_file=require_source_file,
                )
            except Exception as reconciliation_error:
                logger.error(
                    "Could not reconcile document upload %s after commit failure",
                    document_id,
                    exc_info=True,
                )
                raise _DocumentUploadCommitUncertain() from ExceptionGroup(
                    "Upload commit and reconciliation both failed",
                    [commit_error, reconciliation_error],
                )
            if reconciled is not None:
                break
        if reconciled is None:
            logger.warning(
                "Document upload %s remained ambiguous after bounded commit reconciliation",
                document_id,
            )
            raise _DocumentUploadCommitUncertain() from commit_error
        committed_document = reconciled
        logger.warning(
            "Recovered committed document upload %s after commit acknowledgement failure",
            committed_document.id,
        )

    await _run_committed_document_side_effects(
        committed_document.id,
        entity_id,
    )
    return committed_document


async def _remove_or_quarantine_document_file(
    entity_id: str,
    fs_path: str | None,
) -> None:
    if not fs_path:
        return
    root = _entity_root(entity_id)
    target = os.path.realpath(os.path.join(root, fs_path))
    if os.path.commonpath([root, target]) != root:
        raise EntityFilesystemError("Document rollback path escaped the entity root")

    def _remove() -> None:
        try:
            os.remove(target)
            return
        except FileNotFoundError:
            return
        except OSError:
            quarantine_dir = os.path.join(
                root,
                ".ai",
                "document-rollbacks",
            )
            quarantine_path = os.path.join(
                quarantine_dir,
                f"{time.time_ns()}-{os.getpid()}.rollback",
            )
            try:
                os.makedirs(quarantine_dir, exist_ok=True)
                os.replace(target, quarantine_path)
            except OSError as exc:
                raise EntityFilesystemError(
                    "Could not remove or quarantine a rolled-back document file",
                ) from exc
            logger.warning(
                "Quarantined document file after rollback: %s",
                fs_path,
                exc_info=True,
            )

    await asyncio.to_thread(_remove)


async def _read_document_file_for_rollback(
    entity_id: str,
    fs_path: str,
) -> bytes | None:
    from packages.core.services.entity_fs import resolve_path

    full_path = resolve_path(entity_id, fs_path)
    if not full_path:
        raise EntityFilesystemError("Document rollback path is invalid")

    def _read() -> bytes | None:
        try:
            with open(full_path, "rb") as file:
                return file.read()
        except FileNotFoundError:
            return None

    return await asyncio.to_thread(_read)


async def _restore_document_file_after_failure(
    entity_id: str,
    fs_path: str,
    previous_content: bytes | None,
) -> None:
    if previous_content is None:
        await _remove_or_quarantine_document_file(entity_id, fs_path)
        return
    await _write_document_bytes_atomic(
        entity_id,
        fs_path,
        previous_content,
        allow_empty=True,
    )


async def _restore_trashed_document_file_after_failure(
    entity_id: str,
    *,
    original_fs_path: str | None,
    trashed_fs_path: str | None,
) -> None:
    """Move a file back when its matching soft-delete transaction fails."""
    if not original_fs_path or not trashed_fs_path:
        return

    from packages.core.services.entity_fs import resolve_path

    original = resolve_path(entity_id, original_fs_path)
    trashed = resolve_path(entity_id, trashed_fs_path)
    if not original or not trashed:
        raise EntityFilesystemError("Document trash rollback path is invalid")

    def _restore() -> None:
        if not os.path.isfile(trashed):
            return
        os.makedirs(os.path.dirname(original), exist_ok=True)
        os.replace(trashed, original)

    await asyncio.to_thread(_restore)


async def _retrash_restored_document_file_after_failure(
    entity_id: str,
    *,
    restored_fs_path: str | None,
    trashed_fs_path: str | None,
) -> None:
    """Move a restored file back when its database transaction fails."""
    if not restored_fs_path or not trashed_fs_path or restored_fs_path == trashed_fs_path:
        return

    from packages.core.services.entity_fs import resolve_path

    restored = resolve_path(entity_id, restored_fs_path)
    trashed = resolve_path(entity_id, trashed_fs_path)
    if not restored or not trashed:
        raise EntityFilesystemError("Document restore rollback path is invalid")

    def _retrash() -> None:
        if not os.path.isfile(restored):
            return
        os.makedirs(os.path.dirname(trashed), exist_ok=True)
        os.replace(restored, trashed)

    await asyncio.to_thread(_retrash)


def _document_full_path(doc, entity_id: str) -> str | None:
    if not getattr(doc, "fs_path", None):
        return None
    from packages.core.services.entity_fs import resolve_path

    return resolve_path(entity_id, str(doc.fs_path))


def _require_document_filesystem_ready() -> None:
    if not settings.MANOR_FS_ENABLED:
        return
    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        assert_entity_filesystem_ready,
    )

    try:
        assert_entity_filesystem_ready()
    except EntityFilesystemError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Document storage is temporarily unavailable: {exc}",
        ) from exc


def _unique_document_rel_path(entity_id: str, filename: str, *, rel_dir: str | None = None) -> str:
    entity_root = _entity_root(entity_id)
    os.makedirs(entity_root, exist_ok=True)
    root_norm = os.path.normpath(entity_root)
    candidate = _safe_visible_filename(filename, "document")
    target_dir = os.path.normpath(os.path.join(entity_root, rel_dir or ""))
    if os.path.commonpath([root_norm, target_dir]) != root_norm:
        raise HTTPException(400, "Invalid folder path")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.normpath(os.path.join(target_dir, candidate))
    if os.path.commonpath([root_norm, target]) != root_norm:
        raise HTTPException(400, "Invalid filename")
    if os.path.exists(target):
        if candidate.lower().endswith(".diagram.json"):
            base, ext = candidate[:-len(".diagram.json")], ".diagram.json"
        else:
            base, ext = os.path.splitext(candidate)
        stamp = int(time.time())
        candidate = f"{base}_{stamp}{ext}"
        target = os.path.normpath(os.path.join(target_dir, candidate))
        suffix = 1
        while os.path.exists(target):
            candidate = f"{base}_{stamp}_{suffix}{ext}"
            target = os.path.normpath(os.path.join(target_dir, candidate))
            suffix += 1
    return os.path.relpath(target, entity_root)


async def _workspace_storage_for_folder(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
):
    if not folder_id:
        return None
    from packages.core.services.workspace_artifacts import resolve_workspace_folder_binding

    return await resolve_workspace_folder_binding(
        db,
        entity_id=entity_id,
        folder_id=folder_id,
    )


async def _write_document_bytes_atomic(
    entity_id: str,
    rel_path: str,
    content: bytes,
    *,
    allow_empty: bool = True,
) -> str:
    from packages.core.services.entity_fs import EntityFilesystemError, write_entity_file_atomic

    try:
        abs_path = await asyncio.to_thread(
            write_entity_file_atomic,
            entity_id,
            rel_path,
            content,
            expected_size=len(content),
            allow_empty=allow_empty,
        )
    except EntityFilesystemError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Document storage is temporarily unavailable: {exc}",
        ) from exc
    return os.path.relpath(abs_path, _entity_root(entity_id))


async def _copy_document_file_atomic(
    entity_id: str,
    rel_path: str,
    source_path: str,
    *,
    expected_size: int,
    allow_empty: bool = True,
) -> str:
    from packages.core.services.entity_fs import EntityFilesystemError, copy_entity_file_atomic

    try:
        abs_path = await asyncio.to_thread(
            copy_entity_file_atomic,
            entity_id,
            rel_path,
            source_path,
            expected_size=expected_size,
            allow_empty=allow_empty,
        )
    except EntityFilesystemError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Document storage is temporarily unavailable: {exc}",
        ) from exc
    return os.path.relpath(abs_path, _entity_root(entity_id))


def _thumbnail_cache_path(entity_id: str, doc_id: str) -> str:
    root = _entity_root(entity_id)
    cache_dir = os.path.join(root, ".manor-cache", "document-thumbnails")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{doc_id}.jpg")


async def _generate_video_thumbnail(source_path: str, target_path: str) -> None:
    temp_path = f"{os.path.splitext(target_path)[0]}.tmp.jpg"
    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except OSError:
            pass

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0.5",
        "-i",
        source_path,
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2:force_original_aspect_ratio=decrease",
        "-q:v",
        "4",
        temp_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, "Video thumbnail generation is not available") from exc

    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise HTTPException(504, "Video thumbnail generation timed out") from exc

    if proc.returncode != 0 or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        detail = stderr.decode("utf-8", errors="ignore").strip()[:240] or "Could not generate video thumbnail"
        raise HTTPException(422, detail)

    os.replace(temp_path, target_path)


async def _download_remote_thumbnail_source(file_url: str, target_path: str) -> None:
    import aiofiles
    import httpx

    if not file_url.startswith(("http://", "https://")):
        raise HTTPException(404, "Remote file URL is not fetchable for thumbnail generation")

    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
    total = 0
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", file_url) as response:
                if response.status_code >= 400:
                    raise HTTPException(response.status_code, "Remote file is not fetchable for thumbnail generation")
                async with aiofiles.open(target_path, "wb") as fh:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise HTTPException(413, f"Remote file too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB")
                        await fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Remote file is not fetchable for thumbnail generation") from exc

    if total <= 0 or not os.path.exists(target_path) or os.path.getsize(target_path) == 0:
        raise HTTPException(404, "Remote file is empty")


async def _remote_document_stream_response(file_url: str, *, filename: str, media_type: str | None) -> StreamingResponse:
    import httpx

    if not file_url.startswith(("http://", "https://")):
        raise HTTPException(404, "Remote file URL is not fetchable")

    client = httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        request = client.build_request("GET", file_url)
        response = await client.send(request, stream=True)
    except Exception as exc:
        await client.aclose()
        raise HTTPException(502, "Remote file is not fetchable") from exc

    if response.status_code >= 400:
        await response.aclose()
        await client.aclose()
        raise HTTPException(response.status_code, "Remote file is not fetchable")

    safe_name = filename or "download"
    encoded_name = urllib.parse.quote(safe_name)

    async def iterator():
        try:
            async for chunk in response.aiter_bytes(1024 * 256):
                if chunk:
                    yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
        "Cache-Control": "private, max-age=300",
    }
    content_length = response.headers.get("content-length")
    if content_length:
        headers["Content-Length"] = content_length
    return StreamingResponse(
        iterator(),
        media_type=media_type or response.headers.get("content-type") or "application/octet-stream",
        headers=headers,
    )


# ── List + search (no path param — must be before /{doc_id}) ──

@router.get("", response_model=DocumentListResponse)
async def list_my_documents(
    search: str | None = Query(None),
    folder_id: str | None = Query(None),
    workspace_id: str | None = Query(None),
    include_generated_assets: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    search_text = (search or "").strip()
    search_query = search_text or None
    # Folder ids whose documents count toward the current location's storage:
    # the folder itself plus every descendant (None → the whole scope, e.g. the
    # root of the knowledge base or a workspace).
    storage_folder_ids: set[str] | None = None
    if folder_id not in (None, "", "root"):
        from packages.core.services.knowledge_visibility import is_user_visible_folder_path

        folders, folder_by_id = await _load_document_folders(db, user.entity_id)
        folder = folder_by_id.get(folder_id)
        if (
            not folder
            or not is_user_visible_folder_path(_folder_rel_path(folder, folder_by_id))
            or not await _user_can_read_folder_path(db, folder, folder_by_id, user)
        ):
            raise HTTPException(404, "Folder not found")
        storage_folder_ids = _folder_subtree_ids(folders, folder_id)
    list_folder_id = folder_id
    list_folder_ids: set[str] | None = None
    if search_query:
        if folder_id in ("", "root"):
            list_folder_id = None
        elif storage_folder_ids is not None:
            list_folder_id = None
            list_folder_ids = storage_folder_ids
    docs, total = await list_visible_documents(
        db, user.entity_id, name_search=search_query, folder_id=list_folder_id,
        folder_ids=list_folder_ids,
        workspace_id=workspace_id,
        user_id=user.id,
        role=user.role,
        include_generated_assets=include_generated_assets,
        limit=limit, offset=offset,
    )
    total_size, total_files = await visible_storage_usage(
        db, user.entity_id,
        user_id=user.id,
        role=user.role,
        name_search=search_query,
        folder_ids=storage_folder_ids,
        workspace_id=workspace_id,
        include_generated_assets=include_generated_assets,
    )
    # Entity-wide storage status (plan limit) for the "add" UI guard.
    from packages.core.services.plan_gate import check as _plan_check
    gate = await _plan_check(db, user.entity_id, "storage_mb")

    access_ctx = await DocumentAccessContext.load(
        db, entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    await access_ctx.preload_documents(db, docs)
    items = [await _doc_resp_for_user(db, d, user, access_ctx) for d in docs]
    return DocumentListResponse(
        items=items, total=total, total_files=total_files, total_size=total_size,
        storage_used_mb=gate.current, storage_limit_mb=gate.limit,
    )


# ── Upload (fixed path — before /{doc_id}) ──

_UPLOAD_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@dataclass(frozen=True)
class _DocumentUploadRecoveryIntent:
    rel_path: str
    fs_path: str
    owner_id: str = ""
    idempotency_key: str | None = None
    request_fingerprint: str = ""
    expires_at: float = 0.0


class _DocumentUploadRecoveryConflict(HTTPException):
    def __init__(self, message: str):
        super().__init__(409, message)


def _document_upload_recovery_intent_rel_path(
    owner_id: str,
    recovery_token: str,
) -> str:
    digest = hashlib.sha256(
        f"document-upload-v1\0{owner_id}\0{recovery_token}".encode("utf-8"),
    ).hexdigest()
    return os.path.join(
        ".ai",
        "document-upload-intents",
        digest[:2],
        f"{digest}.json",
    )


def _document_upload_recovery_ttl_seconds() -> int:
    raw = os.getenv("DOCUMENT_UPLOAD_RECOVERY_TTL_SECONDS", "86400")
    try:
        value = int(raw)
    except ValueError:
        value = 86400
    return max(3600, min(value, 7 * 86400))


def _document_upload_recovery_expiry(
    payload: dict[str, object],
    marker_mtime: float,
) -> float:
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        return float(expires_at)
    return marker_mtime + _document_upload_recovery_ttl_seconds()


async def _read_document_upload_recovery_payload(
    *,
    entity_id: str,
    intent_rel_path: str,
) -> tuple[dict[str, object], float] | None:
    from packages.core.services.entity_fs import open_entity_file_snapshot, resolve_path

    resolved = resolve_path(entity_id, intent_rel_path)
    if resolved is None:
        raise EntityFilesystemError("Upload recovery intent path is invalid")

    def _read() -> tuple[dict[str, object], float] | None:
        try:
            with open_entity_file_snapshot(entity_id, intent_rel_path) as snapshot:
                with open(snapshot.descriptor_path, "rb") as intent_file:
                    payload = json.load(intent_file)
                if not isinstance(payload, dict):
                    raise EntityFilesystemError("Upload recovery intent is malformed")
                return payload, float(snapshot.stat.st_mtime)
        except EntityFilesystemError:
            if not os.path.lexists(resolved):
                return None
            raise

    return await asyncio.to_thread(_read)


def _parse_document_upload_recovery_intent(
    *,
    entity_id: str,
    intent_rel_path: str,
    payload: dict[str, object],
    marker_mtime: float,
) -> _DocumentUploadRecoveryIntent:
    from packages.core.services.entity_fs import resolve_path

    version = payload.get("version")
    owner_id = payload.get("owner_id")
    idempotency_key = payload.get("idempotency_key")
    request_fingerprint = payload.get("request_fingerprint")
    fs_path = payload.get("fs_path")
    recovery_token = payload.get("recovery_token")
    if version == 1 and isinstance(idempotency_key, str):
        recovery_token = idempotency_key
    if (
        version not in {1, 2}
        or not isinstance(owner_id, str)
        or not owner_id
        or not (idempotency_key is None or isinstance(idempotency_key, str))
        or not isinstance(recovery_token, str)
        or not recovery_token
        or not isinstance(request_fingerprint, str)
        or not request_fingerprint
        or not isinstance(fs_path, str)
        or not fs_path
        or _document_upload_recovery_intent_rel_path(owner_id, recovery_token) != intent_rel_path
    ):
        raise EntityFilesystemError("Upload recovery intent is malformed")
    normalized_fs_path = fs_path.replace("\\", "/").lstrip("/")
    resolved_source = resolve_path(entity_id, normalized_fs_path)
    if resolved_source is None or normalized_fs_path.startswith(".ai/"):
        raise EntityFilesystemError("Upload recovery source path is invalid")
    return _DocumentUploadRecoveryIntent(
        rel_path=intent_rel_path,
        fs_path=normalized_fs_path,
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        expires_at=_document_upload_recovery_expiry(payload, marker_mtime),
    )


async def _has_active_document_upload_recovery_intent(
    *,
    entity_id: str,
    owner_id: str,
    idempotency_key: str | None,
) -> bool:
    if idempotency_key is None:
        return False
    intent_rel_path = _document_upload_recovery_intent_rel_path(owner_id, idempotency_key)
    try:
        loaded = await _read_document_upload_recovery_payload(
            entity_id=entity_id,
            intent_rel_path=intent_rel_path,
        )
        if loaded is None:
            return False
        payload, marker_mtime = loaded
        intent = _parse_document_upload_recovery_intent(
            entity_id=entity_id,
            intent_rel_path=intent_rel_path,
            payload=payload,
            marker_mtime=marker_mtime,
        )
    except (EntityFilesystemError, OSError, ValueError, json.JSONDecodeError):
        logger.warning(
            "Ignoring invalid document upload recovery intent %s",
            intent_rel_path,
            exc_info=True,
        )
        return False
    return (
        intent.owner_id == owner_id
        and intent.idempotency_key == idempotency_key
        and intent.expires_at > time.time()
    )


async def _load_document_upload_recovery_intent(
    *,
    entity_id: str,
    owner_id: str,
    idempotency_key: str | None,
    request_fingerprint: str,
) -> _DocumentUploadRecoveryIntent | None:
    if idempotency_key is None:
        return None
    intent_rel_path = _document_upload_recovery_intent_rel_path(
        owner_id,
        idempotency_key,
    )
    loaded = await _read_document_upload_recovery_payload(
        entity_id=entity_id,
        intent_rel_path=intent_rel_path,
    )
    if loaded is None:
        return None
    payload, marker_mtime = loaded
    try:
        intent = _parse_document_upload_recovery_intent(
            entity_id=entity_id,
            intent_rel_path=intent_rel_path,
            payload=payload,
            marker_mtime=marker_mtime,
        )
    except EntityFilesystemError as exc:
        raise _DocumentUploadRecoveryConflict(
            "Idempotency-Key conflicts with an incomplete document upload",
        ) from exc
    if (
        intent.owner_id != owner_id
        or intent.idempotency_key != idempotency_key
        or intent.request_fingerprint != request_fingerprint
    ):
        raise _DocumentUploadRecoveryConflict(
            "Idempotency-Key conflicts with an incomplete document upload",
        )
    if intent.expires_at <= time.time():
        raise _DocumentUploadRecoveryConflict(
            "This incomplete document upload has expired; start a new upload",
        )
    return intent


async def _create_document_upload_recovery_intent(
    *,
    entity_id: str,
    owner_id: str,
    idempotency_key: str | None,
    request_fingerprint: str,
    fs_path: str,
) -> _DocumentUploadRecoveryIntent | None:
    recovery_token = idempotency_key or (
        f"anonymous-{time.time_ns()}-{os.urandom(16).hex()}"
    )
    intent_rel_path = _document_upload_recovery_intent_rel_path(
        owner_id,
        recovery_token,
    )
    created_at = time.time()
    expires_at = created_at + _document_upload_recovery_ttl_seconds()
    payload = json.dumps(
        {
            "version": 2,
            "owner_id": owner_id,
            "idempotency_key": idempotency_key,
            "recovery_token": recovery_token,
            "request_fingerprint": request_fingerprint,
            "fs_path": fs_path,
            "created_at": created_at,
            "expires_at": expires_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    await _write_document_bytes_atomic(
        entity_id,
        intent_rel_path,
        payload,
        allow_empty=False,
    )
    return _DocumentUploadRecoveryIntent(
        rel_path=intent_rel_path,
        fs_path=fs_path,
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        expires_at=expires_at,
    )


async def _document_upload_source_matches(
    *,
    entity_id: str,
    fs_path: str,
    content_sha256: str,
    file_size: int,
) -> bool:
    from packages.core.services.entity_fs import open_entity_file_snapshot, resolve_path

    resolved = resolve_path(entity_id, fs_path)
    if resolved is None:
        raise EntityFilesystemError("Upload recovery source path is invalid")

    def _matches() -> bool:
        if not os.path.lexists(resolved):
            return False
        try:
            with open_entity_file_snapshot(
                entity_id,
                fs_path,
                expected_content_sha256=content_sha256,
            ) as snapshot:
                return snapshot.stat.st_size == file_size
        except EntityFilesystemError as exc:
            raise _DocumentUploadRecoveryConflict(
                "Stored bytes conflict with this incomplete document upload",
            ) from exc

    return await asyncio.to_thread(_matches)


async def _cleanup_document_upload_recovery_intent(
    entity_id: str,
    intent: _DocumentUploadRecoveryIntent | None,
) -> None:
    if intent is None:
        return
    try:
        from packages.core.services.entity_fs import unlink_entity_file_entry

        await asyncio.to_thread(
            unlink_entity_file_entry,
            entity_id,
            intent.rel_path,
            allow_symlink=True,
        )
    except Exception:
        logger.warning(
            "Could not remove completed document upload recovery intent %s",
            intent.rel_path,
            exc_info=True,
        )


def _iter_stale_document_upload_recovery_markers(
    *,
    now: float,
) -> Iterator[tuple[float, str, str]]:
    root = os.path.realpath(settings.MANOR_FS_ROOT)
    cutoff = now - _document_upload_recovery_ttl_seconds()

    def _entries(path: str) -> Iterator[os.DirEntry[str]]:
        try:
            scanner = os.scandir(path)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return
        with scanner:
            yield from scanner

    for entity_entry in _entries(root):
        if not entity_entry.is_dir(follow_symlinks=False):
            continue
        intents_root = os.path.join(
            entity_entry.path,
            ".ai",
            "document-upload-intents",
        )
        for shard_entry in _entries(intents_root):
            if not shard_entry.is_dir(follow_symlinks=False):
                continue
            for marker_entry in _entries(shard_entry.path):
                try:
                    marker_stat = marker_entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if (
                    not marker_entry.is_file(follow_symlinks=False)
                    or not marker_entry.name.endswith(".json")
                    or marker_stat.st_mtime > cutoff
                ):
                    continue
                rel_path = os.path.relpath(marker_entry.path, entity_entry.path)
                yield marker_stat.st_mtime, entity_entry.name, rel_path


async def cleanup_expired_document_upload_recovery_intents(
    *,
    now: float | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Remove bounded, expired upload intents and unreferenced source bytes."""
    if not settings.MANOR_FS_ENABLED or limit <= 0:
        return {"examined": 0, "cleaned": 0, "sources_removed": 0, "failed": 0}
    cleanup_now = time.time() if now is None else float(now)
    candidates = heapq.nsmallest(
        limit,
        _iter_stale_document_upload_recovery_markers(now=cleanup_now),
    )
    report = {"examined": 0, "cleaned": 0, "sources_removed": 0, "failed": 0}
    from packages.core import database as database_module
    from packages.core.services.entity_fs import unlink_entity_file_entry

    async with database_module.async_session() as cleanup_db:
        for _marker_mtime, entity_id, intent_rel_path in candidates:
            report["examined"] += 1
            try:
                async with _document_filesystem_mutation(entity_id):
                    try:
                        loaded = await _read_document_upload_recovery_payload(
                            entity_id=entity_id,
                            intent_rel_path=intent_rel_path,
                        )
                    except (EntityFilesystemError, OSError, ValueError, json.JSONDecodeError):
                        await asyncio.to_thread(
                            unlink_entity_file_entry,
                            entity_id,
                            intent_rel_path,
                            allow_symlink=True,
                        )
                        report["cleaned"] += 1
                        continue
                    if loaded is None:
                        continue
                    payload, current_mtime = loaded
                    try:
                        intent = _parse_document_upload_recovery_intent(
                            entity_id=entity_id,
                            intent_rel_path=intent_rel_path,
                            payload=payload,
                            marker_mtime=current_mtime,
                        )
                    except EntityFilesystemError:
                        await asyncio.to_thread(
                            unlink_entity_file_entry,
                            entity_id,
                            intent_rel_path,
                            allow_symlink=True,
                        )
                        report["cleaned"] += 1
                        continue
                    if intent.expires_at > cleanup_now:
                        continue
                    document_id = await cleanup_db.scalar(
                        select(Document.id).where(
                            Document.entity_id == entity_id,
                            Document.fs_path == intent.fs_path,
                        ).limit(1)
                    )
                    if document_id is None:
                        removed = await asyncio.to_thread(
                            unlink_entity_file_entry,
                            entity_id,
                            intent.fs_path,
                            allow_symlink=True,
                        )
                        if removed:
                            report["sources_removed"] += 1
                    await asyncio.to_thread(
                        unlink_entity_file_entry,
                        entity_id,
                        intent.rel_path,
                        allow_symlink=True,
                    )
                    report["cleaned"] += 1
            except Exception:
                report["failed"] += 1
                await cleanup_db.rollback()
                logger.warning(
                    "Could not clean expired document upload recovery intent %s/%s",
                    entity_id,
                    intent_rel_path,
                    exc_info=True,
                )
    return report


def _normalize_upload_idempotency_key(raw_key: str | None) -> str | None:
    key = (raw_key or "").strip()
    if not key:
        return None
    if not _UPLOAD_IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise HTTPException(
            400,
            "Idempotency-Key must be 8-128 URL-safe characters",
        )
    return key


def _document_upload_request_fingerprint(
    *,
    content_sha256: str,
    filename: str,
    file_size: int,
    folder_id: str | None,
    visibility: str | None,
    classification: str | None,
    client_visible: bool | None,
) -> str:
    payload = {
        "classification": classification,
        "client_visible": client_visible,
        "content_sha256": content_sha256,
        "file_size": file_size,
        "filename": filename,
        "folder_id": folder_id,
        "visibility": visibility,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _find_idempotent_document_upload(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_id: str,
    idempotency_key: str | None,
) -> Document | None:
    if idempotency_key is None:
        return None
    return await db.scalar(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.owner_id == owner_id,
            Document.upload_idempotency_key == idempotency_key,
        )
    )


async def _require_idempotent_document_upload_visible(
    db: AsyncSession,
    document: Document,
) -> None:
    if await document_is_owned_by_deleted_workspace(
        db,
        document,
        lock_for_read=True,
    ):
        raise HTTPException(404, "Document not found")
    if document.is_trashed:
        raise HTTPException(
            409,
            "This upload already completed, but its document is now in trash",
        )


async def _validate_idempotent_document_upload(
    db: AsyncSession,
    document: Document,
    *,
    request_fingerprint: str,
) -> Document:
    await _require_idempotent_document_upload_visible(db, document)
    if document.upload_request_fingerprint != request_fingerprint:
        raise HTTPException(
            409,
            "Idempotency-Key was already used for a different document upload",
        )
    return document


@router.get("/upload-receipts/{idempotency_key}", response_model=DocumentResponse)
async def get_document_upload_receipt(
    idempotency_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve an ambiguously completed browser upload without resending bytes."""
    normalized_key = _normalize_upload_idempotency_key(idempotency_key)
    # This is a read-side reconciliation endpoint, not a new upload attempt.
    # The durable receipt is scoped to the authenticated uploader and entity;
    # requiring DOCS_UPLOAD again would make an already accepted upload
    # impossible to reconcile if that permission changed while it processed.
    document = await _find_idempotent_document_upload(
        db,
        entity_id=user.entity_id,
        owner_id=user.id,
        idempotency_key=normalized_key,
    )
    if document is None:
        raise HTTPException(404, "Upload receipt not found")
    await _require_idempotent_document_upload_visible(db, document)
    return await _doc_resp_for_user(db, document, user)


@router.post("/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    folder_id: str | None = Query(None, description="Folder to upload into"),
    visibility: str | None = Query(None, description="private | workspace | entity | public"),
    classification: str | None = Query(None, description="public | internal | confidential | restricted"),
    client_visible: bool | None = Query(None, description="Show in client portal"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import aiofiles
    from packages.core.services.knowledge_sync import sync_file_to_knowledge
    from packages.core.services.document_service import get_document

    await _require_document_upload(db, user)
    idempotency_key = _normalize_upload_idempotency_key(idempotency_key)
    existing_receipt = await _find_idempotent_document_upload(
        db,
        entity_id=user.entity_id,
        owner_id=user.id,
        idempotency_key=idempotency_key,
    )
    active_recovery_intent = (
        existing_receipt is None
        and settings.MANOR_FS_ENABLED
        and await _has_active_document_upload_recovery_intent(
            entity_id=user.entity_id,
            owner_id=user.id,
            idempotency_key=idempotency_key,
        )
    )
    # A committed replay does not consume storage again and must remain
    # recoverable even when the first upload brought the entity to its limit.
    # A validated pending intent is the same already-admitted logical attempt,
    # not a new upload. New attempts retain the route's early plan gate;
    # create_document also rechecks at the persistence boundary to close races.
    if existing_receipt is None and not active_recovery_intent:
        await enforce_plan_resource("storage_mb", user=user, db=db)

    # Validate enum-style params; reject unknown values rather than silently
    # accept (avoids "Confidentail" typos surviving into the DB).
    _ALLOWED_VISIBILITY = {"private", "workspace", "entity", "public"}
    _ALLOWED_CLASSIFICATION = {"public", "internal", "confidential", "restricted"}
    if visibility is not None and visibility not in _ALLOWED_VISIBILITY:
        raise HTTPException(400, f"Invalid visibility: {visibility}")
    if classification is not None and classification not in _ALLOWED_CLASSIFICATION:
        raise HTTPException(400, f"Invalid classification: {classification}")
    # Cross-field invariant 1 (RFC §13.14): restricted cannot be public.
    if classification == "restricted" and visibility == "public":
        raise HTTPException(400, "Restricted documents cannot have public visibility")
    # Cross-field: confidential+ cannot be client_visible
    if client_visible and classification in {"confidential", "restricted"}:
        raise HTTPException(400, "Confidential/Restricted documents cannot be client_visible")
    requested_visibility = visibility
    requested_classification = classification
    requested_client_visible = client_visible
    if folder_id:
        _, folder_by_id = await _load_document_folders(db, user.entity_id)
        folder = folder_by_id.get(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")
        await _require_folder_capability(
            db,
            user,
            folder,
            {Capability.UPLOAD_TO, Capability.EDIT},
            "Only the folder owner/admin or a user with upload/edit access can upload to this folder",
        )

    # RFC §13.3: when uploading into a folder, the folder's classification
    # is a floor and its visibility is a ceiling for the new document.
    # Auto-adjust rather than reject; UI surfaces ``folder_adjustments``
    # in the response so the user sees why their picks changed.
    visibility, classification, client_visible, folder_adjustments = await _enforce_folder_invariants(
        db,
        entity_id=user.entity_id,
        folder_id=folder_id,
        visibility=visibility,
        classification=classification,
        client_visible=client_visible,
    )

    # Browser/drop uploads can include client-side directory prefixes such as
    # "upload/photo.png". Knowledge folders should only be created explicitly
    # by the user or by an AI file-writing action, so plain uploads use basename.
    filename = _safe_visible_filename(file.filename, "upload")
    mime_type = file.content_type
    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
    # The filesystem mutation is drained in its own asyncio task. Snapshot
    # request-scoped ORM values before that boundary so a rollback cannot
    # expire ``user`` and mask the original persistence error with
    # MissingGreenlet. The canonical root also matters on platforms where the
    # configured path has an alias (for example /var -> /private/var on macOS).
    entity_id = user.entity_id
    owner_id = user.id
    created_by = user.display_name or user.email
    entity_root = _entity_root(entity_id)
    fs_path = None
    file_size = 0
    content_hasher = hashlib.sha256()
    resolved_folder_id = folder_id
    workspace_binding = await _workspace_storage_for_folder(
        db,
        entity_id=entity_id,
        folder_id=folder_id,
    )

    ext = os.path.splitext(filename)[1].lstrip(".") if "." in filename else None
    if settings.MANOR_FS_ENABLED:
        _require_document_filesystem_ready()
        fd, tmp_path = tempfile.mkstemp(prefix="manor-doc-upload-", suffix=".tmp")
        os.close(fd)
        # Stream to disk in chunks — avoids loading entire file into memory
        try:
            async with aiofiles.open(tmp_path, "wb") as f:
                while chunk := await file.read(1024 * 256):  # 256KB chunks
                    file_size += len(chunk)
                    if file_size > max_bytes:
                        raise CodedError(
                            413,
                            code="page.knowledge.file_too_large_max_mb",
                            message=f"File too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB",
                            vars={"max": settings.MANOR_MAX_UPLOAD_MB},
                        )
                    content_hasher.update(chunk)
                    await f.write(chunk)
            from packages.core.services.upload_security import (
                UploadSecurityError,
                inspect_upload_path,
            )

            try:
                mime_type = await inspect_upload_path(
                    tmp_path,
                    filename=filename,
                    declared_content_type=file.content_type,
                )
            except UploadSecurityError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            request_fingerprint = _document_upload_request_fingerprint(
                content_sha256=content_hasher.hexdigest(),
                filename=filename,
                file_size=file_size,
                folder_id=resolved_folder_id,
                visibility=requested_visibility,
                classification=requested_classification,
                client_visible=requested_client_visible,
            )
            async with _document_filesystem_mutation(entity_id):

                async def persist_upload():
                    nonlocal fs_path
                    recovery_intent: _DocumentUploadRecoveryIntent | None = None
                    is_recovery_retry = False
                    try:
                        existing = await _find_idempotent_document_upload(
                            db,
                            entity_id=entity_id,
                            owner_id=owner_id,
                            idempotency_key=idempotency_key,
                        )
                        if existing is not None:
                            existing = await _validate_idempotent_document_upload(
                                db,
                                existing,
                                request_fingerprint=request_fingerprint,
                            )
                            stale_intent = (
                                _DocumentUploadRecoveryIntent(
                                    rel_path=_document_upload_recovery_intent_rel_path(
                                        owner_id,
                                        idempotency_key,
                                    ),
                                    fs_path="",
                                )
                                if idempotency_key is not None
                                else None
                            )
                            await _cleanup_document_upload_recovery_intent(
                                entity_id,
                                stale_intent,
                            )
                            return existing
                        recovery_intent = await _load_document_upload_recovery_intent(
                            entity_id=entity_id,
                            owner_id=owner_id,
                            idempotency_key=idempotency_key,
                            request_fingerprint=request_fingerprint,
                        )
                        if recovery_intent is None:
                            rel_target = _unique_document_rel_path(
                                entity_id,
                                filename,
                                rel_dir=workspace_binding.storage_dir if workspace_binding else None,
                            )
                            recovery_intent = await _create_document_upload_recovery_intent(
                                entity_id=entity_id,
                                owner_id=owner_id,
                                idempotency_key=idempotency_key,
                                request_fingerprint=request_fingerprint,
                                fs_path=rel_target,
                            )
                        else:
                            is_recovery_retry = True
                            rel_target = recovery_intent.fs_path
                        fs_path = rel_target
                        if not await _document_upload_source_matches(
                            entity_id=entity_id,
                            fs_path=rel_target,
                            content_sha256=content_hasher.hexdigest(),
                            file_size=file_size,
                        ):
                            fs_path = await _copy_document_file_atomic(
                                entity_id,
                                rel_target,
                                tmp_path,
                                expected_size=file_size,
                                allow_empty=True,
                            )
                        sync = await sync_file_to_knowledge(
                            entity_id=entity_id,
                            abs_path=os.path.join(entity_root, fs_path),
                            entity_root=entity_root,
                            source="upload",
                            created_by=created_by,
                            force=True,
                            folder_id=resolved_folder_id,
                            workspace_id=workspace_binding.workspace_id if workspace_binding else None,
                            user_id=owner_id,
                            visibility=visibility,
                            classification=classification,
                            client_visible=client_visible,
                            storage_admission_prevalidated=is_recovery_retry,
                            db=db,
                        )
                        doc = await get_document(db, sync.document_id, entity_id) if sync.document_id else None
                        if not doc:
                            raise HTTPException(500, "Upload saved but document sync failed")
                        # Knowledge sync may infer a normalized display name from content.
                        # Upload/download UX should preserve the user's original filename.
                        doc.name = filename
                        doc.file_type = ext
                        doc.mime_type = mime_type
                        doc.upload_idempotency_key = idempotency_key
                        doc.upload_request_fingerprint = (
                            request_fingerprint if idempotency_key is not None else None
                        )
                        _apply_permission_overrides(
                            doc,
                            owner_id,
                            visibility,
                            classification,
                            client_visible,
                        )
                        await db.flush()
                        doc = await _commit_document_upload_with_reconciliation(
                            db,
                            doc,
                            entity_id=entity_id,
                            owner_id=owner_id,
                            idempotency_key=idempotency_key,
                            request_fingerprint=request_fingerprint,
                            require_source_file=True,
                        )
                        await _cleanup_document_upload_recovery_intent(
                            entity_id,
                            recovery_intent,
                        )
                        return doc
                    except IntegrityError:
                        await _rollback_database_best_effort(db)
                        existing = await _find_idempotent_document_upload(
                            db,
                            entity_id=entity_id,
                            owner_id=owner_id,
                            idempotency_key=idempotency_key,
                        )
                        if existing is not None:
                            existing = await _validate_idempotent_document_upload(
                                db,
                                existing,
                                request_fingerprint=request_fingerprint,
                            )
                            await _cleanup_document_upload_recovery_intent(
                                entity_id,
                                recovery_intent,
                            )
                            return existing
                        await _remove_or_quarantine_document_file(entity_id, fs_path)
                        await _cleanup_document_upload_recovery_intent(
                            entity_id,
                            recovery_intent,
                        )
                        raise
                    except _DocumentUploadCommitUncertain:
                        # The commit outcome could not be proven. Preserve the
                        # source path so a durable receipt can never point at
                        # bytes this request deleted while the DB recovered.
                        await _rollback_database_best_effort(db)
                        raise
                    except _DocumentUploadRecoveryConflict:
                        # This key belongs to an earlier incomplete request.
                        # Never delete that request's recovery bytes.
                        await _rollback_database_best_effort(db)
                        raise
                    except Exception:
                        await _rollback_database_best_effort(db)
                        await _remove_or_quarantine_document_file(entity_id, fs_path)
                        await _cleanup_document_upload_recovery_intent(
                            entity_id,
                            recovery_intent,
                        )
                        raise

                doc = await _finish_document_filesystem_mutation(persist_upload())
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    else:
        # Metadata-only deployments still accept large files. Stream through a
        # bounded temporary file so validation, hashing, and the size gate do
        # not allocate the complete request body a second time.
        fd, tmp_path = tempfile.mkstemp(prefix="manor-doc-upload-", suffix=".tmp")
        os.close(fd)
        try:
            async with aiofiles.open(tmp_path, "wb") as target:
                while chunk := await file.read(1024 * 256):
                    file_size += len(chunk)
                    if file_size > max_bytes:
                        raise CodedError(
                            413,
                            code="page.knowledge.file_too_large_max_mb",
                            message=f"File too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB",
                            vars={"max": settings.MANOR_MAX_UPLOAD_MB},
                        )
                    content_hasher.update(chunk)
                    await target.write(chunk)
            from packages.core.services.upload_security import (
                UploadSecurityError,
                inspect_upload_path,
            )

            try:
                mime_type = await inspect_upload_path(
                    tmp_path,
                    filename=filename,
                    declared_content_type=file.content_type,
                )
            except UploadSecurityError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            request_fingerprint = _document_upload_request_fingerprint(
                content_sha256=content_hasher.hexdigest(),
                filename=filename,
                file_size=file_size,
                folder_id=resolved_folder_id,
                visibility=requested_visibility,
                classification=requested_classification,
                client_visible=requested_client_visible,
            )
            existing = await _find_idempotent_document_upload(
                db,
                entity_id=entity_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                doc = await _validate_idempotent_document_upload(
                    db,
                    existing,
                    request_fingerprint=request_fingerprint,
                )
            else:
                try:
                    doc = await create_document(
                        db,
                        entity_id,
                        name=filename,
                        fs_path=fs_path,
                        file_size=file_size,
                        file_type=ext,
                        mime_type=mime_type,
                        source="upload",
                        created_by=created_by,
                        folder_id=resolved_folder_id,
                        visibility=visibility,
                        classification=classification,
                        client_visible=client_visible,
                        owner_id=owner_id,
                        upload_idempotency_key=idempotency_key,
                        upload_request_fingerprint=(
                            request_fingerprint if idempotency_key is not None else None
                        ),
                    )
                    doc = await _finish_document_filesystem_mutation(
                        _commit_document_upload_with_reconciliation(
                            db,
                            doc,
                            entity_id=entity_id,
                            owner_id=owner_id,
                            idempotency_key=idempotency_key,
                            request_fingerprint=request_fingerprint,
                            require_source_file=False,
                        )
                    )
                except IntegrityError:
                    await _rollback_database_best_effort(db)
                    existing = await _find_idempotent_document_upload(
                        db,
                        entity_id=entity_id,
                        owner_id=owner_id,
                        idempotency_key=idempotency_key,
                    )
                    if existing is None:
                        raise
                    doc = await _validate_idempotent_document_upload(
                        db,
                        existing,
                        request_fingerprint=request_fingerprint,
                    )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return await _doc_resp_for_user(db, doc, user)


_CLASS_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
_VIS_RANK = {"private": 0, "workspace": 1, "entity": 2, "public": 3}


async def _enforce_folder_invariants(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
    visibility: str | None,
    classification: str | None,
    client_visible: bool | None,
) -> tuple[str | None, str | None, bool | None, dict]:
    """When uploading or moving a doc into a folder, enforce RFC §13.3:
      * child classification ≥ folder classification (auto-upgrade)
      * child visibility ⊆ folder visibility (auto-narrow)
      * confidential+ folders mark children non-client_visible

    Returns the (possibly adjusted) tuple + an ``adjustments`` dict so the
    response can show "auto-upgraded to confidential because of folder rules"
    in the UI. ``folder_id=None`` (root) is a no-op.
    """
    adjustments: dict = {}
    if not folder_id:
        return visibility, classification, client_visible, adjustments
    from packages.core.models.document import DocumentFolder

    folder = (
        await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.id == folder_id,
                DocumentFolder.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if not folder:
        return visibility, classification, client_visible, adjustments

    f_class = getattr(folder, "classification", None)
    f_vis = getattr(folder, "visibility", None)
    f_client = getattr(folder, "client_visible", None)

    # Floor: child classification cannot be below folder's.
    if f_class:
        current = classification or "internal"
        if _CLASS_RANK.get(current, 1) < _CLASS_RANK.get(f_class, 1):
            adjustments["classification"] = {"from": current, "to": f_class, "reason": "folder rule"}
            classification = f_class

    # Ceiling: child visibility cannot exceed folder's.
    if f_vis:
        current_v = visibility or "entity"
        if _VIS_RANK.get(current_v, 2) > _VIS_RANK.get(f_vis, 2):
            adjustments["visibility"] = {"from": current_v, "to": f_vis, "reason": "folder rule"}
            visibility = f_vis

    # confidential+ folder forces children non-client_visible.
    if f_client is False or classification in ("confidential", "restricted"):
        if client_visible:
            adjustments["client_visible"] = {"from": True, "to": False, "reason": "folder rule"}
            client_visible = False

    return visibility, classification, client_visible, adjustments


def _apply_permission_overrides(
    doc,
    user_id: str,
    visibility: str | None,
    classification: str | None,
    client_visible: bool | None,
) -> None:
    """Apply permission-v1 field overrides on a freshly synced document.

    Used by the FS-enabled upload path where ``sync_file_to_knowledge``
    creates the row with defaults. The direct ``create_document`` path
    accepts these as kwargs and does not need this helper.
    """
    if visibility is not None:
        doc.visibility = visibility
    elif not getattr(doc, "folder_id", None):
        doc.visibility = USER_ROOT_DOCUMENT_DEFAULT_VISIBILITY
    if classification is not None:
        doc.classification = classification
    if client_visible is not None:
        doc.client_visible = client_visible
    if not getattr(doc, "owner_id", None):
        doc.owner_id = user_id


# ── Create blank document ──

class CreateBlankRequest(BaseModel):
    name: str
    file_type: str = "md"  # md, txt, docx, pptx, xlsx, csv, diagram.json


def _blank_diagram_bytes(title: str) -> bytes:
    document = {
        "version": "editable_diagram_v1",
        "id": "diagram_blank",
        "title": title or "Untitled diagram",
        "canvas": {
            "width": 2400,
            "height": 1600,
            "unit": "px",
            "originX": -120,
            "originY": -90,
        },
        "theme": {
            "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif",
            "labelFontFamily": "Times New Roman, serif",
            "palette": {
                "line": "#111827",
                "accent": "#008cad",
                "containerStroke": "#55a9e6",
                "paper": "#ffffff",
                "text": "#111827",
                "muted": "#64748b",
            },
        },
        "elements": [],
        "groups": [],
        "constraints": [],
    }
    return f"{json.dumps(document, ensure_ascii=False, indent=2)}\n".encode("utf-8")


def _minimal_pptx_bytes(title: str, content: str = "") -> bytes:
    """Create a tiny valid PPTX without optional third-party dependencies."""
    from xml.sax.saxutils import escape

    safe_title = escape(title or "Presentation")
    body = "\n".join(line.strip() for line in (content or "").splitlines() if line.strip())
    body = escape(body[:4000])
    body_shape = ""
    if body:
        body_shape = f"""
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="1371600" y="3200400"/><a:ext cx="9453600" cy="1828800"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>
        <p:txBody><a:bodyPr wrap="square"/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="1800"><a:solidFill><a:srgbClr val="E2E8F0"/></a:solidFill></a:rPr><a:t>{body}</a:t></a:r></a:p></p:txBody>
      </p:sp>"""

    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""",
        "docProps/app.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Manor AI</Application><PresentationFormat>On-screen Show (16:9)</PresentationFormat><Slides>1</Slides></Properties>""",
        "docProps/core.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{safe_title}</dc:title></cp:coreProperties>""",
        "ppt/presentation.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
  <p:sldSz cx="12192000" cy="6858000" type="wide"/><p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>""",
        "ppt/_rels/presentation.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>""",
        "ppt/slides/slide1.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="0F172A"/></a:solidFill></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="914400" y="2057400"/><a:ext cx="10363200" cy="1371600"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>
        <p:txBody><a:bodyPr wrap="square" anchor="mid"/><a:lstStyle/><a:p><a:pPr algn="ctr"/><a:r><a:rPr lang="en-US" sz="4400" b="1"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:rPr><a:t>{safe_title}</a:t></a:r></a:p></p:txBody>
      </p:sp>{body_shape}
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>""",
        "ppt/slides/_rels/slide1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>""",
        "ppt/slideLayouts/slideLayout1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>""",
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>""",
        "ppt/slideMasters/slideMaster1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>""",
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>""",
        "ppt/theme/theme1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Manor"><a:themeElements><a:clrScheme name="Manor"><a:dk1><a:srgbClr val="0F172A"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1E293B"/></a:dk2><a:lt2><a:srgbClr val="F8FAFC"/></a:lt2><a:accent1><a:srgbClr val="0F766E"/></a:accent1><a:accent2><a:srgbClr val="2563EB"/></a:accent2><a:accent3><a:srgbClr val="7C3AED"/></a:accent3><a:accent4><a:srgbClr val="DC2626"/></a:accent4><a:accent5><a:srgbClr val="D97706"/></a:accent5><a:accent6><a:srgbClr val="059669"/></a:accent6><a:hlink><a:srgbClr val="2563EB"/></a:hlink><a:folHlink><a:srgbClr val="7C3AED"/></a:folHlink></a:clrScheme><a:fontScheme name="Manor"><a:majorFont><a:latin typeface="Calibri"/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/></a:minorFont></a:fontScheme><a:fmtScheme name="Manor"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme></a:themeElements></a:theme>""",
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, xml in files.items():
            zf.writestr(path, xml)
    return buf.getvalue()


async def _generate_pptx_bytes(title: str, content: str = "") -> bytes:
    from packages.core.services.docgen_service import generate_pptx

    try:
        return await generate_pptx(title, content)
    except RuntimeError as exc:
        if "python-pptx is required" not in str(exc):
            raise
        return await asyncio.to_thread(_minimal_pptx_bytes, title, content)

@router.post("/create-blank", response_model=DocumentResponse, status_code=201)
async def create_blank_document(
    body: CreateBlankRequest,
    _gate=Depends(require_plan("storage_mb")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a blank document (markdown, text, etc.)."""
    await _require_document_upload(db, user)
    settings = get_settings()
    ext = _safe_file_extension(body.file_type)
    base_name = _safe_visible_filename(body.name, "Untitled")
    filename = base_name if "." in base_name else f"{base_name}.{ext}"
    filename = _safe_visible_filename(filename, f"Untitled.{ext}")

    BLANK_CONTENT: dict[str, bytes] = {
        "md": b"",
        "txt": b"",
        "csv": b"",
        "json": b"{}",
        "html": b"<!DOCTYPE html>\n<html><head><title></title></head><body></body></html>",
    }
    if ext in ("docx", "doc"):
        from packages.core.services.docgen_service import generate_docx

        content = await generate_docx(os.path.splitext(filename)[0] or "Untitled", "")
        mime_type = DOCX_MIME
    elif ext in ("pptx", "ppt", "dps"):
        content = await _generate_pptx_bytes(os.path.splitext(filename)[0] or "Untitled", "")
        mime_type = PPTX_MIME
    elif ext in ("diagram", "diagram.json"):
        diagram_title = (
            filename[: -len(".diagram.json")]
            if filename.lower().endswith(".diagram.json")
            else os.path.splitext(filename)[0]
        )
        content = _blank_diagram_bytes(diagram_title)
        mime_type = "application/json"
    else:
        content = BLANK_CONTENT.get(ext, b"")
        mime_type = f"text/{ext}" if ext in ("md", "txt", "csv", "html") else "application/json"

    file_type = "docx" if ext == "doc" else "pptx" if ext in ("ppt", "dps") else ext
    created_by = user.display_name or user.email
    fs_path = None
    if settings.MANOR_FS_ENABLED:
        _require_document_filesystem_ready()
        async with _document_filesystem_mutation(user.entity_id):

            async def persist_blank_document():
                nonlocal fs_path
                try:
                    fs_path = await _write_document_bytes_atomic(
                        user.entity_id,
                        _unique_document_rel_path(user.entity_id, filename),
                        content,
                        allow_empty=True,
                    )
                    doc = await upsert_document_by_fs_path(
                        db,
                        user.entity_id,
                        name=filename,
                        fs_path=fs_path,
                        file_size=len(content),
                        file_type=file_type,
                        mime_type=mime_type,
                        source="manual",
                        created_by=created_by,
                        owner_id=user.id,
                        visibility=USER_ROOT_DOCUMENT_DEFAULT_VISIBILITY,
                    )
                    doc.source = "manual"
                    doc.created_by = created_by
                    await db.flush()
                    await db.commit()
                    await _invalidate_committed_document_cache(user.entity_id)
                    return doc
                except Exception:
                    await _rollback_database_best_effort(db)
                    await _remove_or_quarantine_document_file(user.entity_id, fs_path)
                    raise

            doc = await _finish_document_filesystem_mutation(
                persist_blank_document(),
            )
    else:
        doc = await create_document(
            db,
            user.entity_id,
            name=filename,
            fs_path=fs_path,
            file_size=len(content),
            file_type=file_type,
            mime_type=mime_type,
            source="manual",
            created_by=created_by,
            owner_id=user.id,
        )
        await db.commit()
        await _invalidate_committed_document_cache(user.entity_id)
    return await _doc_resp_for_user(db, doc, user)


# ── AI Draft ──

def _csv_text_to_xlsx(csv_text: str) -> tuple[bytes, str]:
    """Convert CSV text from LLM into a real XLSX binary file."""
    import csv as csv_mod
    import io

    try:
        from openpyxl import Workbook
    except ImportError:
        # Fallback: save as CSV bytes if openpyxl not installed
        return csv_text.encode("utf-8"), "text/csv"

    wb = Workbook()
    ws = wb.active
    # Strip markdown fences if LLM included them
    cleaned = csv_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]  # remove opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    reader = csv_mod.reader(io.StringIO(cleaned))
    for row in reader:
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class AiDraftRequest(BaseModel):
    prompt: str
    file_type: str = "md"
    name: str | None = None


@router.post("/ai-draft", response_model=DocumentResponse, status_code=201)
async def create_ai_draft(
    body: AiDraftRequest,
    _gate=Depends(require_plan("storage_mb")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a document using AI based on a prompt.

    Creates a placeholder document immediately (vector_status='generating')
    and generates content in the background so the UI stays responsive.
    """
    await _require_document_upload(db, user)
    if not runtime_text_completion_platform_configured():
        raise HTTPException(500, "LLM not configured; missing platform LLM API key")

    ext = _safe_file_extension(body.file_type)

    # Derive filename upfront (placeholder name if auto-generating)
    if body.name:
        base_name = _safe_visible_filename(body.name, "AI Draft")
        filename = base_name if "." in base_name else f"{base_name}.{ext}"
        filename = _safe_visible_filename(filename, f"AI Draft.{ext}")
    else:
        filename = f"AI Draft.{ext}"

    # Determine mime type for the placeholder
    _mime_map: dict[str, str] = {
        "xlsx": XLSX_MIME,
        "docx": DOCX_MIME,
        "doc": DOCX_MIME,
        "pptx": PPTX_MIME,
    }
    mime_type = _mime_map.get(
        ext, f"text/{ext}" if ext in ("md", "txt", "csv", "html", "json") else "application/octet-stream"
    )

    # Create placeholder document immediately
    doc = await create_document(
        db,
        user.entity_id,
        name=filename,
        fs_path=None,
        file_size=0,
        file_type=ext,
        mime_type=mime_type,
        source="ai-draft",
        created_by=(user.display_name or user.email),
        owner_id=user.id,
    )
    # Mark as generating
    doc.vector_status = VectorStatus.GENERATING
    await db.flush()

    resp = await _doc_resp_for_user(db, doc, user)

    async def persist_ai_draft_placeholder() -> None:
        # The background writer must never race a placeholder that is still
        # only visible inside this request transaction.
        await db.commit()
        asyncio.create_task(
            _generate_ai_draft_content(
                doc_id=doc.id,
                entity_id=user.entity_id,
                user_id=user.id,
                prompt=body.prompt,
                ext=ext,
                original_name=filename if body.name else None,
                display_name=user.display_name or user.email,
            )
        )
        await _invalidate_committed_document_cache(user.entity_id)

    await _finish_document_filesystem_mutation(persist_ai_draft_placeholder())

    return resp


async def _generate_ai_draft_content(
    doc_id: str,
    entity_id: str,
    user_id: str | None,
    prompt: str,
    ext: str,
    original_name: str | None,
    display_name: str,
) -> None:
    """Background task: call LLM, write file, update document record."""
    from packages.core.database import async_session as async_session_factory

    async def mark_generation_failed(stage: str, exc: Exception) -> None:
        from sqlalchemy import update

        async with async_session_factory() as db:
            await db.execute(
                update(Document)
                .where(
                    Document.id == doc_id,
                    Document.entity_id == entity_id,
                    Document.is_trashed.is_(False),
                    Document.vector_status == VectorStatus.GENERATING,
                )
                .values(vector_status=VectorStatus.FAILED)
            )
            await db.commit()
        await _invalidate_committed_document_cache(entity_id)
        logger.error("AI draft %s failed for doc %s: %s", stage, doc_id, exc)

    try:
        content_text = await generate_document_ai_draft_content(
            entity_id=entity_id,
            user_id=user_id,
            prompt=prompt,
            file_type=ext,
            document_id=doc_id,
        )
    except Exception as exc:
        await mark_generation_failed("LLM call", exc)
        return

    try:
        # Build the actual file bytes.
        mime_type: str | None = None
        if ext == "xlsx":
            content, mime_type = _csv_text_to_xlsx(content_text)
        elif ext in ("docx", "doc"):
            from packages.core.services.docgen_service import generate_docx

            draft_title = os.path.splitext(original_name or "Document")[0] or "Document"
            content = await generate_docx(draft_title, content_text)
            mime_type = DOCX_MIME
        elif ext == "pptx":
            draft_title = os.path.splitext(original_name or "Presentation")[0] or "Presentation"
            content = await _generate_pptx_bytes(draft_title, content_text)
            mime_type = PPTX_MIME
        else:
            content = content_text.encode("utf-8")
            if ext in ("md", "txt", "csv", "html", "json"):
                mime_type = f"text/{ext}"

        # Derive final filename from content if no name was given.
        if not original_name:
            first_line = content_text.split("\n", 1)[0].strip().lstrip("# ").strip()
            short_name = (first_line[:60] or "AI Draft").rstrip(".")
            filename = f"{short_name}.{ext}"
        else:
            filename = original_name if "." in original_name else f"{original_name}.{ext}"
        filename = _safe_visible_filename(filename, f"AI Draft.{ext}")
    except Exception as exc:
        await mark_generation_failed("file rendering", exc)
        return

    try:
        fs_path = None
        async with async_session_factory() as db:
            if settings.MANOR_FS_ENABLED:
                _require_document_filesystem_ready()
                async with _document_filesystem_mutation(entity_id):

                    async def persist_ai_draft() -> bool:
                        nonlocal fs_path
                        try:
                            current = await db.scalar(
                                select(Document)
                                .where(
                                    Document.id == doc_id,
                                    Document.entity_id == entity_id,
                                    Document.is_trashed.is_(False),
                                    Document.vector_status == VectorStatus.GENERATING,
                                    Document.fs_path.is_(None),
                                )
                                .with_for_update()
                            )
                            if current is None:
                                await db.rollback()
                                return False
                            fs_path = await _write_document_bytes_atomic(
                                entity_id,
                                _unique_document_rel_path(entity_id, filename),
                                content,
                                allow_empty=False,
                            )
                            current.name = filename
                            current.fs_path = fs_path
                            current.file_size = len(content)
                            current.mime_type = mime_type or "application/octet-stream"
                            current.vector_status = VectorStatus.PENDING
                            await db.commit()
                            await _invalidate_committed_document_cache(entity_id)
                            return True
                        except Exception:
                            await _rollback_database_best_effort(db)
                            await _remove_or_quarantine_document_file(entity_id, fs_path)
                            raise

                    persisted = await _finish_document_filesystem_mutation(
                        persist_ai_draft()
                    )
            else:
                current = await db.scalar(
                    select(Document)
                    .where(
                        Document.id == doc_id,
                        Document.entity_id == entity_id,
                        Document.is_trashed.is_(False),
                        Document.vector_status == VectorStatus.GENERATING,
                        Document.fs_path.is_(None),
                    )
                    .with_for_update()
                )
                if current is None:
                    await db.rollback()
                    return
                current.name = filename
                current.file_size = len(content)
                current.mime_type = mime_type or "application/octet-stream"
                current.vector_status = VectorStatus.PENDING
                current.metadata_ = merge_document_metadata(
                    current.metadata_,
                    extra={"content_text": content_text},
                )
                await db.commit()
                await _invalidate_committed_document_cache(entity_id)
                persisted = True
            if not persisted:
                return
    except Exception as exc:  # noqa: BLE001
        await mark_generation_failed("file write", exc)
        return

    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings

        process_document_embeddings.delay(doc_id)
    except Exception:
        pass


# ── Google Drive ──


class GoogleDriveUploadRequest(BaseModel):
    file_id: str
    name: str
    mime_type: str | None = None
    file_size: int | None = None
    modified_time: str | None = None
    access_token: str
    folder_id: str | None = None


GOOGLE_EXPORT_MIMES: dict[str, str] = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

EXPORT_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


@router.post("/from-google-drive", response_model=DocumentResponse, status_code=201)
async def upload_from_google_drive(
    body: GoogleDriveUploadRequest,
    _gate=Depends(require_plan("storage_mb")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download a file from Google Drive using the user's access token and store it."""
    import httpx

    await _require_document_upload(db, user)
    if body.folder_id:
        _, folder_by_id = await _load_document_folders(db, user.entity_id)
        folder = folder_by_id.get(body.folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")
        await _require_folder_capability(
            db,
            user,
            folder,
            {Capability.UPLOAD_TO, Capability.EDIT},
            "Only the folder owner/admin or a user with upload/edit access can upload to this folder",
        )

    settings = get_settings()
    headers = {"Authorization": f"Bearer {body.access_token}"}

    # Google Workspace native formats must be exported
    export_mime = GOOGLE_EXPORT_MIMES.get(body.mime_type or "")
    if export_mime:
        url = f"https://www.googleapis.com/drive/v3/files/{body.file_id}/export?mimeType={export_mime}"
    else:
        url = f"https://www.googleapis.com/drive/v3/files/{body.file_id}?alt=media"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        if resp.status_code != 200:
            raise HTTPException(502, f"Failed to download from Google Drive: {resp.status_code}")

    content = resp.content
    if len(content) > settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB")

    filename = _safe_visible_filename(body.name, "google-drive-file")
    # For native Google formats, append the right extension
    if export_mime:
        ext_suffix = EXPORT_EXTENSIONS.get(export_mime, ".pdf")
        if not filename.lower().endswith(ext_suffix):
            filename += ext_suffix
    filename = _safe_visible_filename(filename, "google-drive-file")

    ext = os.path.splitext(filename)[1].lstrip(".")
    actual_mime = export_mime or body.mime_type

    fs_path = None
    workspace_binding = await _workspace_storage_for_folder(
        db,
        entity_id=user.entity_id,
        folder_id=body.folder_id,
    )
    # Store external sync metadata for future refreshes.
    metadata = merge_document_metadata(
        origin={"workspace_id": workspace_binding.workspace_id} if workspace_binding else None,
        external={
            "google_drive": {
                "file_id": body.file_id,
                "modified_time": body.modified_time,
            }
        },
    )
    visibility, classification, client_visible, _ = await _enforce_folder_invariants(
        db,
        entity_id=user.entity_id,
        folder_id=body.folder_id,
        visibility=None,
        classification=None,
        client_visible=None,
    )

    created_by = user.display_name or user.email
    if settings.MANOR_FS_ENABLED:
        _require_document_filesystem_ready()
        async with _document_filesystem_mutation(user.entity_id):

            async def persist_google_document():
                nonlocal fs_path
                try:
                    fs_path = await _write_document_bytes_atomic(
                        user.entity_id,
                        _unique_document_rel_path(
                            user.entity_id,
                            filename,
                            rel_dir=workspace_binding.storage_dir if workspace_binding else None,
                        ),
                        content,
                        allow_empty=False,
                    )
                    doc = await upsert_document_by_fs_path(
                        db,
                        user.entity_id,
                        name=filename,
                        fs_path=fs_path,
                        file_size=len(content),
                        file_type=ext,
                        mime_type=actual_mime,
                        source="google_drive",
                        created_by=created_by,
                        folder_id=body.folder_id,
                        owner_id=user.id,
                        visibility=visibility,
                        classification=classification,
                        client_visible=client_visible,
                    )
                    doc.source = "google_drive"
                    doc.created_by = created_by
                    doc.metadata_ = metadata
                    await db.flush()
                    await _commit_document_and_dispatch_embeddings(
                        db,
                        doc.id,
                        user.entity_id,
                    )
                    return doc
                except Exception:
                    await _rollback_database_best_effort(db)
                    await _remove_or_quarantine_document_file(user.entity_id, fs_path)
                    raise

            doc = await _finish_document_filesystem_mutation(
                persist_google_document(),
            )
    else:
        doc = await create_document(
            db,
            user.entity_id,
            name=filename,
            fs_path=fs_path,
            file_size=len(content),
            file_type=ext,
            mime_type=actual_mime,
            source="google_drive",
            created_by=created_by,
            folder_id=body.folder_id,
            metadata=metadata,
            visibility=visibility,
            classification=classification,
            client_visible=client_visible,
            owner_id=user.id,
        )
        await _finish_document_filesystem_mutation(
            _commit_document_and_dispatch_embeddings(
                db,
                doc.id,
                user.entity_id,
            )
        )

    return await _doc_resp_for_user(db, doc, user)


@router.post("/{doc_id}/sync-google-drive", status_code=200)
async def sync_google_drive_document(
    doc_id: str,
    body: GoogleDriveUploadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-download a Google Drive document if it has changed."""
    import httpx

    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.EDIT},
        "Only the document owner/admin or a user with edit access can sync this document",
    )
    if doc.source != "google_drive":
        raise HTTPException(400, "Not a Google Drive document")

    # Check if modified time changed
    doc_meta = doc.metadata_ if isinstance(doc.metadata_, dict) else {}
    external_meta = doc_meta.get("external")
    external_meta = external_meta if isinstance(external_meta, dict) else {}
    legacy_google_meta = doc_meta.get("google_drive")
    legacy_google_meta = legacy_google_meta if isinstance(legacy_google_meta, dict) else {}
    gd_meta = external_meta.get("google_drive") or legacy_google_meta
    gd_meta = gd_meta if isinstance(gd_meta, dict) else {}
    observed_modified_time = gd_meta.get("modified_time")
    observed_updated_at = doc.updated_at
    if body.modified_time is not None and observed_modified_time == body.modified_time:
        return {"status": "up_to_date"}

    settings = get_settings()
    headers = {"Authorization": f"Bearer {body.access_token}"}

    export_mime = GOOGLE_EXPORT_MIMES.get(body.mime_type or "")
    if export_mime:
        url = f"https://www.googleapis.com/drive/v3/files/{body.file_id}/export?mimeType={export_mime}"
    else:
        url = f"https://www.googleapis.com/drive/v3/files/{body.file_id}?alt=media"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        if resp.status_code != 200:
            raise HTTPException(502, f"Failed to download from Google Drive: {resp.status_code}")

    content = resp.content
    if len(content) > settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB")

    async def current_document_for_sync() -> tuple[Document, bool]:
        current = await db.scalar(
            select(Document)
            .where(
                Document.id == doc_id,
                Document.entity_id == user.entity_id,
                Document.is_trashed.is_(False),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None:
            await db.rollback()
            raise HTTPException(404, "Document not found")
        await _require_document_capability(
            db,
            user,
            current,
            {Capability.EDIT},
            "Only the document owner/admin or a user with edit access can sync this document",
        )
        if current.source != "google_drive":
            raise HTTPException(400, "Not a Google Drive document")
        current_meta = current.metadata_ if isinstance(current.metadata_, dict) else {}
        current_external_meta = current_meta.get("external")
        current_external_meta = (
            current_external_meta if isinstance(current_external_meta, dict) else {}
        )
        current_legacy_google_meta = current_meta.get("google_drive")
        current_legacy_google_meta = (
            current_legacy_google_meta
            if isinstance(current_legacy_google_meta, dict)
            else {}
        )
        current_google_meta = (
            current_external_meta.get("google_drive") or current_legacy_google_meta
        )
        current_google_meta = (
            current_google_meta if isinstance(current_google_meta, dict) else {}
        )
        current_modified_time = current_google_meta.get("modified_time")
        if body.modified_time is not None and current_modified_time == body.modified_time:
            return current, True
        if (
            current_modified_time != observed_modified_time
            or current.updated_at != observed_updated_at
        ):
            await db.rollback()
            raise HTTPException(
                409,
                "Document changed while Google Drive content was downloading; retry sync",
            )
        return current, False

    # Release the read/auth transaction before waiting for the entity lock.
    # The document and its edit permission are loaded again while locked.
    await db.commit()

    # Overwrite bytes and commit their metadata under the same mutation boundary.
    if settings.MANOR_FS_ENABLED:
        _require_document_filesystem_ready()
        async with _document_filesystem_mutation(user.entity_id):
            async def persist_google_sync() -> bool:
                current, already_synced = await current_document_for_sync()
                if already_synced:
                    await db.rollback()
                    return False
                document_fs_path = str(
                    current.fs_path
                    or _unique_document_rel_path(user.entity_id, current.name)
                )
                previous_content = await _read_document_file_for_rollback(
                    user.entity_id,
                    document_fs_path,
                )
                try:
                    current.fs_path = await _write_document_bytes_atomic(
                        user.entity_id,
                        document_fs_path,
                        content,
                        allow_empty=False,
                    )
                    current.metadata_ = merge_document_metadata(
                        current.metadata_,
                        external={
                            "google_drive": {
                                "file_id": body.file_id,
                                "modified_time": body.modified_time,
                            }
                        },
                    )
                    current.file_size = len(content)
                    current.vector_status = VectorStatus.PENDING
                    await _commit_document_and_dispatch_embeddings(
                        db,
                        doc_id,
                        user.entity_id,
                    )
                    return True
                except Exception:
                    await _rollback_database_best_effort(db)
                    await _restore_document_file_after_failure(
                        user.entity_id,
                        document_fs_path,
                        previous_content,
                    )
                    raise

            synced = await _finish_document_filesystem_mutation(
                persist_google_sync()
            )
    else:
        current, already_synced = await current_document_for_sync()
        if already_synced:
            await db.rollback()
            return {"status": "up_to_date"}
        current.metadata_ = merge_document_metadata(
            current.metadata_,
            external={
                "google_drive": {
                    "file_id": body.file_id,
                    "modified_time": body.modified_time,
                }
            },
        )
        current.file_size = len(content)
        current.vector_status = VectorStatus.PENDING
        await _finish_document_filesystem_mutation(
            _commit_document_and_dispatch_embeddings(
                db,
                doc_id,
                user.entity_id,
            )
        )
        synced = True

    return {"status": "synced" if synced else "up_to_date"}


# ── Create from URL ──

def _url_document_source_to_fetch(doc: Document, entity_id: str) -> str | None:
    if doc.source != "url":
        return None
    metadata = dict(doc.metadata_ or {})
    external = metadata.get("external")
    if not isinstance(external, dict):
        return None
    source_url = str(external.get("source_url") or "").strip()
    if not source_url:
        return None

    if settings.MANOR_FS_ENABLED:
        full_path = _document_full_path(doc, entity_id)
        has_content = bool(full_path and os.path.isfile(full_path))
    else:
        has_content = bool(str(metadata.get("content_text") or "").strip())
    return None if has_content else source_url


async def _dispatch_url_fetch_and_invalidate_cache(
    db: AsyncSession,
    doc: Document,
    entity_id: str,
    source_url: str | None,
) -> None:
    """Dispatch URL work after visible state is durable, then publish cache state."""
    if source_url:
        try:
            from packages.core.tasks.ai_tasks import fetch_and_index_url_document

            fetch_and_index_url_document.delay(doc.id, source_url)
        except Exception:
            logger.warning(
                "Failed to dispatch URL fetch task for %s",
                doc.id,
                exc_info=True,
            )
            doc.vector_status = VectorStatus.FAILED
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                extra={
                    "file_integrity": {
                        "status": "unavailable",
                        "source": "url_dispatch",
                        "recoverable": False,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            await db.commit()
    await _invalidate_committed_document_cache(entity_id)


async def _commit_document_and_dispatch_url_fetch(
    db: AsyncSession,
    doc: Document,
    entity_id: str,
    source_url: str | None,
) -> None:
    """Commit visible state before dispatching work."""
    await db.commit()
    await _dispatch_url_fetch_and_invalidate_cache(
        db,
        doc,
        entity_id,
        source_url,
    )


class CreateFromUrlRequest(BaseModel):
    url: str
    name: str | None = None

@router.post("/from-url", response_model=DocumentResponse, status_code=201)
async def create_from_url(
    body: CreateFromUrlRequest,
    _gate=Depends(require_plan("storage_mb")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a document by fetching content from a URL.

    Returns immediately with a placeholder document card. The actual URL
    fetch, file write, and embedding indexing happen in a background task.
    """
    await _require_document_upload(db, user)
    url = body.url.strip()
    if not url:
        raise HTTPException(422, "URL is required")

    filename = _safe_visible_filename(body.name or url.rstrip("/").split("/")[-1] or "download", "download")
    if "." not in filename:
        filename += ".html"  # best guess until we fetch content-type
    filename = _safe_visible_filename(filename, "download.html")
    ext = os.path.splitext(filename)[1].lstrip(".")

    # Create document record immediately (placeholder)
    doc = await create_document(
        db,
        user.entity_id,
        name=filename,
        file_type=ext,
        source="url",
        created_by=(user.display_name or user.email),
        metadata=merge_document_metadata(external={"source_url": url}),
        owner_id=user.id,
    )
    await _finish_document_filesystem_mutation(
        _commit_document_and_dispatch_url_fetch(
            db,
            doc,
            user.entity_id,
            url,
        )
    )

    return await _doc_resp_for_user(db, doc, user)


# ── Groups (fixed paths — before /{doc_id}) ──


class BatchAddToGroupRequest(BaseModel):
    document_ids: list[str]
    group_id: str


@router.post("/groups/batch-add", status_code=200)
async def batch_add_to_group(
    body: BatchAddToGroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add multiple existing documents to a knowledge group in one request."""
    group = await _require_document_group_manager(db, user, body.group_id)
    workspace_id = str(group.workspace_id or "").strip() or None
    document_ids = list(dict.fromkeys(
        str(document_id).strip()
        for document_id in body.document_ids
        if str(document_id).strip()
    ))
    visible_documents = []
    for doc_id in document_ids:
        doc = await get_visible_document(
            db,
            doc_id,
            user.entity_id,
            user_id=user.id,
            role=user.role,
            workspace_id=workspace_id,
        )
        if doc is not None:
            visible_documents.append(doc)
    manageable_documents, _ = await partition_documents_by_capability(
        db,
        visible_documents,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        required_capability=Capability.MANAGE_METADATA,
        workspace_id=workspace_id,
    )
    added = await add_documents_to_group(
        db,
        [document.id for document in manageable_documents],
        group.id,
        entity_id=user.entity_id,
    )
    if workspace_id and added:
        await db.commit()
        await mark_workspace_knowledge_changed(user.entity_id, workspace_id)
    return {"added": added, "total": len(body.document_ids)}


@router.get("/groups", response_model=list[DocumentGroupResponse])
async def list_my_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    groups = await list_groups(db, user.entity_id)
    readable_workspace_ids = await readable_workspace_ids_for_user(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    from packages.core.models.workspace import Workspace

    active_workspace_ids = set((await db.execute(
        select(Workspace.id).where(
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalars())
    if readable_workspace_ids is not None:
        active_workspace_ids.intersection_update(readable_workspace_ids)
    groups = [
        group
        for group in groups
        if not group.workspace_id or group.workspace_id in active_workspace_ids
    ]
    return [DocumentGroupResponse(id=g.id, entity_id=g.entity_id, name=g.name, workspace_id=g.workspace_id) for g in groups]


@router.post("/groups", response_model=DocumentGroupResponse, status_code=201)
async def create_new_group(
    req: CreateGroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.workspace_id:
        workspace = await lock_workspace_access_boundary(
            db,
            workspace_id=req.workspace_id,
            entity_id=user.entity_id,
        )
        if workspace is None or workspace.deleted_at is not None:
            raise HTTPException(404, "Workspace not found")
        if not await user_can_manage_workspace(
            db,
            workspace_id=req.workspace_id,
            user_id=user.id,
            entity_role=user.role,
        ):
            raise HTTPException(403, "Only Workspace owners/admins can create groups")
        try:
            group = await create_workspace_knowledge_group(
                db,
                entity_id=user.entity_id,
                workspace_id=req.workspace_id,
                name=req.name,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await db.commit()
        await mark_workspace_knowledge_changed(user.entity_id, req.workspace_id)
        await db.refresh(group)
    else:
        group = await create_group(db, user.entity_id, name=req.name)
    return DocumentGroupResponse(id=group.id, entity_id=group.entity_id, name=group.name, workspace_id=group.workspace_id)


# ── Trash (fixed paths — before /{doc_id}) ──

@router.get("/trash", response_model=list[DocumentResponse])
async def list_trashed_documents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    docs = await list_trash(db, user.entity_id)
    access_ctx = await DocumentAccessContext.load(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    await access_ctx.preload_documents(db, docs)
    docs = [
        document
        for document in docs
        if not access_ctx.document_owned_by_deleted_workspace(document)
    ]
    if not await effective_user_has_permission(db, user, Permission.DOCS_DELETE):
        docs = [
            d
            for d in docs
            if await _can_use_document_capability_from_context(
                db,
                user,
                d,
                {Capability.DELETE},
                access_ctx,
            )
        ]
    return [await _doc_resp_for_user(db, d, user, access_ctx) for d in docs]


@router.post("/trash/empty", status_code=204)
async def empty_trash_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entity_id = str(user.entity_id)

    async def persist_empty_trash() -> int:
        # Do not cache permission state or retain a database transaction while
        # waiting for an in-flight file mutation. Authorize after the lock.
        await check_effective_user_permission(db, user, Permission.DOCS_DELETE)
        try:
            count = await empty_trash(db, entity_id)
        except DocumentMutationConflictError as exc:
            await _rollback_database_best_effort(db)
            raise HTTPException(409, str(exc)) from exc
        except BaseException:
            await _rollback_database_best_effort(db)
            raise
        await _invalidate_committed_document_cache(entity_id)
        return count

    if settings.MANOR_FS_ENABLED:
        await db.commit()
        async with _document_filesystem_mutation(entity_id):
            await _finish_document_filesystem_mutation(persist_empty_trash())
        from packages.core.services.workspace_artifact_purge import (
            ARTIFACT_CLEANUP_KIND_FILE,
            DOCUMENT_DERIVED_TREE_CACHE_ROOTS,
            drain_workspace_artifact_purge_jobs,
        )

        for cache_root in DOCUMENT_DERIVED_TREE_CACHE_ROOTS:
            await drain_workspace_artifact_purge_jobs(
                db,
                limit=1000,
                entity_id=entity_id,
                storage_base_prefix=f"{cache_root}/",
            )
        await drain_workspace_artifact_purge_jobs(
            db,
            limit=1000,
            entity_id=entity_id,
            target_kind=ARTIFACT_CLEANUP_KIND_FILE,
        )
    else:
        await _finish_document_filesystem_mutation(persist_empty_trash())


# ── Slide images (server-rendered PPTX) ──

@router.get("/{doc_id}/slides")
async def get_slide_images(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return list of rendered slide image URLs for a PPTX document."""
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    if not _is_pptx_document(doc):
        raise HTTPException(400, "Not a presentation file")
    if not doc.fs_path or not settings.MANOR_FS_ENABLED:
        raise HTTPException(404, "No file on disk")

    entity_id = str(user.entity_id)
    user_id = str(user.id)
    user_role = user.role
    cache_dir = os.path.join(settings.MANOR_FS_ROOT, entity_id, ".slide-cache", doc_id)
    await db.rollback()

    async with _document_filesystem_mutation(entity_id):
        current_doc = await get_visible_document(
            db,
            doc_id,
            entity_id,
            user_id=user_id,
            role=user_role,
        )
        if not current_doc:
            raise HTTPException(404, "Document not found")
        if not _is_pptx_document(current_doc):
            raise HTTPException(400, "Not a presentation file")
        pptx_path = _document_full_path(current_doc, entity_id)
        if not pptx_path or not os.path.isfile(pptx_path):
            raise HTTPException(404, "File not found on disk")
        source_ext = _presentation_source_format(current_doc)
        source_snapshot = await asyncio.to_thread(
            _capture_document_source_snapshot,
            pptx_path,
        )
        await db.rollback()

    async def render_preview():
        from packages.core.services.slide_renderer import (
            OfficeRenderLimitError,
            render_slides,
        )

        try:
            return await render_slides(
                pptx_path,
                cache_dir,
                source_ext=source_ext,
            )
        except OfficeRenderLimitError as exc:
            raise HTTPException(413, str(exc)) from exc
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Slide rendering failed for %s: %s",
                doc_id,
                exc,
            )
            raise HTTPException(502, "Slide rendering failed") from exc

    paths = await _finish_document_preview(
        render_preview(),
        db=db,
        doc_id=doc_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        source_snapshot=source_snapshot,
        accept_result=lambda paths: _publish_rendered_preview_version(
            cache_dir,
            paths,
            source_snapshot.path,
        ),
    )

    version = Path(paths[0]).parent.name if paths else ""
    return {
        "slides": [
            {
                "index": i,
                "url": f"/documents/{doc_id}/slides/{i}?version={version}",
            }
            for i in range(len(paths))
        ],
        "total": len(paths),
        "version": version,
    }


@router.get("/{doc_id}/slides/{slide_index}")
async def get_slide_image(
    doc_id: str,
    slide_index: int,
    version: str = Query(pattern=r"^[0-9a-f]{16}$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a single rendered slide image as lossless PNG."""
    if not settings.MANOR_FS_ENABLED:
        raise HTTPException(404, "No file on disk")

    entity_id = str(user.entity_id)
    user_id = str(user.id)
    user_role = user.role
    cache_dir = os.path.join(settings.MANOR_FS_ROOT, entity_id, ".slide-cache", doc_id)
    await db.rollback()

    try:
        from packages.core.services.slide_renderer import open_cached_slide

        async with _document_filesystem_mutation(entity_id):
            current_doc = await get_visible_document(
                db,
                doc_id,
                entity_id,
                user_id=user_id,
                role=user_role,
            )
            if not current_doc:
                raise HTTPException(404, "Document not found")
            if not _is_pptx_document(current_doc):
                raise HTTPException(400, "Not a presentation file")
            pptx_path = _document_full_path(current_doc, entity_id)
            if not pptx_path or not os.path.isfile(pptx_path):
                raise HTTPException(404, "File not found on disk")
            await db.rollback()
            rendered_file = await open_cached_slide(
                cache_dir,
                version,
                slide_index,
                pptx_path,
            )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(404, "Slide preview version not found") from exc
    except IndexError as exc:
        raise HTTPException(404, "Slide index out of range") from exc
    except Exception as exc:
        logger.warning(
            "Cached slide read failed for %s version %s slide %s: %s",
            doc_id,
            version,
            slide_index,
            exc,
        )
        raise HTTPException(502, "Slide image could not be read") from exc

    encoded_filename = urllib.parse.quote(f"slide-{slide_index + 1}.png")
    return StreamingResponse(
        _stream_open_file(rendered_file),
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        },
    )


@router.get("/{doc_id}/slides/{slide_index}/objects/{object_id}")
async def get_slide_object_image(
    doc_id: str,
    slide_index: int,
    object_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one independently rendered presentation object as transparent PNG."""
    if slide_index < 0 or not re.fullmatch(r"[0-9]+", object_id):
        raise HTTPException(400, "Invalid presentation object reference")
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    if not _is_pptx_document(doc):
        raise HTTPException(400, "Not a presentation file")
    if not doc.fs_path or not settings.MANOR_FS_ENABLED:
        raise HTTPException(404, "No file on disk")
    entity_id = str(user.entity_id)
    user_id = str(user.id)
    user_role = user.role
    cache_dir = os.path.join(
        settings.MANOR_FS_ROOT,
        entity_id,
        ".slide-object-cache",
        doc_id,
    )
    # Object rendering invokes LibreOffice twice and can take long enough that
    # holding an authorization transaction would unnecessarily occupy a pool slot.
    await db.rollback()
    from packages.core.services.slide_renderer import (
        OfficeRenderLimitError,
        PresentationObjectRenderLimitError,
        open_presentation_object,
    )
    from packages.core.services.office_editing import (
        OfficeConversionLimitError,
        OfficeConverterUnavailableError,
        convert_legacy_office_for_editing_cached,
    )
    async with _document_filesystem_mutation(entity_id):
        current_doc = await get_visible_document(
            db,
            doc_id,
            entity_id,
            user_id=user_id,
            role=user_role,
        )
        if not current_doc:
            raise HTTPException(404, "Document not found")
        if not _is_pptx_document(current_doc):
            raise HTTPException(400, "Not a presentation file")
        pptx_path = _document_full_path(current_doc, entity_id)
        if not pptx_path or not os.path.isfile(pptx_path):
            raise HTTPException(404, "File not found on disk")
        source_ext = _presentation_source_format(current_doc)
        source_name = current_doc.name
        source_format = current_doc.file_type
        source_mime = current_doc.mime_type
        source_snapshot = await asyncio.to_thread(
            _capture_document_source_snapshot,
            pptx_path,
        )
        await db.rollback()

    async def render_preview():
        try:
            if source_ext in {"ppt", "dps"}:
                converted = await convert_legacy_office_for_editing_cached(
                    pptx_path,
                    source_name,
                    os.path.join(cache_dir, ".legacy-editable"),
                    source_format=source_format,
                    source_mime=source_mime,
                )
                with tempfile.TemporaryDirectory(
                    prefix="manor-presentation-editable-",
                ) as temporary_dir:
                    editable_path = os.path.join(temporary_dir, "presentation.pptx")
                    try:
                        await _run_thread_to_completion(
                            _copy_open_file_to_path,
                            converted.handle,
                            editable_path,
                        )
                    finally:
                        converted.handle.close()
                    rendered_file = await open_presentation_object(
                        editable_path,
                        cache_dir,
                        slide_index=slide_index,
                        object_id=object_id,
                    )
            else:
                rendered_file = await open_presentation_object(
                    pptx_path,
                    cache_dir,
                    slide_index=slide_index,
                    object_id=object_id,
                )
        except OfficeConverterUnavailableError as exc:
            raise HTTPException(
                503,
                "Office conversion is unavailable on this server",
            ) from exc
        except (OfficeConversionLimitError, OfficeRenderLimitError) as exc:
            raise HTTPException(413, str(exc)) from exc
        except PresentationObjectRenderLimitError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            logger.warning(
                "Presentation object rendering failed for %s slide %s object %s: %s",
                doc_id,
                slide_index,
                object_id,
                exc,
            )
            raise HTTPException(502, "Presentation object rendering failed") from exc
        return rendered_file

    rendered_file = await _finish_document_preview(
        render_preview(),
        db=db,
        doc_id=doc_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        release_result=lambda handle: handle.close(),
        source_snapshot=source_snapshot,
    )
    encoded_filename = urllib.parse.quote(
        f"slide-{slide_index + 1}-object-{object_id}.png",
    )
    return StreamingResponse(
        _stream_open_file(rendered_file),
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        },
    )


# ── Word pages (server-rendered DOCX thumbnails/fallback) ──


def _png_dimensions(path: str) -> tuple[int, int] | None:
    """Read PNG dimensions from IHDR without decoding the page image."""
    try:
        with open(path, "rb") as page_file:
            header = page_file.read(24)
    except OSError:
        return None
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        return None
    width, height = struct.unpack(">II", header[16:24])
    return (width, height) if width > 0 and height > 0 else None


@router.get("/{doc_id}/pages")
async def get_document_page_images(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return lossless, paginated preview URLs for a Word document."""
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    if not _is_docx_document(doc):
        raise HTTPException(400, "Not a Word document")
    if not doc.fs_path or not settings.MANOR_FS_ENABLED:
        raise HTTPException(404, "No file on disk")

    entity_id = str(user.entity_id)
    user_id = str(user.id)
    user_role = user.role
    cache_dir = os.path.join(
        settings.MANOR_FS_ROOT,
        entity_id,
        ".document-page-cache",
        doc_id,
    )

    # Rendering can invoke external processes for several minutes. Release the
    # read-only authorization transaction before waiting for filesystem work.
    await db.rollback()

    async with _document_filesystem_mutation(entity_id):
        current_doc = await get_visible_document(
            db,
            doc_id,
            entity_id,
            user_id=user_id,
            role=user_role,
        )
        if not current_doc:
            raise HTTPException(404, "Document not found")
        if not _is_docx_document(current_doc):
            raise HTTPException(400, "Not a Word document")
        source_path = _document_full_path(current_doc, entity_id)
        if not source_path or not os.path.isfile(source_path):
            raise HTTPException(404, "File not found on disk")
        document_ext = _document_ext(current_doc)
        source_ext = (
            f".{document_ext}"
            if document_ext in {"doc", "docx", "wps"}
            else ".docx"
        )
        source_snapshot = await asyncio.to_thread(
            _capture_document_source_snapshot,
            source_path,
        )
        await db.rollback()

    async def render_preview():
        try:
            from packages.core.services.slide_renderer import (
                OfficeRenderLimitError,
                render_document_pages,
            )

            return await render_document_pages(
                source_path,
                cache_dir,
                source_ext=source_ext,
            )
        except OfficeRenderLimitError as exc:
            raise HTTPException(413, str(exc)) from exc
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Word page rendering failed for %s: %s",
                doc_id,
                exc,
            )
            raise HTTPException(
                502,
                "High-fidelity Word preview rendering failed",
            ) from exc

    paths = await _finish_document_preview(
        render_preview(),
        db=db,
        doc_id=doc_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        source_snapshot=source_snapshot,
        accept_result=lambda paths: _publish_rendered_preview_version(
            cache_dir,
            paths,
            source_snapshot.path,
        ),
    )

    version = Path(paths[0]).parent.name if paths else ""
    pages = []
    for index, path in enumerate(paths):
        dimensions = _png_dimensions(path)
        pages.append(
            {
                "index": index,
                "url": f"/documents/{doc_id}/pages/{index}?version={version}",
                "width": dimensions[0] if dimensions else None,
                "height": dimensions[1] if dimensions else None,
            }
        )
    return {
        "pages": pages,
        "total": len(paths),
        "version": version,
    }


@router.get("/{doc_id}/pages/{page_index}")
async def get_document_page_image(
    doc_id: str,
    page_index: int,
    version: str = Query(pattern=r"^[0-9a-f]{16}$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one server-rendered Word page as a lossless PNG."""
    if not settings.MANOR_FS_ENABLED:
        raise HTTPException(404, "No file on disk")

    entity_id = str(user.entity_id)
    user_id = str(user.id)
    user_role = user.role
    cache_dir = os.path.join(
        settings.MANOR_FS_ROOT,
        entity_id,
        ".document-page-cache",
        doc_id,
    )
    await db.rollback()

    try:
        from packages.core.services.slide_renderer import open_cached_document_page

        async with _document_filesystem_mutation(entity_id):
            current_doc = await get_visible_document(
                db,
                doc_id,
                entity_id,
                user_id=user_id,
                role=user_role,
            )
            if not current_doc:
                raise HTTPException(404, "Document not found")
            if not _is_docx_document(current_doc):
                raise HTTPException(400, "Not a Word document")
            source_path = _document_full_path(current_doc, entity_id)
            if not source_path or not os.path.isfile(source_path):
                raise HTTPException(404, "File not found on disk")
            await db.rollback()
            rendered_file = await open_cached_document_page(
                cache_dir,
                version,
                page_index,
                source_path,
            )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(404, "Word preview version not found") from exc
    except IndexError as exc:
        raise HTTPException(404, "Page index out of range") from exc
    except Exception as exc:
        logger.warning(
            "Cached Word page read failed for %s version %s page %s: %s",
            doc_id,
            version,
            page_index,
            exc,
        )
        raise HTTPException(502, "Word page image could not be read") from exc

    encoded_filename = urllib.parse.quote(f"page-{page_index + 1}.png")
    return StreamingResponse(
        _stream_open_file(rendered_file),
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        },
    )


# ── Content read/write (for in-browser editor) ──

@router.put("/{doc_id}/file", response_model=DocumentResponse)
async def replace_document_file_endpoint(
    doc_id: str,
    file: UploadFile = File(...),
    save_session_id: str | None = Form(default=None, min_length=1, max_length=128),
    save_sequence: int | None = Form(default=None, ge=1, le=9_007_199_254_740_991),
    expected_source_sha256: str | None = Form(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    ),
    _gate=Depends(require_plan("storage_mb")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the binary file for an existing document."""
    if (save_session_id is None) != (save_sequence is None):
        raise HTTPException(
            status_code=422,
            detail="save_session_id and save_sequence must be provided together",
        )
    existing_doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not existing_doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        existing_doc,
        {Capability.EDIT},
        "Only the document owner/admin or a user with edit access can replace this document",
    )

    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
    chunks: list[bytes] = []
    file_size = 0
    while chunk := await file.read(1024 * 256):
        file_size += len(chunk)
        if file_size > max_bytes:
            raise HTTPException(413, f"File too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB")
        chunks.append(chunk)

    content = b"".join(chunks)
    from packages.core.services.upload_security import UploadSecurityError, inspect_upload_content
    try:
        trusted_mime_type = await inspect_upload_content(
            content,
            filename=file.filename or existing_doc.name,
            declared_content_type=file.content_type,
        )
    except UploadSecurityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    try:
        save_result = await save_document_file(
            db,
            doc_id,
            user.entity_id,
            content,
            filename=file.filename,
            mime_type=trusted_mime_type,
            created_by=(user.display_name or user.email),
            save_session_id=save_session_id,
            save_sequence=save_sequence,
            expected_source_sha256=expected_source_sha256,
            mutation_authorizer=_document_mutation_authorizer(
                user,
                {Capability.EDIT},
                "Only the document owner/admin or a user with edit access can replace this document",
            ),
        )
    except DocumentMutationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except EntityFilesystemStaleWriteError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_write_intent",
                "message": str(exc),
            },
        ) from exc
    except EntityFilesystemBusyError as exc:
        raise HTTPException(
            status_code=423,
            detail="Entity filesystem is busy with another mutation; retry shortly",
        ) from exc
    except EntityFilesystemError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Entity filesystem is temporarily unavailable: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if not save_result:
        raise HTTPException(404, "Document not found")
    doc = save_result.document

    if not save_result.replayed:
        try:
            from packages.core.tasks.ai_tasks import process_document_embeddings
            process_document_embeddings.delay(doc.id)
        except Exception:
            pass

    return await _doc_resp_for_user(db, doc, user)


@router.get("/{doc_id}/content")
async def get_document_content_endpoint(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get raw document content for editing."""
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    return await _document_read_response(doc, user, db, text_content=True)


@router.put("/{doc_id}/content")
async def save_document_content_endpoint(
    doc_id: str,
    body: SaveContentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save document content."""
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.EDIT},
        "Only the document owner/admin or a user with edit access can save this document",
    )
    try:
        ok = await save_document_content(
            db,
            doc_id,
            user.entity_id,
            body.content,
            created_by=(user.display_name or user.email),
            save_session_id=body.save_session_id,
            save_sequence=body.save_sequence,
            mutation_authorizer=_document_mutation_authorizer(
                user,
                {Capability.EDIT},
                "Only the document owner/admin or a user with edit access can save this document",
            ),
        )
    except DocumentMutationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except EntityFilesystemStaleWriteError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_write_intent",
                "message": str(exc),
            },
        ) from exc
    except EntityFilesystemBusyError as exc:
        raise HTTPException(
            status_code=423,
            detail="Entity filesystem is busy with another mutation; retry shortly",
        ) from exc
    except EntityFilesystemError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Entity filesystem is temporarily unavailable: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Document not found")
    return {"saved": True}


# ── Download (fixed sub-path — before /{doc_id}) ──

_FIRST_PAGE_THUMBNAIL_EXTS = {
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls",
}

_FIRST_PAGE_THUMBNAIL_MIME_EXTS = {
    "application/pdf": ".pdf",
    PPTX_MIME: ".pptx",
    DOCX_MIME: ".docx",
    XLSX_MIME: ".xlsx",
}


def _first_page_thumbnail_ext(doc) -> str | None:
    file_type = (getattr(doc, "file_type", None) or "").strip().lstrip(".").lower()
    file_type_ext = f".{file_type}" if file_type else ""
    name_ext = os.path.splitext(getattr(doc, "name", "") or "")[1].lower()
    mime_ext = _FIRST_PAGE_THUMBNAIL_MIME_EXTS.get((getattr(doc, "mime_type", None) or "").lower(), "")

    for ext in (file_type_ext, name_ext, mime_ext):
        if ext in _FIRST_PAGE_THUMBNAIL_EXTS:
            return ext
    return None


async def _document_first_page_thumbnail(doc, entity_id: str):
    """Render + serve a first-page JPEG thumbnail for a PDF/office document."""
    source_ext = _first_page_thumbnail_ext(doc)
    if not source_ext:
        raise HTTPException(415, "Thumbnail not available for this file type")
    if not doc.fs_path or not settings.MANOR_FS_ENABLED:
        raise HTTPException(404, "No file on disk")

    source_path = _document_full_path(doc, entity_id)
    if not source_path or not os.path.isfile(source_path):
        raise HTTPException(404, "File not found on disk")

    cache_dir = os.path.join(
        settings.MANOR_FS_ROOT, entity_id, ".doc-thumb-cache", doc.id,
    )
    from packages.core.services.slide_renderer import (
        OfficeRenderLimitError,
        open_first_page,
    )

    rendered_file = None
    try:
        rendered_file, image_path = await open_first_page(
            source_path,
            cache_dir,
            source_ext=source_ext,
        )
        response = FileResponse(
            path=image_path,
            media_type="image/jpeg",
            filename=f"{os.path.splitext(doc.name)[0] or doc.id}-thumbnail.jpg",
            headers={"Cache-Control": "private, max-age=300"},
        )
    except OfficeRenderLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except Exception as exc:
        if rendered_file is not None:
            rendered_file.close()
        logger.warning(
            "Document thumbnail render failed for %s: %s", doc.id, exc,
        )
        raise HTTPException(502, "Thumbnail rendering failed") from exc

    return rendered_file, response


async def _generate_image_thumbnail(source_path: str, target_path: str) -> None:
    """Create a bounded JPEG preview without sending the source image."""

    def _render() -> None:
        from PIL import Image, ImageOps

        temp_path = f"{target_path}.tmp"
        with Image.open(source_path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((640, 640))
            if image.mode not in {"RGB", "L"}:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            image.save(temp_path, format="JPEG", quality=82, optimize=True)
        os.replace(temp_path, target_path)

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    try:
        await asyncio.to_thread(_render)
    except Exception:
        temp_path = f"{target_path}.tmp"
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


async def _cache_thumbnail_file_response(doc, response: FileResponse) -> FileResponse:
    stored = await cache_document_blob_from_path(
        doc,
        "thumbnail",
        str(response.path),
        media_type="image/jpeg",
    )
    response.headers["X-Knowledge-Cache"] = "miss-stored" if stored else "miss-bypass"
    return response


@router.get("/{doc_id}/thumbnail")
async def document_thumbnail(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    cached_thumbnail = await get_cached_document_blob(doc, "thumbnail")
    if cached_thumbnail is not None:
        return Response(
            content=cached_thumbnail.data,
            media_type=cached_thumbnail.media_type,
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Knowledge-Cache": "redis-hit",
                "X-Thumbnail-Cache": "redis-hit",
            },
        )

    if _is_image_document(doc):
        source_path = _document_full_path(doc, user.entity_id)
        remote_source_path: str | None = None
        if source_path and os.path.isfile(source_path):
            source_mtime = os.path.getmtime(source_path)
        elif doc.file_url:
            source_stamp = getattr(doc, "updated_at", None) or getattr(doc, "created_at", None)
            source_mtime = source_stamp.timestamp() if source_stamp else 0
        else:
            raise HTTPException(404, "Image file is not available for thumbnail generation")
        thumb_path = _thumbnail_cache_path(user.entity_id, doc.id)
        disk_hit = (
            os.path.isfile(thumb_path)
            and os.path.getsize(thumb_path) > 0
            and os.path.getmtime(thumb_path) >= source_mtime
        )
        if not disk_hit:
            if not source_path or not os.path.isfile(source_path):
                remote_source_path = f"{thumb_path}.{doc.id}.source.tmp"
            try:
                if remote_source_path:
                    await _download_remote_thumbnail_source(doc.file_url, remote_source_path)
                    source_path = remote_source_path
                await _generate_image_thumbnail(source_path, thumb_path)
            finally:
                if remote_source_path and os.path.exists(remote_source_path):
                    try:
                        os.remove(remote_source_path)
                    except OSError:
                        pass
        image_response = FileResponse(
            path=thumb_path,
            media_type="image/jpeg",
            filename=f"{os.path.splitext(doc.name)[0] or doc.id}-thumbnail.jpg",
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Thumbnail-Cache": "disk-hit" if disk_hit else "generated",
            },
        )
        return await _cache_thumbnail_file_response(doc, image_response)

    if not _is_video_document(doc):
        # PDFs and office files use the renderer's content-addressed disk cache.
        entity_id = str(user.entity_id)
        user_id = str(user.id)
        user_role = user.role
        await db.rollback()
        async with _document_filesystem_mutation(entity_id):
            current_doc = await get_visible_document(
                db,
                doc_id,
                entity_id,
                user_id=user_id,
                role=user_role,
            )
            if not current_doc:
                raise HTTPException(404, "Document not found")
            document_snapshot = SimpleNamespace(
                id=current_doc.id,
                entity_id=current_doc.entity_id,
                name=current_doc.name,
                fs_path=current_doc.fs_path,
                file_url=current_doc.file_url,
                file_size=current_doc.file_size,
                file_type=current_doc.file_type,
                mime_type=current_doc.mime_type,
                created_at=current_doc.created_at,
                updated_at=current_doc.updated_at,
            )
            source_path = _document_full_path(current_doc, entity_id)
            if not source_path or not os.path.isfile(source_path):
                raise HTTPException(404, "File not found on disk")
            source_snapshot = await asyncio.to_thread(
                _capture_document_source_snapshot,
                source_path,
            )
            await db.rollback()

        async def render_preview():
            rendered_file = None
            try:
                rendered_file, first_page = await _document_first_page_thumbnail(
                    document_snapshot,
                    entity_id,
                )
                cached_response = await _cache_thumbnail_file_response(
                    document_snapshot,
                    first_page,
                )
                response_headers = {
                    key: value
                    for key, value in cached_response.headers.items()
                    if key.lower() not in {
                        "accept-ranges",
                        "content-length",
                        "content-type",
                    }
                }
                return rendered_file, response_headers
            except BaseException:
                if rendered_file is not None:
                    rendered_file.close()
                raise

        rendered_file, response_headers = await _finish_document_preview(
            render_preview(),
            db=db,
            doc_id=doc_id,
            entity_id=entity_id,
            user_id=user_id,
            user_role=user_role,
            release_result=lambda result: result[0].close(),
            source_snapshot=source_snapshot,
        )
        return StreamingResponse(
            _stream_open_file(rendered_file),
            media_type="image/jpeg",
            headers=response_headers,
        )

    source_path = _document_full_path(doc, user.entity_id)
    thumb_path = _thumbnail_cache_path(user.entity_id, doc.id)
    remote_source_path: str | None = None
    if source_path and os.path.isfile(source_path):
        source_mtime = os.path.getmtime(source_path)
    elif doc.file_url:
        source_stamp = getattr(doc, "updated_at", None) or getattr(doc, "created_at", None)
        source_mtime = source_stamp.timestamp() if source_stamp else 0
    else:
        raise HTTPException(404, "Video file is not available for thumbnail generation")

    cache_hit = os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0 and os.path.getmtime(thumb_path) >= source_mtime
    if not cache_hit:
        if not source_path or not os.path.isfile(source_path):
            remote_source_path = f"{thumb_path}.{doc.id}.source.tmp"
            try:
                await _download_remote_thumbnail_source(doc.file_url, remote_source_path)
                source_path = remote_source_path
            except Exception:
                if remote_source_path and os.path.exists(remote_source_path):
                    try:
                        os.remove(remote_source_path)
                    except OSError:
                        pass
                raise
        try:
            await _generate_video_thumbnail(source_path, thumb_path)
        finally:
            if remote_source_path and os.path.exists(remote_source_path):
                try:
                    os.remove(remote_source_path)
                except OSError:
                    pass

    video_response = FileResponse(
        path=thumb_path,
        media_type="image/jpeg",
        filename=f"{os.path.splitext(doc.name)[0] or doc.id}-thumbnail.jpg",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Thumbnail-Cache": "hit" if cache_hit else "miss",
        },
    )
    return await _cache_thumbnail_file_response(doc, video_response)


@router.get("/{doc_id}/editable-file")
async def get_editable_document_file(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return an editable OOXML copy of a legacy Office file."""
    entity_id = str(user.entity_id)
    user_id = str(user.id)
    user_role = user.role
    authorization_user = SimpleNamespace(
        id=user_id,
        entity_id=entity_id,
        role=user_role,
    )
    await db.rollback()
    async with _document_filesystem_mutation(entity_id):
        doc = await get_visible_document(
            db,
            doc_id,
            entity_id,
            user_id=user_id,
            role=user_role,
        )
        if not doc:
            raise HTTPException(404, "Document not found")
        await _require_document_capability(
            db,
            authorization_user,
            doc,
            {Capability.EDIT},
            "You do not have edit access to this document",
        )
        source_path = _document_full_path(doc, entity_id)
        if not source_path or not os.path.isfile(source_path):
            raise HTTPException(404, "The source file is not available for editing")
        source_name = doc.name
        source_format = doc.file_type
        source_mime = doc.mime_type
        source_snapshot = await asyncio.to_thread(
            _capture_document_source_snapshot,
            source_path,
        )
        source_sha256 = await asyncio.to_thread(
            _document_source_sha256,
            source_path,
        )
        await db.rollback()

    from packages.core.services.office_editing import (
        OfficeConversionLimitError,
        OfficeConverterUnavailableError,
        convert_legacy_office_for_editing,
    )

    async def convert_and_revalidate():
        converted = None
        try:
            converted = await convert_legacy_office_for_editing(
                source_path,
                source_name,
                source_format=source_format,
                source_mime=source_mime,
            )
            async with _document_filesystem_mutation(entity_id):
                current_doc = await get_visible_document(
                    db,
                    doc_id,
                    entity_id,
                    user_id=user_id,
                    role=user_role,
                )
                if current_doc is None:
                    raise HTTPException(404, "Document not found")
                await _require_document_capability(
                    db,
                    authorization_user,
                    current_doc,
                    {Capability.EDIT},
                    "You do not have edit access to this document",
                )
                current_source_path = _document_full_path(current_doc, entity_id)
                if not await asyncio.to_thread(
                    _document_source_matches,
                    source_snapshot,
                    current_source_path,
                ):
                    raise HTTPException(
                        409,
                        {
                            "code": "document_source_changed",
                            "message": "Document changed while it was being prepared for editing",
                        },
                    )
                await db.rollback()
            return converted
        except BaseException:
            if converted is not None:
                converted.handle.close()
            await _rollback_database_best_effort(db)
            raise

    try:
        converted = await _finish_document_filesystem_mutation(
            convert_and_revalidate(),
            release_result=lambda result: result.handle.close(),
        )
    except OfficeConversionLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OfficeConverterUnavailableError as exc:
        raise HTTPException(503, "Office conversion is unavailable on this server") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "The source file is not available for editing") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Legacy Office conversion failed for %s", doc_id, exc_info=True)
        raise HTTPException(502, "This Office file could not be converted for editing") from exc
    encoded_name = urllib.parse.quote(converted.filename)
    return StreamingResponse(
        _stream_open_file(converted.handle),
        media_type=converted.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Cache-Control": "private, no-store",
            "Content-Length": str(converted.size),
            "X-Manor-Source-SHA256": source_sha256,
        },
    )


@router.get("/{doc_id}/download")
async def download_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")

    return await _document_read_response(doc, user, db, download=True)


@router.get("/{doc_id}/preview/content")
async def preview_document_content(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Inline bytes for authorized viewing, separate from the download action."""
    doc = await get_visible_document(
        db, doc_id, user.entity_id, user_id=user.id, role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    response = await _document_read_response(doc, user, db)
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = "sandbox"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def _metadata_document_file_response(
    *, name: str, file_type: str | None, mime_type: str | None, content: str | None,
) -> Response:
    """Serialize inline editor content using the same format contract for private/public reads."""
    from packages.core.services.office_editing import OfficeConversionFactory, OfficeFileFormat

    source_format = OfficeConversionFactory.resolve_format(
        name, source_format=file_type, source_mime=mime_type,
    )
    title = os.path.splitext(name)[0] or "Document"
    if source_format in {OfficeFileFormat.DOCX, OfficeFileFormat.DOC, OfficeFileFormat.WPS}:
        from packages.core.services.docgen_service import generate_docx
        from packages.core.services.document_service import _editor_html_to_docgen_text

        data = await generate_docx(title, _editor_html_to_docgen_text(content or ""))
        mime_type = DOCX_MIME
        name = f"{title}.docx"
    elif source_format in {OfficeFileFormat.PPTX, OfficeFileFormat.PPT, OfficeFileFormat.DPS}:
        data = await _generate_pptx_bytes(title, content or "")
        mime_type = PPTX_MIME
        name = f"{title}.pptx"
    elif source_format is not None:
        # A text/HTML metadata value is not a serialized workbook. Never label
        # those bytes as XLSX (or invent an empty workbook and lose content).
        raise HTTPException(415, "Original spreadsheet file is unavailable")
    elif content is not None:
        data = content.encode("utf-8")
    else:
        raise HTTPException(404, "File not found")
    return Response(
        content=data,
        media_type=mime_type or "text/plain",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"},
    )


async def _document_read_response(
    doc: Document, user: User, db: AsyncSession, *, download: bool = False,
    text_content: bool = False,
):
    # Cache I/O can suspend an authorized request while its grant is revoked.
    # Reload policy afterwards; keep physical bytes stable through the response
    # snapshot, and hold Workspace -> folder -> document locks while reading DB
    # content. Never wait for the filesystem lock with policy locks held.
    doc_id, entity_id = str(doc.id), str(user.entity_id)
    credential = AuthenticatedUserCredential.from_user(user)
    had_file = bool(doc.fs_path)
    cache_kind = "content" if text_content else "download"
    cache_key = document_hot_cache_key(doc, cache_kind)
    cached_content = None
    if topic_ledger_workspace_id_for_document(doc) is None:
        if text_content:
            cached_content = await get_cached_document_text(doc)
        elif _download_is_hot_cacheable(doc):
            cached_content = await get_cached_document_blob(doc, cache_kind)
    await db.rollback()
    read_boundary = None
    transferred = False
    try:
        for _attempt in range(2):
            doc = await get_document_for_update(db, doc_id, entity_id)
            try:
                authorized = await ResourcePermissionGate.authorize_document_read(
                    db, credential=credential, document=doc, download=download,
                )
            except DocumentDownloadNotAllowed as exc:
                raise HTTPException(403, "Document download is not allowed") from exc
            if authorized is None:
                raise HTTPException(404, "Document not found")
            authorization_user = SimpleNamespace(
                id=authorized.actor.user_id,
                entity_id=authorized.actor.entity_id,
                role=authorized.actor.role,
            )
            is_live_topic_ledger = topic_ledger_workspace_id_for_document(doc) is not None
            if is_live_topic_ledger or cache_key != document_hot_cache_key(doc, cache_kind):
                cached_content = None
            source_path = (
                _document_full_path(doc, entity_id)
                if cached_content is None and doc.fs_path and not is_live_topic_ledger else None
            )
            if source_path and not os.path.isfile(source_path):
                source_path = None
            # Cache, remote and metadata-only responses require current DB
            # authorization, but must not depend on a mounted/writable disk.
            if source_path is None or read_boundary is not None:
                break
            if not had_file:
                raise HTTPException(409, "Document storage changed; retry")
            # Resolve again after acquiring the physical boundary, always in
            # filesystem -> Workspace -> folder -> document lock order.
            await db.rollback()
            boundary = entity_filesystem_read_boundary(_entity_root(entity_id))
            await boundary.__aenter__()
            read_boundary = boundary

        if text_content:
            content = cached_content
            cache_status = "redis-hit"
            if content is None:
                content = await get_document_content(
                    db, doc_id, entity_id, allow_filesystem=source_path is not None,
                )
                if content is None:
                    raise HTTPException(404, "Document not found or no content")
                stored = not is_live_topic_ledger and await cache_document_text(doc, content)
                cache_status = "miss-stored" if stored else "miss-bypass"
            result = JSONResponse(
                {"content": content}, headers={"X-Knowledge-Cache": cache_status},
            )
        else:
            result = await _authorized_document_file_response(
                doc, authorization_user, db, cached_download=cached_content,
                read_boundary=read_boundary, source_path=source_path,
            )
        await db.commit()
        transferred = isinstance(result, EntitySnapshotFileResponse)
        return result
    except DocumentMutationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        if read_boundary is not None and not transferred:
            await read_boundary.__aexit__(None, None, None)


async def _authorized_document_file_response(
    doc: Document, user: User, db: AsyncSession, *, cached_download, read_boundary,
    source_path: str | None,
):
    is_live_topic_ledger = topic_ledger_workspace_id_for_document(doc) is not None
    hot_cacheable_download = _download_is_hot_cacheable(doc) and not is_live_topic_ledger
    if cached_download is not None:
        encoded_name = urllib.parse.quote(doc.name or "download")
        return Response(
            content=cached_download.data,
            media_type=cached_download.media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
                "Cache-Control": "private, max-age=300",
                "X-Knowledge-Cache": "redis-hit",
            },
        )

    # Only read the source selected under the physical/authorization boundary.
    if source_path is not None:
        _mark_document_file_available(doc, source="filesystem")
        cached = (
            await cache_document_blob_from_path(
                doc,
                "download",
                source_path,
                media_type=doc.mime_type or "application/octet-stream",
            )
            if hot_cacheable_download
            else False
        )
        return EntitySnapshotFileResponse(
            path=source_path,
            read_boundary=read_boundary,
            media_type=doc.mime_type or "application/octet-stream",
            filename=doc.name,
            headers={"X-Knowledge-Cache": "miss-stored" if cached else "miss-bypass"},
        )

    # Fallback to file_url (e.g. S3 / external storage)
    if doc.file_url and not is_live_topic_ledger:
        return await _remote_document_stream_response(
            doc.file_url,
            filename=doc.name,
            media_type=doc.mime_type,
        )

    # Fallback: serve editor content from metadata/DB. Binary office editors
    # can exist as metadata-only rows when local FS is disabled; synthesize a
    # real Office file so the import pipeline can still open and recover.
    from packages.core.services.office_editing import OfficeConversionFactory

    name, file_type, mime_type = doc.name, doc.file_type, doc.mime_type
    metadata_cacheable = hot_cacheable_download and OfficeConversionFactory.resolve_format(
        name, source_format=file_type, source_mime=mime_type,
    ) is None
    content = await get_document_content(db, doc.id, user.entity_id, allow_filesystem=False)
    result = await _metadata_document_file_response(
        name=name, file_type=file_type, mime_type=mime_type, content=content,
    )
    cached = metadata_cacheable and await cache_document_blob(
        doc, "download", result.body, media_type=result.media_type,
    )
    result.headers["X-Knowledge-Cache"] = "miss-stored" if cached else "miss-bypass"
    return result


# ── Versions (sub-path of /{doc_id} — before bare /{doc_id}) ──

def _ver_resp(v) -> DocumentVersionResponse:
    return DocumentVersionResponse(
        id=v.id, document_id=v.document_id,
        version_number=v.version_number, name=v.name,
        fs_path=v.fs_path, file_size=v.file_size,
        change_summary=v.change_summary,
        created_by=v.created_by,
        created_at=v.created_at.isoformat() if v.created_at else None,
    )


@router.get("/{doc_id}/versions", response_model=list[DocumentVersionResponse])
async def list_document_versions(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    versions = await list_versions(db, doc_id, user.entity_id)
    return [_ver_resp(v) for v in versions]


@router.post("/{doc_id}/versions", response_model=DocumentVersionResponse, status_code=201)
async def create_document_version(
    doc_id: str,
    req: CreateVersionRequest = CreateVersionRequest(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.EDIT},
        "Only the document owner/admin or a user with edit access can create versions",
    )
    try:
        version = await create_version(
            db, doc_id, user.entity_id,
            change_summary=req.change_summary,
            created_by=(user.display_name or user.email),
        )
    except ValueError:
        raise HTTPException(404, "Document not found")
    return _ver_resp(version)


# ── Trash / Restore per document ──

@router.post("/{doc_id}/trash", status_code=200)
async def trash_one_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entity_id = str(user.entity_id)

    async def persist_trash() -> bool:
        # Load and authorize only after the filesystem lock is held.  Waiting
        # for an in-flight writer must not retain a database transaction or use
        # permissions/path state captured before the wait.
        try:
            locked_doc = await get_document_for_update(
                db,
                doc_id,
                entity_id,
            )
        except DocumentMutationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        if locked_doc is None:
            return False
        visible_doc = await get_visible_document(
            db,
            doc_id,
            entity_id,
            user_id=user.id,
            role=user.role,
        )
        if not visible_doc:
            raise HTTPException(404, "Document not found")
        await _require_document_delete(
            db,
            user,
            locked_doc,
            "Only the document owner/admin or a user with delete access can trash this document",
        )
        doc = locked_doc

        original_fs_path = str(doc.fs_path) if doc.fs_path else None
        trashed_fs_path = (
            os.path.join(
                ".trash",
                "documents",
                doc.id,
                os.path.basename(original_fs_path),
            )
            if original_fs_path
            else None
        )
        cancels_ai_draft = (
            doc.vector_status == VectorStatus.GENERATING
            and _is_unrecoverable_ai_draft(doc)
        )
        if cancels_ai_draft:
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                extra={"restore_blocked_reason": AI_DRAFT_CANCELLED_BY_TRASH},
            )
        try:
            ok = await trash_document(
                db,
                doc_id,
                entity_id,
                trashed_by=(user.display_name or user.email),
            )
            if not ok:
                return False

            # Clear embedding to free vector index space.
            from sqlalchemy import update as sa_update
            from packages.core.models.document import Document as DocModel
            from packages.core.models.permission import (
                ResourceGrant,
                ResourceGrantPending,
                Share,
            )

            revoked_at = datetime.now(timezone.utc)
            await db.execute(
                sa_update(Share)
                .where(
                    Share.entity_id == entity_id,
                    Share.resource_type == "document",
                    Share.resource_id == doc_id,
                    Share.status == "active",
                )
                .values(status="revoked", revoked_at=revoked_at, revoked_by=user.id)
            )
            await db.execute(
                sa_update(ResourceGrant)
                .where(
                    ResourceGrant.entity_id == entity_id,
                    ResourceGrant.resource_type == "document",
                    ResourceGrant.resource_id == doc_id,
                    ResourceGrant.status == "active",
                )
                .values(status="revoked", revoked_at=revoked_at, revoked_by=user.id)
            )
            await db.execute(
                sa_update(ResourceGrantPending)
                .where(
                    ResourceGrantPending.entity_id == entity_id,
                    ResourceGrantPending.resource_id == doc_id,
                    ResourceGrantPending.status == "pending",
                )
                .values(
                    status="denied",
                    decided_by=user.id,
                    decided_at=revoked_at,
                    decision_note="Document moved to Trash",
                )
            )

            await db.execute(
                sa_update(DocModel)
                .where(DocModel.id == doc_id)
                .values(
                    vector_status=(
                        VectorStatus.FAILED
                        if cancels_ai_draft
                        else VectorStatus.PENDING
                    )
                )
            )
            try:
                # A savepoint keeps rolling-schema compatibility from aborting
                # the outer trash transaction when pgvector is unavailable.
                async with db.begin_nested():
                    await db.execute(
                        text("UPDATE documents SET embedding = NULL WHERE id = :id"),
                        {"id": doc_id},
                    )
            except Exception:
                pass  # pgvector column may not exist
            await db.commit()
        except BaseException:
            try:
                await _restore_trashed_document_file_after_failure(
                    entity_id,
                    original_fs_path=original_fs_path,
                    trashed_fs_path=trashed_fs_path,
                )
            except Exception:
                logger.exception(
                    "Could not restore document file after trash transaction failure",
                )
            await _rollback_database_best_effort(db)
            raise

        await _invalidate_committed_document_cache(entity_id)
        return True

    if settings.MANOR_FS_ENABLED:
        # Authentication dependencies share this session and may have updated
        # membership/impersonation state. Commit that work and release the
        # connection before a potentially five-second filesystem-lock wait.
        await db.commit()
        async with _document_filesystem_mutation(entity_id):
            ok = await _finish_document_filesystem_mutation(persist_trash())
    else:
        ok = await _finish_document_filesystem_mutation(persist_trash())
    if not ok:
        raise HTTPException(404, "Document not found")
    return {"trashed": True}


@router.post("/{doc_id}/restore", status_code=200)
async def restore_one_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entity_id = str(user.entity_id)

    async def persist_restore() -> bool:
        # Load and authorize after acquiring the entity lock so both lifecycle
        # state and permissions reflect changes committed while this waited.
        try:
            doc = await get_document_for_update(
                db,
                doc_id,
                entity_id,
                include_trashed=True,
            )
        except DocumentMutationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        if doc is None:
            raise HTTPException(404, "Document not found or not trashed")
        await _require_document_delete(
            db,
            user,
            doc,
            "Only the document owner/admin or a user with delete access can restore this document",
        )

        trashed_fs_path = str(doc.fs_path) if doc.fs_path else None
        committed = False
        try:
            if _is_unrecoverable_ai_draft(doc):
                doc.vector_status = VectorStatus.FAILED
                doc.metadata_ = merge_document_metadata(
                    doc.metadata_,
                    extra={"restore_blocked_reason": AI_DRAFT_CANCELLED_BY_TRASH},
                )
                await db.commit()
                await _invalidate_committed_document_cache(entity_id)
                raise DocumentRestoreConflict(
                    "This AI Draft was trashed before generation finished and has no content to restore"
                )
            ok = await restore_document(db, doc_id, entity_id)
            if not ok:
                return False
            source_url = _url_document_source_to_fetch(doc, entity_id)
            doc.vector_status = VectorStatus.PENDING
            await db.commit()
            committed = True
            if source_url:
                await _dispatch_url_fetch_and_invalidate_cache(
                    db,
                    doc,
                    entity_id,
                    source_url,
                )
            else:
                await _dispatch_document_embeddings_and_invalidate_cache(
                    doc.id,
                    entity_id,
                )
        except DocumentRestoreConflict:
            await _rollback_database_best_effort(db)
            raise
        except BaseException:
            if not committed:
                restored_fs_path = doc.__dict__.get("fs_path")
                try:
                    await _retrash_restored_document_file_after_failure(
                        entity_id,
                        restored_fs_path=(str(restored_fs_path) if restored_fs_path else None),
                        trashed_fs_path=trashed_fs_path,
                    )
                except Exception:
                    logger.exception(
                        "Could not retrash document file after restore transaction failure",
                    )
            await _rollback_database_best_effort(db)
            raise
        return True

    try:
        if settings.MANOR_FS_ENABLED:
            await db.commit()
            async with _document_filesystem_mutation(entity_id):
                ok = await _finish_document_filesystem_mutation(persist_restore())
        else:
            ok = await _finish_document_filesystem_mutation(persist_restore())
    except DocumentRestoreConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Document not found or not trashed")
    return {"restored": True}


# ── Folders (MUST be before /{doc_id} wildcard) ──

class FolderResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    parent_id: str | None = None
    document_count: int = 0
    created_at: str | None = None
    # ── Permission-v1 (RFC §13.3) ──────────────────────────────────────
    visibility: str | None = None
    classification: str | None = None
    owner_id: str | None = None
    client_visible: bool | None = None
    current_user_capabilities: list[str] = Field(default_factory=list)


class DocumentBrowseResponse(DocumentListResponse):
    folders: list[FolderResponse] = Field(default_factory=list)
    documents: list[DocumentResponse] = Field(default_factory=list)
    total_folders: int = 0
    total_documents: int = 0
    direct_total_files: int = 0
    direct_total_size: int = 0
    max_upload_mb: int


def _folder_resp(
    f,
    document_count: int = 0,
    *,
    current_user_capabilities: set[str] | None = None,
) -> FolderResponse:
    return FolderResponse(
        id=f.id, entity_id=f.entity_id, name=f.name,
        parent_id=f.parent_id, document_count=document_count,
        created_at=f.created_at.isoformat() if f.created_at else None,
        visibility=getattr(f, "visibility", None),
        classification=getattr(f, "classification", None),
        owner_id=getattr(f, "owner_id", None),
        client_visible=getattr(f, "client_visible", None),
        current_user_capabilities=_ordered_capabilities(current_user_capabilities or set()),
    )


async def _folder_resp_for_user(
    db: AsyncSession,
    f,
    user: User,
    *,
    document_count: int = 0,
    access_ctx: DocumentAccessContext | None = None,
) -> FolderResponse:
    capabilities: set[str]
    can_manage = bool(getattr(f, "owner_id", None) == user.id)
    if not can_manage:
        can_manage = (
            access_ctx.is_admin
            if access_ctx is not None
            else await user_is_effective_entity_admin(db, user)
        )
    if can_manage:
        capabilities = set(_FOLDER_OWNER_CAPABILITIES)
    else:
        if access_ctx is not None:
            capabilities = access_ctx.folder_capabilities(getattr(f, "id", None))
        else:
            capabilities = await folder_grant_capabilities_for_user(
                db,
                entity_id=user.entity_id,
                folder_id=getattr(f, "id", None),
                user_id=user.id,
            )
        capabilities.add(Capability.VIEW)
    return _folder_resp(
        f,
        document_count=document_count,
        current_user_capabilities=capabilities,
    )


class CreateFolderRequest(BaseModel):
    name: str
    parent_id: str | None = None


class RenameFolderRequest(BaseModel):
    name: str


class MoveFolderRequest(BaseModel):
    parent_id: str | None = None


async def _load_document_folders(db: AsyncSession, entity_id: str):
    from sqlalchemy import select
    from packages.core.models.document import DocumentFolder

    result = await db.execute(
        select(DocumentFolder)
        .where(DocumentFolder.entity_id == entity_id)
        .execution_options(populate_existing=True)
    )
    folders = list(result.scalars().all())
    return folders, {f.id: f for f in folders}


def _folder_counts_from_direct_counts(
    folders: list,
    direct_counts: dict[str, int],
) -> dict[str, int]:
    """Roll authorized direct counts up through the supplied Folder topology."""
    direct_counts = {str(folder_id): count for folder_id, count in direct_counts.items()}
    child_ids_by_parent: dict[str | None, list[str]] = {}
    for folder in folders:
        child_ids_by_parent.setdefault(folder.parent_id, []).append(str(folder.id))
    count_cache: dict[str, int] = {}

    def recursive_document_count(folder_id: str, seen: set[str] | None = None) -> int:
        if folder_id in count_cache:
            return count_cache[folder_id]
        seen = set() if seen is None else seen
        if folder_id in seen:
            return direct_counts.get(folder_id, 0)
        seen.add(folder_id)
        total = direct_counts.get(folder_id, 0)
        for child_id in child_ids_by_parent.get(folder_id, []):
            total += recursive_document_count(child_id, seen.copy())
        count_cache[folder_id] = total
        return total

    return {str(folder.id): recursive_document_count(str(folder.id)) for folder in folders}


def _folder_counts_from_documents(folders: list, documents: list[Document]) -> dict[str, int]:
    """Project folder counts from one already-authorized Document snapshot."""
    direct_counts: dict[str, int] = {}
    for document in documents:
        if document.folder_id:
            direct_counts[str(document.folder_id)] = direct_counts.get(str(document.folder_id), 0) + 1
    return _folder_counts_from_direct_counts(folders, direct_counts)


async def _authorized_folder_listing_snapshot(
    db: AsyncSession,
    user: User,
    *,
    credential: AuthenticatedUserCredential,
    folders: list[DocumentFolder],
) -> tuple[list[DocumentFolder], dict[str, int], DocumentAccessContext]:
    """Finalize counts first, then Folder rows through the last permission Gate."""
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    folder_ids = {str(folder.id) for folder in folders}

    async def authorize_document_batch(documents: list[Document]) -> list[Document]:
        authorized = await ResourcePermissionGate.authorize_document_batch_read(
            db,
            credential=credential,
            documents=documents,
        )
        if authorized is None:
            raise HTTPException(404, "Document not found")
        return list(authorized.documents)

    direct_counts = await visible_document_counts_by_folder(
        db,
        user.entity_id,
        folder_ids=folder_ids,
        user_id=user.id,
        role=user.role,
        authorize_batch=authorize_document_batch,
    )
    current_folders, current_folder_by_id = await _load_document_folders(
        db, user.entity_id,
    )
    authorized_folders = await ResourcePermissionGate.authorize_folder_batch_read(
        db,
        credential=credential,
        folders=current_folders,
    )
    if authorized_folders is None:
        raise HTTPException(404, "Folder not found")
    visible_folders = [
        folder
        for folder in authorized_folders.folders
        if is_user_visible_folder_path(_folder_rel_path(folder, current_folder_by_id))
    ]
    return (
        visible_folders,
        _folder_counts_from_direct_counts(visible_folders, direct_counts),
        authorized_folders.access_context,
    )


def _folder_rel_path(folder, folder_by_id: dict[str, object]) -> str:
    parts: list[str] = []
    current = folder
    seen: set[str] = set()
    while current and current.id not in seen:
        seen.add(current.id)
        parts.append(current.name)
        current = folder_by_id.get(current.parent_id) if current.parent_id else None
    return "/".join(reversed(parts))


def _folder_subtree_ids(folders: list, root_id: str) -> set[str]:
    child_ids_by_parent: dict[str | None, list[str]] = {}
    for folder in folders:
        child_ids_by_parent.setdefault(folder.parent_id, []).append(folder.id)

    subtree_ids: set[str] = set()
    stack = [root_id]
    while stack:
        current_id = stack.pop()
        if current_id in subtree_ids:
            continue
        subtree_ids.add(current_id)
        stack.extend(child_ids_by_parent.get(current_id, []))
    return subtree_ids


async def _user_can_read_folder_path(
    db: AsyncSession,
    folder,
    folder_by_id: dict[str, object],
    user: User,
) -> bool:
    current = folder
    seen: set[str] = set()
    while current and current.id not in seen:
        seen.add(current.id)
        if not await user_can_read_folder(
            db,
            current,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
        ):
            return False
        current = folder_by_id.get(current.parent_id) if current.parent_id else None
    return True


async def _visible_document_folders_for_user(
    db: AsyncSession,
    user: User,
    *,
    credential: AuthenticatedUserCredential | None = None,
) -> tuple[list, dict[str, object], DocumentAccessContext]:
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    credential = credential or AuthenticatedUserCredential.from_user(user)
    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    authorized = await ResourcePermissionGate.authorize_folder_batch_read(
        db,
        credential=credential,
        folders=folders,
    )
    if authorized is None:
        raise HTTPException(404, "Folder not found")
    visible_folders = []
    for f in authorized.folders:
        if not is_user_visible_folder_path(_folder_rel_path(f, folder_by_id)):
            continue
        visible_folders.append(f)
    return (
        visible_folders,
        {f.id: f for f in visible_folders},
        authorized.access_context,
    )


_GENERATED_MEDIA_SOURCES = frozenset(
    {"ai_generated", "sandbox", "bash", "agent", "elevenlabs", "mcp"}
)
_GENERATED_MEDIA_FILE_TYPES = frozenset(
    {"png", "jpg", "jpeg", "webp", "gif", "mp4", "mov", "webm", "mp3", "wav", "m4a"}
)


def _document_matches_browse_filters(
    document: Document,
    *,
    search_query: str | None,
    workspace_id: str | None,
    include_generated_assets: bool,
    access_context: DocumentAccessContext,
) -> bool:
    """Reapply mutable SQL scope fields to a Gate-refreshed Document."""
    from packages.core.services.knowledge_visibility import is_user_visible_path

    fs_path = str(document.fs_path or "")
    if fs_path and not is_user_visible_path(fs_path):
        return False
    if not include_generated_assets and document.source in _GENERATED_MEDIA_SOURCES:
        mime_type = str(document.mime_type or "").lower()
        if (
            mime_type.startswith(("image/", "video/", "audio/"))
            or document.file_type in _GENERATED_MEDIA_FILE_TYPES
        ):
            return False
    if search_query:
        metadata_text = json.dumps(
            document.metadata_ if isinstance(document.metadata_, dict) else {},
            ensure_ascii=False,
            default=str,
        )
        searchable_values = (
            document.name,
            document.fs_path,
            document.file_type,
            document.mime_type,
            document.source,
            metadata_text,
        )
        if not any(search_query in str(value or "").lower() for value in searchable_values):
            return False
    if (
        workspace_id
        and workspace_id not in access_context.document_workspace_ids(document)
    ):
        return False
    return True


@router.get("/browse", response_model=DocumentBrowseResponse)
async def browse_documents(
    search: str | None = Query(None),
    folder_id: str | None = Query(None),
    workspace_id: str | None = Query(None),
    scope: str | None = Query(None),
    include_generated_assets: bool = Query(True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.plan_gate import check as _plan_check

    credential = AuthenticatedUserCredential.from_user(user)
    search_text = (search or "").strip()
    search_query = search_text or None
    global_document_scope = scope == "all" or bool(workspace_id)
    browse_folder_id = None if folder_id in (None, "", "root") else folder_id

    visible_folders, visible_folder_by_id, _access_ctx = await _visible_document_folders_for_user(
        db,
        user,
        credential=credential,
    )
    if browse_folder_id and browse_folder_id not in visible_folder_by_id:
        raise HTTPException(404, "Folder not found")

    if global_document_scope and not search_query:
        document_folder_id = None
    elif search_query:
        document_folder_id = None
    else:
        document_folder_id = browse_folder_id or "root"

    docs, _total = await list_visible_documents(
        db,
        user.entity_id,
        name_search=search_query,
        folder_id=document_folder_id,
        workspace_id=workspace_id,
        user_id=user.id,
        role=user.role,
        include_generated_assets=include_generated_assets,
        limit=None,
        offset=0,
    )
    if not workspace_id and (search_query or scope == "all"):
        docs = [
            document for document in docs
            if not document.folder_id or document.folder_id in visible_folder_by_id
        ]

    gate = await _plan_check(db, user.entity_id, "storage_mb")

    preflight_folders = await ResourcePermissionGate.authorize_folder_batch_read(
        db,
        credential=credential,
        folders=visible_folders,
    )
    if preflight_folders is None:
        raise HTTPException(404, "Folder not found")
    preflight_visible_folders = list(preflight_folders.folders)
    if browse_folder_id and browse_folder_id not in preflight_folders.folder_ids:
        raise HTTPException(404, "Folder not found")
    include_root_documents_in_storage = False
    if workspace_id:
        storage_folder_ids = None
    elif browse_folder_id and not search_query and scope != "all":
        storage_folder_ids = _folder_subtree_ids(
            preflight_visible_folders, browse_folder_id,
        )
    else:
        storage_folder_ids = set(preflight_folders.folder_ids)
        include_root_documents_in_storage = True
    storage_documents, _storage_total = await list_visible_documents(
        db,
        preflight_folders.actor.entity_id,
        name_search=search_query,
        folder_ids=storage_folder_ids,
        workspace_id=workspace_id,
        user_id=preflight_folders.actor.user_id,
        role=preflight_folders.actor.role,
        include_generated_assets=include_generated_assets,
        limit=None,
        offset=0,
    )
    if include_root_documents_in_storage and storage_folder_ids is not None:
        root_documents, _root_total = await list_visible_documents(
            db,
            preflight_folders.actor.entity_id,
            name_search=search_query,
            folder_id="root",
            workspace_id=workspace_id,
            user_id=preflight_folders.actor.user_id,
            role=preflight_folders.actor.role,
            include_generated_assets=include_generated_assets,
            limit=None,
            offset=0,
        )
        storage_documents.extend(root_documents)
    candidate_documents = {
        str(document.id): document
        for document in [*docs, *storage_documents]
    }
    final_documents = await ResourcePermissionGate.authorize_document_batch_read(
        db,
        credential=credential,
        documents=list(candidate_documents.values()),
        workspace_id=workspace_id,
    )
    if final_documents is None:
        raise HTTPException(404, "Document not found")

    # DocumentAccessContext refreshes overlapping Folder identity-map rows.
    # Reload and authorize all Folders afterwards so folder metadata,
    # capabilities and the topology used below come from the final Gate.
    current_folders, current_folder_by_id = await _load_document_folders(
        db, final_documents.actor.entity_id,
    )
    final_folders = await ResourcePermissionGate.authorize_folder_batch_read(
        db,
        credential=credential,
        folders=current_folders,
    )
    if final_folders is None:
        raise HTTPException(404, "Folder not found")
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    final_visible_folders = [
        folder
        for folder in final_folders.folders
        if is_user_visible_folder_path(_folder_rel_path(folder, current_folder_by_id))
    ]
    final_visible_folder_ids = {str(folder.id) for folder in final_visible_folders}
    if browse_folder_id and browse_folder_id not in final_visible_folder_ids:
        raise HTTPException(404, "Folder not found")

    authorized_documents = [
        document
        for document in final_documents.documents
        if _document_matches_browse_filters(
            document,
            search_query=search_query.lower() if search_query else None,
            workspace_id=workspace_id,
            include_generated_assets=include_generated_assets,
            access_context=final_documents.access_context,
        )
        and (
            workspace_id is not None
            or document.folder_id is None
            or str(document.folder_id) in final_visible_folder_ids
        )
    ]
    if global_document_scope or search_query:
        docs = authorized_documents
    else:
        docs = [
            document
            for document in authorized_documents
            if (str(document.folder_id) if document.folder_id else None) == browse_folder_id
        ]

    if workspace_id:
        storage_documents = authorized_documents
    elif browse_folder_id and not search_query and scope != "all":
        final_storage_folder_ids = _folder_subtree_ids(
            final_visible_folders, browse_folder_id,
        )
        storage_documents = [
            document
            for document in authorized_documents
            if str(document.folder_id or "") in final_storage_folder_ids
        ]
    else:
        storage_documents = authorized_documents

    if global_document_scope and not search_query:
        direct_folders = []
    elif search_query:
        q = search_query.lower()
        direct_folders = [
            folder
            for folder in final_visible_folders
            if q in (folder.name or "").lower()
        ]
    else:
        direct_folders = [
            folder
            for folder in final_visible_folders
            if (folder.parent_id or None) == browse_folder_id
        ]
    direct_folders.sort(key=lambda folder: (folder.name or "").lower())

    items = [
        await _doc_resp_for_user(db, document, user, final_documents.access_context)
        for document in docs
    ]
    total = len(docs)
    direct_total_size = sum(
        int(getattr(document, "file_size", None) or 0)
        for document in docs
    )
    total_files = len(storage_documents)
    total_size = sum(
        int(getattr(document, "file_size", None) or 0)
        for document in storage_documents
    )
    folder_counts = _folder_counts_from_documents(
        final_visible_folders,
        storage_documents,
    )
    folder_responses = [
        await _folder_resp_for_user(
            db,
            folder,
            user,
            document_count=folder_counts.get(folder.id, 0),
            access_ctx=final_folders.access_context,
        )
        for folder in direct_folders
    ]

    return DocumentBrowseResponse(
        items=items,
        documents=items,
        folders=folder_responses,
        total=total,
        total_documents=total,
        total_folders=len(folder_responses),
        direct_total_files=total,
        direct_total_size=direct_total_size,
        total_files=total_files,
        total_size=total_size,
        storage_used_mb=gate.current,
        storage_limit_mb=gate.limit,
        max_upload_mb=settings.MANOR_MAX_UPLOAD_MB,
    )


@router.get("/indexing-status", response_model=list[DocumentIndexingStatusResponse])
async def document_indexing_statuses(
    ids: list[str] | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return only mutable indexing fields for visible documents.

    Knowledge uses this endpoint while a small set of documents is pending or
    processing. It avoids rebuilding and transferring the full folder browse
    payload every five seconds.
    """

    document_ids = list(dict.fromkeys(ids or []))
    if len(document_ids) > 100:
        raise HTTPException(422, "At most 100 document IDs may be polled")

    statuses: list[DocumentIndexingStatusResponse] = []
    for document_id in document_ids:
        document = await get_visible_document(
            db,
            document_id,
            user.entity_id,
            user_id=user.id,
            role=user.role,
        )
        if document is None:
            continue
        metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
        indexing = metadata.get("indexing")
        statuses.append(
            DocumentIndexingStatusResponse(
                id=document.id,
                vector_status=document.vector_status,
                indexing_progress=indexing if isinstance(indexing, dict) else None,
            )
        )
    return statuses


@router.get("/folder-tree", response_model=list[FolderResponse])
async def document_folder_tree(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    credential = AuthenticatedUserCredential.from_user(user)
    visible_folders, _, _access_ctx = await _visible_document_folders_for_user(
        db, user, credential=credential,
    )
    visible_folders, folder_counts, access_context = (
        await _authorized_folder_listing_snapshot(
            db,
            user,
            credential=credential,
            folders=visible_folders,
        )
    )
    return [
        await _folder_resp_for_user(
            db, f, user,
            document_count=folder_counts.get(f.id, 0),
            access_ctx=access_context,
        )
        for f in sorted(
            visible_folders,
            key=lambda f: ((f.parent_id or ""), (f.name or "").lower()),
        )
    ]


async def _validate_folder_position(
    db: AsyncSession,
    entity_id: str,
    *,
    name: str,
    parent_id: str | None,
    folder_id: str | None = None,
) -> tuple[list, dict[str, object]]:
    """Validate a logical Knowledge folder name/parent pair."""
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    clean_name = name.strip()
    if not clean_name or "/" in clean_name or "\\" in clean_name:
        raise HTTPException(400, "Folder name must be a single path segment")

    folders, folder_by_id = await _load_document_folders(db, entity_id)
    if parent_id and parent_id not in folder_by_id:
        raise HTTPException(404, "Parent folder not found")

    if folder_id:
        if parent_id == folder_id:
            raise HTTPException(400, "Cannot move a folder into itself")
        current = folder_by_id.get(parent_id) if parent_id else None
        while current:
            if current.id == folder_id:
                raise HTTPException(400, "Cannot move a folder into its own child")
            current = folder_by_id.get(current.parent_id) if current.parent_id else None

    parent_parts: list[str] = []
    parent = folder_by_id.get(parent_id) if parent_id else None
    if parent:
        parent_parts = [p for p in _folder_rel_path(parent, folder_by_id).split("/") if p]
    candidate_path = "/".join([*parent_parts, clean_name])
    if not is_user_visible_folder_path(candidate_path):
        raise HTTPException(400, "Folder path is reserved for system use")

    duplicate = next(
        (
            f for f in folders
            if f.parent_id == parent_id and f.name == clean_name and f.id != folder_id
        ),
        None,
    )
    if duplicate:
        raise HTTPException(409, "A folder with that name already exists here")
    return folders, folder_by_id


@router.get("/folders", response_model=list[FolderResponse])
async def list_folders(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    credential = AuthenticatedUserCredential.from_user(user)
    visible_folders, _, _access_ctx = await _visible_document_folders_for_user(
        db, user, credential=credential,
    )
    visible_folders, folder_counts, access_context = (
        await _authorized_folder_listing_snapshot(
            db,
            user,
            credential=credential,
            folders=visible_folders,
        )
    )
    visible_folders = sorted(visible_folders, key=lambda f: f.created_at, reverse=True)

    return [
        await _folder_resp_for_user(
            db, f, user,
            document_count=folder_counts.get(f.id, 0),
            access_ctx=access_context,
        )
        for f in visible_folders
    ]


@router.post("/folders", response_model=FolderResponse, status_code=201)
async def create_folder(
    body: CreateFolderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import DocumentFolder
    await _require_document_upload(db, user)
    parent = None
    if body.parent_id:
        _, parent_by_id = await _load_document_folders(db, user.entity_id)
        parent = parent_by_id.get(body.parent_id)
        if parent is None:
            raise HTTPException(404, "Folder not found")
        await _require_folder_capability(
            db,
            user,
            parent,
            {Capability.UPLOAD_TO, Capability.EDIT},
            "Only the folder owner/admin or a user with upload/edit access can create folders here",
        )
    await _validate_folder_position(
        db,
        user.entity_id,
        name=body.name,
        parent_id=body.parent_id,
    )
    folder = DocumentFolder(
        id=generate_ulid(),
        entity_id=user.entity_id,
        name=body.name.strip(),
        parent_id=body.parent_id,
    )
    # Inherit owner + perm fields from parent (RFC §13.3 — child
    # classification ≥ parent, child visibility ⊆ parent). New folder
    # gets the creating user as owner; visibility/classification default
    # to the parent's values when present, else leave as DB defaults.
    if parent is not None:
        if getattr(parent, "visibility", None):
            folder.visibility = parent.visibility
        if getattr(parent, "classification", None):
            folder.classification = parent.classification
        if getattr(parent, "client_visible", None) is not None:
            folder.client_visible = parent.client_visible
    folder.owner_id = user.id
    db.add(folder)
    await db.flush()
    return await _folder_resp_for_user(db, folder, user, document_count=0)


@router.put("/folders/{folder_id}", response_model=FolderResponse)
async def rename_folder(
    folder_id: str,
    body: RenameFolderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id.get(folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    await _require_folder_manager(db, user, folder)
    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id[folder_id]
    clean_name = body.name.strip()
    await _validate_folder_position(
        db,
        user.entity_id,
        name=clean_name,
        parent_id=folder.parent_id,
        folder_id=folder_id,
    )
    from packages.core.services.workspace_artifacts import (
        contains_workspace_artifact_root,
        resolve_workspace_folder_binding,
    )
    workspace_binding = await resolve_workspace_folder_binding(
        db,
        entity_id=user.entity_id,
        folder_id=folder.id,
    )
    is_workspace_root = (
        workspace_binding is not None
        and workspace_binding.artifact_folder_id == folder.id
        and not workspace_binding.relative_parts
    )
    if is_workspace_root:
        from packages.core.services.entity_service import update_workspace

        workspace = await update_workspace(
            db,
            workspace_binding.workspace_id,
            user.entity_id,
            name=clean_name,
        )
        if workspace is None:
            raise HTTPException(404, "Workspace not found")
        await db.refresh(folder)
    else:
        if await contains_workspace_artifact_root(
            db,
            entity_id=user.entity_id,
            folder_ids=_folder_subtree_ids(folders, folder_id),
        ):
            raise HTTPException(409, "Folders containing Workspaces cannot be renamed")
        folder.name = clean_name
    await db.flush()
    return await _folder_resp_for_user(db, folder, user, document_count=0)


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.models.document import Document, DocumentFolder, DocumentGroupMember
    from packages.core.models.workspace import Workspace

    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id.get(folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    await _require_folder_manager(db, user, folder)
    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id[folder_id]
    folder_ids = _folder_subtree_ids(folders, folder_id)
    await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.entity_id == user.entity_id,
            DocumentFolder.id.in_(folder_ids),
        ).order_by(DocumentFolder.id).with_for_update()
    )
    workspace_roots = list((await db.execute(
        select(Workspace).where(
            Workspace.entity_id == user.entity_id,
            Workspace.artifact_folder_id.in_(folder_ids),
        ).with_for_update()
    )).scalars().all())
    if workspace_roots:
        raise HTTPException(409, "Workspace folders cannot be deleted from Knowledge")
    folder_id_list = list(folder_ids)
    docs = list((await db.execute(
        select(Document).where(
            Document.entity_id == user.entity_id,
            Document.folder_id.in_(folder_id_list),
        ).order_by(Document.id).with_for_update()
    )).scalars().all())
    doc_ids = [doc.id for doc in docs]

    if doc_ids:
        from packages.core.services.comment_service import delete_resource_comments

        await delete_resource_comments(db, user.entity_id, "document", doc_ids)

    for doc in docs:
        if doc.fs_path and settings.MANOR_FS_ENABLED:
            full = _document_full_path(doc, user.entity_id)
            if full and os.path.isfile(full):
                os.remove(full)

    if doc_ids:
        await db.execute(
            DocumentGroupMember.__table__.delete()
            .where(DocumentGroupMember.document_id.in_(doc_ids))
        )
        await db.execute(
            Document.__table__.delete()
            .where(Document.entity_id == user.entity_id, Document.id.in_(doc_ids))
        )
    await db.execute(
        DocumentFolder.__table__.delete()
        .where(DocumentFolder.entity_id == user.entity_id, DocumentFolder.id.in_(folder_id_list))
    )
    await db.flush()
    from packages.core.services.tool_cache_version import bump_tool_cache_version
    await bump_tool_cache_version(user.entity_id, "documents")


@router.post("/folders/{folder_id}/move", response_model=FolderResponse)
async def move_folder(
    folder_id: str,
    body: MoveFolderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id.get(folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    parent = None
    mutation_folders = [folder]
    if body.parent_id:
        parent = folder_by_id.get(body.parent_id)
        if not parent:
            raise HTTPException(404, "Parent folder not found")
        mutation_folders.append(parent)
    await _require_folder_managers(db, user, mutation_folders)
    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id[folder_id]
    from packages.core.services.workspace_artifacts import contains_workspace_artifact_root
    if await contains_workspace_artifact_root(
        db,
        entity_id=user.entity_id,
        folder_ids=_folder_subtree_ids(folders, folder_id),
    ):
        raise HTTPException(409, "Workspace folders cannot be moved")
    await _validate_folder_position(
        db,
        user.entity_id,
        name=folder.name,
        parent_id=body.parent_id,
        folder_id=folder_id,
    )
    new_vis, new_cls, new_cv, _adjustments = await _enforce_folder_invariants(
        db,
        entity_id=user.entity_id,
        folder_id=body.parent_id,
        visibility=folder.visibility,
        classification=folder.classification,
        client_visible=folder.client_visible,
    )
    folder.parent_id = body.parent_id
    folder.visibility = new_vis
    folder.classification = new_cls
    folder.client_visible = new_cv
    await db.flush()

    # Reapply the moved root's now-effective floor/ceiling through the whole
    # subtree. A move is a policy change, not just a parent_id mutation.
    children_by_parent: dict[str, list[DocumentFolder]] = {}
    for candidate in folders:
        if candidate.id != folder.id and candidate.parent_id:
            children_by_parent.setdefault(candidate.parent_id, []).append(candidate)
    ordered_descendants: list[DocumentFolder] = []
    queue = list(children_by_parent.get(folder.id, []))
    while queue:
        descendant = queue.pop(0)
        ordered_descendants.append(descendant)
        queue.extend(children_by_parent.get(descendant.id, []))
    for descendant in ordered_descendants:
        sub_vis, sub_cls, sub_cv, _ = await _enforce_folder_invariants(
            db,
            entity_id=user.entity_id,
            folder_id=descendant.parent_id,
            visibility=descendant.visibility,
            classification=descendant.classification,
            client_visible=descendant.client_visible,
        )
        descendant.visibility = sub_vis
        descendant.classification = sub_cls
        descendant.client_visible = sub_cv
    subtree_ids = [folder.id, *[descendant.id for descendant in ordered_descendants]]
    subtree_docs = list((await db.execute(
        select(Document).where(
            Document.entity_id == user.entity_id,
            Document.folder_id.in_(subtree_ids),
            Document.is_trashed.is_(False),
        )
    )).scalars().all())
    for document in subtree_docs:
        doc_vis, doc_cls, doc_cv, _ = await _enforce_folder_invariants(
            db,
            entity_id=user.entity_id,
            folder_id=document.folder_id,
            visibility=document.visibility,
            classification=document.classification,
            client_visible=document.client_visible,
        )
        document.visibility = doc_vis
        document.classification = doc_cls
        document.client_visible = doc_cv
    await db.flush()
    return await _folder_resp_for_user(db, folder, user, document_count=0)


# ── Move document to folder ──

class MoveToFolderRequest(BaseModel):
    folder_id: str | None = None  # None = move to root


@router.post("/{doc_id}/move", response_model=DocumentResponse)
async def move_document_to_folder(
    doc_id: str,
    body: MoveToFolderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.MANAGE_METADATA},
        "Only the document owner/admin or a user with metadata access can move this document",
    )
    target_folder_path = None
    workspace_binding = None
    if body.folder_id:
        from sqlalchemy import select as sel
        from packages.core.models.document import DocumentFolder
        folder_by_id = (await _load_document_folders(db, user.entity_id))[1]
        folder = (await db.execute(
            sel(DocumentFolder).where(
                DocumentFolder.id == body.folder_id,
                DocumentFolder.entity_id == user.entity_id,
            )
        )).scalar_one_or_none()
        if not folder:
            raise HTTPException(404, "Folder not found")
        await _require_folder_capability(
            db,
            user,
            folder,
            {Capability.UPLOAD_TO, Capability.EDIT},
            "Only the target folder owner/admin or a user with upload/edit access can move documents here",
        )
        from packages.core.services.knowledge_visibility import is_user_visible_folder_path
        target_folder_path = _folder_rel_path(folder, folder_by_id)
        if not is_user_visible_folder_path(target_folder_path):
            raise HTTPException(404, "Folder not found")
        workspace_binding = await _workspace_storage_for_folder(
            db,
            entity_id=user.entity_id,
            folder_id=body.folder_id,
        )
        if workspace_binding:
            target_folder_path = workspace_binding.storage_dir
    # RFC §13.3: moving into a folder auto-applies the folder's classification
    # floor and visibility ceiling. We do this before setting folder_id so
    # the audit trail and response reflect the post-move state.
    new_vis, new_cls, new_cv, folder_adjustments = await _enforce_folder_invariants(
        db,
        entity_id=user.entity_id,
        folder_id=body.folder_id,
        visibility=getattr(doc, "visibility", None),
        classification=getattr(doc, "classification", None),
        client_visible=getattr(doc, "client_visible", None),
    )
    if folder_adjustments:
        if "visibility" in folder_adjustments:
            doc.visibility = new_vis
        if "classification" in folder_adjustments:
            doc.classification = new_cls
        if "client_visible" in folder_adjustments:
            doc.client_visible = new_cv
    from packages.core.services.document_file_move import move_document_file_to_folder
    from packages.core.services.document_file_state import mark_document_file_missing

    fs_move = move_document_file_to_folder(
        doc,
        entity_id=user.entity_id,
        target_folder_path=target_folder_path,
    )
    if fs_move.reason == "fs_unavailable":
        raise HTTPException(503, "Document storage is temporarily unavailable")
    if fs_move.reason == "missing_source":
        mark_document_file_missing(doc, source="document_move", trash=False)
    doc.folder_id = body.folder_id
    if workspace_binding:
        doc.metadata_ = merge_document_metadata(
            doc.metadata_,
            origin={"workspace_id": workspace_binding.workspace_id},
        )
    await db.flush()
    from packages.core.services.tool_cache_version import bump_tool_cache_version
    await bump_tool_cache_version(user.entity_id, "documents")
    return await _doc_resp_for_user(db, doc, user)


# ── Single document (path param — MUST be after fixed paths) ──

@router.put("/{doc_id}", response_model=DocumentResponse)
async def rename_one_document(
    doc_id: str,
    body: RenameDocumentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename a document."""
    current = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not current:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        current,
        {Capability.MANAGE_METADATA},
        "Only the document owner/admin or a user with metadata access can rename this document",
    )
    doc = await rename_document(db, doc_id, user.entity_id, body.name)
    if not doc:
        raise HTTPException(404, "Document not found")
    return await _doc_resp_for_user(db, doc, user)


@router.get("/{doc_id}", response_model=DocumentResponse)
async def get_one_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
        allow_redacted=True,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    return await _doc_resp_for_user(db, doc, user)


@router.delete("/{doc_id}", status_code=204)
async def delete_one_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entity_id = str(user.entity_id)
    doc = await get_visible_document(
        db,
        doc_id,
        entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        doc = (await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.entity_id == entity_id,
                Document.is_trashed.is_(True),
            )
        )).scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.DELETE},
        "Only the document owner/admin or a user with delete access can delete this document",
    )
    source_cleanup_bases = {doc.fs_path} if doc.fs_path else set()
    try:
        ok = await delete_document(
            db,
            doc_id,
            entity_id,
            include_trashed=bool(doc.is_trashed),
        )
    except DocumentMutationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Document not found")
    from packages.core.services.workspace_artifact_purge import (
        ARTIFACT_CLEANUP_KIND_FILE,
        document_derived_file_cleanup_bases,
        document_derived_tree_cleanup_bases,
        drain_workspace_artifact_purge_jobs,
        enqueue_artifact_cleanup_jobs,
    )

    cleanup_bases = document_derived_tree_cleanup_bases({doc_id})
    file_cleanup_bases = (
        source_cleanup_bases
        | document_derived_file_cleanup_bases({doc_id})
    )
    await enqueue_artifact_cleanup_jobs(db, entity_id, cleanup_bases)
    await enqueue_artifact_cleanup_jobs(
        db,
        entity_id,
        file_cleanup_bases,
        target_kind=ARTIFACT_CLEANUP_KIND_FILE,
    )
    await db.commit()
    cleanup_bases.update(file_cleanup_bases)
    await drain_workspace_artifact_purge_jobs(
        db,
        limit=len(cleanup_bases),
        entity_id=entity_id,
        storage_bases=cleanup_bases,
    )
    await _invalidate_committed_document_cache(entity_id)


@router.post("/{doc_id}/reindex", status_code=200)
async def reindex_one_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset a single document's vector_status to pending and trigger re-indexing."""
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.MANAGE_METADATA},
        "Only the document owner/admin or a user with metadata access can re-index this document",
    )
    doc.vector_status = VectorStatus.PENDING
    # Clear existing embedding
    try:
        await db.execute(text("UPDATE documents SET embedding = NULL WHERE id = :id"), {"id": doc_id})
    except Exception:
        pass
    await db.commit()
    # Trigger Celery task after commit so worker sees updated row
    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings
        process_document_embeddings.delay(doc_id)
    except Exception:
        pass
    return {"status": "pending"}


@router.post("/{doc_id}/cancel-index", status_code=200)
async def cancel_index_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel indexing for a document — clear embedding and set status to 'skipped'."""
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.MANAGE_METADATA},
        "Only the document owner/admin or a user with metadata access can cancel indexing for this document",
    )
    doc.vector_status = VectorStatus.SKIPPED
    try:
        await db.execute(text("UPDATE documents SET embedding = NULL WHERE id = :id"), {"id": doc_id})
    except Exception:
        pass
    await db.flush()
    return {"status": "skipped"}


@router.post("/reindex", status_code=200)
async def reindex_documents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset all entity documents to pending and trigger re-indexing."""
    if not await user_is_effective_entity_admin(db, user):
        raise HTTPException(403, "Only owner/admin can re-index all documents")
    count = await trigger_reindex(db, user.entity_id)
    return {"count": count}


@router.get("/{doc_id}/workspaces", response_model=list[str])
async def get_document_workspaces(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return workspace IDs this document belongs to (via groups)."""
    from sqlalchemy import select
    from packages.core.models.document import DocumentGroupMember, DocumentGroup

    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    result = await db.execute(
        select(DocumentGroup.workspace_id)
        .join(DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id)
        .where(
            DocumentGroupMember.document_id == doc_id,
            DocumentGroup.entity_id == user.entity_id,
            DocumentGroup.workspace_id.isnot(None),
        )
    )
    workspace_ids = {str(row[0]) for row in result.all() if row[0]}
    readable_workspace_ids = await readable_workspace_ids_for_user(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    from packages.core.models.workspace import Workspace

    active_workspace_ids = set((await db.execute(
        select(Workspace.id).where(
            Workspace.entity_id == user.entity_id,
            Workspace.id.in_(workspace_ids),
            Workspace.deleted_at.is_(None),
        )
    )).scalars()) if workspace_ids else set()
    if readable_workspace_ids is not None:
        active_workspace_ids.intersection_update(readable_workspace_ids)
    return sorted(active_workspace_ids)


@router.post("/{doc_id}/groups/{group_id}", status_code=200)
async def add_to_group(
    doc_id: str, group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group = await _require_document_group_manager(db, user, group_id)
    workspace_id = str(group.workspace_id or "").strip() or None
    doc = await get_visible_document(
        db,
        doc_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
        workspace_id=workspace_id,
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    await _require_document_capability(
        db,
        user,
        doc,
        {Capability.MANAGE_METADATA},
        "Only the document owner/admin or a user with metadata access can add this document",
    )
    added = await add_document_to_group(db, doc_id, group_id, entity_id=user.entity_id)
    if workspace_id and added:
        await db.commit()
        await mark_workspace_knowledge_changed(user.entity_id, workspace_id)
    return {"added": added}
