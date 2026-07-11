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

import base64
import hashlib
import io
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    RUNTIME_CHAT_ATTACHMENT_VOICE_SOURCE,
    runtime_assert_credit_available,
)
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workspace import Workspace
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.text_extraction import extract_text
from packages.core.services.tool_cache_version import bump_tool_cache_version

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
    for sig, mime in _IMAGE_SIGNATURES:
        if data[:len(sig)] == sig:
            # RIFF container could be non-webp; verify WEBP marker.
            if sig == b"RIFF":
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    return "image/webp"
                return declared
            return mime
    return declared


def _prepare_inline_image(data: bytes, declared_mime: str) -> tuple[bytes, str] | None:
    """Return image bytes small enough for multimodal prompt inlining.

    Large KB/generated images are common. Instead of dropping them from vision
    context, try to downscale/re-encode to JPEG under the inline cap while
    keeping the original saved URL in text context.
    """
    actual_mime = _detect_image_mime(data, declared_mime)
    if len(data) <= _MAX_INLINE_IMAGE_BYTES:
        return data, actual_mime
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        return None

    try:
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
_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif"}
_WORKSPACE_DEFAULT_COLLECTION_NAME = "Workspace Knowledge"
_WORKSPACE_GROUP_DEFAULT_KIND = "workspace_collection"
_ATTACHMENT_TEXT_READY_NOTE = (
    "Attachment text below was extracted by the backend for this turn. "
    "Use it directly for ordinary analysis, summarization, and Q&A; invoke a skill only "
    "for a skill-specific operation beyond reading this attachment."
)


def _safe_upload_ext(filename: str, mime: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if not ext and mime:
        ext = "." + mime.split(";", 1)[0].split("/")[-1].lower()
    ext = re.sub(r"[^a-z0-9.]", "", ext or ".bin")
    return ext if ext.startswith(".") else f".{ext}"


def _save_chat_upload(content: bytes, filename: str, entity_id: str, mime: str) -> tuple[str, str]:
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

    rel_path = f"uploads/chat/{safe_name}"
    filepath = os.path.join(get_entity_root(entity_id), rel_path)
    if not os.path.isfile(filepath) or os.path.getsize(filepath) != len(content):
        write_entity_file_atomic(
            entity_id,
            rel_path,
            content,
            expected_size=len(content),
            allow_empty=True,
        )
    return rel_path, f"/api/v1/fs/{entity_id}/{rel_path}"


def _save_chat_image(content: bytes, filename: str, entity_id: str, mime: str) -> str:
    """Persist an attached image and return a stable local URL."""
    _, local_url = _save_chat_upload(content, filename, entity_id, mime)
    return local_url


def _fs_path_from_local_url(entity_id: str, local_url: str) -> str | None:
    prefix = f"/api/v1/fs/{entity_id}/"
    if not local_url.startswith(prefix):
        return None
    return local_url[len(prefix):].strip("/") or None


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


async def _register_workspace_chat_upload(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
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
    if not workspace_id:
        return None

    group = await _ensure_workspace_upload_group(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    if not group:
        return None

    name = os.path.basename(filename or saved_rel_path) or "attachment"
    ext = os.path.splitext(name)[1].lstrip(".").lower() or None
    if ext and len(ext) > 20:
        ext = ext[:20]
    existing = (await db.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.fs_path == saved_rel_path,
        ).limit(1)
    )).scalar_one_or_none()

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
        existing.name = name
        existing.file_url = saved_url
        existing.file_size = file_size
        existing.file_type = ext
        existing.mime_type = mime or existing.mime_type
        existing.source = existing.source or "chat_upload"
        existing.created_by = existing.created_by or user_id
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
        doc = Document(
            entity_id=entity_id,
            name=name,
            fs_path=saved_rel_path,
            file_url=saved_url,
            file_size=file_size,
            file_type=ext,
            mime_type=mime or "application/octet-stream",
            source="chat_upload",
            created_by=user_id,
            metadata_=merge_document_metadata(
                None,
                origin=origin,
                extra={
                    "chat_upload": chat_upload,
                },
            ),
        )
        db.add(doc)
        await db.flush()

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

        mime = (f.content_type or "").lower()

        # Image branch — pass through as multimodal block.
        if mime.startswith(_IMAGE_MIME_PREFIX):
            # Detect actual format from magic bytes — browsers sometimes
            # send the wrong content_type (e.g. jpeg for a PNG file).
            mime = _detect_image_mime(content, mime)
            local_url = _save_chat_image(content, f.filename or "image.png", entity_id, mime)
            saved_rel_path = _fs_path_from_local_url(entity_id, local_url)
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
            saved_rel_path, saved_url = _save_chat_upload(
                content,
                f.filename or "video",
                entity_id,
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
                    saved_rel_path=saved_rel_path,
                    saved_url=saved_url,
                    file_size=len(content),
                    mime=mime or "video/mp4",
                )
            continue

        # Audio branch — save as a structured reference, then transcribe via
        # Whisper so ordinary chat still sees the speech content.
        if any(mime.startswith(p) for p in _AUDIO_MIME_PREFIXES):
            saved_rel_path, saved_url = _save_chat_upload(
                content,
                f.filename or "audio",
                entity_id,
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
                stt_model = await resolve_model_for_user("stt", user_id=user_id, entity_id=entity_id, db=db)
                metadata = await resolve_llm_metadata_for_user(
                    "stt",
                    user_id=user_id,
                    entity_id=entity_id,
                    db=db,
                )
                user_key = (metadata or {}).get("llm_api_key")
            except Exception:
                pass

            try:
                if not user_key:
                    await runtime_assert_credit_available(
                        entity_id,
                        source=RUNTIME_CHAT_ATTACHMENT_VOICE_SOURCE,
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
                    cost_usd=whisper_cost_usd(result.duration_seconds),
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
        saved_rel_path, saved_url = _save_chat_upload(
            content,
            f.filename or "attachment",
            entity_id,
            mime or "application/octet-stream",
        )
        workspace_doc_id = await _register_workspace_chat_upload(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            filename=f.filename or "attachment",
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
        from packages.core.services.document_access import get_visible_document

        for document_id in document_ids:
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
                kb_url = f"/api/v1/fs/{entity_id}/{doc.fs_path}"
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
                kb_url = f"/api/v1/fs/{entity_id}/{doc.fs_path}"
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
                kb_url = f"/api/v1/fs/{entity_id}/{doc.fs_path}"
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
            reference_parts = [f"document_id={doc.id}"]
            if doc.fs_path:
                reference_parts.append(f"path={doc.fs_path}")
            out.add_attachment_ref(
                kind="knowledge_document",
                name=doc.name,
                mime=doc.mime_type,
                path=doc.fs_path,
                url=doc.file_url,
                document_id=doc.id,
                text=True,
            )
            append_text("Document", doc.name, text, reference="; ".join(reference_parts))
        else:
            out.unread_filenames.append(f"KB: {doc.name}")

    if out.unread_filenames:
        text_parts.append(
            "[Attachment notes: " + ", ".join(out.unread_filenames) + "]"
        )

    out.text_context = "\n\n---\n\n".join(text_parts)
    return out
