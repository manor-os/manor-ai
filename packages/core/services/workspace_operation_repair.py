"""Startup repair for workspace operation runtime rows.

Older workspaces can have a rich ``operating_model`` JSON contract but no
materialized runtime rows yet. This backfill keeps startup self-healing without
blocking API readiness, mirroring the filesystem Knowledge backfill pattern.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_operation_service import repair_workspace_operation_runtime

logger = logging.getLogger(__name__)

_REPAIR_KEY = "workspace_operation_runtime_repair_v1"
_LOCK_KEY = "manor:workspace_operation_runtime_repair_v1"
_DEFAULT_LIMIT = 500


@dataclass
class WorkspaceOperationRepairReport:
    scanned: int = 0
    repaired: int = 0
    skipped_marked: int = 0
    skipped_empty: int = 0
    errors: int = 0
    limited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def repair_workspace_operation_runtime_backfill(
    db: AsyncSession,
    *,
    limit: int | None = _DEFAULT_LIMIT,
) -> WorkspaceOperationRepairReport:
    """Repair existing operation runtime state for all eligible workspaces.

    The scan is additive and non-destructive. A marker in ``Workspace.settings``
    prevents repeat startup runs from generating duplicate activity entries.
    Manual repair remains available through the workspace API.
    """
    report = WorkspaceOperationRepairReport()
    result = await db.execute(
        select(
            Workspace.id,
            Workspace.entity_id,
            Workspace.settings,
            Workspace.heartbeat_enabled,
            Workspace.operating_model,
        )
        .where(Workspace.deleted_at.is_(None))
        .order_by(Workspace.created_at.asc())
    )
    workspaces = list(result.mappings().all())

    for row in workspaces:
        workspace_id = row["id"]
        entity_id = row["entity_id"]
        report.scanned += 1
        settings = dict(row["settings"] or {})
        marker = settings.get(_REPAIR_KEY)
        if isinstance(marker, dict) and marker.get("completed") is True:
            report.skipped_marked += 1
            continue
        if not _has_repairable_runtime_payload(row):
            settings[_REPAIR_KEY] = {
                "completed": True,
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "result": {"skipped": "empty"},
            }
            workspace = await db.get(Workspace, workspace_id)
            if workspace is not None:
                workspace.settings = settings
            await db.commit()
            report.skipped_empty += 1
            continue
        if limit is not None and report.repaired >= limit:
            report.limited = True
            break

        try:
            repair_result = await repair_workspace_operation_runtime(
                db,
                workspace_id,
                entity_id,
                user_id=None,
            )
            settings[_REPAIR_KEY] = {
                "completed": True,
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "result": _summarize_repair_result(repair_result or {}),
            }
            workspace = await db.get(Workspace, workspace_id)
            if workspace is not None:
                workspace.settings = settings
            await db.commit()
            report.repaired += 1
            logger.info(
                "Workspace operation runtime repair: workspace=%s result=%s",
                workspace_id,
                settings[_REPAIR_KEY]["result"],
            )
        except Exception:
            report.errors += 1
            await db.rollback()
            logger.warning(
                "Workspace operation runtime repair failed: workspace=%s",
                workspace_id,
                exc_info=True,
            )

    return report


async def run_startup_workspace_operation_runtime_repair() -> None:
    """Run the one-time runtime repair after API startup."""
    if os.getenv("WORKSPACE_OPERATION_REPAIR_ON_STARTUP", "true").lower() not in (
        "true",
        "1",
        "yes",
    ):
        logger.info("Workspace operation repair skipped: WORKSPACE_OPERATION_REPAIR_ON_STARTUP disabled")
        return

    limit = _env_optional_int("WORKSPACE_OPERATION_REPAIR_STARTUP_LIMIT", _DEFAULT_LIMIT)
    delay_seconds = _env_optional_int("WORKSPACE_OPERATION_REPAIR_STARTUP_DELAY_SECONDS", 5) or 0
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    async with async_session() as db:
        locked = await _try_advisory_lock(db)
        if not locked:
            logger.info("Workspace operation repair skipped: another worker holds the startup lock")
            return
        try:
            report = await repair_workspace_operation_runtime_backfill(db, limit=limit)
            logger.info("Workspace operation startup repair complete: %s", report.to_dict())
        finally:
            await _release_advisory_lock(db)


def _has_repairable_runtime_payload(workspace: Workspace | Mapping[str, Any]) -> bool:
    if isinstance(workspace, Mapping):
        heartbeat_enabled = workspace.get("heartbeat_enabled")
        operating_model_value = workspace.get("operating_model")
    else:
        heartbeat_enabled = workspace.heartbeat_enabled
        operating_model_value = workspace.operating_model

    if bool(heartbeat_enabled):
        return True
    operating_model = operating_model_value if isinstance(operating_model_value, dict) else {}
    for key in ("goals", "rules", "agent_mappings", "channel_config"):
        if _non_empty(operating_model.get(key)):
            return True
    return False


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _summarize_repair_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_revision": result.get("workspace_revision"),
        "goals": result.get("goals") or {},
        "agent_mappings": result.get("agent_mappings") or {},
        "channels": result.get("channels") or {},
        "governance": result.get("governance") or {},
    }


async def _try_advisory_lock(db: AsyncSession) -> bool:
    try:
        result = await db.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:key), 0)"),
            {"key": _LOCK_KEY},
        )
        return bool(result.scalar())
    except Exception:
        await db.rollback()
        logger.debug("Workspace operation repair advisory lock unavailable; continuing without lock", exc_info=True)
        return True


async def _release_advisory_lock(db: AsyncSession) -> None:
    try:
        await db.execute(
            text("SELECT pg_advisory_unlock(hashtext(:key), 0)"),
            {"key": _LOCK_KEY},
        )
    except Exception:
        await db.rollback()
        logger.debug("Workspace operation repair advisory unlock skipped", exc_info=True)


def _env_optional_int(name: str, default: int | None) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None
