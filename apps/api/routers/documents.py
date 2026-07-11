"""Document endpoints — CRUD, groups, file upload/download."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.models.document import VectorStatus
from packages.core.config import get_settings
from packages.core.services.document_service import (
    create_document, delete_document,
    rename_document,
    list_groups, create_group, add_document_to_group,
    trigger_reindex,
    get_document_content, save_document_content, save_document_file,
    upsert_document_by_fs_path,
)
from packages.core.services.document_access import (
    effective_document_capabilities_for_user,
    folder_grant_capabilities_for_user,
    get_visible_document,
    list_visible_documents,
    user_has_document_capability,
    user_has_folder_capability,
    user_can_read_folder,
    visible_document_counts_by_folder,
    visible_storage_usage,
)
from packages.core.services.version_service import (
    create_version, list_versions,
    trash_document, restore_document, list_trash, empty_trash,
)
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.document_ai_draft import generate_document_ai_draft_content
from packages.core.ai.runtime import runtime_text_completion_platform_configured
from apps.api.deps import get_current_user, require_plan
from packages.core.models.permission import Capability
from packages.core.permissions import Permission, has_permission

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
settings = get_settings()
logger = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STALE_FILE_INTEGRITY_STATUSES = {"missing", "invalid_path", "unavailable", "error"}
USER_ROOT_DOCUMENT_DEFAULT_VISIBILITY = "private"


class DocumentResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    fs_path: str | None = None
    file_size: int | None = None
    file_type: str | None = None
    mime_type: str | None = None
    source: str = "upload"
    vector_status: str = VectorStatus.PENDING
    indexing_progress: dict | None = None
    created_by: str | None = None
    folder_id: str | None = None
    created_at: str | None = None
    # ── Permission-v1 fields (see docs/PERMISSIONS_DESIGN_ZH.md §13) ─────
    visibility: str | None = None
    classification: str | None = None
    owner_id: str | None = None
    client_visible: bool | None = None
    legal_hold: bool | None = None
    legal_hold_reason: str | None = None
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
    Capability.LEGAL_HOLD,
]
_DOCUMENT_OWNER_CAPABILITIES = set(_DOCUMENT_CAPABILITY_ORDER) - {Capability.UPLOAD_TO}
_FOLDER_OWNER_CAPABILITIES = set(_DOCUMENT_CAPABILITY_ORDER)


def _ordered_capabilities(capabilities: set[str]) -> list[str]:
    ordered = [capability for capability in _DOCUMENT_CAPABILITY_ORDER if capability in capabilities]
    ordered.extend(sorted(capability for capability in capabilities if capability not in set(_DOCUMENT_CAPABILITY_ORDER)))
    return ordered


def _doc_resp(d, *, current_user_capabilities: set[str] | None = None) -> DocumentResponse:
    meta = d.metadata_ if hasattr(d, "metadata_") else None
    indexing = meta.get("indexing") if isinstance(meta, dict) else None
    artifact_meta = meta.get("artifact") if isinstance(meta, dict) else None
    generation_meta = meta.get("generation") if isinstance(meta, dict) else None
    artifact_meta = artifact_meta if isinstance(artifact_meta, dict) else {}
    generation_meta = generation_meta if isinstance(generation_meta, dict) else {}
    return DocumentResponse(
        id=d.id, entity_id=d.entity_id, name=d.name,
        fs_path=d.fs_path, file_size=d.file_size,
        file_type=d.file_type, mime_type=d.mime_type,
        source=d.source, vector_status=d.vector_status,
        indexing_progress=indexing,
        created_by=d.created_by, folder_id=d.folder_id,
        created_at=d.created_at.isoformat() if d.created_at else None,
        # ── Permission-v1 fields ─────────────────────────────────────────
        visibility=getattr(d, "visibility", None),
        classification=getattr(d, "classification", None),
        owner_id=getattr(d, "owner_id", None),
        client_visible=getattr(d, "client_visible", None),
        legal_hold=getattr(d, "legal_hold", None),
        legal_hold_reason=getattr(d, "legal_hold_reason", None),
        pii_detected=getattr(d, "pii_detected", None),
        quarantine_status=getattr(d, "quarantine_status", None),
        editor_recipe_document_id=artifact_meta.get("editor_recipe_document_id") or generation_meta.get("editor_recipe_document_id"),
        editor_recipe_path=artifact_meta.get("editor_recipe_path") or generation_meta.get("editor_recipe_path"),
        editor_recipe_name=artifact_meta.get("editor_recipe_name") or generation_meta.get("editor_recipe_name"),
        current_user_capabilities=_ordered_capabilities(current_user_capabilities or set()),
    )


async def _doc_resp_for_user(db: AsyncSession, d, user: User) -> DocumentResponse:
    capabilities = await effective_document_capabilities_for_user(
        db,
        document=d,
        user_id=user.id,
        role=user.role,
    )
    if _can_manage_document(user, d):
        capabilities.update(_DOCUMENT_OWNER_CAPABILITIES)
    return _doc_resp(d, current_user_capabilities=capabilities)


def _can_manage_document(user: User, doc) -> bool:
    if user.role in ("owner", "admin"):
        return True
    if getattr(doc, "owner_id", None) == user.id:
        return True
    created_by = getattr(doc, "created_by", None)
    return bool(created_by and created_by in {user.id, user.email, user.display_name})


def _require_document_manager(user: User, doc) -> None:
    if not _can_manage_document(user, doc):
        raise HTTPException(403, "Only an owner/admin or the document owner can modify this document")


async def _can_use_document_capability(
    db: AsyncSession,
    user: User,
    doc,
    capabilities: set[str],
) -> bool:
    if _can_manage_document(user, doc):
        return True
    return await user_has_document_capability(
        db,
        document=doc,
        user_id=user.id,
        capabilities=capabilities,
    )


async def _require_document_capability(
    db: AsyncSession,
    user: User,
    doc,
    capabilities: set[str],
    message: str,
) -> None:
    if not await _can_use_document_capability(db, user, doc, capabilities):
        raise HTTPException(403, message)


def _require_document_upload(user: User) -> None:
    if not has_permission(user.role, Permission.DOCS_UPLOAD):
        raise HTTPException(403, "This role cannot create or upload documents")


def _can_manage_folder(user: User, folder) -> bool:
    if user.role in ("owner", "admin"):
        return True
    return bool(getattr(folder, "owner_id", None) == user.id)


def _require_folder_manager(user: User, folder) -> None:
    if not _can_manage_folder(user, folder):
        raise HTTPException(403, "Only an owner/admin or the folder owner can modify this folder")


async def _can_use_folder_capability(
    db: AsyncSession,
    user: User,
    folder,
    capabilities: set[str],
) -> bool:
    if _can_manage_folder(user, folder):
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
    if not await _can_use_folder_capability(db, user, folder, capabilities):
        raise HTTPException(403, message)


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


def _is_video_document(doc) -> bool:
    ext = (getattr(doc, "file_type", None) or os.path.splitext(getattr(doc, "name", "") or "")[1].lstrip(".")).lower()
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return ext in _VIDEO_THUMB_EXTENSIONS or any(mime.startswith(prefix) for prefix in _VIDEO_THUMB_MIME_PREFIXES)


def _document_ext(doc) -> str:
    name_ext = os.path.splitext(getattr(doc, "name", "") or "")[1].lstrip(".").lower()
    file_type = (getattr(doc, "file_type", None) or "").lstrip(".").lower()
    return name_ext or file_type


def _is_pptx_document(doc) -> bool:
    ext = _document_ext(doc)
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return ext in {"pptx", "ppt", "dps"} or mime == PPTX_MIME


def _is_docx_document(doc) -> bool:
    ext = _document_ext(doc)
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return ext in {"docx", "doc"} or mime == DOCX_MIME


async def _repair_pptx_file_if_needed(doc, full_path: str, db: AsyncSession) -> None:
    """Convert legacy empty/text .pptx placeholders into real PPTX files."""
    if not _is_pptx_document(doc) or not full_path:
        return

    should_repair = False
    raw_text = ""

    def _inspect_existing() -> tuple[bool, str]:
        if not os.path.isfile(full_path) or os.path.getsize(full_path) == 0:
            return True, ""
        try:
            with open(full_path, "rb") as f:
                head = f.read(4096)
            if not head.startswith(b"PK"):
                return True, head.decode("utf-8", errors="ignore")
            with zipfile.ZipFile(full_path) as zf:
                if "ppt/presentation.xml" not in set(zf.namelist()):
                    return True, ""
        except Exception:
            return True, ""
        return False, ""

    should_repair, raw_text = await asyncio.to_thread(_inspect_existing)
    if not should_repair:
        return

    title = os.path.splitext(getattr(doc, "name", None) or "Presentation")[0] or "Presentation"
    file_bytes = await _generate_pptx_bytes(title, raw_text)
    await _write_document_bytes_atomic(
        doc.entity_id,
        doc.fs_path,
        file_bytes,
        allow_empty=False,
    )
    doc.file_size = len(file_bytes)
    doc.file_type = "pptx"
    doc.mime_type = PPTX_MIME
    await db.flush()
    await db.commit()


def _entity_root(entity_id: str) -> str:
    return os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))


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


def _unique_document_rel_path(entity_id: str, filename: str) -> str:
    entity_root = _entity_root(entity_id)
    os.makedirs(entity_root, exist_ok=True)
    root_norm = os.path.normpath(entity_root)
    candidate = _safe_visible_filename(filename, "document")
    target = os.path.normpath(os.path.join(entity_root, candidate))
    if os.path.commonpath([root_norm, target]) != root_norm:
        raise HTTPException(400, "Invalid filename")
    if os.path.exists(target):
        if candidate.lower().endswith(".diagram.json"):
            base, ext = candidate[:-len(".diagram.json")], ".diagram.json"
        else:
            base, ext = os.path.splitext(candidate)
        stamp = int(time.time())
        candidate = f"{base}_{stamp}{ext}"
        target = os.path.normpath(os.path.join(entity_root, candidate))
        suffix = 1
        while os.path.exists(target):
            candidate = f"{base}_{stamp}_{suffix}{ext}"
            target = os.path.normpath(os.path.join(entity_root, candidate))
            suffix += 1
    return os.path.relpath(target, entity_root)


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
    temp_path = f"{target_path}.tmp"
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


async def _download_remote_video_thumbnail_source(file_url: str, target_path: str) -> None:
    import aiofiles
    import httpx

    if not file_url.startswith(("http://", "https://")):
        raise HTTPException(404, "Remote video URL is not fetchable for thumbnail generation")

    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
    total = 0
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", file_url) as response:
                if response.status_code >= 400:
                    raise HTTPException(response.status_code, "Remote video is not fetchable for thumbnail generation")
                async with aiofiles.open(target_path, "wb") as fh:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise HTTPException(413, f"Video file too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB")
                        await fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Remote video is not fetchable for thumbnail generation") from exc

    if total <= 0 or not os.path.exists(target_path) or os.path.getsize(target_path) == 0:
        raise HTTPException(404, "Remote video is empty")


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

    items = [await _doc_resp_for_user(db, d, user) for d in docs]
    return DocumentListResponse(
        items=items, total=total, total_files=total_files, total_size=total_size,
        storage_used_mb=gate.current, storage_limit_mb=gate.limit,
    )


# ── Upload (fixed path — before /{doc_id}) ──

@router.post("/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    folder_id: str | None = Query(None, description="Folder to upload into"),
    visibility: str | None = Query(None, description="private | workspace | entity | public"),
    classification: str | None = Query(None, description="public | internal | confidential | restricted"),
    client_visible: bool | None = Query(None, description="Show in client portal"),
    _gate=Depends(require_plan("storage_mb")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import aiofiles
    from packages.core.services.knowledge_sync import sync_file_to_knowledge
    from packages.core.services.document_service import get_document

    _require_document_upload(user)

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
    visibility, classification, client_visible, folder_adjustments = (
        await _enforce_folder_invariants(
            db,
            entity_id=user.entity_id,
            folder_id=folder_id,
            visibility=visibility,
            classification=classification,
            client_visible=client_visible,
        )
    )

    # Browser/drop uploads can include client-side directory prefixes such as
    # "upload/photo.png". Knowledge folders should only be created explicitly
    # by the user or by an AI file-writing action, so plain uploads use basename.
    filename = _safe_visible_filename(file.filename, "upload")
    mime_type = file.content_type
    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
    fs_path = None
    file_size = 0
    resolved_folder_id = folder_id

    if settings.MANOR_FS_ENABLED:
        _require_document_filesystem_ready()
        rel_target = _unique_document_rel_path(user.entity_id, filename)
        fd, tmp_path = tempfile.mkstemp(prefix="manor-doc-upload-", suffix=".tmp")
        os.close(fd)
        # Stream to disk in chunks — avoids loading entire file into memory
        try:
            async with aiofiles.open(tmp_path, "wb") as f:
                while chunk := await file.read(1024 * 256):  # 256KB chunks
                    file_size += len(chunk)
                    if file_size > max_bytes:
                        raise HTTPException(413, f"File too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB")
                    await f.write(chunk)
            fs_path = await _copy_document_file_atomic(
                user.entity_id,
                rel_target,
                tmp_path,
                expected_size=file_size,
                allow_empty=True,
            )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    else:
        # No filesystem — just read to get size for DB record
        content = await file.read()
        file_size = len(content)
        if file_size > max_bytes:
            raise HTTPException(413, f"File too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB")

    ext = os.path.splitext(filename)[1].lstrip(".") if "." in filename else None
    if settings.MANOR_FS_ENABLED and fs_path:
        sync = await sync_file_to_knowledge(
            entity_id=user.entity_id,
            abs_path=os.path.join(settings.MANOR_FS_ROOT, user.entity_id, fs_path),
            entity_root=os.path.join(settings.MANOR_FS_ROOT, user.entity_id),
            source="upload",
            created_by=(user.display_name or user.email),
            force=True,
            folder_id=resolved_folder_id,
        )
        doc = await get_document(db, sync.document_id, user.entity_id) if sync.document_id else None
        if not doc:
            raise HTTPException(500, "Upload saved but document sync failed")
        # Knowledge sync may infer a normalized display name from content.
        # Upload/download UX should preserve the user's original filename.
        doc.name = filename
        doc.file_type = ext
        doc.mime_type = mime_type
        # FS sync created the doc with defaults; apply permission-v1 overrides
        # post-hoc so both upload paths honor the user's choices.
        _apply_permission_overrides(doc, user.id, visibility, classification, client_visible)
        await db.flush()
    else:
        doc = await create_document(
            db, user.entity_id,
            name=filename, fs_path=fs_path, file_size=file_size,
            file_type=ext, mime_type=mime_type, source="upload",
            created_by=(user.display_name or user.email),
            folder_id=resolved_folder_id,
            visibility=visibility,
            classification=classification,
            client_visible=client_visible,
            owner_id=user.id,
        )

    await db.commit()

    # Trigger async embedding generation
    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings
        process_document_embeddings.delay(doc.id)
    except Exception:
        pass  # Celery not available in dev mode

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
    _require_document_upload(user)
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
            filename[:-len(".diagram.json")]
            if filename.lower().endswith(".diagram.json")
            else os.path.splitext(filename)[0]
        )
        content = _blank_diagram_bytes(diagram_title)
        mime_type = "application/json"
    else:
        content = BLANK_CONTENT.get(ext, b"")
        mime_type = f"text/{ext}" if ext in ("md", "txt", "csv", "html") else "application/json"

    fs_path = None
    if settings.MANOR_FS_ENABLED:
        _require_document_filesystem_ready()
        fs_path = await _write_document_bytes_atomic(
            user.entity_id,
            _unique_document_rel_path(user.entity_id, filename),
            content,
            allow_empty=True,
        )

    file_type = "docx" if ext == "doc" else "pptx" if ext in ("ppt", "dps") else ext
    created_by = user.display_name or user.email
    if fs_path:
        doc = await upsert_document_by_fs_path(
            db, user.entity_id,
            name=filename, fs_path=fs_path, file_size=len(content),
            file_type=file_type,
            mime_type=mime_type,
            source="manual", created_by=created_by,
        )
        doc.source = "manual"
        doc.created_by = created_by
        doc.owner_id = user.id
        if not getattr(doc, "folder_id", None):
            doc.visibility = USER_ROOT_DOCUMENT_DEFAULT_VISIBILITY
        await db.flush()
    else:
        doc = await create_document(
            db, user.entity_id,
            name=filename, fs_path=fs_path, file_size=len(content),
            file_type=file_type,
            mime_type=mime_type,
            source="manual", created_by=created_by,
            owner_id=user.id,
        )
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
    _require_document_upload(user)
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
    mime_type = _mime_map.get(ext, f"text/{ext}" if ext in ("md", "txt", "csv", "html", "json") else "application/octet-stream")

    # Create placeholder document immediately
    doc = await create_document(
        db, user.entity_id,
        name=filename, fs_path=None, file_size=0,
        file_type=ext, mime_type=mime_type,
        source="ai-draft", created_by=(user.display_name or user.email),
        owner_id=user.id,
    )
    # Mark as generating
    doc.vector_status = VectorStatus.GENERATING
    await db.flush()

    resp = await _doc_resp_for_user(db, doc, user)

    # Launch background generation
    asyncio.create_task(_generate_ai_draft_content(
        doc_id=doc.id,
        entity_id=user.entity_id,
        user_id=user.id,
        prompt=body.prompt,
        ext=ext,
        original_name=filename if body.name else None,
        display_name=user.display_name or user.email,
    ))

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

    try:
        content_text = await generate_document_ai_draft_content(
            entity_id=entity_id,
            user_id=user_id,
            prompt=prompt,
            file_type=ext,
            document_id=doc_id,
        )
    except Exception as exc:
        # Mark document as failed
        async with async_session_factory() as db:
            from sqlalchemy import update
            from packages.core.models.document import Document
            await db.execute(
                update(Document).where(Document.id == doc_id).values(vector_status=VectorStatus.FAILED)
            )
            await db.commit()
        import logging
        logging.getLogger(__name__).error("AI draft LLM call failed for doc %s: %s", doc_id, exc)
        return

    # Build the actual file bytes
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

    # Derive final filename from content if no name was given
    if not original_name:
        first_line = content_text.split("\n", 1)[0].strip().lstrip("# ").strip()
        short_name = (first_line[:60] or "AI Draft").rstrip(".")
        filename = f"{short_name}.{ext}"
    else:
        filename = original_name if "." in original_name else f"{original_name}.{ext}"
    filename = _safe_visible_filename(filename, f"AI Draft.{ext}")

    try:
        fs_path = None
        if settings.MANOR_FS_ENABLED:
            _require_document_filesystem_ready()
            fs_path = await _write_document_bytes_atomic(
                entity_id,
                _unique_document_rel_path(entity_id, filename),
                content,
                allow_empty=False,
            )
    except Exception as exc:  # noqa: BLE001
        async with async_session_factory() as db:
            from sqlalchemy import update
            from packages.core.models.document import Document
            await db.execute(
                update(Document).where(Document.id == doc_id).values(vector_status=VectorStatus.FAILED)
            )
            await db.commit()
        import logging
        logging.getLogger(__name__).error("AI draft file write failed for doc %s: %s", doc_id, exc)
        return

    # Update the document record with actual content info
    async with async_session_factory() as db:
        from sqlalchemy import update
        from packages.core.models.document import Document
        await db.execute(
            update(Document).where(Document.id == doc_id).values(
                name=filename,
                fs_path=fs_path,
                file_size=len(content),
                mime_type=mime_type or "application/octet-stream",
                vector_status=VectorStatus.PENDING,
            )
        )
        await db.commit()

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

    _require_document_upload(user)
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
    if settings.MANOR_FS_ENABLED:
        _require_document_filesystem_ready()
        fs_path = await _write_document_bytes_atomic(
            user.entity_id,
            _unique_document_rel_path(user.entity_id, filename),
            content,
            allow_empty=False,
        )

    # Store external sync metadata for future refreshes.
    metadata = merge_document_metadata(
        external={
            "google_drive": {
                "file_id": body.file_id,
                "modified_time": body.modified_time,
            }
        }
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
    if fs_path:
        doc = await upsert_document_by_fs_path(
            db, user.entity_id,
            name=filename, fs_path=fs_path, file_size=len(content),
            file_type=ext, mime_type=actual_mime,
            source="google_drive", created_by=created_by,
            folder_id=body.folder_id,
        )
        doc.source = "google_drive"
        doc.created_by = created_by
        doc.metadata_ = metadata
        doc.visibility = visibility
        doc.classification = classification
        doc.client_visible = client_visible
        doc.owner_id = user.id
        await db.flush()
    else:
        doc = await create_document(
            db, user.entity_id,
            name=filename, fs_path=fs_path, file_size=len(content),
            file_type=ext, mime_type=actual_mime,
            source="google_drive", created_by=created_by,
            folder_id=body.folder_id,
            metadata=metadata,
            visibility=visibility,
            classification=classification,
            client_visible=client_visible,
            owner_id=user.id,
        )

    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings
        process_document_embeddings.delay(doc.id)
    except Exception:
        pass

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
    doc_meta = doc.metadata_ or {}
    gd_meta = (doc_meta.get("external") or {}).get("google_drive") or doc_meta.get("google_drive", {})
    if gd_meta.get("modified_time") == body.modified_time:
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

    # Overwrite file on filesystem
    if settings.MANOR_FS_ENABLED and doc.fs_path:
        _require_document_filesystem_ready()
        await _write_document_bytes_atomic(
            user.entity_id,
            doc.fs_path,
            content,
            allow_empty=False,
        )

    # Update metadata
    doc.metadata_ = merge_document_metadata(
        doc.metadata_,
        external={
            "google_drive": {
                "file_id": body.file_id,
                "modified_time": body.modified_time,
            }
        },
    )
    doc.file_size = len(content)
    doc.vector_status = VectorStatus.PENDING
    await db.commit()

    # Re-index
    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings
        process_document_embeddings.delay(doc_id)
    except Exception:
        pass

    return {"status": "synced"}


# ── Create from URL ──

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
    _require_document_upload(user)
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
        db, user.entity_id,
        name=filename, file_type=ext,
        source="url", created_by=(user.display_name or user.email),
        metadata=merge_document_metadata(external={"source_url": url}),
        owner_id=user.id,
    )
    await db.commit()

    # Dispatch background fetch + index
    try:
        from packages.core.tasks.ai_tasks import fetch_and_index_url_document
        fetch_and_index_url_document.delay(doc.id, url)
    except Exception:
        logger.warning("Failed to dispatch URL fetch task for %s", doc.id, exc_info=True)
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
        await db.flush()

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
    added = 0
    for doc_id in body.document_ids:
        result = await add_document_to_group(db, doc_id, body.group_id, entity_id=user.entity_id)
        if result:
            added += 1
    return {"added": added, "total": len(body.document_ids)}


@router.get("/groups", response_model=list[DocumentGroupResponse])
async def list_my_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    groups = await list_groups(db, user.entity_id)
    return [DocumentGroupResponse(id=g.id, entity_id=g.entity_id, name=g.name, workspace_id=g.workspace_id) for g in groups]


@router.post("/groups", response_model=DocumentGroupResponse, status_code=201)
async def create_new_group(
    req: CreateGroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group = await create_group(db, user.entity_id, name=req.name, workspace_id=req.workspace_id)
    return DocumentGroupResponse(id=group.id, entity_id=group.entity_id, name=group.name, workspace_id=group.workspace_id)


# ── Trash (fixed paths — before /{doc_id}) ──

@router.get("/trash", response_model=list[DocumentResponse])
async def list_trashed_documents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    docs = await list_trash(db, user.entity_id)
    return [await _doc_resp_for_user(db, d, user) for d in docs]


@router.post("/trash/empty", status_code=204)
async def empty_trash_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await empty_trash(db, user.entity_id)


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

    pptx_path = _document_full_path(doc, user.entity_id)
    if not pptx_path or not os.path.isfile(pptx_path):
        raise HTTPException(404, "File not found on disk")

    cache_dir = os.path.join(settings.MANOR_FS_ROOT, user.entity_id, ".slide-cache", doc_id)

    try:
        from packages.core.services.slide_renderer import render_slides
        paths = await render_slides(pptx_path, cache_dir)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Slide rendering failed for %s: %s", doc_id, exc)
        raise HTTPException(502, f"Slide rendering failed: {exc}")

    return {
        "slides": [
            {"index": i, "url": f"/documents/{doc_id}/slides/{i}"}
            for i in range(len(paths))
        ],
        "total": len(paths),
    }


@router.get("/{doc_id}/slides/{slide_index}")
async def get_slide_image(
    doc_id: str,
    slide_index: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a single rendered slide image as JPEG."""
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

    cache_dir = os.path.join(settings.MANOR_FS_ROOT, user.entity_id, ".slide-cache", doc_id)

    try:
        from packages.core.services.slide_renderer import render_slides
        pptx_path = _document_full_path(doc, user.entity_id)
        if not pptx_path or not os.path.isfile(pptx_path):
            raise HTTPException(404, "File not found on disk")
        paths = await render_slides(pptx_path, cache_dir)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Slide rendering failed")

    if slide_index < 0 or slide_index >= len(paths):
        raise HTTPException(404, "Slide index out of range")

    return FileResponse(
        path=paths[slide_index],
        media_type="image/jpeg",
        filename=f"slide-{slide_index + 1}.jpg",
    )


# ── Content read/write (for in-browser editor) ──

@router.put("/{doc_id}/file", response_model=DocumentResponse)
async def replace_document_file_endpoint(
    doc_id: str,
    file: UploadFile = File(...),
    _gate=Depends(require_plan("storage_mb")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the binary file for an existing document."""
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

    try:
        doc = await save_document_file(
            db,
            doc_id,
            user.entity_id,
            b"".join(chunks),
            filename=file.filename,
            mime_type=file.content_type,
            created_by=(user.display_name or user.email),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if not doc:
        raise HTTPException(404, "Document not found")

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
    content = await get_document_content(db, doc_id, user.entity_id)
    if content is None:
        raise HTTPException(404, "Document not found or no content")
    return {"content": content}


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
    ok = await save_document_content(
        db, doc_id, user.entity_id, body.content, created_by=(user.display_name or user.email),
    )
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


async def _document_first_page_thumbnail(doc, entity_id: str) -> FileResponse:
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
    try:
        from packages.core.services.slide_renderer import render_first_page
        image_path = await render_first_page(source_path, cache_dir, source_ext=source_ext)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Document thumbnail render failed for %s: %s", doc.id, exc,
        )
        raise HTTPException(502, "Thumbnail rendering failed")

    return FileResponse(
        path=image_path,
        media_type="image/jpeg",
        filename=f"{os.path.splitext(doc.name)[0] or doc.id}-thumbnail.jpg",
        headers={"Cache-Control": "private, max-age=300"},
    )


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
    if not _is_video_document(doc):
        # Non-video: render a first-page thumbnail for PDFs and office files.
        return await _document_first_page_thumbnail(doc, user.entity_id)

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
                await _download_remote_video_thumbnail_source(doc.file_url, remote_source_path)
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

    return FileResponse(
        path=thumb_path,
        media_type="image/jpeg",
        filename=f"{os.path.splitext(doc.name)[0] or doc.id}-thumbnail.jpg",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Thumbnail-Cache": "hit" if cache_hit else "miss",
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

    # Try filesystem first
    if doc.fs_path:
        full_path = _document_full_path(doc, user.entity_id)
        if full_path and _is_pptx_document(doc):
            await _repair_pptx_file_if_needed(doc, full_path, db)
        if full_path and os.path.isfile(full_path):
            if _mark_document_file_available(doc, source="filesystem"):
                await db.commit()
            return FileResponse(
                path=full_path,
                media_type=doc.mime_type or "application/octet-stream",
                filename=doc.name,
            )

    # Fallback to file_url (e.g. S3 / external storage)
    if doc.file_url:
        return await _remote_document_stream_response(
            doc.file_url,
            filename=doc.name,
            media_type=doc.mime_type,
        )

    # Fallback: serve editor content from metadata/DB. Binary office editors
    # can exist as metadata-only rows when local FS is disabled; synthesize a
    # real Office file so the import pipeline can still open and recover.
    content = await get_document_content(db, doc.id, user.entity_id)

    if _is_docx_document(doc):
        from packages.core.services.docgen_service import generate_docx
        from packages.core.services.document_service import _editor_html_to_docgen_text

        title = os.path.splitext(doc.name or "Document")[0] or "Document"
        file_bytes = await generate_docx(title, _editor_html_to_docgen_text(content or ""))
        doc.file_size = len(file_bytes)
        doc.file_type = "docx"
        doc.mime_type = DOCX_MIME
        await db.flush()
        await db.commit()
        return Response(
            content=file_bytes,
            media_type=DOCX_MIME,
            headers={"Content-Disposition": f'attachment; filename="{doc.name}"'},
        )

    if _is_pptx_document(doc):
        title = os.path.splitext(doc.name or "Presentation")[0] or "Presentation"
        file_bytes = await _generate_pptx_bytes(title, content or "")
        doc.file_size = len(file_bytes)
        doc.file_type = "pptx"
        doc.mime_type = PPTX_MIME
        await db.flush()
        await db.commit()
        return Response(
            content=file_bytes,
            media_type=PPTX_MIME,
            headers={"Content-Disposition": f'attachment; filename="{doc.name}"'},
        )

    # Fallback: serve content from DB (text-based documents created via API)
    if content:
        return Response(
            content=content.encode("utf-8"),
            media_type=doc.mime_type or "text/plain",
            headers={"Content-Disposition": f'attachment; filename="{doc.name}"'},
        )

    raise HTTPException(404, "No file available for this document")


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
        {Capability.DELETE},
        "Only the document owner/admin or a user with delete access can trash this document",
    )
    ok = await trash_document(db, doc_id, user.entity_id, trashed_by=(user.display_name or user.email))
    if not ok:
        raise HTTPException(404, "Document not found")
    # Clear embedding to free vector index space
    from sqlalchemy import update as sa_update
    from packages.core.models.document import Document as DocModel
    await db.execute(
        sa_update(DocModel).where(DocModel.id == doc_id).values(vector_status=VectorStatus.PENDING)
    )
    try:
        await db.execute(text("UPDATE documents SET embedding = NULL WHERE id = :id"), {"id": doc_id})
    except Exception:
        pass  # pgvector column may not exist
    return {"trashed": True}


@router.post("/{doc_id}/restore", status_code=200)
async def restore_one_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await restore_document(db, doc_id, user.entity_id)
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
) -> FolderResponse:
    capabilities: set[str]
    if _can_manage_folder(user, f):
        capabilities = set(_FOLDER_OWNER_CAPABILITIES)
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
        select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
    )
    folders = list(result.scalars().all())
    return folders, {f.id: f for f in folders}


async def _visible_folder_counts(
    db: AsyncSession,
    entity_id: str,
    folders: list,
    *,
    user_id: str | None = None,
    role: str | None = None,
) -> dict[str, int]:
    visible_folder_ids = {f.id for f in folders}
    if visible_folder_ids:
        direct_counts = await visible_document_counts_by_folder(
            db,
            entity_id,
            folder_ids=visible_folder_ids,
            user_id=user_id,
            role=role,
        )
    else:
        direct_counts = {}
    child_ids_by_parent: dict[str | None, list[str]] = {}
    for folder in folders:
        child_ids_by_parent.setdefault(folder.parent_id, []).append(folder.id)
    count_cache: dict[str, int] = {}

    def recursive_document_count(folder_id: str, seen: set[str] | None = None) -> int:
        if folder_id in count_cache:
            return count_cache[folder_id]
        if seen is None:
            seen = set()
        if folder_id in seen:
            return direct_counts.get(folder_id, 0)
        seen.add(folder_id)
        total = direct_counts.get(folder_id, 0)
        for child_id in child_ids_by_parent.get(folder_id, []):
            total += recursive_document_count(child_id, seen.copy())
        count_cache[folder_id] = total
        return total

    return {folder.id: recursive_document_count(folder.id) for folder in folders}


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
) -> tuple[list, dict[str, object]]:
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    visible_folders = []
    for f in folders:
        if not is_user_visible_folder_path(_folder_rel_path(f, folder_by_id)):
            continue
        if not await _user_can_read_folder_path(db, f, folder_by_id, user):
            continue
        visible_folders.append(f)
    return visible_folders, {f.id: f for f in visible_folders}


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

    search_text = (search or "").strip()
    search_query = search_text or None
    global_document_scope = scope == "all" or bool(workspace_id)
    browse_folder_id = None if folder_id in (None, "", "root") else folder_id

    visible_folders, visible_folder_by_id = await _visible_document_folders_for_user(db, user)
    if browse_folder_id and browse_folder_id not in visible_folder_by_id:
        raise HTTPException(404, "Folder not found")

    folder_counts = await _visible_folder_counts(
        db,
        user.entity_id,
        visible_folders,
        user_id=user.id,
        role=user.role,
    )
    if global_document_scope and not search_query:
        direct_folders = []
        document_folder_id = None
        storage_folder_ids = None
    elif search_query:
        q = search_query.lower()
        direct_folders = [
            f for f in visible_folders
            if q in (f.name or "").lower()
        ]
        document_folder_id = None
        storage_folder_ids = None
    else:
        direct_folders = [
            f for f in visible_folders
            if (f.parent_id or None) == browse_folder_id
        ]
        document_folder_id = browse_folder_id or "root"
        storage_folder_ids = (
            _folder_subtree_ids(visible_folders, browse_folder_id)
            if browse_folder_id
            else None
        )

    direct_folders = sorted(direct_folders, key=lambda f: (f.name or "").lower())
    folder_responses = [
        await _folder_resp_for_user(db, f, user, document_count=folder_counts.get(f.id, 0))
        for f in direct_folders
    ]

    docs, total = await list_visible_documents(
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
    total_size, total_files = await visible_storage_usage(
        db,
        user.entity_id,
        user_id=user.id,
        role=user.role,
        name_search=search_query,
        folder_ids=storage_folder_ids,
        workspace_id=workspace_id,
        include_generated_assets=include_generated_assets,
    )
    gate = await _plan_check(db, user.entity_id, "storage_mb")
    items = [await _doc_resp_for_user(db, d, user) for d in docs]

    return DocumentBrowseResponse(
        items=items,
        documents=items,
        folders=folder_responses,
        total=total,
        total_documents=total,
        total_folders=len(folder_responses),
        total_files=total_files,
        total_size=total_size,
        storage_used_mb=gate.current,
        storage_limit_mb=gate.limit,
    )


@router.get("/folder-tree", response_model=list[FolderResponse])
async def document_folder_tree(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    visible_folders, _ = await _visible_document_folders_for_user(db, user)
    folder_counts = await _visible_folder_counts(
        db,
        user.entity_id,
        visible_folders,
        user_id=user.id,
        role=user.role,
    )
    return [
        await _folder_resp_for_user(db, f, user, document_count=folder_counts.get(f.id, 0))
        for f in sorted(visible_folders, key=lambda f: ((f.parent_id or ""), (f.name or "").lower()))
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
    visible_folders, _ = await _visible_document_folders_for_user(db, user)
    visible_folders = sorted(visible_folders, key=lambda f: f.created_at, reverse=True)
    folder_counts = await _visible_folder_counts(
        db,
        user.entity_id,
        visible_folders,
        user_id=user.id,
        role=user.role,
    )

    return [
        await _folder_resp_for_user(db, f, user, document_count=folder_counts.get(f.id, 0))
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
    _require_document_upload(user)
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
    if body.parent_id:
        _, parent_by_id = await _load_document_folders(db, user.entity_id)
        parent = parent_by_id.get(body.parent_id)
        if parent is not None:
            await _require_folder_capability(
                db,
                user,
                parent,
                {Capability.UPLOAD_TO, Capability.EDIT},
                "Only the folder owner/admin or a user with upload/edit access can create folders here",
            )
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
    _, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id.get(folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    _require_folder_manager(user, folder)
    await _validate_folder_position(
        db,
        user.entity_id,
        name=body.name,
        parent_id=folder.parent_id,
        folder_id=folder_id,
    )
    folder.name = body.name.strip()
    await db.flush()
    return await _folder_resp_for_user(db, folder, user, document_count=0)


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.models.document import Document, DocumentFolder, DocumentGroupMember

    folders, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id.get(folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    _require_folder_manager(user, folder)
    folder_ids = _folder_subtree_ids(folders, folder_id)
    folder_id_list = list(folder_ids)
    docs = list((await db.execute(
        select(Document).where(
            Document.entity_id == user.entity_id,
            Document.folder_id.in_(folder_id_list),
        )
    )).scalars().all())
    doc_ids = [doc.id for doc in docs]

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
    _, folder_by_id = await _load_document_folders(db, user.entity_id)
    folder = folder_by_id.get(folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    _require_folder_manager(user, folder)
    if body.parent_id:
        parent = folder_by_id.get(body.parent_id)
        if not parent:
            raise HTTPException(404, "Parent folder not found")
        _require_folder_manager(user, parent)
    await _validate_folder_position(
        db,
        user.entity_id,
        name=folder.name,
        parent_id=body.parent_id,
        folder_id=folder_id,
    )
    folder.parent_id = body.parent_id
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
        {Capability.DELETE},
        "Only the document owner/admin or a user with delete access can delete this document",
    )
    if doc.fs_path and settings.MANOR_FS_ENABLED:
        full = _document_full_path(doc, user.entity_id)
        if full and os.path.isfile(full):
            os.remove(full)
    ok = await delete_document(db, doc_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Document not found")


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
    return [row[0] for row in result.all()]


@router.post("/{doc_id}/groups/{group_id}", status_code=200)
async def add_to_group(
    doc_id: str, group_id: str,
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
    added = await add_document_to_group(db, doc_id, group_id, entity_id=user.entity_id)
    return {"added": added}
