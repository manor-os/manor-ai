"""Maintenance Celery tasks for runtime filesystem housekeeping."""
from __future__ import annotations

import logging
import os

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker as _run_async

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="calendar.reconcile_booking_metadata",
    max_retries=5,
)
def reconcile_booking_metadata_task(self, owner_id: str, booking_id: str) -> dict:
    """Recover provider event metadata after a booking-side persistence failure."""
    async def _run() -> dict:
        from apps.api.routers.calendar_settings import reconcile_booking_metadata
        from packages.core.database import async_session

        async with async_session() as db:
            reconciled = await reconcile_booking_metadata(db, owner_id, booking_id)
        return {"ok": reconciled, "booking_id": booking_id}

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        countdown = min(300, 10 * (2 ** self.request.retries))
        raise self.retry(exc=exc, countdown=countdown)


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


@celery_app.task(name="maintenance.cleanup_document_upload_recovery")
def cleanup_document_upload_recovery() -> dict:
    """Remove expired browser-upload intents and unreferenced source bytes."""

    async def _run() -> dict:
        from apps.api.routers.documents import (
            cleanup_expired_document_upload_recovery_intents,
        )

        report = await cleanup_expired_document_upload_recovery_intents(
            limit=_env_int("DOCUMENT_UPLOAD_RECOVERY_CLEANUP_LIMIT", 200),
        )
        return {"ok": True, **report}

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintenance.cleanup_document_upload_recovery failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@celery_app.task(name="maintenance.cleanup_ai_edit_sessions")
def cleanup_ai_edit_sessions() -> dict:
    """Delete inactive host-owned AI Edit conversations after their TTL."""

    async def _run() -> dict:
        from packages.core.database import async_session
        from packages.core.services.conversation_lifecycle import (
            cleanup_expired_ai_edit_conversations,
        )
        from packages.core.services.runtime_run_service import (
            cancel_runtime_run_resources,
        )

        cancelled_runs = []
        async with async_session() as db:
            deleted = await cleanup_expired_ai_edit_conversations(
                db,
                cancelled_runtime_runs=cancelled_runs,
            )
            await db.commit()
        for run in cancelled_runs:
            await cancel_runtime_run_resources(run)
        return {"ok": True, "deleted": deleted}

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintenance.cleanup_ai_edit_sessions failed: %s", exc)
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


@celery_app.task(name="maintenance.sync_openrouter_pricing")
def sync_openrouter_pricing() -> dict:
    """Refresh the local OpenRouter model pricing cache."""

    async def _run() -> dict:
        from packages.core.services.openrouter_pricing_sync import sync_openrouter_pricing_cache

        return await sync_openrouter_pricing_cache(timeout_s=45.0)

    try:
        res = _run_async(_run())
        logger.info(
            "maintenance.sync_openrouter_pricing: synced %s models -> %s",
            res.get("count"),
            res.get("path"),
        )
        return {"ok": True, **res}
    except Exception as exc:  # noqa: BLE001
        logger.warning("maintenance.sync_openrouter_pricing failed: %s", exc)
        return {"ok": False, "error": str(exc)}




def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
