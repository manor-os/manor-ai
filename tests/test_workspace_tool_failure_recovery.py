"""A failed file-producing Task must expose its cause and retry only unfinished work."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.constants.execution import ExecutionPlanStatus
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Message, Task
from packages.core.models.workspace import Workspace
from packages.core.plans.executor import PlanExecutor
from packages.core.services.task_retry_service import prepare_task_retry


@pytest.mark.asyncio
async def test_file_task_failure_projects_real_blocker_and_retry_preserves_completed_step(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="AI SDE preparation")
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Create baseline diagnostic packet", status="in_progress",
        expected_output={"deliverables": [{"kind": "file", "name": "baseline_diagnostic_packet"}]},
    )
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        task_id=task.id, status="running", plan_dag={},
    )
    completed = ExecutionStep(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        plan_id=plan.id, step_key="extract_curriculum_scope", kind="subagent",
        step_status="done", result={"scope_summary": "Seven modules, 90–120 minutes."},
    )
    reason = "search_tools was blocked by permission.agent_tool_binding"
    failure = {"reason": reason, "requires_human": True, "retryable": False, "blockers": ["search_tools"]}
    failed = ExecutionStep(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        plan_id=plan.id, step_key="create_diagnostic_packet", kind="subagent",
        step_status="failed", attempt_count=1,
        result={"status": "failed", "summary": reason, "failure": failure},
        error={"type": "StepResultFailed", "message": reason, "failure": failure},
    )
    db_session.add_all([workspace, task, plan, completed, failed])
    await db_session.commit()

    await PlanExecutor._finalize(db_session, plan, ExecutionPlanStatus.FAILED)
    await db_session.flush()
    assert task.status == "waiting_on_customer"
    card = (await db_session.execute(select(Message).where(
        Message.pending_action["task_id"].as_string() == task.id,
        Message.pending_action["kind"].as_string() == "task_recovery",
    ))).scalar_one()
    assert reason in card.pending_action["payload"]["why"]
    assert "This workspace task needs a saved" not in card.pending_action["payload"]["why"]
    assert reason in card.content
    assert not task.actual_output.get("files")
    await db_session.commit()

    retried = await prepare_task_retry(
        db_session, task=task, user_id=generate_ulid(), user_label="Reviewer",
    )
    assert retried.plan_id == plan.id
    assert completed.step_status == "done"
    assert completed.result["scope_summary"] == "Seven modules, 90–120 minutes."
    assert failed.step_status == "pending"
    assert failed.attempt_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["same_plan", "other_plan", "other_workspace", "other_entity", "resolved", "specific_error"])
async def test_old_recovery_card_projection_is_scoped_and_does_not_rewrite_approval(db_session, variant):
    from datetime import datetime, timezone
    from apps.api.routers.workspace_chat import _hydrate_messages
    from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl

    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Recovery")
    plan_id = generate_ulid()
    reason = "search_tools: permission.agent_tool_binding"
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Diagnostic packet", status="waiting_on_customer",
        actual_output={"plan_id": plan_id, "supervisor_verdict": "needs_human", "supervisor_evidence": reason},
    )
    db_session.add_all([workspace, task])
    await db_session.flush()
    old_reason = "This workspace task needs a saved file/media/document deliverable, but no saved file link or path was recorded."
    if variant == "specific_error":
        old_reason = "Replacement planning could not be queued."
    card = await ensure_task_recovery_hitl(
        db_session, task, plan_id=generate_ulid() if variant == "other_plan" else plan_id,
        prompt="The run stopped.", issue=old_reason,
    )
    assert card is not None
    if variant == "other_workspace":
        task.workspace_id = generate_ulid()
    elif variant == "other_entity":
        task.entity_id = generate_ulid()
    elif variant == "resolved":
        card.resolved_at = datetime.now(timezone.utc)
    await db_session.flush()

    [response] = await _hydrate_messages(
        db_session, [card], entity_id=entity_id, workspace_id=workspace.id,
    )
    assert response.pending_action["payload"]["why"] == (reason if variant == "same_plan" else old_reason)
    assert card.pending_action["payload"]["why"] == old_reason
    await db_session.refresh(card)
    assert card.pending_action["payload"]["why"] == old_reason
