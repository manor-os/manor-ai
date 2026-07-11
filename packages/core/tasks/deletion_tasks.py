"""Background tasks that hard-delete soft-deleted resources after the
grace window elapses.

Workflow:
  1. User clicks "Delete workspace" → API marks ``Workspace.deleted_at``.
  2. Workspace stays in the trash for ``WORKSPACE_PURGE_GRACE_DAYS``
     days (env-configurable, default 30).
  3. ``ops.purge_soft_deleted_workspaces`` runs nightly. For each row
     past the cutoff, it calls ``entity_service.purge_workspace`` which
     cascades through all workspace-scoped tables.
  4. After purge, the row is gone — restore is no longer possible.

User-account purge follows the same pattern but lives alongside this
file (``ops.purge_soft_deleted_users``) so all "soft → hard" sweeps
share the same scheduler entry point.
"""
from __future__ import annotations

import logging

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker as _run_async

logger = logging.getLogger(__name__)


@celery_app.task(name="ops.purge_soft_deleted_workspaces")
def purge_soft_deleted_workspaces():
    """Nightly: hard-delete workspaces whose grace window has expired."""
    _run_async(_async_purge_workspaces())


@celery_app.task(name="ops.purge_soft_deleted_users")
def purge_soft_deleted_users():
    """Nightly: hard-delete user accounts whose grace window has
    expired. Also handles cascade for users who were sole admins of
    their entity (entity gets purged with them)."""
    _run_async(_async_purge_users())


async def _async_purge_workspaces():
    from packages.core.database import create_worker_session
    from packages.core.services.entity_service import (
        list_workspaces_due_for_purge, purge_workspace,
    )

    purged = 0
    failed = 0
    async with create_worker_session()() as db:
        candidates = await list_workspaces_due_for_purge(db)
        for ws in candidates:
            try:
                ok = await purge_workspace(db, ws.id)
                if ok:
                    purged += 1
                    logger.info(
                        "ops.purge_workspaces: purged workspace=%s entity=%s "
                        "deleted_at=%s",
                        ws.id, ws.entity_id, ws.deleted_at,
                    )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception(
                    "ops.purge_workspaces: workspace=%s purge failed: %s",
                    ws.id, exc,
                )
                # Don't poison the loop — let the next workspace try.
                await db.rollback()
                continue
        if purged or failed:
            await db.commit()

    if purged or failed:
        logger.info(
            "ops.purge_workspaces: done — purged=%d failed=%d", purged, failed,
        )


async def _async_purge_users():
    from packages.core.database import create_worker_session
    from packages.core.services.user_lifecycle import (
        list_users_due_for_purge, purge_user,
    )

    purged = 0
    failed = 0
    async with create_worker_session()() as db:
        candidates = await list_users_due_for_purge(db)
        for user in candidates:
            try:
                ok = await purge_user(db, user.id)
                if ok:
                    purged += 1
                    logger.info(
                        "ops.purge_users: purged user=%s entity=%s "
                        "deleted_at=%s",
                        user.id, user.entity_id, user.deleted_at,
                    )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception(
                    "ops.purge_users: user=%s purge failed: %s",
                    user.id, exc,
                )
                await db.rollback()
                continue
        if purged or failed:
            await db.commit()

    if purged or failed:
        logger.info(
            "ops.purge_users: done — purged=%d failed=%d", purged, failed,
        )
