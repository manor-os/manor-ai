"""One-time filesystem-to-Knowledge backfill.

This is intentionally additive and non-blocking: it never rewrites user files,
never deletes documents/folders, and only creates/updates the Knowledge
projection for paths allowed by the visibility policy.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text

from packages.core.config import get_settings
from packages.core.database import async_session
from packages.core.models.user import Entity
from packages.core.services.knowledge_sync import ensure_folder_path, sync_file_to_knowledge
from packages.core.services.knowledge_visibility import is_user_visible_folder_path, is_user_visible_path, normalize_rel_path

logger = logging.getLogger(__name__)

_BACKFILL_KEY = "knowledge_backfill_v1"
_LOCK_KEY = "manor:knowledge_backfill_v1"
_DEFAULT_LIMIT = 1000


@dataclass
class KnowledgeBackfillReport:
    entity_id: str
    files_synced: int = 0
    folders_synced: int = 0
    hidden_skipped: int = 0
    errors: int = 0
    limited: bool = False


async def backfill_entity_knowledge(
    entity_id: str,
    *,
    limit: int | None = _DEFAULT_LIMIT,
    mark_source: str = "startup_backfill",
) -> KnowledgeBackfillReport:
    """Backfill one entity's visible filesystem paths into Knowledge."""
    settings = get_settings()
    report = KnowledgeBackfillReport(entity_id=entity_id)
    if not settings.MANOR_FS_ENABLED:
        return report

    entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
    if not os.path.isdir(entity_root):
        return report

    processed = 0
    for dirpath, dirnames, filenames in os.walk(entity_root):
        rel_dir = normalize_rel_path(os.path.relpath(dirpath, entity_root))

        visible_dirnames: list[str] = []
        for dirname in dirnames:
            child_rel = normalize_rel_path(os.path.join(rel_dir, dirname))
            if is_user_visible_path(child_rel):
                visible_dirnames.append(dirname)
            else:
                report.hidden_skipped += 1
        dirnames[:] = visible_dirnames

        if rel_dir and is_user_visible_folder_path(rel_dir):
            try:
                if await ensure_folder_path(entity_id, rel_dir):
                    report.folders_synced += 1
            except Exception:
                report.errors += 1
                logger.debug("Knowledge backfill folder failed: entity=%s path=%s", entity_id, rel_dir, exc_info=True)

        for filename in filenames:
            rel_file = normalize_rel_path(os.path.join(rel_dir, filename))
            if not is_user_visible_path(rel_file):
                report.hidden_skipped += 1
                continue
            if limit is not None and processed >= limit:
                report.limited = True
                return report
            try:
                result = await sync_file_to_knowledge(
                    entity_id=entity_id,
                    abs_path=os.path.join(dirpath, filename),
                    entity_root=entity_root,
                    source=mark_source,
                    created_by="system",
                    force=True,
                )
                if result.synced:
                    report.files_synced += 1
                    processed += 1
            except Exception:
                report.errors += 1
                logger.debug("Knowledge backfill file failed: entity=%s path=%s", entity_id, rel_file, exc_info=True)
    return report


async def run_startup_knowledge_backfill() -> None:
    """Run the one-time backfill after API startup without blocking startup."""
    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        logger.info("Knowledge backfill skipped: MANOR_FS_ENABLED=false")
        return
    if os.getenv("KNOWLEDGE_BACKFILL_ON_STARTUP", "true").lower() not in ("true", "1", "yes"):
        logger.info("Knowledge backfill skipped: KNOWLEDGE_BACKFILL_ON_STARTUP disabled")
        return

    limit = _env_int("KNOWLEDGE_BACKFILL_STARTUP_LIMIT", _DEFAULT_LIMIT)
    delay_seconds = _env_int("KNOWLEDGE_BACKFILL_STARTUP_DELAY_SECONDS", 5)
    if delay_seconds > 0:
        import asyncio
        await asyncio.sleep(delay_seconds)

    async with async_session() as db:
        locked = await _try_advisory_lock(db)
        if not locked:
            logger.info("Knowledge backfill skipped: another worker holds the startup lock")
            return
        try:
            result = await db.execute(
                select(Entity).where(Entity.deleted_at.is_(None)).order_by(Entity.created_at.asc())
            )
            entities = list(result.scalars().all())
            total_files = 0
            total_folders = 0
            total_errors = 0
            for entity in entities:
                settings_dict = dict(entity.settings or {})
                if settings_dict.get(_BACKFILL_KEY, {}).get("completed") is True:
                    continue
                report = await backfill_entity_knowledge(entity.id, limit=limit)
                total_files += report.files_synced
                total_folders += report.folders_synced
                total_errors += report.errors
                settings_dict[_BACKFILL_KEY] = {
                    "completed": not report.limited,
                    "last_run_at": datetime.now(timezone.utc).isoformat(),
                    "files_synced": report.files_synced,
                    "folders_synced": report.folders_synced,
                    "hidden_skipped": report.hidden_skipped,
                    "errors": report.errors,
                    "limited": report.limited,
                }
                entity.settings = settings_dict
                await db.commit()
                logger.info(
                    "Knowledge backfill entity=%s files=%d folders=%d hidden=%d errors=%d limited=%s",
                    entity.id,
                    report.files_synced,
                    report.folders_synced,
                    report.hidden_skipped,
                    report.errors,
                    report.limited,
                )
            logger.info(
                "Knowledge startup backfill complete: entities=%d files=%d folders=%d errors=%d",
                len(entities), total_files, total_folders, total_errors,
            )
        finally:
            await _release_advisory_lock(db)


async def _try_advisory_lock(db) -> bool:
    result = await db.execute(
        text("SELECT pg_try_advisory_lock(hashtext(:key), 0)"),
        {"key": _LOCK_KEY},
    )
    return bool(result.scalar())


async def _release_advisory_lock(db) -> None:
    await db.execute(
        text("SELECT pg_advisory_unlock(hashtext(:key), 0)"),
        {"key": _LOCK_KEY},
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
