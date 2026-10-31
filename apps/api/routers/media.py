"""Media generation API — video/image job management."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, get_db
from packages.core.config import get_settings
from packages.core.contracts.media_job_output import public_media_job_params
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.media_job import MediaJob, MediaJobStatus
from packages.core.models.permission import Capability
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.document_service import get_document, upsert_document_by_fs_path
from packages.core.services.entity_fs import copy_entity_file_atomic
from packages.core.services.open_music_catalog import (
    OpenMusicCatalogUnavailable,
    OpenMusicDownloadError,
    OpenMusicLicenseError,
    download_open_music_track,
    get_open_music_track,
    search_open_music,
)
from packages.core.services.video_editor_render import (
    VideoEditorRenderError,
    finalize_video_editor_preview,
)

router = APIRouter(prefix="/api/v1/media", tags=["media"])


class VideoJobResponse(BaseModel):
    id: str
    status: str
    kind: str
    prompt: str
    model: str | None = None
    params: dict = {}
    result_url: str | None = None
    document_id: str | None = None
    error: str | None = None
    duration_seconds: int | None = None
    credits: int | None = None
    byok: bool = False
    # {"phase": queued|submitting|generating|saving|done|failed, "at": iso}
    progress: dict | None = None
    eta_seconds: int | None = None
    regenerated_from: str | None = None
    lineage_root: str | None = None
    run_id: str | None = None
    unit_id: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class VideoEditorFinalizeResponse(BaseModel):
    document: dict
    render: dict


class VideoEditorRecipeLinkResponse(BaseModel):
    document: dict
    recipe: dict


class FreeMusicTrackResponse(BaseModel):
    id: str
    title: str
    creator: str
    duration_seconds: float | None = None
    preview_url: str
    source_url: str
    license: str
    license_name: str
    license_url: str
    attribution: str
    source: str
    provider: str
    genres: list[str] = Field(default_factory=list)
    catalog: str = "openverse"


class FreeMusicCatalogFailure(BaseModel):
    catalog: str
    error: str


class FreeMusicSearchResponse(BaseModel):
    items: list[FreeMusicTrackResponse] = Field(default_factory=list)
    page: int
    page_count: int
    total: int
    # The catalog that answered, and catalogs that failed before it.
    catalog: str = "openverse"
    catalog_failures: list[FreeMusicCatalogFailure] = Field(default_factory=list)


class FreeMusicImportRequest(BaseModel):
    track_id: str
    source_document_id: str


class FreeMusicImportResponse(BaseModel):
    document: dict
    track: FreeMusicTrackResponse


def _safe_mp4_name(value: str, fallback: str = "edited-video") -> str:
    leaf = os.path.basename((value or "").replace("\\", "/")).strip()
    stem = os.path.splitext(leaf)[0] if leaf else fallback
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "-", stem).strip(" .-") or fallback
    return f"{stem[:220]}.mp4"


def _copy_upload_with_limit(source, target_path: str, max_bytes: int) -> int:
    size = 0
    with open(target_path, "wb") as target:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("preview_too_large")
            target.write(chunk)
    return size


async def _media_source_document(
    db: AsyncSession, document_id: str, user: User,
) -> Document | None:
    document = await get_document(
        db, document_id, user.entity_id, allow_workspace_chat_upload=True,
    )
    if document is not None:
        return document
    # Private chat attachments use hidden storage, but their uploader may
    # still create a personal output. Do not expose another user's attachment.
    return (await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.entity_id == user.entity_id,
            Document.owner_id == user.id,
            Document.source == "chat_upload",
            Document.fs_path.like("uploads/chat/%"),
            Document.is_trashed.is_(False),
        )
    )).scalar_one_or_none()


async def _workspace_output_destination(
    db: AsyncSession, source: Document, user: User,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve actual Workspace storage/shares, excluding context-only provenance."""
    from apps.api.routers.documents import _workspace_storage_for_folder
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
        infer_workspace_id_from_storage_path,
        workspace_artifact_storage_base,
    )
    from packages.core.services.workspace_access import user_can_write_workspace_artifacts

    entity_id = user.entity_id

    binding = await _workspace_storage_for_folder(
        db, entity_id=entity_id, folder_id=source.folder_id,
    )
    physical_workspace_id = await infer_workspace_id_from_storage_path(
        entity_id=entity_id, rel_path=source.fs_path or "",
    )
    if binding and physical_workspace_id and binding.workspace_id != physical_workspace_id:
        raise HTTPException(409, "Source file has conflicting Workspace ownership")
    group_rows = (await db.execute(
        select(DocumentGroup.id, DocumentGroup.workspace_id)
        .join(DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id)
        .join(Workspace, Workspace.id == DocumentGroup.workspace_id)
        .where(
            DocumentGroupMember.document_id == source.id,
            DocumentGroup.entity_id == entity_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
        .order_by(DocumentGroup.workspace_id, DocumentGroup.id)
    )).all() if source.visibility == "workspace" else []
    group_ids = [str(group_id) for group_id, _ in group_rows]
    workspace_id = (
        binding.workspace_id if binding else physical_workspace_id
    ) or (str(group_rows[0][1]) if group_rows else None)
    if not workspace_id:
        # Derived files are personal by default, even when the source sits in
        # a non-Workspace shared folder or hidden chat-upload storage.
        return None, None, []
    for shared_workspace_id in {
        workspace_id, *(str(group_workspace_id) for _, group_workspace_id in group_rows)
    }:
        if not await user_can_write_workspace_artifacts(
            db, workspace_id=shared_workspace_id, user_id=user.id,
            entity_role=user.role,
        ):
            raise HTTPException(403, "Workspace write access is required for derived files")
    if binding:
        return source.folder_id, binding.storage_dir, group_ids
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one()
    root = await ensure_workspace_artifact_folder(db, workspace)
    return root.id, workspace_artifact_storage_base(root.id), group_ids


@router.get("/free-music/search", response_model=FreeMusicSearchResponse)
async def search_free_music(
    q: str,
    page: int = 1,
    page_size: int = 18,
    _user: User = Depends(get_current_user),
):
    """Search commercially reusable music without exposing an upstream SDK."""
    try:
        return FreeMusicSearchResponse(**await search_open_music(q, page=page, page_size=page_size))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OpenMusicCatalogUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc


# A catalog or file host that is only temporarily unavailable answers 503 with
# Retry-After; an item it cannot serve at all stays a 502.
FREE_MUSIC_RETRY_AFTER_SECONDS = 30


def _free_music_upstream_error(exc: Exception) -> HTTPException:
    if getattr(exc, "retryable", False):
        return HTTPException(
            503, str(exc), headers={"Retry-After": str(FREE_MUSIC_RETRY_AFTER_SECONDS)},
        )
    return HTTPException(502, str(exc))


@router.post("/free-music/import", response_model=FreeMusicImportResponse, status_code=201)
async def import_free_music(
    body: FreeMusicImportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-verify and freeze an open-catalog track beside an editable project document."""
    from apps.api.routers.documents import (
        _copy_document_file_atomic,
        _doc_resp_for_user,
        _require_document_capability,
        _require_document_filesystem_ready,
        _require_document_upload,
        _safe_visible_filename,
        _unique_document_rel_path,
    )

    await _require_document_upload(db, user)
    source_document = await _media_source_document(db, body.source_document_id, user)
    if not source_document:
        raise HTTPException(404, "Source project document not found")
    await _require_document_capability(
        db,
        user,
        source_document,
        {Capability.EDIT},
        "Edit access to the source project document is required",
    )

    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        raise HTTPException(503, "Free music import requires the Manor entity filesystem")
    _require_document_filesystem_ready()
    try:
        track = await get_open_music_track(body.track_id)
    except OpenMusicLicenseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OpenMusicCatalogUnavailable as exc:
        raise _free_music_upstream_error(exc) from exc

    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
    persisted_path: str | None = None
    document = None
    with tempfile.TemporaryDirectory(prefix="manor-free-music-") as temp_dir:
        temp_path = os.path.join(temp_dir, "track.download")
        try:
            downloaded = await download_open_music_track(track, temp_path, max_bytes=max_bytes)
            raw_stem = os.path.splitext(track.title)[0] or "free-music"
            filename = _safe_visible_filename(
                f"{raw_stem} — {track.creator}.{downloaded.extension}",
                f"free-music.{downloaded.extension}",
            )
            target_folder_id, storage_dir, workspace_group_ids = await _workspace_output_destination(
                db, source_document, user,
            )
            target_rel = _unique_document_rel_path(
                user.entity_id,
                filename,
                rel_dir=storage_dir,
            )
            persisted_path = await _copy_document_file_atomic(
                user.entity_id,
                target_rel,
                temp_path,
                expected_size=downloaded.file_size,
                allow_empty=False,
            )
            target_visibility = "workspace" if storage_dir else "private"
            document = await upsert_document_by_fs_path(
                db,
                user.entity_id,
                fs_path=target_rel,
                name=filename,
                file_size=downloaded.file_size,
                file_type=downloaded.extension,
                mime_type=downloaded.mime_type,
                source=track.catalog,
                created_by=user.display_name or user.email or user.id,
                owner_id=user.id,
                visibility=target_visibility,
                folder_id=target_folder_id,
            )
            db.add_all(
                DocumentGroupMember(document_id=document.id, group_id=group_id)
                for group_id in workspace_group_ids
            )
            document.source = track.catalog
            document.owner_id = user.id
            document.visibility = target_visibility
            document.classification = getattr(source_document, "classification", "internal") or "internal"
            document.client_visible = False
            document.metadata_ = merge_document_metadata(
                getattr(document, "metadata_", None),
                origin={"user_id": user.id, "tool": "video_editor_free_music"},
                artifact={"role": "music_bed", "format": downloaded.extension},
                external={
                    track.catalog: {
                        "id": track.id,
                        "source": track.source,
                        "provider": track.provider,
                        "source_url": track.source_url,
                        "license": track.license,
                        "license_name": track.license_name,
                        "license_url": track.license_url,
                        "creator": track.creator,
                        "attribution": track.attribution,
                    }
                },
            )
            await db.commit()
            await db.refresh(document)
        except OpenMusicDownloadError as exc:
            raise _free_music_upstream_error(exc) from exc
        except Exception:
            if persisted_path:
                try:
                    from packages.core.services.entity_fs import resolve_path

                    persisted_abs_path = resolve_path(user.entity_id, persisted_path)
                    if os.path.isfile(persisted_abs_path):
                        os.remove(persisted_abs_path)
                except Exception:
                    pass
            raise

    if document is None:
        raise HTTPException(500, "Free music import did not create a document")
    response = await _doc_resp_for_user(db, document, user)
    document_payload = response.model_dump() if hasattr(response, "model_dump") else response.dict()
    return FreeMusicImportResponse(document=document_payload, track=FreeMusicTrackResponse(**track.to_dict()))


@router.post("/video-editor/finalize", response_model=VideoEditorFinalizeResponse)
async def finalize_video_editor_export(
    preview: UploadFile = File(...),
    source_document_id: str = Form(...),
    output_name: str = Form("edited-video.mp4"),
    fps: int = Form(30),
    crf: int = Form(18),
    preset: str = Form("veryfast"),
    target_duration_seconds: float | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Finalize a browser-composited preview as a delivery-safe H.264/AAC MP4."""
    from apps.api.routers.documents import (
        _doc_resp_for_user,
        _require_document_capability,
        _require_document_upload,
    )

    await _require_document_upload(db, user)
    source_document = await _media_source_document(db, source_document_id, user)
    if not source_document:
        raise HTTPException(404, "Source video not found")
    await _require_document_capability(
        db,
        user,
        source_document,
        {Capability.EDIT},
        "Edit access to the source video is required",
    )

    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        raise HTTPException(503, "Video finalization requires the Manor entity filesystem")
    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
    final_name = _safe_mp4_name(output_name)
    target_folder_id, storage_dir, workspace_group_ids = await _workspace_output_destination(
        db, source_document, user,
    )
    target_visibility = "workspace" if storage_dir else "private"
    if (
        target_folder_id is None
        and source_document.visibility == "private"
        and source_document.owner_id == user.id
        and source_document.folder_id
    ):
        target_folder_id = source_document.folder_id
        storage_dir = os.path.dirname(source_document.fs_path or "") or None
    source_dir = storage_dir or ""
    target_leaf = f"{os.path.splitext(final_name)[0]}-{generate_ulid()[:10].lower()}.mp4"
    target_rel = "/".join(part for part in (source_dir, target_leaf) if part)
    target_visibility = "workspace" if storage_dir else "private"
    persisted_path: str | None = None

    with tempfile.TemporaryDirectory(prefix="manor-video-editor-finalize-") as temp_dir:
        preview_path = os.path.join(temp_dir, "browser-preview.webm")
        output_path = os.path.join(temp_dir, "final.mp4")
        try:
            await preview.seek(0)
            try:
                preview_size = await asyncio.to_thread(
                    _copy_upload_with_limit,
                    preview.file,
                    preview_path,
                    max_bytes,
                )
            except ValueError as exc:
                if str(exc) == "preview_too_large":
                    raise HTTPException(413, f"Video preview too large. Max {settings.MANOR_MAX_UPLOAD_MB}MB") from exc
                raise
            if preview_size <= 0:
                raise HTTPException(422, "Video preview is empty")

            try:
                render = await finalize_video_editor_preview(
                    preview_path,
                    output_path,
                    fps=fps,
                    crf=crf,
                    preset=preset,
                    target_duration_seconds=target_duration_seconds,
                )
            except VideoEditorRenderError as exc:
                message = str(exc)
                status = 503 if "required" in message.lower() or "unavailable" in message.lower() else 422
                raise HTTPException(status, message) from exc

            persisted_path = await asyncio.to_thread(
                copy_entity_file_atomic,
                user.entity_id,
                target_rel,
                output_path,
                expected_size=render.file_size,
                allow_empty=False,
            )
            document = await upsert_document_by_fs_path(
                db,
                user.entity_id,
                fs_path=target_rel,
                name=final_name,
                file_size=render.file_size,
                file_type="mp4",
                mime_type="video/mp4",
                source="ai_generated",
                created_by=user.id,
                owner_id=user.id,
                visibility=target_visibility,
                folder_id=target_folder_id,
            )
            db.add_all(
                DocumentGroupMember(document_id=document.id, group_id=group_id)
                for group_id in workspace_group_ids
            )
            document.source = "ai_generated"
            document.owner_id = user.id
            document.visibility = target_visibility
            document.classification = getattr(source_document, "classification", "internal") or "internal"
            document.client_visible = False
            document.metadata_ = merge_document_metadata(
                getattr(document, "metadata_", None),
                origin={"user_id": user.id, "tool": "video_editor_finalize"},
                artifact={"role": "final_video", "format": "mp4"},
                generation={
                    "operation": "video_editor_finalize",
                    "source_document_id": source_document.id,
                    "fps": round(render.fps, 3),
                    "crf": min(28, max(14, int(crf or 18))),
                    "preset": preset,
                },
            )
            await db.commit()
            await db.refresh(document)
        except Exception:
            if persisted_path and os.path.isfile(persisted_path):
                try:
                    os.remove(persisted_path)
                except OSError:
                    pass
            raise
        finally:
            await preview.close()

    document_response = await _doc_resp_for_user(db, document, user)
    document_payload = document_response.model_dump() if hasattr(document_response, "model_dump") else document_response.dict()
    return VideoEditorFinalizeResponse(document=document_payload, render=asdict(render))


@router.post("/video-editor/link-recipe", response_model=VideoEditorRecipeLinkResponse)
async def link_video_editor_recipe(
    final_document_id: str = Form(...),
    recipe_document_id: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pair a rendered/source video with its editable timeline sidecar."""
    from apps.api.routers.documents import _doc_resp_for_user, _require_document_capability

    final_document = await get_document(db, final_document_id, user.entity_id)
    recipe_document = await get_document(db, recipe_document_id, user.entity_id)
    if not final_document:
        raise HTTPException(404, "Video document not found")
    if not recipe_document:
        raise HTTPException(404, "Video edit recipe not found")
    await _require_document_capability(
        db,
        user,
        final_document,
        {Capability.EDIT},
        "Edit access to the video is required",
    )
    await _require_document_capability(
        db,
        user,
        recipe_document,
        {Capability.EDIT},
        "Edit access to the video recipe is required",
    )

    video_mime = str(final_document.mime_type or "").lower()
    video_file_type = str(final_document.file_type or "").lower()
    if not (
        video_mime.startswith("video/")
        or video_file_type in {"mp4", "webm", "mov", "mkv"}
    ):
        raise HTTPException(422, "Target document is not a video")
    recipe_name = str(recipe_document.name or "")
    recipe_type = str(recipe_document.mime_type or recipe_document.file_type or "").lower()
    if not recipe_name.lower().endswith(".video-edit.json") or recipe_type not in {
        "application/json",
        "text/json",
        "json",
    }:
        raise HTTPException(422, "Recipe must be a .video-edit.json document")
    if recipe_document.folder_id != final_document.folder_id:
        raise HTTPException(422, "Video and recipe must be stored in the same folder")

    final_document.metadata_ = merge_document_metadata(
        getattr(final_document, "metadata_", None),
        artifact={
            "editor_recipe_document_id": recipe_document.id,
            "editor_recipe_path": recipe_document.fs_path,
            "editor_recipe_name": recipe_document.name,
        },
        generation={
            "editor_recipe_document_id": recipe_document.id,
            "editor_recipe_path": recipe_document.fs_path,
        },
    )
    recipe_document.metadata_ = merge_document_metadata(
        getattr(recipe_document, "metadata_", None),
        artifact={
            "role": "editor_recipe",
            "storage_scope": "artifact",
            "paired_video_document_id": final_document.id,
            "paired_video_path": final_document.fs_path,
        },
        generation={
            "operation": "video_editor_recipe_link",
            "final_document_id": final_document.id,
            "final_video_path": final_document.fs_path,
        },
    )
    await db.commit()
    await db.refresh(final_document)
    await db.refresh(recipe_document)

    final_response = await _doc_resp_for_user(db, final_document, user)
    recipe_response = await _doc_resp_for_user(db, recipe_document, user)
    final_payload = final_response.model_dump() if hasattr(final_response, "model_dump") else final_response.dict()
    recipe_payload = recipe_response.model_dump() if hasattr(recipe_response, "model_dump") else recipe_response.dict()
    return VideoEditorRecipeLinkResponse(document=final_payload, recipe=recipe_payload)


_VIDEO_TYPICAL_SAMPLE = 20
# What a new version replays from the job it redoes; the rest is recomputed.
_REGENERATE_REPLAYED_PARAMS = (
    "duration", "resolution", "aspect_ratio", "first_frame_url", "last_frame_url",
    "reference_urls", "reference_video_urls", "audio_reference_urls", "generate_audio",
    "camera_fixed", "seed", "output_name",
)


async def _typical_video_seconds(db: AsyncSession, entity_id: str, model: str | None) -> float | None:
    """Median start-to-finish time of this company's recent jobs on the model."""
    if not model:
        return None
    rows = (await db.execute(
        select(MediaJob.started_at, MediaJob.completed_at)
        .where(
            MediaJob.entity_id == entity_id,
            MediaJob.kind == "video",
            MediaJob.model == model,
            MediaJob.status == MediaJobStatus.COMPLETED.value,
            MediaJob.started_at.is_not(None),
            MediaJob.completed_at.is_not(None),
        )
        .order_by(MediaJob.completed_at.desc())
        .limit(_VIDEO_TYPICAL_SAMPLE)
    )).all()
    durations = sorted(
        (completed - started).total_seconds() for started, completed in rows if completed > started
    )
    if not durations:
        return None
    return durations[len(durations) // 2]


def _eta_seconds(job: MediaJob, typical: float | None) -> int | None:
    if typical is None or job.status in MediaJobStatus.terminal():
        return None
    began = job.started_at or job.created_at
    if began is None:
        return int(typical)
    if began.tzinfo is None:
        began = began.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - began).total_seconds()
    return max(0, int(typical - elapsed))


def _job_response(job: MediaJob, *, eta_seconds: int | None = None) -> VideoJobResponse:
    params = job.params or {}
    return VideoJobResponse(
        id=job.id,
        status=job.status,
        kind=job.kind,
        prompt=job.prompt,
        model=job.model,
        params=public_media_job_params(params),
        result_url=job.result_url,
        document_id=params.get("result_document_id"),
        error=job.error,
        duration_seconds=job.duration_seconds,
        credits=job.credits,
        byok=job.byok,
        progress=params.get("progress") if isinstance(params.get("progress"), dict) else None,
        eta_seconds=eta_seconds,
        regenerated_from=params.get("regenerated_from"),
        lineage_root=params.get("lineage_root"),
        run_id=params.get("run_id"),
        unit_id=params.get("unit_id"),
        created_at=job.created_at.isoformat() if job.created_at else None,
        started_at=job.started_at.isoformat() if job.started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


async def _job_responses(db: AsyncSession, entity_id: str, jobs: list[MediaJob]) -> list[VideoJobResponse]:
    typical: dict[str | None, float | None] = {}
    for job in jobs:
        if job.kind == "video" and job.status not in MediaJobStatus.terminal() and job.model not in typical:
            typical[job.model] = await _typical_video_seconds(db, entity_id, job.model)
    return [_job_response(job, eta_seconds=_eta_seconds(job, typical.get(job.model))) for job in jobs]


@router.get("/jobs/{job_id}", response_model=VideoJobResponse)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the status of a media generation job."""
    job = (await db.execute(
        select(MediaJob).where(
            MediaJob.id == job_id,
            MediaJob.entity_id == user.entity_id,
        )
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    return (await _job_responses(db, user.entity_id, [job]))[0]


@router.get("/jobs", response_model=list[VideoJobResponse])
async def list_jobs(
    kind: str | None = None,
    status: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    run_id: str | None = None,
    lineage_root: str | None = None,
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List media generation jobs for the current entity.

    ``lineage_root`` returns every version of one clip, oldest first, so a
    viewer can number them v1, v2, v3.
    """
    q = select(MediaJob).where(MediaJob.entity_id == user.entity_id)
    if kind:
        q = q.where(MediaJob.kind == kind)
    if status:
        q = q.where(MediaJob.status == status)
    if conversation_id:
        q = q.where(MediaJob.conversation_id == conversation_id)
    if workspace_id:
        q = q.where(MediaJob.params["workspace_id"].astext == workspace_id)
    if run_id:
        q = q.where(MediaJob.params["run_id"].astext == run_id)
    if lineage_root:
        q = q.where(or_(
            MediaJob.id == lineage_root,
            MediaJob.params["lineage_root"].astext == lineage_root,
        )).order_by(MediaJob.created_at.asc())
    else:
        q = q.order_by(MediaJob.created_at.desc())
    rows = list((await db.execute(q.limit(min(limit, 50)))).scalars().all())
    return await _job_responses(db, user.entity_id, rows)


class CreateVideoJobRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    workspace_id: str | None = None
    name: str | None = Field(default=None, max_length=200)
    model: str | None = None
    duration: int | None = Field(default=None, ge=1, le=60)
    resolution: str | None = None
    aspect_ratio: str | None = None
    first_frame_url: str | None = None
    reference_urls: list[str] = Field(default_factory=list, max_length=30)


class RegenerateVideoJobRequest(BaseModel):
    """A new version of a clip. Unset fields keep the earlier version's value."""

    prompt: str | None = Field(default=None, max_length=8000)
    model: str | None = None
    duration: int | None = Field(default=None, ge=1, le=60)
    resolution: str | None = None
    aspect_ratio: str | None = None
    # Keep the earlier version's seed (its composition); false asks for another take.
    keep_seed: bool = True


async def _require_workspace_writer(db: AsyncSession, user: User, workspace_id: str | None) -> None:
    from packages.core.services.workspace_access import user_can_write_workspace_artifacts

    if workspace_id and not await user_can_write_workspace_artifacts(
        db, workspace_id=workspace_id, user_id=user.id, entity_role=user.role,
    ):
        raise HTTPException(403, "Workspace write access is required to generate here")


async def _start_video_job(
    db: AsyncSession,
    user: User,
    *,
    prompt: str,
    name: str,
    params: dict,
    workspace_id: str | None,
    conversation_id: str | None = None,
    workflow_context: dict | None = None,
    lineage: dict | None = None,
) -> VideoJobResponse:
    """Create a video job through the same runtime path agents use.

    Credits, BYOK routing and the reservation are all decided there; a refusal
    (no credits, unsupported model or reference) comes back as a 422 the user
    can act on.
    """
    from packages.core.ai.runtime import runtime_generate_video_media

    raw = await runtime_generate_video_media(
        entity_id=user.entity_id,
        user_id=user.id,
        prompt=prompt,
        name=name,
        params=params,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        workflow_context=workflow_context,
        lineage=lineage,
    )
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(502, "Video generation returned an unreadable result")
    job_id = result.get("job_id") if isinstance(result, dict) else None
    if not job_id:
        raise HTTPException(422, str((result or {}).get("error") or "Video generation was not started"))
    job = (await db.execute(
        select(MediaJob).where(MediaJob.id == job_id, MediaJob.entity_id == user.entity_id)
    )).scalar_one()
    return (await _job_responses(db, user.entity_id, [job]))[0]


@router.post("/jobs", response_model=VideoJobResponse, status_code=201)
async def create_job(
    body: CreateVideoJobRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a video now, outside an agent turn (for example from an input picker)."""
    await _require_workspace_writer(db, user, body.workspace_id)
    params = {
        key: value
        for key, value in body.model_dump(exclude={"prompt", "workspace_id", "name"}).items()
        if value not in (None, [])
    }
    return await _start_video_job(
        db, user,
        prompt=body.prompt.strip(),
        name=(body.name or "").strip(),
        params=params,
        workspace_id=body.workspace_id,
    )


@router.post("/jobs/{job_id}/regenerate", response_model=VideoJobResponse, status_code=201)
async def regenerate_job(
    job_id: str,
    body: RegenerateVideoJobRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Make a new version of a finished video clip; the earlier versions stay."""
    job = (await db.execute(
        select(MediaJob).where(MediaJob.id == job_id, MediaJob.entity_id == user.entity_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.kind != "video":
        raise HTTPException(422, "Only video clips can be regenerated")
    if job.status not in MediaJobStatus.terminal():
        raise HTTPException(409, "This clip is still being generated")
    params = dict(job.params or {})
    workspace_id = str(params.get("workspace_id") or "").strip() or None
    if job.user_id != user.id:
        if not workspace_id:
            raise HTTPException(403, "Only the person who made this clip can regenerate it")
        await _require_workspace_writer(db, user, workspace_id)

    replay = {key: params[key] for key in _REGENERATE_REPLAYED_PARAMS if params.get(key) is not None}
    if not body.keep_seed:
        replay.pop("seed", None)
    for key in ("duration", "resolution", "aspect_ratio"):
        value = getattr(body, key)
        if value is not None:
            replay[key] = value
    model = (body.model or job.model or "").strip()
    if model:
        replay["model"] = model
    prompt = (body.prompt or "").strip() or str(params.get("source_prompt") or job.prompt)
    workflow_context = {
        source: params[key]
        for key, source in (
            ("run_id", "_workflow_run_id_from_context"),
            ("unit_id", "_workflow_scene_id_from_context"),
        )
        if params.get(key)
    }
    return await _start_video_job(
        db, user,
        prompt=prompt,
        name=str(params.get("output_name") or ""),
        params=replay,
        workspace_id=workspace_id,
        conversation_id=job.conversation_id,
        workflow_context=workflow_context or None,
        lineage={"regenerated_from": job.id, "lineage_root": str(params.get("lineage_root") or job.id)},
    )
