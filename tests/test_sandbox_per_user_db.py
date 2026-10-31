"""One shared sandbox per user, end to end on Postgres and a real (in-process) runner.

The control plane (sandbox tool, session service, reservation queue, run
parking/resume) runs against the real reservation queue on Postgres and the
real sandbox runner (``tests/sandbox_inprocess_runner.py``: real routes,
``SkillRunner``, slot queue; only the Docker CLI is scripted). Commands block
until the test releases them, so every slot transition is deterministic.

* seven durable runs of ONE user, each in its own conversation, ask for a
  sandbox at once: one ticket creates ONE container, the others follow it;
* with 4 execution slots, 4 commands run and 3 durable runs park
  (``waiting_resource`` / ``sandbox_exec_slot``) instead of seeing "busy";
  a parked run that is cancelled leaves the queue cleanly; the others resume
  and complete in FIFO order, each collecting its own execution;
* another user gets a container of their own and is denied this one by id;
  user-less system work gets its own task-keyed container;
* when the container dies mid-flight, a running command gets an explicit
  "lost while running, not retried" result, while a command that had not
  started (a parked durable run, and an in-process task-worker waiter) is
  run on the user's new container.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.sandbox_inprocess_runner import install_inprocess_runner
from tests.sandbox_scheduling_isolation import fence_foreign_scheduling, lift_scheduling_fence
from tests.test_sandbox_session_service import fake_cache  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _own_scheduling(db_session):
    """Claims here are global: only each test's own runners and tickets may be placed."""

    fence = await fence_foreign_scheduling(db_session)
    await db_session.commit()
    yield
    await db_session.rollback()
    await lift_scheduling_fence(db_session, fence)
    await db_session.commit()


@pytest.fixture
def ids():
    suffix = uuid.uuid4().hex.upper()[:16]
    return {
        "suffix": suffix,
        "entity": f"E{suffix}"[:26],
        "user": f"U1{suffix}"[:26],
        "other_user": f"U2{suffix}"[:26],
        "task": f"01T{suffix}AAAAAAA"[:26],
    }


@pytest.fixture
async def env(db_session, fake_cache, monkeypatch, ids):  # noqa: F811 - fixture
    import packages.core.database as database_module
    from packages.core.config import get_settings
    from packages.core.models.runtime_run import (
        RuntimeOutboxEvent,
        RuntimeRun,
        SandboxInstance,
        SandboxReservation,
        SandboxRunner,
        SandboxRunnerStatus,
    )
    from packages.core.services import sandbox_session_service

    runner, host = install_inprocess_runner(monkeypatch, f"sandbox_main_pu_{ids['suffix']}")
    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database_module, "async_session", factory)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_POLL_SECONDS", 1)
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_MAX_WAIT_SECONDS", 60)
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_MAX_PENDING", 100000)
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_MAX_PENDING_PER_USER", 10)
    monkeypatch.setattr(settings, "SANDBOX_EXEC_QUEUE_WAIT_SECONDS", 120)
    sandbox_session_service._RUNNER_CAPABILITIES_CACHE.clear()
    runner_row = SandboxRunner(
        id=f"runner-{ids['suffix']}"[:80],
        base_url=f"http://runner-{ids['suffix'].lower()}",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=50,
        executing_limit=50,
    )
    db_session.add(runner_row)
    await db_session.commit()
    stop = asyncio.Event()
    scheduler = asyncio.create_task(_scheduler_loop(factory, stop))
    try:
        yield {"factory": factory, "runner": runner, "host": host, "runner_row": runner_row}
    finally:
        stop.set()
        await scheduler
        for sandbox_id in list(runner._sandboxes):
            try:
                await runner.destroy_sandbox(sandbox_id)
            except Exception:  # noqa: BLE001 - teardown
                pass
        async with factory() as db:
            entities = [ids["entity"]]
            reservation_ids = (
                await db.execute(select(SandboxReservation.id).where(SandboxReservation.entity_id.in_(entities)))
            ).scalars().all()
            run_ids = (await db.execute(select(RuntimeRun.id).where(RuntimeRun.entity_id.in_(entities)))).scalars().all()
            await db.execute(delete(SandboxInstance).where(SandboxInstance.reservation_id.in_(reservation_ids)))
            await db.execute(delete(SandboxReservation).where(SandboxReservation.entity_id.in_(entities)))
            await db.execute(delete(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id.in_([*run_ids, *reservation_ids])))
            await db.execute(delete(RuntimeRun).where(RuntimeRun.entity_id.in_(entities)))
            await db.execute(delete(SandboxRunner).where(SandboxRunner.id == runner_row.id))
            await db.commit()


async def _scheduler_loop(factory, stop: asyncio.Event) -> None:
    """The sandbox scheduler's allocation steps for this test's tickets."""

    while not stop.is_set():
        try:
            await _schedule_once(factory)
        except Exception:  # noqa: BLE001 - surfaced by the test's own assertions
            import logging

            logging.getLogger(__name__).exception("test scheduler step failed")
        await asyncio.sleep(0.05)


async def _schedule_once(factory) -> None:
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.services.sandbox_queue_service import (
        adopt_follower_reservations,
        claim_next_sandbox_allocation,
        complete_sandbox_allocation,
        sandbox_create_idempotency_key,
    )

    async with factory() as db:
        await adopt_follower_reservations(db)
        claim = await claim_next_sandbox_allocation(db)
        await db.commit()
    if claim is None:
        return
    reservation, runner = claim
    payload = dict(reservation.request_payload or {})
    async with sandbox_sdk.SandboxClient(base_url=runner.base_url) as client:
        created = await client.create_from_files(
            skill_name=payload["skill_name"],
            files=dict(payload["files"]),
            env=dict(payload.get("env") or {}),
            auto_install=False,
            idempotency_key=sandbox_create_idempotency_key(reservation.id, payload),
        )
    async with factory() as db:
        await complete_sandbox_allocation(
            db,
            reservation_id=reservation.id,
            sandbox_id=created.sandbox_id,
            runner_id=runner.id,
            expected_reservation_version=reservation.version,
        )
        await db.commit()


async def _new_run(factory, ids, conversation: str, *, user: str | None = None) -> str:
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus

    run_id = ("01R" + uuid.uuid4().hex.upper())[:26]
    async with factory() as db:
        db.add(RuntimeRun(
            id=run_id, root_run_id=run_id, conversation_id=conversation,
            entity_id=ids["entity"], user_id=user or ids["user"], status=RuntimeRunStatus.RUNNING.value,
        ))
        await db.commit()
    return run_id


def _ctx(ids, run_id: str, conversation: str, call: str, attempt: int = 1, *, user: str | None = None) -> dict:
    return {
        "entity_id": ids["entity"],
        "user_id": user or ids["user"],
        "conversation_id": conversation,
        "_agent_id_from_context": "agent-main",
        "_runtime_run_id_from_context": run_id,
        "_runtime_tool_call_id_from_context": call,
        "_runtime_tool_attempt_from_context": attempt,
    }


async def _park(factory, run_id: str, suspension) -> None:
    """What the agent loop does with a RuntimeToolSuspension: persist and release the run."""

    from packages.core.services.runtime_run_service import persist_runtime_run_suspension

    control = suspension.to_dict()
    control["checkpoint"] = {"messages": [], "pending_tool_call": {"id": "c", "name": "sandbox", "arguments": {}}}
    async with factory() as db:
        await persist_runtime_run_suspension(db, run_id=run_id, control=control)
        await db.commit()


async def _resumable(factory, run_id: str) -> bool:
    """The run can be claimed again (its ticket is allocated / its command left the queue)."""

    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.services.runtime_run_service import claim_runtime_run_execution

    async with factory() as db:
        claimed = await claim_runtime_run_execution(db, run_id=run_id, worker_id=f"test-{run_id}")
        if claimed is not None:
            # Hand the lease back so the next tool call is not "leased elsewhere".
            run = await db.get(RuntimeRun, run_id)
            run.lease_owner = None
            run.lease_expires_at = None
        await db.commit()
        return claimed is not None


async def _until(predicate, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.05)


def _sandbox_id(text: str) -> str:
    for line in str(text).splitlines():
        if line.startswith("sandbox_id: "):
            return line.split(": ", 1)[1].strip()
    raise AssertionError(text)


async def _wake(env) -> int:
    from packages.core.tasks.runtime_tasks import _wake_exec_slot_waiters

    return await _wake_exec_slot_waiters(env["factory"])


async def _open_for_runs(env, ids, runs: list[tuple[str, str]], call: str = "create") -> list[str]:
    """Every run opens the user's sandbox: park on the (shared) ticket, resume, attach."""

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import sandbox_tools

    factory = env["factory"]
    first = await asyncio.gather(*(
        sandbox_tools._sandbox_create(**_ctx(ids, run_id, conversation, call)) for run_id, conversation in runs
    ))
    for (run_id, _), outcome in zip(runs, first):
        assert isinstance(outcome, RuntimeToolSuspension), outcome
        await _park(factory, run_id, outcome)
    for run_id, _ in runs:
        await _until(lambda run_id=run_id: _resumable(factory, run_id))
    resumed = await asyncio.gather(*(
        sandbox_tools._sandbox_create(**_ctx(ids, run_id, conversation, call, attempt=2))
        for run_id, conversation in runs
    ))
    return [_sandbox_id(text) for text in resumed]


@pytest.mark.asyncio
async def test_one_user_seven_conversations_one_container_four_slots_fifo_and_clean_cancel(env, ids):
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import sandbox_tools
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus, SandboxReservation, SandboxReservationStatus
    from packages.core.services.runtime_run_service import cancel_runtime_run_resources

    factory, runner, host = env["factory"], env["runner"], env["host"]
    conversations = [f"01C{index}{ids['suffix']}AAAAAA"[:26] for index in range(7)]
    runs = [(await _new_run(factory, ids, conversation), conversation) for conversation in conversations]

    # ── single flight: ONE container for the user, whatever the conversation ──
    opened = await _open_for_runs(env, ids, runs)
    assert len(set(opened)) == 1
    sandbox_id = opened[0]
    assert len(host.running_containers()) == 1 and list(runner._sandboxes) == [sandbox_id]
    async with factory() as db:
        tickets = (await db.execute(
            select(SandboxReservation).where(SandboxReservation.entity_id == ids["entity"])
        )).scalars().all()
    leaders = [ticket for ticket in tickets if ticket.sandbox_id]
    assert len(leaders) == 1, "exactly one ticket created a container; the others followed it"
    assert runner._sandboxes[sandbox_id].max_concurrent_executions == 4

    # ── 7 commands, 4 slots: 4 run, 3 durable runs park instead of "busy" ──
    # (Started one after another so that arrival order -- FIFO -- is known.)
    tasks = []
    for index, (run_id, conversation) in enumerate(runs[:4]):
        tasks.append(asyncio.create_task(sandbox_tools._sandbox_exec(
            sandbox_id=sandbox_id, command=f"work-{index}", **_ctx(ids, run_id, conversation, f"exec-{index}"),
        )))
        assert await asyncio.to_thread(host.wait_started, f"work-{index}")
    parked = []
    for index, (run_id, conversation) in enumerate(runs[4:], start=4):
        parked.append(await sandbox_tools._sandbox_exec(
            sandbox_id=sandbox_id, command=f"work-{index}", **_ctx(ids, run_id, conversation, f"exec-{index}"),
        ))
    assert all(isinstance(item, RuntimeToolSuspension) for item in parked), parked
    assert {item.reason for item in parked} == {"sandbox_exec_slot"}
    for (run_id, _), suspension in zip(runs[4:], parked):
        await _park(factory, run_id, suspension)
    async with factory() as db:
        parked_runs = [await db.get(RuntimeRun, run_id) for run_id, _ in runs[4:]]
        assert [(run.status, run.status_reason) for run in parked_runs] == [
            (RuntimeRunStatus.WAITING_RESOURCE.value, "sandbox_exec_slot")
        ] * 3
    sandbox = runner._sandboxes[sandbox_id]
    assert len(sandbox.queued_execution_ids) == 3
    # Each waiting command is its own conversation's: fair order is arrival order.
    queued_commands = [sandbox._waiters[eid].command for eid in sandbox.queued_execution_ids]
    assert queued_commands == ["work-4", "work-5", "work-6"]

    # ── a waiting run is cancelled: its command leaves the queue, never runs ──
    cancelled_run = runs[6][0]
    async with factory() as db:
        run = await db.get(RuntimeRun, cancelled_run)
        run.status = RuntimeRunStatus.CANCELLED.value
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
    await cancel_runtime_run_resources(run)
    assert [sandbox._waiters[eid].command for eid in sandbox.queued_execution_ids] == ["work-4", "work-5"]
    await _wake(env)
    async with factory() as db:
        ticket = (await db.execute(select(SandboxReservation).where(
            SandboxReservation.runtime_run_id == cancelled_run,
            SandboxReservation.tool_call_id == "exec-6:exec-slot",
        ))).scalar_one()
        assert ticket.status == SandboxReservationStatus.CANCELLED.value
    assert len(runner._sandboxes) == 1, "a cancelled run never destroys the user's container"

    # ── slots free one at a time: the parked runs resume in FIFO order ──
    assert await _wake(env) == 0, "nothing left the queue yet"
    resumed = []
    for index, released in ((4, "work-0"), (5, "work-1")):
        host.release(released)
        assert await asyncio.to_thread(host.wait_started, f"work-{index}")
        await _until(lambda: _wake_and_check(env, runs[index][0]))
        run_id, conversation = runs[index]
        resumed.append(asyncio.create_task(sandbox_tools._sandbox_exec(
            sandbox_id=sandbox_id, command=f"work-{index}", **_ctx(ids, run_id, conversation, f"exec-{index}", attempt=2),
        )))
    for index in (2, 3, 4, 5):
        host.release(f"work-{index}")
    results = [*await asyncio.gather(*tasks[:4]), *await asyncio.gather(*resumed)]
    assert results == [f"out:work-{index}\n\n[exit_code: 0]" for index in range(6)]
    assert host.start_order == [f"work-{index}" for index in range(6)], "FIFO; the cancelled command never ran"
    # Each command ran exactly once: a resumed call collected its parked execution.
    assert len(host.launches) == 6
    async with factory() as db:
        consumed = (await db.execute(select(SandboxReservation.status).where(
            SandboxReservation.tool_call_id.in_(["exec-4:exec-slot", "exec-5:exec-slot"]),
        ))).scalars().all()
    assert consumed == [SandboxReservationStatus.CONSUMED.value] * 2

    # ── another user: their own container; this one is denied to them by id ──
    # (A member of a shared workspace conversation is just another user.)
    other_conversation = f"01X{ids['suffix']}AAAAAAA"[:26]
    other_run = await _new_run(factory, ids, other_conversation, user=ids["other_user"])
    [other_sandbox] = await _open_for_runs_as(env, ids, other_run, other_conversation, ids["other_user"])
    assert other_sandbox != sandbox_id and len(runner._sandboxes) == 2
    denied = json.loads(await sandbox_tools._sandbox_exec(
        sandbox_id=sandbox_id, command="id",
        **_ctx(ids, other_run, other_conversation, "steal", user=ids["other_user"]),
    ))
    assert denied["error"]["code"] == "sandbox_access_denied"

    # ── user-less system work: a container of its own task, denied to the user ──
    userless = await sandbox_tools._sandbox_create(
        entity_id=ids["entity"], user_id="", task_id=ids["task"], conversation_id=conversations[0],
        _agent_id_from_context="agent-system",
    )
    userless_id = _sandbox_id(userless)
    assert userless_id not in {sandbox_id, other_sandbox}
    for intruder in (_ctx(ids, runs[0][0], conversations[0], "peek"),):
        refused = json.loads(await sandbox_tools._sandbox_exec(sandbox_id=userless_id, command="id", **intruder))
        assert refused["error"]["code"] == "sandbox_access_denied"
    refused = json.loads(await sandbox_tools._sandbox_exec(
        entity_id=ids["entity"], user_id="", task_id=ids["task"], conversation_id=conversations[0],
        _agent_id_from_context="agent-system", sandbox_id=sandbox_id, command="id",
    ))
    assert refused["error"]["code"] == "sandbox_access_denied"

    # ── handover and cancel follow the USER, not the conversation ──
    from packages.core.services.sandbox_queue_service import sandboxes_free_for_handover

    async def free() -> set[str]:
        async with factory() as db:
            return await sandboxes_free_for_handover(
                db, runner_id=env["runner_row"].id, sandbox_ids=[sandbox_id],
            )

    # The run whose ticket created the container is cancelled while the
    # user's other conversations still run: the container stays theirs.
    leader_run = leaders[0].runtime_run_id
    async with factory() as db:
        run = await db.get(RuntimeRun, leader_run)
        run.status = RuntimeRunStatus.CANCELLED.value
        await db.commit()
        await db.refresh(run)
    await cancel_runtime_run_resources(run)
    assert sandbox_id in runner._sandboxes
    assert await free() == set(), "a live run of the user keeps the container from the queue"
    async with factory() as db:
        for run_id, _ in runs:
            run = await db.get(RuntimeRun, run_id)
            if run.status not in RuntimeRunStatus.terminal():
                run.status = RuntimeRunStatus.COMPLETED.value
        await db.commit()
    assert await free() == {sandbox_id}, "once all of the user's work ended, the queue may take it"


async def _wake_and_check(env, run_id: str) -> bool:
    await _wake(env)
    return await _resumable(env["factory"], run_id)


async def _open_for_runs_as(env, ids, run_id, conversation, user) -> list[str]:
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import sandbox_tools

    factory = env["factory"]
    first = await sandbox_tools._sandbox_create(**_ctx(ids, run_id, conversation, "create", user=user))
    assert isinstance(first, RuntimeToolSuspension), first
    await _park(factory, run_id, first)
    await _until(lambda: _resumable(factory, run_id))
    return [_sandbox_id(await sandbox_tools._sandbox_create(**_ctx(ids, run_id, conversation, "create", 2, user=user)))]


@pytest.mark.asyncio
async def test_container_lost_mid_flight_running_command_reported_not_started_ones_rerun(env, ids):
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import sandbox_tools

    factory, runner, host = env["factory"], env["runner"], env["host"]
    conversations = [f"01L{index}{ids['suffix']}AAAAAA"[:26] for index in range(5)]
    runs = [(await _new_run(factory, ids, conversation), conversation) for conversation in conversations]
    opened = await _open_for_runs(env, ids, runs)
    sandbox_id = opened[0]
    assert set(opened) == {sandbox_id}

    running = [
        asyncio.create_task(sandbox_tools._sandbox_exec(
            sandbox_id=sandbox_id, command=f"long-{index}", **_ctx(ids, run_id, conversation, f"long-{index}"),
        ))
        for index, (run_id, conversation) in enumerate(runs[:4])
    ]
    for index in range(4):
        assert await asyncio.to_thread(host.wait_started, f"long-{index}")
    # A durable run's command waits for a slot: the run parks.
    run_id, conversation = runs[4]
    parked = await sandbox_tools._sandbox_exec(
        sandbox_id=sandbox_id, command="later", **_ctx(ids, run_id, conversation, "later"),
    )
    assert isinstance(parked, RuntimeToolSuspension) and parked.reason == "sandbox_exec_slot"
    await _park(factory, run_id, parked)
    # A task worker (no durable run) waits in-process for a slot.
    worker = asyncio.create_task(sandbox_tools._sandbox_exec(
        entity_id=ids["entity"], user_id=ids["user"], task_id=ids["task"], conversation_id="",
        _agent_id_from_context="agent-worker", sandbox_id=sandbox_id, command="worker-job",
    ))
    await _until(lambda: _queued(runner, sandbox_id, 2))

    # The container dies (host OOM, daemon restart...).
    [container] = host.running_containers()
    host.kill_container(container)

    # Running commands: an explicit loss, never re-run.
    lost = [json.loads(result) for result in await asyncio.gather(*running)]
    assert {item["error"]["code"] for item in lost} == {"sandbox_lost_while_running"}
    assert all("NOT retried" in item["error"]["message"] for item in lost)

    # The task worker's command never started: it runs on the user's new container.
    await _until(lambda: _started(host, "worker-job"), timeout=30)
    host.release("worker-job")
    moved = await worker
    assert moved.startswith(f"[sandbox {sandbox_id} was gone before this command started")
    assert moved.endswith("out:worker-job\n\n[exit_code: 0]")
    replacement = moved.split("new sandbox ")[1].split(".")[0]
    assert replacement != sandbox_id and replacement in runner._sandboxes

    # The parked durable run wakes, finds the sandbox gone, and runs its
    # never-started command on the user's (new) container.
    await _until(lambda: _wake_and_check(env, run_id))
    task = asyncio.create_task(sandbox_tools._sandbox_exec(
        sandbox_id=sandbox_id, command="later", **_ctx(ids, run_id, conversation, "later", attempt=2),
    ))
    await _until(lambda: _started_or_done(host, "later", task))
    if task.done() and isinstance(task.result(), RuntimeToolSuspension):
        # It waits for the user's container like any admission, then runs there.
        await _park(factory, run_id, task.result())
        await _until(lambda: _resumable(factory, run_id))
        task = asyncio.create_task(sandbox_tools._sandbox_exec(
            sandbox_id=sandbox_id, command="later", **_ctx(ids, run_id, conversation, "later", attempt=3),
        ))
        await _until(lambda: _started(host, "later"))
    host.release("later")
    final = await task
    assert "was gone before this command started" in final and final.endswith("out:later\n\n[exit_code: 0]")
    assert [launch["command"] for launch in host.launches].count("later") == 1
    assert [launch["container"] for launch in host.launches if launch["command"] == "later"] != [container]


@pytest.mark.asyncio
async def test_a_parked_run_wakes_at_its_deadline_even_when_the_runner_never_answers(env, ids, monkeypatch):
    """No lost requests: an unreachable runner never strands a parked run."""

    from datetime import timedelta

    from packages.core.models.runtime_run import RuntimeOutboxEvent, SandboxReservation, SandboxReservationStatus
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_queue_service import enqueue_exec_slot_wait
    from packages.core.models.runtime_run import RuntimeRun

    async def unreachable(_sandbox_id):
        return "http://127.0.0.1:9"  # nothing listens: every probe fails

    monkeypatch.setattr(sandbox_session_service, "sandbox_runner_base_url", unreachable)
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.services.sandbox_sdk.client import SandboxClient as RealClient

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", RealClient)
    factory = env["factory"]
    tickets = {}
    for name, offset in (("patient", 3600), ("overdue", -1)):
        run_id = await _new_run(factory, ids, f"01W{name}{ids['suffix']}AAAA"[:26])
        async with factory() as db:
            run = await db.get(RuntimeRun, run_id)
            ticket = await enqueue_exec_slot_wait(
                db, run=run, tool_call_id=f"call-{name}", sandbox_id="sb-down", execution_id=f"e-{name}",
                wait_seconds=60,
            )
            ticket.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=offset)
            run.status = "waiting_resource"
            run.checkpoint = {"messages": [], "pending_tool_call": {"id": "c", "name": "sandbox", "arguments": {}}}
            await db.commit()
            tickets[name] = (ticket.id, run_id)
    assert await _wake(env) == 1
    async with factory() as db:
        patient = await db.get(SandboxReservation, tickets["patient"][0])
        overdue = await db.get(SandboxReservation, tickets["overdue"][0])
        assert patient.status == SandboxReservationStatus.EXEC_WAITING.value
        assert (overdue.status, overdue.last_error) == (SandboxReservationStatus.EXEC_READY.value, "deadline")
        resume = await db.scalar(select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == tickets["overdue"][1],
            RuntimeOutboxEvent.event_type == "runtime.resume_run",
        ))
        assert resume is not None
    assert await _resumable(factory, tickets["overdue"][1])


async def _queued(runner, sandbox_id: str, count: int) -> bool:
    sandbox = runner._sandboxes.get(sandbox_id)
    return sandbox is not None and len(sandbox.queued_execution_ids) == count


async def _started(host, command: str) -> bool:
    return host.started[command].is_set()


async def _started_or_done(host, command: str, task: asyncio.Task) -> bool:
    return host.started[command].is_set() or task.done()


@pytest.mark.asyncio
async def test_concurrent_admissions_never_create_a_second_container(env, ids, monkeypatch):
    """Every admission looks up the in-flight leader and then enqueues. Two
    admissions that both look up before either enqueues would each lead and
    each create a container. The Redis owner lock normally serialises them;
    with Redis unavailable (admission then proceeds unlocked) the per-scope
    Postgres lock still forces the second to see the first. The lookup is
    slowed so the admissions overlap every time."""

    from packages.core.cache import cache
    from packages.core.models.runtime_run import SandboxReservation
    from packages.core.services import sandbox_queue_service

    async def redis_unavailable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cache, "acquire_lease", redis_unavailable)

    real_lookup = sandbox_queue_service.find_inflight_scope_reservation

    async def slow_lookup(*args, **kwargs):
        found = await real_lookup(*args, **kwargs)
        await asyncio.sleep(0.3)
        return found

    monkeypatch.setattr(sandbox_queue_service, "find_inflight_scope_reservation", slow_lookup)
    factory, host = env["factory"], env["host"]
    conversations = [f"01K{index}{ids['suffix']}AAAAAA"[:26] for index in range(3)]
    runs = [(await _new_run(factory, ids, conversation), conversation) for conversation in conversations]

    opened = await _open_for_runs(env, ids, runs)

    assert len(set(opened)) == 1
    assert len(host.running_containers()) == 1
    async with factory() as db:
        tickets = (await db.execute(
            select(SandboxReservation).where(SandboxReservation.entity_id == ids["entity"])
        )).scalars().all()
    assert len([ticket for ticket in tickets if ticket.sandbox_id]) == 1


@pytest.mark.asyncio
async def test_a_follower_is_never_claimed_for_a_container_of_its_own(db_session, ids):
    """Seen under load: the scheduler settles followers (``SKIP LOCKED``) and
    then claims a ticket in the same transaction. A follower whose row was
    locked during the settling, and unlocked just before the claim, was
    claimable once its leader had allocated -- and got a second container for
    the user. Followers are now only ever settled, never claimed."""

    from datetime import timedelta

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
        SANDBOX_FOLLOWS_PAYLOAD_KEY,
        adopt_follower_reservations,
        claim_next_sandbox_allocation,
        reservation_sandbox_id,
    )

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    runner = SandboxRunner(
        id=f"runner-f-{ids['suffix']}"[:80], base_url=f"http://runner-f-{ids['suffix'].lower()}",
        status=SandboxRunnerStatus.HEALTHY.value, active_limit=50, executing_limit=50,
    )
    runs = [
        RuntimeRun(
            id=("01F" + uuid.uuid4().hex.upper())[:26], conversation_id=f"01C{index}{ids['suffix']}AAAAAA"[:26],
            entity_id=ids["entity"], user_id=ids["user"], status=RuntimeRunStatus.WAITING_RESOURCE.value,
        )
        for index in range(2)
    ]
    for run in runs:
        run.root_run_id = run.id
    async with factory() as db:
        db.add_all([runner, *runs])
        await db.flush()
        leader = SandboxReservation(
            runtime_run_id=runs[0].id, root_run_id=runs[0].id, tool_call_id="lead",
            entity_id=ids["entity"], user_id=ids["user"], status=SandboxReservationStatus.ALLOCATED.value,
            enqueued_at=now, deadline_at=now + timedelta(minutes=5), request_payload={},
            runner_id=runner.id, sandbox_id=f"sbx-{ids['suffix']}",
        )
        db.add(leader)
        await db.flush()
        follower = SandboxReservation(
            runtime_run_id=runs[1].id, root_run_id=runs[1].id, tool_call_id="follow",
            entity_id=ids["entity"], user_id=ids["user"], status=SandboxReservationStatus.QUEUED.value,
            enqueued_at=now, deadline_at=now + timedelta(minutes=5), priority=-1000,
            request_payload={SANDBOX_FOLLOWS_PAYLOAD_KEY: leader.id},
        )
        db.add_all([follower, SandboxInstance(
            sandbox_id=leader.sandbox_id, reservation_id=leader.id, runtime_run_id=runs[0].id,
            root_run_id=runs[0].id, runner_id=runner.id, status="active",
        )])
        await db.commit()
    try:
        async with factory() as holder, factory() as scheduler:
            # Something else holds the follower's row while the scheduler settles followers...
            await holder.execute(
                select(SandboxReservation).where(SandboxReservation.id == follower.id).with_for_update()
            )
            assert await adopt_follower_reservations(scheduler, leader_id=leader.id) == 0
            # ...and lets go before the claim of the same scheduler step.
            await holder.rollback()
            claim = await claim_next_sandbox_allocation(scheduler)
            assert claim is None or claim[0].id != follower.id
            await scheduler.rollback()
        async with factory() as scheduler:
            # The next step settles it onto the leader's container.
            assert await adopt_follower_reservations(scheduler, leader_id=leader.id) == 1
            settled = await scheduler.get(SandboxReservation, follower.id)
            assert settled.status == SandboxReservationStatus.ALLOCATED.value
            assert reservation_sandbox_id(settled) == leader.sandbox_id
            await scheduler.rollback()
    finally:
        async with factory() as db:
            await db.execute(delete(SandboxInstance).where(SandboxInstance.runner_id == runner.id))
            await db.execute(delete(SandboxReservation).where(SandboxReservation.entity_id == ids["entity"]))
            await db.execute(delete(RuntimeRun).where(RuntimeRun.entity_id == ids["entity"]))
            await db.execute(delete(SandboxRunner).where(SandboxRunner.id == runner.id))
            await db.commit()


async def _allocated_leader(env, ids, conversation: str) -> str:
    """A durable run whose ticket created the user's container, not yet attached.

    Its container exists and its ticket is ALLOCATED; nothing is in the Redis
    registry yet (the run registers when it resumes).
    """

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import sandbox_tools

    factory = env["factory"]
    run_id = await _new_run(factory, ids, conversation)
    first = await sandbox_tools._sandbox_create(**_ctx(ids, run_id, conversation, "create"))
    assert isinstance(first, RuntimeToolSuspension), first
    await _park(factory, run_id, first)
    await _until(lambda: _resumable(factory, run_id))
    return run_id


def _registry_unavailable(monkeypatch, fake_cache) -> None:  # noqa: F811 - fixture value
    """The owner registry is lost (evicted, Redis down): reads miss, writes vanish."""

    from packages.core.services.sandbox_session_service import SANDBOX_OWNER_REGISTRY_PREFIX

    real_get, real_set = fake_cache.get, fake_cache.set

    async def get(key, *args, **kwargs):
        if str(key).startswith(SANDBOX_OWNER_REGISTRY_PREFIX):
            return None
        return await real_get(key, *args, **kwargs)

    async def set_(key, value, *args, **kwargs):
        if str(key).startswith(SANDBOX_OWNER_REGISTRY_PREFIX):
            return True
        return await real_set(key, value, *args, **kwargs)

    from packages.core import cache as cache_module

    monkeypatch.setattr(cache_module.cache, "get", get)
    monkeypatch.setattr(cache_module.cache, "set", set_)


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["durable_run", "task_worker", "durable_run_registry_lost"])
async def test_an_admission_racing_the_leaders_attach_gets_the_leaders_container(
    env, ids, monkeypatch, fake_cache, admission,  # noqa: F811 - fixture
):
    """The window the owner lock never covered, forced exactly.

    The leader's run attaches its container (Redis registration, ticket
    CONSUMED) without the owner lock. Another admission of the same user
    reads the empty registry, then -- held there by the test -- the leader
    attaches, then the admission goes on to look for the scope's ticket. A
    consumed ticket is no longer "in flight", so it used to enqueue a ticket
    of its own and the user got a second container. Postgres knows the
    scope's container (its active instance row): the admission must get it.
    """

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import sandbox_tools
    from packages.core.models.runtime_run import SandboxReservation, SandboxReservationStatus
    from packages.core.services import sandbox_session_service

    factory, host, runner = env["factory"], env["host"], env["runner"]
    leader_conversation = f"01A{ids['suffix']}AAAAAAA"[:26]
    other_conversation = f"01B{ids['suffix']}AAAAAAA"[:26]
    leader_run = await _allocated_leader(env, ids, leader_conversation)
    if admission == "durable_run_registry_lost":
        _registry_unavailable(monkeypatch, fake_cache)

    real_load = sandbox_session_service.load_sandbox_session
    read_empty, leader_attached = asyncio.Event(), asyncio.Event()

    async def load_then_hold(owner, *, scope):
        record = await real_load(owner, scope=scope)
        if record is None and not read_empty.is_set():
            read_empty.set()
            await leader_attached.wait()
        return record

    monkeypatch.setattr(sandbox_session_service, "load_sandbox_session", load_then_hold)
    if admission == "task_worker":
        racer = asyncio.create_task(sandbox_tools._sandbox_create(
            entity_id=ids["entity"], user_id=ids["user"], task_id=ids["task"], conversation_id="",
            _agent_id_from_context="agent-worker",
        ))
    else:
        other_run = await _new_run(factory, ids, other_conversation)
        racer = asyncio.create_task(
            sandbox_tools._sandbox_create(**_ctx(ids, other_run, other_conversation, "create"))
        )
    await asyncio.wait_for(read_empty.wait(), 10)

    # Inside the racer's window: the leader resumes and attaches.
    attached = await sandbox_tools._sandbox_create(**_ctx(ids, leader_run, leader_conversation, "create", 2))
    sandbox_id = _sandbox_id(attached)
    async with factory() as db:
        lead = (await db.execute(select(SandboxReservation).where(
            SandboxReservation.runtime_run_id == leader_run,
        ))).scalar_one()
        assert lead.status == SandboxReservationStatus.CONSUMED.value
    leader_attached.set()

    outcome = await asyncio.wait_for(racer, 30)
    assert not isinstance(outcome, RuntimeToolSuspension), "the racer queued a container of its own"
    assert _sandbox_id(outcome) == sandbox_id
    await asyncio.sleep(0.5)  # let the scheduler act on anything that was queued
    assert list(runner._sandboxes) == [sandbox_id] and len(host.running_containers()) == 1
    async with factory() as db:
        tickets = (await db.execute(
            select(SandboxReservation).where(SandboxReservation.entity_id == ids["entity"])
        )).scalars().all()
    assert [ticket.id for ticket in tickets] == [lead.id], "no second ticket for the user's scope"


@pytest.mark.asyncio
async def test_a_container_postgres_still_names_but_the_runner_lost_is_never_reused(
    env, ids, monkeypatch, fake_cache,  # noqa: F811 - fixture
):
    """Postgres is the authority for WHICH container a scope has, never for
    whether it is alive: a container the runner reclaimed behind the control
    plane's back (instance row still active, registry gone) is probed, retired
    in Postgres, and replaced by a NEW container."""

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import sandbox_tools
    from packages.core.models.runtime_run import SandboxInstance

    factory, runner = env["factory"], env["runner"]
    leader_conversation = f"01D{ids['suffix']}AAAAAAA"[:26]
    leader_run = await _allocated_leader(env, ids, leader_conversation)
    old = _sandbox_id(await sandbox_tools._sandbox_create(**_ctx(ids, leader_run, leader_conversation, "create", 2)))
    _registry_unavailable(monkeypatch, fake_cache)
    await runner.destroy_sandbox(old)  # the runner reaps it; nobody tells the control plane
    async with factory() as db:
        assert (await db.scalar(select(SandboxInstance.status).where(SandboxInstance.sandbox_id == old))) == "active"

    conversation = f"01E{ids['suffix']}AAAAAAA"[:26]
    run_id = await _new_run(factory, ids, conversation)
    first = await sandbox_tools._sandbox_create(**_ctx(ids, run_id, conversation, "create"))
    assert isinstance(first, RuntimeToolSuspension), first
    await _park(factory, run_id, first)
    await _until(lambda: _resumable(factory, run_id))
    new = _sandbox_id(await sandbox_tools._sandbox_create(**_ctx(ids, run_id, conversation, "create", 2)))
    assert new != old and list(runner._sandboxes) == [new]
    async with factory() as db:
        assert (await db.scalar(select(SandboxInstance.status).where(SandboxInstance.sandbox_id == old))) == "released"


@pytest.mark.asyncio
async def test_a_nested_skill_loop_finds_the_users_container_when_the_registry_lost_it(
    env, ids, monkeypatch, fake_cache,  # noqa: F811 - fixture
):
    """A prompt skill's nested loop may not queue a container (nothing could
    wait on it), so a registry miss used to refuse it -- even though the user
    has a container. Postgres names it; the nested call uses it."""

    from packages.core.ai.tools import sandbox_tools

    factory, runner = env["factory"], env["runner"]
    leader_conversation = f"01N{ids['suffix']}AAAAAAA"[:26]
    leader_run = await _allocated_leader(env, ids, leader_conversation)
    sandbox_id = _sandbox_id(
        await sandbox_tools._sandbox_create(**_ctx(ids, leader_run, leader_conversation, "create", 2))
    )
    _registry_unavailable(monkeypatch, fake_cache)

    conversation = f"01P{ids['suffix']}AAAAAAA"[:26]
    run_id = await _new_run(factory, ids, conversation)
    nested = await sandbox_tools._sandbox_create(
        **_ctx(ids, run_id, conversation, "nested-create"), _runtime_parent_skill_from_context="research",
    )
    assert _sandbox_id(nested) == sandbox_id, nested
    assert list(runner._sandboxes) == [sandbox_id]


async def _scope_fixture(factory, ids, *, leader_status: str, followers: int):
    """A scope's leader ticket in ``leader_status`` and ``followers`` tickets following it."""

    from datetime import timedelta

    from packages.core.models.runtime_run import (
        RuntimeRun,
        RuntimeRunStatus,
        SandboxReservation,
        SandboxReservationStatus,
        SandboxRunner,
        SandboxRunnerStatus,
    )
    from packages.core.services.sandbox_queue_service import (
        SANDBOX_FOLLOWS_PAYLOAD_KEY,
        SANDBOX_SCOPE_PAYLOAD_KEY,
    )

    now = datetime.now(timezone.utc)
    scope_key = f"sandbox:owner:{ids['entity']}:{ids['user']}:user"
    request = {SANDBOX_SCOPE_PAYLOAD_KEY: scope_key, "skill_name": "generic-sandbox", "files": {}}
    runner = SandboxRunner(
        id=f"runner-s-{ids['suffix']}"[:80], base_url=f"http://runner-s-{ids['suffix'].lower()}",
        status=SandboxRunnerStatus.HEALTHY.value, active_limit=50, executing_limit=50,
    )
    runs = []
    for index in range(followers + 1):
        run_id = ("01S" + uuid.uuid4().hex.upper())[:26]
        runs.append(RuntimeRun(
            id=run_id, root_run_id=run_id, conversation_id=f"01C{index}{ids['suffix']}AAAAAA"[:26],
            entity_id=ids["entity"], user_id=ids["user"], status=RuntimeRunStatus.WAITING_RESOURCE.value,
        ))
    async with factory() as db:
        db.add_all([runner, *runs])
        await db.flush()
        leader = SandboxReservation(
            runtime_run_id=runs[0].id, root_run_id=runs[0].id, tool_call_id="lead",
            entity_id=ids["entity"], user_id=ids["user"], status=leader_status,
            enqueued_at=now, deadline_at=now + timedelta(minutes=5), request_payload=dict(request),
        )
        db.add(leader)
        await db.flush()
        tickets = [
            SandboxReservation(
                runtime_run_id=run.id, root_run_id=run.id, tool_call_id=f"follow-{index}",
                entity_id=ids["entity"], user_id=ids["user"], status=SandboxReservationStatus.QUEUED.value,
                enqueued_at=now + timedelta(milliseconds=index + 1), deadline_at=now + timedelta(minutes=5),
                request_payload={**request, SANDBOX_FOLLOWS_PAYLOAD_KEY: leader.id},
            )
            for index, run in enumerate(runs[1:])
        ]
        db.add_all(tickets)
        await db.commit()
    return runner, leader, tickets


async def _scope_cleanup(factory, ids, runner) -> None:
    from packages.core.models.runtime_run import (
        RuntimeOutboxEvent,
        RuntimeRun,
        SandboxInstance,
        SandboxReservation,
        SandboxRunner,
    )

    async with factory() as db:
        reservation_ids = (await db.execute(
            select(SandboxReservation.id).where(SandboxReservation.entity_id == ids["entity"])
        )).scalars().all()
        await db.execute(delete(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id.in_(reservation_ids)))
        await db.execute(delete(SandboxInstance).where(SandboxInstance.runner_id == runner.id))
        await db.execute(delete(SandboxReservation).where(SandboxReservation.entity_id == ids["entity"]))
        await db.execute(delete(RuntimeRun).where(RuntimeRun.entity_id == ids["entity"]))
        await db.execute(delete(SandboxRunner).where(SandboxRunner.id == runner.id))
        await db.commit()


@pytest.mark.asyncio
async def test_followers_of_a_leader_that_failed_elect_one_new_leader(db_session, ids):
    """A leader that ended without a container used to turn EVERY follower into
    an ordinary ticket: each was then claimed and created a container. The
    followers are settled under the scope's lock instead: the first leads, the
    next follows it -- also when two scheduler replicas settle them at once
    (the second waits for the first's decision)."""

    from packages.core.models.runtime_run import SandboxReservation, SandboxReservationStatus
    from packages.core.services.sandbox_queue_service import (
        SANDBOX_FOLLOWS_PAYLOAD_KEY,
        adopt_follower_reservations,
        claim_next_sandbox_allocation,
    )

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    runner, leader, (first, second) = await _scope_fixture(
        factory, ids, leader_status=SandboxReservationStatus.FAILED.value, followers=2,
    )
    try:
        async with factory() as holder, factory() as replica_a, factory() as replica_b:
            # Replica A settles the first follower while the second is locked...
            await holder.execute(
                select(SandboxReservation).where(SandboxReservation.id == second.id).with_for_update()
            )
            assert await adopt_follower_reservations(replica_a) == 1
            await holder.rollback()
            # ...and replica B settles the second before A commits: it must wait.
            settle_b = asyncio.create_task(adopt_follower_reservations(replica_b))
            await asyncio.sleep(0.5)
            assert not settle_b.done(), "the second settlement must wait for the scope's lock"
            await replica_a.commit()
            assert await asyncio.wait_for(settle_b, 10) == 1
            await replica_b.commit()
        async with factory() as db:
            settled = {
                ticket.id: dict(ticket.request_payload or {})
                for ticket in (await db.execute(
                    select(SandboxReservation).where(SandboxReservation.id.in_([first.id, second.id]))
                )).scalars()
            }
        assert SANDBOX_FOLLOWS_PAYLOAD_KEY not in settled[first.id]
        assert settled[second.id].get(SANDBOX_FOLLOWS_PAYLOAD_KEY) == first.id
        async with factory() as db:
            claim = await claim_next_sandbox_allocation(db)
            assert claim is not None and claim[0].id == first.id
            assert await claim_next_sandbox_allocation(db) is None, "only one ticket may create the container"
            await db.rollback()
    finally:
        await _scope_cleanup(factory, ids, runner)


@pytest.mark.asyncio
async def test_a_lost_ticket_is_requeued_behind_the_scopes_new_leader(db_session, ids):
    """A run whose container was lost before it resumed is requeued -- behind
    the ticket already creating the user's next container, not as a second
    creator."""

    from datetime import timedelta

    from packages.core.models.runtime_run import RuntimeRun, SandboxReservation, SandboxReservationStatus
    from packages.core.services.sandbox_queue_service import (
        SANDBOX_FOLLOWS_PAYLOAD_KEY,
        recover_waiting_sandbox_runs,
    )

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    runner, leader, (lost,) = await _scope_fixture(
        factory, ids, leader_status=SandboxReservationStatus.QUEUED.value, followers=1,
    )
    try:
        async with factory() as db:
            ticket = await db.get(SandboxReservation, lost.id)
            payload = dict(ticket.request_payload)
            payload.pop(SANDBOX_FOLLOWS_PAYLOAD_KEY)
            ticket.request_payload = payload
            ticket.status = SandboxReservationStatus.RELEASED.value
            ticket.attempt_count = 1
            run = await db.get(RuntimeRun, ticket.runtime_run_id)
            run.reservation_id = ticket.id
            await db.commit()
        async with factory() as db:
            assert await recover_waiting_sandbox_runs(db, now=datetime.now(timezone.utc) + timedelta(seconds=1)) == 1
            await db.commit()
        async with factory() as db:
            requeued = await db.get(SandboxReservation, lost.id)
            assert requeued.status == SandboxReservationStatus.REQUEUED.value
            assert requeued.request_payload.get(SANDBOX_FOLLOWS_PAYLOAD_KEY) == leader.id
    finally:
        await _scope_cleanup(factory, ids, runner)
