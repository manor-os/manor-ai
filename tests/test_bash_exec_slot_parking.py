"""bash parks a durable run while its command waits for a sandbox slot.

The user's shared sandbox has a fixed number of command slots. When every
slot is taken by the user's other work, a durable chat run must not wait
inside the tool (a worker blocked in a tool starves the control plane):
like the ``sandbox`` tool's exec, bash leaves its command in the runner's
fair FIFO and parks the run (``waiting_resource`` / ``sandbox_exec_slot``)
on the generic exec-slot ticket. The scheduler's exec-slot sweep wakes the
run once the command left the queue, and the resumed call collects that
same execution and saves its file changes back exactly once. Callers that
cannot park (task workers, workflows, system contexts) wait in-process,
bounded by ``SANDBOX_EXEC_QUEUE_WAIT_SECONDS``, and are told accurately when
the command never got a slot. A command parked on a sandbox that is lost
never started, so it runs once on the user's new sandbox (no request is
lost); one that was running when its sandbox was lost is reported, never
run again.

Driven on real Postgres (reservation tickets, run parking, the wake sweep)
against ``bash_sandbox_fakes.LocalSandbox`` with a slot queue (real ``sh``,
real mirror scripts).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from sqlalchemy import delete, select

import packages.core.database as db_module
from packages.core.ai.tools import bash_tool
from packages.core.config import get_settings
from tests.test_bash_in_sandbox import fs_root, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.integration

COMMAND = "echo parked > Knowledge/parked.md && echo done"


@pytest.fixture
def durable(world, monkeypatch):  # noqa: F811 - fixture
    from packages.core.services import sandbox_session_service

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(settings, "SANDBOX_EXEC_QUEUE_WAIT_SECONDS", 30)
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_POLL_SECONDS", 1)
    sandbox = world["sandbox"]
    sandbox.slots = 1

    async def client_for(_sandbox_id, timeout=None):
        return sandbox

    # The wake sweep and run cancellation reach the runner through the entry point.
    monkeypatch.setattr(sandbox_session_service, "sandbox_client_for", client_for)
    return world


async def _new_run(world) -> str:
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus

    run_id = ("01R" + uuid.uuid4().hex.upper())[:26]
    async with db_module.async_session() as db:
        db.add(RuntimeRun(
            id=run_id, root_run_id=run_id, conversation_id="conv-1",
            entity_id=world["entity_id"], user_id=world["owner"], status=RuntimeRunStatus.RUNNING.value,
        ))
        await db.commit()
    return run_id


async def _bash(world, command: str, *, run_id: str | None = None, call: str = "call-1", attempt: int = 1, **extra):
    context = {
        "_user_id_from_context": world["owner"],
        "_agent_id_from_context": "agent-1",
        "conversation_id": "conv-1",
    }
    if run_id:
        context.update({
            "_runtime_run_id_from_context": run_id,
            "_runtime_tool_call_id_from_context": call,
            "_runtime_tool_attempt_from_context": attempt,
        })
    return await bash_tool._bash(world["entity_id"], command=command, **context, **extra)


async def _park(run_id: str, suspension) -> None:
    """What the agent loop does with a RuntimeToolSuspension: persist and release the run."""

    from packages.core.services.runtime_run_service import persist_runtime_run_suspension

    control = suspension.to_dict()
    control["checkpoint"] = {"messages": [], "pending_tool_call": {"id": "c", "name": "bash", "arguments": {}}}
    async with db_module.async_session() as db:
        await persist_runtime_run_suspension(db, run_id=run_id, control=control)
        await db.commit()


async def _wake() -> int:
    from packages.core.tasks.runtime_tasks import _wake_exec_slot_waiters

    return await _wake_exec_slot_waiters(db_module.async_session)


async def _ticket(run_id: str, call: str = "call-1"):
    from packages.core.models.runtime_run import SandboxReservation

    async with db_module.async_session() as db:
        return (await db.execute(select(SandboxReservation).where(
            SandboxReservation.runtime_run_id == run_id,
            SandboxReservation.tool_call_id == f"{call}:exec-slot",
        ))).scalar_one_or_none()


async def _cleanup(run_ids: list[str]) -> None:
    from packages.core.models.runtime_run import RuntimeOutboxEvent, RuntimeRun, SandboxReservation

    async with db_module.async_session() as db:
        reservation_ids = (await db.execute(
            select(SandboxReservation.id).where(SandboxReservation.runtime_run_id.in_(run_ids))
        )).scalars().all()
        await db.execute(delete(SandboxReservation).where(SandboxReservation.runtime_run_id.in_(run_ids)))
        await db.execute(delete(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id.in_([*run_ids, *reservation_ids])
        ))
        await db.execute(delete(RuntimeRun).where(RuntimeRun.id.in_(run_ids)))
        await db.commit()


async def _until(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.05)


def _stagings(sandbox) -> int:
    return sum("/manor-stage-" in call["command"] for call in sandbox.calls)


def _user_launches(sandbox) -> list[str]:
    """User commands that took a slot and ran (the mirror scripts are not user commands)."""
    return [
        eid for eid in sandbox.launched
        if not sandbox.executions[eid].command.startswith("python3 -c")
    ]


@pytest.mark.asyncio
async def test_a_full_sandbox_parks_the_run_and_the_resume_collects_and_saves_exactly_once(durable, monkeypatch):
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus, SandboxReservationStatus

    world, sandbox = durable, durable["sandbox"]
    saves: list[str] = []
    real_save = bash_tool._save_changes

    async def counting_save(**kwargs):
        saves.append(kwargs["command"])
        return await real_save(**kwargs)

    monkeypatch.setattr(bash_tool, "_save_changes", counting_save)
    run_id = await _new_run(world)
    try:
        # Every slot of the user's sandbox is taken by their other work.
        sandbox.occupy("other-conversation")
        started = time.monotonic()
        parked = await _bash(world, COMMAND, run_id=run_id)
        assert isinstance(parked, RuntimeToolSuspension), parked
        assert parked.kind == "waiting_resource" and parked.reason == "sandbox_exec_slot"
        # It parked at once instead of waiting out the 30s slot budget in-process,
        # after staging ran on the side channel next to the full slots.
        assert time.monotonic() - started < 10
        helpers = [call for call in sandbox.calls if call["command"].startswith("python3 -c")]
        assert helpers and all(call.get("side_channel") is True for call in helpers)
        assert len(sandbox.waiting) == 1 and _user_launches(sandbox) == []
        assert saves == [], "nothing is saved back before the command ran"
        await _park(run_id, parked)
        async with db_module.async_session() as db:
            run = await db.get(RuntimeRun, run_id)
            assert (run.status, run.status_reason) == (RuntimeRunStatus.WAITING_RESOURCE.value, "sandbox_exec_slot")
            # Cancelling the parked run would withdraw exactly this command.
            assert run.active_execution_id == sandbox.queued_execution_ids()[0]
        ticket = await _ticket(run_id)
        assert ticket.status == SandboxReservationStatus.EXEC_WAITING.value
        payload = ticket.request_payload
        assert payload["execution_id"] == sandbox.queued_execution_ids()[0]
        assert payload["bash"]["command"].endswith(COMMAND)
        assert "MANOR_CLI_TOKEN" not in json.dumps(payload), "the ticket never holds the CLI token"

        assert await _wake() == 0, "still queued: the run stays parked"

        # The other work ends: the queued command takes the slot and runs.
        sandbox.free("other-conversation")
        await _until(lambda: _done(sandbox, payload["execution_id"]))
        assert await _wake() == 1
        assert (await _ticket(run_id)).status == SandboxReservationStatus.EXEC_READY.value
        # The user's work fills the sandbox again before the run resumes: the
        # read-back still runs on the side channel, never behind that work.
        sandbox.occupy("other-conversation-2")

        stagings = _stagings(sandbox)
        resumed_raw = await _bash(world, COMMAND, run_id=run_id, attempt=2)
        assert isinstance(resumed_raw, str), (
            "the resumed call collects its parked command; it never starts (and parks) it again"
        )
        resumed = json.loads(resumed_raw)
        assert resumed["exit_code"] == 0 and resumed["stdout"].strip() == "done", resumed
        assert (world["root"] / "Knowledge" / "parked.md").read_text() == "parked\n"
        assert _user_launches(sandbox) == [payload["execution_id"]], "collected, never run again"
        assert _stagings(sandbox) == stagings, "the working copy it ran on is never staged again"
        assert len(saves) == 1, "file changes are saved back exactly once"
        assert (await _ticket(run_id)).status == SandboxReservationStatus.CONSUMED.value
        async with db_module.async_session() as db:
            assert (await db.get(RuntimeRun, run_id)).active_execution_id is None
    finally:
        sandbox.free("other-conversation")
        sandbox.free("other-conversation-2")
        await _cleanup([run_id])


async def _done(sandbox, execution_id: str) -> bool:
    record = sandbox.executions.get(execution_id)
    return bool(record and record.terminal)


@pytest.mark.asyncio
async def test_a_parked_workspace_command_is_saved_back_under_its_workspace_scope(durable, monkeypatch):
    """The resumed call never resolves the Workspace again: the ticket carries
    the output folder the command started with, so a new file it left at the
    entity root is refused on resume as it would have been in-process."""

    sandbox = durable["sandbox"]

    async def artifact_base(**_kwargs):
        return "Workspaces/_by_id/F"

    monkeypatch.setattr(bash_tool, "_workspace_bash_artifact_base", artifact_base)
    command = "python3 -c \"open('stray.txt','w').write('x')\""
    run_id = await _new_run(durable)
    try:
        sandbox.occupy("other-conversation")
        await _park(run_id, await _bash(durable, command, run_id=run_id, workspace_id="ws_1"))
        execution_id = (await _ticket(run_id)).request_payload["execution_id"]
        sandbox.free("other-conversation")
        await _until(lambda: _done(sandbox, execution_id))
        assert await _wake() == 1

        resumed = json.loads(await _bash(durable, command, run_id=run_id, attempt=2, workspace_id="ws_1"))
        assert resumed["error"] == "bash_output_outside_workspace", resumed
        assert not (durable["root"] / "stray.txt").exists()
    finally:
        sandbox.free("other-conversation")
        await _cleanup([run_id])


@pytest.mark.asyncio
async def test_cancelling_a_parked_run_withdraws_its_command(durable):
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus, SandboxReservationStatus
    from packages.core.services.runtime_run_service import cancel_runtime_run_resources

    world, sandbox = durable, durable["sandbox"]
    run_id = await _new_run(world)
    try:
        sandbox.occupy("other-conversation")
        parked = await _bash(world, COMMAND, run_id=run_id)
        assert isinstance(parked, RuntimeToolSuspension), parked
        await _park(run_id, parked)
        [queued] = sandbox.queued_execution_ids()

        async with db_module.async_session() as db:
            run = await db.get(RuntimeRun, run_id)
            run.status = RuntimeRunStatus.CANCELLED.value
            await db.commit()
            await db.refresh(run)
        await cancel_runtime_run_resources(run)
        assert sandbox.queued_execution_ids() == [], "the parked command left the queue"
        assert sandbox.executions[queued].status == "cancelled"
        await _wake()
        assert (await _ticket(run_id)).status == SandboxReservationStatus.CANCELLED.value

        sandbox.free("other-conversation")
        await asyncio.sleep(0.2)
        assert _user_launches(sandbox) == [], "a withdrawn command never runs"
        assert not (world["root"] / "Knowledge" / "parked.md").exists()
    finally:
        sandbox.free("other-conversation")
        await _cleanup([run_id])


@pytest.mark.asyncio
async def test_a_task_worker_waits_in_process_bounded_and_is_told_the_command_never_started(durable, monkeypatch):
    world, sandbox = durable, durable["sandbox"]
    monkeypatch.setattr(get_settings(), "SANDBOX_EXEC_QUEUE_WAIT_SECONDS", 2)
    sandbox.occupy("other-conversation")
    try:
        # No durable run to park (a task worker / workflow / system context).
        started = time.monotonic()
        result = json.loads(await _bash(world, COMMAND))
        elapsed = time.monotonic() - started
        assert result["error"] == "sandbox_queue_timeout", result
        assert result["command_executed"] is False
        assert "2s" in result["reason"], "the timeout reported is the budget it actually waited"
        assert 1.5 <= elapsed < 15, elapsed
        assert _user_launches(sandbox) == [] and sandbox.queued_execution_ids() == []
        assert not (world["root"] / "Knowledge" / "parked.md").exists()

        # A slot that frees within the budget: the in-process wait runs the command.
        monkeypatch.setattr(get_settings(), "SANDBOX_EXEC_QUEUE_WAIT_SECONDS", 20)

        async def free_soon():
            await asyncio.sleep(0.5)
            sandbox.free("other-conversation")

        freer = asyncio.create_task(free_soon())
        result = json.loads(await _bash(world, COMMAND))
        await freer
        assert result["exit_code"] == 0 and result["stdout"].strip() == "done", result
        assert (world["root"] / "Knowledge" / "parked.md").read_text() == "parked\n"
    finally:
        sandbox.free("other-conversation")


# ── A sandbox lost under a command ──────────────────────────────────────


def _two_sandboxes(world, monkeypatch, tmp_path):
    """The user's sandbox, and the new one admission hands out once it is lost."""

    from packages.core.ai.tools import sandbox_tools
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_queue_service import SandboxRouteNotFoundError
    from tests.bash_sandbox_fakes import LocalSandbox

    new = LocalSandbox(base=tmp_path / "sandbox-new", slots=1)
    new.scratch.mkdir(parents=True, exist_ok=True)
    boxes = {"sbx-old": world["sandbox"], "sbx-new": new}
    state = {"current": "sbx-old", "gone": set()}

    async def bound(entity_id, runtime_context):
        return state["current"], None

    async def client_for(sandbox_id, timeout=None):
        if sandbox_id in state["gone"]:
            raise SandboxRouteNotFoundError(f"no route to {sandbox_id}")
        return boxes[sandbox_id]

    monkeypatch.setattr(bash_tool, "_bash_sandbox", bound)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for)
    monkeypatch.setattr(sandbox_session_service, "sandbox_client_for", client_for)
    return boxes, state


async def _claim(run_id: str) -> None:
    """The woken run is claimed by a worker (then released: the test drives the call)."""

    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.services.runtime_run_service import claim_runtime_run_execution

    async with db_module.async_session() as db:
        claimed = await claim_runtime_run_execution(db, run_id=run_id, worker_id=f"test-{run_id}")
        assert claimed is not None, "the woken run is claimable"
        run = await db.get(RuntimeRun, run_id)
        run.lease_owner = None
        run.lease_expires_at = None
        await db.commit()


def _counting_saves(monkeypatch) -> list[str]:
    saves: list[str] = []
    real_save = bash_tool._save_changes

    async def counting_save(**kwargs):
        saves.append(kwargs["sandbox_id"])
        return await real_save(**kwargs)

    monkeypatch.setattr(bash_tool, "_save_changes", counting_save)
    return saves


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loss", "new_full"),
    [("route_gone", False), ("container_lost", False), ("container_lost", True)],
)
async def test_a_command_parked_on_a_lost_sandbox_runs_once_on_the_users_new_sandbox(
    durable, monkeypatch, tmp_path, loss, new_full,
):
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.models.runtime_run import SandboxReservationStatus

    world = durable
    boxes, state = _two_sandboxes(world, monkeypatch, tmp_path)
    old, new = boxes["sbx-old"], boxes["sbx-new"]
    saves = _counting_saves(monkeypatch)
    run_id = await _new_run(world)
    try:
        old.occupy("other-conversation")
        parked = await _bash(world, COMMAND, run_id=run_id)
        assert isinstance(parked, RuntimeToolSuspension), parked
        await _park(run_id, parked)

        # The container dies while the command waits for its slot: it never
        # started. Admission now hands out the user's new sandbox.
        if loss == "route_gone":
            state["gone"].add("sbx-old")
        else:
            old.lose_container()
        state["current"] = "sbx-new"
        if new_full:
            new.occupy("other-conversation")
        assert await _wake() == 1
        await _claim(run_id)

        resumed = await _bash(world, COMMAND, run_id=run_id, attempt=2)
        if new_full:
            # The new sandbox is full too: the run parks again, on the same
            # ticket, now naming the new sandbox's execution.
            assert isinstance(resumed, RuntimeToolSuspension), resumed
            await _park(run_id, resumed)
            ticket = await _ticket(run_id)
            assert ticket.status == SandboxReservationStatus.EXEC_WAITING.value
            assert ticket.request_payload["sandbox_id"] == "sbx-new"
            assert ticket.request_payload["execution_id"] == new.queued_execution_ids()[0]
            new.free("other-conversation")
            await _until(lambda: _done(new, ticket.request_payload["execution_id"]))
            assert await _wake() == 1
            await _claim(run_id)
            resumed = await _bash(world, COMMAND, run_id=run_id, attempt=3)
        assert isinstance(resumed, str), resumed
        result = json.loads(resumed)
        assert result["exit_code"] == 0 and result["stdout"].strip() == "done", result
        assert (world["root"] / "Knowledge" / "parked.md").read_text() == "parked\n"
        assert _user_launches(old) == [], "it never ran on the lost container"
        assert len(_user_launches(new)) == 1, "it ran exactly once, on the user's new sandbox"
        assert _stagings(new) >= 1, "staged afresh into the new sandbox's work area"
        assert saves == ["sbx-new"], "saved back exactly once, from the new sandbox"
        assert (await _ticket(run_id)).status == SandboxReservationStatus.CONSUMED.value
    finally:
        old.free("other-conversation")
        new.free("other-conversation")
        await _cleanup([run_id])


@pytest.mark.asyncio
async def test_a_command_running_when_its_sandbox_is_lost_is_reported_never_rerun(durable, monkeypatch, tmp_path):
    world = durable
    boxes, state = _two_sandboxes(world, monkeypatch, tmp_path)
    old, new = boxes["sbx-old"], boxes["sbx-new"]
    saves = _counting_saves(monkeypatch)
    run_id = await _new_run(world)
    try:
        call = asyncio.create_task(_bash(world, "sleep 1.5; " + COMMAND, run_id=run_id))

        async def running() -> bool:
            return any(
                record.status == "running" and not record.command.startswith("python3 -c")
                for record in old.executions.values()
            )

        await _until(running)
        state["current"] = "sbx-new"
        old.lose_container()
        result = json.loads(await call)
        assert result["error"] == "sandbox_lost", result
        assert result["command_executed"] is True
        assert _user_launches(new) == [], "a command that ran is never run again"
        assert saves == [], "nothing is saved back from a lost container"
        assert not (world["root"] / "Knowledge" / "parked.md").exists()
        await asyncio.sleep(1.6)  # let the orphaned local process finish
    finally:
        await _cleanup([run_id])
