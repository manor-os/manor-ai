"""A run parked on a sandbox ticket always has a way forward.

Review finding (2026-10-05): a run in ``waiting_resource`` woke only when a
``runtime.resume_run`` delivery found its ticket ALLOCATED. The ticket sweeps
own the ticket, never the run, so a resume lost after the outbox marked it
delivered, or a container the runner reclaimed before the run claimed it,
left the run waiting forever, and the conversation's one-active-run
constraint refused every later turn.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

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
from packages.core.services import sandbox_queue_service
from packages.core.services.sandbox_queue_service import (
    claim_next_sandbox_allocation,
    complete_sandbox_allocation,
    enqueue_exec_slot_wait,
    enqueue_sandbox_reservation,
    recover_waiting_sandbox_runs,
    redeliver_exec_slot_resumes,
    release_sandbox_instance,
    wake_exec_slot_wait,
)
from tests.sandbox_scheduling_isolation import fence_foreign_scheduling


@pytest.fixture
async def db_session(db_session):
    """Claims here see only this test's runners and tickets (rolled back with it)."""

    await fence_foreign_scheduling(db_session)
    yield db_session


def _run(index: int) -> RuntimeRun:
    run_id = f"01RECOVER{index:02d}RUNTIME00000000"[:26]
    return RuntimeRun(
        id=run_id,
        root_run_id=run_id,
        conversation_id=f"conv_run_recovery_{index}",
        entity_id="entity_run_recovery",
        user_id=f"user_run_recovery_{index}",
        status=RuntimeRunStatus.RUNNING.value,
    )


async def _parked_on_allocation(db, index: int, *, now: datetime):
    """A run whose ticket was allocated at ``now`` and is waiting for its resume."""

    run = _run(index)
    runner = SandboxRunner(
        id=f"runner-recovery-{index}",
        base_url=f"http://10.0.9.{index}:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=4,
        executing_limit=2,
    )
    db.add_all([run, runner])
    await db.flush()
    # Top priority: tickets other tests left queued in a shared shard
    # database must not be claimed ahead of this one.
    reservation = await enqueue_sandbox_reservation(
        db, run=run, tool_call_id=f"call-recovery-{index}", request_payload={"skill_name": "pptx"},
        priority=0, now=now,
    )
    await db.flush()
    claimed = await claim_next_sandbox_allocation(db, now=now)
    assert claimed is not None and claimed[0].id == reservation.id, claimed
    # The claim may place the ticket on any healthy runner, including one
    # another test left in a shared shard database: complete on that runner.
    runner = claimed[1]
    await complete_sandbox_allocation(
        db,
        reservation_id=reservation.id,
        sandbox_id=f"sbx-recovery-{index}",
        runner_id=runner.id,
        expected_reservation_version=reservation.version,
        now=now,
    )
    await db.flush()
    assert run.status == RuntimeRunStatus.WAITING_RESOURCE.value
    return run, reservation, runner


async def _resume_events(db, run: RuntimeRun) -> int:
    return int(await db.scalar(
        select(func.count(RuntimeOutboxEvent.id)).where(
            RuntimeOutboxEvent.aggregate_id == run.id,
            RuntimeOutboxEvent.event_type == "runtime.resume_run",
        )
    ) or 0)


@pytest.mark.integration
async def test_a_lost_resume_is_delivered_again_once_per_window(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, reservation, _runner = await _parked_on_allocation(db_session, 1, now=now)
    assert await _resume_events(db_session, run) == 1

    await recover_waiting_sandbox_runs(db_session, now=now + timedelta(seconds=30))  # global count; this run's state is asserted below
    assert await _resume_events(db_session, run) == 1, "a fresh allocation is still being delivered"

    later = now + timedelta(seconds=61)
    assert await recover_waiting_sandbox_runs(db_session, now=later) >= 1  # at least this run; others may share the DB
    await db_session.flush()
    assert await _resume_events(db_session, run) == 2
    redelivery = (await db_session.execute(
        select(RuntimeOutboxEvent)
        .where(RuntimeOutboxEvent.aggregate_id == run.id)
        .order_by(RuntimeOutboxEvent.created_at.desc())
        .limit(1)
    )).scalar_one()
    # The resume claim checks the ticket's exact version.
    assert redelivery.payload["reservation_version"] == reservation.version
    assert redelivery.payload["reservation_id"] == reservation.id

    # Idempotent inside the same window.
    await recover_waiting_sandbox_runs(db_session, now=later + timedelta(seconds=1))  # global count; this run's state is asserted below
    assert await _resume_events(db_session, run) == 2


@pytest.mark.integration
async def test_two_sweeps_straddling_a_clock_minute_redeliver_once(db_session) -> None:
    # A sweep 0.5 s before a 60 s clock boundary and the next one a second
    # later must not both redeliver: the window slides from the last one.
    base = datetime.now(timezone.utc).replace(microsecond=0)
    boundary = base + timedelta(seconds=120 - base.timestamp() % 60)
    now = boundary - timedelta(seconds=61.5)
    run, _reservation, _runner = await _parked_on_allocation(db_session, 7, now=now)
    first = boundary - timedelta(seconds=0.5)
    await recover_waiting_sandbox_runs(db_session, now=first)
    await db_session.flush()
    assert await _resume_events(db_session, run) == 2
    await recover_waiting_sandbox_runs(db_session, now=first + timedelta(seconds=1))
    await db_session.flush()
    assert await _resume_events(db_session, run) == 2


async def _woken_on_exec_slot(db, index: int, *, now: datetime):
    """A run whose exec-slot ticket was woken at ``now`` and awaits its resume."""

    run = _run(index)
    run.status = RuntimeRunStatus.WAITING_RESOURCE.value
    db.add(run)
    await db.flush()
    reservation = await enqueue_exec_slot_wait(
        db,
        run=run,
        tool_call_id=f"call-exec-slot-{index}",
        sandbox_id=f"sbx-exec-slot-{index}",
        execution_id=f"exec-slot-{index}",
        wait_seconds=600,
        now=now,
    )
    await db.flush()
    assert await wake_exec_slot_wait(db, reservation_id=reservation.id, reason="slot_free", now=now)
    await db.flush()
    assert reservation.status == SandboxReservationStatus.EXEC_READY.value
    return run, reservation


@pytest.mark.integration
async def test_exec_slot_sweeps_straddling_a_clock_minute_redeliver_once(db_session) -> None:
    # The exec-slot redelivery shares the sliding window: a sweep 0.5 s
    # before a 60 s clock boundary and the next one a second later send one
    # redelivery, and a sweep a full window after it sends the next.
    base = datetime.now(timezone.utc).replace(microsecond=0)
    boundary = base + timedelta(seconds=120 - base.timestamp() % 60)
    now = boundary - timedelta(seconds=61.5)
    run, _reservation = await _woken_on_exec_slot(db_session, 8, now=now)
    assert await _resume_events(db_session, run) == 1
    first = boundary - timedelta(seconds=0.5)
    await redeliver_exec_slot_resumes(db_session, now=first)
    await db_session.flush()
    assert await _resume_events(db_session, run) == 2
    await redeliver_exec_slot_resumes(db_session, now=first + timedelta(seconds=1))
    await db_session.flush()
    assert await _resume_events(db_session, run) == 2
    await redeliver_exec_slot_resumes(db_session, now=first + timedelta(seconds=61))
    await db_session.flush()
    assert await _resume_events(db_session, run) == 3


@pytest.mark.integration
async def test_a_run_a_worker_holds_is_left_alone(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, _reservation, _runner = await _parked_on_allocation(db_session, 2, now=now)
    run.lease_expires_at = now + timedelta(minutes=5)
    await db_session.flush()

    await recover_waiting_sandbox_runs(db_session, now=now + timedelta(seconds=90))  # global count; this run's state is asserted below
    assert await _resume_events(db_session, run) == 1


@pytest.mark.integration
async def test_a_container_lost_before_the_resume_is_replaced_on_the_same_ticket(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, reservation, runner = await _parked_on_allocation(db_session, 3, now=now)
    # The runner reclaimed the container before the run claimed its resume.
    assert await release_sandbox_instance(db_session, sandbox_id="sbx-recovery-3", now=now)
    await db_session.flush()
    assert reservation.status == SandboxReservationStatus.RELEASED.value
    assert run.status == RuntimeRunStatus.WAITING_RESOURCE.value

    later = now + timedelta(seconds=5)
    assert await recover_waiting_sandbox_runs(db_session, now=later) >= 1  # at least this run; others may share the DB
    await db_session.flush()
    assert reservation.status == SandboxReservationStatus.REQUEUED.value
    assert reservation.sandbox_id is None and reservation.runner_id is None
    assert reservation.deadline_at > later
    assert reservation.last_error == "sandbox_lost_before_resume"
    assert run.status == RuntimeRunStatus.WAITING_RESOURCE.value
    # E2E 2026-10-06: the requeued ticket kept its idempotency key and the
    # runner handed back the very container that was lost. A new generation
    # asks for a new container.
    assert reservation.request_payload[sandbox_queue_service.SANDBOX_GENERATION_KEY] == 1
    assert sandbox_queue_service.sandbox_create_idempotency_key(
        reservation.id, reservation.request_payload,
    ) != reservation.id

    # The scheduler allocates a fresh container to the same ticket.
    reclaimed = await claim_next_sandbox_allocation(db_session, now=later)
    assert reclaimed is not None and reclaimed[0].id == reservation.id, reclaimed
    runner = reclaimed[1]
    await complete_sandbox_allocation(
        db_session,
        reservation_id=reservation.id,
        sandbox_id="sbx-recovery-3b",
        runner_id=runner.id,
        expected_reservation_version=reservation.version,
        now=later,
    )
    await db_session.flush()
    assert reservation.status == SandboxReservationStatus.ALLOCATED.value
    assert reservation.sandbox_id == "sbx-recovery-3b"

    # A late release of the lost container cannot end the ticket that moved on.
    assert await db_session.get(SandboxInstance, "sbx-recovery-3") is None
    assert not await release_sandbox_instance(db_session, sandbox_id="sbx-recovery-3", now=later)
    assert reservation.status == SandboxReservationStatus.ALLOCATED.value


async def _assert_one_terminal_notice(db_session, run) -> None:
    """The run ended outside its loop through the one terminal transition."""
    from sqlalchemy import func, select

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.runtime_run_terminal import RUNTIME_RUN_TERMINAL_NOTICE_EVENT

    await db_session.flush()
    notices = await db_session.scalar(
        select(func.count(RuntimeOutboxEvent.id)).where(
            RuntimeOutboxEvent.event_type == RUNTIME_RUN_TERMINAL_NOTICE_EVENT,
            RuntimeOutboxEvent.aggregate_id == run.id,
        )
    )
    assert notices == 1


@pytest.mark.integration
async def test_releasing_a_container_never_ends_a_ticket_that_names_another(db_session) -> None:
    now = datetime.now(timezone.utc)
    _run_row, reservation, _runner = await _parked_on_allocation(db_session, 6, now=now)
    reservation.sandbox_id = "sbx-recovery-6-newer"
    await db_session.flush()

    assert await release_sandbox_instance(db_session, sandbox_id="sbx-recovery-6", now=now)
    assert reservation.status == SandboxReservationStatus.ALLOCATED.value


@pytest.mark.integration
async def test_a_ticket_that_keeps_losing_its_container_fails_the_run(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, reservation, _runner = await _parked_on_allocation(db_session, 4, now=now)
    await release_sandbox_instance(db_session, sandbox_id="sbx-recovery-4", now=now)
    reservation.attempt_count = 5
    await db_session.flush()

    assert await recover_waiting_sandbox_runs(db_session, now=now, max_attempts=5) >= 1  # at least this run; others may share the DB
    assert run.status == RuntimeRunStatus.FAILED.value
    assert run.status_reason == "sandbox_lost_before_resume"
    assert run.completed_at is not None
    await _assert_one_terminal_notice(db_session, run)


@pytest.mark.integration
@pytest.mark.parametrize("status", sorted(sandbox_queue_service._RUN_ENDING_RESERVATION_STATUSES))
async def test_a_ticket_that_ended_without_a_container_frees_the_run(db_session, status) -> None:
    now = datetime.now(timezone.utc)
    run = _run(10 + sorted(sandbox_queue_service._RUN_ENDING_RESERVATION_STATUSES).index(status))
    db_session.add(run)
    await db_session.flush()
    reservation = await enqueue_sandbox_reservation(
        db_session, run=run, tool_call_id=f"call-ended-{status}", request_payload={}, now=now,
    )
    reservation.status = status
    reservation.last_error = f"why-{status}"
    await db_session.flush()

    assert await recover_waiting_sandbox_runs(db_session, now=now) >= 1  # at least this run; others may share the DB
    assert run.status == RuntimeRunStatus.FAILED.value
    # A stable code, never the ticket's raw last_error.
    assert run.status_reason == sandbox_queue_service._RUN_ENDING_REASON_BY_RESERVATION_STATUS[status]
    assert run.status_reason != f"why-{status}"
    await _assert_one_terminal_notice(db_session, run)


@pytest.mark.integration
async def test_a_run_parked_on_a_question_is_never_touched(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, reservation, _runner = await _parked_on_allocation(db_session, 5, now=now)
    await release_sandbox_instance(db_session, sandbox_id="sbx-recovery-5", now=now)
    run.human_input_request_id = "01HUMANINPUTREQUEST0000000"
    await db_session.flush()

    await recover_waiting_sandbox_runs(db_session, now=now + timedelta(minutes=5))  # global count; this run's state is asserted below
    assert reservation.status == SandboxReservationStatus.RELEASED.value
    assert run.status == RuntimeRunStatus.WAITING_RESOURCE.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "destroyed"),
    [
        # Lease recovery requeued the ticket and a newer attempt on the same
        # runner is creating (or just recorded) the very same container.
        ({"status": SandboxReservationStatus.ALLOCATING.value, "runner_id": "runner-a", "sandbox_id": None}, False),
        ({"status": SandboxReservationStatus.ALLOCATED.value, "runner_id": "runner-a", "sandbox_id": "sbx-shared"}, False),
        # The ticket moved to another runner, or ended: the container is stray.
        ({"status": SandboxReservationStatus.ALLOCATING.value, "runner_id": "runner-b", "sandbox_id": None}, True),
        ({"status": SandboxReservationStatus.FAILED.value, "runner_id": "runner-a", "sandbox_id": None}, True),
    ],
)
async def test_a_stale_allocation_never_destroys_the_container_a_newer_attempt_holds(
    monkeypatch, current, destroyed,
) -> None:
    from types import SimpleNamespace

    from packages.core.services.sandbox_queue_service import SandboxAllocationStateError
    from packages.core.tasks import runtime_tasks

    claimed = SimpleNamespace(
        id="ticket-1", status=SandboxReservationStatus.ALLOCATING.value, version=2,
        runner_id="runner-a", request_payload={"skill_name": "pptx"}, sandbox_id=None,
    )
    reads = iter([claimed, SimpleNamespace(**current)])

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, _statement):
            row = next(reads)
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def get(self, model, _key):
            if model is SandboxRunner:
                return SimpleNamespace(id="runner-a", base_url="http://runner-a:8000")
            return next(reads)

        async def commit(self):
            return None

    calls: list[str] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def destroy(self, sandbox_id):
            calls.append(f"destroy:{sandbox_id}")

    async def create(**_kwargs):
        return "sbx-shared"

    async def complete(*_args, **_kwargs):
        raise SandboxAllocationStateError("reservation_version_mismatch")

    async def fail(*_args, **_kwargs):
        calls.append("fail")

    monkeypatch.setattr(runtime_tasks, "_durable_external_sandbox_enabled", lambda: True)
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: Session)
    monkeypatch.setattr(runtime_tasks, "_create_reserved_sandbox", create)
    monkeypatch.setattr(runtime_tasks, "complete_sandbox_allocation", complete)
    monkeypatch.setattr(runtime_tasks, "fail_sandbox_allocation", fail)
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)

    result = await runtime_tasks.allocate_reserved_sandbox_once(reservation_id="ticket-1", reservation_version=2)

    assert result["allocated"] is False
    assert ("destroy:sbx-shared" in calls) is destroyed


def test_lease_retries_share_a_key_and_a_lost_container_changes_it() -> None:
    key = sandbox_queue_service.sandbox_create_idempotency_key
    assert key("ticket", {"skill_name": "pptx"}) == "ticket"
    assert key("ticket", None) == "ticket"
    assert key("ticket", {"sandbox_generation": 1}) == "ticket.g1"
    assert key("ticket", {"sandbox_generation": 2}) != key("ticket", {"sandbox_generation": 1})


@pytest.mark.asyncio
async def test_the_allocation_create_uses_the_generation_key(monkeypatch) -> None:
    from packages.core.tasks import runtime_tasks

    sent: dict = {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_from_builtin(self, **kwargs):
            sent.update(kwargs)
            from types import SimpleNamespace

            return SimpleNamespace(sandbox_id="sbx-fresh")

    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)
    sandbox_id = await runtime_tasks._create_reserved_sandbox(
        reservation_id="ticket-9",
        base_url="http://runner:8000",
        payload={"skill_name": "docx", "source": "builtin", "sandbox_generation": 1},
    )
    assert sandbox_id == "sbx-fresh"
    assert sent["idempotency_key"] == "ticket-9.g1"


@pytest.mark.asyncio
async def test_the_allocation_create_resolves_the_isolation_ref_or_refuses(monkeypatch) -> None:
    """A ticket carries only an isolation reference; the scheduler resolves it
    into the create arguments, and never creates without it."""
    from types import SimpleNamespace

    from packages.core.services import sandbox_isolation
    from packages.core.tasks import runtime_tasks

    sent: list[dict] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_from_files(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(sandbox_id="sbx-isolated")

    async def resolve(ref: str) -> dict:
        return {"network_profile": ref}

    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)
    monkeypatch.setattr(sandbox_isolation, "_RESOLVERS", {})
    sandbox_isolation.register_isolation_resolver("test:", resolve)
    payload = {"skill_name": "desktop", "source": "workspace", "files": {"README.md": "x"},
               "isolation_key": "k", "isolation_ref": "test:1"}

    assert await runtime_tasks._create_reserved_sandbox(
        reservation_id="ticket-1", base_url="http://runner:8000", payload=payload,
    ) == "sbx-isolated"
    assert sent[-1]["network_profile"] == "test:1"

    sent.clear()
    with pytest.raises(sandbox_isolation.SandboxIsolationUnavailable) as refused:
        await runtime_tasks._create_reserved_sandbox(
            reservation_id="ticket-2", base_url="http://runner:8000",
            payload={**payload, "isolation_ref": "unknown:2"},
        )
    assert sent == []
    # Permanent for the ticket: failed at once, never retried.
    from packages.core.services.sandbox_queue_service import classify_allocation_error

    assert classify_allocation_error(refused.value)[0] == "fatal"
