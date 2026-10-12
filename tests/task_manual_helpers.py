"""Minimal real Task/Plan/Worker rows for manual-execution admission tests."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from packages.core.constants.approvals import ApprovalOutcome
from packages.core.governance.approvals import ApprovalDecision
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.user import User, UserMembership
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceStaff


async def task_manual_scenario(db):
    entity_id = generate_ulid()
    user = User(
        id=generate_ulid(), entity_id=entity_id,
        email=f"manual-task-{entity_id}@example.com", password_hash="test-only",
        role="owner", status="active",
    )
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id,
        name="Manual execution admission", status="active",
    )
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id,
        name="Task reader", status="active", config={},
    )
    subscription = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, agent_id=agent.id,
        workspace_id=workspace.id, name="Reader", service_key="reader",
        status="active",
    )
    worker = Worker(
        id=generate_ulid(), entity_id=entity_id, kind="internal",
        display_name="Manual task worker", status="active",
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True,
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Read supplied evidence", status="in_progress",
        creator_id=user.id, owner_service_key="reader", delegate_service_keys=[],
    )
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        task_id=task.id, status="running", execution_mode="live",
        approval_required=False, plan_dag={"steps": []},
    )
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=entity_id,
        workspace_id=workspace.id, service_key=subscription.service_key,
        step_key="read_evidence", kind="subagent", params={"prompt": "Read the supplied evidence."},
        depends_on=[], step_status="pending", risk_level="low",
        requires_approval=False, attempt_count=0, max_attempts=3,
    )
    db.add_all([
        user, workspace, agent, subscription, worker, task, plan, step,
        UserMembership(user_id=user.id, entity_id=entity_id, role="owner", status="active"),
        WorkspaceStaff(workspace_id=workspace.id, user_id=user.id, role="owner", status="active"),
        SubscriptionWorker(
            subscription_id=subscription.id, worker_id=worker.id,
            priority=100, is_preferred=True,
        ),
    ])
    await db.flush()
    return SimpleNamespace(
        entity_id=entity_id, user=user, workspace=workspace, agent=agent,
        subscription=subscription, worker=worker, task=task, plan=plan, step=step,
    )


def allow_step_approval(monkeypatch):
    approval = AsyncMock(return_value=ApprovalDecision(outcome=ApprovalOutcome.ALLOW))
    monkeypatch.setattr("packages.core.governance.approvals.resolve_approval", approval)
    return approval
