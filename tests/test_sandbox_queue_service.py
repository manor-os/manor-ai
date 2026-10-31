from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

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
from packages.core.services import sandbox_queue_service
from packages.core.services.sandbox_queue_service import (
    SandboxAllocationStateError,
    SandboxRouteNotFoundError,
    cancel_queued_sandbox_reservation,
    claim_next_sandbox_allocation,
    complete_sandbox_allocation,
    configured_sandbox_runners,
    consume_sandbox_reservation,
    enqueue_sandbox_reservation,
    expire_sandbox_reservations,
    fail_sandbox_allocation,
    resolve_sandbox_runner,
)


@pytest.fixture
async def db_session(db_session):
    """Each queue test owns the whole runner/reservation pool.

    The test database is not truncated between tests, and other suites leave
    healthy runners and queued reservations behind (test_runtime_run_terminal
    inserts ``runner-<reservation id>``). A claim picks across the whole pool,
    so leftovers from whichever files share this shard change its answer.
    """
    from sqlalchemy import delete

    for model in (SandboxInstance, SandboxReservation, SandboxRunner):
        await db_session.execute(delete(model))
    await db_session.flush()
    yield db_session


def test_single_runner_fallback_uses_deployable_capacity_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        sandbox_queue_service,
        "get_settings",
        lambda: SimpleNamespace(
            SANDBOX_RUNNER_ACTIVE_LIMIT=5,
            SANDBOX_RUNNER_EXECUTING_LIMIT=2,
        ),
    )
    assert configured_sandbox_runners(
        raw_json="",
        fallback_url="http://10.0.0.20:8000",
    ) == [
        {
            "id": "default",
            "base_url": "http://10.0.0.20:8000",
            "active_limit": 5,
            "executing_limit": 2,
            "config": {},
            "pool": "default",
        }
    ]


def test_effective_runner_limits_follow_observation_with_optional_caps() -> None:
    effective_runner_limits = sandbox_queue_service.effective_runner_limits

    assert effective_runner_limits(
        observed_active=8,
        observed_executing=3,
        active_cap=0,
        executing_cap=0,
    ) == (8, 3)
    assert effective_runner_limits(
        observed_active=8,
        observed_executing=3,
        active_cap=6,
        executing_cap=2,
    ) == (6, 2)
    assert effective_runner_limits(
        observed_active=0,
        observed_executing=-1,
        active_cap=0,
        executing_cap=-1,
    ) == (1, 1)
    assert effective_runner_limits(
        observed_active=4,
        observed_executing=9,
        active_cap=0,
        executing_cap=0,
    ) == (4, 4)


def _run(index: int) -> RuntimeRun:
    run_id = f"01QUEUE{index:02d}RUNTIME0000000000"
    return RuntimeRun(
        id=run_id,
        root_run_id=run_id,
        conversation_id=f"conv_queue_service_{index}",
        entity_id="entity_queue_service",
        user_id=f"user_queue_service_{index}",
        status=RuntimeRunStatus.RUNNING.value,
    )


def _user_run(user: str, index: int) -> RuntimeRun:
    run_id = f"01QUEUE{user.upper()[:5]:<5}{index:02d}RUNTIME00000"[:26]
    return RuntimeRun(
        id=run_id,
        root_run_id=run_id,
        conversation_id=f"conv_queue_{user}_{index}",
        entity_id="entity_queue_service",
        user_id=f"user_queue_{user}",
        status=RuntimeRunStatus.RUNNING.value,
    )


@pytest.mark.integration
async def test_claim_is_fair_and_atomically_reserves_runner_capacity(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-queue-test",
        base_url="http://10.0.0.10:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=2,
        executing_limit=1,
    )
    low, high, old = _run(1), _run(2), _run(3)
    db_session.add_all([runner, low, high, old])
    await db_session.flush()
    await enqueue_sandbox_reservation(
        db_session,
        run=low,
        tool_call_id="tool-low",
        request_payload={"skill_name": "low"},
        priority=200,
        now=now - timedelta(seconds=20),
    )
    expected = await enqueue_sandbox_reservation(
        db_session,
        run=high,
        tool_call_id="tool-high",
        request_payload={"skill_name": "high"},
        priority=50,
        now=now,
    )
    await enqueue_sandbox_reservation(
        db_session,
        run=old,
        tool_call_id="tool-old",
        request_payload={"skill_name": "old"},
        priority=100,
        now=now - timedelta(seconds=30),
    )
    await db_session.flush()

    claim = await claim_next_sandbox_allocation(db_session, now=now)

    assert claim is not None
    reservation, claimed_runner = claim
    assert reservation.id == expected.id
    assert reservation.status == SandboxReservationStatus.ALLOCATING.value
    assert reservation.attempt_count == 1
    assert reservation.next_attempt_at == now + timedelta(seconds=240)
    assert claimed_runner.id == runner.id
    assert claimed_runner.active_count == 1
    allocation_event = (
        await db_session.execute(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.event_type == "runtime.allocate_sandbox",
                RuntimeOutboxEvent.aggregate_id == reservation.id,
            )
        )
    ).scalar_one()
    assert allocation_event.payload == {
        "reservation_id": reservation.id,
        "reservation_version": reservation.version,
    }


@pytest.mark.integration
async def test_stale_allocation_recovery_returns_runner_slot_and_requeues(db_session) -> None:
    now = datetime.now(timezone.utc)
    run = _run(8)
    runner = SandboxRunner(
        id="runner-stale-allocation",
        base_url="http://10.0.0.18:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=2,
        executing_limit=1,
    )
    db_session.add_all([run, runner])
    await db_session.flush()
    reservation = await enqueue_sandbox_reservation(
        db_session,
        run=run,
        tool_call_id="tool-stale-allocation",
        request_payload={"skill_name": "stale"},
        now=now,
    )
    await db_session.flush()
    assert await claim_next_sandbox_allocation(db_session, now=now) is not None

    recovered = await sandbox_queue_service.recover_stale_sandbox_allocations(
        db_session,
        now=now + timedelta(seconds=241),
        retry_delay_seconds=2,
    )
    await db_session.flush()

    assert recovered == 1
    assert reservation.status == SandboxReservationStatus.REQUEUED.value
    assert reservation.runner_id is None
    assert reservation.last_error == "sandbox_allocation_lease_expired"
    assert reservation.next_attempt_at == now + timedelta(seconds=243)
    assert runner.active_count == 0


@pytest.mark.integration
async def test_runner_health_reconciliation_drops_stale_count_but_keeps_live_reservations(
    db_session,
) -> None:
    runner = SandboxRunner(
        id="runner-health-reconcile",
        base_url="http://10.0.0.13:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=5,
        executing_limit=2,
        active_count=5,
    )
    db_session.add(runner)
    await db_session.flush()

    assert (
        await sandbox_queue_service.reconciled_runner_active_count(
            db_session,
            runner_id=runner.id,
            observed_active=0,
        )
        == 0
    )

    run = _run(9)
    db_session.add(run)
    await db_session.flush()
    reservation = await enqueue_sandbox_reservation(
        db_session,
        run=run,
        tool_call_id="tool-health-reconcile",
        request_payload={"skill_name": "health-reconcile"},
    )
    reservation.status = SandboxReservationStatus.ALLOCATING.value
    reservation.runner_id = runner.id
    await db_session.flush()

    assert (
        await sandbox_queue_service.reconciled_runner_active_count(
            db_session,
            runner_id=runner.id,
            observed_active=0,
        )
        == 1
    )
    assert (
        await sandbox_queue_service.reconciled_runner_active_count(
            db_session,
            runner_id=runner.id,
            observed_active=2,
        )
        == 2
    )


@pytest.mark.integration
async def test_runner_health_refresh_claim_is_throttled_transactionally(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-health-claim",
        base_url="http://10.0.0.14:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=5,
        executing_limit=2,
        last_health_at=now - timedelta(minutes=1),
    )
    db_session.add(runner)
    await db_session.flush()

    first = await sandbox_queue_service.claim_due_sandbox_runner_health_checks(
        db_session,
        now=now,
        interval_seconds=15,
    )
    second = await sandbox_queue_service.claim_due_sandbox_runner_health_checks(
        db_session,
        now=now,
        interval_seconds=15,
    )

    assert first == [runner.id]
    assert second == []
    assert runner.last_health_at == now


@pytest.mark.integration
async def test_runner_reconcile_applies_changed_caps_to_last_observed_capacity(db_session) -> None:
    runner = SandboxRunner(
        id="runner-cap-reconcile",
        base_url="http://10.0.0.15:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=8,
        executing_limit=3,
        config={"observed_active_limit": 8, "observed_executing_limit": 3},
    )
    db_session.add(runner)
    await db_session.flush()

    await sandbox_queue_service.reconcile_sandbox_runners(
        db_session,
        [
            {
                "id": runner.id,
                "base_url": runner.base_url,
                "active_limit": 6,
                "executing_limit": 2,
                "config": {},
            }
        ],
    )

    assert (runner.active_limit, runner.executing_limit) == (6, 2)

    await sandbox_queue_service.reconcile_sandbox_runners(
        db_session,
        [
            {
                "id": runner.id,
                "base_url": runner.base_url,
                "active_limit": 0,
                "executing_limit": 0,
                "config": {},
            }
        ],
    )

    assert (runner.active_limit, runner.executing_limit) == (8, 3)


@pytest.mark.integration
async def test_runner_reconcile_uses_legacy_capacity_and_invalidates_changed_url(db_session) -> None:
    rows = await sandbox_queue_service.reconcile_sandbox_runners(
        db_session,
        [
            {
                "id": "runner-legacy-health",
                "base_url": "http://10.0.0.16:8000",
                "active_limit": 0,
                "executing_limit": 0,
                "config": {},
            }
        ],
    )
    runner = rows[0]
    await db_session.flush()

    assert (runner.active_limit, runner.executing_limit) == (5, 2)
    assert runner.status == SandboxRunnerStatus.UNKNOWN.value

    runner.status = SandboxRunnerStatus.HEALTHY.value
    await db_session.flush()
    await sandbox_queue_service.reconcile_sandbox_runners(
        db_session,
        [
            {
                "id": runner.id,
                "base_url": "http://10.0.0.17:8000",
                "active_limit": 0,
                "executing_limit": 0,
                "config": {},
            }
        ],
    )

    assert runner.base_url == "http://10.0.0.17:8000"
    assert runner.status == SandboxRunnerStatus.UNKNOWN.value


@pytest.mark.integration
async def test_completed_allocation_is_idempotent_and_writes_resume_outbox(db_session) -> None:
    now = datetime.now(timezone.utc)
    run = _run(4)
    runner = SandboxRunner(
        id="runner-allocation-test",
        base_url="http://10.0.0.11:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=2,
        executing_limit=1,
    )
    db_session.add_all([run, runner])
    await db_session.flush()
    reservation = await enqueue_sandbox_reservation(
        db_session,
        run=run,
        tool_call_id="tool-idempotent",
        request_payload={"skill_name": "idempotent"},
        now=now,
    )
    await db_session.flush()
    claim = await claim_next_sandbox_allocation(db_session, now=now)
    assert claim is not None
    claimed_version = reservation.version

    first = await complete_sandbox_allocation(
        db_session,
        reservation_id=reservation.id,
        sandbox_id="sandbox-idempotent",
        runner_id=runner.id,
        expected_reservation_version=claimed_version,
        now=now,
    )
    second = await complete_sandbox_allocation(
        db_session,
        reservation_id=reservation.id,
        sandbox_id="sandbox-idempotent",
        runner_id=runner.id,
        expected_reservation_version=claimed_version,
        now=now,
    )
    await db_session.flush()

    assert first.id == second.id == reservation.id
    assert first.status == SandboxReservationStatus.ALLOCATED.value
    assert first.sandbox_id == "sandbox-idempotent"
    assert run.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert await db_session.get(SandboxInstance, "sandbox-idempotent") is not None
    outbox_count = await db_session.scalar(
        select(func.count(RuntimeOutboxEvent.id)).where(
            RuntimeOutboxEvent.aggregate_id == run.id
        )
    )
    assert outbox_count == 1


@pytest.mark.integration
async def test_stateful_route_resolves_the_original_runner_only_while_active(db_session) -> None:
    now = datetime.now(timezone.utc)
    run = _run(5)
    runner = SandboxRunner(
        id="runner-routing-test",
        base_url="http://10.0.0.12:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=1,
        executing_limit=1,
    )
    db_session.add_all([run, runner])
    await db_session.flush()
    reservation = await enqueue_sandbox_reservation(
        db_session,
        run=run,
        tool_call_id="tool-route",
        request_payload={"skill_name": "route"},
        now=now,
    )
    await db_session.flush()
    assert await claim_next_sandbox_allocation(db_session, now=now) is not None
    await complete_sandbox_allocation(
        db_session,
        reservation_id=reservation.id,
        sandbox_id="sandbox-route",
        runner_id=runner.id,
        now=now,
    )
    await db_session.flush()

    resolved = await resolve_sandbox_runner(db_session, "sandbox-route")
    assert resolved.id == runner.id
    assert resolved.base_url == "http://10.0.0.12:8000"

    instance = await db_session.get(SandboxInstance, "sandbox-route")
    assert instance is not None
    instance.status = "released"
    instance.released_at = now
    await db_session.flush()

    with pytest.raises(SandboxRouteNotFoundError):
        await resolve_sandbox_runner(db_session, "sandbox-route")


async def _task_reservation(
    db_session,
    *,
    requester_id: str,
    tool_call_id: str,
    user_suffix: str,
    now: datetime,
    max_wait_seconds: int | None = None,
) -> SandboxReservation:
    """Enqueue one ticket owned by a task worker that has no RuntimeRun."""

    reservation = await enqueue_sandbox_reservation(
        db_session,
        run=None,
        tool_call_id=tool_call_id,
        request_payload={"skill_name": "task-requester"},
        now=now,
        max_wait_seconds=max_wait_seconds,
        entity_id="entity_queue_service",
        user_id=f"user_task_{user_suffix}",
        requester_kind=SandboxRequesterKind.TASK.value,
        requester_id=requester_id,
    )
    await db_session.flush()
    return reservation


@pytest.mark.integration
async def test_enqueue_task_requester_without_run(db_session) -> None:
    now = datetime.now(timezone.utc)

    reservation = await _task_reservation(
        db_session,
        requester_id="task_1",
        tool_call_id="call_1",
        user_suffix="enqueue",
        now=now,
    )

    assert reservation.runtime_run_id is None
    assert reservation.root_run_id is None
    assert reservation.requester_kind == SandboxRequesterKind.TASK.value
    assert reservation.requester_id == "task_1"
    assert reservation.status == SandboxReservationStatus.QUEUED.value

    repeated = await _task_reservation(
        db_session,
        requester_id="task_1",
        tool_call_id="call_1",
        user_suffix="enqueue",
        now=now,
    )
    assert repeated.id == reservation.id

    other_call = await _task_reservation(
        db_session,
        requester_id="task_1",
        tool_call_id="call_2",
        user_suffix="enqueue",
        now=now,
    )
    assert other_call.id != reservation.id

    with pytest.raises(ValueError):
        await enqueue_sandbox_reservation(
            db_session,
            run=None,
            tool_call_id="call_3",
            request_payload={},
            entity_id="entity_queue_service",
            user_id="user_task_enqueue",
            requester_kind=SandboxRequesterKind.TASK.value,
        )


@pytest.mark.integration
async def test_enqueue_admits_a_users_second_ticket(db_session) -> None:
    """Admission has no per-user cap: a user's second ticket queues behind the
    first instead of failing the request (the requester would otherwise have
    to wait in-process, holding a worker slot, for its own ticket to drain)."""

    now = datetime.now(timezone.utc)
    first, second = _user_run("admit", 1), _user_run("admit", 2)
    db_session.add_all([first, second])
    await db_session.flush()

    ticket_1 = await enqueue_sandbox_reservation(
        db_session, run=first, tool_call_id="call-admit-1", request_payload={}, now=now,
    )
    ticket_2 = await enqueue_sandbox_reservation(
        db_session, run=second, tool_call_id="call-admit-2", request_payload={}, now=now,
    )
    task_ticket = await enqueue_sandbox_reservation(
        db_session,
        run=None,
        tool_call_id="call-admit-task",
        request_payload={},
        now=now,
        entity_id="entity_queue_service",
        user_id="user_queue_admit",
        requester_kind=SandboxRequesterKind.TASK.value,
        requester_id="task_admit",
    )
    await db_session.flush()
    assert len({ticket_1.id, ticket_2.id, task_ticket.id}) == 3
    assert {ticket_1.status, ticket_2.status, task_ticket.status} == {
        SandboxReservationStatus.QUEUED.value
    }

    # The global cap still bites.
    with pytest.raises(sandbox_queue_service.SandboxQueueFullError, match="sandbox_queue_full"):
        await enqueue_sandbox_reservation(
            db_session,
            run=_user_run("admit", 3),
            tool_call_id="call-admit-3",
            request_payload={},
            now=now,
            max_pending=3,
        )


@pytest.mark.integration
async def test_claim_serialises_one_users_tickets_and_lets_others_through(db_session) -> None:
    """The per-user limit is a claim-time fairness rule: while a user has a
    ticket in flight, their queued tickets are skipped and other users'
    tickets are claimed ahead of them; once it drains, theirs is next."""

    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-fairness",
        base_url="http://10.0.0.40:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=4,
        executing_limit=2,
    )
    hog_1, hog_2, other = _user_run("hog", 1), _user_run("hog", 2), _user_run("other", 1)
    db_session.add_all([runner, hog_1, hog_2, other])
    await db_session.flush()

    in_flight = await enqueue_sandbox_reservation(
        db_session, run=hog_1, tool_call_id="call-hog-1", request_payload={},
        now=now - timedelta(seconds=30),
    )
    hog_queued = await enqueue_sandbox_reservation(
        db_session, run=hog_2, tool_call_id="call-hog-2", request_payload={},
        now=now - timedelta(seconds=20),
    )
    other_queued = await enqueue_sandbox_reservation(
        db_session, run=other, tool_call_id="call-other-1", request_payload={},
        now=now - timedelta(seconds=10),
    )
    await db_session.flush()

    first = await claim_next_sandbox_allocation(db_session, now=now, max_in_flight_per_user=1)
    assert first is not None and first[0].id == in_flight.id

    # The hog's older queued ticket is skipped for the other user's newer one.
    second = await claim_next_sandbox_allocation(db_session, now=now, max_in_flight_per_user=1)
    assert second is not None and second[0].id == other_queued.id

    # Nothing else is claimable while the hog's first ticket is in flight...
    assert await claim_next_sandbox_allocation(db_session, now=now, max_in_flight_per_user=1) is None

    # ...and it is next once that ticket drains.
    in_flight.status = SandboxReservationStatus.CONSUMED.value
    await db_session.flush()
    third = await claim_next_sandbox_allocation(db_session, now=now, max_in_flight_per_user=1)
    assert third is not None and third[0].id == hog_queued.id


@pytest.mark.integration
async def test_vanished_instances_are_released_against_the_runner_listing(db_session) -> None:
    """The runner's idle reaper removes containers without telling the
    coordinator; the health refresh reconciles active instance rows against
    the runner's listing so a release_pending ticket cannot pin a slot
    forever. Rows younger than the grace period are left alone."""

    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-vanished",
        base_url="http://10.0.0.50:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=5,
        executing_limit=2,
        active_count=3,
    )
    gone, alive, young = _user_run("gone", 1), _user_run("alive", 1), _user_run("young", 1)
    db_session.add_all([runner, gone, alive, young])
    await db_session.flush()
    instances: dict[str, SandboxInstance] = {}
    for run, sandbox_id, status, age in (
        (gone, "sbx-gone", SandboxReservationStatus.RELEASE_PENDING.value, timedelta(hours=1)),
        (alive, "sbx-alive", SandboxReservationStatus.CONSUMED.value, timedelta(hours=1)),
        (young, "sbx-young", SandboxReservationStatus.CONSUMED.value, timedelta(seconds=5)),
    ):
        reservation = await enqueue_sandbox_reservation(
            db_session, run=run, tool_call_id=f"call-{sandbox_id}", request_payload={}, now=now,
        )
        reservation.status = status
        reservation.sandbox_id = sandbox_id
        reservation.runner_id = runner.id
        instances[sandbox_id] = SandboxInstance(
            sandbox_id=sandbox_id,
            reservation_id=reservation.id,
            runtime_run_id=run.id,
            root_run_id=run.root_run_id,
            runner_id=runner.id,
            status="active",
            created_at=now - age,
        )
        db_session.add(instances[sandbox_id])
    await db_session.flush()

    released = await sandbox_queue_service.release_vanished_sandbox_instances(
        db_session, runner_id=runner.id, live_sandbox_ids=["sbx-alive"], now=now,
    )
    await db_session.flush()

    assert released == ["sbx-gone"]
    assert instances["sbx-gone"].status == "released"
    assert instances["sbx-alive"].status == "active"
    assert instances["sbx-young"].status == "active", "a fresh row may have raced the listing"
    gone_ticket = await db_session.get(SandboxReservation, instances["sbx-gone"].reservation_id)
    assert gone_ticket.status == SandboxReservationStatus.RELEASED.value
    assert runner.active_count == 2
    # Idempotent: a second pass finds nothing left to release.
    assert await sandbox_queue_service.release_vanished_sandbox_instances(
        db_session, runner_id=runner.id, live_sandbox_ids=["sbx-alive"], now=now,
    ) == []


@pytest.mark.integration
async def test_restore_consumed_reservation_only_undoes_release_pending(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-restore",
        base_url="http://10.0.0.51:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=5,
        executing_limit=2,
    )
    run = _user_run("restore", 1)
    db_session.add_all([runner, run])
    await db_session.flush()
    reservation = await enqueue_sandbox_reservation(
        db_session, run=run, tool_call_id="call-restore", request_payload={}, now=now,
    )
    reservation.status = SandboxReservationStatus.RELEASE_PENDING.value
    reservation.sandbox_id = "sbx-restore"
    reservation.runner_id = runner.id
    db_session.add(SandboxInstance(
        sandbox_id="sbx-restore", reservation_id=reservation.id, runtime_run_id=run.id,
        root_run_id=run.root_run_id, runner_id=runner.id, status="active",
    ))
    await db_session.flush()

    assert await sandbox_queue_service.restore_consumed_reservation(db_session, sandbox_id="sbx-restore")
    assert reservation.status == SandboxReservationStatus.CONSUMED.value
    # Already consumed (or any other state): nothing to undo.
    assert not await sandbox_queue_service.restore_consumed_reservation(db_session, sandbox_id="sbx-restore")
    assert not await sandbox_queue_service.restore_consumed_reservation(db_session, sandbox_id="sbx-unknown")


@pytest.mark.integration
async def test_complete_task_reservation_emits_no_resume_event(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-task-complete",
        base_url="http://10.0.0.30:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=2,
        executing_limit=1,
    )
    db_session.add(runner)
    await db_session.flush()
    reservation = await _task_reservation(
        db_session,
        requester_id="task_complete",
        tool_call_id="call-task-complete",
        user_suffix="complete",
        now=now,
    )
    reservation.status = SandboxReservationStatus.ALLOCATING.value
    reservation.runner_id = runner.id
    reservation.attempt_count = 1
    runner.active_count += 1
    await db_session.flush()

    allocated = await complete_sandbox_allocation(
        db_session,
        reservation_id=reservation.id,
        sandbox_id="sandbox-task-complete",
        runner_id=runner.id,
        now=now,
    )
    await db_session.flush()

    assert allocated.status == SandboxReservationStatus.ALLOCATED.value
    instance = await db_session.get(SandboxInstance, "sandbox-task-complete")
    assert instance is not None
    assert instance.runtime_run_id is None
    assert instance.root_run_id is None
    resume_events = await db_session.scalar(
        select(func.count(RuntimeOutboxEvent.id)).where(
            RuntimeOutboxEvent.event_type == "runtime.resume_run",
            RuntimeOutboxEvent.dedupe_key.like(f"runtime.resume_run:{reservation.id}:%"),
        )
    )
    assert resume_events == 0


@pytest.mark.integration
async def test_expire_and_fail_task_reservation_without_run(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-task-lifecycle",
        base_url="http://10.0.0.31:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=4,
        executing_limit=2,
        active_count=2,
    )
    db_session.add(runner)
    await db_session.flush()

    expiring = await _task_reservation(
        db_session,
        requester_id="task_expire",
        tool_call_id="call-task-expire",
        user_suffix="expire",
        now=now - timedelta(seconds=600),
        max_wait_seconds=60,
    )
    expired = await expire_sandbox_reservations(db_session, now=now)
    await db_session.flush()

    assert expired >= 1
    assert expiring.status == SandboxReservationStatus.EXPIRED.value
    assert expiring.last_error == "sandbox_queue_timeout"

    failing = await _task_reservation(
        db_session,
        requester_id="task_fail",
        tool_call_id="call-task-fail",
        user_suffix="fail",
        now=now - timedelta(seconds=600),
        max_wait_seconds=60,
    )
    failing.status = SandboxReservationStatus.ALLOCATING.value
    failing.runner_id = runner.id
    failing.attempt_count = 1
    await db_session.flush()

    failed = await fail_sandbox_allocation(
        db_session,
        reservation_id=failing.id,
        error="sandbox_create_failed",
        now=now,
    )
    await db_session.flush()

    assert failed.status == SandboxReservationStatus.FAILED.value
    assert failed.runner_id is None
    assert runner.active_count == 1

    stale = await _task_reservation(
        db_session,
        requester_id="task_stale",
        tool_call_id="call-task-stale",
        user_suffix="stale",
        now=now,
    )
    stale.status = SandboxReservationStatus.ALLOCATING.value
    stale.runner_id = runner.id
    stale.attempt_count = 1
    stale.next_attempt_at = now - timedelta(seconds=1)
    await db_session.flush()

    recovered = await sandbox_queue_service.recover_stale_sandbox_allocations(
        db_session,
        now=now,
        retry_delay_seconds=2,
    )
    await db_session.flush()

    assert recovered >= 1
    assert stale.status == SandboxReservationStatus.REQUEUED.value
    assert stale.runner_id is None


@pytest.mark.integration
async def test_cancel_queued_task_reservation(db_session) -> None:
    now = datetime.now(timezone.utc)
    reservation = await _task_reservation(
        db_session,
        requester_id="task_cancel",
        tool_call_id="call-task-cancel",
        user_suffix="cancel",
        now=now,
    )

    assert (
        await cancel_queued_sandbox_reservation(
            db_session,
            reservation_id=reservation.id,
            reason="task_worker_gave_up",
            now=now,
        )
        is True
    )
    await db_session.flush()
    assert reservation.status == SandboxReservationStatus.CANCELLED.value
    assert reservation.completed_at == now
    assert reservation.last_error == "task_worker_gave_up"

    assert (
        await cancel_queued_sandbox_reservation(
            db_session,
            reservation_id=reservation.id,
            reason="task_worker_gave_up",
            now=now,
        )
        is False
    )

    allocated = await _task_reservation(
        db_session,
        requester_id="task_cancel_allocated",
        tool_call_id="call-task-cancel-allocated",
        user_suffix="cancel2",
        now=now,
    )
    allocated.status = SandboxReservationStatus.ALLOCATED.value
    await db_session.flush()

    assert (
        await cancel_queued_sandbox_reservation(
            db_session,
            reservation_id=allocated.id,
            reason="task_worker_gave_up",
            now=now,
        )
        is False
    )
    assert allocated.status == SandboxReservationStatus.ALLOCATED.value


@pytest.mark.integration
async def test_consume_task_reservation(db_session) -> None:
    now = datetime.now(timezone.utc)
    reservation = await _task_reservation(
        db_session,
        requester_id="task_consume",
        tool_call_id="call-task-consume",
        user_suffix="consume",
        now=now,
    )

    # A ticket the scheduler has not allocated cannot be consumed.
    with pytest.raises(SandboxAllocationStateError):
        await consume_sandbox_reservation(
            db_session,
            reservation_id=reservation.id,
            now=now,
        )

    reservation.status = SandboxReservationStatus.ALLOCATED.value
    reservation.sandbox_id = "sandbox-task-consume"
    await db_session.flush()
    version_before = reservation.version

    consumed = await consume_sandbox_reservation(
        db_session,
        reservation_id=reservation.id,
        now=now,
    )
    await db_session.flush()

    assert consumed.id == reservation.id
    assert consumed.status == SandboxReservationStatus.CONSUMED.value
    assert consumed.consumed_at == now
    assert consumed.version == version_before + 1

    # Idempotent: a retried attach neither re-stamps nor re-versions the ticket.
    again = await consume_sandbox_reservation(
        db_session,
        reservation_id=reservation.id,
        now=now + timedelta(seconds=5),
    )
    assert again.status == SandboxReservationStatus.CONSUMED.value
    assert again.consumed_at == now
    assert again.version == version_before + 1

    with pytest.raises(SandboxAllocationStateError):
        await consume_sandbox_reservation(
            db_session,
            reservation_id="missing-reservation",
            now=now,
        )


@pytest.mark.integration
async def test_expire_releases_abandoned_allocated_task_ticket(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id="runner-task-abandoned",
        base_url="http://10.0.0.32:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=4,
        executing_limit=2,
        active_count=1,
    )
    db_session.add(runner)
    await db_session.flush()

    abandoned = await _task_reservation(
        db_session,
        requester_id="task_abandoned",
        tool_call_id="call-task-abandoned",
        user_suffix="abandoned",
        now=now,
    )
    abandoned.status = SandboxReservationStatus.ALLOCATED.value
    abandoned.runner_id = runner.id
    abandoned.sandbox_id = "sandbox-task-abandoned"
    abandoned.allocated_at = now - timedelta(hours=1)
    db_session.add(
        SandboxInstance(
            sandbox_id="sandbox-task-abandoned",
            reservation_id=abandoned.id,
            runner_id=runner.id,
            status="active",
        )
    )

    fresh = await _task_reservation(
        db_session,
        requester_id="task_fresh",
        tool_call_id="call-task-fresh",
        user_suffix="fresh",
        now=now,
    )
    fresh.status = SandboxReservationStatus.ALLOCATED.value
    fresh.runner_id = runner.id
    fresh.sandbox_id = "sandbox-task-fresh"
    fresh.allocated_at = now
    await db_session.flush()

    touched = await expire_sandbox_reservations(db_session, now=now)
    await db_session.flush()

    assert touched >= 1
    assert abandoned.status == SandboxReservationStatus.RELEASED.value
    assert abandoned.last_error == "task_consume_timeout"
    instance = await db_session.get(SandboxInstance, "sandbox-task-abandoned")
    assert instance is not None
    assert instance.status == "released"
    assert runner.active_count == 0

    # A ticket whose poller could still be waiting is left alone.
    assert fresh.status == SandboxReservationStatus.ALLOCATED.value
    assert fresh.last_error is None


@pytest.mark.integration
async def test_expire_orphaned_pending_checkpoint_ticket_without_failing_a_run_that_moved_on(
    db_session,
) -> None:
    """A ticket that never reached its checkpoint expires; only its own run fails.

    Both tickets belong to one run. The first stayed ``pending_checkpoint``
    (the acquiring call could not suspend), the run later parked on the
    second. Expiring the orphan must not touch the run, which is waiting on
    the live ticket; a ``pending_checkpoint`` ticket the run *is* parked on
    (worker died before the checkpoint) fails the run like a queued one.
    """

    now = datetime.now(timezone.utc)
    moved_on, stuck = _run(71), _run(72)
    db_session.add_all([moved_on, stuck])
    await db_session.flush()

    orphan = await enqueue_sandbox_reservation(
        db_session,
        run=moved_on,
        tool_call_id="call-orphan",
        request_payload={"skill_name": "pdf"},
        now=now - timedelta(seconds=600),
        max_wait_seconds=60,
        activate=False,
    )
    live = await enqueue_sandbox_reservation(
        db_session,
        run=moved_on,
        tool_call_id="call-live",
        request_payload={"skill_name": "pdf"},
        now=now,
        max_wait_seconds=600,
    )
    assert moved_on.reservation_id == live.id
    parked = await enqueue_sandbox_reservation(
        db_session,
        run=stuck,
        tool_call_id="call-parked",
        request_payload={"skill_name": "pdf"},
        now=now - timedelta(seconds=600),
        max_wait_seconds=60,
        activate=False,
    )
    assert orphan.status == SandboxReservationStatus.PENDING_CHECKPOINT.value
    assert parked.status == SandboxReservationStatus.PENDING_CHECKPOINT.value
    await db_session.flush()

    expired = await expire_sandbox_reservations(db_session, now=now)
    await db_session.flush()

    assert expired >= 2
    assert orphan.status == SandboxReservationStatus.EXPIRED.value
    assert orphan.last_error == "sandbox_queue_timeout"
    assert live.status == SandboxReservationStatus.QUEUED.value
    assert moved_on.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert moved_on.reservation_id == live.id

    assert parked.status == SandboxReservationStatus.EXPIRED.value
    assert stuck.status == RuntimeRunStatus.FAILED.value
    assert stuck.status_reason == "sandbox_queue_timeout"


async def _allocating_ticket(db_session, *, run: RuntimeRun, runner_id: str, now: datetime) -> SandboxReservation:
    runner = SandboxRunner(
        id=runner_id,
        base_url=f"http://{runner_id}:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=4,
        executing_limit=2,
    )
    db_session.add_all([run, runner])
    await db_session.flush()
    reservation = await enqueue_sandbox_reservation(
        db_session,
        run=run,
        tool_call_id=f"call-{runner_id}",
        request_payload={"skill_name": "pdf"},
        now=now,
    )
    await db_session.flush()
    claim = await claim_next_sandbox_allocation(db_session, now=now)
    assert claim is not None and claim[0].id == reservation.id
    assert run.reservation_id == reservation.id
    return reservation


def _move_run_to_another_wait(run: RuntimeRun) -> None:
    # What a later park of a different kind does: drop the ticket pointer.
    run.reservation_id = None
    run.status = RuntimeRunStatus.WAITING_RESOURCE.value
    run.status_reason = "awaiting_human_input"


async def _resume_events(db_session, run_id: str) -> int:
    return int(
        await db_session.scalar(
            select(func.count(RuntimeOutboxEvent.id)).where(
                RuntimeOutboxEvent.aggregate_id == run_id,
                RuntimeOutboxEvent.event_type == "runtime.resume_run",
            )
        )
        or 0
    )


@pytest.mark.integration
async def test_stale_allocation_completion_leaves_a_run_that_moved_on_untouched(db_session) -> None:
    now = datetime.now(timezone.utc)
    current_run, moved_on = _run(81), _run(82)
    current = await _allocating_ticket(db_session, run=current_run, runner_id="runner-complete-current", now=now)
    stale = await _allocating_ticket(db_session, run=moved_on, runner_id="runner-complete-stale", now=now)
    _move_run_to_another_wait(moved_on)
    await db_session.flush()

    await complete_sandbox_allocation(
        db_session, reservation_id=stale.id, sandbox_id="sandbox-stale", runner_id="runner-complete-stale", now=now,
    )
    await complete_sandbox_allocation(
        db_session, reservation_id=current.id, sandbox_id="sandbox-current", runner_id="runner-complete-current", now=now,
    )
    await db_session.flush()

    assert stale.status == SandboxReservationStatus.ALLOCATED.value
    assert moved_on.status_reason == "awaiting_human_input"
    assert moved_on.reservation_id is None
    assert await _resume_events(db_session, moved_on.id) == 0

    assert current.status == SandboxReservationStatus.ALLOCATED.value
    assert current_run.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert current_run.status_reason == "sandbox_allocated"
    assert await _resume_events(db_session, current_run.id) == 1


@pytest.mark.integration
async def test_stale_allocation_recovery_fails_only_the_run_parked_on_the_ticket(db_session) -> None:
    now = datetime.now(timezone.utc)
    current_run, moved_on = _run(83), _run(84)
    current = await _allocating_ticket(db_session, run=current_run, runner_id="runner-recover-current", now=now)
    stale = await _allocating_ticket(db_session, run=moved_on, runner_id="runner-recover-stale", now=now)
    _move_run_to_another_wait(moved_on)
    await db_session.flush()

    recovered = await sandbox_queue_service.recover_stale_sandbox_allocations(
        db_session, now=now + timedelta(seconds=241), max_attempts=1,
    )
    await db_session.flush()

    assert recovered == 2
    assert stale.status == SandboxReservationStatus.FAILED.value
    assert moved_on.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert moved_on.status_reason == "awaiting_human_input"
    assert current.status == SandboxReservationStatus.FAILED.value
    assert current_run.status == RuntimeRunStatus.FAILED.value
    assert current_run.status_reason == "sandbox_allocation_failed"


@pytest.mark.integration
async def test_failed_allocation_fails_only_the_run_parked_on_the_ticket(db_session) -> None:
    now = datetime.now(timezone.utc)
    current_run, later_ticket_run = _run(85), _run(86)
    current = await _allocating_ticket(db_session, run=current_run, runner_id="runner-fail-current", now=now)
    stale = await _allocating_ticket(db_session, run=later_ticket_run, runner_id="runner-fail-stale", now=now)
    # This run moved on to a later sandbox ticket rather than another wait kind.
    later = await enqueue_sandbox_reservation(
        db_session,
        run=later_ticket_run,
        tool_call_id="call-later-ticket",
        request_payload={"skill_name": "pdf"},
        now=now,
    )
    assert later_ticket_run.reservation_id == later.id
    await db_session.flush()

    await fail_sandbox_allocation(db_session, reservation_id=stale.id, error="boom", now=now, max_attempts=1)
    await fail_sandbox_allocation(db_session, reservation_id=current.id, error="boom", now=now, max_attempts=1)
    await db_session.flush()

    assert stale.status == SandboxReservationStatus.FAILED.value
    assert later_ticket_run.status == RuntimeRunStatus.WAITING_RESOURCE.value
    assert later_ticket_run.status_reason == "sandbox_capacity"
    assert current.status == SandboxReservationStatus.FAILED.value
    assert current_run.status == RuntimeRunStatus.FAILED.value
    assert current_run.status_reason == "sandbox_allocation_failed"
