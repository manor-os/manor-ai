"""Durable fair queue and stateful routing for external Sandbox runners."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from packages.core.config import get_settings
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
    SandboxInstance,
    SandboxRequesterKind,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.services.runtime_run_terminal import finish_runtime_run_out_of_loop


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
# A ticket that owns (or is acquiring) a runner slot. The per-user fairness
# rule in ``claim_next_sandbox_allocation`` counts these.
_IN_FLIGHT_RESERVATION_STATUSES = (
    SandboxReservationStatus.ALLOCATING.value,
    SandboxReservationStatus.ALLOCATED.value,
)

# A CONSUMED reservation no longer pins a runner slot: since warm reuse, the
# container outlives its reservation, so occupancy is driven by the runner's
# observed ``active_sandboxes`` (health check) once allocation has finished.
_RUNNER_SLOT_RESERVATION_STATUSES = (
    SandboxReservationStatus.ALLOCATING.value,
    SandboxReservationStatus.ALLOCATED.value,
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
    run: RuntimeRun | None = None,
    tool_call_id: str,
    request_payload: dict[str, Any],
    priority: int = 100,
    now: datetime | None = None,
    max_pending: int | None = None,
    max_wait_seconds: int | None = None,
    activate: bool = True,
    entity_id: str | None = None,
    user_id: str | None = None,
    requester_kind: str = SandboxRequesterKind.RUN.value,
    requester_id: str | None = None,
) -> SandboxReservation:
    """Create one idempotent ticket and enforce bounded pending admission.

    A durable Runtime run passes ``run`` and is suspended on the ticket. A task
    worker has no ``RuntimeRun`` at all: it passes ``run=None`` plus its own
    identity and polls the ticket instead of being resumed by the outbox.
    """

    if run is not None:
        requester_kind = SandboxRequesterKind.RUN.value
        requester_id = run.id
        owner_entity_id = run.entity_id
        owner_user_id = run.user_id
    else:
        if requester_kind != SandboxRequesterKind.TASK.value:
            raise ValueError("a reservation without a run must be requester_kind='task'")
        if not requester_id:
            raise ValueError("a task reservation requires requester_id")
        if not entity_id or not user_id:
            raise ValueError("a task reservation requires entity_id and user_id")
        owner_entity_id = entity_id
        owner_user_id = user_id

    settings = get_settings()
    current_time = now or datetime.now(timezone.utc)
    global_limit = max_pending or settings.SANDBOX_QUEUE_MAX_PENDING
    wait_seconds = max_wait_seconds or settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS

    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('manor:sandbox:queue'))"))
    if run is not None:
        idempotency_filters = (
            SandboxReservation.runtime_run_id == run.id,
            SandboxReservation.tool_call_id == tool_call_id,
        )
    else:
        idempotency_filters = (
            SandboxReservation.requester_kind == requester_kind,
            SandboxReservation.requester_id == requester_id,
            SandboxReservation.tool_call_id == tool_call_id,
        )
    existing = (
        await db.execute(select(SandboxReservation).where(*idempotency_filters))
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
    # No per-user admission cap: one user's second ticket is queued behind
    # their first and serialised at claim time (``claim_next_sandbox_allocation``),
    # so the requester never has to wait in-process, holding a worker slot,
    # for its own earlier ticket to drain.

    reservation = SandboxReservation(
        runtime_run_id=run.id if run is not None else None,
        root_run_id=run.root_run_id if run is not None else None,
        requester_kind=requester_kind,
        requester_id=requester_id,
        tool_call_id=tool_call_id,
        entity_id=owner_entity_id,
        user_id=owner_user_id,
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
    if run is not None:
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
                            # A ticket whose run never reached its checkpoint
                            # (the worker died, or the acquiring call could
                            # not suspend) is nobody's: nothing activates it.
                            SandboxReservationStatus.PENDING_CHECKPOINT.value,
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
        # Task requesters have no run to fail; the poller observes EXPIRED.
        run = (
            (
                await db.execute(
                    select(RuntimeRun)
                    .where(RuntimeRun.id == reservation.runtime_run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reservation.runtime_run_id
            else None
        )
        # Only the run still parked on this very ticket times out with it; a
        # run that moved on to a later reservation must not be failed by a
        # stale one it abandoned.
        if run is not None and run.reservation_id == reservation.id:
            await finish_runtime_run_out_of_loop(
                db,
                run,
                status=RuntimeRunStatus.FAILED,
                reason="sandbox_queue_timeout",
                now=current_time,
            )
    return len(rows) + await _release_abandoned_task_allocations(db, now=current_time)


async def _release_abandoned_task_allocations(
    db: AsyncSession,
    *,
    now: datetime,
) -> int:
    """Return the runner slots of task tickets nobody ever consumed.

    A task requester polls its own ticket instead of being resumed, so when
    the poller gives up (or its worker dies) between ALLOCATED and CONSUMED,
    nothing moves the ticket again: ALLOCATED is in
    ``_RUNNER_SLOT_RESERVATION_STATUSES`` and neither the expiry sweep nor the
    stale-allocation recovery looks at it, so the slot is pinned forever.

    Once the poller's whole wait budget plus two poll intervals have passed no
    poller can still be waiting on this ticket, so the reservation is released
    and the runner's coordinator slot returned. The *container* is not
    destroyed here: the runner's own idle reaper owns that, and until it runs
    the runner's observed ``active_sandboxes`` keeps the capacity counted, so
    releasing the ticket cannot overcommit the runner.
    """

    settings = get_settings()
    abandoned_after = timedelta(
        seconds=settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS
        + 2 * settings.SANDBOX_QUEUE_POLL_SECONDS
    )
    rows = list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(
                    SandboxReservation.requester_kind
                    == SandboxRequesterKind.TASK.value,
                    SandboxReservation.status
                    == SandboxReservationStatus.ALLOCATED.value,
                    SandboxReservation.sandbox_id.is_not(None),
                    SandboxReservation.allocated_at <= now - abandoned_after,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reservation in rows:
        released = await release_sandbox_instance(
            db,
            sandbox_id=reservation.sandbox_id,
            now=now,
        )
        if not released:
            # No active instance to release (already reaped, or never
            # recorded): retire the ticket here so it is not re-swept forever.
            reservation.status = SandboxReservationStatus.RELEASED.value
            reservation.completed_at = now
            reservation.version += 1
        reservation.last_error = "task_consume_timeout"
    return len(rows)


async def claim_next_sandbox_allocation(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    allocation_lease_seconds: int | None = None,
    max_in_flight_per_user: int | None = None,
) -> tuple[SandboxReservation, SandboxRunner] | None:
    """Claim the fairest ticket and reserve one runner slot atomically.

    Fairness across users is enforced here rather than at admission: a user
    may hold at most ``SANDBOX_QUEUE_MAX_PENDING_PER_USER`` tickets in flight
    (allocating or allocated), and their further tickets stay queued while
    other users' tickets are claimed ahead of them. Queued tickets cost
    nothing but a row, so a requester never waits in-process for its own
    earlier ticket to drain.
    """

    settings = get_settings()
    current_time = now or datetime.now(timezone.utc)
    lease_seconds = max(1, allocation_lease_seconds or settings.SANDBOX_ALLOCATION_LEASE_SECONDS)
    user_limit = max(1, max_in_flight_per_user or settings.SANDBOX_QUEUE_MAX_PENDING_PER_USER)
    in_flight = aliased(SandboxReservation)
    user_in_flight = (
        select(func.count(in_flight.id))
        .where(
            in_flight.user_id == SandboxReservation.user_id,
            in_flight.status.in_(_IN_FLIGHT_RESERVATION_STATUSES),
        )
        .correlate(SandboxReservation)
        .scalar_subquery()
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
                user_in_flight < user_limit,
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
        if will_fail and reservation.runtime_run_id:
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
            if run is not None:
                await finish_runtime_run_out_of_loop(
                    db,
                    run,
                    status=RuntimeRunStatus.FAILED,
                    reason="sandbox_allocation_failed",
                    now=current_time,
                )
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
    # A task requester has no run to resume: its worker polls the reservation,
    # so allocation commits the route and stops there.
    if reservation.runtime_run_id:
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
    if will_fail and reservation.runtime_run_id:
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
        if run is not None:
            await finish_runtime_run_out_of_loop(
                db,
                run,
                status=RuntimeRunStatus.FAILED,
                reason="sandbox_allocation_failed",
                now=current_time,
            )
    else:
        reservation.status = SandboxReservationStatus.REQUEUED.value
        reservation.next_attempt_at = current_time + timedelta(seconds=retry_delay_seconds)
    return reservation


async def load_sandbox_reservation(
    db: AsyncSession,
    reservation_id: str,
) -> SandboxReservation | None:
    """Read one ticket without locking, for a requester polling its own slot."""

    return (
        await db.execute(
            select(SandboxReservation).where(SandboxReservation.id == reservation_id)
        )
    ).scalar_one_or_none()


async def consume_sandbox_reservation(
    db: AsyncSession,
    *,
    reservation_id: str,
    now: datetime | None = None,
) -> SandboxReservation:
    """Latch an allocated ticket as consumed once its Sandbox is bound.

    Idempotent: a ticket already CONSUMED is returned unchanged, so a retried
    attach does not bump the version twice. Any other status is a caller bug
    (the Sandbox was never allocated to this ticket) and raises.
    """

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
    if reservation.status == SandboxReservationStatus.CONSUMED.value:
        return reservation
    if reservation.status != SandboxReservationStatus.ALLOCATED.value:
        raise SandboxAllocationStateError("reservation_not_allocated")
    reservation.status = SandboxReservationStatus.CONSUMED.value
    reservation.consumed_at = current_time
    reservation.version += 1
    return reservation


async def cancel_queued_sandbox_reservation(
    db: AsyncSession,
    *,
    reservation_id: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Cancel a ticket that has not been claimed yet; return whether it moved."""

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(SandboxReservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        return False
    if reservation.status not in {
        SandboxReservationStatus.QUEUED.value,
        SandboxReservationStatus.REQUEUED.value,
        SandboxReservationStatus.PENDING_CHECKPOINT.value,
    }:
        # ALLOCATING and later own a runner slot; only the allocation path may
        # release those, so a poller giving up must not touch them here.
        return False
    reservation.status = SandboxReservationStatus.CANCELLED.value
    reservation.completed_at = current_time
    reservation.last_error = str(reason)[:2000]
    reservation.version += 1
    return True


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
        raise SandboxRouteNotFoundError(
            f"no active runner route for sandbox {sandbox_id}: its instance row is missing or released"
        )
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


#: An active instance row younger than this may describe a container the
#: runner's listing raced with (created after the listing was taken).
_VANISHED_INSTANCE_GRACE_SECONDS = 120


async def release_vanished_sandbox_instances(
    db: AsyncSession,
    *,
    runner_id: str,
    live_sandbox_ids: Iterable[str],
    now: datetime | None = None,
    grace_seconds: int = _VANISHED_INSTANCE_GRACE_SECONDS,
) -> list[str]:
    """Release instance rows for containers the runner no longer has.

    The runner's idle reaper (and a crash) removes containers without telling
    the coordinator, so an instance row stays ``active`` and its reservation
    can sit in ``release_pending`` forever, pinning a runner slot through
    ``reconciled_runner_active_count``. The runner's listing is the truth:
    every active row older than the grace period whose container is not
    listed is released here. Returns the released sandbox ids.
    """

    current_time = now or datetime.now(timezone.utc)
    live = {str(sandbox_id) for sandbox_id in live_sandbox_ids}
    rows = list(
        (
            await db.execute(
                select(SandboxInstance)
                .where(
                    SandboxInstance.runner_id == runner_id,
                    SandboxInstance.status == "active",
                    SandboxInstance.created_at <= current_time - timedelta(seconds=grace_seconds),
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    released: list[str] = []
    for instance in rows:
        if instance.sandbox_id in live:
            continue
        if await release_sandbox_instance(db, sandbox_id=instance.sandbox_id, now=current_time):
            released.append(instance.sandbox_id)
    return released


async def restore_consumed_reservation(
    db: AsyncSession,
    *,
    sandbox_id: str,
    now: datetime | None = None,
) -> bool:
    """Put a warm-kept container's reservation back to ``consumed``.

    A run cancel marks its reservation ``release_pending`` before it knows
    whether the container is kept warm for reuse. A kept container is not
    released, so the ticket must not stay in a slot-pinning state: a consumed
    ticket lets the runner's observed count drive occupancy again.
    """

    instance = (
        await db.execute(
            select(SandboxInstance)
            .where(SandboxInstance.sandbox_id == sandbox_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if instance is None:
        return False
    reservation = await db.get(SandboxReservation, instance.reservation_id)
    if reservation is None or reservation.status != SandboxReservationStatus.RELEASE_PENDING.value:
        return False
    reservation.status = SandboxReservationStatus.CONSUMED.value
    reservation.version += 1
    return True


__all__ = [
    "SandboxAllocationStateError",
    "SandboxQueueFullError",
    "SandboxRouteNotFoundError",
    "cancel_queued_sandbox_reservation",
    "claim_due_sandbox_runner_health_checks",
    "claim_next_sandbox_allocation",
    "complete_sandbox_allocation",
    "configured_sandbox_runners",
    "consume_sandbox_reservation",
    "effective_runner_limits",
    "enqueue_sandbox_reservation",
    "expire_sandbox_reservations",
    "fail_sandbox_allocation",
    "load_sandbox_reservation",
    "reconciled_runner_active_count",
    "recover_stale_sandbox_allocations",
    "release_vanished_sandbox_instances",
    "restore_consumed_reservation",
    "reconcile_sandbox_runners",
    "release_sandbox_instance",
    "resolve_sandbox_runner",
]
