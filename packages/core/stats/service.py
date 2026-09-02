"""Workspace Stat CRUD, deterministic collectors, and observation writes."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.goals import GoalStatus
from packages.core.constants.task import TaskStatus
from packages.core.models.base import generate_ulid
from packages.core.models.document import (
    Document,
    DocumentGroup,
    DocumentGroupMember,
    Integration,
    VectorStatus,
)
from packages.core.models.goal import Goal
from packages.core.models.scheduler import AgentExecution
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.services.task_state_machine import TERMINAL_STATUSES
from packages.core.stats.integration_keys import (
    get_stat_integration_spec,
    provider_aliases_for_stat_integration,
    stat_integration_key_for_provider,
)
from packages.core.stats.library import get_library_entry


logger = logging.getLogger(__name__)


STAT_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,119}$")
SUPPORTED_VALUE_TYPES = {"number", "percent", "currency", "duration"}
SUPPORTED_COLLECTOR_TYPES = {"manual", "workspace_internal", "integration"}
SUPPORTED_WINDOWS = {
    "latest", "lifetime", "rolling_24h", "rolling_7d", "rolling_30d",
    "calendar_week", "calendar_month",
}
_LIBRARY_DEFAULT_CADENCE = object()
_STAT_NUMBER_QUANTUM = Decimal("0.000001")
_MAX_ABS_STAT_NUMBER = Decimal("1000000000000000000")


class StatError(ValueError):
    pass


def _normalize_stat_number(value: object) -> Decimal:
    """Return the exact value that fits the persisted NUMERIC(24, 6)."""
    if isinstance(value, bool):
        raise StatError("stat value must be a finite number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        normalized = number.quantize(_STAT_NUMBER_QUANTUM, rounding=ROUND_HALF_UP)
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        raise StatError("stat value must be a finite number") from None
    if not normalized.is_finite():
        raise StatError("stat value must be a finite number")
    if abs(normalized) >= _MAX_ABS_STAT_NUMBER:
        raise StatError("stat value exceeds the supported numeric range")
    return normalized


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _window_bounds(window: str, at: datetime) -> tuple[Optional[datetime], datetime]:
    window = str(window or "latest").strip().lower()
    if window in {"latest", "lifetime"}:
        return None, at
    if window == "rolling_24h":
        return at - timedelta(hours=24), at
    if window == "rolling_7d":
        return at - timedelta(days=7), at
    if window == "rolling_30d":
        return at - timedelta(days=30), at
    if window == "calendar_week":
        start = at.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=at.weekday())
        return start, at
    if window == "calendar_month":
        return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0), at
    raise StatError(f"unsupported stat window: {window}")


def _validate_definition(*, key: str, value_type: str, collector_type: str, window: str) -> None:
    if not STAT_KEY_RE.fullmatch(str(key or "")):
        raise StatError("stat key must start with a letter and contain only lowercase letters, numbers, '.', '_' or '-'")
    if value_type not in SUPPORTED_VALUE_TYPES:
        raise StatError(f"unsupported value_type: {value_type}")
    if collector_type not in SUPPORTED_COLLECTOR_TYPES:
        raise StatError(f"unsupported collector_type: {collector_type}")
    if window not in SUPPORTED_WINDOWS:
        raise StatError(f"unsupported window: {window}")


async def create_stat(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    key: str,
    name: str,
    description: Optional[str] = None,
    value_type: str = "number",
    unit: Optional[str] = None,
    window: str = "latest",
    collector_type: str = "manual",
    collector_config: Optional[dict[str, Any]] = None,
    collection_cadence: Optional[str] = None,
    freshness_limit_seconds: Optional[int] = None,
    origin: str = "user",
    library_key: Optional[str] = None,
    goal_eligible: bool = True,
    install_schedule: bool = True,
) -> WorkspaceStat:
    key = str(key or "").strip().lower()
    value_type = str(value_type or "number").strip().lower()
    collector_type = str(collector_type or "manual").strip().lower()
    window = str(window or "latest").strip().lower()
    _validate_definition(key=key, value_type=value_type, collector_type=collector_type, window=window)
    existing = (await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.workspace_id == workspace_id,
        WorkspaceStat.key == key,
    ))).scalar_one_or_none()
    if existing:
        raise StatError(f"a stat with key {key!r} already exists in this workspace")

    stat = WorkspaceStat(
        entity_id=entity_id,
        workspace_id=workspace_id,
        key=key,
        name=str(name or key).strip(),
        description=str(description).strip() if description else None,
        value_type=value_type,
        unit=str(unit).strip() if unit else None,
        window=window,
        collector_type=collector_type,
        collector_config=dict(collector_config or {}),
        collection_cadence=str(collection_cadence).strip().lower() if collection_cadence else None,
        freshness_limit_seconds=freshness_limit_seconds,
        origin=str(origin or "user").strip().lower(),
        library_key=library_key,
        goal_eligible=bool(goal_eligible),
    )
    db.add(stat)
    await db.flush()
    if install_schedule:
        from packages.core.stats.scheduling import sync_stat_collection_schedule
        await sync_stat_collection_schedule(db, stat)
    return stat


async def create_stat_from_library(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    library_key: str,
    key: Optional[str] = None,
    name: Optional[str] = None,
    window: Optional[str] = None,
    collection_cadence: Optional[str] | object = _LIBRARY_DEFAULT_CADENCE,
    collector_overrides: Optional[dict[str, Any]] = None,
    origin: str = "library",
    install_schedule: bool = True,
) -> WorkspaceStat:
    entry = get_library_entry(library_key)
    if entry is None:
        raise StatError(f"unknown stat library key: {library_key}")
    overrides = dict(collector_overrides or {})
    config = dict(entry.collector_config)
    if entry.integration_key is not None:
        requested_key = overrides.pop("integration_key", None)
        if requested_key and str(requested_key) != entry.integration_key.value:
            raise StatError(
                f"library stat {entry.key!r} requires integration_key "
                f"{entry.integration_key.value!r}"
            )
        legacy_provider = overrides.pop("provider", None)
        if legacy_provider:
            legacy_key = stat_integration_key_for_provider(legacy_provider)
            if legacy_key != entry.integration_key:
                raise StatError(
                    f"library stat {entry.key!r} does not support provider "
                    f"{legacy_provider!r}"
                )
        config["integration_key"] = entry.integration_key.value
    config.update(overrides)
    return await create_stat(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        key=key or entry.key,
        name=name or entry.name,
        description=entry.description,
        value_type=entry.value_type,
        unit=entry.unit,
        window=window or entry.default_window,
        collector_type=entry.collector_type,
        collector_config=config,
        collection_cadence=(
            entry.default_cadence
            if collection_cadence is _LIBRARY_DEFAULT_CADENCE
            else collection_cadence
        ),
        freshness_limit_seconds=entry.freshness_limit_seconds,
        origin=origin,
        library_key=entry.key,
        goal_eligible=entry.goal_eligible,
        install_schedule=install_schedule,
    )


async def get_stat(db: AsyncSession, stat_id: str, entity_id: str) -> Optional[WorkspaceStat]:
    return (await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.id == stat_id,
        WorkspaceStat.entity_id == entity_id,
    ))).scalar_one_or_none()


async def get_stat_by_key(
    db: AsyncSession, *, workspace_id: str, key: str,
) -> Optional[WorkspaceStat]:
    return (await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.workspace_id == workspace_id,
        WorkspaceStat.key == key,
    ))).scalar_one_or_none()


async def list_stats(db: AsyncSession, *, entity_id: str, workspace_id: str) -> list[WorkspaceStat]:
    return list((await db.execute(
        select(WorkspaceStat)
        .where(
            WorkspaceStat.entity_id == entity_id,
            WorkspaceStat.workspace_id == workspace_id,
        )
        .order_by(WorkspaceStat.created_at.asc(), WorkspaceStat.key.asc())
    )).scalars().all())


async def update_stat(db: AsyncSession, stat: WorkspaceStat, **fields: Any) -> WorkspaceStat:
    allowed = {
        "name", "description", "unit", "window", "collector_config",
        "collection_cadence", "freshness_limit_seconds", "status", "goal_eligible",
    }
    changed = False
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "window" and value not in SUPPORTED_WINDOWS:
            raise StatError(f"unsupported window: {value}")
        if getattr(stat, key) != value:
            setattr(stat, key, value)
            changed = True
    if changed:
        stat.revision += 1
        from packages.core.stats.scheduling import sync_stat_collection_schedule
        await sync_stat_collection_schedule(db, stat)
    await db.flush()
    return stat


async def delete_stat(db: AsyncSession, stat: WorkspaceStat) -> None:
    from packages.core.stats.scheduling import remove_stat_collection_schedule
    await remove_stat_collection_schedule(db, stat)
    await db.execute(update(Goal).where(Goal.stat_id == stat.id).values(stat_id=None))
    await db.execute(delete(WorkspaceStatObservation).where(WorkspaceStatObservation.stat_id == stat.id))
    await db.delete(stat)
    await db.flush()


async def list_observations(
    db: AsyncSession, *, stat_id: str, limit: int = 100,
) -> list[WorkspaceStatObservation]:
    return list((await db.execute(
        select(WorkspaceStatObservation)
        .where(WorkspaceStatObservation.stat_id == stat_id)
        .order_by(WorkspaceStatObservation.observed_at.desc())
        .limit(max(1, min(int(limit), 500)))
    )).scalars().all())


async def record_observation(
    db: AsyncSession,
    stat: WorkspaceStat,
    *,
    value: Decimal | float | int,
    source: str,
    observed_at: Optional[datetime] = None,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    evidence: Optional[dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> WorkspaceStatObservation:
    observed_at = observed_at or _utcnow()
    idempotency_key = idempotency_key or f"manual:{generate_ulid()}"
    existing = (await db.execute(select(WorkspaceStatObservation).where(
        WorkspaceStatObservation.stat_id == stat.id,
        WorkspaceStatObservation.idempotency_key == idempotency_key,
    ))).scalar_one_or_none()
    if existing:
        return existing

    value_dec = _normalize_stat_number(value)
    observation = WorkspaceStatObservation(
        stat_id=stat.id,
        entity_id=stat.entity_id,
        workspace_id=stat.workspace_id,
        value=value_dec,
        observed_at=observed_at,
        window_start=window_start,
        window_end=window_end,
        source=source,
        evidence=evidence,
        collector_revision=stat.revision,
        idempotency_key=idempotency_key,
    )
    db.add(observation)
    stat.current_value = value_dec
    stat.current_value_updated_at = observed_at
    stat.last_collection_status = "success"
    stat.last_collection_error = None
    await db.flush()

    from packages.core.goals.service import record_measurement as record_goal_measurement
    from packages.core.goals.numbers import project_stat_value_to_goal_number
    linked_goals = list((await db.execute(select(Goal).where(
        Goal.stat_id == stat.id,
        Goal.entity_id == stat.entity_id,
        Goal.status == GoalStatus.ACTIVE.value,
    ))).scalars().all())
    projection_errors: list[dict[str, str]] = []
    for goal in linked_goals:
        try:
            goal_value = project_stat_value_to_goal_number(value_dec)
        except ValueError as exc:
            projection_errors.append({
                "goal_id": goal.id,
                "goal_key": goal.goal_key,
                "reason": str(exc),
            })
            logger.warning(
                "Stat %s value %s cannot be represented by linked Goal %s: %s",
                stat.id,
                value_dec,
                goal.id,
                exc,
            )
            continue
        await record_goal_measurement(
            db,
            goal,
            value=goal_value,
            source=f"workspace_stat:{stat.key}",
            meta={"stat_id": stat.id, "observation_id": observation.id},
            measured_at=observed_at,
        )
    if projection_errors:
        stat.last_collection_status = "error"
        stat.last_collection_error = (
            "Goal measurement projection failed for "
            + ", ".join(
                f"{item['goal_id']} ({item['reason']})"
                for item in projection_errors
            )
        )[:2000]
        observation.evidence = {
            **(observation.evidence or {}),
            "goal_projection_errors": projection_errors,
        }
        await db.flush()
    return observation


async def collect_stat(
    db: AsyncSession,
    stat: WorkspaceStat,
    *,
    observed_at: Optional[datetime] = None,
) -> WorkspaceStatObservation:
    if stat.status != "active":
        raise StatError(f"stat is {stat.status}")
    if stat.collector_type == "manual":
        raise StatError("manual stat requires an explicit value")

    observed_at = observed_at or _utcnow()
    window_start, window_end = _window_bounds(stat.window, observed_at)
    bucket_end = window_end.replace(second=0, microsecond=0)
    bucket_start = window_start.replace(second=0, microsecond=0) if window_start else None
    period = bucket_start.isoformat() if bucket_start else "latest"
    idempotency_key = f"collect:r{stat.revision}:{stat.window}:{period}:{bucket_end.isoformat()}"

    try:
        if stat.collector_type == "workspace_internal":
            value, evidence = await _collect_workspace_internal(
                db, stat, window_start=window_start, window_end=window_end,
            )
            source = "workspace_internal"
        elif stat.collector_type == "integration":
            value, evidence = await _collect_integration(db, stat)
            source = f"integration:{evidence['provider']}"
        else:
            raise StatError(f"unsupported collector type: {stat.collector_type}")
        return await record_observation(
            db,
            stat,
            value=value,
            source=source,
            observed_at=observed_at,
            window_start=window_start,
            window_end=window_end,
            evidence=evidence,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        stat.last_collection_status = "error"
        stat.last_collection_error = str(exc)[:2000]
        await db.flush()
        if isinstance(exc, StatError):
            raise
        raise StatError(str(exc)) from exc


def _time_filter(column: Any, start: Optional[datetime], end: datetime) -> list[Any]:
    filters = [column <= end]
    if start is not None:
        filters.append(column >= start)
    return filters


async def _collect_workspace_internal(
    db: AsyncSession,
    stat: WorkspaceStat,
    *,
    window_start: Optional[datetime],
    window_end: datetime,
) -> tuple[Decimal, dict[str, Any]]:
    metric = str((stat.collector_config or {}).get("metric") or "").strip()
    task_base = [Task.entity_id == stat.entity_id, Task.workspace_id == stat.workspace_id]

    async def count_tasks(*filters: Any) -> int:
        return int((await db.execute(
            select(func.count()).select_from(Task).where(*task_base, *filters)
        )).scalar_one() or 0)

    if metric == "tasks_created":
        value = await count_tasks(*_time_filter(Task.created_at, window_start, window_end))
        return Decimal(value), {"metric": metric, "count": value}
    if metric == "tasks_completed":
        value = await count_tasks(
            Task.status == TaskStatus.COMPLETED.value,
            Task.completed_at.is_not(None),
            *_time_filter(Task.completed_at, window_start, window_end),
        )
        return Decimal(value), {"metric": metric, "count": value}
    if metric == "task_completion_rate":
        created = await count_tasks(*_time_filter(Task.created_at, window_start, window_end))
        completed = await count_tasks(
            Task.status == TaskStatus.COMPLETED.value,
            Task.completed_at.is_not(None),
            *_time_filter(Task.created_at, window_start, window_end),
            Task.completed_at <= window_end,
        )
        value = Decimal(completed * 100) / Decimal(created) if created else Decimal("0")
        return value, {"metric": metric, "created": created, "completed": completed}
    if metric == "on_time_completion_rate":
        completed = await count_tasks(
            Task.status == TaskStatus.COMPLETED.value,
            Task.deadline.is_not(None),
            Task.completed_at.is_not(None),
            *_time_filter(Task.completed_at, window_start, window_end),
        )
        on_time = await count_tasks(
            Task.status == TaskStatus.COMPLETED.value,
            Task.deadline.is_not(None),
            Task.completed_at.is_not(None),
            Task.completed_at <= Task.deadline,
            *_time_filter(Task.completed_at, window_start, window_end),
        )
        value = Decimal(on_time * 100) / Decimal(completed) if completed else Decimal("0")
        return value, {"metric": metric, "completed_with_deadline": completed, "on_time": on_time}
    if metric == "overdue_task_count":
        value = await count_tasks(
            Task.status.not_in(TERMINAL_STATUSES),
            Task.deadline.is_not(None),
            Task.deadline < window_end,
        )
        return Decimal(value), {"metric": metric, "count": value, "as_of": window_end.isoformat()}
    if metric == "blocked_task_count":
        value = await count_tasks(or_(
            Task.status == TaskStatus.BLOCKED.value,
            Task.details["dependency_status"].astext == "waiting",
        ))
        return Decimal(value), {"metric": metric, "count": value, "as_of": window_end.isoformat()}
    if metric == "avg_task_cycle_time_hours":
        completed_rows = list((await db.execute(select(
            Task.started_at, Task.completed_at,
        ).where(
            *task_base,
            Task.status == TaskStatus.COMPLETED.value,
            Task.started_at.is_not(None),
            Task.completed_at.is_not(None),
            *_time_filter(Task.completed_at, window_start, window_end),
        ))).all())
        durations = [
            (completed_at - started_at).total_seconds() / 3600.0
            for started_at, completed_at in completed_rows
        ]
        value = Decimal(str(sum(durations) / len(durations) if durations else 0))
        return value, {"metric": metric, "average_hours": float(value)}

    if metric in {"workflow_run_count", "workflow_success_rate"}:
        base = [
            WorkflowRun.entity_id == stat.entity_id,
            WorkflowRun.workspace_id == stat.workspace_id,
            *_time_filter(WorkflowRun.created_at, window_start, window_end),
        ]
        total = int((await db.execute(select(func.count()).select_from(WorkflowRun).where(*base))).scalar_one() or 0)
        if metric == "workflow_run_count":
            return Decimal(total), {"metric": metric, "count": total}
        terminal = int((await db.execute(select(func.count()).select_from(WorkflowRun).where(
            *base, WorkflowRun.status.in_({"completed", "failed", "cancelled"}),
        ))).scalar_one() or 0)
        completed = int((await db.execute(select(func.count()).select_from(WorkflowRun).where(
            *base, WorkflowRun.status == "completed",
        ))).scalar_one() or 0)
        value = Decimal(completed * 100) / Decimal(terminal) if terminal else Decimal("0")
        return value, {"metric": metric, "terminal": terminal, "completed": completed, "total": total}

    if metric in {"agent_run_count", "agent_success_rate"}:
        base = [
            AgentExecution.entity_id == stat.entity_id,
            AgentExecution.workspace_id == stat.workspace_id,
            *_time_filter(AgentExecution.created_at, window_start, window_end),
        ]
        total = int((await db.execute(select(func.count()).select_from(AgentExecution).where(*base))).scalar_one() or 0)
        if metric == "agent_run_count":
            return Decimal(total), {"metric": metric, "count": total}
        terminal = int((await db.execute(select(func.count()).select_from(AgentExecution).where(
            *base, AgentExecution.status.in_({"completed", "failed", "cancelled"}),
        ))).scalar_one() or 0)
        completed = int((await db.execute(select(func.count()).select_from(AgentExecution).where(
            *base, AgentExecution.status == "completed",
        ))).scalar_one() or 0)
        value = Decimal(completed * 100) / Decimal(terminal) if terminal else Decimal("0")
        return value, {"metric": metric, "terminal": terminal, "completed": completed, "total": total}

    if metric == "knowledge_ready_documents":
        total = int((await db.execute(
            select(func.count(func.distinct(Document.id)))
            .select_from(Document)
            .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
            .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
            .where(
            Document.entity_id == stat.entity_id,
            DocumentGroup.workspace_id == stat.workspace_id,
            Document.is_trashed.is_(False),
            Document.vector_status.in_({VectorStatus.READY, VectorStatus.INDEXED}),
        ))).scalar_one() or 0)
        return Decimal(total), {"metric": metric, "count": total, "as_of": window_end.isoformat()}

    raise StatError(f"unsupported workspace_internal metric: {metric}")


async def _collect_integration(
    db: AsyncSession, stat: WorkspaceStat,
) -> tuple[Decimal, dict[str, Any]]:
    config = stat.collector_config or {}
    integration_key = str(config.get("integration_key") or "").strip()
    if not integration_key:
        legacy_key = stat_integration_key_for_provider(config.get("provider"))
        integration_key = legacy_key.value if legacy_key else ""
    spec = get_stat_integration_spec(integration_key)
    metric_key = str(config.get("metric_key") or "").strip()
    connection_id = str(config.get("connection_id") or "").strip()
    if spec is None or not metric_key:
        raise StatError(
            "integration collector requires a supported integration_key and metric_key"
        )

    from packages.core.goals import measurers as measurer_registry
    measurer = measurer_registry.get(spec.measurer_key)
    if measurer is None:
        raise StatError(
            f"no stat measurer is registered for integration_key {integration_key!r}"
        )
    query = select(Integration).where(
        Integration.entity_id == stat.entity_id,
        Integration.provider.in_(
            provider_aliases_for_stat_integration(spec.key)
        ),
        Integration.status == "active",
    )
    if connection_id:
        query = query.where(Integration.id == connection_id)
    integration = (await db.execute(query.order_by(Integration.created_at.desc()).limit(1))).scalar_one_or_none()
    if integration is None:
        raise StatError(f"no active {integration_key} connection is available")
    value = await measurer(integration, dict(config.get("params") or {}), metric_key)
    return Decimal(str(value)), {
        "integration_key": integration_key,
        "provider": spec.provider_key,
        "connection_id": integration.id,
        "metric_key": metric_key,
    }
