"""Workspace automatic-running pause preserves explicitly requested work."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.planner import (
    PlannerError,
    WorkspaceAutonomyPausedError,
    _lock_task_planning_origin,
)
from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim
from packages.core.services.task_execution_origin import (
    plan_manual_execution_predicate,
    record_manual_plan_execution,
    record_manual_task_execution,
    task_has_manual_execution,
)


@pytest.mark.parametrize("details", [
    {"scheduled_job_id": "job"}, {"scheduled_run_id": "run"},
    {"strategist_review_id": "review"}, {"automation_origin": {}},
])
def test_automation_requester_attribution_does_not_authorize_manual_execution(details):
    task = SimpleNamespace(creator_id="owner", details=details)
    assert not task_has_manual_execution(task)
    record_manual_task_execution(task, user_id="operator", action="task_retry")
    assert task_has_manual_execution(task)
    assert all(task.details[key] == value for key, value in details.items())


def _planning_claim(task_id: str) -> TaskCoordinatorClaim:
    return TaskCoordinatorClaim(
        "run", task_id, "event", "generation", "token",
        datetime.now(timezone.utc) + timedelta(minutes=5), 1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_planner_paused_admission_uses_task_origin(db_session, manual):
    workspace = Workspace(
        id=generate_ulid(), entity_id=generate_ulid(), name="Paused", status="paused",
    )
    task = Task(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        title="Patch existing landing page", status="in_progress", task_type="general",
        creator_id=generate_ulid(), details={} if manual else {"strategist_review_id": "review"},
    )
    db_session.add_all([workspace, task])
    await db_session.flush()

    if manual:
        admitted, _ = await _lock_task_planning_origin(
            db_session, task.id, task_coordinator_claim=_planning_claim(task.id),
        )
        assert admitted.id == task.id
    else:
        with pytest.raises(WorkspaceAutonomyPausedError):
            await _lock_task_planning_origin(
                db_session, task.id, task_coordinator_claim=_planning_claim(task.id),
            )
        admitted, _ = await _lock_task_planning_origin(
            db_session, task.id, manual_user_id="operator",
            task_coordinator_claim=_planning_claim(task.id),
        )
        assert admitted.id == task.id

    workspace.deleted_at = datetime.now(timezone.utc)
    await db_session.flush()
    with pytest.raises(PlannerError, match="unavailable"):
        await _lock_task_planning_origin(
            db_session, task.id, manual_user_id="operator",
            task_coordinator_claim=_planning_claim(task.id),
        )


@pytest.mark.asyncio
async def test_dispatcher_paused_auto_rows_do_not_starve_manual_lease(db_session, monkeypatch):
    from tests.task_manual_helpers import allow_step_approval, task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    scenario.workspace.status = "paused"
    approval = allow_step_approval(monkeypatch)
    for index in range(6):
        task = Task(
            id=generate_ulid(), entity_id=scenario.entity_id,
            workspace_id=scenario.workspace.id, title=f"Automatic {index}",
            status="in_progress", creator_id=scenario.user.id,
            details={"strategist_review_id": "automatic-review"},
        )
        plan = ExecutionPlan(
            id=generate_ulid(), entity_id=scenario.entity_id, workspace_id=scenario.workspace.id,
            task_id=task.id, status="running", plan_dag={"steps": []},
        )
        step = ExecutionStep(
            id=generate_ulid(), plan_id=plan.id, entity_id=scenario.entity_id,
            workspace_id=scenario.workspace.id, service_key="reader", step_key=f"auto_{index}",
            kind="subagent", params=dict(scenario.step.params), step_status="pending",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add_all([task, plan, step])
    await db_session.flush()

    leased = await Dispatcher().checkout_steps_for_worker(db_session, scenario.worker, max_n=1)

    assert len(leased) == 1
    assert leased[0][1].id == scenario.step.id
    assert scenario.workspace.status == "paused"
    approval.assert_awaited_once()
    auto_statuses = (await db_session.execute(select(ExecutionStep.step_status).where(
        ExecutionStep.workspace_id == scenario.workspace.id,
        ExecutionStep.id != scenario.step.id,
    ))).scalars().all()
    assert auto_statuses == ["pending"] * 6


@pytest.mark.asyncio
async def test_manual_plan_receipt_and_sql_admission_match(db_session):
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=generate_ulid(), workspace_id=generate_ulid(),
        status="draft", plan_dag={"steps": []}, dispatcher_state={"cycle_count": 3},
    )
    db_session.add(plan)
    await db_session.flush()
    query = select(ExecutionPlan.id).where(ExecutionPlan.id == plan.id, plan_manual_execution_predicate())
    assert (await db_session.execute(query)).scalar_one_or_none() is None
    record_manual_plan_execution(plan, user_id="operator", action="plan_approval")
    await db_session.flush()
    assert (await db_session.execute(query)).scalar_one() == plan.id
    assert plan.dispatcher_state["cycle_count"] == 3


@pytest.mark.asyncio
async def test_manual_plan_on_paused_workspace_still_checks_setup(db_session):
    from packages.core.plans.executor import PlanExecutor

    workspace = Workspace(
        id=generate_ulid(), entity_id=generate_ulid(), name="Paused with setup", status="paused",
        settings={"_blueprint": {"blueprint_id": "builtin:blocked", "live_setup_requirements": [
            {"kind": "note", "detail": "Complete setup first.", "payload": {}, "blocking": True},
        ]}},
    )
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        status="draft", plan_dag={"steps": []},
    )
    record_manual_plan_execution(plan, user_id="operator", action="plan_create")
    db_session.add_all([workspace, plan])
    await db_session.flush()

    @asynccontextmanager
    async def factory():
        yield db_session

    result = await PlanExecutor(session_factory=factory).run_cycle(plan.id)
    assert result["status"] == "blocked_setup"
    assert plan.status == "draft"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_kind", ["user", "system"])
async def test_paused_proposal_user_approval_authorizes_only_its_tasks(db_session, monkeypatch, actor_kind):
    from packages.core.strategist.service import approve_proposal
    from tests.test_proposal_items import _seed_workspace

    workspace = await _seed_workspace(db_session)
    workspace.status = "paused"
    review_id = generate_ulid()
    task = Task(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        title="Explicitly approved task", status="proposed", task_type="ai_generated",
        owner_service_key="ops", details={"strategist_review_id": review_id},
    )
    db_session.add(task)
    await db_session.commit()
    dispatch = Mock()
    monkeypatch.setattr("packages.core.tasks.ai_tasks.plan_and_run_task.delay", dispatch)

    from tests.approval_settings_test_utils import initialize_test_task_proposal
    from packages.core.proposals.approval_guard import ProposalApprovalBlocked

    await initialize_test_task_proposal(db_session, workspace, review_id=review_id)
    if actor_kind == "system":
        with pytest.raises(ProposalApprovalBlocked, match="human decision"):
            await approve_proposal(db_session, entity_id=workspace.entity_id, review_id=review_id,
                actor_kind="system")
        assert task.status == "proposed" and not task_has_manual_execution(task)
        assert workspace.status == "paused"
        dispatch.assert_not_called()
        return
    moved = await approve_proposal(
        db_session, entity_id=workspace.entity_id, review_id=review_id,
        actor_kind=actor_kind, actor_id=workspace.entity_id if actor_kind == "user" else None,
    )
    await db_session.flush()

    assert moved == [task.id]
    assert workspace.status == "paused"
    assert task.details["strategist_review_id"] == review_id
    assert task_has_manual_execution(task) is (actor_kind == "user")
    if actor_kind == "user":
        admitted, _ = await _lock_task_planning_origin(
            db_session, task.id, task_coordinator_claim=_planning_claim(task.id),
        )
        assert admitted.id == task.id
    else:
        with pytest.raises(WorkspaceAutonomyPausedError):
            await _lock_task_planning_origin(
                db_session, task.id, task_coordinator_claim=_planning_claim(task.id),
            )


@pytest.mark.asyncio
async def test_task_api_cannot_write_manual_execution_receipt(client):
    from tests.test_plan_retries import _auth
    import packages.core.database as db_module

    headers, _ = await _auth(client, "manualreceipt")
    response = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Client cannot forge runtime lineage",
        "details": {"_manual_execution": {"user_id": "another-user", "action": "forged"}},
    })
    assert response.status_code == 201, response.text
    async with db_module.async_session() as db:
        task = await db.get(Task, response.json()["id"])
        assert "_manual_execution" not in task.details
        task.details = {"scheduled_job_id": "automatic-job"}
        await db.commit()
        task_id = task.id
    updated = await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={
        "details": {
            "scheduled_job_id": None,
            "_manual_execution": {"user_id": "another-user", "action": "forged"},
        },
    })
    assert updated.status_code == 200, updated.text
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        assert task.details["scheduled_job_id"] == "automatic-job"
        assert "_manual_execution" not in task.details
        assert not task_has_manual_execution(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["approve", "retry"])
async def test_paused_workspace_manual_plan_action_persists_execution_lineage(client, monkeypatch, operation):
    import packages.core.database as db_module
    from tests.test_plan_retries import (
        _assert_exact_pending_plan_cycle_wake,
        _auth,
        _capture_plan_cycle_hints,
        _create_plan,
    )

    headers, entity_id = await _auth(client, f"pausedplan{operation}")
    plan_id, failed_step_id, _, task_id = await _create_plan(
        entity_id, status="pending_approval" if operation == "approve" else "failed", with_task=True,
    )
    async with db_module.async_session() as db:
        workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Paused manual action", status="paused")
        agent = Agent(
            id=generate_ulid(), entity_id=entity_id, name="Plan Agent", status="active",
        )
        subscription = AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
            agent_id=agent.id, service_key="content", status="active",
        )
        db.add_all([workspace, agent, subscription])
        plan = await db.get(ExecutionPlan, plan_id)
        task = await db.get(Task, task_id)
        step = await db.get(ExecutionStep, failed_step_id)
        plan.workspace_id = task.workspace_id = step.workspace_id = workspace.id
        task.owner_service_key = subscription.service_key
        task.owner_subscription_id = subscription.id
        task.details = {"strategist_review_id": "automatic-review"}
        if operation == "approve":
            task.status = "waiting_on_customer"
            plan.approval_required = True
        await db.commit()
        workspace_id = workspace.id
    plan_hints = _capture_plan_cycle_hints(monkeypatch)

    action_path = "approve" if operation == "approve" else "retry-failed-steps"
    response = await client.post(f"/api/v1/plans/{plan_id}/{action_path}", headers=headers, json={})

    assert response.status_code == 200, response.text
    assert [hint["plan_id"] for hint in plan_hints] == [plan_id]
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        plan = await db.get(ExecutionPlan, plan_id)
        workspace = await db.get(Workspace, workspace_id)
        await _assert_exact_pending_plan_cycle_wake(db, plan_id, plan_hints[0])
        assert task_has_manual_execution(task)
        assert plan.dispatcher_state["_manual_execution"]["user_id"]
        assert task.details["strategist_review_id"] == "automatic-review"
        assert workspace.status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_paused_task_runner_preserves_exact_runtime_approval_resume_gate(db_session, manual):
    from packages.core.ai.task_runner import TaskRunner
    from packages.core.services.task_runtime_approvals import TASK_RUNTIME_HITL_DETAIL_KEY

    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Paused Agent", status="paused")
    task = Task(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        title="Resume exact approved action", status="pending", creator_id=generate_ulid(),
        details={"scheduled_job_id": "job", TASK_RUNTIME_HITL_DETAIL_KEY: {"state": "resume_queued"}},
    )
    if manual:
        record_manual_task_execution(task, user_id="operator", action="runtime_approval_resume")
    db_session.add_all([workspace, task])
    await db_session.flush()

    @asynccontextmanager
    async def factory():
        yield db_session

    result = await TaskRunner(session_factory=factory).run(task.id)
    assert result["reason"] == (
        "runtime_approval_resume_identity_required" if manual else "workspace_autonomy_paused"
    )
    assert task.status == "pending"
