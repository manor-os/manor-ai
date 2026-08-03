"""Runtime event persistence helpers for Manor AI Runtime Harness."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import RuntimeEnvelope
from packages.core.ai.runtime.persistence import (
    runtime_envelope_meta,
    runtime_events,
    runtime_mark_events_persisted,
    runtime_persisted_event_count,
)
from packages.core.models.runtime_learning import RuntimeEventLog

logger = logging.getLogger(__name__)


def runtime_event_records_from_envelope(
    envelope: RuntimeEnvelope,
    *,
    source: str = "runtime",
    message_id: str | None = None,
    trace_id: str | None = None,
    start_index: int = 0,
) -> list[dict[str, Any]]:
    """Convert an envelope's runtime events into durable row payloads.

    ``start_index`` skips events already written for this envelope (see
    ``runtime_persisted_event_count``); ``sequence`` stays the absolute
    position in the run so the rows still order correctly.
    """
    if not envelope.entity_id:
        return []
    runtime_meta = runtime_envelope_meta(envelope) or {}
    rows: list[dict[str, Any]] = []
    for idx, event in enumerate(runtime_events(envelope)):
        if idx < start_index:
            continue
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            continue
        rows.append({
            "entity_id": envelope.entity_id,
            "workspace_id": envelope.workspace_id,
            "agent_id": envelope.agent_id,
            "user_id": envelope.user_id,
            "conversation_id": envelope.conversation_id,
            "message_id": message_id,
            "task_id": envelope.task_id,
            "trace_id": trace_id,
            "surface": envelope.surface.value,
            "profile": envelope.profile.value,
            "principal_kind": envelope.principal.kind.value,
            "event_type": event_type,
            "tool_name": event.get("tool_name"),
            "source": source,
            "sequence": idx,
            "event_data": dict(event),
            "runtime": runtime_meta,
        })
    return rows


async def persist_runtime_events(
    db: AsyncSession,
    envelope: RuntimeEnvelope,
    *,
    source: str = "runtime",
    message_id: str | None = None,
    trace_id: str | None = None,
    start_index: int = 0,
) -> list[RuntimeEventLog]:
    """Persist runtime events for later audit/query.

    Callers should treat this as a best-effort side channel; message/task state
    remains authoritative for the user-facing flow.
    """
    records = runtime_event_records_from_envelope(
        envelope,
        source=source,
        message_id=message_id,
        trace_id=trace_id,
        start_index=start_index,
    )
    logs = [RuntimeEventLog(**record) for record in records]
    if logs:
        db.add_all(logs)
    return logs


async def persist_runtime_events_best_effort(
    envelope: RuntimeEnvelope | None,
    *,
    source: str = "runtime",
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Persist runtime events in an isolated transaction.

    Runtime event rows are audit/learning side data. They must never roll back
    the user-visible message, channel reply, or task completion they describe.

    Idempotent per event: handlers now persist from a ``finally`` so the call
    happens on every exit path, and ``runtime_event_logs`` has no unique
    constraint to catch a repeat. Only events past the envelope's persisted
    watermark are written.
    """
    if envelope is None:
        return 0
    already_persisted = runtime_persisted_event_count(envelope)
    if len(runtime_events(envelope)) <= already_persisted:
        return 0
    try:
        from packages.core.database import async_session

        async with async_session() as db:
            logs = await persist_runtime_events(
                db,
                envelope,
                source=source,
                message_id=message_id,
                trace_id=trace_id,
                start_index=already_persisted,
            )
            await db.commit()
            runtime_mark_events_persisted(envelope, already_persisted + len(logs))
            return len(logs)
    except Exception:
        logger.warning(
            "Failed to persist runtime events source=%s message_id=%s trace_id=%s",
            source,
            message_id,
            trace_id,
            exc_info=True,
        )
        return 0


async def list_runtime_events(
    db: AsyncSession,
    *,
    entity_id: str,
    conversation_id: str | None = None,
    task_id: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[RuntimeEventLog]:
    stmt = select(RuntimeEventLog).where(RuntimeEventLog.entity_id == entity_id)
    if conversation_id:
        stmt = stmt.where(RuntimeEventLog.conversation_id == conversation_id)
    if task_id:
        stmt = stmt.where(RuntimeEventLog.task_id == task_id)
    if event_type:
        stmt = stmt.where(RuntimeEventLog.event_type == event_type)
    stmt = stmt.order_by(RuntimeEventLog.created_at.desc()).limit(max(1, min(limit, 500)))
    return list((await db.execute(stmt)).scalars().all())
