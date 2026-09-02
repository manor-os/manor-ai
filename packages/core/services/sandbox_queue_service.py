"""Durable fair queue and stateful routing for external Sandbox runners."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
    SandboxInstance,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)


class SandboxQueueFullError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SandboxRouteNotFoundError(LookupError):
    pass


class SandboxAllocationStateError(RuntimeError):
    pass


_PENDING_RESERVATION_STATUSES = (
    SandboxReservationStatus.PENDING_CHECKPOINT.value,
    SandboxReservationStatus.QUEUED.value,
    SandboxReservationStatus.REQUEUED.value,
    SandboxReservationStatus.ALLOCATING.value,
)

_RUNNER_SLOT_RESERVATION_STATUSES = (
    SandboxReservationStatus.ALLOCATING.value,
    SandboxReservationStatus.ALLOCATED.value,
    SandboxReservationStatus.CONSUMED.value,
    SandboxReservationStatus.RELEASE_PENDING.value,
)
_LEGACY_RUNNER_ACTIVE_LIMIT = 5
_LEGACY_RUNNER_EXECUTING_LIMIT = 2


def effective_runner_limits(
    *,
    observed_active: int,
    observed_executing: int,
    active_cap: int,
    executing_cap: int,
) -> tuple[int, int]:
    """Apply optional coordinator caps to the runner's observed capacity."""

    active = max(1, observed_active)
    if active_cap > 0:
        active = min(active, active_cap)
    executing = min(active, max(1, observed_executing))
    if executing_cap > 0:
        executing = min(executing, executing_cap)
    return active, executing


async def reconciled_runner_active_count(
    db: AsyncSession,
    *,
    runner_id: str,
    observed_active: int,
) -> int:
    """Reconcile runner capacity without dropping in-flight coordinator slots."""

    coordinator_active = await db.scalar(
        select(func.count(SandboxReservation.id)).where(
            SandboxReservation.runner_id == runner_id,
            SandboxReservation.status.in_(_RUNNER_SLOT_RESERVATION_STATUSES),
        )
    )
    return max(int(coordinator_active or 0), max(0, int(observed_active)))


async def claim_due_sandbox_runner_health_checks(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    interval_seconds: int | None = None,
) -> list[str]:
    """Claim due runner probes so scheduler replicas do not duplicate them."""

    current_time = now or datetime.now(timezone.utc)
    interval = max(
        1,
        interval_seconds or get_settings().SANDBOX_RUNNER_HEALTH_INTERVAL_SECONDS,
    )
    runners = list(
        (
            await db.execute(
                select(SandboxRunner)
                .where(
                    SandboxRunner.status != SandboxRunnerStatus.DRAINING.value,
                    or_(
                        SandboxRunner.last_health_at.is_(None),
                        SandboxRunner.last_health_at
                        <= current_time - timedelta(seconds=interval),
                    ),
                )
                .order_by(SandboxRunner.id.asc())
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for runner in runners:
        runner.last_health_at = current_time
    return [runner.id for runner in runners]


def configured_sandbox_runners(
    raw_json: str | None = None,
    *,
    fallback_url: str | None = None,
) -> list[dict[str, Any]]:
    """Parse runner config while retaining the one-runner migration path."""

    settings = get_settings()
    raw = settings.SANDBOX_RUNNERS_JSON if raw_json is None else raw_json
    fallback = settings.SANDBOX_SERVICE_URL if fallback_url is None else fallback_url
    if str(raw or "").strip():
        parsed = json.loads(str(raw))
        if not isinstance(parsed, list):
            raise ValueError("SANDBOX_RUNNERS_JSON must be a JSON list")
    elif str(fallback or "").strip():
        parsed = [{"id": "default", "base_url": str(fallback).strip()}]
    else:
        return []

    runners: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    default_active_cap = max(0, settings.SANDBOX_RUNNER_ACTIVE_LIMIT)
    default_executing_cap = max(0, settings.SANDBOX_RUNNER_EXECUTING_LIMIT)
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("each Sandbox runner must be a JSON object")
        runner_id = str(item.get("id") or "").strip()
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if not runner_id or not base_url.startswith(("http://", "https://")):
            raise ValueError("each Sandbox runner requires id and HTTP(S) base_url")
        if runner_id in seen_ids:
            raise ValueError(f"duplicate Sandbox runner id: {runner_id}")
        seen_ids.add(runner_id)
        active_cap = max(0, int(item.get("active_limit", default_active_cap) or 0))
        executing_cap = max(
            0,
            int(item.get("executing_limit", default_executing_cap) or 0),
        )
        runners.append(
            {
                "id": runner_id,
                "base_url": base_url,
                "active_limit": active_cap,
                "executing_limit": executing_cap,
                "config": dict(item.get("config") or {}),
            }
        )
    return runners


async def reconcile_sandbox_runners(
    db: AsyncSession,
    runner_configs: list[dict[str, Any]],
) -> list[SandboxRunner]:
    """Upsert configured runners and drain rows removed from configuration."""

    configured_ids: set[str] = set()
    rows: list[SandboxRunner] = []
    for config in runner_configs:
        runner_id = str(config["id"])
        configured_ids.add(runner_id)
        runner = (
            await db.execute(
                select(SandboxRunner)
                .where(SandboxRunner.id == runner_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        active_cap = max(0, int(config.get("active_limit") or 0))
        executing_cap = max(0, int(config.get("executing_limit") or 0))
        if runner is None:
            runner = SandboxRunner(
                id=runner_id,
                base_url=str(config["base_url"]),
                active_limit=_LEGACY_RUNNER_ACTIVE_LIMIT,
                executing_limit=_LEGACY_RUNNER_EXECUTING_LIMIT,
            )
            db.add(runner)
        existing_config = dict(runner.config or {})
        observed_active = max(
            1,
            int(existing_config.get("observed_active_limit") or runner.active_limit or 1),
        )
        observed_executing = max(
            1,
            int(
                existing_config.get("observed_executing_limit")
                or runner.executing_limit
                or 1
            ),
        )
        base_url = str(config["base_url"]).rstrip("/")
        url_changed = runner.base_url.rstrip("/") != base_url
        runner.base_url = base_url
        runner.active_limit, runner.executing_limit = effective_runner_limits(
            observed_active=observed_active,
            observed_executing=observed_executing,
            active_cap=active_cap,
            executing_cap=executing_cap,
        )
        runner.config = {
            **dict(config.get("config") or {}),
            "active_cap": active_cap,
            "executing_cap": executing_cap,
            "observed_active_limit": observed_active,
            "observed_executing_limit": observed_executing,
        }
        if url_changed or runner.status == SandboxRunnerStatus.DRAINING.value:
            runner.status = SandboxRunnerStatus.UNKNOWN.value
            runner.last_health_at = None
            runner.last_error = None
        rows.append(runner)

    existing = list(
        (
            await db.execute(select(SandboxRunner).with_for_update())
        ).scalars().all()
    )
    for runner in existing:
        if runner.id not in configured_ids:
            runner.status = SandboxRunnerStatus.DRAINING.value
    return rows


async def enqueue_sandbox_reservation(
    db: AsyncSession,
    *,
    run: RuntimeRun,
    tool_call_id: str,
    request_payload: dict[str, Any],
    priority: int = 100,
    now: datetime | None = None,
    max_pending: int | None = None,
    max_pending_per_user: int | None = None,
    max_wait_seconds: int | None = None,
    activate: bool = True,
) -> SandboxReservation:
    """Create one idempotent ticket and enforce bounded pending admission."""

    settings = get_settings()
    current_time = now or datetime.now(timezone.utc)
    global_limit = max_pending or settings.SANDBOX_QUEUE_MAX_PENDING
    user_limit = max_pending_per_user or settings.SANDBOX_QUEUE_MAX_PENDING_PER_USER
    wait_seconds = max_wait_seconds or settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS

    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('manor:sandbox:queue'))"))
    existing = (
        await db.execute(
            select(SandboxReservation).where(
                SandboxReservation.runtime_run_id == run.id,
                SandboxReservation.tool_call_id == tool_call_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    pending_count = await db.scalar(
        select(func.count(SandboxReservation.id)).where(
            SandboxReservation.status.in_(_PENDING_RESERVATION_STATUSES)
        )
    )
    if int(pending_count or 0) >= global_limit:
        raise SandboxQueueFullError("sandbox_queue_full")
    user_pending_count = await db.scalar(
        select(func.count(SandboxReservation.id)).where(
            SandboxReservation.user_id == run.user_id,
            SandboxReservation.status.in_(_PENDING_RESERVATION_STATUSES),
        )
    )
    if int(user_pending_count or 0) >= user_limit:
        raise SandboxQueueFullError("sandbox_user_queue_limit")

    reservation = SandboxReservation(
        runtime_run_id=run.id,
        root_run_id=run.root_run_id,
        tool_call_id=tool_call_id,
        entity_id=run.entity_id,
        user_id=run.user_id,
        status=(
            SandboxReservationStatus.QUEUED.value
            if activate
            else SandboxReservationStatus.PENDING_CHECKPOINT.value
        ),
        priority=priority,
        enqueued_at=current_time,
        deadline_at=current_time + timedelta(seconds=wait_seconds),
        request_payload=dict(request_payload),
    )
    db.add(reservation)
    await db.flush()
    run.reservation_id = reservation.id
    run.status = RuntimeRunStatus.WAITING_RESOURCE.value
    run.status_reason = "sandbox_capacity"
    run.version += 1
    return reservation


async def expire_sandbox_reservations(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    current_time = now or datetime.now(timezone.utc)
    rows = list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(
                    SandboxReservation.status.in_(
                        [
                            SandboxReservationStatus.QUEUED.value,
                            SandboxReservationStatus.REQUEUED.value,
                        ]
                    ),
                    SandboxReservation.deadline_at <= current_time,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reservation in rows:
        reservation.status = SandboxReservationStatus.EXPIRED.value
        reservation.completed_at = current_time
        reservation.last_error = "sandbox_queue_timeout"
        reservation.version += 1
        run = await db.get(RuntimeRun, reservation.runtime_run_id)
        if run is not None and run.status not in RuntimeRunStatus.terminal():
            run.status = RuntimeRunStatus.FAILED.value
            run.status_reason = "sandbox_queue_timeout"
            run.completed_at = current_time
            run.version += 1
    return len(rows)


async def claim_next_sandbox_allocation(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    allocation_lease_seconds: int | None = None,
) -> tuple[SandboxReservation, SandboxRunner] | None:
    """Claim the fairest ticket and reserve one runner slot atomically."""

    current_time = now or datetime.now(timezone.utc)
    lease_seconds = max(
        1,
        allocation_lease_seconds or get_settings().SANDBOX_ALLOCATION_LEASE_SECONDS,
    )
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(
                SandboxReservation.status.in_(
                    [
                        SandboxReservationStatus.QUEUED.value,
                        SandboxReservationStatus.REQUEUED.value,
                    ]
                ),
                SandboxReservation.deadline_at > current_time,
                or_(
                    SandboxReservation.next_attempt_at.is_(None),
                    SandboxReservation.next_attempt_at <= current_time,
                ),
            )
            .order_by(
                SandboxReservation.priority.asc(),
                SandboxReservation.enqueued_at.asc(),
                SandboxReservation.id.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if reservation is None:
        return None

    runner = (
        await db.execute(
            select(SandboxRunner)
            .where(
                SandboxRunner.status == SandboxRunnerStatus.HEALTHY.value,
                SandboxRunner.active_count < SandboxRunner.active_limit,
            )
            .order_by(SandboxRunner.active_count.asc(), SandboxRunner.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if runner is None:
        return None

    reservation.status = SandboxReservationStatus.ALLOCATING.value
    reservation.runner_id = runner.id
    reservation.attempt_count += 1
    reservation.next_attempt_at = current_time + timedelta(seconds=lease_seconds)
    reservation.version += 1
    runner.active_count += 1
    db.add(
        RuntimeOutboxEvent(
            event_type="runtime.allocate_sandbox",
            aggregate_id=reservation.id,
            dedupe_key=f"runtime.allocate_sandbox:{reservation.id}:{reservation.version}",
            payload={
                "reservation_id": reservation.id,
                "reservation_version": reservation.version,
            },
            available_at=current_time,
            created_at=current_time,
        )
    )
    return reservation, runner


async def recover_stale_sandbox_allocations(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    allocation_lease_seconds: int | None = None,
    retry_delay_seconds: int = 2,
    max_attempts: int = 5,
) -> int:
    """Return slots held by allocation tasks that exceeded their lease."""

    current_time = now or datetime.now(timezone.utc)
    lease_seconds = max(
        1,
        allocation_lease_seconds or get_settings().SANDBOX_ALLOCATION_LEASE_SECONDS,
    )
    rows = list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(
                    SandboxReservation.status == SandboxReservationStatus.ALLOCATING.value,
                    or_(
                        SandboxReservation.next_attempt_at <= current_time,
                        (
                            SandboxReservation.next_attempt_at.is_(None)
                            & (
                                SandboxReservation.updated_at
                                <= current_time - timedelta(seconds=lease_seconds)
                            )
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reservation in rows:
        will_fail = (
            reservation.deadline_at <= current_time
            or reservation.attempt_count >= max_attempts
        )
        run = None
        if will_fail:
            run = (
                await db.execute(
                    select(RuntimeRun)
                    .where(RuntimeRun.id == reservation.runtime_run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
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
        reservation.last_error = "sandbox_allocation_lease_expired"
        reservation.version += 1
        if will_fail:
            reservation.status = SandboxReservationStatus.FAILED.value
            reservation.completed_at = current_time
            reservation.next_attempt_at = None
            if run is not None and run.status not in RuntimeRunStatus.terminal():
                run.status = RuntimeRunStatus.FAILED.value
                run.status_reason = "sandbox_allocation_failed"
                run.completed_at = current_time
                run.version += 1
        else:
            reservation.status = SandboxReservationStatus.REQUEUED.value
            reservation.next_attempt_at = current_time + timedelta(seconds=retry_delay_seconds)
    return len(rows)


async def complete_sandbox_allocation(
    db: AsyncSession,
    *,
    reservation_id: str,
    sandbox_id: str,
    runner_id: str,
    expected_reservation_version: int | None = None,
    now: datetime | None = None,
) -> SandboxReservation:
    """Commit allocation, route, and resume outbox exactly once."""

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(SandboxReservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise SandboxAllocationStateError("reservation_not_found")
    if reservation.status == SandboxReservationStatus.ALLOCATED.value:
        if reservation.sandbox_id == sandbox_id and reservation.runner_id == runner_id:
            return reservation
        raise SandboxAllocationStateError("reservation_already_allocated")
    if (
        expected_reservation_version is not None
        and reservation.version != expected_reservation_version
    ):
        raise SandboxAllocationStateError("reservation_version_mismatch")
    if (
        reservation.status != SandboxReservationStatus.ALLOCATING.value
        or reservation.runner_id != runner_id
    ):
        raise SandboxAllocationStateError("reservation_not_allocating")

    reservation.status = SandboxReservationStatus.ALLOCATED.value
    reservation.sandbox_id = sandbox_id
    reservation.allocated_at = current_time
    reservation.next_attempt_at = None
    reservation.version += 1
    db.add(
        SandboxInstance(
            sandbox_id=sandbox_id,
            reservation_id=reservation.id,
            runtime_run_id=reservation.runtime_run_id,
            root_run_id=reservation.root_run_id,
            runner_id=runner_id,
            status="active",
        )
    )
    run = await db.get(RuntimeRun, reservation.runtime_run_id)
    if run is not None and run.status not in RuntimeRunStatus.terminal():
        run.status = RuntimeRunStatus.WAITING_RESOURCE.value
        run.status_reason = "sandbox_allocated"
        run.version += 1
    db.add(
        RuntimeOutboxEvent(
            event_type="runtime.resume_run",
            aggregate_id=reservation.runtime_run_id,
            dedupe_key=f"runtime.resume_run:{reservation.id}:{reservation.version}",
            payload={
                "run_id": reservation.runtime_run_id,
                "reservation_id": reservation.id,
                "reservation_version": reservation.version,
            },
            available_at=current_time,
            created_at=current_time,
        )
    )
    return reservation


async def fail_sandbox_allocation(
    db: AsyncSession,
    *,
    reservation_id: str,
    error: str,
    expected_reservation_version: int | None = None,
    now: datetime | None = None,
    retry_delay_seconds: int = 2,
    max_attempts: int = 5,
) -> SandboxReservation:
    """Return a reserved runner slot and either requeue or fail the ticket."""

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(SandboxReservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one()
    if (
        reservation.status == SandboxReservationStatus.ALLOCATING.value
        and expected_reservation_version is not None
        and reservation.version != expected_reservation_version
    ):
        return reservation
    if reservation.status != SandboxReservationStatus.ALLOCATING.value:
        if (
            reservation.runner_id
            and reservation.status
            in {
                SandboxReservationStatus.CANCELLED.value,
                SandboxReservationStatus.RELEASE_PENDING.value,
            }
        ):
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
            reservation.last_error = str(error)[:2000]
            reservation.version += 1
        return reservation
    will_fail = (
        reservation.deadline_at <= current_time
        or reservation.attempt_count >= max_attempts
    )
    run = None
    if will_fail:
        run = (
            await db.execute(
                select(RuntimeRun)
                .where(RuntimeRun.id == reservation.runtime_run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
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
    reservation.last_error = str(error)[:2000]
    reservation.version += 1
    reservation.runner_id = None
    if will_fail:
        reservation.status = SandboxReservationStatus.FAILED.value
        reservation.completed_at = current_time
        if run is not None and run.status not in RuntimeRunStatus.terminal():
            run.status = RuntimeRunStatus.FAILED.value
            run.status_reason = "sandbox_allocation_failed"
            run.completed_at = current_time
            run.version += 1
    else:
        reservation.status = SandboxReservationStatus.REQUEUED.value
        reservation.next_attempt_at = current_time + timedelta(seconds=retry_delay_seconds)
    return reservation


async def resolve_sandbox_runner(
    db: AsyncSession,
    sandbox_id: str,
) -> SandboxRunner:
    """Resolve a stateful operation to the runner that owns the Sandbox."""

    row = (
        await db.execute(
            select(SandboxRunner)
            .join(SandboxInstance, SandboxInstance.runner_id == SandboxRunner.id)
            .where(
                SandboxInstance.sandbox_id == sandbox_id,
                SandboxInstance.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SandboxRouteNotFoundError(sandbox_id)
    return row


async def release_sandbox_instance(
    db: AsyncSession,
    *,
    sandbox_id: str,
    now: datetime | None = None,
) -> bool:
    """Mark a destroyed Sandbox released and return its runner reservation."""

    current_time = now or datetime.now(timezone.utc)
    instance = (
        await db.execute(
            select(SandboxInstance)
            .where(SandboxInstance.sandbox_id == sandbox_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if instance is None or instance.status == "released":
        return False
    instance.status = "released"
    instance.released_at = current_time
    reservation = await db.get(SandboxReservation, instance.reservation_id)
    if reservation is not None:
        reservation.status = SandboxReservationStatus.RELEASED.value
        reservation.completed_at = current_time
        reservation.version += 1
    runner = (
        await db.execute(
            select(SandboxRunner)
            .where(SandboxRunner.id == instance.runner_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if runner is not None:
        runner.active_count = max(0, runner.active_count - 1)
    return True


__all__ = [
    "SandboxAllocationStateError",
    "SandboxQueueFullError",
    "SandboxRouteNotFoundError",
    "claim_due_sandbox_runner_health_checks",
    "claim_next_sandbox_allocation",
    "complete_sandbox_allocation",
    "configured_sandbox_runners",
    "effective_runner_limits",
    "enqueue_sandbox_reservation",
    "expire_sandbox_reservations",
    "fail_sandbox_allocation",
    "reconciled_runner_active_count",
    "recover_stale_sandbox_allocations",
    "reconcile_sandbox_runners",
    "release_sandbox_instance",
    "resolve_sandbox_runner",
]
