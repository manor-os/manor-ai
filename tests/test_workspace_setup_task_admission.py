"""Setup can use the normal Task runtime only through its durable job receipt."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.core.constants.blueprints import installed_blueprint_job_id
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.plans.executor import PlanExecutor
from packages.core.plans.planner import PlannerError, _assert_workspace_setup_allows_planning
from packages.core.services.blueprint_startup_service import blueprint_startup_contract_fingerprint
from packages.core.services.task_execution import resolve_task_execution
from packages.core.services.workspace_readiness import (
    evaluate_workspace_automatic_setup_status,
    task_is_authorized_workspace_setup,
)


async def _setup(db):
    entity_id, workspace_id, agent_id = generate_ulid(), generate_ulid(), generate_ulid()
    contract = {
        "checks": [
            {
                "key": "identity",
                "kind": "workspace_identity_assets",
                "setup_job_id": "prepare-assets",
                "asset_key": "character",
            }
        ]
    }
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Setup test",
        status="active",
        settings={
            "blocking_setup": contract,
            "_blueprint": {
                "blueprint_id": "builtin:setup-test",
                "live_setup_requirements": [],
                "portable_setting_keys": ["blocking_setup"],
                "runtime_contract_fingerprints": {"blocking_setup": blueprint_startup_contract_fingerprint(contract)},
            },
        },
    )
    sub = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        service_key="production",
        status="active",
    )
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=installed_blueprint_job_id("prepare-assets", workspace_id),
        entity_id=entity_id,
        workspace_id=workspace_id,
        job_type="manual",
        enabled=True,
        execution_type="agent",
        agent_id=agent_id,
        revision=1,
        execution_target={"agent_subscription_id": sub.id, "service_key": sub.service_key},
    )
    run = ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="running")
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Prepare shared assets",
        status="in_progress",
        agent_id=agent_id,
        owner_subscription_id=sub.id,
        details={"scheduled_job_id": job.job_id, "scheduled_run_id": run.id},
    )
    run.result = {
        "scheduled_child_kind": "agent_task",
        "scheduled_child_id": task.id,
        "dispatch": {
            "kind": "agent_task",
            "args": [task.id, agent_id],
            "kwargs": {"scheduled_job_id": job.job_id, "scheduled_run_id": run.id},
        },
        "dispatch_ledger": {"revision": job.revision},
    }
    db.add_all([workspace, sub, job, run, task])
    await db.flush()
    return workspace, sub, job, run, task


@pytest.mark.asyncio
async def test_setup_child_keeps_coordinated_runtime_and_passes_planner_gate(db_session):
    workspace, _sub, _job, _run, task = await _setup(db_session)
    route = await resolve_task_execution(db_session, task)
    assert route.runtime_kind.value == "coordinated_plan"
    await _assert_workspace_setup_allows_planning(db_session, SimpleNamespace(workspace=workspace), task)


@pytest.mark.asyncio
async def test_copied_job_markers_do_not_authorize_an_ordinary_task(db_session):
    workspace, _sub, _job, _run, setup_task = await _setup(db_session)
    ordinary = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Ordinary work",
        status="in_progress",
        agent_id=setup_task.agent_id,
        owner_subscription_id=setup_task.owner_subscription_id,
        details=deepcopy(setup_task.details),
    )
    db_session.add(ordinary)
    await db_session.flush()
    assert not await task_is_authorized_workspace_setup(db_session, ordinary)
    with pytest.raises(PlannerError, match="setup is incomplete"):
        await _assert_workspace_setup_allows_planning(db_session, SimpleNamespace(workspace=workspace), ordinary)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation",
    [
        "job_disabled",
        "revision_changed",
        "run_cancelled",
        "workspace_paused",
        "workspace_deleted",
        "contract_modified",
        "subscription_paused",
        "foreign_job",
        "foreign_agent",
        "forged_child",
        "malformed_dispatch",
        "malformed_kwargs",
        "malformed_ledger",
    ],
)
async def test_setup_authority_is_revalidated_on_every_gate(db_session, revocation):
    workspace, sub, job, run, task = await _setup(db_session)
    assert await task_is_authorized_workspace_setup(db_session, task)
    if revocation == "job_disabled":
        job.enabled = False
    elif revocation == "revision_changed":
        job.revision += 1
    elif revocation == "run_cancelled":
        run.status = "cancelled"
    elif revocation == "workspace_paused":
        workspace.status = "paused"
    elif revocation == "workspace_deleted":
        from datetime import datetime, timezone

        workspace.deleted_at = datetime.now(timezone.utc)
    elif revocation == "contract_modified":
        changed = deepcopy(workspace.settings)
        changed["blocking_setup"]["checks"][0]["setup_job_id"] = "normal-work"
        workspace.settings = changed
    elif revocation == "subscription_paused":
        sub.status = "paused"
    elif revocation == "foreign_job":
        job.workspace_id = generate_ulid()
    elif revocation == "foreign_agent":
        task.agent_id = generate_ulid()
    elif revocation == "forged_child":
        run.result = {**run.result, "scheduled_child_id": generate_ulid()}
    elif revocation == "malformed_dispatch":
        run.result = {**run.result, "dispatch": "invalid"}
    elif revocation == "malformed_kwargs":
        run.result = {**run.result, "dispatch": {**run.result["dispatch"], "kwargs": "invalid"}}
    elif revocation == "malformed_ledger":
        run.result = {**run.result, "dispatch_ledger": "invalid"}
    await db_session.flush()
    assert not await task_is_authorized_workspace_setup(db_session, task)
    with pytest.raises(PlannerError):
        await _assert_workspace_setup_allows_planning(db_session, SimpleNamespace(workspace=workspace), task)


@pytest.mark.asyncio
async def test_automatic_setup_uses_actual_run_and_scoped_child_when_job_summary_is_empty(db_session):
    workspace, _sub, job, run, task = await _setup(db_session)
    assert job.last_status is None
    assert await evaluate_workspace_automatic_setup_status(
        db_session, workspace, [{"setup_job_id": "prepare-assets"}]
    ) == ("running", task.id)
    run.status = "error"
    await db_session.flush()
    assert await evaluate_workspace_automatic_setup_status(
        db_session, workspace, [{"setup_job_id": "prepare-assets"}]
    ) == ("retrying", task.id)
    job.enabled = False
    await db_session.flush()
    assert await evaluate_workspace_automatic_setup_status(
        db_session, workspace, [{"setup_job_id": "prepare-assets"}]
    ) == ("failed", task.id)


@pytest.mark.asyncio
async def test_setup_executor_reaches_normal_billing_gate_and_revocation_stops_it(db_session, monkeypatch):
    from packages.core.plans import executor

    workspace, _sub, job, _run, task = await _setup(db_session)
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        task_id=task.id,
        status="draft",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
    )
    db_session.add(plan)
    await db_session.flush()

    class BillingGateReached(Exception):
        pass

    async def billing(*args, **kwargs):
        raise BillingGateReached

    monkeypatch.setattr(executor, "runtime_ensure_task_billing_context", billing)

    @asynccontextmanager
    async def sessions():
        yield db_session

    runner = PlanExecutor(session_factory=sessions)
    with pytest.raises(BillingGateReached):
        await runner.run_cycle(plan.id)
    job.enabled = False
    await db_session.flush()
    result = await runner.run_cycle(plan.id)
    assert result["status"] == "blocked_setup"
    assert plan.status == "draft"


@pytest.mark.asyncio
async def test_setup_dispatcher_reaches_budget_gate_and_still_blocks_other_tasks(db_session, monkeypatch):
    from decimal import Decimal
    from packages.core import budget
    from packages.core.dispatcher.service import Dispatcher
    from packages.core.models.execution import ExecutionStep
    from packages.core.models.worker import SubscriptionWorker, Worker
    from packages.core.models.workspace import Agent

    workspace, sub, job, _run, task = await _setup(db_session)
    worker = Worker(
        id=generate_ulid(), entity_id=workspace.entity_id, kind="internal",
        display_name="Setup worker", status="active", monthly_spent_usd=Decimal("0"),
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
    )
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        task_id=task.id, status="running", execution_mode="live", approval_required=False,
        plan_dag={"steps": []},
    )
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=workspace.entity_id,
        workspace_id=workspace.id, step_key="prepare_assets", kind="subagent",
        service_key=sub.service_key, params={"prompt": "Prepare the shared assets."},
        depends_on=[], step_status="pending", risk_level="low", requires_approval=False,
        attempt_count=0, max_attempts=3,
    )
    ordinary = Task(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        title="Ordinary work", status="in_progress", agent_id=task.agent_id,
        owner_subscription_id=sub.id, details=deepcopy(task.details),
    )
    db_session.add_all([
        worker, plan, step, ordinary,
        Agent(id=task.agent_id, entity_id=workspace.entity_id, name="Producer",
              slug=f"producer-{task.agent_id}", status="active"),
        SubscriptionWorker(subscription_id=sub.id, worker_id=worker.id,
                           priority=100, is_preferred=True),
    ])
    await db_session.flush()

    class BudgetGateReached(Exception):
        pass

    async def check_budget(*args, **kwargs):
        raise BudgetGateReached

    monkeypatch.setattr(budget, "check_workspace_budget", check_budget)
    dispatcher = Dispatcher()
    with pytest.raises(BudgetGateReached):
        await dispatcher.checkout_steps_for_worker(db_session, worker, plan_id=plan.id)
    plan.task_id = ordinary.id
    await db_session.flush()
    assert await dispatcher.checkout_steps_for_worker(db_session, worker, plan_id=plan.id) == []
    plan.task_id = task.id
    job.enabled = False
    await db_session.flush()
    assert await dispatcher.checkout_steps_for_worker(db_session, worker, plan_id=plan.id) == []
    assert step.step_status == "pending"
    assert step.current_lease_id is None
