"""Domain event emission helpers."""
from __future__ import annotations

import asyncio
import logging
from weakref import WeakKeyDictionary
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, event, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.notification_types import NotificationOutboxStatus
from packages.core.models.base import generate_ulid
from packages.core.models.event import EventLog

logger = logging.getLogger(__name__)

_EXTERNAL_EVENT_QUEUE_KEY = "manor_external_event_queue"
_EXTERNAL_EVENT_LISTENERS_KEY = "manor_external_event_listeners"
_EVENT_PERSISTENCE_TASKS: set[asyncio.Task[None]] = set()
_EXTERNAL_EVENT_DELIVERY_TASKS: set[asyncio.Task[None]] = set()
_WORKER_EVENT_PERSISTENCE_TIMEOUT_SECONDS = 10.0
_WORKER_EXTERNAL_EVENT_DRAIN_TIMEOUT_SECONDS = 2.0
_EXTERNAL_EVENT_ATTEMPT_TIMEOUT_SECONDS = 30.0
_EXTERNAL_EVENT_MAX_CONCURRENCY = 2
_EXTERNAL_EVENT_DELIVERY_LEASE = timedelta(minutes=2)
_EXTERNAL_EVENT_MAX_ATTEMPTS = 5
_EXTERNAL_EVENT_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
_EXTERNAL_EVENT_DELIVERY_SEMAPHORES: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Semaphore,
] = WeakKeyDictionary()


@dataclass(frozen=True)
class _ExternalEventDeliveryClaim:
    event_id: str
    entity_id: str
    workspace_id: str | None
    event_type: str
    payload: dict[str, Any]
    completed_sinks: dict[str, Any]
    attempt_count: int
    claim_token: str


def _eligible_external_event_delivery(when: datetime):
    return or_(
        and_(
            EventLog.external_delivery_status
            == NotificationOutboxStatus.PENDING.value,
            EventLog.external_delivery_available_at <= when,
        ),
        and_(
            EventLog.external_delivery_status
            == NotificationOutboxStatus.PROCESSING.value,
            or_(
                EventLog.external_delivery_locked_until.is_(None),
                EventLog.external_delivery_locked_until <= when,
            ),
        ),
    )


def _external_event_retry_at(when: datetime, attempt_count: int) -> datetime:
    index = max(0, min(attempt_count - 1, len(_EXTERNAL_EVENT_RETRY_DELAYS) - 1))
    return when + _EXTERNAL_EVENT_RETRY_DELAYS[index]


def _active_workspace_exists():
    from packages.core.models.workspace import Workspace

    return exists().where(
        Workspace.id == EventLog.workspace_id,
        Workspace.entity_id == EventLog.entity_id,
        Workspace.deleted_at.is_(None),
        Workspace.status == "active",
    )


async def _cancel_inactive_workspace_deliveries(
    db: AsyncSession,
    *,
    eligible,
    event_ids: tuple[str, ...] | None = None,
) -> int:
    query = (
        update(EventLog)
        .where(
            eligible,
            EventLog.workspace_id.is_not(None),
            ~_active_workspace_exists(),
        )
        .values(
            external_delivery_status=NotificationOutboxStatus.CANCELED.value,
            external_delivery_locked_until=None,
            external_delivery_claim_token=None,
            external_delivery_last_error=(
                "workspace inactive or deleted before external event delivery"
            ),
        )
    )
    if event_ids is not None:
        query = query.where(EventLog.id.in_(set(event_ids)))
    result = await db.execute(query)
    return int(result.rowcount or 0)


async def _resolve_event_workspace_id(
    db: AsyncSession,
    *,
    entity_id: str,
    payload: dict[str, Any],
    workspace_id: str | None,
) -> str | None:
    if workspace_id:
        return workspace_id

    payload_workspace_id = payload.get("workspace_id")
    if isinstance(payload_workspace_id, str) and payload_workspace_id.strip():
        return payload_workspace_id.strip()

    task_id = payload.get("task_id")
    if isinstance(task_id, str) and task_id.strip() and hasattr(db, "scalar"):
        from packages.core.models.task import Task

        task_workspace_id = await db.scalar(
            select(Task.workspace_id).where(
                Task.id == task_id,
                Task.entity_id == entity_id,
            )
        )
        if task_workspace_id:
            return str(task_workspace_id)

    document_id = payload.get("document_id")
    if isinstance(document_id, str) and document_id.strip() and hasattr(db, "execute"):
        from packages.core.models.document import Document
        from packages.core.services.document_access import document_workspace_ids

        document = (await db.execute(
            select(Document).where(
                Document.id == document_id,
                Document.entity_id == entity_id,
            )
        )).scalar_one_or_none()
        if document is not None:
            workspace_ids = await document_workspace_ids(db, document)
            if len(workspace_ids) == 1:
                return next(iter(workspace_ids))

    return None


async def _claim_external_event_deliveries(
    db: AsyncSession,
    *,
    when: datetime,
    batch_size: int,
    event_ids: tuple[str, ...] | None = None,
) -> tuple[list[_ExternalEventDeliveryClaim], int, int]:
    eligible = _eligible_external_event_delivery(when)
    canceled = await _cancel_inactive_workspace_deliveries(
        db,
        eligible=eligible,
        event_ids=event_ids,
    )
    exhausted_query = (
        update(EventLog)
        .where(
            eligible,
            EventLog.external_delivery_attempt_count >= _EXTERNAL_EVENT_MAX_ATTEMPTS,
        )
        .values(
            external_delivery_status=NotificationOutboxStatus.FAILED.value,
            external_delivery_locked_until=None,
            external_delivery_claim_token=None,
            external_delivery_last_error=(
                "external event delivery retry limit exhausted after worker interruption"
            ),
        )
    )
    if event_ids is not None:
        exhausted_query = exhausted_query.where(EventLog.id.in_(set(event_ids)))
    exhausted = await db.execute(exhausted_query)

    query = (
        select(EventLog)
        .where(
            eligible,
            EventLog.external_delivery_attempt_count < _EXTERNAL_EVENT_MAX_ATTEMPTS,
        )
        .order_by(
            EventLog.external_delivery_available_at.asc(),
            EventLog.created_at.asc(),
        )
        .limit(max(1, batch_size))
        .with_for_update(skip_locked=True)
    )
    if event_ids is not None:
        query = query.where(EventLog.id.in_(set(event_ids)))
    rows = list((await db.scalars(query)).all())
    claims: list[_ExternalEventDeliveryClaim] = []
    for entry in rows:
        claim_token = generate_ulid()
        entry.external_delivery_status = NotificationOutboxStatus.PROCESSING.value
        entry.external_delivery_attempt_count += 1
        entry.external_delivery_locked_until = when + _EXTERNAL_EVENT_DELIVERY_LEASE
        entry.external_delivery_claim_token = claim_token
        entry.external_delivery_last_error = None
        claims.append(
            _ExternalEventDeliveryClaim(
                event_id=entry.id,
                entity_id=entry.entity_id or "",
                workspace_id=entry.workspace_id,
                event_type=entry.event_type,
                payload=dict(entry.payload or {}),
                completed_sinks=dict(entry.external_delivery_completed_sinks or {}),
                attempt_count=entry.external_delivery_attempt_count,
                claim_token=claim_token,
            )
        )
    await db.commit()
    return claims, int(exhausted.rowcount or 0), canceled


async def _finish_external_event_delivery(
    claim: _ExternalEventDeliveryClaim,
    *,
    outcome: str,
    when: datetime,
    error: str | None = None,
) -> str:
    from packages.core.database import async_session

    async with async_session() as db:
        result = await _finish_external_event_delivery_in_session(
            db,
            claim,
            outcome=outcome,
            when=when,
            error=error,
        )
        await db.commit()
    return result


async def _finish_external_event_delivery_in_session(
    db: AsyncSession,
    claim: _ExternalEventDeliveryClaim,
    *,
    outcome: str,
    when: datetime,
    error: str | None = None,
) -> str:
    if outcome == NotificationOutboxStatus.DELIVERED.value:
        values = {
            "external_delivery_status": NotificationOutboxStatus.DELIVERED.value,
            "external_delivery_locked_until": None,
            "external_delivery_claim_token": None,
            "external_delivery_delivered_at": when,
            "external_delivery_last_error": None,
        }
    elif outcome == NotificationOutboxStatus.CANCELED.value:
        values = {
            "external_delivery_status": NotificationOutboxStatus.CANCELED.value,
            "external_delivery_locked_until": None,
            "external_delivery_claim_token": None,
            "external_delivery_last_error": (error or "external event delivery canceled")[:2000],
        }
    elif claim.attempt_count >= _EXTERNAL_EVENT_MAX_ATTEMPTS:
        values = {
            "external_delivery_status": NotificationOutboxStatus.FAILED.value,
            "external_delivery_locked_until": None,
            "external_delivery_claim_token": None,
            "external_delivery_last_error": (error or "external event delivery failed")[:2000],
        }
        outcome = NotificationOutboxStatus.FAILED.value
    else:
        values = {
            "external_delivery_status": NotificationOutboxStatus.PENDING.value,
            "external_delivery_available_at": _external_event_retry_at(
                when,
                claim.attempt_count,
            ),
            "external_delivery_locked_until": None,
            "external_delivery_claim_token": None,
            "external_delivery_last_error": (error or "external event delivery failed")[:2000],
        }
        outcome = "retried"

    result = await db.execute(
        update(EventLog)
        .where(
            EventLog.id == claim.event_id,
            EventLog.external_delivery_status
            == NotificationOutboxStatus.PROCESSING.value,
            EventLog.external_delivery_claim_token == claim.claim_token,
        )
        .values(**values)
    )
    return outcome if result.rowcount else "stale"


async def _mark_external_sink_completed(
    claim: _ExternalEventDeliveryClaim,
    completed_sinks: dict[str, Any],
    sink_key: str,
) -> dict[str, Any]:
    updated = dict(completed_sinks)
    updated[sink_key] = True
    statement = (
        update(EventLog)
        .where(
            EventLog.id == claim.event_id,
            EventLog.external_delivery_status
            == NotificationOutboxStatus.PROCESSING.value,
            EventLog.external_delivery_claim_token == claim.claim_token,
        )
        .values(external_delivery_completed_sinks=updated)
    )
    from packages.core.database import async_session

    async with async_session() as mark_db:
        await mark_db.execute(statement)
        await mark_db.commit()
    return updated


async def _deliver_and_finish_external_event(
    claim: _ExternalEventDeliveryClaim,
    *,
    db: AsyncSession | None = None,
) -> str:
    completed_sinks = dict(claim.completed_sinks or {})

    async def mark_completed(sink_key: str) -> None:
        nonlocal completed_sinks
        completed_sinks = await asyncio.shield(
            _mark_external_sink_completed(
                claim,
                completed_sinks,
                sink_key,
            )
        )

    try:
        if completed_sinks.get("webhook") is not True:
            await deliver_webhook_event(claim.entity_id, claim.event_type, claim.payload)
            await mark_completed("webhook")
        if completed_sinks.get("task_external") is not True:
            await deliver_task_external_event(
                claim.entity_id,
                claim.event_type,
                claim.payload,
                completed_sinks=completed_sinks,
                mark_sink_completed=mark_completed,
            )
            await mark_completed("task_external")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if db is not None:
            return await _finish_external_event_delivery_in_session(
                db,
                claim,
                outcome=NotificationOutboxStatus.FAILED.value,
                when=datetime.now(timezone.utc),
                error=str(exc),
            )
        return await _finish_external_event_delivery(
            claim,
            outcome=NotificationOutboxStatus.FAILED.value,
            when=datetime.now(timezone.utc),
            error=str(exc),
        )
    if db is not None:
        return await _finish_external_event_delivery_in_session(
            db,
            claim,
            outcome=NotificationOutboxStatus.DELIVERED.value,
            when=datetime.now(timezone.utc),
        )
    return await _finish_external_event_delivery(
        claim,
        outcome=NotificationOutboxStatus.DELIVERED.value,
        when=datetime.now(timezone.utc),
    )


def _external_event_delivery_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _EXTERNAL_EVENT_DELIVERY_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_EXTERNAL_EVENT_MAX_CONCURRENCY)
        _EXTERNAL_EVENT_DELIVERY_SEMAPHORES[loop] = semaphore
    return semaphore


async def _process_external_event_delivery_unbounded(
    claim: _ExternalEventDeliveryClaim,
) -> str:
    try:
        async with asyncio.timeout(_EXTERNAL_EVENT_ATTEMPT_TIMEOUT_SECONDS):
            if claim.workspace_id:
                from packages.core.database import async_session
                from packages.core.models.workspace import Workspace

                async with async_session() as db:
                    workspace = (await db.execute(
                        select(Workspace)
                        .where(
                            Workspace.id == claim.workspace_id,
                            Workspace.entity_id == claim.entity_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )).scalar_one_or_none()
                    if (
                        workspace is None
                        or workspace.deleted_at is not None
                        or workspace.status != "active"
                    ):
                        outcome = await _finish_external_event_delivery_in_session(
                            db,
                            claim,
                            outcome=NotificationOutboxStatus.CANCELED.value,
                            when=datetime.now(timezone.utc),
                            error="workspace inactive or deleted before external event delivery",
                        )
                        await db.commit()
                        return outcome
                    outcome = await _deliver_and_finish_external_event(
                        claim,
                        db=db,
                    )
                    await db.commit()
                    return outcome
            return await _deliver_and_finish_external_event(claim)
    except asyncio.CancelledError:
        # The processing lease is the durable recovery marker. A later sweep
        # reclaims it without making the committed business task run again.
        raise
    except Exception as exc:  # noqa: BLE001
        return await _finish_external_event_delivery(
            claim,
            outcome=NotificationOutboxStatus.FAILED.value,
            when=datetime.now(timezone.utc),
            error=str(exc),
        )


async def _process_external_event_delivery(
    claim: _ExternalEventDeliveryClaim,
) -> str:
    # One delivery can hold the Workspace lifecycle session while opening
    # additional durable sink sessions. Apply backpressure before any of those
    # connections are checked out so a burst of committed artifact events
    # cannot starve foreground workflow credit and governance checks.
    async with _external_event_delivery_semaphore():
        return await _process_external_event_delivery_unbounded(claim)


async def dispatch_due_external_events(
    *,
    batch_size: int = 10,
    event_ids: tuple[str, ...] | None = None,
    when: datetime | None = None,
) -> dict[str, int]:
    """Claim and deliver a bounded batch from the durable EventLog outbox."""
    from packages.core.database import async_session

    current_time = when or datetime.now(timezone.utc)
    async with async_session() as db:
        claims, exhausted, canceled = await _claim_external_event_deliveries(
            db,
            when=current_time,
            batch_size=batch_size,
            event_ids=event_ids,
        )
    outcomes = await asyncio.gather(
        *(_process_external_event_delivery(claim) for claim in claims)
    )
    counts = {
        "claimed": len(claims),
        "delivered": outcomes.count(NotificationOutboxStatus.DELIVERED.value),
        "retried": outcomes.count("retried"),
        "failed": exhausted + outcomes.count(NotificationOutboxStatus.FAILED.value),
        "canceled": canceled + outcomes.count(NotificationOutboxStatus.CANCELED.value),
        "stale": outcomes.count("stale"),
    }
    return counts


async def _deliver_committed_external_events(event_ids: tuple[str, ...]) -> None:
    """Fast-path committed delivery; the durable sweep is the fallback."""
    if not event_ids:
        return
    await dispatch_due_external_events(
        event_ids=event_ids,
        batch_size=min(len(event_ids), 50),
    )


def _track_event_task(
    task: asyncio.Task[None],
    task_set: set[asyncio.Task[None]],
    *,
    label: str,
) -> None:
    task_set.add(task)

    def finished(done: asyncio.Task[None]) -> None:
        task_set.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            logger.warning("%s was cancelled", label)
        except Exception:  # noqa: BLE001
            logger.warning("%s failed", label, exc_info=True)

    task.add_done_callback(finished)


def _track_event_persistence(task: asyncio.Task[None]) -> None:
    _track_event_task(task, _EVENT_PERSISTENCE_TASKS, label="Event persistence")


def _track_external_event_delivery(task: asyncio.Task[None]) -> None:
    _track_event_task(
        task,
        _EXTERNAL_EVENT_DELIVERY_TASKS,
        label="Committed external event delivery",
    )


async def _drain_current_loop_tasks(
    task_set: set[asyncio.Task[None]],
    *,
    timeout_seconds: float,
    label: str,
) -> None:
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(timeout_seconds):
            while pending := tuple(
                task
                for task in task_set
                if task.get_loop() is loop and not task.done()
            ):
                await asyncio.gather(*pending, return_exceptions=True)
    except TimeoutError:
        pending = tuple(
            task
            for task in task_set
            if task.get_loop() is loop and not task.done()
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning("Timed out draining %s (%d task(s))", label, len(pending))


async def drain_external_event_deliveries(
    *,
    persistence_timeout_seconds: float | None = None,
    delivery_timeout_seconds: float | None = None,
) -> None:
    """Bound worker shutdown while preserving delivery through EventLog state."""
    await _drain_current_loop_tasks(
        _EVENT_PERSISTENCE_TASKS,
        timeout_seconds=(
            _WORKER_EVENT_PERSISTENCE_TIMEOUT_SECONDS
            if persistence_timeout_seconds is None
            else persistence_timeout_seconds
        ),
        label="event persistence",
    )
    await _drain_current_loop_tasks(
        _EXTERNAL_EVENT_DELIVERY_TASKS,
        timeout_seconds=(
            _WORKER_EXTERNAL_EVENT_DRAIN_TIMEOUT_SECONDS
            if delivery_timeout_seconds is None
            else delivery_timeout_seconds
        ),
        label="external event delivery",
    )


def _schedule_external_events_after_commit(session) -> None:
    """Schedule delivery only for the outermost successful transaction."""
    if session.in_nested_transaction():
        return
    queued = tuple(dict.fromkeys(session.info.pop(_EXTERNAL_EVENT_QUEUE_KEY, ())))
    if not queued:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _track_external_event_delivery(
        loop.create_task(_deliver_committed_external_events(queued)),
    )


def _clear_external_events_after_rollback(session, _previous_transaction) -> None:
    """Discard queued delivery after an outer rollback; keep savepoint entries.

    Savepoint entries are retained until the outer commit, where the committed
    EventLog lookup filters any rows that the savepoint rolled back.
    """
    if not session.in_transaction():
        session.info.pop(_EXTERNAL_EVENT_QUEUE_KEY, None)


def _queue_external_event_after_commit(db: AsyncSession, event_id: str) -> None:
    sync_session = db.sync_session
    sync_session.info.setdefault(_EXTERNAL_EVENT_QUEUE_KEY, []).append(event_id)
    if sync_session.info.get(_EXTERNAL_EVENT_LISTENERS_KEY):
        return
    event.listen(sync_session, "after_commit", _schedule_external_events_after_commit)
    event.listen(
        sync_session,
        "after_soft_rollback",
        _clear_external_events_after_rollback,
    )
    sync_session.info[_EXTERNAL_EVENT_LISTENERS_KEY] = True


async def emit_in_session(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    *,
    source: str | None = None,
    payload: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    notify: bool = True,
    deliver_after_commit: bool = False,
) -> int:
    """Persist an event and run same-transaction notification fan-out.

    Use this from code that already owns a DB transaction. When
    ``deliver_after_commit`` is true, external delivery is queued for the
    outermost commit and skipped if this event row is rolled back.
    """
    from packages.core.services.event_service import log_event

    event_payload = dict(payload or {})
    entry = await log_event(
        db,
        entity_id,
        event_type,
        source=source,
        payload=event_payload,
    )
    entry.workspace_id = await _resolve_event_workspace_id(
        db,
        entity_id=entity_id,
        payload=event_payload,
        workspace_id=workspace_id,
    )
    if deliver_after_commit:
        entry.external_delivery_status = NotificationOutboxStatus.PENDING.value
        entry.external_delivery_attempt_count = 0
        entry.external_delivery_available_at = datetime.now(timezone.utc)
        entry.external_delivery_locked_until = None
        entry.external_delivery_claim_token = None
        entry.external_delivery_delivered_at = None
        entry.external_delivery_last_error = None
        entry.external_delivery_completed_sinks = {}
        _queue_external_event_after_commit(db, entry.id)
    if not notify:
        return 0

    try:
        from packages.core.services.task_event_notifications import notify_task_event
        return await notify_task_event(db, entity_id, event_type, event_payload)
    except Exception as e:
        logger.debug("Task event notification failed: %s", e)
        return 0


async def deliver_webhook_event(
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Deliver an already-committed domain event to webhook subscribers."""
    from packages.core.services.webhook_service import deliver_event

    result = await deliver_event(entity_id, event_type, payload or {})
    if int((result or {}).get("failed", 0)) > 0:
        raise RuntimeError(f"webhook delivery failed: {result}")


async def deliver_task_external_event(
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    completed_sinks: dict[str, Any] | None = None,
    mark_sink_completed=None,
) -> None:
    """Deliver committed task events to configured external channels."""
    from packages.core.services.task_external_notifications import (
        deliver_task_external_notifications,
    )

    await deliver_task_external_notifications(
        entity_id,
        event_type,
        payload or {},
        completed_sinks=completed_sinks,
        mark_sink_completed=mark_sink_completed,
    )


def emit(
    entity_id: str,
    event_type: str,
    source: str | None = None,
    payload: dict[str, Any] | None = None,
):
    """Fire-and-forget event log.

    Creates an asyncio task to log the event without blocking the caller.
    Safe to call from sync or async code.
    """
    async def _log():
        try:
            from packages.core.database import async_session
            async with async_session() as db:
                await emit_in_session(
                    db,
                    entity_id,
                    event_type,
                    source=source,
                    payload=payload,
                    deliver_after_commit=True,
                )
                await db.commit()
        except Exception as e:
            logger.debug("Event emission failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        _track_event_persistence(loop.create_task(_log()))
    except RuntimeError:
        pass  # No event loop -- skip (e.g. during testing)
