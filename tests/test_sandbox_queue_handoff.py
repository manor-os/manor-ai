"""A finished conversation's warm container goes to the queue, not to the reaper.

E2E (2026-10-06): five warm containers of conversations that had already
finished held all five runner slots, and a new conversation's ticket waited
the full queue limit and expired, because a warm container is only reclaimed
by the runner's idle reaper (300-600 s). While tickets wait and a runner is
full, the scheduler now destroys the least recently used warm containers whose
conversations have no active run, and claims the freed slot in the same tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from packages.core.models.runtime_run import (
    RuntimeRun,
    RuntimeRunStatus,
    SandboxInstance,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.services.sandbox_queue_service import (
    count_waiting_sandbox_reservations,
    enqueue_sandbox_reservation,
    sandboxes_free_for_handover,
)


def _run(tag: str, *, conversation: str, status: str, user: str = "user_handover") -> RuntimeRun:
    run_id = f"01HANDOVER{tag:<6}RUNTIME00000"[:26].replace(" ", "0")
    return RuntimeRun(
        id=run_id, root_run_id=run_id, conversation_id=conversation,
        entity_id="entity_handover", user_id=user, status=status,
    )


async def _container(db, runner: SandboxRunner, run: RuntimeRun | None, sandbox_id: str) -> None:
    if run is not None:
        intended = run.status
        reservation = await enqueue_sandbox_reservation(
            db, run=run, tool_call_id=f"call-{sandbox_id}", request_payload={},
        )
        run.status = intended  # enqueue parks the run; this one already moved on
    else:  # a task worker's ticket: no run at all
        reservation = SandboxReservation(
            requester_kind="task", requester_id=f"task-{sandbox_id}", tool_call_id=f"call-{sandbox_id}",
            entity_id="entity_handover", user_id="user_handover",
            enqueued_at=datetime.now(timezone.utc), deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            request_payload={},
        )
        db.add(reservation)
        await db.flush()
    reservation.status = SandboxReservationStatus.CONSUMED.value
    reservation.sandbox_id = sandbox_id
    db.add(SandboxInstance(
        sandbox_id=sandbox_id, reservation_id=reservation.id,
        runtime_run_id=run.id if run else None, root_run_id=run.id if run else None,
        runner_id=runner.id, status="active",
    ))
    await db.flush()


@pytest.mark.integration
async def test_only_containers_of_users_with_no_active_run_are_free(db_session) -> None:
    """A user's container serves all of the user's conversations: while ANY run
    of that user is live (another conversation's, or parked), it is in use."""
    runner = SandboxRunner(
        id="runner-handover", base_url="http://10.0.8.1:8000",
        status=SandboxRunnerStatus.HEALTHY.value, active_limit=4, executing_limit=2, active_count=4,
    )
    finished = _run("DONE", conversation="conv-done", status=RuntimeRunStatus.COMPLETED.value, user="user_done")
    live = _run("LIVE", conversation="conv-live", status=RuntimeRunStatus.COMPLETED.value)
    # The same user is still working -- in another conversation.
    live_elsewhere = _run("LIVE2", conversation="conv-other", status=RuntimeRunStatus.RUNNING.value)
    parked = _run("PARK", conversation="conv-park", status=RuntimeRunStatus.WAITING_RESOURCE.value, user="user_park")
    db_session.add_all([runner, finished, live, live_elsewhere, parked])
    await db_session.flush()
    await _container(db_session, runner, finished, "sbx-done")
    # The container was allocated by a finished run; the same user's run in
    # another conversation still uses it warm.
    await _container(db_session, runner, live, "sbx-live")
    await _container(db_session, runner, parked, "sbx-park")
    await _container(db_session, runner, None, "sbx-task")

    free = await sandboxes_free_for_handover(
        db_session, runner_id=runner.id,
        sandbox_ids=["sbx-done", "sbx-live", "sbx-park", "sbx-task", "sbx-unknown"],
    )

    assert free == {"sbx-done"}


@pytest.mark.integration
async def test_waiting_counts_only_tickets_the_scheduler_could_allocate_now(db_session) -> None:
    now = datetime.now(timezone.utc)
    runs = [_run(f"W{i}", conversation=f"conv-w{i}", status=RuntimeRunStatus.RUNNING.value) for i in range(4)]
    db_session.add_all(runs)
    await db_session.flush()
    tickets = [
        await enqueue_sandbox_reservation(db_session, run=run, tool_call_id=f"wait-{i}", request_payload={}, now=now)
        for i, run in enumerate(runs)
    ]
    tickets[1].status = SandboxReservationStatus.REQUEUED.value
    tickets[2].next_attempt_at = now + timedelta(minutes=1)      # backing off
    tickets[3].deadline_at = now - timedelta(seconds=1)          # already expired
    await db_session.flush()

    assert await count_waiting_sandbox_reservations(db_session, now=now) >= 2
    before = await count_waiting_sandbox_reservations(db_session, now=now)
    tickets[0].status = SandboxReservationStatus.ALLOCATING.value
    await db_session.flush()
    assert await count_waiting_sandbox_reservations(db_session, now=now) == before - 1


def _scheduler_harness(
    monkeypatch, *, waiting: int, listing: list, free: set[str], runner_attrs: dict | None = None,
    need_mb: int | None = None, waiting_in_runner_pool: int | None = None,
):
    from packages.core.tasks import runtime_tasks

    calls: list[str] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, _statement):
            runner = SimpleNamespace(**{
                "id": "runner-full", "base_url": "http://runner-full:8000",
                "active_count": 5, "active_limit": 5, "config": {},
                **(runner_attrs or {}),
            })
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [runner]))

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def list(self):
            calls.append("list")
            return listing

        async def destroy(self, sandbox_id):
            calls.append(f"destroy:{sandbox_id}")

    async def count(_db, *, pool=None, **_kwargs):
        if pool is None or waiting_in_runner_pool is None:
            return waiting
        return waiting_in_runner_pool

    async def free_for(_db, *, runner_id, sandbox_ids):
        return set(sandbox_ids) & free

    async def forget(sandbox_id, **_kwargs):
        calls.append(f"forget:{sandbox_id}")
        return None

    async def next_need(_db, **_kwargs):
        return need_mb, None

    monkeypatch.setattr(runtime_tasks, "count_waiting_sandbox_reservations", count)
    monkeypatch.setattr(runtime_tasks, "next_waiting_ticket_need", next_need)
    monkeypatch.setattr(runtime_tasks, "sandboxes_free_for_handover", free_for)
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)
    monkeypatch.setattr("packages.core.services.sandbox_session_service.forget_vanished_sandbox", forget)
    return runtime_tasks, Session, calls


def _info(
    sandbox_id: str, *, last_used: float, status: str = "ready", executing: str | None = None,
    memory: str = "512m",
):
    return SimpleNamespace(
        sandbox_id=sandbox_id, status=status, last_used_at=last_used, active_execution_id=executing,
        config={"memory": memory},
    )


@pytest.mark.asyncio
async def test_the_scheduler_hands_the_least_recently_used_free_container_to_the_queue(monkeypatch) -> None:
    runtime_tasks, Session, calls = _scheduler_harness(
        monkeypatch,
        waiting=1,
        listing=[
            _info("sbx-recent-free", last_used=200.0),
            _info("sbx-old-free", last_used=100.0),
            _info("sbx-oldest-busy", last_used=50.0),          # its conversation still runs
            _info("sbx-executing", last_used=10.0, status="executing", executing="manor-x"),
        ],
        free={"sbx-recent-free", "sbx-old-free", "sbx-executing"},
    )

    handed = await runtime_tasks._hand_finished_sandboxes_to_queue(lambda: Session())

    assert handed == 1
    assert calls == ["list", "destroy:sbx-old-free", "forget:sbx-old-free"]


@pytest.mark.asyncio
async def test_another_pools_queue_never_takes_a_runners_warm_containers(monkeypatch) -> None:
    """A render ticket waiting for its own fleet cannot use a slot freed on a
    full default runner, so that runner keeps its warm work."""

    runtime_tasks, Session, calls = _scheduler_harness(
        monkeypatch,
        waiting=1,
        waiting_in_runner_pool=0,
        listing=[_info("sbx-free", last_used=1.0)],
        free={"sbx-free"},
    )

    assert await runtime_tasks._hand_finished_sandboxes_to_queue(lambda: Session()) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_nothing_is_handed_over_without_a_waiting_ticket(monkeypatch) -> None:
    runtime_tasks, Session, calls = _scheduler_harness(
        monkeypatch, waiting=0, listing=[_info("sbx-free", last_used=1.0)], free={"sbx-free"},
    )

    assert await runtime_tasks._hand_finished_sandboxes_to_queue(lambda: Session()) == 0
    assert calls == []


def test_the_handover_calls_forget_with_arguments_its_real_signature_accepts() -> None:
    # The scheduler fakes forget_vanished_sandbox above; a signature change in
    # the real function would otherwise disable the handover silently (the
    # scheduler swallows the TypeError and retries every tick).
    import ast
    import inspect
    from pathlib import Path

    from packages.core.services.sandbox_session_service import forget_vanished_sandbox

    source = Path("packages/core/tasks/runtime_tasks.py").read_text()
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "forget_vanished_sandbox"
    ]
    assert calls, "the handover forgets the sandbox it destroyed"
    signature = inspect.signature(forget_vanished_sandbox)
    for call in calls:
        signature.bind("sbx", *(None for _ in call.args[1:]), **{kw.arg: None for kw in call.keywords})


@pytest.mark.asyncio
async def test_a_runner_with_free_slots_but_no_memory_for_the_next_ticket_frees_just_enough(monkeypatch) -> None:
    """Memory admission: two of five slots are taken, yet the next ticket's 2g
    container does not fit. Idle warm containers are freed (oldest first)
    until it does, and no more."""

    runtime_tasks, Session, calls = _scheduler_harness(
        monkeypatch,
        waiting=1,
        need_mb=2048,
        runner_attrs={
            "active_count": 3,
            "config": {"memory": {"available_mb": 1000, "max_container_mb": 3840, "default_mb": 512}},
        },
        listing=[
            _info("sbx-newest", last_used=300.0, memory="1g"),
            _info("sbx-oldest", last_used=100.0, memory="512m"),
            _info("sbx-middle", last_used=200.0, memory="1g"),
        ],
        free={"sbx-newest", "sbx-oldest", "sbx-middle"},
    )

    handed = await runtime_tasks._hand_finished_sandboxes_to_queue(lambda: Session())

    # Deficit 1048 MiB: the 512m oldest is not enough, the 1g next one is.
    assert handed == 2
    assert calls == [
        "list", "destroy:sbx-oldest", "forget:sbx-oldest", "destroy:sbx-middle", "forget:sbx-middle",
    ]


@pytest.mark.asyncio
async def test_a_runner_whose_memory_fits_the_next_ticket_is_left_alone(monkeypatch) -> None:
    runtime_tasks, Session, calls = _scheduler_harness(
        monkeypatch,
        waiting=1,
        need_mb=None,  # the runner's 512m default
        runner_attrs={
            "active_count": 3,
            "config": {"memory": {"available_mb": 1000, "max_container_mb": 3840, "default_mb": 512}},
        },
        listing=[_info("sbx-free", last_used=1.0)],
        free={"sbx-free"},
    )

    assert await runtime_tasks._hand_finished_sandboxes_to_queue(lambda: Session()) == 0
    assert calls == []
