"""Video Edit and workflow code nodes admit containers like every other consumer.

Before 2026-10 they built their own ``SandboxClient`` on
``SANDBOX_SERVICE_URL``: their containers took runner slots invisibly to the
scheduler, failed at once on a full runner instead of waiting, had no instance
row (a Stop could not destroy a running render; multi-runner routing ignored
them), and a Video Edit ``start`` orphaned the previous session's container.
These tests drive the real entry point against a real database and the real
scheduler allocation code, with an in-memory runner standing in for Docker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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
from sandbox_entry_fakes import FakeRunner

pytestmark = pytest.mark.integration

ENTITY = "entity_single_entry"


@pytest_asyncio.fixture
async def entry(db_session, monkeypatch, tmp_path):
    """External-runner coordination over the test database and a fake runner."""

    from conftest import TEST_DATABASE_URL

    import packages.core.database as db_module
    from packages.core.config import get_settings
    from packages.core.services import entity_fs, sandbox_sdk

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session", factory)
    monkeypatch.setattr(db_module, "create_worker_session", lambda: factory)

    settings = get_settings()
    for name, value in {
        "MANOR_RUNTIME_EXECUTION_MODE": "durable",
        "SANDBOX_COORDINATION_MODE": "external-runner",
        "SANDBOX_SERVICE_URL": "",
        "SANDBOX_API_TOKEN": "entry-secret",
        "SANDBOX_QUEUE_POLL_SECONDS": 1,
        "SANDBOX_QUEUE_MAX_WAIT_SECONDS": 30,
        "SANDBOX_QUEUE_MAX_PENDING": 100,
        "SANDBOX_QUEUE_MAX_PENDING_PER_USER": 3,
    }.items():
        monkeypatch.setattr(settings, name, value)

    fake = FakeRunner()
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", fake.client_class)
    tag = uuid.uuid4().hex[:8]
    runner_id = f"runner-entry-{tag}"
    # Runners come from SANDBOX_RUNNERS_JSON on external-runner deployments,
    # where SANDBOX_SERVICE_URL may be unset.
    monkeypatch.setattr(settings, "SANDBOX_RUNNERS_JSON", json.dumps([
        {"id": runner_id, "base_url": f"http://runner-entry-{tag}:8000"},
    ]))
    async with factory() as db:
        # The test process holds this database (conftest's advisory lock):
        # runners another run left behind must not take this test's tickets.
        await db.execute(update(SandboxRunner).values(status=SandboxRunnerStatus.DRAINING.value))
        db.add(SandboxRunner(
            id=runner_id, base_url=f"http://runner-entry-{tag}:8000",
            status=SandboxRunnerStatus.HEALTHY.value, active_limit=4, executing_limit=2,
        ))
        await db.commit()

    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))

    def atomic_write(entity_id, rel_path, data, **kwargs):
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)

    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", atomic_write)
    env = SimpleNamespace(factory=factory, fake=fake, runner_id=runner_id, tag=tag, root=tmp_path, runs=[])
    try:
        yield env
    finally:
        async with factory() as db:
            tickets = select(SandboxReservation.id).where(SandboxReservation.entity_id == ENTITY)
            await db.execute(delete(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id.in_(tickets)))
            await db.execute(delete(SandboxInstance).where(SandboxInstance.runner_id == runner_id))
            await db.execute(delete(SandboxReservation).where(SandboxReservation.entity_id == ENTITY))
            await db.execute(delete(SandboxRunner).where(SandboxRunner.id == runner_id))
            if env.runs:
                await db.execute(delete(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id.in_(env.runs)))
                await db.execute(delete(RuntimeRun).where(RuntimeRun.id.in_(env.runs)))
            await db.commit()
        await engine.dispose()


async def _run(env, *, user_id: str = "user_entry") -> RuntimeRun:
    run_id = f"01ENTRY{uuid.uuid4().hex[:19].upper()}"
    run = RuntimeRun(
        id=run_id, root_run_id=run_id, conversation_id=f"conv-{env.tag}-{run_id[-6:]}",
        entity_id=ENTITY, user_id=user_id, status=RuntimeRunStatus.RUNNING.value,
    )
    async with env.factory() as db:
        db.add(run)
        await db.commit()
    env.runs.append(run_id)
    return run


async def _park(env, run: RuntimeRun, suspension) -> None:
    """What the runtime does with a tool's suspension: checkpoint, then queue."""

    from packages.core.services.runtime_run_service import persist_runtime_run_suspension

    async with env.factory() as db:
        await persist_runtime_run_suspension(
            db, run_id=run.id, control={"checkpoint": {"messages": []}, "reservation_id": suspension.reservation_id},
        )
        await db.commit()


async def _schedule_once(env) -> str | None:
    """One scheduler tick: claim the fairest ticket, then allocate it."""

    from packages.core.services.sandbox_queue_service import claim_next_sandbox_allocation
    from packages.core.tasks.runtime_tasks import allocate_reserved_sandbox_once

    async with env.factory() as db:
        claim = await claim_next_sandbox_allocation(db)
        await db.commit()
    if claim is None:
        return None
    reservation, _runner = claim
    await allocate_reserved_sandbox_once(reservation_id=reservation.id, reservation_version=reservation.version)
    return reservation.id


async def _ticket(env, reservation_id: str) -> SandboxReservation:
    async with env.factory() as db:
        return await db.get(SandboxReservation, reservation_id)


async def _instance(env, sandbox_id: str) -> SandboxInstance | None:
    async with env.factory() as db:
        return await db.get(SandboxInstance, sandbox_id)


async def _runner_row(env) -> SandboxRunner:
    async with env.factory() as db:
        return await db.get(SandboxRunner, env.runner_id)


def _video_runtime(env):
    def handle(sandbox_id, command, container):
        if command.endswith("video_runtime.py probe"):
            return SimpleNamespace(stdout=json.dumps({"status": "ok", "runtime": {}}), stderr="", exit_code=0)
        if command in {"test -d /skill/project", "mkdir -p /skill/project"}:
            return SimpleNamespace(stdout="", stderr="", exit_code=0)
        if command.endswith("video_runtime.py manifest"):
            return SimpleNamespace(stdout=json.dumps({"status": "ok", "files": []}), stderr="", exit_code=0)
        raise AssertionError(f"unexpected command {command}")

    return handle


# ── one-shot render in a chat run ─────────────────────────────────────


async def test_a_chat_render_parks_on_its_own_ticket_resumes_and_discards_its_container(entry) -> None:
    from packages.core.ai.runtime import runtime_load_sandbox_binding, runtime_sandbox_scope
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import video_edit_tools

    run = await _run(entry)
    project = entry.root / "Videos" / "parked-render"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>approved</main>")

    def render(sandbox_id, command, container):
        assert "video_runtime.py render" in command
        container["files"]["/skill/project/renders/final.mp4"] = b"mp4-bytes"
        return SimpleNamespace(
            stdout=json.dumps({"status": "ok", "files": ["renders/final.mp4"]}), stderr="", exit_code=0,
        )

    entry.fake.exec_handler = render
    context = SimpleNamespace(
        entity_id=ENTITY, user_id="user_entry", agent_id="agent-1", conversation_id=run.conversation_id,
        sandbox_binding_key=run.conversation_id, runtime_run_id=run.id, runtime_tool_call_id="call-render",
        task_id=None, parent_skill=None,
    )

    with pytest.raises(video_edit_tools.VideoSandboxParked) as parked:
        await video_edit_tools._run_video_sandbox(project_dir=project, operation="render", context=context)
    suspension = parked.value.suspension
    # No container yet: the run is parked on a durable ticket for this call.
    assert isinstance(suspension, RuntimeToolSuspension)
    assert entry.fake.created == []
    await _park(entry, run, suspension)
    assert await _schedule_once(entry) == suspension.reservation_id

    code, _payload, outputs, _stderr = await video_edit_tools._run_video_sandbox(
        project_dir=project, operation="render", context=context,
    )
    assert code == 0 and outputs == {"renders/final.mp4": b"mp4-bytes"}
    [created] = entry.fake.created
    # The scheduler created it under the ticket's idempotency key with the
    # render's resources, recorded it for the run, and the one-shot released
    # it again after the command.
    assert created["idempotency_key"] == suspension.reservation_id
    assert created["config"]["memory"] == "4g" and created["config"]["network"] == "none"
    instance = await _instance(entry, created["sandbox_id"])
    assert instance.reservation_id == suspension.reservation_id
    assert instance.root_run_id == run.root_run_id and instance.runner_id == entry.runner_id
    assert instance.status == "released"
    ticket = await _ticket(entry, suspension.reservation_id)
    assert ticket.tool_call_id == "call-render:video-render"
    assert ticket.status == SandboxReservationStatus.RELEASED.value
    assert entry.fake.destroyed == [created["sandbox_id"]]
    assert entry.fake.live() == set()
    assert (await _runner_row(entry)).active_count == 0
    # A one-shot is never bound: no sandbox tool of the user's scope may operate it.
    scope = runtime_sandbox_scope(entity_id=ENTITY, user_id="user_entry", binding_key=run.conversation_id)
    assert await runtime_load_sandbox_binding(scope, created["sandbox_id"]) is None


# ── workflow code nodes ───────────────────────────────────────────────


async def test_workflow_code_node_waits_for_a_full_runner_instead_of_failing(entry) -> None:
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    # Every slot is taken: the node must wait on its ticket, not fail.
    async with entry.factory() as db:
        runner = await db.get(SandboxRunner, entry.runner_id)
        runner.active_count = runner.active_limit
        await db.commit()
    entry.fake.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        stdout='{"answer": 42}', stderr="", exit_code=0,
    )
    workflow_run = WorkflowRun(
        id=f"01WFENTRY{uuid.uuid4().hex[:17].upper()}", workflow_id="wf-entry", entity_id=ENTITY,
        variables={"value": 42}, step_results={}, started_by=None,
    )
    step = {"id": "calc", "type": "code", "config": {"code": "print(inputs)", "language": "python"}}
    node = asyncio.create_task(WorkflowRunner()._execute_step(step, workflow_run, None))

    async def queued_ticket():
        while True:
            async with entry.factory() as db:
                ticket = (await db.execute(select(SandboxReservation).where(
                    SandboxReservation.requester_id == f"workflow-run:{workflow_run.id}",
                ))).scalar_one_or_none()
            if ticket is not None:
                return ticket
            await asyncio.sleep(0.1)

    ticket = await asyncio.wait_for(queued_ticket(), timeout=30)
    assert ticket.requester_kind == "task" and ticket.user_id == ""  # a user-less scheduled run
    assert await _schedule_once(entry) is None, "nothing may be allocated while the runner is full"
    await asyncio.sleep(1.5)
    assert not node.done()

    async with entry.factory() as db:
        runner = await db.get(SandboxRunner, entry.runner_id)
        runner.active_count = 0
        await db.commit()
    assert await _schedule_once(entry) == ticket.id
    result = await asyncio.wait_for(node, timeout=30)

    assert result["status"] == "completed" and result["output"] == {"answer": 42}
    [created] = entry.fake.created
    assert created["skill_name"] == "workflow-code-calc" and created["config"]["network"] == "none"
    # A full container queues the command (fair per work area) instead of
    # answering busy (sandbox-user-conversation's exec queue).
    [executed] = [call for name, call in entry.fake.calls if name == "exec"]
    assert executed["queue_timeout"] > 0
    assert executed["fair_key"] == f"workflow-run:{workflow_run.id}"
    assert executed["skill"] == "workflow-code-calc"
    instance = await _instance(entry, created["sandbox_id"])
    assert instance.reservation_id == ticket.id and instance.status == "released"
    assert (await _ticket(entry, ticket.id)).status == SandboxReservationStatus.RELEASED.value
    assert entry.fake.live() == set()
    assert (await _runner_row(entry)).active_count == 0


# ── Video Edit ────────────────────────────────────────────────────────


def _video_call(run: RuntimeRun, call_id: str, *, agent: str = "agent-1", user: str = "user_entry", **arguments):
    from packages.core.ai.tools import video_edit_tools

    return video_edit_tools._video_edit_handler(
        entity_id=ENTITY, user_id=user, conversation_id=run.conversation_id,
        _user_id_from_context=user, _agent_id_from_context=agent,
        _runtime_run_id_from_context=run.id, _runtime_tool_call_id_from_context=call_id,
        **arguments,
    )


async def test_video_session_parks_resumes_survives_cancel_and_start_replaces_it(entry) -> None:
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.services.runtime_run_service import cancel_runtime_run_resources
    from packages.core.services.sandbox_session_service import is_sandbox_kept_warm

    run = await _run(entry)
    entry.fake.exec_handler = _video_runtime(entry)

    parked = await _video_call(run, "call-start", operation="start", project_path="Videos/entry")
    assert isinstance(parked, RuntimeToolSuspension)
    assert entry.fake.created == []
    await _park(entry, run, parked)
    assert await _schedule_once(entry) == parked.reservation_id
    started = json.loads(await _video_call(run, "call-start", operation="start", project_path="Videos/entry"))
    assert started["status"] == "ready"
    [first] = entry.fake.created
    assert first["skill_name"] == "video-edit-runtime"
    assert {key: first["config"][key] for key in ("network", "memory", "cpus", "pids_limit", "workdir_tmpfs_size", "exec_timeout")} == {
        "network": "none", "memory": "4g", "cpus": 4.0, "pids_limit": 512,
        "workdir_tmpfs_size": "2g", "exec_timeout": 1800,
    }
    instance = await _instance(entry, first["sandbox_id"])
    assert instance.status == "active" and instance.root_run_id == run.root_run_id
    assert (await _ticket(entry, parked.reservation_id)).tool_call_id == "call-start:video-session"
    # The session is the user's dedicated role container (one per user), and
    # every command in it queues for a slot fairly per work area instead of
    # answering busy when the user's other work fills it.
    assert (await _ticket(entry, parked.reservation_id)).request_payload["sandbox_scope"].endswith(
        ":user_entry:user:video-edit"
    )
    execs = [call for name, call in entry.fake.calls if name == "exec"]
    assert execs and all(
        call["queue_timeout"] > 0
        and call["fair_key"] == run.conversation_id
        and call["skill"] == "video-edit-runtime"
        for call in execs
    ), execs

    # The session outlives the run: a Stop of the run leaves it in place.
    assert await is_sandbox_kept_warm(first["sandbox_id"])
    async with entry.factory() as db:
        await cancel_runtime_run_resources(await db.get(RuntimeRun, run.id))
    assert first["sandbox_id"] in entry.fake.live()

    # The next turn's start replaces it: the old container is destroyed before
    # the new one is even queued.
    async with entry.factory() as db:
        old = await db.get(RuntimeRun, run.id)
        old.status = RuntimeRunStatus.COMPLETED.value
        await db.commit()
    next_turn = await _run(entry)
    async with entry.factory() as db:
        # A later turn of the same conversation.
        row = await db.get(RuntimeRun, next_turn.id)
        row.conversation_id = run.conversation_id
        await db.commit()
    next_turn.conversation_id = run.conversation_id
    parked_again = await _video_call(next_turn, "call-restart", operation="start", project_path="Videos/entry")
    assert isinstance(parked_again, RuntimeToolSuspension)
    assert entry.fake.destroyed == [first["sandbox_id"]]
    assert (await _instance(entry, first["sandbox_id"])).status == "released"
    await _park(entry, next_turn, parked_again)
    assert await _schedule_once(entry) == parked_again.reservation_id
    restarted = json.loads(await _video_call(next_turn, "call-restart", operation="start", project_path="Videos/entry"))
    assert restarted["status"] == "ready" and restarted["session_id"] != started["session_id"]
    second = entry.fake.created[-1]["sandbox_id"]
    assert entry.fake.live() == {second}

    # The old session record is gone with its container.
    stale = json.loads(await _video_call(next_turn, "call-stale", operation="status", session_id=started["session_id"]))
    assert "not found" in stale["message"]

    # The session is the user's: another agent of the conversation works in
    # it, another user cannot operate it.
    for agent in ("agent-2", ""):
        shared = json.loads(await _video_call(
            next_turn, "call-shared", operation="status", session_id=restarted["session_id"], agent=agent,
        ))
        assert shared["status"] == "active" and shared["recovered"] is False, agent
    denied = json.loads(await _video_call(
        next_turn, "call-denied", operation="status", session_id=restarted["session_id"], user="user_other",
    ))
    assert denied["error"] == "permission_denied"
    # The user owns it in all of their conversations.
    other_conversation = await _run(entry)
    elsewhere = json.loads(await _video_call(
        other_conversation, "call-elsewhere", operation="status", session_id=restarted["session_id"],
    ))
    assert elsewhere["status"] == "active" and elsewhere["recovered"] is False
    assert entry.fake.live() == {second}

    # The session record expires (24 h) long before the warm registry (7 d):
    # a start must still replace the container its scope registered, even
    # when another agent of the conversation starts it (the scope is the
    # user's conversation, not the agent).
    from packages.core.ai.runtime.video_edit_sessions import delete_video_edit_session, load_video_edit_session

    await delete_video_edit_session(await load_video_edit_session(restarted["session_id"]))
    parked_third = await _video_call(
        next_turn, "call-third", operation="start", project_path="Videos/entry", agent="agent-2",
    )
    assert isinstance(parked_third, RuntimeToolSuspension)
    assert entry.fake.live() == set()
    assert (await _instance(entry, second)).status == "released"
    await _park(entry, next_turn, parked_third)
    assert await _schedule_once(entry) == parked_third.reservation_id
    third_session = json.loads(await _video_call(
        next_turn, "call-third", operation="start", project_path="Videos/entry", agent="agent-2",
    ))
    third = entry.fake.created[-1]["sandbox_id"]
    assert entry.fake.live() == {third}

    closed = json.loads(await _video_call(next_turn, "call-close", operation="close", session_id=third_session["session_id"]))
    assert closed["status"] == "closed"
    assert entry.fake.live() == set()
    assert (await _instance(entry, third)).status == "released"
    assert (await _runner_row(entry)).active_count == 0


async def test_a_stop_cancels_a_running_render_and_destroys_its_container(entry) -> None:
    """A render inside the video-edit skill's own tool loop cannot park: it is
    allocated inline when a runner has room, recorded for the run, and the
    run's cancel stops the command and destroys the container."""

    from packages.core.ai.tools import video_edit_tools
    from packages.core.services.runtime_run_service import cancel_runtime_run_resources

    run = await _run(entry)
    project = entry.root / "Videos" / "render"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>approved</main>")
    rendering = asyncio.Event()

    async def render(sandbox_id, command, container):
        assert "video_runtime.py render" in command
        rendering.set()
        while sandbox_id in entry.fake.containers:
            await asyncio.sleep(0.05)
        return SimpleNamespace(stdout="", stderr="Killed", exit_code=137)

    entry.fake.exec_handler = render
    context = SimpleNamespace(
        entity_id=ENTITY, user_id="user_entry", agent_id="agent-1", conversation_id=run.conversation_id,
        sandbox_binding_key=run.conversation_id, runtime_run_id=run.id, runtime_tool_call_id="call-skill",
        task_id=None, parent_skill="video-edit",
    )
    job = asyncio.create_task(video_edit_tools._run_video_sandbox(
        project_dir=project, operation="render", context=context,
    ))
    await asyncio.wait_for(rendering.wait(), timeout=30)
    [created] = entry.fake.created
    sandbox_id = created["sandbox_id"]
    instance = await _instance(entry, sandbox_id)
    assert instance.status == "active" and instance.root_run_id == run.root_run_id
    ticket = await _ticket(entry, instance.reservation_id)
    assert ticket.requester_kind == "task" and ticket.requester_id == f"run:{run.id}"
    async with entry.factory() as db:
        live_run = await db.get(RuntimeRun, run.id)
        assert live_run.active_sandbox_id == sandbox_id
        execution_id = live_run.active_execution_id
    assert execution_id and execution_id.startswith("manor-")

    async with entry.factory() as db:
        await cancel_runtime_run_resources(await db.get(RuntimeRun, run.id))
    exit_code, _payload, outputs, _stderr = await asyncio.wait_for(job, timeout=30)

    assert exit_code == 137 and outputs == {}
    assert entry.fake.cancelled == [(sandbox_id, execution_id)]
    assert sandbox_id in entry.fake.destroyed
    assert entry.fake.live() == set()
    assert (await _instance(entry, sandbox_id)).status == "released"
    assert (await _runner_row(entry)).active_count == 0


async def test_an_inline_render_on_a_full_runner_fails_fast_and_leaves_no_ticket(entry) -> None:
    from packages.core.ai.tools import video_edit_tools

    run = await _run(entry)
    async with entry.factory() as db:
        runner = await db.get(SandboxRunner, entry.runner_id)
        runner.active_count = runner.active_limit
        await db.commit()
    context = SimpleNamespace(
        entity_id=ENTITY, user_id="user_entry", agent_id="agent-1", conversation_id=run.conversation_id,
        sandbox_binding_key=run.conversation_id, runtime_run_id=run.id, runtime_tool_call_id="call-skill",
        task_id=None, parent_skill="video-edit",
    )
    with pytest.raises(video_edit_tools.VideoEditRuntimeUnavailable) as raised:
        await video_edit_tools._run_video_sandbox(project_dir=Path(entry.root), operation="render", context=context)
    assert raised.value.code == "capacity_full" and raised.value.retryable
    assert entry.fake.created == []
    async with entry.factory() as db:
        tickets = (await db.execute(select(SandboxReservation).where(
            SandboxReservation.requester_id == f"run:{run.id}",
        ))).scalars().all()
    assert tickets == []


async def test_a_workflow_step_render_on_a_full_runner_waits_on_its_own_ticket(entry) -> None:
    """A Workflow agent step has no durable run to park and no task, but it is
    a background requester: it queues a ticket for its run and waits, so an
    on-demand render fleet can scale out, instead of failing at once with
    capacity_full (staging 2026-10-22: no render runner was ever started)."""

    from packages.core.ai.runtime.sandbox import runtime_workflow_binding_key
    from packages.core.ai.tools import video_edit_tools

    async with entry.factory() as db:
        runner = await db.get(SandboxRunner, entry.runner_id)
        runner.active_count = runner.active_limit
        await db.commit()
    workflow_run_id = f"01WF{uuid.uuid4().hex[:22].upper()}"
    context = SimpleNamespace(
        entity_id=ENTITY, user_id="user_entry", agent_id="agent-1", conversation_id=None,
        sandbox_binding_key=runtime_workflow_binding_key(workflow_run_id),
        runtime_run_id=None, runtime_tool_call_id="call-review",
        task_id=None, workflow_run_id=workflow_run_id, parent_skill=None,
    )
    entry.fake.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        stdout="", stderr="render stub", exit_code=1,
    )
    job = asyncio.create_task(video_edit_tools._run_video_sandbox(
        project_dir=Path(entry.root), operation="render", context=context,
    ))
    ticket = None
    for _ in range(200):
        async with entry.factory() as db:
            ticket = (await db.execute(select(SandboxReservation).where(
                SandboxReservation.requester_id == f"workflow-run:{workflow_run_id}",
            ))).scalars().first()
        if ticket is not None or job.done():
            break
        await asyncio.sleep(0.05)
    assert not job.done(), "a Workflow step must wait for a runner, not fail fast"
    assert ticket is not None and ticket.status == SandboxReservationStatus.QUEUED.value

    # Once a slot frees, the waiting call gets its container and runs.
    async with entry.factory() as db:
        runner = await db.get(SandboxRunner, entry.runner_id)
        runner.active_count = 0
        await db.commit()
    assert await _schedule_once(entry) == ticket.id
    with contextlib.suppress(video_edit_tools.VideoEditRuntimeUnavailable):
        await asyncio.wait_for(job, timeout=30)
    assert [c["sandbox_id"] for c in entry.fake.created] and entry.fake.live() == set()


# ── one entry point x one container per scope (sandbox-user-conversation) ──


async def test_an_inline_session_start_never_joins_or_claims_a_ticket_another_call_is_creating(entry) -> None:
    """A Video Edit start inside the skill's own tool loop cannot park or wait.
    While another conversation of the same user is creating the user's video
    session container, it neither joins that ticket nor claims it inline
    (giving up would cancel the ticket the other call waits on): it is
    refused retryably and writes nothing."""

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import video_edit_tools

    first = await _run(entry)
    parked = await _video_call(first, "call-start", operation="start", project_path="Videos/entry")
    assert isinstance(parked, RuntimeToolSuspension)
    await _park(entry, first, parked)

    second = await _run(entry)
    context = SimpleNamespace(
        entity_id=ENTITY, user_id="user_entry", agent_id="agent-1", conversation_id=second.conversation_id,
        sandbox_binding_key=second.conversation_id, runtime_run_id=second.id, runtime_tool_call_id="call-skill",
        task_id=None, parent_skill="video-edit",
    )
    with pytest.raises(video_edit_tools.VideoEditRuntimeUnavailable) as raised:
        await video_edit_tools._admit_video_sandbox(context, session=True)
    assert raised.value.code == "capacity_full" and raised.value.retryable

    assert entry.fake.created == []
    leader = await _ticket(entry, parked.reservation_id)
    assert leader.status == SandboxReservationStatus.QUEUED.value and leader.runner_id is None
    async with entry.factory() as db:
        inline = (await db.execute(select(SandboxReservation).where(
            SandboxReservation.requester_id == f"run:{second.id}",
        ))).scalars().all()
    assert inline == []
    # The other call still gets its container.
    assert await _schedule_once(entry) == parked.reservation_id


async def test_a_session_start_replaces_the_container_only_postgres_still_names(entry) -> None:
    """The warm registry is a cache: when it lost the user's session container,
    Postgres still names it (its active instance row). A start must replace
    that container too -- never adopt it through load_skill."""

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.runtime.video_edit_sessions import delete_video_edit_session, load_video_edit_session
    from packages.core.services.sandbox_session_service import SandboxOwner, forget_sandbox_session

    run = await _run(entry)
    entry.fake.exec_handler = _video_runtime(entry)
    parked = await _video_call(run, "call-start", operation="start", project_path="Videos/entry")
    await _park(entry, run, parked)
    assert await _schedule_once(entry) == parked.reservation_id
    started = json.loads(await _video_call(run, "call-start", operation="start", project_path="Videos/entry"))
    assert started["status"] == "ready"
    [first] = entry.fake.created

    # Neither the warm registry nor the session record names it any more.
    await forget_sandbox_session(
        SandboxOwner(entity_id=ENTITY, user_id="user_entry"), first["sandbox_id"], scope="user:video-edit",
    )
    await delete_video_edit_session(await load_video_edit_session(started["session_id"]))
    async with entry.factory() as db:
        old = await db.get(RuntimeRun, run.id)
        old.status = RuntimeRunStatus.COMPLETED.value
        await db.commit()
    next_turn = await _run(entry)
    parked_again = await _video_call(next_turn, "call-restart", operation="start", project_path="Videos/entry")

    assert isinstance(parked_again, RuntimeToolSuspension)
    assert entry.fake.destroyed == [first["sandbox_id"]]
    assert (await _instance(entry, first["sandbox_id"])).status == "released"
    assert not [name for name, _call in entry.fake.calls if name == "load_skill"]
