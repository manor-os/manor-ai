"""Repair DB documents whose filesystem payload is missing."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import case, select

from packages.core.database import async_session
from packages.core.models.document import Document, VectorStatus
from packages.core.models.media_job import MediaJob
from packages.core.services.entity_fs import (
    EntityFilesystemError,
    assert_entity_filesystem_ready,
    is_fs_enabled,
    resolve_path,
    write_entity_file_atomic,
)

logger = logging.getLogger(__name__)
_STALE_FILE_INTEGRITY_STATUSES = frozenset({"missing", "invalid_path", "unavailable", "error"})


@dataclass
class DocumentFileRepairReport:
    checked: int = 0
    missing: int = 0
    restored: int = 0
    requeued: int = 0
    marked_failed: int = 0
    marked_missing: int = 0
    skipped: int = 0
    errors: int = 0
    limited: bool = False
    filesystem_unavailable: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "checked": self.checked,
            "missing": self.missing,
            "restored": self.restored,
            "requeued": self.requeued,
            "marked_failed": self.marked_failed,
            "marked_missing": self.marked_missing,
            "skipped": self.skipped,
            "errors": self.errors,
            "limited": self.limited,
            "filesystem_unavailable": self.filesystem_unavailable,
        }


async def repair_missing_document_files(
    *,
    entity_id: str | None = None,
    limit: int = 200,
    dry_run: bool = False,
    mark_failed: bool = False,
    heal_existing_only: bool = False,
) -> DocumentFileRepairReport:
    """Scan document rows and repair missing filesystem files when possible.

    A document is only considered healthy when its ``fs_path`` exists under the
    entity filesystem. Missing generated-media files are restored from the
    matching ``media_jobs.source_url`` when that provider URL is still
    available. Rows that cannot be restored record ``file_integrity`` as
    missing; vector status is only changed when ``mark_failed`` is explicitly
    enabled. When ``heal_existing_only`` is true, the scan only fixes stale DB
    missing markers for files that already exist and skips all missing files.
    """
    report = DocumentFileRepairReport()
    if not is_fs_enabled():
        return report
    try:
        fs_root = assert_entity_filesystem_ready()
    except EntityFilesystemError as exc:
        report.filesystem_unavailable = True
        logger.warning("Document file repair skipped: %s", exc)
        return report
    if not os.path.isdir(fs_root):
        report.filesystem_unavailable = True
        logger.warning("Document file repair skipped: filesystem root unavailable: %s", fs_root)
        return report

    async with async_session() as db:
        stale_file_integrity = (
            Document.metadata_["file_integrity"]["status"].astext.in_(
                sorted(_STALE_FILE_INTEGRITY_STATUSES)
            )
            | (Document.metadata_["file_integrity"]["recoverable"].astext == "false")
        )
        stmt = (
            select(Document)
            .where(
                Document.fs_path.is_not(None),
                Document.is_trashed == False,  # noqa: E712
            )
            .order_by(
                case((stale_file_integrity, 0), else_=1),
                Document.updated_at.desc(),
            )
            .limit(limit + 1)
        )
        if entity_id:
            stmt = stmt.where(Document.entity_id == entity_id)
        docs = list((await db.execute(stmt)).scalars().all())
        if len(docs) > limit:
            report.limited = True
            docs = docs[:limit]

        for doc in docs:
            report.checked += 1
            try:
                await _repair_document_if_missing(
                    db,
                    doc,
                    report,
                    dry_run=dry_run,
                    mark_failed=mark_failed,
                    heal_existing_only=heal_existing_only,
                )
            except Exception as exc:  # noqa: BLE001
                report.errors += 1
                logger.warning(
                    "Document file repair failed: entity=%s doc=%s path=%s error=%s",
                    doc.entity_id,
                    doc.id,
                    doc.fs_path,
                    exc,
                    exc_info=True,
                )
        if not dry_run:
            await db.commit()
    return report


async def _repair_document_if_missing(
    db,
    doc: Document,
    report: DocumentFileRepairReport,
    *,
    dry_run: bool,
    mark_failed: bool = False,
    heal_existing_only: bool = False,
) -> None:
    rel_path = str(doc.fs_path or "").lstrip("/")
    if not rel_path:
        report.skipped += 1
        return
    abs_path = resolve_path(doc.entity_id, rel_path)
    if abs_path is None:
        if heal_existing_only:
            report.skipped += 1
            return
        report.missing += 1
        if not dry_run:
            doc.metadata_ = _with_file_integrity(doc.metadata_, status="invalid_path")
            if mark_failed:
                doc.vector_status = VectorStatus.FAILED
        if mark_failed:
            report.marked_failed += 1
        else:
            report.marked_missing += 1
        return
    if os.path.isfile(abs_path):
        if not dry_run and _has_stale_file_integrity(doc.metadata_):
            doc.metadata_ = _with_file_integrity(doc.metadata_, status="ok", resolved_from="filesystem")
            if doc.vector_status in {VectorStatus.FAILED, VectorStatus.SKIPPED}:
                doc.vector_status = VectorStatus.PENDING
                report.requeued += 1
        return
    if heal_existing_only:
        report.skipped += 1
        return
    if doc.file_url:
        report.skipped += 1
        return

    report.missing += 1
    job = await _find_recoverable_media_job(db, doc)
    if job and job.source_url:
        try:
            data = await _download_repair_source(job.source_url)
            expected = int(job.file_size or 0) or len(data)
            if dry_run:
                report.restored += 1
                return
            path = write_entity_file_atomic(
                doc.entity_id,
                rel_path,
                data,
                expected_size=expected,
                allow_empty=False,
            )
            doc.file_size = os.path.getsize(path)
            doc.metadata_ = _with_file_integrity(
                doc.metadata_,
                status="ok",
                restored_from_media_job_id=job.id,
            )
            if doc.vector_status in {VectorStatus.FAILED, VectorStatus.SKIPPED}:
                doc.vector_status = VectorStatus.PENDING
            report.restored += 1
            return
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Document file repair could not restore doc=%s from media job=%s: %s",
                doc.id,
                job.id,
                exc,
            )

    if dry_run:
        if mark_failed:
            report.marked_failed += 1
        else:
            report.marked_missing += 1
        return
    doc.metadata_ = _with_file_integrity(
        doc.metadata_,
        status="missing",
        recoverable=bool(job and job.source_url),
    )
    if mark_failed and doc.vector_status != VectorStatus.FAILED:
        doc.vector_status = VectorStatus.FAILED
    if mark_failed:
        report.marked_failed += 1
    else:
        report.marked_missing += 1


async def _find_recoverable_media_job(db, doc: Document) -> MediaJob | None:
    result = await db.execute(
        select(MediaJob)
        .where(
            MediaJob.entity_id == doc.entity_id,
            MediaJob.status == "completed",
            MediaJob.source_url.is_not(None),
            MediaJob.source_url != "",
            MediaJob.params["result_document_id"].astext == doc.id,
        )
        .order_by(MediaJob.completed_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job:
        return job

    result = await db.execute(
        select(MediaJob)
        .where(
            MediaJob.entity_id == doc.entity_id,
            MediaJob.status == "completed",
            MediaJob.source_url.is_not(None),
            MediaJob.source_url != "",
            MediaJob.result_url.ilike(f"%/{doc.fs_path}"),
        )
        .order_by(MediaJob.completed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _download_repair_source(source_url: str) -> bytes:
    headers: dict[str, str] = {}
    host = urlparse(source_url).netloc.lower()

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(source_url, headers=headers)
        resp.raise_for_status()
        data = resp.content
    if not data:
        raise EntityFilesystemError("Provider repair source returned an empty file")
    return data


def _with_file_integrity(metadata: dict | None, **fields: object) -> dict:
    updated = dict(metadata or {})
    integrity = dict(updated.get("file_integrity") or {})
    integrity.update(fields)
    integrity["checked_at"] = datetime.now(timezone.utc).isoformat()
    if fields.get("status") == "ok":
        integrity.pop("recoverable", None)
        integrity.pop("error", None)
    updated["file_integrity"] = integrity
    return updated


def _has_stale_file_integrity(metadata: dict | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    integrity = metadata.get("file_integrity")
    if not isinstance(integrity, dict):
        return False
    status = str(integrity.get("status") or "").lower()
    return status in _STALE_FILE_INTEGRITY_STATUSES or integrity.get("recoverable") is False
