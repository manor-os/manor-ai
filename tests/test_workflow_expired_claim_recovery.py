"""Crash recovery resumes replay-safe runs and fails only unknown effects."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from packages.core.models.execution_claim import RuntimeExecutionClaim
from packages.core.models.workflow import WorkflowRun
from packages.core.services import workflow_run_execution_claim as claims
from packages.core.services.workflow_claim_recovery import (
    MAX_WORKFLOW_EXECUTION_RECOVERIES,
    decide_workflow_claim_loss,
    workflow_claim_handoff_digest,
)
from packages.core.tasks import ai_tasks
from test_workflows import _auth


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "renewed", "replaced", "deleted"])
async def test_expired_token_takeover_is_atomic(client, db_session, change):
    now = datetime.now(timezone.utc)
    key = "workflow-run:interrupted"
    old_token, new_token = "0" * 26, "1" * 26
    row = RuntimeExecutionClaim(
        claim_key=key, claim_token=old_token,
        expires_at=now - timedelta(seconds=1),
    )
    db_session.add(row)
    await db_session.commit()
    if change == "renewed":
        row.expires_at = now + timedelta(minutes=10)
    elif change == "replaced":
        row.claim_token = "2" * 26
    elif change == "deleted":
        await db_session.delete(row)
    await db_session.commit()

    acquired = await claims._acquire_postgres_execution_claim(
        key, new_token, lease_ttl_seconds=60, expired_claim_token=old_token,
    )
    assert acquired is (change == "unchanged")
    if change != "deleted":
        await db_session.refresh(row)
    if change == "unchanged":
        assert row.claim_token == new_token
        assert row.expires_at > now
    elif change == "replaced":
        assert row.claim_token == "2" * 26
    elif change == "renewed":
        assert row.claim_token == old_token


@pytest.mark.asyncio
async def test_handoff_lease_is_adopted_by_exactly_one_successor(client, db_session):
    key = "workflow-run:handoff"
    retained, first, second = "0" * 26, "1" * 26, "2" * 26
    db_session.add(RuntimeExecutionClaim(
        claim_key=key, claim_token=retained,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    await db_session.commit()

    assert await claims._acquire_postgres_execution_claim(
        key, first, lease_ttl_seconds=60, handoff_claim_token=retained,
    ) is True
    # A duplicate delivery of the same handoff finds the lease live and owned.
    assert await claims._acquire_postgres_execution_claim(
        key, second, lease_ttl_seconds=60, handoff_claim_token=retained,
    ) is False


@pytest.mark.asyncio
async def test_lease_renewal_does_not_queue_behind_business_admission(
    client, db_session, monkeypatch,
):
    from packages.core import database as db_module
    from packages.core.config import get_settings
    from packages.core.database_runtime import DatabaseEngineKind, _loop_admission

    key, token = "workflow-run:admission", "0" * 26
    db_session.add(RuntimeExecutionClaim(
        claim_key=key, claim_token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    await db_session.commit()
    # The production worker engine, with its admission runtime installed.
    engine = db_module.create_database_engine(get_settings(), kind=DatabaseEngineKind.WORKER)
    factory = db_module.create_session_factory(engine)
    monkeypatch.setattr(db_module, "create_worker_session", lambda: factory)
    runtime = engine.sync_engine._manor_database_runtime
    assert runtime.admission_enabled
    gate = _loop_admission(runtime.policy)
    for _ in range(runtime.policy.limit):
        await gate.acquire()
    try:
        # The business work this lease fences holds every permit of the loop.
        assert await asyncio.wait_for(
            claims._extend_postgres_execution_claim(key, token, lease_ttl_seconds=60),
            timeout=runtime.policy.timeout + 5,
        ) is True
    finally:
        for _ in range(runtime.policy.limit):
            gate.release()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_selects_active_runs_with_expired_ownership(client, db_session, monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session

    now = datetime.now(timezone.utc)
    cases = [
        ("expired", "running", -1),
        ("live", "running", 600),
        ("pending", "pending", -1),
        ("paused", "paused", -1),
        ("approval", "waiting_approval", -1),
        ("done", "completed", -1),
        ("legacy", "running", None),
        # No lease at all: only a node left running long enough ago counts.
        ("orphaned", "running", "orphan"),
        ("settling", "running", "recent"),
        ("orphan_done", "completed", "orphan"),
        ("stepping", "running", "orphan"),
    ]
    running_node = [{"node_id": "work", "status": "completed"}, {"node_id": "work", "status": "running"}]
    for run_id, status, ttl in cases:
        orphan = isinstance(ttl, str)
        db_session.add(WorkflowRun(
            id=run_id, workflow_id="workflow", entity_id="entity", status=status,
            **({"execution_trace": running_node} if orphan else {}),
            **({"updated_at": now - timedelta(hours=1)} if ttl == "orphan" else {}),
            # A person steps this run: it waits for their next step.
            **({"trigger_data": {"_execution_driver": "manual_step"}} if run_id == "stepping" else {}),
        ))
        if orphan:
            continue
        if ttl is not None:
            db_session.add(RuntimeExecutionClaim(
                claim_key=f"workflow-run:{run_id}", claim_token="0" * 26,
                expires_at=now + timedelta(seconds=ttl),
            ))
    await db_session.commit()
    seen = []

    async def recover(run_id, **kwargs):
        seen.append((run_id, kwargs.get("expired_claim_token")))
        return {"expired": "running", "pending": "failed", "orphaned": "failed"}[run_id]

    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)
    monkeypatch.setattr(WorkflowRunner, "_recover_lost_execution_claim", recover)
    assert await ai_tasks._recover_expired_workflow_claims() == {
        "checked": 3, "settled": 2, "resumed": 1,
    }
    assert seen == [("expired", "0" * 26), ("pending", "0" * 26), ("orphaned", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_settles", [True, False])
async def test_a_node_that_breaks_the_run_session_never_leaves_it_running(
    client, db_session, monkeypatch, owner_settles,
):
    from sqlalchemy import select

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session
    from packages.core.models.workflow import WorkflowProject
    from packages.core.services import workflow_chat_projection, workflow_project_service
    from test_workflow_projects import _keyed_project_runner

    async def unguarded_create(db, **values):
        # Any node whose flush fails and which reports the failure itself.
        db.add(WorkflowProject(revision=0, **values))
        await db.flush()

    run = await _keyed_project_runner(client, db_session, f"broken_session_{owner_settles}", {
        "operation": "create", "project_type": "article", "project_key": "{{topic}}", "state": {},
    }, ["topic"])
    assert (await run(topic="same")).status == "completed"
    monkeypatch.setattr(workflow_project_service, "create_workflow_project", unguarded_create)
    queued: list[dict] = []
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(
        lambda run_id, *_a, **kwargs: queued.append({"run_id": run_id, **kwargs}) or True
    ))
    if not owner_settles:
        async def owner_cannot_settle(db, *, run):
            raise RuntimeError("settling failed")

        monkeypatch.setattr(workflow_chat_projection, "project_workflow_run_status", owner_cannot_settle)
    broken = await run(topic="same")

    if not owner_settles:
        # The owner crashed while settling. Its lease was released cleanly,
        # so no expiry would ever surface the run: recovery hands it to the
        # next worker at once instead of leaving it ownerless.
        assert broken.status == "running"
        handoffs = [item for item in queued if item.get("claim_handoff_token")]
        assert [item["run_id"] for item in handoffs] == [broken.id]
        monkeypatch.undo()
        await WorkflowRunner(session_factory=async_session).run(
            broken.id, claim_handoff_token=handoffs[0]["claim_handoff_token"],
        )
        broken = await db_session.get(WorkflowRun, broken.id, populate_existing=True)
    assert broken.status == "failed"
    assert broken.step_results["project"]["status"] == "failed"
    assert "uq_workflow_projects_business_key" in broken.step_results["project"]["error_details"]
    assert broken.error and "uq_workflow_projects_business_key" not in broken.error
    assert await db_session.scalar(select(RuntimeExecutionClaim).where(
        RuntimeExecutionClaim.claim_key == f"workflow-run:{broken.id}")) is None


@pytest.mark.asyncio
async def test_sweep_republishes_open_runs_no_worker_ever_claimed(client, db_session, monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.constants.workflow import WORKFLOW_DISPATCH_RECOVERY_TRIGGER_FIELD as FIELD
    from packages.core.database import async_session

    now = datetime.now(timezone.utc)
    minutes = lambda value: now - timedelta(minutes=value)  # noqa: E731
    cases = [
        # (id, status, started minutes ago, claimed, extra columns)
        ("lost", "running", 10, False, {}),
        ("lost-pending", "pending", 10, False, {}),
        ("claimed", "running", 10, True, {}),
        ("fresh", "running", 1, False, {}),
        ("paused", "paused", 10, False, {}),
        ("scheduled", "running", 10, False, {"trigger_source": "schedule"}),
        ("continuing", "running", 10, False, {"continuation_token": "0" * 26}),
        ("ancient", "running", 2 * 24 * 60, False, {}),
        # Republished once 7 minutes ago: the second wait is 10 minutes.
        ("backing-off", "running", 30, False, {
            "updated_at": minutes(7), "trigger_data": {FIELD: {"attempts": 1}},
        }),
    ]
    for run_id, status, age, claimed, extra in cases:
        db_session.add(WorkflowRun(
            id=run_id, workflow_id="workflow", entity_id="entity", status=status,
            started_at=minutes(age), **extra,
        ))
        if claimed:
            db_session.add(RuntimeExecutionClaim(
                claim_key=f"workflow-run:{run_id}", claim_token="0" * 26,
                expires_at=now + timedelta(minutes=10),
            ))
    await db_session.commit()
    published = []
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(lambda run_id, *a, **k: published.append(run_id) or True))

    assert await ai_tasks._redispatch_unclaimed_workflow_runs() == {
        "checked": 3, "published": 2, "deferred": 1,
    }
    assert sorted(published) == ["lost", "lost-pending"]
    lost = await db_session.get(WorkflowRun, "lost", populate_existing=True)
    assert lost.trigger_data[FIELD]["attempts"] == 1
    # Republishing counts as activity: the next sweep waits instead of repeating it.
    published.clear()
    assert (await ai_tasks._redispatch_unclaimed_workflow_runs())["published"] == 0
    assert published == []


async def _crash_owner_inside_step(
    monkeypatch, db_session, run_id: str, step_id: str, *, settled_result: dict | None = None,
    tool_intents: list[tuple[str, object]] | None = None,
):
    """Leave the durable state a killed owner leaves, with its live lease row.

    Without ``settled_result`` the owner dies while the node runs. With it,
    the node returns that result and the owner dies after committing the
    node's receipt but before committing the run's outcome. ``tool_intents``
    are the tool calls the node starts (through the Runtime's pre-I/O hook)
    before it dies.
    """
    from sqlalchemy import update

    from packages.core.ai.runtime.tool_effect_intents import runtime_record_tool_effect_intent
    from packages.core.ai.workflow_runner import WorkflowRunner

    started = asyncio.Event()
    original_execute = WorkflowRunner._execute_step_safe
    original_inner = WorkflowRunner._execute_step
    original_boundary = WorkflowRunner._externally_paused_or_cancelled
    original_release = claims._release_postgres_execution_claim

    async def block_in_step(self, step, run, db, **kwargs):
        if step["id"] == step_id:
            if settled_result is not None:
                return dict(settled_result)
            if tool_intents is not None:
                return await original_execute(self, step, run, db, **kwargs)
            started.set()
            await asyncio.Event().wait()
        return await original_execute(self, step, run, db, **kwargs)

    async def die_inside_node(self, step, run, db, **kwargs):
        if step["id"] != step_id:
            return await original_inner(self, step, run, db, **kwargs)
        for tool_name, classification in tool_intents or ():
            await runtime_record_tool_effect_intent(tool_name, {}, None, classification)
        started.set()
        await asyncio.Event().wait()

    async def die_after_receipt(run, db, **kwargs):
        if step_id in (run.step_results or {}):
            started.set()
            await asyncio.Event().wait()
        return await original_boundary(run, db, **kwargs)

    async def process_died(*_args):
        return True

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", block_in_step)
    monkeypatch.setattr(WorkflowRunner, "_execute_step", die_inside_node)
    monkeypatch.setattr(
        WorkflowRunner, "_externally_paused_or_cancelled", staticmethod(die_after_receipt),
    )
    monkeypatch.setattr(claims, "_release_postgres_execution_claim", process_died)
    owner = asyncio.create_task(WorkflowRunner().run(run_id))
    await asyncio.wait_for(started.wait(), timeout=10)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", original_execute)
    monkeypatch.setattr(WorkflowRunner, "_execute_step", original_inner)
    monkeypatch.setattr(
        WorkflowRunner, "_externally_paused_or_cancelled", staticmethod(original_boundary),
    )
    monkeypatch.setattr(claims, "_release_postgres_execution_claim", original_release)
    await db_session.execute(
        update(RuntimeExecutionClaim)
        .where(RuntimeExecutionClaim.claim_key == f"workflow-run:{run_id}")
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()


async def _start_unexecuted_run(client, user: str, steps: list[dict]) -> tuple[dict, str]:
    headers = await _auth(client, user)
    workflow = await client.post(
        "/api/v1/workflows", headers=headers, json={"name": user, "steps": steps},
    )
    assert workflow.status_code == 201, workflow.text
    started = await client.post(
        f"/api/v1/workflows/{workflow.json()['id']}/run",
        headers=headers, json={"execute": False},
    )
    assert started.status_code == 201, started.text
    return headers, started.json()["id"]


def _chain(*steps: dict) -> list[dict]:
    nodes = [{"id": "trigger", "type": "trigger", "config": {}}, *steps,
             {"id": "end", "type": "end", "config": {}}]
    for node, following in zip(nodes, nodes[1:]):
        node["next"] = [following["id"]]
    nodes[-1]["next"] = []
    return nodes


@pytest.mark.asyncio
async def test_sweep_resumes_the_interrupted_step_in_a_worker(client, db_session, monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session

    headers, run_id = await _start_unexecuted_run(client, "wfclaimresume", _chain(
        {"id": "prepare", "type": "transform", "config": {"set": {"prepared": "yes"}}},
        {"id": "work", "type": "transform", "config": {"set": {"worked": "yes"}}},
    ))
    await _crash_owner_inside_step(monkeypatch, db_session, run_id, "work")
    queued: list[dict] = []
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(
        lambda run_id, *_a, **kwargs: queued.append({"run_id": run_id, **kwargs}) or True
    ))
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)

    assert await ai_tasks._recover_expired_workflow_claims() == {
        "checked": 1, "settled": 0, "resumed": 1,
    }
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "running" and run["error"] is None
    assert run["step_results"]["prepare"]["status"] == "completed"
    assert "work" not in run["step_results"]
    assert run["trigger_data"]["_execution_recovery"]["attempts"] == 1
    assert run["trigger_data"]["_execution_recovery"]["interrupted_step_ids"] == ["work"]
    assert [item["run_id"] for item in queued] == [run_id]

    # The broker lost that delivery: its retained lease expires unadopted and
    # the sweep hands the same interruption off again without counting it.
    await db_session.execute(
        RuntimeExecutionClaim.__table__.update()
        .where(RuntimeExecutionClaim.claim_key == f"workflow-run:{run_id}")
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()
    assert (await ai_tasks._recover_expired_workflow_claims())["resumed"] == 1
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["trigger_data"]["_execution_recovery"]["attempts"] == 1
    assert len(queued) == 2 and queued[1]["claim_handoff_token"] != queued[0]["claim_handoff_token"]

    executed: list[str] = []
    original_execute = WorkflowRunner._execute_step_safe

    async def record(self, step, run, db, **kwargs):
        executed.append(step["id"])
        return await original_execute(self, step, run, db, **kwargs)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", record)
    assert await WorkflowRunner(session_factory=async_session).run(
        run_id, claim_handoff_token=queued[1]["claim_handoff_token"],
    ) == "executed"
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "completed", run
    assert executed[0] == "work", "resumes at the interrupted step, not the entry"
    assert "prepare" not in executed
    assert run["variables"]["work"]["worked"] == "yes"
    assert await ai_tasks._recover_expired_workflow_claims() == {
        "checked": 0, "settled": 0, "resumed": 0,
    }


@pytest.mark.asyncio
async def test_interrupted_step_with_unknown_effects_fails_explicitly(client, db_session, monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session

    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
        RuntimeToolEffect,
    )

    headers, run_id = await _start_unexecuted_run(client, "wfclaimunknown", _chain(
        {"id": "evidence", "type": "transform", "config": {"set": {"evidence": "accepted"}}},
        {"id": "produce", "type": "agent", "name": "Produce",
         "config": {"prompt": "Render and publish the video."}},
    ))
    # The node started a call that may change the world, then its worker died.
    await _crash_owner_inside_step(monkeypatch, db_session, run_id, "produce", tool_intents=[
        ("publish_video", RuntimeToolClassification(RuntimeToolEffect.UNKNOWN, reason="unclassified")),
    ])
    stored = await db_session.get(WorkflowRun, run_id)
    await db_session.refresh(stored)
    assert stored.trigger_data["_step_effect_journal"]["produce"]["effect_intent"] is True
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(
        lambda *_a, **_k: pytest.fail("an uncertain step must not be replayed")
    ))
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)

    async def replay_forbidden(*args, **kwargs):
        pytest.fail("a successor replayed a step with unknown effects")

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", replay_forbidden)
    # The original delivery is redelivered (worker lost) and wins the race
    # against the sweep: it must route through recovery, not re-run the node.
    assert await WorkflowRunner(session_factory=async_session).run(run_id) == "executed"
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "failed"
    # People read that the step was interrupted and can be retried; the stored error keeps the cause.
    assert run["error"].startswith('"Produce" stopped before it finished')
    assert run["step_results"]["evidence"]["status"] == "completed"
    assert "produce" not in run["step_results"]
    stored = await db_session.get(WorkflowRun, run_id)
    await db_session.refresh(stored)
    assert "'Produce' (agent)" in stored.error and "not re-run automatically" in stored.error
    assert stored.terminal_effects_completed_at is not None

    async def forbidden(*args, **kwargs):
        pytest.fail("late delivery replayed an interrupted step")

    monkeypatch.setattr(WorkflowRunner, "_run_loop", forbidden)
    await WorkflowRunner(session_factory=async_session).run(run_id)
    assert await ai_tasks._recover_expired_workflow_claims() == {
        "checked": 0, "settled": 0, "resumed": 0,
    }


@pytest.mark.asyncio
async def test_interrupted_agent_step_that_changed_nothing_is_rerun(client, db_session, monkeypatch):
    """2026-10-21: an OOM-killed planning step failed the run although it had
    only read its inputs; its effect-intent journal now proves it is replayable."""
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session

    headers, run_id = await _start_unexecuted_run(client, "wfclaimeffectfree", _chain(
        {"id": "collect", "type": "transform", "config": {"set": {"topic": "weekly sales"}}},
        {"id": "draft", "type": "agent", "name": "Draft report",
         "config": {"prompt": "Draft the weekly sales report."}},
    ))
    # Reads (and their pre-I/O reports) do not count as effects.
    await _crash_owner_inside_step(monkeypatch, db_session, run_id, "draft", tool_intents=[
        ("read_file", SimpleNamespace(effect=RuntimeToolEffect.READ_ONLY)),
    ])
    queued: list[dict] = []
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(
        lambda run_id, *_a, **kwargs: queued.append({"run_id": run_id, **kwargs}) or True
    ))
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)

    assert await ai_tasks._recover_expired_workflow_claims() == {
        "checked": 1, "settled": 0, "resumed": 1,
    }
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "running" and run["error"] is None
    assert run["trigger_data"]["_execution_recovery"]["effect_free_step_ids"] == ["draft"]

    executed: list[str] = []

    async def finish(self, step, run, db, **_kwargs):
        executed.append(step["id"])
        return {"status": "completed", "output": f"{step['id']} done"}

    monkeypatch.setattr(WorkflowRunner, "_execute_step", finish)
    assert await WorkflowRunner(session_factory=async_session).run(
        run_id, claim_handoff_token=queued[0]["claim_handoff_token"],
    ) == "executed"
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "completed", run
    assert executed[0] == "draft"
    assert run["step_results"]["draft"]["output"] == "draft done"


@pytest.mark.asyncio
async def test_worker_shutdown_finishes_the_running_agent_step_and_hands_off_the_next(
    client, db_session, monkeypatch,
):
    from celery.signals import worker_shutting_down
    from sqlalchemy import select

    import packages.core.celery_app  # noqa: F401 - connects the shutdown receiver
    from packages.core import worker_drain
    from packages.core.ai.workflow_runner import (
        WORKFLOW_RUN_EXECUTION_RESUMED_ELSEWHERE,
        WorkflowRunner,
    )
    from packages.core.database import async_session

    headers, run_id = await _start_unexecuted_run(client, "wfdrainhandoff", _chain(
        {"id": "build", "type": "agent", "name": "Build", "config": {"prompt": "Build the edit."}},
        {"id": "render", "type": "agent", "name": "Render", "config": {"prompt": "Render it."}},
    ))
    monkeypatch.setattr(worker_drain, "_drain_flag", None)
    worker_drain.register_worker_main_process()
    executed: list[str] = []
    original_execute = WorkflowRunner._execute_step_safe

    async def agent_node(self, step, run, db, **kwargs):
        if step["type"] != "agent":
            return await original_execute(self, step, run, db, **kwargs)
        executed.append(step["id"])
        if step["id"] == "build":
            # A rollout's SIGTERM reaches the worker while this node runs.
            worker_shutting_down.send(
                sender="manor-worker-heavy@test", sig="SIGTERM", how="Warm", exitcode=0,
            )
        return {"status": "completed", "output": f"{step['id']} done"}

    queued: list[dict] = []
    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", agent_node)
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(
        lambda run_id, *_a, **kwargs: queued.append({"run_id": run_id, **kwargs}) or True
    ))
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)

    assert await WorkflowRunner(session_factory=async_session).run(run_id) == (
        WORKFLOW_RUN_EXECUTION_RESUMED_ELSEWHERE
    )
    assert executed == ["build"], "a draining worker must not start the next node"
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "running" and run["error"] is None
    assert run["step_results"]["build"]["status"] == "completed"
    assert "render" not in run["step_results"]
    stored = await db_session.get(WorkflowRun, run_id)
    await db_session.refresh(stored)
    assert "render" not in {entry.get("node_id") for entry in stored.execution_trace}
    [handoff] = queued
    lease = (await db_session.execute(
        select(RuntimeExecutionClaim)
        .where(RuntimeExecutionClaim.claim_key == f"workflow-run:{run_id}")
    )).scalar_one()
    assert lease.claim_token == handoff["claim_handoff_token"]

    # The replacement worker adopts the retained lease and runs the next node.
    worker_drain.register_worker_main_process()
    assert await WorkflowRunner(session_factory=async_session).run(
        run_id, claim_handoff_token=handoff["claim_handoff_token"],
    ) == "executed"
    assert executed == ["build", "render"]
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "completed", run
    assert "_execution_recovery" not in run["trigger_data"]
    assert await ai_tasks._recover_expired_workflow_claims() == {
        "checked": 0, "settled": 0, "resumed": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_status", ["failed", "paused"])
async def test_successor_applies_a_receipt_whose_run_outcome_was_never_committed(
    client, db_session, monkeypatch, settled_status,
):
    """2026-10-14: owners died between a node's receipt and the run outcome.

    The resumed run then had a failed node that was neither runnable nor done
    and failed with a bare "No runnable steps and workflow not complete".
    """
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session

    headers, run_id = await _start_unexecuted_run(
        client, f"wfclaimreceipt{settled_status}", _chain(
            {"id": "plan", "type": "transform", "config": {"set": {"planned": "yes"}}},
            {"id": "produce", "type": "agent", "name": "Produce",
             "config": {"prompt": "Render the video."}},
        ),
    )
    settled = (
        {"status": "failed",
         "error": "credit_check_unavailable: Unable to verify workspace budget right now."}
        if settled_status == "failed"
        else {"status": "paused", "wait_type": "approval", "output": "Approve the cut"}
    )
    await _crash_owner_inside_step(
        monkeypatch, db_session, run_id, "produce", settled_result=settled,
    )
    queued: list[dict] = []
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(
        lambda run_id, *_a, **kwargs: queued.append({"run_id": run_id, **kwargs}) or True
    ))
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)

    async def replay_forbidden(*args, **kwargs):
        pytest.fail("a node with a committed receipt was executed again")

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", replay_forbidden)
    assert (await ai_tasks._recover_expired_workflow_claims())["checked"] == 1
    if queued:
        await WorkflowRunner(session_factory=async_session).run(
            run_id, claim_handoff_token=queued[0]["claim_handoff_token"],
        )

    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == settled_status, run
    assert run["current_step_id"] == "produce"
    if settled_status == "failed":
        assert run["error"].startswith("credit_check_unavailable"), run["error"]


@pytest.mark.asyncio
async def test_manual_step_settles_an_unapplied_failed_receipt(client, db_session):
    from sqlalchemy import update

    headers, run_id = await _start_unexecuted_run(client, "wfclaimreceiptstep", _chain(
        {"id": "produce", "type": "agent", "config": {"prompt": "Render the video."}},
    ))
    await db_session.execute(
        update(WorkflowRun).where(WorkflowRun.id == run_id).values(
            status="running",
            current_step_id="produce",
            step_results={
                "trigger": {"status": "completed"},
                "produce": {"status": "failed", "error": "credit_check_unavailable"},
            },
        )
    )
    await db_session.commit()

    stepped = await client.post(f"/api/v1/workflows/runs/{run_id}/step", headers=headers)
    assert stepped.status_code == 200, stepped.text
    assert stepped.json()["status"] == "failed"
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "failed"
    assert run["error"] == "credit_check_unavailable"


def test_no_runnable_steps_error_names_every_step_and_state():
    from packages.core.ai.workflow_runner import _no_runnable_steps_error

    steps = [
        {"id": "start", "type": "trigger"},
        {"id": "produce", "type": "agent"},
        {"id": "note", "type": "note"},
        {"id": "end", "type": "end"},
    ]
    run = SimpleNamespace(
        current_step_id="produce",
        step_results={
            "start": {"status": "completed"},
            "produce": {"status": "failed", "continued": True},
        },
    )
    error = _no_runnable_steps_error(steps, run)
    assert error.startswith("No runnable steps and workflow not complete")
    assert "current step: produce" in error
    assert "start [trigger]: completed" in error
    assert "produce [agent]: failed (continued)" in error
    assert "end [end]: not run" in error
    assert "note" not in error.split("Steps:")[1]


def test_claim_loss_decision_counts_interruptions_not_redeliveries():
    def run(state=None, trace=None):
        return SimpleNamespace(
            trigger_data={"_execution_recovery": state} if state else {},
            execution_trace=trace if trace is not None else [
                {"node_id": "work", "status": "running"},
            ],
            definition_snapshot={},
        )

    now = datetime.now(timezone.utc)
    work = [{"id": "work", "type": "transform"}]
    first = decide_workflow_claim_loss(run(), work, lost_claim_token="a", now=now)
    assert first.resume and first.recovery_state["attempts"] == 1

    at_limit = {"attempts": MAX_WORKFLOW_EXECUTION_RECOVERIES,
                "handoff_claim_digest": workflow_claim_handoff_digest("handoff")}
    redelivered = decide_workflow_claim_loss(
        run(at_limit), work, lost_claim_token="handoff", now=now,
    )
    assert redelivered.resume
    assert redelivered.recovery_state["attempts"] == MAX_WORKFLOW_EXECUTION_RECOVERIES
    exhausted = decide_workflow_claim_loss(
        run(at_limit), work, lost_claim_token="successor", now=now,
    )
    assert not exhausted.resume and "automatic recovery stopped" in exhausted.error

    # A trace at capacity cannot prove a node never started: fail closed.
    full_trace = [{"node_id": "other", "status": "completed"}] * 2000
    unrecorded = decide_workflow_claim_loss(
        run(trace=full_trace), [{"id": "send", "type": "http", "config": {"method": "POST"}}],
        lost_claim_token=None, now=now,
    )
    assert not unrecorded.resume and "(http)" in unrecorded.error
    read_only = decide_workflow_claim_loss(
        run(trace=full_trace), [{"id": "read", "type": "http", "config": {"method": "GET"}}],
        lost_claim_token=None, now=now,
    )
    assert read_only.resume


def test_recovery_beat_has_a_control_consumer_and_expires():
    from packages.core.celery_app import celery_app
    from packages.core.queues import CeleryQueue, queue_for_task

    entry = celery_app.conf.beat_schedule["workflow-expired-claim-recovery"]
    assert entry["task"] == "workflow.recover_expired_claims"
    assert queue_for_task(entry["task"]) is CeleryQueue.CONTROL
    assert entry["options"]["expires"] <= entry["schedule"]
