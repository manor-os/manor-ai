"""Transactional lifecycle helpers for durable Runtime runs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import logging

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import is_master_agent
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_references,
)


class RuntimeRunNotFoundError(LookupError):
    pass


logger = logging.getLogger(__name__)


MAX_RUNTIME_RUN_RECOVERY_ATTEMPTS = 3


async def create_runtime_run(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    execution_payload: dict[str, Any],
    assistant_message_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    parent_run_id: str | None = None,
    root_run_id: str | None = None,
) -> RuntimeRun:
    """Stage a new durable run; the active-root index arbitrates races."""

    if agent_id and not is_master_agent(agent_id):
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            agent_ids=(agent_id,),
        )

    run_id = generate_ulid()
    resolved_root_id = root_run_id or run_id
    run = RuntimeRun(
        id=run_id,
        root_run_id=resolved_root_id,
        parent_run_id=parent_run_id,
        conversation_id=conversation_id,
        assistant_message_id=assistant_message_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        status=RuntimeRunStatus.QUEUED.value,
        execution_payload=dict(execution_payload),
    )
    db.add(run)
    if parent_run_id is None:
        db.add(
            RuntimeOutboxEvent(
                event_type="runtime.execute_run",
                aggregate_id=run.id,
                dedupe_key=f"runtime.execute_run:{run.id}:1",
                payload={"run_id": run.id},
                available_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
        )
    return run


async def claim_runtime_run_execution(
    db: AsyncSession,
    *,
    run_id: str,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = 60,
    expected_reservation_id: str | None = None,
    expected_reservation_version: int | None = None,
) -> RuntimeRun | None:
    """Claim a queued/resumable run without allowing concurrent execution."""

    current_time = now or datetime.now(timezone.utc)
    run = (
        await db.execute(
            select(RuntimeRun).where(RuntimeRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or run.status in RuntimeRunStatus.terminal():
        return None
    if run.status in {RuntimeRunStatus.CANCEL_REQUESTED.value, RuntimeRunStatus.CANCELLING.value}:
        run.status = RuntimeRunStatus.CANCELLED.value
        run.completed_at = current_time
        run.lease_owner = None
        run.lease_expires_at = None
        run.version += 1
        return None
    if run.lease_expires_at is not None and run.lease_expires_at > current_time:
        return None
    if run.status == RuntimeRunStatus.WAITING_RESOURCE.value:
        if not run.checkpoint or not run.reservation_id:
            return None
        reservation = await db.get(SandboxReservation, run.reservation_id)
        if reservation is None or reservation.status != SandboxReservationStatus.ALLOCATED.value:
            return None
        if expected_reservation_id and reservation.id != expected_reservation_id:
            return None
        if (
            expected_reservation_version is not None
            and reservation.version != expected_reservation_version
        ):
            return None
        run.status = RuntimeRunStatus.RESUMING.value
    elif run.status not in {
        RuntimeRunStatus.QUEUED.value,
        RuntimeRunStatus.RUNNING.value,
        RuntimeRunStatus.RESUMING.value,
    }:
        return None
    else:
        run.status = RuntimeRunStatus.RUNNING.value
    run.started_at = run.started_at or current_time
    run.lease_owner = worker_id
    run.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
    run.version += 1
    return run


async def renew_runtime_run_lease(
    db: AsyncSession,
    *,
    run_id: str,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = 60,
) -> bool:
    current_time = now or datetime.now(timezone.utc)
    run = (
        await db.execute(
            select(RuntimeRun).where(RuntimeRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if (
        run is None
        or run.status in RuntimeRunStatus.terminal()
        or run.lease_owner != worker_id
    ):
        return False
    run.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
    return True


async def recover_expired_runtime_run_leases(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = 20,
    max_recovery_attempts: int = MAX_RUNTIME_RUN_RECOVERY_ATTEMPTS,
) -> tuple[int, int]:
    """Requeue root Runtime runs whose executing worker stopped renewing.

    Redis can retain a late-acked Celery delivery until the global visibility
    timeout expires. Runtime leases are intentionally much shorter, so a
    rolling deploy must create a fresh delivery instead of leaving the chat in
    ``running`` for that entire broker window. Versioned outbox keys make the
    recovery idempotent; a bounded attempt count prevents repeated worker loss
    from becoming an automatic retry loop.
    """

    current_time = now or datetime.now(timezone.utc)
    limit = max(1, min(int(batch_size), 100))
    attempts_limit = max(0, int(max_recovery_attempts))
    runs = list(
        (
            await db.execute(
                select(RuntimeRun)
                .where(
                    RuntimeRun.parent_run_id.is_(None),
                    RuntimeRun.status.in_(
                        (
                            RuntimeRunStatus.RUNNING.value,
                            RuntimeRunStatus.RESUMING.value,
                            RuntimeRunStatus.CANCEL_REQUESTED.value,
                            RuntimeRunStatus.CANCELLING.value,
                        )
                    ),
                    RuntimeRun.lease_expires_at.is_not(None),
                    RuntimeRun.lease_expires_at <= current_time,
                )
                .order_by(RuntimeRun.lease_expires_at.asc(), RuntimeRun.id.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    recovery_counts: dict[str, int] = {}
    if runs:
        count_rows = (
            await db.execute(
                select(
                    RuntimeOutboxEvent.aggregate_id,
                    func.count(RuntimeOutboxEvent.id),
                )
                .where(
                    RuntimeOutboxEvent.aggregate_id.in_([run.id for run in runs]),
                    RuntimeOutboxEvent.event_type == "runtime.execute_run",
                    RuntimeOutboxEvent.dedupe_key.contains(":recovery:"),
                )
                .group_by(RuntimeOutboxEvent.aggregate_id)
            )
        ).all()
        recovery_counts = {
            aggregate_id: int(count) for aggregate_id, count in count_rows
        }

    recovered = 0
    failed = 0
    for run in runs:
        run.lease_owner = None
        run.lease_expires_at = None
        run.version += 1

        if run.status in {
            RuntimeRunStatus.CANCEL_REQUESTED.value,
            RuntimeRunStatus.CANCELLING.value,
        }:
            run.status = RuntimeRunStatus.CANCELLED.value
            run.status_reason = "user_cancelled"
            run.completed_at = current_time
            continue

        recovery_attempts = recovery_counts.get(run.id, 0)
        if recovery_attempts >= attempts_limit:
            run.status = RuntimeRunStatus.FAILED.value
            run.status_reason = "runtime_recovery_exhausted"
            run.error = {
                "code": "runtime_recovery_exhausted",
                "message": "The runtime worker stopped before the request completed.",
            }
            run.completed_at = current_time
            failed += 1
            continue

        run.status = RuntimeRunStatus.QUEUED.value
        run.status_reason = None
        db.add(
            RuntimeOutboxEvent(
                event_type="runtime.execute_run",
                aggregate_id=run.id,
                dedupe_key=f"runtime.execute_run:{run.id}:recovery:{run.version}",
                payload={"run_id": run.id},
                available_at=current_time,
                created_at=current_time,
            )
        )
        recovered += 1
    return recovered, failed


async def complete_runtime_run_execution(
    db: AsyncSession,
    *,
    run_id: str,
    worker_id: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> RuntimeRun:
    """Commit a terminal result once; late delivery can only observe it."""

    current_time = now or datetime.now(timezone.utc)
    run = (
        await db.execute(
            select(RuntimeRun).where(RuntimeRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise RuntimeRunNotFoundError(run_id)
    if run.status in RuntimeRunStatus.terminal():
        return run
    if run.lease_owner != worker_id:
        raise RuntimeError("runtime_run_lease_lost")
    if run.cancel_requested_at is not None or run.status in {
        RuntimeRunStatus.CANCEL_REQUESTED.value,
        RuntimeRunStatus.CANCELLING.value,
    }:
        run.status = RuntimeRunStatus.CANCELLED.value
        run.status_reason = "user_cancelled"
    elif error is not None:
        run.status = RuntimeRunStatus.FAILED.value
        run.status_reason = str(error.get("code") or "runtime_execution_failed")
        run.error = dict(error)
    else:
        run.status = RuntimeRunStatus.COMPLETED.value
        run.status_reason = None
        run.result = dict(result or {})
    run.completed_at = current_time
    run.lease_owner = None
    run.lease_expires_at = None
    run.active_sandbox_id = None
    run.active_execution_id = None
    run.version += 1
    return run


async def set_runtime_run_active_execution(
    db: AsyncSession,
    *,
    run_id: str,
    sandbox_id: str,
    execution_id: str,
) -> None:
    run = (
        await db.execute(
            select(RuntimeRun).where(RuntimeRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or run.status in RuntimeRunStatus.terminal():
        return
    run.active_sandbox_id = sandbox_id
    run.active_execution_id = execution_id
    run.version += 1


async def clear_runtime_run_active_execution(
    db: AsyncSession,
    *,
    run_id: str,
    execution_id: str,
) -> None:
    run = (
        await db.execute(
            select(RuntimeRun).where(RuntimeRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or run.active_execution_id != execution_id:
        return
    run.active_sandbox_id = None
    run.active_execution_id = None
    run.version += 1


async def project_runtime_run_status(
    db: AsyncSession,
    run: RuntimeRun,
    *,
    poll_after_seconds: int,
) -> dict[str, Any]:
    """Build the stable public status projection without exposing checkpoints."""

    queue: dict[str, Any] | None = None
    if run.reservation_id:
        reservation = (
            await db.execute(
                select(SandboxReservation).where(
                    SandboxReservation.id == run.reservation_id
                )
            )
        ).scalar_one_or_none()
        if reservation is not None:
            position: int | None = None
            if reservation.status in {
                SandboxReservationStatus.QUEUED.value,
                SandboxReservationStatus.REQUEUED.value,
            }:
                ahead = await db.scalar(
                    select(func.count(SandboxReservation.id)).where(
                        SandboxReservation.status.in_(
                            [
                                SandboxReservationStatus.QUEUED.value,
                                SandboxReservationStatus.REQUEUED.value,
                            ]
                        ),
                        or_(
                            SandboxReservation.priority < reservation.priority,
                            and_(
                                SandboxReservation.priority == reservation.priority,
                                SandboxReservation.enqueued_at < reservation.enqueued_at,
                            ),
                            and_(
                                SandboxReservation.priority == reservation.priority,
                                SandboxReservation.enqueued_at == reservation.enqueued_at,
                                SandboxReservation.id < reservation.id,
                            ),
                        ),
                    )
                )
                position = int(ahead or 0) + 1
            queue = {
                "ticket": reservation.id,
                "position": position,
                "eta_seconds": None,
                "poll_after_seconds": poll_after_seconds,
                "deadline_at": reservation.deadline_at.isoformat(),
            }

    return {
        "id": run.id,
        "root_run_id": run.root_run_id,
        "conversation_id": run.conversation_id,
        "status": run.status,
        "status_reason": run.status_reason,
        "queue": queue,
        "cancel_requested_at": (
            run.cancel_requested_at.isoformat() if run.cancel_requested_at else None
        ),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "version": run.version,
    }


async def request_runtime_run_cancel(
    db: AsyncSession,
    *,
    run_id: str,
    entity_id: str,
    user_id: str | None = None,
    reason: str = "user_cancelled",
) -> RuntimeRun:
    """Idempotently cancel a root run, its live descendants, and reservations."""

    requested_identity = (
        await db.execute(
            select(RuntimeRun.root_run_id, RuntimeRun.user_id)
            .where(RuntimeRun.id == run_id, RuntimeRun.entity_id == entity_id)
        )
    ).one_or_none()
    if requested_identity is None or (
        user_id is not None and requested_identity.user_id != user_id
    ):
        raise RuntimeRunNotFoundError(run_id)

    root_id = requested_identity.root_run_id
    reservations = list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(SandboxReservation.root_run_id == root_id)
                .order_by(SandboxReservation.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    runs = list(
        (
            await db.execute(
                select(RuntimeRun)
                .where(RuntimeRun.root_run_id == root_id)
                .order_by(RuntimeRun.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    requested = next((run for run in runs if run.id == run_id), None)
    if requested is None:
        raise RuntimeRunNotFoundError(run_id)
    now = datetime.now(timezone.utc)
    for run in runs:
        if run.status in RuntimeRunStatus.terminal():
            continue
        run.cancel_requested_at = run.cancel_requested_at or now
        if run.status in {
            RuntimeRunStatus.QUEUED.value,
            RuntimeRunStatus.WAITING_RESOURCE.value,
        }:
            run.status = RuntimeRunStatus.CANCELLED.value
            run.completed_at = now
            run.lease_owner = None
            run.lease_expires_at = None
        else:
            run.status = RuntimeRunStatus.CANCEL_REQUESTED.value
        run.status_reason = reason
        run.version += 1

    for reservation in reservations:
        if reservation.status in SandboxReservationStatus.terminal():
            continue
        if reservation.sandbox_id:
            reservation.status = SandboxReservationStatus.RELEASE_PENDING.value
        else:
            if reservation.runner_id:
                runner = (
                    await db.execute(
                        select(SandboxRunner)
                        .where(SandboxRunner.id == reservation.runner_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if runner is not None:
                    runner.active_count = max(0, runner.active_count - 1)
                reservation.runner_id = None
            reservation.status = SandboxReservationStatus.CANCELLED.value
            reservation.completed_at = now
        reservation.version += 1

    return requested


async def persist_runtime_run_suspension(
    db: AsyncSession,
    *,
    run_id: str,
    control: dict[str, Any],
) -> RuntimeRun:
    """Commit a tool checkpoint before making its reservation schedulable."""

    checkpoint = control.get("checkpoint")
    reservation_id = str(control.get("reservation_id") or "")
    if not isinstance(checkpoint, dict) or not reservation_id:
        raise ValueError("invalid Runtime suspension control")
    run = (
        await db.execute(
            select(RuntimeRun)
            .where(RuntimeRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise RuntimeRunNotFoundError(run_id)
    if run.status in RuntimeRunStatus.terminal():
        return run
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(
                SandboxReservation.id == reservation_id,
                SandboxReservation.runtime_run_id == run.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise ValueError("Runtime suspension reservation does not belong to run")
    run.checkpoint = dict(checkpoint)
    run.reservation_id = reservation.id
    run.status = RuntimeRunStatus.WAITING_RESOURCE.value
    run.status_reason = str(control.get("reason") or "sandbox_capacity")
    run.version += 1
    run.lease_owner = None
    run.lease_expires_at = None
    if reservation.status == SandboxReservationStatus.PENDING_CHECKPOINT.value:
        reservation.status = SandboxReservationStatus.QUEUED.value
        reservation.version += 1
    return run


async def cancel_runtime_run_resources(run: RuntimeRun) -> None:
    """Best-effort cancellation and cleanup after DB cancellation commits."""

    from packages.core.config import get_settings

    settings = get_settings()
    if settings.SANDBOX_COORDINATION_MODE != "external-runner":
        return
    from packages.core.database import async_session
    from packages.core.models.runtime_run import SandboxInstance, SandboxRunner
    from packages.core.services.sandbox_queue_service import release_sandbox_instance
    from packages.core.services.sandbox_sdk import SandboxClient
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(SandboxInstance, SandboxRunner)
                    .join(SandboxRunner, SandboxRunner.id == SandboxInstance.runner_id)
                    .where(
                        SandboxInstance.root_run_id == run.root_run_id,
                        SandboxInstance.status == "active",
                    )
                )
            ).all()
        )
    for instance, runner in rows:
        try:
            async with SandboxClient(
                base_url=runner.base_url,
                timeout=30.0,
                api_token=settings.SANDBOX_API_TOKEN,
            ) as client:
                if (
                    run.active_sandbox_id == instance.sandbox_id
                    and run.active_execution_id
                ):
                    try:
                        await client.cancel_execution(
                            instance.sandbox_id,
                            run.active_execution_id,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to cancel active Sandbox execution run=%s sandbox=%s",
                            run.id,
                            instance.sandbox_id,
                            exc_info=True,
                        )
                try:
                    await client.destroy(instance.sandbox_id)
                except SandboxNotFoundError:
                    logger.info(
                        "Cancelled Sandbox was already removed run=%s sandbox=%s",
                        run.id,
                        instance.sandbox_id,
                    )
            async with async_session() as release_db:
                await release_sandbox_instance(
                    release_db,
                    sandbox_id=instance.sandbox_id,
                )
                await release_db.commit()
        except Exception:
            logger.warning(
                "Failed to release cancelled Sandbox run=%s sandbox=%s",
                run.id,
                instance.sandbox_id,
                exc_info=True,
            )


__all__ = [
    "MAX_RUNTIME_RUN_RECOVERY_ATTEMPTS",
    "RuntimeRunNotFoundError",
    "claim_runtime_run_execution",
    "cancel_runtime_run_resources",
    "clear_runtime_run_active_execution",
    "complete_runtime_run_execution",
    "create_runtime_run",
    "project_runtime_run_status",
    "recover_expired_runtime_run_leases",
    "renew_runtime_run_lease",
    "persist_runtime_run_suspension",
    "request_runtime_run_cancel",
    "set_runtime_run_active_execution",
]
