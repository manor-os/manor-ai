"""Versioned display variants of Knowledge media, served by ``manor-media``.

``generate_display_variants`` runs on the heavy worker after a write (and
when ``GET /documents/{id}/thumbnail`` finds an unrecorded variant): it names
the source's content version, writes ``{entity}/.manor-media/{doc}/{version}/
thumb.jpg`` once, and records ``{version, variants}`` in
``Document.metadata_["display_variants"]`` in a short transaction. Signed URLs
are only issued for what is recorded (``media_urls.variant_version``).

Only the ``thumb`` variant exists in P1 (docs/design/document-media-delivery-zh.md §7).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from packages.core.services.media_urls import (
    _IMAGE_EXTENSIONS,
    _VIDEO_EXTENSIONS,
    DISPLAY_VARIANTS_KEY,
    MEDIA_DIR,
    content_version,
    media_rel_path,
    media_signing_enabled,
    recorded_variants,
    sha256_file,
    thumbnail_source_kind,
    with_recorded_variants,
)

logger = logging.getLogger(__name__)

THUMB_MAX_PIXELS = 640
_VIDEO_TIMEOUT_SECONDS = 60
BACKFILL_BATCH_SIZE = 20


@contextmanager
def _write_variant_atomically(target_path: str, suffix: str = ".tmp") -> Iterator[str]:
    # API requests and workers can publish the same variant at the same time.
    temp_path = f"{target_path}.{uuid4().hex}{suffix}"
    try:
        yield temp_path
        os.replace(temp_path, target_path)
    finally:
        with suppress(OSError):
            os.remove(temp_path)


def render_image_thumbnail(source_path: str, target_path: str) -> None:
    """A ≤640 px JPEG of an image, written atomically (the API's thumbnail too)."""
    from PIL import Image, ImageOps

    with _write_variant_atomically(target_path) as temp_path:
        with Image.open(source_path) as opened:
            # A JPEG decodes straight at a fraction of its size; others decode once, then shrink.
            opened.draft("RGB", (THUMB_MAX_PIXELS * 2, THUMB_MAX_PIXELS * 2))
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((THUMB_MAX_PIXELS, THUMB_MAX_PIXELS))
            if image.mode not in {"RGB", "L"}:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            image.save(temp_path, format="JPEG", quality=82, optimize=True)


def video_thumbnail_command(source_path: str, target_path: str) -> list[str]:
    """ffmpeg arguments for the first-frame poster used by every video thumbnail."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", "0.5", "-i", source_path,
        "-frames:v", "1",
        "-vf", f"scale={THUMB_MAX_PIXELS}:-2:force_original_aspect_ratio=decrease",
        "-q:v", "4",
        target_path,
    ]


async def render_video_thumbnail(source_path: str, target_path: str) -> None:
    with _write_variant_atomically(target_path, suffix=".tmp.jpg") as temp_path:
        proc = await asyncio.create_subprocess_exec(
            *video_thumbnail_command(source_path, temp_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_VIDEO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError("video thumbnail timed out")
        if proc.returncode != 0 or not os.path.isfile(temp_path) or os.path.getsize(temp_path) == 0:
            raise RuntimeError(stderr.decode("utf-8", errors="ignore").strip()[:240] or "ffmpeg failed")


def variant_path(entity_root: str, document_id: str, version: str, variant: str = "thumb") -> str:
    return os.path.join(entity_root, media_rel_path(document_id, version, variant))


def needs_display_variants(document: Any) -> bool:
    """Whether the current version of this Document has no recorded ``thumb`` yet."""
    if not media_signing_enabled() or not getattr(document, "fs_path", None):
        return False
    if thumbnail_source_kind(document) is None:
        return False
    version = content_version(document)
    recorded_version, variants = recorded_variants(document)
    if version is not None and recorded_version == version and _render_failed(document):
        # This version could not be rendered; a new version tries again.
        return False
    return version is None or recorded_version != version or "thumb" not in variants


def _render_failed(document: Any) -> bool:
    record = (getattr(document, "metadata_", None) or {}).get(DISPLAY_VARIANTS_KEY)
    return isinstance(record, dict) and record.get("failed") is True


def schedule_display_variants(document: Any, *, check: bool = True) -> None:
    """Best-effort dispatch of committed work; ``/thumbnail`` re-dispatches a lost one."""
    if check and not needs_display_variants(document):
        return
    try:
        from packages.core.tasks.media_tasks import generate_display_variants_task

        generate_display_variants_task.delay(str(document.id))
    except Exception:
        logger.debug("display variant dispatch failed for %s", getattr(document, "id", None), exc_info=True)


_PENDING_KEY = "media_display_variant_ids"
_LISTENING_KEY = "media_display_variant_listening"


def schedule_display_variants_after_commit(db: Any, document: Any) -> None:
    """Dispatch once this write commits (a rollback drops it), whoever commits it.

    Write paths that hand their session to the caller (an upload commits after
    Knowledge sync returns) cannot dispatch themselves: the worker would read
    the row before it exists.
    """
    if not needs_display_variants(document):
        return
    from sqlalchemy import event

    session = getattr(db, "sync_session", db)
    # Listen for the session's lifetime, not once: a SAVEPOINT commit (an
    # upload's nested write) also fires after_commit and would use up a
    # one-shot listener before the outer commit that makes the row visible.
    if not session.info.get(_LISTENING_KEY):
        session.info[_LISTENING_KEY] = True
        event.listen(session, "after_commit", _dispatch_pending)
        event.listen(session, "after_rollback", _discard_pending)
    session.info.setdefault(_PENDING_KEY, set()).add(str(document.id))


def _dispatch_pending(sync_session: Any) -> None:
    if sync_session.in_nested_transaction():
        return
    for document_id in sync_session.info.pop(_PENDING_KEY, set()):
        schedule_display_variants(SimpleNamespace(id=document_id), check=False)


def _discard_pending(sync_session: Any) -> None:
    if not sync_session.in_nested_transaction():
        sync_session.info.pop(_PENDING_KEY, None)


def publish_variant_file(source_path: str, target_path: str) -> None:
    """Copy an already-rendered variant to its versioned path without a partial file."""
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with _write_variant_atomically(target_path) as temp_path:
        shutil.copyfile(source_path, temp_path)


def publish_variant_bytes(data: bytes, target_path: str) -> None:
    """Write already-rendered variant bytes to their versioned path without a partial file."""
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with _write_variant_atomically(target_path) as temp_path:
        with open(temp_path, "wb") as handle:
            handle.write(data)


def _prune_versions(entity_root: str, document_id: str, keep: set[str]) -> None:
    """Drop older version directories; keep the current one and the one before it."""
    base = os.path.join(entity_root, MEDIA_DIR, document_id)
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if name not in keep:
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)


def _source_version(document: Any, source_stat: os.stat_result) -> tuple[str | None, bool]:
    """(version, whether the digest must be computed) for the file as it is now.

    A stored digest wins. The size/path/mtime fallback is used only while it
    still matches the file on disk, so a larger video is not read twice.
    """
    if getattr(document, "content_sha256", None):
        return content_version(document), False
    integrity = (getattr(document, "metadata_", None) or {}).get("file_integrity") or {}
    if integrity.get("mtime_ns") == source_stat.st_mtime_ns and getattr(document, "file_size", None) == source_stat.st_size:
        return content_version(document), False
    return None, True


async def backfill_display_variants(limit: int = BACKFILL_BATCH_SIZE) -> int:
    """Queue a batch of Documents with no variant record; returns how many.

    A rendered, unreadable or missing file leaves a record, so the sweep runs dry.
    A random pick keeps Documents whose run keeps crashing from starving the rest.
    """
    from sqlalchemy import func, select

    from packages.core.database import create_worker_session
    from packages.core.models.document import Document

    if not media_signing_enabled():
        return 0
    async with create_worker_session()() as db:
        document_ids = (await db.execute(
            select(Document.id)
            .where(
                Document.is_trashed.is_(False),
                Document.fs_path.isnot(None),
                Document.quarantine_status == "clean",
                func.lower(Document.file_type).in_(_IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS),
                ~Document.metadata_.has_key(DISPLAY_VARIANTS_KEY),
            )
            .order_by(func.random())
            .limit(limit)
        )).scalars().all()
    for document_id in document_ids:
        schedule_display_variants(SimpleNamespace(id=document_id), check=False)
    return len(document_ids)


async def generate_display_variants(document_id: str) -> str:
    """Write and record the ``thumb`` variant for one Document; returns a status word."""
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.document import Document
    from packages.core.services.entity_fs import canonical_entity_root, resolve_path

    if not media_signing_enabled():
        return "disabled"
    async with create_worker_session()() as db:
        doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
        if doc is None or doc.is_trashed or not doc.fs_path or doc.quarantine_status != "clean":
            return "skipped"
        kind = thumbnail_source_kind(doc)
        if kind is None:
            return "skipped"
        snapshot = SimpleNamespace(
            id=doc.id, entity_id=doc.entity_id, fs_path=doc.fs_path, file_size=doc.file_size,
            content_sha256=doc.content_sha256, metadata_=dict(doc.metadata_ or {}),
        )
    # No transaction is held while the file is read or rendered.
    source_path = resolve_path(snapshot.entity_id, snapshot.fs_path)
    if not source_path or not os.path.isfile(source_path):
        # Ends the backfill's interest; a file that comes back is repaired by /thumbnail.
        await _record(document_id, snapshot, "", None, variants=[], failed=True)
        return "missing"
    before = os.stat(source_path)
    version, must_hash = _source_version(snapshot, before)
    computed_sha: str | None = None
    if must_hash:
        computed_sha = await asyncio.to_thread(sha256_file, source_path)
        after = os.stat(source_path)
        if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
            return "source_changed"
        version = content_version(SimpleNamespace(**{**vars(snapshot), "content_sha256": computed_sha}))
    if version is None:
        return "skipped"
    entity_root = canonical_entity_root(snapshot.entity_id)
    target = variant_path(entity_root, snapshot.id, version)
    if not (os.path.isfile(target) and os.path.getsize(target) > 0):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            if kind == "image":
                await asyncio.to_thread(render_image_thumbnail, source_path, target)
            else:
                await render_video_thumbnail(source_path, target)
        except Exception as exc:  # noqa: BLE001 - an unreadable file is not retried
            logger.info("display variant render failed for %s: %s", snapshot.id, exc)
            await _record(document_id, snapshot, version, computed_sha, variants=[], failed=True)
            return "render_failed"

    previous_version, _variants = recorded_variants(snapshot)
    if not await _record(document_id, snapshot, version, computed_sha, variants=["thumb"]):
        return "superseded"
    keep = {version} | ({previous_version} if previous_version else set())
    await asyncio.to_thread(_prune_versions, entity_root, snapshot.id, keep)
    return "ready"


async def _record(
    document_id: str, snapshot: Any, version: str, computed_sha: str | None, *, variants: list[str], failed: bool = False,
) -> bool:
    """Store what exists for ``version`` in one short transaction, unless the bytes moved on."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.database import create_worker_session
    from packages.core.models.document import Document

    async with create_worker_session()() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == document_id).with_for_update()
        )).scalar_one_or_none()
        # A newer write schedules its own run.
        if (
            doc is None or doc.is_trashed or doc.fs_path != snapshot.fs_path
            or doc.file_size != snapshot.file_size
            or (doc.content_sha256 or None) != (snapshot.content_sha256 or None)
        ):
            return False
        if failed:
            current_version, current_variants = recorded_variants(doc)
            if "thumb" in current_variants and current_version == content_version(doc):
                return False
        if computed_sha is not None:
            doc.content_sha256 = computed_sha
        metadata = with_recorded_variants(doc.metadata_, version=version, variants=variants)
        if failed:
            metadata[DISPLAY_VARIANTS_KEY]["failed"] = True
        doc.metadata_ = metadata
        # A thumbnail is not an edit: writing the column as it is keeps its onupdate
        # from showing a backfilled file as modified just now.
        flag_modified(doc, "updated_at")
        await db.commit()
    return True
