"""Extract file content for chat context injection.

Handles three attachment types:
  1. Local image uploads — base64-encoded data URLs the LLM consumes
     directly via ``image_url`` content blocks (vision-capable models).
  2. Local non-image files — text extracted, injected into the message
     as an ``<attached_files>…</attached_files>`` text block.
  3. Knowledge-base documents — content fetched from filesystem or metadata.

Returns ``FileAttachments`` with both halves so callers can build a
multimodal message (text + images) when the model supports it. Pure
text-only callers can ignore ``image_blocks``.

Backwards compat: ``build_file_context`` still returns ``FileAttachments``
where ``__str__`` resolves to the text part — so any caller doing
``if file_context:`` keeps working.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    ChatSurface,
    RUNTIME_CHAT_ATTACHMENT_VOICE_SOURCE,
    runtime_assert_credit_available,
)
from packages.core.constants.document_groups import WorkspaceDocumentGroupKind
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.services.file_refs import parse_file_reference
from packages.core.services.generated_file_refs import entity_fs_open_url
from packages.core.models.permission import Capability, ResourceGrant, ResourceType, Share, Visibility
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.text_extraction import extract_text
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.image_formats import heif_mime_from_header, register_heif_decoder
from packages.core.services.image_metadata import ImageMetadataStripError, strip_image_metadata

logger = logging.getLogger(__name__)


# Cap inline image size — data URLs are counted as prompt text by some
# OpenRouter routes, so large/many images can blow past context limits. We
# still save every image to /api/v1/fs so tools like generate_video can use it.
_MAX_INLINE_IMAGE_BYTES = int(os.getenv("CHAT_INLINE_IMAGE_MAX_BYTES", str(1 * 1024 * 1024)))
_MAX_INLINE_IMAGE_TOTAL_BYTES = int(os.getenv("CHAT_INLINE_IMAGE_TOTAL_MAX_BYTES", str(1536 * 1024)))
_MAX_INLINE_TEXT_CHARS = int(os.getenv("CHAT_INLINE_TEXT_MAX_CHARS", str(20_000)))
_MAX_INLINE_TEXT_TOTAL_CHARS = int(os.getenv("CHAT_INLINE_TEXT_TOTAL_MAX_CHARS", str(60_000)))
_IMAGE_MIME_PREFIX = "image/"

# Magic-byte signatures for common image formats.
_IMAGE_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP — check below
]


def _detect_image_mime(data: bytes, declared: str) -> str:
    """Return the actual image MIME type by inspecting magic bytes.

    Falls back to ``declared`` if the format cannot be determined.
    """
    heif_mime = heif_mime_from_header(data[:256])
    if heif_mime:
        return heif_mime
    for sig, mime in _IMAGE_SIGNATURES:
        if data[:len(sig)] == sig:
            # RIFF container could be non-webp; verify WEBP marker.
            if sig == b"RIFF":
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    return "image/webp"
                return declared
            return mime
    return declared


# A referenced video/audio reaches the model as a URL only; its stored
# description and transcript let the agent answer without probing the file.
_KB_MEDIA_DESCRIPTION_MAX_CHARS = 6000


def _kb_media_description(doc: Document) -> list[str]:
    from packages.core.services.knowledge_media_analysis import stored_media_text

    text = stored_media_text(doc.metadata_)
    if not text:
        return []
    if len(text) > _KB_MEDIA_DESCRIPTION_MAX_CHARS:
        text = text[:_KB_MEDIA_DESCRIPTION_MAX_CHARS] + "…"
    return [f"[Content of {doc.name} (automatic description and transcript)]\n{text}"]


def _prepare_inline_image(data: bytes, declared_mime: str) -> tuple[bytes, str] | None:
    """Return image bytes small enough for multimodal prompt inlining.

    Large KB/generated images are common. Instead of dropping them from vision
    context, try to downscale/re-encode to JPEG under the inline cap while
    keeping the original saved URL in text context.
    """
    actual_mime = _detect_image_mime(data, declared_mime)
    if len(data) <= _MAX_INLINE_IMAGE_BYTES and actual_mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return data, actual_mime
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        return None

    try:
        if actual_mime in {"image/heic", "image/heif"}:
            register_heif_decoder()
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in {"RGB", "L"}:
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "RGBA":
                    background.paste(img, mask=img.getchannel("A"))
                else:
                    background.paste(img.convert("RGB"))
                img = background
            else:
                img = img.convert("RGB")

            for max_side in (1600, 1280, 1024, 800):
                candidate = img.copy()
                candidate.thumbnail((max_side, max_side))
                for quality in (86, 78, 70, 62):
                    buf = io.BytesIO()
                    candidate.save(buf, format="JPEG", quality=quality, optimize=True)
                    compressed = buf.getvalue()
                    if len(compressed) <= _MAX_INLINE_IMAGE_BYTES:
                        return compressed, "image/jpeg"
    except Exception:
        logger.debug("failed to prepare inline image", exc_info=True)
        return None
    return None

_AUDIO_MIME_PREFIXES = ("audio/",)
_VIDEO_MIME_PREFIX = "video/"
_VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "webm", "avi", "mkv"}
_AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "ogg", "opus", "flac", "webm"}
_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif"}
_WORKSPACE_DEFAULT_COLLECTION_NAME = "Workspace Knowledge"
_WORKSPACE_GROUP_DEFAULT_KIND = WorkspaceDocumentGroupKind.DEFAULT_COLLECTION.value
_ATTACHMENT_TEXT_READY_NOTE = (
    "Attachment text below was extracted by the backend for this turn. "
    "Use it directly for ordinary analysis, summarization, and Q&A; invoke a skill only "
    "for a skill-specific operation beyond reading this attachment."
)


def _safe_upload_ext(filename: str, mime: str) -> str:
    if mime in {"image/heic", "image/heif"}:
        return ".heic" if mime == "image/heic" else ".heif"
    ext = os.path.splitext(filename or "")[1].lower()
    if not ext and mime:
        ext = "." + mime.split(";", 1)[0].split("/")[-1].lower()
    ext = re.sub(r"[^a-z0-9.]", "", ext or ".bin")
    return ext if ext.startswith(".") else f".{ext}"


def _save_chat_upload(
    content: bytes, filename: str, entity_id: str, mime: str,
    *, storage_scope: str | None = None,
) -> tuple[str, str]:
    """Persist an attached file to the entity filesystem.

    Returns ``(relative_path, local_url)``. The filename is content-hash based
    so repeated references to the same upload do not create duplicate files.
    """
    from packages.core.services.entity_fs import get_entity_root, write_entity_file_atomic

    ext = _safe_upload_ext(filename, mime)
    if ext == ".jpeg":
        ext = ".jpg"
    short_hash = hashlib.sha256(content).hexdigest()[:16]
    safe_name = f"{short_hash}{ext}"

    scope_dir = hashlib.sha256(storage_scope.encode()).hexdigest()[:16] if storage_scope else None
    rel_path = "/".join(part for part in ("uploads/chat", scope_dir, safe_name) if part)
    filepath = os.path.join(get_entity_root(entity_id), rel_path)
    if not os.path.isfile(filepath) or os.path.getsize(filepath) != len(content):
        write_entity_file_atomic(
            entity_id,
            rel_path,
            content,
            expected_size=len(content),
            allow_empty=True,
        )
    return rel_path, entity_fs_open_url(entity_id, rel_path)


def _save_chat_image(
    content: bytes, filename: str, entity_id: str, mime: str,
    *, storage_scope: str | None = None,
) -> str:
    """Persist an attached image and return a stable local URL."""
    if storage_scope:
        _, local_url = _save_chat_upload(
            content, filename, entity_id, mime, storage_scope=storage_scope,
        )
    else:
        _, local_url = _save_chat_upload(content, filename, entity_id, mime)
    return local_url


def _fs_path_from_local_url(entity_id: str, local_url: str) -> str | None:
    parsed = parse_file_reference(local_url, entity_id=entity_id)
    if parsed is None or parsed.entity_id != entity_id:
        return None
    return parsed.fs_path or None


def _document_media_extensions(doc: Document) -> set[str]:
    candidates = (
        getattr(doc, "file_type", None),
        getattr(doc, "fs_path", None),
        getattr(doc, "file_url", None),
        getattr(doc, "name", None),
    )
    extensions: set[str] = set()
    for candidate in candidates:
        value = str(candidate or "").strip().lower()
        if not value:
            continue
        ext = value.rsplit("?", 1)[0].rsplit("#", 1)[0].rsplit(".", 1)[-1].strip()
        if ext:
            extensions.add(ext)
    return extensions


def _document_is_video(doc: Document, mime: str) -> bool:
    return mime.startswith(_VIDEO_MIME_PREFIX) or bool(_document_media_extensions(doc) & _VIDEO_EXTENSIONS)


def _document_is_audio(doc: Document, mime: str) -> bool:
    return any(mime.startswith(p) for p in _AUDIO_MIME_PREFIXES) or bool(
        _document_media_extensions(doc) & _AUDIO_EXTENSIONS
    )


def _document_is_image(doc: Document, mime: str) -> bool:
    return mime.startswith(_IMAGE_MIME_PREFIX) or bool(_document_media_extensions(doc) & _IMAGE_EXTENSIONS)


def _compact_attachment_ref(ref: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in ref.items() if value is not None and value != ""}


async def _ensure_workspace_upload_group(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
) -> DocumentGroup | None:
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if not workspace:
        return None

    groups = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        ).order_by(DocumentGroup.created_at.asc())
    )).scalars().all()
    for group in groups:
        settings = group.settings or {}
        if settings.get("workspace_file_bucket"):
            continue
        if settings.get("default_collection") or settings.get("kind") == _WORKSPACE_GROUP_DEFAULT_KIND:
            return group

    group = DocumentGroup(
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=_WORKSPACE_DEFAULT_COLLECTION_NAME,
        settings={
            "kind": _WORKSPACE_GROUP_DEFAULT_KIND,
            "default_collection": True,
            "purpose": "General workspace knowledge available to agents.",
            "user_manageable": True,
        },
    )
    db.add(group)
    await db.flush()
    return group


def _attachment_document_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("id", "document_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def bind_workspace_knowledge_documents(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    attachments: Any = None,
    tool_name: str = "hitl_upload",
) -> list[dict[str, Any]]:
    """Project HITL attachment Documents into this Workspace's Knowledge.

    Need Input uploads that go through ``/documents/upload`` without a folder
    become private entity-root files. Fulfilling the workspace card makes the
    same Document visible from the Workspace root without moving its bytes or
    changing its durable identity. Missing or foreign ids stay unchanged so
    the human answer still fulfills.
    """
    if not isinstance(attachments, list):
        return []

    from packages.core.services.document_access import (
        effective_document_capabilities_for_user,
        user_can_read_document,
    )
    from packages.core.services.document_service import _get_document_for_policy_mutation
    from packages.core.services.knowledge_visibility import normalize_rel_path
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
    )

    bound: list[dict[str, Any]] = []
    group = None
    workspace = None
    artifact_folder = None
    mutated = False
    for item in attachments:
        if not isinstance(item, dict):
            continue
        original = dict(item)
        document_id = _attachment_document_id(item)
        if not document_id:
            bound.append(original)
            continue
        async def authorize_attachment(auth_db: AsyncSession, document: Document) -> None:
            capabilities = await effective_document_capabilities_for_user(
                auth_db,
                document=document,
                user_id=user_id,
            )
            can_publish = bool(capabilities.intersection({
                Capability.SHARE_INTERNAL,
                Capability.GRANT_ACCESS,
            }))
            can_read = await user_can_read_document(
                auth_db,
                document,
                entity_id=entity_id,
                user_id=user_id,
                allow_redacted=False,
            )
            if not can_publish or not can_read:
                raise HTTPException(404, "Document not found")

        doc = await _get_document_for_policy_mutation(
            db,
            document_id,
            entity_id,
            is_trashed=False,
            require_visible_path=False,
            mutation_authorizer=authorize_attachment,
        )
        if doc is None:
            bound.append(original)
            continue
        workspace_memberships = set((await db.scalars(
            select(DocumentGroup.workspace_id)
            .join(DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id)
            .where(
                DocumentGroupMember.document_id == doc.id,
                DocumentGroup.workspace_id.is_not(None),
            )
        )).all())
        has_document_acl = (await db.scalar(
            select(ResourceGrant.id).where(
                ResourceGrant.entity_id == entity_id,
                ResourceGrant.resource_type == ResourceType.DOCUMENT,
                ResourceGrant.resource_id == doc.id,
            ).limit(1)
        )) is not None or (await db.scalar(
            select(Share.id).where(
                Share.entity_id == entity_id,
                Share.resource_type == ResourceType.DOCUMENT,
                Share.resource_id == doc.id,
            ).limit(1)
        )) is not None
        origin = doc.metadata_.get("origin") if isinstance(doc.metadata_, dict) else None
        origin_workspace_id = str((origin or {}).get("workspace_id") or "").strip()
        has_external_workspace_origin = bool(
            origin_workspace_id and origin_workspace_id != workspace_id
        )
        can_auto_home = (
            doc.folder_id is None
            and not has_external_workspace_origin
            and not (workspace_memberships - {workspace_id})
            and not has_document_acl
        )
        if can_auto_home:
            if doc.visibility == Visibility.PRIVATE:
                doc.visibility = Visibility.WORKSPACE
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                origin={
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "tool_name": tool_name,
                },
            )
            mutated = True
        if group is None:
            group = await _ensure_workspace_upload_group(
                db, entity_id=entity_id, workspace_id=workspace_id,
            )
        if group is not None:
            member = (await db.execute(
                select(DocumentGroupMember).where(
                    DocumentGroupMember.document_id == doc.id,
                    DocumentGroupMember.group_id == group.id,
                ).limit(1)
            )).scalar_one_or_none()
            if not member:
                db.add(DocumentGroupMember(document_id=doc.id, group_id=group.id))
        if workspace is None:
            workspace = (await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                ).limit(1)
            )).scalar_one_or_none()
            if workspace is not None:
                artifact_folder = await ensure_workspace_artifact_folder(db, workspace)
        if can_auto_home and artifact_folder is not None:
            if doc.folder_id != artifact_folder.id:
                doc.folder_id = artifact_folder.id
                mutated = True
        enriched = dict(original)
        enriched["id"] = doc.id
        enriched["document_id"] = doc.id
        enriched["name"] = doc.name or original.get("name") or doc.id
        enriched["type"] = original.get("type") or "knowledge"
        if doc.fs_path:
            enriched["fs_path"] = doc.fs_path
        bound.append(enriched)

    if mutated:
        await db.flush()
        await bump_tool_cache_version(entity_id, "documents")
    return bound


async def resolve_workspace_input_attachments(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str,
    attachments: Any,
) -> list[dict[str, Any]]:
    """Resolve user-selected documents before a Workspace input is consumed.

    Existing Knowledge remains in its original location and access boundary.
    Only an explicitly local input upload that is actor-owned, unfiled and has
    no prior sharing or Workspace association follows upload projection.
    """
    from packages.core.services.document_access import (
        document_workspace_ids,
        get_visible_document,
    )
    from packages.core.services.workspace_access import user_can_write_workspace_artifacts

    if not isinstance(attachments, list) or len(attachments) > 50:
        raise HTTPException(422, "Attachments must be a list of at most 50 documents")
    documents: list[Document] = []
    seen: set[str] = set()
    upload_intent_ids: set[str] = set()
    for item in attachments:
        document_id = _attachment_document_id(item)
        if not document_id:
            raise HTTPException(422, "Each attachment must identify a Knowledge document")
        if item.get("origin") == "input_upload":
            upload_intent_ids.add(document_id)
        if document_id in seen:
            continue
        document = await get_visible_document(
            db, document_id, entity_id, user_id=user_id,
        )
        if document is None:
            raise HTTPException(404, "An attached document is no longer available")
        seen.add(document_id)
        documents.append(document)

    upload_ids = sorted(
        document.id for document in documents
        if document.id in upload_intent_ids
        and document.source == "upload" and document.owner_id == user_id
        and document.folder_id is None
    )
    if upload_ids:
        # Two independently answered cards may carry the same upload. Lock in
        # a stable order and reload before deciding its first Workspace home.
        (await db.scalars(select(Document).where(
            Document.entity_id == entity_id, Document.id.in_(upload_ids),
        ).order_by(Document.id).with_for_update().execution_options(populate_existing=True))).all()
        for document_id in upload_ids:
            if await get_visible_document(db, document_id, entity_id, user_id=user_id) is None:
                raise HTTPException(404, "An attached document is no longer available")

    resolved: list[dict[str, Any]] = []
    for document in documents:
        reference = {
            "id": document.id,
            "document_id": document.id,
            "name": document.name,
            "type": "knowledge",
            "viewer_url": f"/viewer/{document.id}",
        }
        if document.fs_path:
            reference["fs_path"] = document.fs_path
        # A selected existing document must not be moved or implicitly shared.
        unfiled_upload = (
            document.id in upload_intent_ids
            and document.source == "upload"
            and document.owner_id == user_id
            and document.folder_id is None
            and not await document_workspace_ids(db, document)
        )
        has_acl = False
        if unfiled_upload:
            has_acl = (await db.scalar(select(ResourceGrant.id).where(
                ResourceGrant.entity_id == entity_id,
                ResourceGrant.resource_type == ResourceType.DOCUMENT,
                ResourceGrant.resource_id == document.id,
            ).limit(1))) is not None or (await db.scalar(select(Share.id).where(
                Share.entity_id == entity_id,
                Share.resource_type == ResourceType.DOCUMENT,
                Share.resource_id == document.id,
            ).limit(1))) is not None
        if unfiled_upload and not has_acl and await user_can_write_workspace_artifacts(
            db, workspace_id=workspace_id, user_id=user_id,
        ):
            bound = await bind_workspace_knowledge_documents(
                db, entity_id=entity_id, workspace_id=workspace_id,
                user_id=user_id, attachments=[reference],
            )
            reference = bound[0]
        resolved.append(reference)
    return resolved


async def _register_workspace_chat_upload(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    surface: ChatSurface | None = None,
    filename: str,
    saved_rel_path: str,
    saved_url: str,
    file_size: int,
    mime: str,
) -> str | None:
    """Make a transient chat upload visible in Workspace Knowledge.

    Chat runtime files live under the hidden ``uploads/chat`` prefix so they do
    not clutter global Knowledge. In workspace chat, users still expect the
    uploaded document to appear in that workspace, so we attach the Document row
    to the workspace's default collection explicitly.
    """
    if surface != ChatSurface.WORKSPACE_CHAT or not workspace_id or not user_id:
        return None

    from packages.core.models.permission import Capability
    from packages.core.services.actor_authorization import (
        AuthenticatedUserCredential, resolve_current_user_actor,
    )
    from packages.core.services.document_access import effective_document_capabilities_for_user
    from packages.core.services.document_service import create_document, _enforce_storage_limit
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
        lock_workspace_recipient_authorization,
        user_can_write_workspace_artifacts,
    )

    # Match the existing lifecycle/authorization lock order: Workspace, its
    # permission inputs, then Document. Re-read authority after any lock wait.
    workspace = await lock_workspace_access_boundary(
        db, workspace_id=workspace_id, entity_id=entity_id,
    )
    if workspace is None or workspace.deleted_at is not None:
        return None
    await lock_workspace_recipient_authorization(
        db, workspace_id=workspace_id, entity_id=entity_id, user_id=user_id,
    )

    # The authenticated entity can differ from the User's primary entity.
    # Resolve current membership instead of trusting an id or cached role.
    actor = await resolve_current_user_actor(
        db,
        AuthenticatedUserCredential(user_id=user_id, entity_id=entity_id, token_version=None),
    )
    if actor is None:
        return None
    # Prior request authorization may have cached an older WorkspaceStaff
    # object. Refresh it under the authorization locks before the role helper.
    (await db.execute(
        select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.user_id == user_id,
        ).execution_options(populate_existing=True)
    )).scalars().all()
    if not await user_can_write_workspace_artifacts(
        db, workspace_id=workspace_id, user_id=user_id, entity_role=actor.role,
    ):
        return None

    name = os.path.basename(filename or saved_rel_path) or "attachment"
    ext = os.path.splitext(name)[1].lstrip(".").lower() or None
    if ext and len(ext) > 20:
        ext = ext[:20]
    existing = (await db.execute(
        select(Document)
        .where(
            Document.entity_id == entity_id,
            Document.fs_path == saved_rel_path,
        )
        .order_by(
            Document.is_trashed.asc(),
            func.coalesce(Document.updated_at, Document.created_at).desc(),
            Document.created_at.desc(),
            Document.id.desc(),
        )
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()

    # Keep Document and collection membership atomic even if collection
    # resolution fails after the document has been flushed.
    async with db.begin_nested():
        origin = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "tool_name": "chat_upload",
        }
        chat_upload = {
            "url": saved_url,
            "path": saved_rel_path,
        }
        if existing:
            capabilities = await effective_document_capabilities_for_user(
                db, document=existing, user_id=user_id, _actor=actor,
            )
            if Capability.EDIT not in capabilities:
                return None
            if existing.is_trashed:
                await _enforce_storage_limit(db, entity_id)
            existing.name = name
            existing.file_url = saved_url
            existing.file_size = file_size
            existing.file_type = ext
            existing.mime_type = mime or existing.mime_type
            existing.source = "chat_upload"
            existing.created_by = existing.created_by or user_id
            existing.owner_id = existing.owner_id or user_id
            existing.visibility = Visibility.WORKSPACE
            existing.is_trashed = False
            existing.trashed_at = None
            existing.trashed_by = None
            existing.metadata_ = merge_document_metadata(
                existing.metadata_,
                origin=origin,
                extra={
                    "chat_upload": chat_upload,
                },
            )
            doc = existing
        else:
            doc = await create_document(
                db, entity_id,
                name=name,
                fs_path=saved_rel_path,
                file_url=saved_url,
                file_size=file_size,
                file_type=ext,
                mime_type=mime or "application/octet-stream",
                source="chat_upload",
                created_by=user_id,
                owner_id=user_id,
                visibility=Visibility.WORKSPACE,
                metadata=merge_document_metadata(
                    None,
                    origin=origin,
                    extra={
                        "chat_upload": chat_upload,
                    },
                ),
                emit_created_event=False,
            )

        # No group or membership is created until authorization and storage
        # admission succeed. Existing live documents remain idempotent at quota.
        group = await _ensure_workspace_upload_group(
            db, entity_id=entity_id, workspace_id=workspace_id,
        )
        if group is None:
            raise HTTPException(409, "Workspace upload collection is unavailable; retry")

        member = (await db.execute(
            select(DocumentGroupMember).where(
                DocumentGroupMember.document_id == doc.id,
                DocumentGroupMember.group_id == group.id,
            ).limit(1)
        )).scalar_one_or_none()
        if not member:
            db.add(DocumentGroupMember(document_id=doc.id, group_id=group.id))
            await db.flush()

        await bump_tool_cache_version(entity_id, "documents")
        return doc.id


def _context_heading(kind: str, name: str, reference: str = "") -> str:
    heading = f"{kind}: {name}"
    if reference:
        heading += f" | {reference}"
    return f"[{heading}]"


@dataclass
class FileAttachments:
    text_context: str = ""
    """``<attached_files>``-style text block for non-image attachments
    and KB documents. Empty when nothing extracted."""

    image_blocks: list[dict] = field(default_factory=list)
    """List of OpenAI-format ``image_url`` content blocks the caller
    can splice into a multimodal user message:
        {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}
    """

    image_urls: list[str] = field(default_factory=list)
    """Stable local URLs (``/api/v1/fs/...``) for each attached image.
    Tools like ``generate_video`` can reference these in ``reference_urls``
    or ``first_frame_url`` since images are persisted to the entity FS."""

    video_urls: list[str] = field(default_factory=list)
    """Stable local URLs for video attachments/documents.

    Video-generation tools can pass these through ``reference_video_urls``
    when the user selects an all-reference video workflow.
    """

    audio_urls: list[str] = field(default_factory=list)
    """Stable local URLs for audio attachments/documents.

    Video-generation tools can pass these through ``audio_reference_urls``
    and audio tools can reuse them as source/reference stems.
    """

    image_reference_lines: list[str] = field(default_factory=list)
    """Exact inline reference lines added to ``text_context`` for images.
    Persist these in chat history so follow-up turns can recover the URL."""

    unread_filenames: list[str] = field(default_factory=list)
    """Files we received but couldn't fully inject (unsupported type, too
    large, decode failed, or intentionally truncated). Already merged into
    ``text_context`` so the LLM can acknowledge them; exposed for telemetry /
    UI hints too."""

    attachment_refs: list[dict[str, Any]] = field(default_factory=list)
    """Structured attachment references for RuntimeEnvelope trace/mounts.

    These refs are intentionally compact: prompt-visible content is still
    carried by ``text_context`` / ``image_blocks``, while Runtime metadata gets
    stable paths, URLs, document IDs, and MIME types for audit and policy.
    """

    def add_attachment_ref(self, **ref: Any) -> None:
        compact = _compact_attachment_ref(ref)
        if compact:
            self.attachment_refs.append(compact)

    def to_runtime_context(self) -> dict[str, Any]:
        if not self.attachment_refs and not self.text_context and not self.image_blocks:
            return {}
        return {
            "refs": list(self.attachment_refs),
            "counts": {
                "refs": len(self.attachment_refs),
                "image_blocks": len(self.image_blocks),
                "image_urls": len(self.image_urls),
                "unread": len(self.unread_filenames),
            },
            "has_text_context": bool(self.text_context),
            "has_multimodal_images": bool(self.image_blocks),
        }

    # Backwards-compat: legacy callers used ``str(file_context)`` /
    # ``if file_context:`` against the old return shape (a plain str).
    # Preserve both. Truthiness reflects "any text OR any image".
    def __str__(self) -> str:
        return self.text_context

    def __bool__(self) -> bool:
        return bool(self.text_context) or bool(self.image_blocks)


async def build_file_context(
    files: list[UploadFile],
    document_ids: list[str],
    entity_id: str,
    db: AsyncSession,
    *,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    surface: ChatSurface | None = None,
) -> FileAttachments:
    """Build attachments from uploaded files and/or KB document IDs.

    Returns ``FileAttachments`` — caller decides how to present
    text_context (always safe to inject as a string in the user
    message) vs image_blocks (only useful on vision-capable models).
    """
    out = FileAttachments()
    text_parts: list[str] = []
    inline_image_bytes = 0
    inline_text_chars = 0
    storage_scope = (
        f"{workspace_id}:{user_id}"
        if surface == ChatSurface.WORKSPACE_CHAT and workspace_id and user_id
        else None
    )

    def save_upload(content: bytes, filename: str, mime: str) -> tuple[str, str]:
        if storage_scope:
            return _save_chat_upload(
                content, filename, entity_id, mime, storage_scope=storage_scope,
            )
        return _save_chat_upload(content, filename, entity_id, mime)

    def append_text(kind: str, name: str, text: str, reference: str = "") -> None:
        """Append extracted text without letting attachments dominate prompt."""
        nonlocal inline_text_chars
        clean = (text or "").strip()
        if not clean:
            return
        remaining = max(0, _MAX_INLINE_TEXT_TOTAL_CHARS - inline_text_chars)
        budget = min(_MAX_INLINE_TEXT_CHARS, remaining)
        heading = _context_heading(kind, name, reference)
        if len(clean) <= budget:
            text_parts.append(f"{heading}\n{clean}")
            inline_text_chars += len(clean)
            return

        preview = clean[:budget].rstrip() if budget > 0 else ""
        omitted = len(clean) - len(preview)
        note = (
            f"[Context truncated: omitted {omitted} characters to stay within the chat "
            "token budget. Use the referenced file/document path with read_file, search, "
            "or a document-specific tool if more detail is needed.]"
        )
        body = f"{preview}\n\n{note}" if preview else note
        text_parts.append(f"{heading}\n{body}")
        inline_text_chars += len(preview)
        out.unread_filenames.append(f"{name} (truncated for context budget)")

    for f in files:
        content = await f.read()
        if not content:
            out.unread_filenames.append(f.filename or "(unnamed)")
            continue

        from packages.core.services.upload_security import UploadSecurityError, inspect_upload_content
        try:
            mime = await inspect_upload_content(
                content,
                filename=f.filename,
                declared_content_type=f.content_type,
            )
        except UploadSecurityError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

        # Image branch — pass through as multimodal block.
        if mime.startswith(_IMAGE_MIME_PREFIX):
            # Location and camera metadata are removed before the bytes are
            # saved or inlined for the model, so neither copy carries them.
            try:
                stripped = await asyncio.to_thread(strip_image_metadata, content, media_type=mime)
            except ImageMetadataStripError as exc:
                raise HTTPException(
                    exc.status_code, detail={"code": exc.code, "message": str(exc)},
                ) from exc
            if stripped is not None:
                content = stripped
            # Detect actual format from magic bytes — browsers sometimes
            # send the wrong content_type (e.g. jpeg for a PNG file).
            mime = _detect_image_mime(content, mime)
            if storage_scope:
                local_url = _save_chat_image(
                    content, f.filename or "image.png", entity_id, mime,
                    storage_scope=storage_scope,
                )
            else:
                local_url = _save_chat_image(content, f.filename or "image.png", entity_id, mime)
            saved_rel_path = _fs_path_from_local_url(entity_id, local_url)
            if workspace_id and saved_rel_path:
                await _register_workspace_chat_upload(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    filename=f.filename or "image.png",
                    surface=surface,
                    saved_rel_path=saved_rel_path,
                    saved_url=local_url,
                    file_size=len(content),
                    mime=mime,
                )
            out.image_urls.append(local_url)
            out.add_attachment_ref(
                kind="chat_upload",
                name=f.filename or "image.png",
                mime=mime,
                path=saved_rel_path,
                url=local_url,
                image=True,
            )
            image_reference_line = f"[Image: {f.filename or 'attached image'} → {local_url}]"
            out.image_reference_lines.append(image_reference_line)
            text_parts.append(image_reference_line)

            inline_image = _prepare_inline_image(content, mime)
            if (
                inline_image is None
                or inline_image_bytes + len(inline_image[0]) > _MAX_INLINE_IMAGE_TOTAL_BYTES
            ):
                logger.info(
                    "image attachment %s saved but not inlined: size=%d total_before=%d",
                    f.filename, len(content), inline_image_bytes,
                )
                out.unread_filenames.append(
                    f"{f.filename or 'image'} (saved as {local_url}; not inlined for context budget)"
                )
                continue
            inline_bytes, inline_mime = inline_image
            b64 = base64.b64encode(inline_bytes).decode("ascii")
            data_url = f"data:{inline_mime};base64,{b64}"
            out.image_blocks.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
            inline_image_bytes += len(inline_bytes)
            continue

        # Video branch — save as a structured reference. We intentionally do
        # not transcribe arbitrary uploaded videos here; video mode may need
        # the actual clip bytes as reference media.
        if mime.startswith(_VIDEO_MIME_PREFIX):
            saved_rel_path, saved_url = save_upload(
                content,
                f.filename or "video",
                mime or "video/mp4",
            )
            out.video_urls.append(saved_url)
            out.add_attachment_ref(
                kind="chat_upload",
                name=f.filename or "video",
                mime=mime or "video/mp4",
                path=saved_rel_path,
                url=saved_url,
                video=True,
            )
            reference_line = f"[Video: {f.filename or 'attached video'} → {saved_url}]"
            text_parts.append(reference_line)
            if workspace_id:
                await _register_workspace_chat_upload(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    filename=f.filename or "video",
                    surface=surface,
                    saved_rel_path=saved_rel_path,
                    saved_url=saved_url,
                    file_size=len(content),
                    mime=mime or "video/mp4",
                )
            continue

        # Audio branch — save as a structured reference, then transcribe via
        # Whisper so ordinary chat still sees the speech content.
        if any(mime.startswith(p) for p in _AUDIO_MIME_PREFIXES):
            saved_rel_path, saved_url = save_upload(
                content,
                f.filename or "audio",
                mime or "audio/webm",
            )
            out.audio_urls.append(saved_url)
            out.add_attachment_ref(
                kind="chat_upload",
                name=f.filename or "audio",
                mime=mime or "audio/webm",
                path=saved_rel_path,
                url=saved_url,
                audio=True,
            )
            reference_line = f"[Audio: {f.filename or 'attached audio'} → {saved_url}]"
            text_parts.append(reference_line)
            if workspace_id:
                await _register_workspace_chat_upload(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    filename=f.filename or "audio",
                    surface=surface,
                    saved_rel_path=saved_rel_path,
                    saved_url=saved_url,
                    file_size=len(content),
                    mime=mime or "audio/webm",
                )
            from packages.core.services.voice.whisper import (
                WhisperError,
                transcribe_blob,
                whisper_cost_usd,
            )
            from packages.core.services.usage_service import record_media_usage

            user_key = None
            stt_model = None
            try:
                from packages.core.services.model_resolver import (
                    resolve_llm_metadata_for_user,
                    resolve_model_for_user,
                )
                stt_model = await resolve_model_for_user(
                    "stt", user_id=user_id, entity_id=entity_id, db=db, workspace_id=workspace_id,
                )
                metadata = await resolve_llm_metadata_for_user(
                    "stt",
                    user_id=user_id,
                    entity_id=entity_id,
                    db=db,
                    workspace_id=workspace_id,
                )
                user_key = (metadata or {}).get("llm_api_key")
            except Exception:
                pass

            try:
                if not user_key:
                    await runtime_assert_credit_available(
                        entity_id,
                        source=RUNTIME_CHAT_ATTACHMENT_VOICE_SOURCE,
                        workspace_id=workspace_id,
                    )
                result = await transcribe_blob(
                    content,
                    mime=mime or "audio/webm",
                    filename=f.filename or "audio.webm",
                    user_api_key=user_key,
                    resolved_model=stt_model,
                )
            except WhisperError as exc:
                logger.info("audio attachment %s transcription failed: %s", f.filename, exc)
                out.unread_filenames.append(
                    f"{f.filename or 'audio'} ({exc})"
                )
                continue

            transcript = (result.text or "").strip()
            if transcript:
                append_text("Audio", f.filename or "voice clip", transcript)
            else:
                # Whisper returned empty — silence or undetectable speech.
                out.unread_filenames.append(
                    f"{f.filename or 'audio'} (no speech detected)"
                )

            # Bill against the workspace entity. Best-effort.
            try:
                await record_media_usage(
                    db,
                    entity_id=entity_id,
                    kind="whisper",
                    model=result.model,
                    cost_usd=whisper_cost_usd(result.duration_seconds, result.model, getattr(result, "usage", None)),
                    units=int(result.duration_seconds),
                    workspace_id=workspace_id,
                    user_id=user_id,
                    source=RUNTIME_CHAT_ATTACHMENT_VOICE_SOURCE,
                    byok=bool(user_key),
                )
                await db.commit()
            except Exception:
                logger.debug("whisper attachment billing failed (best-effort)", exc_info=True)
            continue

        # Text-extractable branch — persist for later chunked reads, then extract
        # a bounded preview for the immediate prompt.
        saved_rel_path, saved_url = save_upload(
            content,
            f.filename or "attachment",
            mime or "application/octet-stream",
        )
        workspace_doc_id = await _register_workspace_chat_upload(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            filename=f.filename or "attachment",
            surface=surface,
            saved_rel_path=saved_rel_path,
            saved_url=saved_url,
            file_size=len(content),
            mime=mime or "application/octet-stream",
        )
        reference_parts = [
            f"path={saved_rel_path}",
            f"url={saved_url}",
            f"sandbox_path=/workspace/{saved_rel_path} (only inside explicit skill sandboxes)",
        ]
        if workspace_doc_id:
            reference_parts.append(f"workspace_document_id={workspace_doc_id}")
        reference = "; ".join(reference_parts)
        out.add_attachment_ref(
            kind="chat_upload",
            name=f.filename or "attachment",
            mime=mime or "application/octet-stream",
            path=saved_rel_path,
            url=saved_url,
            document_id=workspace_doc_id,
            text=True,
        )
        suffix = os.path.splitext(f.filename or "")[1] or ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            text = await extract_text(
                tmp_path, mime_type=mime, file_type=suffix.lstrip("."),
            )
        finally:
            os.unlink(tmp_path)
        if text:
            append_text(
                "File",
                f.filename or saved_rel_path,
                f"{_ATTACHMENT_TEXT_READY_NOTE}\n\n{text}",
                reference=reference,
            )
        else:
            # Extraction returned empty — usually means unsupported type
            # (audio, archive, exotic doc) or a missing extractor lib.
            # Surface so the LLM tells the user we got the file but
            # couldn't read it instead of silently ignoring.
            text_parts.append(
                f"{_context_heading('File', f.filename or saved_rel_path, reference)}\n"
                "The file was uploaded and saved, but automatic text extraction returned no readable text. "
                "Do not search the host filesystem or invoke a sandbox just to locate this file; "
                "ask the user for a different format if needed."
            )
            out.unread_filenames.append(
                f"{f.filename or '(unnamed)'} (saved as {saved_rel_path}; text extraction failed)"
            )

    # KB documents — resolve through document ACL before injecting content into chat.
    docs_by_id: dict = {}
    if document_ids:
        from packages.core.services.document_access import (
            document_is_public_agent_visible,
            get_visible_document,
        )
        from packages.core.services.document_service import get_document

        for document_id in document_ids:
            if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT:
                # Public entrypoints never inherit legacy user-less access or
                # a signed-in owner's broader document permissions.
                document = await get_document(db, document_id, entity_id)
                if not await document_is_public_agent_visible(
                    db, document, entity_id=entity_id, workspace_id=workspace_id,
                ):
                    continue
            else:
                document = await get_visible_document(
                    db,
                    document_id,
                    entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
            if document:
                docs_by_id[document.id] = document

    fs_root = os.getenv("MANOR_FS_ROOT", "/mnt/manor")
    for doc_id in document_ids:
        doc = docs_by_id.get(doc_id)
        if not doc:
            continue

        # KB image documents — inject as multimodal block + provide stable URL.
        doc_mime = (doc.mime_type or "").lower()
        if _document_is_video(doc, doc_mime):
            if doc.fs_path:
                kb_url = entity_fs_open_url(entity_id, doc.fs_path)
                out.video_urls.append(kb_url)
                out.add_attachment_ref(
                    kind="knowledge_document",
                    name=doc.name,
                    mime=doc.mime_type,
                    path=doc.fs_path,
                    url=kb_url,
                    document_id=doc.id,
                    video=True,
                )
                text_parts.append(f"[Video from KB: {doc.name} → {kb_url}]")
                text_parts.extend(_kb_media_description(doc))
                continue
            if doc.file_url:
                out.video_urls.append(doc.file_url)
                out.add_attachment_ref(
                    kind="knowledge_document",
                    name=doc.name,
                    mime=doc.mime_type,
                    url=doc.file_url,
                    document_id=doc.id,
                    video=True,
                )
                text_parts.append(f"[Video from KB: {doc.name} → {doc.file_url}]")
                continue

        if _document_is_audio(doc, doc_mime):
            if doc.fs_path:
                kb_url = entity_fs_open_url(entity_id, doc.fs_path)
                out.audio_urls.append(kb_url)
                out.add_attachment_ref(
                    kind="knowledge_document",
                    name=doc.name,
                    mime=doc.mime_type,
                    path=doc.fs_path,
                    url=kb_url,
                    document_id=doc.id,
                    audio=True,
                )
                text_parts.append(f"[Audio from KB: {doc.name} → {kb_url}]")
                text_parts.extend(_kb_media_description(doc))
                continue
            if doc.file_url:
                out.audio_urls.append(doc.file_url)
                out.add_attachment_ref(
                    kind="knowledge_document",
                    name=doc.name,
                    mime=doc.mime_type,
                    url=doc.file_url,
                    document_id=doc.id,
                    audio=True,
                )
                text_parts.append(f"[Audio from KB: {doc.name} → {doc.file_url}]")
                continue

        if _document_is_image(doc, doc_mime):
            if doc.fs_path:
                kb_url = entity_fs_open_url(entity_id, doc.fs_path)
                out.image_urls.append(kb_url)
                out.add_attachment_ref(
                    kind="knowledge_document",
                    name=doc.name,
                    mime=doc.mime_type,
                    path=doc.fs_path,
                    url=kb_url,
                    document_id=doc.id,
                    image=True,
                )
                image_reference_line = f"[Image from KB: {doc.name} → {kb_url}]"
                out.image_reference_lines.append(image_reference_line)
                text_parts.append(image_reference_line)
                full_path = os.path.join(fs_root, entity_id, doc.fs_path)
                if os.path.isfile(full_path):
                    with open(full_path, "rb") as _img_f:
                        img_bytes = _img_f.read()
                    inline_image = _prepare_inline_image(img_bytes, doc_mime)
                    if inline_image and inline_image_bytes + len(inline_image[0]) <= _MAX_INLINE_IMAGE_TOTAL_BYTES:
                        # Detect actual format — stored mime_type may be wrong.
                        inline_bytes, actual_mime = inline_image
                        img_b64 = base64.b64encode(inline_bytes).decode("ascii")
                        out.image_blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{actual_mime};base64,{img_b64}"},
                        })
                        inline_image_bytes += len(inline_bytes)
                    else:
                        out.unread_filenames.append(
                            f"KB: {doc.name} (reference URL provided; image not inlined for context budget)"
                        )
                    continue
                out.unread_filenames.append(
                    f"KB: {doc.name} (reference URL provided; image bytes not found locally)"
                )
                continue
            if doc.file_url:
                out.image_urls.append(doc.file_url)
                out.image_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": doc.file_url},
                })
                out.add_attachment_ref(
                    kind="knowledge_document",
                    name=doc.name,
                    mime=doc.mime_type,
                    url=doc.file_url,
                    document_id=doc.id,
                    image=True,
                )
                image_reference_line = f"[Image from KB: {doc.name} → {doc.file_url}]"
                out.image_reference_lines.append(image_reference_line)
                text_parts.append(image_reference_line)
                continue

        kb_url = entity_fs_open_url(entity_id, doc.fs_path) if doc.fs_path else doc.file_url
        reference_parts = [f"document_id={doc.id}"]
        if doc.fs_path:
            reference_parts.append(f"path={doc.fs_path}")
        if kb_url:
            reference_parts.append(f"url={kb_url}")
        out.add_attachment_ref(
            kind="knowledge_document",
            name=doc.name,
            mime=doc.mime_type,
            path=doc.fs_path,
            url=kb_url,
            document_id=doc.id,
            text=True,
        )
        text = ""
        if doc.fs_path:
            full_path = os.path.join(fs_root, entity_id, doc.fs_path)
            text = await extract_text(
                full_path, mime_type=doc.mime_type, file_type=doc.file_type,
            )
        if not text and doc.metadata_:
            for key in ("content", "content_text"):
                value = doc.metadata_.get(key)
                if isinstance(value, str):
                    text = value[:100_000]
                    break
        if text:
            append_text("Document", doc.name, text, reference="; ".join(reference_parts))
        else:
            out.unread_filenames.append(f"KB: {doc.name}")

    if out.unread_filenames:
        text_parts.append(
            "[Attachment notes: " + ", ".join(out.unread_filenames) + "]"
        )

    out.text_context = "\n\n---\n\n".join(text_parts)
    return out
