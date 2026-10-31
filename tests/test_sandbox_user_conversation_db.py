"""One sandbox per user (across conversations), against the real reservation queue.

Under durable external-runner coordination every sandbox comes from a
reservation ticket. These tests drive the real queue service on Postgres:

* parallel delegates of one conversation (no durable run of their own, no
  task) coalesce onto ONE ticket and ONE container, and every one of them
  resumes on it;
* a delegate that asks while its parent's run is parked on a ticket joins
  that ticket instead of allocating a second container;
* another member of the same workspace conversation gets their own ticket
  and container and cannot reach this one by id, while the same user in
  another conversation shares the user's one container (in its own work area);
* user-less system work is owned by its workspace and scoped to its task;
* cancelling a run never destroys the container while another run of the
  same user -- in any conversation -- is live.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_sandbox_session_service import (  # noqa: F401 - fixtures
    ScriptedSandboxClient,
    _bundle,
    _load_result,
    fake_cache,
    scripted_client,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def ids():
    suffix = uuid.uuid4().hex.upper()[:18]
    return {
        "suffix": suffix,
        "entity": f"E{suffix}"[:26],
        "user": f"U1{suffix}"[:26],
        "other_user": f"U2{suffix}"[:26],
        "conversation": f"01C{suffix}AAAAA"[:26],
        "other_conversation": f"01D{suffix}AAAAA"[:26],
        "workspace": f"W{suffix}"[:26],
    }


@pytest.fixture
async def queue_env(db_session, fake_cache, scripted_client, monkeypatch, ids):
    """Durable external-runner admission on the test database, one runner."""

    import packages.core.database as database_module
    from packages.core.config import get_settings
    from packages.core.models.runtime_run import (
        RuntimeRun,
        SandboxInstance,
        SandboxReservation,
        SandboxRunner,
        SandboxRunnerStatus,
    )
    from packages.core.services import sandbox_session_service

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database_module, "async_session", factory)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_POLL_SECONDS", 1)
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_MAX_WAIT_SECONDS", 30)
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_MAX_PENDING", 100000)

    async def exec_injection(_base_url):
        return "exec"

    monkeypatch.setattr(sandbox_session_service, "_runner_env_injection", exec_injection)
    runner = SandboxRunner(
        id=f"runner-{ids['suffix']}"[:80],
        base_url=f"http://runner-{ids['suffix'].lower()}:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=50,
        executing_limit=50,
    )
    db_session.add(runner)
    await db_session.commit()
    yield {"factory": factory, "runner": runner}

    async with factory() as db:
        entities = [ids["entity"]]
        reservation_ids = (
            await db.execute(select(SandboxReservation.id).where(SandboxReservation.entity_id.in_(entities)))
        ).scalars().all()
        await db.execute(delete(SandboxInstance).where(SandboxInstance.reservation_id.in_(reservation_ids)))
        await db.execute(delete(SandboxReservation).where(SandboxReservation.entity_id.in_(entities)))
        await db.execute(delete(RuntimeRun).where(RuntimeRun.entity_id.in_(entities)))
        await db.execute(delete(SandboxRunner).where(SandboxRunner.id == runner.id))
        await db.commit()


async def _scope_tickets(factory, scope_key: str):
    from packages.core.models.runtime_run import SandboxReservation

    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(SandboxReservation)
                    .where(SandboxReservation.request_payload["sandbox_scope"].astext == scope_key)
                    .order_by(SandboxReservation.enqueued_at)
                )
            ).scalars().all()
        )


async def _allocate_waiting(factory, runner, scope_key: str, sandbox_ids: dict) -> None:
    """The scheduler for this test's tickets only (the test DB is shared)."""

    from packages.core.models.runtime_run import (
        SandboxReservation,
        SandboxReservationStatus,
        SandboxRunner,
    )
    from packages.core.services.sandbox_queue_service import complete_sandbox_allocation

    async with factory() as db:
        tickets = (
            await db.execute(
                select(SandboxReservation)
                .where(
                    SandboxReservation.request_payload["sandbox_scope"].astext == scope_key,
                    SandboxReservation.status.in_(
                        [SandboxReservationStatus.QUEUED.value, SandboxReservationStatus.REQUEUED.value]
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for ticket in tickets:
            row = await db.get(SandboxRunner, runner.id, with_for_update=True)
            ticket.status = SandboxReservationStatus.ALLOCATING.value
            ticket.runner_id = runner.id
            ticket.attempt_count += 1
            ticket.version += 1
            row.active_count += 1
            await db.flush()
            sandbox_id = f"sbx-{len(sandbox_ids)}-{ticket.id[-8:]}"
            sandbox_ids[ticket.id] = sandbox_id
            await complete_sandbox_allocation(
                db,
                reservation_id=ticket.id,
                sandbox_id=sandbox_id,
                runner_id=runner.id,
                expected_reservation_version=ticket.version,
            )
        await db.commit()


def _scheduling_sleep(queue_env, scope_key: str, sandbox_ids: dict):
    async def sleep(_seconds: float) -> None:
        await asyncio.sleep(0.01)
        await _allocate_waiting(queue_env["factory"], queue_env["runner"], scope_key, sandbox_ids)

    return sleep


def _owner(ids, *, user: str | None = None, workspace: str = ""):
    from packages.core.services.sandbox_session_service import SandboxOwner

    return SandboxOwner(entity_id=ids["entity"], user_id=ids["user"] if user is None else user, workspace_id=workspace)


def _binding(conversation: str, agent: str, **extra):
    from packages.core.services.sandbox_session_service import SandboxBinding

    return SandboxBinding(conversation_id=conversation, agent_id=agent, skill_id="skill_docx", **extra)


# ── single flight: parallel delegates ────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_delegates_coalesce_onto_one_ticket_and_one_container(queue_env, ids, scripted_client):
    from packages.core.models.runtime_run import SandboxReservationStatus
    from packages.core.services.sandbox_session_service import SandboxSession, acquire_sandbox_session

    agents = [f"agent-{index}" for index in range(8)]
    scope_key = f"sandbox:owner:{ids['entity']}:{ids['user']}:user"
    sandbox_ids: dict = {}
    scripted_client.load_results = [_load_result("unused")] * len(agents)

    sessions = await asyncio.gather(
        *(
            acquire_sandbox_session(
                owner=_owner(ids),
                bundle=_bundle("docx"),
                binding=_binding(ids["conversation"], agent),
                sleep=_scheduling_sleep(queue_env, scope_key, sandbox_ids),
            )
            for agent in agents
        )
    )

    assert all(isinstance(session, SandboxSession) for session in sessions), sessions
    tickets = await _scope_tickets(queue_env["factory"], scope_key)
    # ONE ticket, ONE container, every delegate resumed on it.
    assert len(tickets) == 1, [ticket.status for ticket in tickets]
    assert len(sandbox_ids) == 1
    [container] = sandbox_ids.values()
    assert {session.sandbox_id for session in sessions} == {container}
    assert tickets[0].status == SandboxReservationStatus.CONSUMED.value
    assert tickets[0].requester_id == f"conversation:{ids['conversation']}"

    from packages.core.ai.runtime.sandbox import RuntimeSandboxScope, runtime_load_sandbox_binding

    record = await runtime_load_sandbox_binding(
        RuntimeSandboxScope(ids["entity"], ids["user"], ids["conversation"]), container
    )
    assert sorted(record["agents"]) == agents


@pytest.mark.asyncio
async def test_a_delegate_joins_its_parents_parked_run_ticket(queue_env, ids, scripted_client, db_session):
    """The parent run parks on its ticket; a delegate of the same conversation
    joins it (no second container), attaches without consuming it, and the
    parent attaches the same container when it resumes."""

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.models.runtime_run import (
        RuntimeRun,
        RuntimeRunStatus,
        SandboxReservation,
        SandboxReservationStatus,
    )
    from packages.core.services.sandbox_session_service import (
        SandboxSession,
        acquire_sandbox_session,
        attach_durable_reservation,
    )

    run_id = f"01R{ids['suffix']}AAAAA"[:26]
    run = RuntimeRun(
        id=run_id, root_run_id=run_id, conversation_id=ids["conversation"],
        entity_id=ids["entity"], user_id=ids["user"], status=RuntimeRunStatus.RUNNING.value,
    )
    db_session.add(run)
    await db_session.commit()
    scope_key = f"sandbox:owner:{ids['entity']}:{ids['user']}:user"
    parent_binding = _binding(
        ids["conversation"], "agent-parent", runtime_run_id=run_id, runtime_tool_call_id="call-1",
    )

    async with queue_env["factory"]() as db:
        parked = await acquire_sandbox_session(
            owner=_owner(ids), bundle=_bundle("docx"), binding=parent_binding, db=db,
        )
    assert isinstance(parked, RuntimeToolSuspension)
    async with queue_env["factory"]() as db:
        ticket = await db.get(SandboxReservation, parked.reservation_id)
        ticket.status = SandboxReservationStatus.QUEUED.value  # the persisted checkpoint activates it
        await db.commit()

    sandbox_ids: dict = {}
    scripted_client.load_results = [_load_result("unused")] * 2
    delegate = await acquire_sandbox_session(
        owner=_owner(ids),
        bundle=_bundle("docx"),
        binding=_binding(ids["conversation"], "agent-delegate"),
        sleep=_scheduling_sleep(queue_env, scope_key, sandbox_ids),
    )
    assert isinstance(delegate, SandboxSession)
    tickets = await _scope_tickets(queue_env["factory"], scope_key)
    assert [ticket.id for ticket in tickets] == [parked.reservation_id]
    # The run's ticket is the run's to consume when it resumes.
    assert tickets[0].status == SandboxReservationStatus.ALLOCATED.value

    async with queue_env["factory"]() as db:
        resumed = await attach_durable_reservation(
            owner=_owner(ids), bundle=_bundle("docx"), binding=parent_binding, db=db,
        )
    assert isinstance(resumed, SandboxSession)
    assert resumed.sandbox_id == delegate.sandbox_id
    [consumed] = await _scope_tickets(queue_env["factory"], scope_key)
    assert consumed.status == SandboxReservationStatus.CONSUMED.value


# ── what never shares ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_other_member_gets_their_own_ticket_the_same_users_other_conversation_shares(
    queue_env, ids, scripted_client, monkeypatch,
):
    from packages.core.ai.tools import sandbox_tools
    from packages.core.services.sandbox_session_service import SandboxSession, acquire_sandbox_session

    sandbox_ids: dict = {}

    async def acquire(user: str, conversation: str):
        scope_key = f"sandbox:owner:{ids['entity']}:{user}:user"
        return scope_key, await acquire_sandbox_session(
            owner=_owner(ids, user=user),
            bundle=_bundle("docx"),
            binding=_binding(conversation, "agent-1"),
            sleep=_scheduling_sleep(queue_env, scope_key, sandbox_ids),
        )

    # The same user's other conversation reuses (reloads the Skill into) the user's container.
    scripted_client.load_results = [_load_result("unused")]
    mine_key, mine = await acquire(ids["user"], ids["conversation"])
    member_key, member = await acquire(ids["other_user"], ids["conversation"])
    elsewhere_key, elsewhere = await acquire(ids["user"], ids["other_conversation"])

    assert all(isinstance(session, SandboxSession) for session in (mine, member, elsewhere))
    assert mine.sandbox_id == elsewhere.sandbox_id != member.sandbox_id
    assert mine_key == elsewhere_key
    for key in (mine_key, member_key):
        assert len(await _scope_tickets(queue_env["factory"], key)) == 1

    def forbid(*_args, **_kwargs):
        raise AssertionError("a foreign sandbox must not reach a runner")

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", forbid)
    for user, conversation in ((ids["other_user"], ids["conversation"]), ("", ids["conversation"])):
        denied = await sandbox_tools._sandbox_exec(
            entity_id=ids["entity"], user_id=user, conversation_id=conversation,
            sandbox_id=mine.sandbox_id, command="cat /workspace/secret", _agent_id_from_context="agent-1",
        )
        assert json.loads(denied)["error"]["code"] == "sandbox_access_denied"


@pytest.mark.asyncio
async def test_userless_work_gets_a_workspace_owned_sandbox_per_task(queue_env, ids, scripted_client):
    from packages.core.ai.runtime.sandbox import runtime_task_binding_key
    from packages.core.services.sandbox_session_service import SandboxSession, acquire_sandbox_session

    sandbox_ids: dict = {}
    sessions = {}
    for task in ("01T" + ids["suffix"] + "AAAAA", "01T" + ids["suffix"] + "BBBBB"):
        key = runtime_task_binding_key(task[:26])
        scope_key = f"sandbox:owner:{ids['entity']}:system:conversation:{key}"
        sessions[task] = (
            scope_key,
            await acquire_sandbox_session(
                owner=_owner(ids, user="", workspace=ids["workspace"]),
                bundle=_bundle("docx"),
                binding=_binding(key, "agent-strategist", task_id=task[:26]),
                sleep=_scheduling_sleep(queue_env, scope_key, sandbox_ids),
            ),
        )
    (key_a, a), (key_b, b) = sessions.values()
    assert isinstance(a, SandboxSession) and isinstance(b, SandboxSession)
    assert a.sandbox_id != b.sandbox_id
    [ticket_a] = await _scope_tickets(queue_env["factory"], key_a)
    assert ticket_a.user_id == "" and ticket_a.requester_kind == "task"


# ── cancellation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_run_never_destroys_the_container_while_the_user_has_a_live_run(
    queue_env, ids, db_session, monkeypatch,
):
    from packages.core.models.runtime_run import (
        RuntimeRun,
        RuntimeRunStatus,
        SandboxInstance,
        SandboxReservation,
        SandboxReservationStatus,
    )
    from packages.core.services import runtime_run_service
    from packages.core.services import sandbox_session_service

    cancelled_id = f"01X{ids['suffix']}AAAAA"[:26]
    live_id = f"01Y{ids['suffix']}AAAAA"[:26]
    now = datetime.now(timezone.utc)
    cancelled = RuntimeRun(
        id=cancelled_id, root_run_id=cancelled_id, conversation_id=ids["conversation"],
        entity_id=ids["entity"], user_id=ids["user"], status=RuntimeRunStatus.CANCELLED.value,
        active_sandbox_id="sbx-shared", active_execution_id="exec-cancelled",
    )
    # The live run is in ANOTHER conversation of the same user: the
    # container is the user's, so it still keeps it.
    live = RuntimeRun(
        id=live_id, root_run_id=live_id, conversation_id=ids["other_conversation"],
        entity_id=ids["entity"], user_id=ids["user"], status=RuntimeRunStatus.RUNNING.value,
    )
    ticket = SandboxReservation(
        runtime_run_id=cancelled_id, root_run_id=cancelled_id, tool_call_id="call-x",
        entity_id=ids["entity"], user_id=ids["user"], status=SandboxReservationStatus.CONSUMED.value,
        deadline_at=now, request_payload={}, runner_id=queue_env["runner"].id, sandbox_id="sbx-shared",
    )
    db_session.add_all([cancelled, live, ticket])
    await db_session.flush()
    db_session.add(SandboxInstance(
        sandbox_id="sbx-shared", reservation_id=ticket.id, runtime_run_id=cancelled_id,
        root_run_id=cancelled_id, runner_id=queue_env["runner"].id, status="active",
    ))
    await db_session.commit()

    calls: list = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def cancel_execution(self, sandbox_id, execution_id):
            calls.append(("cancel", sandbox_id, execution_id))

        async def destroy(self, sandbox_id):
            calls.append(("destroy", sandbox_id))

    async def not_warm(_sandbox_id):
        return False

    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)
    monkeypatch.setattr(sandbox_session_service, "is_sandbox_kept_warm", not_warm)

    await runtime_run_service.cancel_runtime_run_resources(cancelled)
    # Only the cancelled run's own command was stopped; the user's container
    # stays for their live run in the other conversation.
    assert calls == [("cancel", "sbx-shared", "exec-cancelled")]

    async with queue_env["factory"]() as db:
        await db.execute(
            RuntimeRun.__table__.update().where(RuntimeRun.id == live_id).values(status=RuntimeRunStatus.COMPLETED.value)
        )
        await db.commit()
    calls.clear()
    await runtime_run_service.cancel_runtime_run_resources(cancelled)
    # The user is idle and nothing keeps it warm: now it is released.
    assert ("destroy", "sbx-shared") in calls
