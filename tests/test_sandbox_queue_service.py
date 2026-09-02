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
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.services import sandbox_queue_service
from packages.core.services.sandbox_queue_service import (
    SandboxRouteNotFoundError,
    claim_next_sandbox_allocation,
    complete_sandbox_allocation,
    configured_sandbox_runners,
    enqueue_sandbox_reservation,
    resolve_sandbox_runner,
)


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
