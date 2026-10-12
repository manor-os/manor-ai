"""Task-owned, current execution evidence; no provider or repair calls."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Workspace
from packages.core.services.task_execution import task_execution_intent_generation
from packages.core.strategist.execution_snapshot import _blocker_fact, _error_fact, gather_execution_snapshot

pytestmark = pytest.mark.oss_regression


@pytest.fixture(autouse=True)
def _isolate_sandbox_owner_registry():
    """This reader never touches Redis or the Sandbox registry."""
    yield


def task_row(**changes):
    values = dict(id=generate_ulid(), entity_id=generate_ulid(), workspace_id=generate_ulid(),
                  title="Research", status="blocked", task_type="general", details={},
                  status_changed_at=datetime.now(timezone.utc), owner_service_key="research",
                  owner_subscription_id=generate_ulid(), delegate_service_keys=[])
    values.update(changes)
    return Task(**values)


def add_blocker(task, *, agent_id=None):
    agent_id = agent_id or generate_ulid()
    run = TaskCoordinatorRun(id=generate_ulid(), task_id=task.id, entity_id=task.entity_id,
        workspace_id=task.workspace_id, root_subscription_id=task.owner_subscription_id,
        root_agent_id=agent_id, selection_fingerprint="selection", lifecycle_generation="earlier-start",
        phase="waiting_human", state_version=4)
    task.details = {"capability_blocker": {
        "kind": "tool_missing", "stage": "planning", "generation": task_execution_intent_generation(task),
        "run_id": run.id, "selection_fingerprint": run.selection_fingerprint, "fingerprint": "recorded-gap",
        "missing_capabilities": ["web.safe_search"], "affected_agents": [{
            "service_key": "research", "subscription_id": task.owner_subscription_id, "agent_id": agent_id,
        }],
    }}
    scope = NS(subscription_id=task.owner_subscription_id, agent_id=agent_id, available=True, capability_ids=())
    return run, scope


def test_blocker_comparison_requires_exact_deployment_and_resolved_scope():
    task = task_row()
    run, scope = add_blocker(task)
    scopes = {"research": scope}
    assert _blocker_fact(task, run, scopes)["scope_status"] == "still_missing"
    scope.capability_ids = ("web.safe_search",)
    task.details["capability_blocker"]["affected_agents"][0]["secret"] = "not-for-display"
    changed = _blocker_fact(task, run, scopes)
    assert changed["scope_status"] == "configuration_changed"
    assert changed["recorded_missing_capabilities"] == ["web.safe_search"]
    assert changed["current_missing_capabilities"] == []
    assert changed["affected_agents"] == [{"service_key": "research",
        "subscription_id": scope.subscription_id, "agent_id": scope.agent_id}]
    assert "not-for-display" not in str(changed)
    scope.subscription_id = generate_ulid()
    assert _blocker_fact(task, run, scopes)["current_missing_capabilities"] is None
    scope.subscription_id = task.owner_subscription_id
    scope.agent_id = generate_ulid()
    assert _blocker_fact(task, run, scopes)["scope_status"] == "unavailable"
    scope.agent_id = run.root_agent_id
    scope.available = False
    assert _blocker_fact(task, run, scopes)["scope_status"] == "unavailable"


def test_legacy_ownerless_task_uses_exact_coordinator_root_and_partial_affected_scope():
    task = task_row()
    run, scope = add_blocker(task)
    task.owner_service_key = task.owner_subscription_id = None
    task.details["capability_blocker"]["generation"] = task_execution_intent_generation(task)
    fact = _blocker_fact(task, run, {"research": scope})
    assert fact["scope_status"] == "still_missing"
    assert fact["affected_agents"][0]["subscription_id"] == run.root_subscription_id
    assert fact["affected_agents"][0]["agent_id"] == run.root_agent_id
    task.owner_service_key, task.owner_subscription_id = "research", scope.subscription_id
    task.delegate_service_keys = ["writer"]
    task.details["capability_blocker"]["generation"] = task_execution_intent_generation(task)
    assert _blocker_fact(task, run, {"research": scope})["scope_status"] == "still_missing"


@pytest.mark.parametrize("kind", ["connection_required", "approval_required", "policy_denied"])
def test_tool_binding_does_not_prove_provider_health_or_approval(kind):
    task = task_row()
    run, scope = add_blocker(task)
    task.details["capability_blocker"]["kind"] = kind
    scope.capability_ids = ("web.safe_search",)
    fact = _blocker_fact(task, run, {"research": scope})
    assert fact["kind"] == kind
    assert fact["scope_status"] == "unavailable"
    assert fact["current_missing_capabilities"] is None


@pytest.mark.parametrize("field,value", [("kind", []), ("kind", {}), ("stage", []), ("stage", {})])
def test_malformed_blocker_classification_is_ignored(field, value):
    task = task_row()
    run, scope = add_blocker(task)
    task.details["capability_blocker"][field] = value
    assert _blocker_fact(task, run, {"research": scope}) is None


def test_blocker_refs_are_bounded_and_ownerless_tasks_cannot_borrow_another_agent():
    task = task_row()
    run, scope = add_blocker(task)
    task.details["capability_blocker"]["fingerprint"] = "untrusted legacy value" * 1000
    assert len(_blocker_fact(task, run, {"research": scope})["ref"]) == len(f"capability:{task.id}:") + 64
    task.owner_service_key = task.owner_subscription_id = None
    blocker = task.details["capability_blocker"]
    blocker["generation"] = task_execution_intent_generation(task)
    foreign = NS(subscription_id=generate_ulid(), agent_id=generate_ulid(), available=True, capability_ids=("web.safe_search",))
    blocker["affected_agents"] = [{"service_key": "foreign", "subscription_id": foreign.subscription_id, "agent_id": foreign.agent_id}]
    result = _blocker_fact(task, run, {"research": scope, "foreign": foreign})
    assert result["scope_status"] == "unavailable" and result["current_missing_capabilities"] is None


@pytest.mark.parametrize("change", ["generation", "run", "selection", "resumed", "has_plan"])
def test_old_capability_occurrence_is_not_a_current_blocker(change):
    task = task_row()
    run, scope = add_blocker(task)
    if change == "generation":
        task.status_changed_at += timedelta(seconds=1)
    elif change == "run":
        run.id = generate_ulid()
    elif change == "selection":
        run.selection_fingerprint = "replacement"
    elif change == "resumed":
        task.status = "in_progress"
    else:
        run.current_plan_id = generate_ulid()
    assert _blocker_fact(task, run, {"research": scope}) is None


def test_error_evidence_is_bounded_redacted_and_cannot_name_another_tasks_plan():
    task = task_row()
    plan = NS(id=generate_ulid(), task_id=task.id)
    log = NS(id=generate_ulid(), created_at=task.status_changed_at,
             meta={"plan_id": plan.id, "error": "coordinator_acceptance_exhausted"},
             content="api_key=sk-secret-value-1234567890 " + "x" * 1400)
    fact = _error_fact(task, log, {plan.id: plan})
    assert fact["source_task_id"] == task.id and fact["plan_id"] == plan.id
    assert fact["code"] == "coordinator_acceptance_exhausted"
    assert len(fact["message"]) <= 1000 and "sk-secret-value" not in fact["message"]
    log.meta.update(error={"type": "completion_invalid"}, stop_reason="max_rounds")
    nested = _error_fact(task, log, {plan.id: plan})
    assert nested["code"] == "completion_invalid" and nested["stop_reason"] == "max_rounds"
    plan.task_id = generate_ulid()
    assert _error_fact(task, log, {plan.id: plan}) is None
    plan.task_id = task.id
    log.created_at -= timedelta(seconds=1)
    assert _error_fact(task, log, {plan.id: plan}) is None


@pytest.mark.asyncio
async def test_snapshot_read_failure_propagates_instead_of_reporting_no_work():
    db = NS(no_autoflush=nullcontext(), execute=AsyncMock(side_effect=RuntimeError("database unavailable")))
    workspace = NS(id="workspace", entity_id="entity")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await gather_execution_snapshot(db, workspace=workspace, tasks=[], plans=[], outcomes={}, service_capabilities={})


async def seed(db):
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Snapshot", status="active")
    held = task_row(entity_id=workspace.entity_id, workspace_id=workspace.id, title="Inspect completed delivery")
    pending = task_row(entity_id=workspace.entity_id, workspace_id=workspace.id, status="pending", title="Pending script")
    research = task_row(entity_id=workspace.entity_id, workspace_id=workspace.id, title="Find reference")
    foreign = task_row()
    plan = ExecutionPlan(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                         task_id=held.id, status="running", plan_dag={})
    step = ExecutionStep(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                        plan_id=plan.id, step_key="draft", kind="subagent", step_status="done", params={}, result={})
    run = TaskCoordinatorRun(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        task_id=held.id, current_plan_id=plan.id, root_subscription_id=held.owner_subscription_id,
        root_agent_id=generate_ulid(), selection_fingerprint="held-selection", lifecycle_generation="earlier-start",
        phase="waiting_human", checkpoint={"messages": [{"content": "private checkpoint should stay private"}]})
    gap_run, scope = add_blocker(research)
    error = TaskLog(id=generate_ulid(), task_id=held.id, log_type="ai_execution_failed",
                    content="Coordinator could not submit a valid completion within the bounded budget.",
                    meta={"plan_id": plan.id}, created_at=held.status_changed_at)
    db.add_all([workspace, held, pending, research, foreign, plan, step, run, gap_run, error])
    await db.flush()
    return NS(workspace=workspace, held=held, pending=pending, research=research, foreign=foreign,
              plan=plan, step=step, run=run, gap_run=gap_run, scope=scope)


async def read(db, fixture):
    return await gather_execution_snapshot(db, workspace=fixture.workspace,
        tasks=[fixture.research, fixture.foreign], plans=[fixture.plan], outcomes={"pending": [fixture.pending]},
        service_capabilities={"research": fixture.scope})


@pytest.mark.asyncio
async def test_db_snapshot_preserves_task_error_ownership_and_pending_state(db_session):
    fixture = await seed(db_session)
    snapshot = await read(db_session, fixture)
    by_id = {fact["task_id"]: fact for fact in snapshot["tasks"]}
    assert set(by_id) == {fixture.held.id, fixture.pending.id, fixture.research.id}
    held = by_id[fixture.held.id]
    assert held["owner_service_key"] == fixture.held.owner_service_key
    assert held["owner_subscription_id"] == fixture.held.owner_subscription_id
    assert held["coordinator"]["root_agent_id"] == fixture.run.root_agent_id
    assert held["coordinator"]["root_subscription_id"] == fixture.run.root_subscription_id
    assert held["observed_state"] == "blocked" and held["plan_status"] == "running"
    assert held["step_counts"] == {"done": 1} and held["current_blocker"] is None
    assert held["latest_error"]["source_task_id"] == fixture.held.id
    assert held["latest_error"]["plan_id"] == fixture.plan.id
    assert by_id[fixture.research.id]["current_blocker"]["source_task_id"] == fixture.research.id
    assert by_id[fixture.research.id]["current_blocker"]["scope_status"] == "still_missing"
    assert by_id[fixture.research.id]["current_blocker"]["affected_agents"] == [{
        "service_key": "research", "subscription_id": fixture.gap_run.root_subscription_id,
        "agent_id": fixture.gap_run.root_agent_id,
    }]
    assert by_id[fixture.pending.id]["observed_state"] == "not_started"
    assert "private checkpoint" not in str(snapshot)
    assert (await read(db_session, fixture))["revision"] == snapshot["revision"]
    assert not db_session.dirty
    fixture.held.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await db_session.flush()
    assert (await read(db_session, fixture))["revision"] == snapshot["revision"]


@pytest.mark.asyncio
async def test_db_only_current_admitted_lease_proves_running_and_revision_changes(db_session):
    fixture = await seed(db_session)
    fixture.held.status = "in_progress"
    fixture.run.phase = "waiting_child"
    fixture.step.step_status = "running"
    now = datetime.now(timezone.utc)
    old = WorkLease(id=generate_ulid(), entity_id=fixture.workspace.entity_id, workspace_id=fixture.workspace.id,
        plan_id=fixture.plan.id, step_id=fixture.step.id, worker_id=generate_ulid(), status="active",
        lease_until=now + timedelta(minutes=2), execution_claim_id="old", execution_claimed_at=now)
    current = WorkLease(id=generate_ulid(), entity_id=fixture.workspace.entity_id, workspace_id=fixture.workspace.id,
        plan_id=fixture.plan.id, step_id=fixture.step.id, worker_id=generate_ulid(), status="active",
        lease_until=now + timedelta(minutes=2), execution_claim_id="preminted")
    fixture.step.current_lease_id = current.id
    db_session.add_all([old, current])
    await db_session.flush()
    allocated = await read(db_session, fixture)
    assert next(row for row in allocated["tasks"] if row["task_id"] == fixture.held.id)["observed_state"] == "assigned"
    current.execution_claimed_at = now
    await db_session.flush()
    running = await read(db_session, fixture)
    assert next(row for row in running["tasks"] if row["task_id"] == fixture.held.id)["observed_state"] == "running"
    assert running["revision"] != allocated["revision"]
    current.lease_until = now - timedelta(seconds=1)
    await db_session.flush()
    assert next(row for row in (await read(db_session, fixture))["tasks"] if row["task_id"] == fixture.held.id)["observed_state"] == "not_started"


@pytest.mark.asyncio
async def test_db_cross_task_plan_pointer_does_not_borrow_steps_or_errors(db_session):
    fixture = await seed(db_session)
    fixture.gap_run.current_plan_id = fixture.plan.id
    await db_session.flush()
    fact = next(row for row in (await read(db_session, fixture))["tasks"] if row["task_id"] == fixture.research.id)
    assert fact["plan_id"] is None and fact["step_counts"] == {}
    assert fact["current_blocker"] is None and fact["latest_error"] is None
