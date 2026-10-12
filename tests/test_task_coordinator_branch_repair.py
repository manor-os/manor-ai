"""Same-Plan V3 recovery preserves successful effects and coordinator fences."""
from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.document import Document
from packages.core.models.task import Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION, TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.plans.executor import (
    PlanExecutor, TaskCoordinatorReplanOutcome,
    _TASK_COORDINATOR_REPAIR_KEY, prepare_task_coordinator_replan,
)
from packages.core.services.retry_policy import resolve_retry_policy_for_step
from packages.core.services.task_coordinator_steps import (
    TASK_COORDINATOR_CONTROL_KEY, TASK_COORDINATOR_STEP_INPUTS_KEY,
    TASK_COORDINATOR_DEPENDENCY_ARTIFACTS_KEY,
    _settled_tool_result, ack_settled_task_coordinator_batch,
    release_ready_task_coordinator_steps, task_coordinator_step_input_digest,
)
from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim
from packages.core.services.task_execution import CoordinatorRuntimeSelection, task_execution_intent_generation


@pytest.fixture(autouse=True)
def local_files(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")


def _receipt(plan, step, *, pending=False):
    return ToolEffectEvidenceFactory.bind_effect_id(ToolEffectEvidenceFactory.effect("pending" if pending else "action", {
        "tool": "generate_file", "tool_call_id": "Q-write-once",
        "arguments_hash": "a" * 64, "owner_plan_id": plan.id,
        "owner_step_id": step.id, "effect_generation": 0,
        "intent_state": "pending" if pending else "returned",
        "capability_id": "workspace.file.create", "document_id": "saved-Q",
    }))


async def _seed(db, *, paused_fanin=False):
    entity_id, workspace_id, task_id, plan_id, run_id = (generate_ulid() for _ in range(5))
    user_id = generate_ulid()
    task = Task(id=task_id, entity_id=entity_id, workspace_id=workspace_id,
                title="P and Q then R", creator_id=user_id,
                status="in_progress", task_type="general", details={})
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id, task_id=task_id,
        status="running", plan_dag={"steps": [{"key": key} for key in ("PQRS" if paused_fanin else "PQR")]},
        dispatcher_state={TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
                          TASK_COORDINATOR_RUN_ID_KEY: run_id},
    )
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    generation = generate_ulid()
    run = TaskCoordinatorRun(
        id=run_id, task_id=task_id, entity_id=entity_id, workspace_id=workspace_id,
        root_subscription_id=generate_ulid(), root_agent_id=generate_ulid(),
        selection_fingerprint="b" * 64,
        lifecycle_generation=task_execution_intent_generation(task),
        phase="running", current_plan_id=plan_id, checkpoint={"messages": []},
        state_version=0, wake_generation=generation, claim_generation=generation,
        claim_token="owner", claim_expires_at=expiry,
    )
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Repair fence", status="active")
    agent = Agent(id=run.root_agent_id, entity_id=entity_id, name="Writer", status="active")
    subscription = AgentSubscription(
        id=run.root_subscription_id, entity_id=entity_id, workspace_id=workspace_id,
        agent_id=agent.id, service_key="writer", status="active",
    )
    run.selection_fingerprint = CoordinatorRuntimeSelection.from_subscriptions([subscription]).stable_fingerprint()
    steps = [ExecutionStep(
        id=generate_ulid(), plan_id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        step_key=key, kind="subagent", step_status="paused", params={"prompt": key},
        depends_on=(["P", "Q", "S"] if paused_fanin else ["P", "Q"]) if key == "R" else (["Q"] if key == "S" else []),
        evidence_refs=[],
    ) for key in ("PQRS" if paused_fanin else "PQR")]
    db.add_all([
        Entity(id=entity_id, name="Isolated DAG repair"),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.invalid", password_hash="unused", role="owner", status="active"),
        UserMembership(entity_id=entity_id, user_id=user_id, role="owner", status="active"),
        workspace, agent, subscription, task, plan, run, *steps,
    ])
    await db.flush()
    claim = TaskCoordinatorClaim(run_id, task_id, generate_ulid(), generation, "owner", expiry, 0)
    release = await release_ready_task_coordinator_steps(
        db, task_id=task_id, plan_id=plan_id, claim=claim,
        expected_state_version=0, tool_call_id="first-batch",
    )
    assert release.step_ids == (steps[0].id, steps[1].id)
    p, q, r = steps[:3]
    p.step_status = "failed"
    p.attempt_count = 3
    p.error = {"type": "AgenticLoopError", "message": "stateful tool call failed"}
    p.last_execution_error = deepcopy(p.error)
    p.result = {"status": "failed"}
    q.step_status = "done"
    q.attempt_count = 1
    q.result = {"document_id": "saved-Q", "fs_path": "documents/Q.md"}
    q.evidence_refs = [_receipt(plan, q)]
    run.checkpoint = {**run.checkpoint, "messages": [{
        "role": "tool", "tool_call_id": "first-batch",
        "content": _settled_tool_result(plan_id, release.step_ids, {step.id: step for step in steps}),
    }]}
    await ack_settled_task_coordinator_batch(
        db, task_id=task_id, plan_id=plan_id, claim=claim,
        expected_state_version=run.state_version,
    )
    await db.flush()
    return SimpleNamespace(task=task, plan=plan, run=run, p=p, q=q, r=r, steps=steps, claim=claim,
                           workspace=workspace, agent=agent, subscription=subscription)


async def _saved_q_document(db, scope, tmp_path):
    source = tmp_path / scope.task.entity_id / "Q.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Q saved source", encoding="utf-8")
    document = Document(
        id=generate_ulid(), entity_id=scope.task.entity_id, name="Q.md", fs_path="Q.md",
        owner_id=scope.task.creator_id, visibility="entity", file_type="md",
        metadata_={"producing_plan_id": scope.plan.id, "producing_step_id": scope.q.id},
    )
    db.add(document)
    await db.flush()
    scope.q.result = {"document_id": document.id, "fs_path": document.fs_path,
                      "source_sha256": hashlib.sha256(b"Q saved source").hexdigest()}
    scope.q.evidence_refs = [ToolEffectEvidenceFactory.bind_effect_id({
        **scope.q.evidence_refs[0],
        "fields": {**scope.q.evidence_refs[0]["fields"], "document_id": document.id},
    })]
    return document, source


async def _repair(db, scope, *, call_id="repair-call", version=None):
    return await prepare_task_coordinator_replan(
        db, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
        issue="Fix P's tool input schema and preserve Q's existing saved file.",
        expected_state_version=scope.run.state_version if version is None else version,
        tool_call_id=call_id,
    )


async def _execution_state(db, scope):
    """Snapshot all mutation owners, including extra Plans and historical leases."""
    await db.flush()
    plans = list((await db.execute(select(ExecutionPlan).where(
        ExecutionPlan.task_id == scope.task.id,
    ).order_by(ExecutionPlan.id))).scalars().all())
    leases = list((await db.execute(select(WorkLease).where(
        WorkLease.plan_id.in_([plan.id for plan in plans]),
    ).order_by(WorkLease.id))).scalars().all())
    rows = [scope.task, scope.run, *plans, *scope.steps, *leases]
    for row in rows:
        await db.refresh(row)
    return deepcopy({
        (type(row).__name__, row.id): {
            column.key: getattr(row, column.key) for column in row.__table__.columns
        }
        for row in rows
    })


def test_input_digest_detects_business_edits_but_not_retry_bookkeeping():
    task = SimpleNamespace(id="T", details={"business": "original"})
    plan = SimpleNamespace(id="plan", plan_dag={})
    upstream = SimpleNamespace(id="Q", step_key="Q", step_status="done", result={"text": "source"},
                               expected_output_schema=None, evidence_refs=[])
    step = SimpleNamespace(id="P", step_key="P", params={"prompt": "${{ steps.Q.result.text }}"},
                           depends_on=["Q"], step_status="failed", expected_output_schema=None)
    original = task_coordinator_step_input_digest(task, plan, step, [upstream, step])
    step.params = {"prompt": "source", "retry_policy": {"max_attempts": 4}, "_supervisor_feedback": "fix"}
    assert task_coordinator_step_input_digest(task, plan, step, [upstream, step]) == original
    upstream.result = {"text": "changed"}
    assert task_coordinator_step_input_digest(task, plan, step, [upstream, step]) != original
    upstream.result = {"text": "source"}
    step.params["_execution_contract"] = {"delivery": "artifact", "effects": "authorized"}
    assert task_coordinator_step_input_digest(task, plan, step, [upstream, step]) != original


@pytest.mark.asyncio
async def test_repairs_only_failed_branch_and_releases_it_after_ack(db_session, tmp_path):
    scope = await _seed(db_session)
    document, _ = await _saved_q_document(db_session, scope, tmp_path)
    document_version = document.updated_at
    q_before = deepcopy({column.key: getattr(scope.q, column.key) for column in ExecutionStep.__table__.columns})
    failed_error = deepcopy(scope.p.error)
    outcome, detail, event = await _repair(db_session, scope)
    assert outcome is TaskCoordinatorReplanOutcome.REPAIRED
    assert detail == scope.plan.id and event is None
    assert scope.plan.status == "running" and scope.task.status == "in_progress"
    assert scope.run.current_plan_id == scope.plan.id
    assert scope.p.step_status == scope.r.step_status == "paused"
    assert scope.p.attempt_count == 3 and scope.p.max_attempts == 4
    assert scope.p.last_execution_error == failed_error
    assert (await resolve_retry_policy_for_step(db_session, scope.p, plan=scope.plan)).max_attempts == 4
    assert {column.key: getattr(scope.q, column.key) for column in ExecutionStep.__table__.columns} == q_before
    assert scope.plan.dispatcher_state[_TASK_COORDINATOR_REPAIR_KEY]["steps"][0]["error"] == failed_error
    released = await release_ready_task_coordinator_steps(
        db_session, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
        expected_state_version=scope.run.state_version, tool_call_id="retry-P",
    )
    assert released.step_ids == (scope.p.id,)
    assert scope.r.step_status == "paused" and scope.q.attempt_count == 1
    scope.p.step_status = "done"
    scope.p.attempt_count += 1
    scope.p.result = {"text": "P done"}
    scope.run.checkpoint = {**scope.run.checkpoint, "messages": [{
        "role": "tool", "tool_call_id": "retry-P",
        "content": _settled_tool_result(scope.plan.id, released.step_ids, {scope.p.id: scope.p}),
    }]}
    await ack_settled_task_coordinator_batch(
        db_session, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
        expected_state_version=scope.run.state_version,
    )
    terminal = await release_ready_task_coordinator_steps(
        db_session, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
        expected_state_version=scope.run.state_version, tool_call_id="run-R",
    )
    assert terminal.step_ids == (scope.r.id,)
    assert scope.q.result == q_before["result"] and scope.q.evidence_refs == q_before["evidence_refs"]
    await db_session.refresh(document)
    assert document.updated_at == document_version
    assert (await db_session.execute(select(Document.id).where(Document.entity_id == scope.task.entity_id))).scalars().all() == [document.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["params", "task", "dag", "done_input", "missing_snapshot", "pending_effect", "lease_only_effect", "failed_writer", "executed_descendant", "live_claim", "attempt_cap", "unconfirmed_failure"])
async def test_unsafe_or_changed_branch_does_not_repair(db_session, monkeypatch, drift):
    scope = await _seed(db_session)
    if drift == "params":
        scope.p.params = {"prompt": "write somewhere else"}
    elif drift == "task":
        scope.task.description = "A different request"
    elif drift == "dag":
        scope.plan.plan_dag = {"steps": [{"key": "changed"}]}
    elif drift == "done_input":
        scope.q.params = {"prompt": "A different contract"}
    elif drift == "missing_snapshot":
        scope.plan.dispatcher_state = {key: value for key, value in scope.plan.dispatcher_state.items() if key != TASK_COORDINATOR_STEP_INPUTS_KEY}
    elif drift == "pending_effect":
        scope.q.evidence_refs = [_receipt(scope.plan, scope.q, pending=True)]
    elif drift == "failed_writer":
        scope.p.evidence_refs = [_receipt(scope.plan, scope.p)]
        scope.p.runtime_checkpoint = {"messages": [{"role": "tool", "tool_call_id": "Q-write-once", "content": "saved"}]}
    elif drift == "executed_descendant":
        scope.r.step_status = "done"
        scope.r.attempt_count = 1
    elif drift == "attempt_cap":
        scope.p.attempt_count = 10
    elif drift == "unconfirmed_failure":
        # An unconfirmed effect is never replayed, even without retryable=False.
        scope.p.error = {"type": "StepResultFailed", "failure": {"do_not_replay": True}}
    elif drift == "live_claim":
        db_session.add(WorkLease(
            id=generate_ulid(), step_id=scope.p.id, plan_id=scope.plan.id,
            entity_id=scope.task.entity_id, workspace_id=scope.task.workspace_id,
            worker_id=generate_ulid(), lease_until=datetime.now(timezone.utc),
            status="expired", execution_claim_id="still-running",
        ))
    elif drift == "lease_only_effect":
        db_session.add(WorkLease(
            id=generate_ulid(), step_id=scope.q.id, plan_id=scope.plan.id,
            entity_id=scope.task.entity_id, workspace_id=scope.task.workspace_id,
            worker_id=generate_ulid(), lease_until=datetime.now(timezone.utc),
            status="expired", evidence_refs=[_receipt(scope.plan, scope.q, pending=True)],
        ))

    before = await _execution_state(db_session, scope)
    structural_replan = AsyncMock(side_effect=RuntimeError("existing structural replan boundary"))
    monkeypatch.setattr(PlanExecutor, "_maybe_replan", structural_replan)
    if drift in {"pending_effect", "lease_only_effect", "failed_writer"}:
        # Unknown writes retain their existing gate;
        # settled sibling evidence must never hide those facts.
        with pytest.raises(RuntimeError, match="structural replan boundary"):
            await _repair(db_session, scope)
        structural_replan.assert_awaited_once()
    else:
        outcome, diagnostic, event = await _repair(db_session, scope)
        assert outcome is TaskCoordinatorReplanOutcome.BLOCKED and event is None
        assert diagnostic["contract"] == "tool_error"
        assert diagnostic["error"] == "plan_replacement_would_repeat_effects"
        assert diagnostic["details"]["effect_state"] == "settled"
        assert diagnostic["details"]["action_executed"] is False
        assert diagnostic["details"]["do_not_replay"] is True
        assert diagnostic["details"]["delivery_accepted"] is False
        assert diagnostic["details"]["available_actions"] == ["inspect_task_delivery"]
        # Repeating even the same refused control call neither grants an
        # attempt nor changes the original Plan/effect identities.
        assert await _repair(db_session, scope) == (outcome, diagnostic, event)
        structural_replan.assert_not_awaited()
    assert await _execution_state(db_session, scope) == before
    assert scope.p.step_status == "failed"
    assert _TASK_COORDINATOR_REPAIR_KEY not in scope.plan.dispatcher_state


@pytest.mark.asyncio
async def test_model_human_flag_alone_does_not_stop_effect_free_branch_repair(db_session, monkeypatch):
    scope = await _seed(db_session)
    scope.p.error = {"type": "StepResultFailed", "failure": {"requires_human": True}}
    sibling_before = deepcopy(scope.q.result), deepcopy(scope.q.evidence_refs), scope.q.attempt_count
    replan = AsyncMock(side_effect=AssertionError("Same-Plan repair must preserve successful siblings"))
    monkeypatch.setattr(PlanExecutor, "_maybe_replan", replan)

    outcome, plan_id, event = await _repair(db_session, scope)

    assert outcome is TaskCoordinatorReplanOutcome.REPAIRED and plan_id == scope.plan.id
    assert event is None
    assert scope.p.step_status == "paused"
    assert scope.p.max_attempts == 4
    assert (scope.q.result, scope.q.evidence_refs, scope.q.attempt_count) == sibling_before
    assert scope.task.status == "in_progress" and scope.run.phase == "running"
    replan.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_is_once_per_plan_and_exact_call_replay_is_read_only(db_session, monkeypatch):
    scope = await _seed(db_session)
    old_version = scope.run.state_version
    await _repair(db_session, scope)
    version = scope.run.state_version
    outcome, _, _ = await _repair(db_session, scope, version=old_version)
    assert outcome is TaskCoordinatorReplanOutcome.REPAIRED
    assert scope.run.state_version == version
    assert scope.p.attempt_count == 3 and scope.p.max_attempts == 4
    with pytest.raises(ValueError, match="state version"):
        await _repair(db_session, scope, call_id="stale-other-call", version=old_version)
    scope.p.step_status = "failed"
    scope.p.attempt_count = 4

    before = await _execution_state(db_session, scope)
    exhausted = AsyncMock(side_effect=AssertionError("Settled writers cannot enter structural replan"))
    monkeypatch.setattr(PlanExecutor, "_maybe_replan", exhausted)
    outcome, diagnostic, event = await _repair(db_session, scope, call_id="second-repair")
    assert outcome is TaskCoordinatorReplanOutcome.BLOCKED and event is None
    assert diagnostic["error"] == "plan_replacement_would_repeat_effects"
    assert diagnostic["details"]["action_executed"] is False
    assert diagnostic["details"]["do_not_replay"] is True
    assert diagnostic["details"]["delivery_accepted"] is False
    assert await _execution_state(db_session, scope) == before
    exhausted.assert_not_awaited()
    assert scope.p.attempt_count == scope.p.max_attempts == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("contenders", [2, 8, 32])
async def test_concurrent_repairs_have_one_version_winner(db_session, contenders):
    scope = await _seed(db_session)
    version = scope.run.state_version
    q_before = deepcopy(scope.q.evidence_refs)
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    start = asyncio.Event()

    async def contender(index):
        await start.wait()
        async with factory() as db:
            try:
                outcome, _, _ = await _repair(db, scope, call_id=f"repair-{index}", version=version)
                await db.commit()
                return outcome
            except ValueError as exc:
                await db.rollback()
                assert "state version" in str(exc)
                return "stale"

    pending = [asyncio.create_task(contender(index)) for index in range(contenders)]
    start.set()
    results = await asyncio.wait_for(asyncio.gather(*pending), timeout=30)
    assert results.count(TaskCoordinatorReplanOutcome.REPAIRED) == 1
    assert results.count("stale") == contenders - 1
    await db_session.refresh(scope.p)
    await db_session.refresh(scope.q)
    await db_session.refresh(scope.run)
    assert scope.p.step_status == "paused" and scope.p.attempt_count == 3 and scope.p.max_attempts == 4
    assert scope.q.evidence_refs == q_before and scope.q.attempt_count == 1
    assert scope.run.state_version == version + 1
    assert len((await db_session.execute(select(ExecutionPlan).where(ExecutionPlan.task_id == scope.task.id))).scalars().all()) == 1
    assert not (await db_session.execute(select(WorkLease).where(WorkLease.plan_id == scope.plan.id))).scalars().all()


@pytest.mark.asyncio
async def test_unacknowledged_batch_and_stale_claim_cannot_repair(db_session):
    scope = await _seed(db_session)
    scope.run.checkpoint = {TASK_COORDINATOR_CONTROL_KEY: {"unacknowledged": True}}
    assert (await _repair(db_session, scope))[0] is TaskCoordinatorReplanOutcome.BLOCKED
    assert scope.p.step_status == "failed"
    scope.run.checkpoint = {}
    scope.run.claim_token = "replacement-owner"
    with pytest.raises(ValueError, match="current claim"):
        await _repair(db_session, scope)


@pytest.mark.asyncio
async def test_old_failed_lease_cannot_settle_repaired_step(db_session):
    from packages.core.dispatcher.service import Dispatcher, LeaseNotActive

    scope = await _seed(db_session)
    lease = WorkLease(
        id=generate_ulid(), step_id=scope.p.id, plan_id=scope.plan.id,
        entity_id=scope.task.entity_id, workspace_id=scope.task.workspace_id,
        worker_id=generate_ulid(), lease_until=datetime.now(timezone.utc),
        status="failed", error=deepcopy(scope.p.error),
    )
    db_session.add(lease)
    await db_session.flush()
    assert (await _repair(db_session, scope))[0] is TaskCoordinatorReplanOutcome.REPAIRED
    with pytest.raises(LeaseNotActive):
        await Dispatcher().complete_lease(db_session, lease.id, result={"text": "late old result"})
    assert scope.p.step_status == "paused" and scope.p.result is None
    assert lease.status == "failed" and lease.error == scope.p.last_execution_error


@pytest.mark.asyncio
@pytest.mark.parametrize("paused_fanin", [False, True])
async def test_repair_revives_never_ran_descendant_but_still_requires_release(db_session, paused_fanin):
    scope = await _seed(db_session, paused_fanin=paused_fanin)
    scope.r.step_status = "skipped"
    scope.r.finished_at = datetime.now(timezone.utc)
    assert (await _repair(db_session, scope))[0] is TaskCoordinatorReplanOutcome.REPAIRED
    assert scope.r.step_status == "paused" and scope.r.attempt_count == 0
    assert scope.r.finished_at is None


@pytest.mark.asyncio
async def test_repair_rollback_leaves_no_budget_grant_and_new_release_rechecks_inputs(db_session):
    scope = await _seed(db_session)
    await db_session.commit()
    await _repair(db_session, scope)
    await db_session.rollback()
    for row in (scope.task, scope.plan, scope.run, *scope.steps):
        await db_session.refresh(row)
    assert scope.p.step_status == "failed" and scope.p.attempt_count == 3
    assert _TASK_COORDINATOR_REPAIR_KEY not in scope.plan.dispatcher_state
    assert (await _repair(db_session, scope))[0] is TaskCoordinatorReplanOutcome.REPAIRED
    scope.p.params = {**scope.p.params, "prompt": "changed after repair"}
    result = await release_ready_task_coordinator_steps(
        db_session, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
        expected_state_version=scope.run.state_version, tool_call_id="release-changed-P",
    )
    assert result.status == "blocked" and "inputs changed" in result.reason
    assert scope.p.step_status == "paused" and scope.q.step_status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["inactive", "deleted", "unsubscribed", "rebound"])
@pytest.mark.parametrize("operation", ["repair", "release"])
async def test_revoked_workspace_or_changed_live_binding_rejects_mutation(db_session, mutation, operation):
    scope = await _seed(db_session)
    if operation == "release":
        assert (await _repair(db_session, scope))[0] is TaskCoordinatorReplanOutcome.REPAIRED
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with factory() as revoke_db:
        workspace = await revoke_db.get(Workspace, scope.workspace.id)
        subscription = await revoke_db.get(AgentSubscription, scope.subscription.id)
        if mutation == "inactive":
            workspace.status = "inactive"
        elif mutation == "deleted":
            workspace.deleted_at = datetime.now(timezone.utc)
        elif mutation == "unsubscribed":
            subscription.status = "paused"
        else:
            subscription.agent_id = generate_ulid()
        await revoke_db.commit()
    async with factory() as stale_db:
        with pytest.raises(ValueError):
            if operation == "repair":
                await _repair(stale_db, scope)
            else:
                await release_ready_task_coordinator_steps(
                    stale_db, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
                    expected_state_version=scope.run.state_version, tool_call_id="stale-release",
                )
        await stale_db.rollback()
    await db_session.refresh(scope.p)
    await db_session.refresh(scope.q)
    assert scope.p.step_status == ("failed" if operation == "repair" else "paused")
    assert scope.p.attempt_count == 3 and scope.q.step_status == "done"
    assert not (await db_session.execute(select(WorkLease).where(WorkLease.plan_id == scope.plan.id))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "bytes", "trash", "revoked_membership"])
async def test_failed_consumer_revalidates_pinned_artifact_before_repair(db_session, tmp_path, change):
    scope = await _seed(db_session)
    document, source = await _saved_q_document(db_session, scope, tmp_path)
    scope.p.step_status = "done"
    scope.p.error = None
    scope.p.result = {"text": "P done"}
    release = await release_ready_task_coordinator_steps(
        db_session, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
        expected_state_version=scope.run.state_version, tool_call_id="consume-Q",
    )
    assert release.status == "released" and release.step_ids == (scope.r.id,)
    pinned = scope.plan.dispatcher_state[TASK_COORDINATOR_DEPENDENCY_ARTIFACTS_KEY][scope.q.id]
    assert pinned[0]["source_sha256"] == hashlib.sha256(b"Q saved source").hexdigest()
    scope.r.step_status = "failed"
    scope.r.attempt_count = 3
    scope.r.error = {"type": "AgenticLoopError", "message": "tool input schema failed"}
    scope.run.checkpoint = {**scope.run.checkpoint, "messages": [{
        "role": "tool", "tool_call_id": "consume-Q",
        "content": _settled_tool_result(scope.plan.id, release.step_ids, {scope.r.id: scope.r}),
    }]}
    await ack_settled_task_coordinator_batch(
        db_session, task_id=scope.task.id, plan_id=scope.plan.id, claim=scope.claim,
        expected_state_version=scope.run.state_version,
    )
    if change == "bytes":
        source.write_text("unexpected source change", encoding="utf-8")
    elif change == "trash":
        document.is_trashed = True
    elif change == "revoked_membership":
        membership = (await db_session.execute(select(UserMembership).where(
            UserMembership.user_id == scope.task.creator_id,
        ))).scalar_one()
        membership.status = "inactive"
    outcome, _, _ = await _repair(db_session, scope)
    if change == "unchanged":
        assert outcome is TaskCoordinatorReplanOutcome.REPAIRED
        assert scope.r.step_status == "paused" and scope.r.attempt_count == 3
    else:
        assert outcome is TaskCoordinatorReplanOutcome.BLOCKED
        assert scope.r.step_status == "failed" and scope.r.attempt_count == 3
        assert _TASK_COORDINATOR_REPAIR_KEY not in scope.plan.dispatcher_state
    assert scope.q.step_status == "done" and scope.q.attempt_count == 1
