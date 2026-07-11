"""Cleanup for hidden chat attachment uploads.

Chat image attachments are saved under ``uploads/chat`` so media tools can
reuse them by minting short-lived provider URLs. They are runtime inputs, not
Knowledge documents, so this module only cleans that hidden folder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import time
from typing import Iterable
from urllib.parse import unquote, urlsplit

from packages.core.services.knowledge_visibility import normalize_rel_path

logger = logging.getLogger(__name__)

CHAT_UPLOAD_PREFIX = "uploads/chat/"
ACTIVE_MEDIA_STATUSES = ("pending", "processing")


@dataclass
class ChatUploadCleanupReport:
    entities_scanned: int = 0
    files_seen: int = 0
    files_deleted: int = 0
    bytes_deleted: int = 0
    skipped_recent: int = 0
    skipped_active: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "entities_scanned": self.entities_scanned,
            "files_seen": self.files_seen,
            "files_deleted": self.files_deleted,
            "bytes_deleted": self.bytes_deleted,
            "skipped_recent": self.skipped_recent,
            "skipped_active": self.skipped_active,
            "errors": self.errors,
        }


def media_param_upload_refs(entity_id: str, params: dict | None) -> set[str]:
    """Return ``uploads/chat/...`` refs from media job params for one entity."""
    refs: set[str] = set()
    if not isinstance(params, dict):
        return refs

    values: list[object] = [
        params.get("first_frame_url"),
        params.get("last_frame_url"),
        params.get("image_url"),
    ]
    ref_urls = params.get("reference_urls")
    if isinstance(ref_urls, list):
        values.extend(ref_urls)

    for value in values:
        rel = local_upload_rel_path(value, entity_id)
        if rel:
            refs.add(rel)
    return refs


def local_upload_rel_path(value: object, entity_id: str) -> str | None:
    """Extract a normalized ``uploads/chat/...`` path from a local FS URL."""
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    parsed = urlsplit(raw)
    path = parsed.path if parsed.scheme else raw
    path = unquote(path)

    if path.startswith("/api/v1/fs/"):
        parts = path.split("/", 5)
        if len(parts) < 6 or parts[4] != entity_id:
            return None
        rel = normalize_rel_path(parts[5])
    else:
        rel = normalize_rel_path(path)

    if rel.startswith(CHAT_UPLOAD_PREFIX):
        return rel
    return None


async def load_active_media_upload_refs() -> set[tuple[str, str]]:
    """Load chat-upload refs used by unfinished media jobs.

    These files are protected from cleanup even when old, because a stuck or
    slow provider job may still need to retry/fetch its inputs.
    """
    from packages.core.database import create_worker_session
    from packages.core.models.media_job import MediaJob
    from sqlalchemy import select

    refs: set[tuple[str, str]] = set()
    async with create_worker_session()() as db:
        result = await db.execute(
            select(MediaJob.entity_id, MediaJob.params).where(
                MediaJob.status.in_(ACTIVE_MEDIA_STATUSES)
            )
        )
        for entity_id, params in result.all():
            for rel in media_param_upload_refs(str(entity_id), params):
                refs.add((str(entity_id), rel))
    return refs


async def cleanup_expired_chat_uploads() -> dict:
    """Cleanup entry point for scheduled workers."""
    from packages.core.config import get_settings

    settings = get_settings()
    if not settings.MANOR_CHAT_UPLOAD_CLEANUP_ENABLED:
        return {"ok": True, "skipped": "disabled"}
    if not settings.MANOR_FS_ENABLED:
        return {"ok": True, "skipped": "fs_disabled"}

    retention_days = max(1, int(settings.MANOR_CHAT_UPLOAD_RETENTION_DAYS or 30))
    active_refs = await load_active_media_upload_refs()
    report = cleanup_chat_uploads_on_disk(
        settings.MANOR_FS_ROOT,
        retention_days=retention_days,
        active_refs=active_refs,
    )
    logger.info(
        "chat upload cleanup: entities=%d seen=%d deleted=%d bytes=%d active=%d errors=%d",
        report.entities_scanned,
        report.files_seen,
        report.files_deleted,
        report.bytes_deleted,
        report.skipped_active,
        len(report.errors),
    )
    return {"ok": not report.errors, "retention_days": retention_days, **report.to_dict()}


def cleanup_chat_uploads_on_disk(
    fs_root: str,
    *,
    retention_days: int,
    active_refs: Iterable[tuple[str, str]] = (),
    now: float | None = None,
) -> ChatUploadCleanupReport:
    """Delete expired files from every entity's hidden ``uploads/chat`` dir."""
    report = ChatUploadCleanupReport()
    root = os.path.abspath(fs_root)
    if not os.path.isdir(root):
        return report

    cutoff_ts = (time.time() if now is None else now) - (max(1, retention_days) * 86400)
    protected = {(entity_id, normalize_rel_path(rel)) for entity_id, rel in active_refs}

    for entry in os.scandir(root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        entity_id = entry.name
        entity_root = os.path.abspath(entry.path)
        upload_dir = os.path.join(entity_root, "uploads", "chat")
        if not os.path.isdir(upload_dir):
            continue

        report.entities_scanned += 1
        for dirpath, dirnames, filenames in os.walk(upload_dir, topdown=True, followlinks=False):
            dirnames[:] = [
                name for name in dirnames
                if not os.path.islink(os.path.join(dirpath, name))
            ]
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                rel_path = normalize_rel_path(os.path.relpath(full_path, entity_root))
                report.files_seen += 1

                if (entity_id, rel_path) in protected:
                    report.skipped_active += 1
                    continue

                try:
                    stat = os.stat(full_path, follow_symlinks=False)
                except OSError as exc:
                    report.errors.append(f"stat failed: {rel_path}: {exc}")
                    continue

                if stat.st_mtime >= cutoff_ts:
                    report.skipped_recent += 1
                    continue

                try:
                    os.remove(full_path)
                except OSError as exc:
                    report.errors.append(f"delete failed: {rel_path}: {exc}")
                    continue

                report.files_deleted += 1
                report.bytes_deleted += int(stat.st_size or 0)

        _remove_empty_dirs(upload_dir)

    return report


def _remove_empty_dirs(root: str) -> None:
    """Remove empty nested folders but keep ``uploads/chat`` itself."""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
        if os.path.abspath(dirpath) == os.path.abspath(root):
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
