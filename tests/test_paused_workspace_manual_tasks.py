"""Workspace automatic-running pause preserves explicitly requested work."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
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

    from tests.approval_settings_test_utils import initialize_test_task_proposal
    from packages.core.proposals.approval_guard import ProposalApprovalBlocked

    await initialize_test_task_proposal(db_session, workspace, review_id=review_id)
    if actor_kind == "system":
        with pytest.raises(ProposalApprovalBlocked, match="human decision"):
            await approve_proposal(db_session, entity_id=workspace.entity_id, review_id=review_id,
                actor_kind="system")
        assert task.status == "proposed" and not task_has_manual_execution(task)
        assert workspace.status == "paused"
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


