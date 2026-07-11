"""Maintenance Celery tasks for runtime filesystem housekeeping."""
from __future__ import annotations

import logging
import os

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker as _run_async

logger = logging.getLogger(__name__)


@celery_app.task(name="maintenance.cleanup_chat_uploads")
def cleanup_chat_uploads() -> dict:
    """Delete expired hidden chat-upload inputs.

    This only touches ``uploads/chat`` runtime attachments. User-facing
    Knowledge files and generated media/documents are intentionally excluded.
    """
    async def _run() -> dict:
        from packages.core.services.chat_upload_cleanup import cleanup_expired_chat_uploads

        return await cleanup_expired_chat_uploads()

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintenance.cleanup_chat_uploads failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@celery_app.task(name="maintenance.repair_missing_document_files")
def repair_missing_document_files() -> dict:
    """Repair Knowledge rows whose backing filesystem file is missing."""
    repair_enabled = os.getenv("DOCUMENT_FILE_REPAIR_ENABLED", "true").lower() in {"true", "1", "yes"}
    stale_heal_enabled = os.getenv("DOCUMENT_FILE_STALE_HEAL_ENABLED", "true").lower() in {"true", "1", "yes"}
    if not repair_enabled and not stale_heal_enabled:
        return {"ok": True, "skipped": "document file repair and stale heal disabled"}

    async def _run() -> dict:
        from packages.core.services.document_file_repair import repair_missing_document_files

        limit = _env_int("DOCUMENT_FILE_REPAIR_LIMIT", 200)
        if repair_enabled:
            mark_failed = os.getenv("DOCUMENT_FILE_REPAIR_MARK_FAILED", "false").lower() in {"true", "1", "yes"}
            report = await repair_missing_document_files(limit=limit, mark_failed=mark_failed)
            return {"ok": True, "mode": "repair", **report.to_dict()}

        report = await repair_missing_document_files(limit=limit, heal_existing_only=True)
        return {"ok": True, "mode": "safe_stale_heal", **report.to_dict()}

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintenance.repair_missing_document_files failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
