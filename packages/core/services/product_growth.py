"""Factories for durable, idempotent product-growth facts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.core.constants.analytics import ProductGrowthMilestone
from packages.core.models.base import generate_ulid
from packages.core.models.product_growth import ProductGrowthEvent


_EVENT_IDENTITY_COLUMNS = (
    "entity_id",
    "milestone",
    "source_kind",
    "source_id",
)


async def record_product_growth_milestone(
    db: Any,
    *,
    entity_id: str | None,
    user_id: str | None,
    milestone: ProductGrowthMilestone | str,
    source_kind: str,
    source_id: str | None,
    workspace_id: str | None = None,
    occurred_at: datetime | None = None,
) -> bool:
    """Insert one immutable milestone, returning whether it was new.

    The source identity is the idempotency key. Replayed finalizers and
    repeated factory calls therefore converge on one fact without relying on
    caller-side existence checks.
    """
    normalized_entity_id = str(entity_id or "").strip()
    normalized_user_id = str(user_id or "").strip()
    normalized_source_kind = str(source_kind or "").strip()
    normalized_source_id = str(source_id or "").strip()
    if not all((
        normalized_entity_id,
        normalized_user_id,
        normalized_source_kind,
        normalized_source_id,
    )):
        return False

    normalized_milestone = ProductGrowthMilestone(milestone).value
    timestamp = occurred_at or datetime.now(timezone.utc)
    values = {
        "id": generate_ulid(),
        "entity_id": normalized_entity_id,
        "workspace_id": str(workspace_id) if workspace_id else None,
        "user_id": normalized_user_id,
        "milestone": normalized_milestone,
        "source_kind": normalized_source_kind,
        "source_id": normalized_source_id,
        "occurred_at": timestamp,
    }

    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else getattr(db, "bind", None)
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name == "postgresql":
        statement = pg_insert(ProductGrowthEvent).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=list(_EVENT_IDENTITY_COLUMNS),
        )
        result = await db.execute(statement)
        return bool(result.rowcount)
    if dialect_name == "sqlite":
        statement = sqlite_insert(ProductGrowthEvent).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=list(_EVENT_IDENTITY_COLUMNS),
        )
        result = await db.execute(statement)
        return bool(result.rowcount)

    # Non-production dialect fallback. The database uniqueness constraint is
    # still authoritative; this branch mainly serves isolated test doubles.
    existing = await db.scalar(select(ProductGrowthEvent.id).where(
        ProductGrowthEvent.entity_id == normalized_entity_id,
        ProductGrowthEvent.milestone == normalized_milestone,
        ProductGrowthEvent.source_kind == normalized_source_kind,
        ProductGrowthEvent.source_id == normalized_source_id,
    ))
    if existing is not None:
        return False
    db.add(ProductGrowthEvent(**values))
    await db.flush()
    return True


async def record_scheduled_job_created_milestone(db: Any, job: Any) -> bool:
    """Record a scheduled definition from any workspace or entity scope."""
    return await record_product_growth_milestone(
        db,
        entity_id=getattr(job, "entity_id", None),
        workspace_id=getattr(job, "workspace_id", None),
        user_id=getattr(job, "user_id", None),
        milestone=ProductGrowthMilestone.AUTOMATION_CREATED,
        source_kind="scheduled_job",
        source_id=getattr(job, "id", None),
        occurred_at=getattr(job, "created_at", None),
    )


async def persist_scheduled_job(db: Any, job: Any) -> Any:
    """Persist a ScheduledJob and its attributed creation fact together."""
    from packages.core.schedule_clock import refresh_scheduled_job_next_run_at

    refresh_scheduled_job_next_run_at(job)
    db.add(job)
    await db.flush()
    await record_scheduled_job_created_milestone(db, job)
    return job
