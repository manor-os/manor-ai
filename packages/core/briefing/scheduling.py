"""ScheduledJob row management for daily briefings.

Mirrors the strategist + goal_measurement scheduling pattern:

  install_briefing_schedule(db, workspace, time_of_day, timezone)
      derives a stable job_id ``br:<workspace_id>``, parses the time
      into a cron expression, upserts the ScheduledJob row.

  remove_briefing_schedule(db, workspace_id)
      drops the row.

The scheduler tick (packages.core.tasks.scheduler_tasks) routes
``execution_type='briefing'`` to the ``run_morning_briefing`` Celery task.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.user import User
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)


_TIME_RE = re.compile(r"^(?P<hh>\d{1,2}):(?P<mm>\d{2})$")
_DEFAULT_BRIEFING_TIME = "08:00"
_DEFAULT_TIMEZONE = "UTC"
_TIME_KEYS = (
    "post_time_local",
    "time_of_day",
    "daily_briefing_time",
    "daily_summary_time",
    "morning_briefing_time",
    "briefing_time",
)
_TIMEZONE_KEYS = (
    "tz",
    "timezone",
    "briefing_timezone",
    "daily_briefing_timezone",
)
_NESTED_KEYS = (
    "daily_briefing",
    "daily_summary",
    "workspace_daily_summary",
    "morning_briefing",
)


def _parse_time_parts(time_of_day: str) -> tuple[int, int]:
    m = _TIME_RE.match(time_of_day or "")
    if not m:
        raise ValueError(
            f"time_of_day must be HH:MM, got {time_of_day!r}"
        )
    hh = int(m.group("hh"))
    mm = int(m.group("mm"))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"invalid time {time_of_day!r}")
    return hh, mm


def _normalize_time_of_day(time_of_day: str) -> str:
    hh, mm = _parse_time_parts(time_of_day)
    return f"{hh:02d}:{mm:02d}"


def _parse_time_to_cron(time_of_day: str) -> str:
    """``"08:00"`` → ``"0 8 * * *"`` cron expression.

    Five-field crontab format (m h dom mon dow). Daily at HH:MM.
    Quartz-style 6-field (with seconds prefix) is not used here —
    the existing scheduler tick parses 5-field.
    """
    hh, mm = _parse_time_parts(time_of_day)
    return f"{mm} {hh} * * *"


def _job_id_for(workspace_id: str) -> str:
    return f"br:{workspace_id}"


def _string_setting(settings: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested_string_setting(
    settings: dict[str, Any],
    keys: tuple[str, ...],
) -> Optional[str]:
    for parent_key in _NESTED_KEYS:
        nested = settings.get(parent_key)
        if isinstance(nested, dict):
            value = _string_setting(nested, keys)
            if value:
                return value
    return None


def _user_timezone(user: Optional[User]) -> Optional[str]:
    if user is None:
        return None
    tz = getattr(user, "timezone", None)
    if isinstance(tz, str) and tz.strip():
        return tz.strip()
    return None


def resolve_briefing_schedule_settings(
    *,
    workspace: Workspace,
    user: Optional[User] = None,
    params: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    """Resolve the daily briefing wall-clock schedule from user settings.

    Explicit template params represent the user's current UI selection. When
    omitted, use user preferences for the briefing time and the user's profile
    timezone. Workspace settings remain a compatibility fallback.
    """

    params = params or {}
    user_preferences = (user.preferences if user is not None else None) or {}
    workspace_settings = workspace.settings or {}

    raw_time = (
        _string_setting(params, _TIME_KEYS)
        or _nested_string_setting(params, _TIME_KEYS)
        or _string_setting(user_preferences, _TIME_KEYS)
        or _nested_string_setting(user_preferences, _TIME_KEYS)
        or _string_setting(workspace_settings, _TIME_KEYS)
        or _nested_string_setting(workspace_settings, _TIME_KEYS)
        or _DEFAULT_BRIEFING_TIME
    )
    raw_timezone = (
        _string_setting(params, _TIMEZONE_KEYS)
        or _nested_string_setting(params, _TIMEZONE_KEYS)
        or _string_setting(user_preferences, _TIMEZONE_KEYS)
        or _nested_string_setting(user_preferences, _TIMEZONE_KEYS)
        or _user_timezone(user)
        or _string_setting(workspace_settings, _TIMEZONE_KEYS)
        or _nested_string_setting(workspace_settings, _TIMEZONE_KEYS)
        or _DEFAULT_TIMEZONE
    )
    return _normalize_time_of_day(raw_time), raw_timezone


def briefing_schedule_preferences_changed(preferences: dict[str, Any]) -> bool:
    if _string_setting(preferences, _TIME_KEYS + _TIMEZONE_KEYS):
        return True
    return any(key in preferences for key in _NESTED_KEYS)


async def sync_user_briefing_schedules(
    db: AsyncSession,
    user: User,
) -> list[ScheduledJob]:
    """Apply the user's current briefing settings to their existing jobs."""

    rows = list((await db.execute(
        select(ScheduledJob, Workspace)
        .join(Workspace, ScheduledJob.workspace_id == Workspace.id)
        .where(
            ScheduledJob.entity_id == user.entity_id,
            ScheduledJob.execution_type == "briefing",
            ScheduledJob.user_id == user.id,
            Workspace.deleted_at.is_(None),
        )
    )).all())
    updated: list[ScheduledJob] = []
    for job, workspace in rows:
        time_of_day, timezone = resolve_briefing_schedule_settings(
            workspace=workspace,
            user=user,
        )
        job.cron_expr = _parse_time_to_cron(time_of_day)
        job.timezone = timezone
        job.execution_target = {"workspace_id": workspace.id}
        job.schedule_kind = "cron"
        job.every_seconds = None
        updated.append(job)
    if updated:
        await db.flush()
    return updated


async def install_briefing_schedule(
    db: AsyncSession,
    workspace: Workspace,
    *,
    time_of_day: str = "08:00",
    timezone: str = "UTC",
    user_id: str | None = None,
) -> ScheduledJob:
    """Get-or-update the workspace's daily briefing schedule.

    Idempotent — derived job_id means re-installation just updates the
    cron + timezone in place. Caller commits.
    """
    cron_expr = _parse_time_to_cron(time_of_day)
    job_id = _job_id_for(workspace.id)

    existing = (await db.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == job_id)
    )).scalar_one_or_none()

    if existing:
        existing.entity_id = workspace.entity_id
        existing.workspace_id = workspace.id
        existing.schedule_kind = "cron"
        existing.cron_expr = cron_expr
        existing.every_seconds = None
        existing.timezone = timezone
        existing.execution_type = "briefing"
        existing.execution_target = {"workspace_id": workspace.id}
        if user_id is not None:
            existing.user_id = user_id
        existing.enabled = True
        existing.consecutive_errors = 0
        await db.flush()
        return existing

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name=f"Morning briefing: {workspace.name}",
        job_type="cron",
        schedule_kind="cron",
        cron_expr=cron_expr,
        timezone=timezone,
        execution_type="briefing",
        execution_target={"workspace_id": workspace.id},
        user_id=user_id,
        enabled=True,
    )
    db.add(job)
    await db.flush()
    return job


async def remove_briefing_schedule(
    db: AsyncSession, workspace_id: str,
) -> None:
    await db.execute(
        delete(ScheduledJob).where(ScheduledJob.job_id == _job_id_for(workspace_id))
    )
    await db.flush()
