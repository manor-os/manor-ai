from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.blueprints.launch_review import (
    ConnectionIssue,
    GoalSnapshot,
    HumanLoad,
    OperationsSection,
    RunBreakdown,
    RunOutcomes,
    build_launch_readiness,
    build_value_evidence,
)
from packages.core.blueprints.report import simulate_report
from packages.core.constants.execution import COORDINATOR_PARAM_KEY
from packages.core.governance.approval_policy_service import (
    account_defaults,
    initialize_workspace_approval,
)
from packages.core.models.base import generate_ulid
from packages.core.models.billing import CreditUsageLog
from packages.core.models.execution import ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.user import Entity
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.models.workspace import Agent, Workspace
from packages.core.services.credit_service import credits_to_usd


def _outcomes(succeeded: int = 0, failed: int = 0) -> RunOutcomes:
    finished = succeeded + failed
    return RunOutcomes(
        total=finished, succeeded=succeeded, failed=failed,
        success_rate=succeeded / finished if finished else None,
    )


def _operations(**surfaces: RunOutcomes) -> OperationsSection:
    return OperationsSection(
        tasks=surfaces.get("tasks", RunOutcomes()),
        agents=surfaces.get("agents", RunOutcomes()),
        workflows=surfaces.get("workflows", RunOutcomes()),
        automations=surfaces.get("automations", RunOutcomes()),
    )


# 95% overall hides one workflow that fails most of its runs.
_ONE_BROKEN_WORKFLOW = RunOutcomes(
    total=100, succeeded=95, failed=5, success_rate=0.95,
    below_target=[RunBreakdown(
        key="wf-launch", label="Launch campaign", total=19, succeeded=7, failed=12,
        success_rate=7 / 19, judged=19,
    )],
)
_DISCONNECTED_REQUIRED = [ConnectionIssue(label="X", reason="Health check failed", required=True)]
_DISCONNECTED_OPTIONAL = [ConnectionIssue(label="Slack", reason="Not connected", required=False)]


@pytest.mark.parametrize(
    ("unmet_setup", "operations", "budget", "connections", "verdict"),
    [
        pytest.param(["no active 'email' channel"], _operations(tasks=_outcomes(10)), 100.0, [], "blocked", id="setup-gap-blocks"),
        pytest.param([], _operations(), 100.0, [], "insufficient_data", id="no-finished-work"),
        pytest.param([], _operations(workflows=_outcomes(1)), 100.0, [], "insufficient_data", id="one-run-is-not-evidence"),
        pytest.param([], _operations(workflows=_outcomes(6, 4)), 100.0, [], "at_risk", id="low-success-rate"),
        pytest.param([], _operations(tasks=_outcomes(10)), 1.0, [], "at_risk", id="over-budget"),
        # A required integration that is down stops normal work like missing setup.
        pytest.param([], _operations(tasks=_outcomes(10)), 100.0, _DISCONNECTED_REQUIRED, "blocked", id="required-integration-down"),
        pytest.param([], _operations(workflows=_ONE_BROKEN_WORKFLOW), 100.0, [], "at_risk", id="one-workflow-broken"),
        # Warnings (8/10 is between the fail and pass lines, no budget cap, an
        # optional integration missing) stay visible but do not change an
        # otherwise launchable verdict.
        pytest.param([], _operations(tasks=_outcomes(8, 2)), None, _DISCONNECTED_OPTIONAL, "ready", id="warnings-only"),
    ],
)
def test_launch_verdict_separates_blockers_missing_evidence_and_risks(
    unmet_setup, operations, budget, connections, verdict,
) -> None:
    readiness = build_launch_readiness(
        unmet_setup=unmet_setup,
        operations=operations,
        observation_days=3.0,
        projected_monthly_usd=50.0,
        monthly_budget_usd=budget,
        connections=connections,
    )
    assert readiness.verdict == verdict


def _goal(pace: str | None, measured: bool = True) -> GoalSnapshot:
    return GoalSnapshot(title=f"Goal {pace}", pace_status=pace, measured=measured)


@pytest.mark.parametrize(
    ("goals", "human", "verdict"),
    [
        pytest.param([], HumanLoad(), "unproven", id="no-goals-no-reviews"),
        pytest.param([_goal(None, measured=False)], HumanLoad(accepted=2), "unproven", id="unmeasured-goal-too-few-reviews"),
        pytest.param([_goal("on_track"), _goal("at_risk")], HumanLoad(accepted=9, rejected=1), "below_expectations", id="goal-at-risk"),
        pytest.param([_goal("on_track")], HumanLoad(accepted=4, rejected=6), "below_expectations", id="output-mostly-rejected"),
        pytest.param([_goal("behind"), _goal("ahead")], HumanLoad(accepted=9, rejected=1), "concerns", id="goal-behind"),
        # A missing signal is not a concern when the other one is good.
        pytest.param([_goal("achieved")], HumanLoad(), "on_track", id="goal-on-pace-no-reviews"),
        pytest.param([_goal("on_track")], HumanLoad(accepted=19, rejected=1), "on_track", id="on-pace-and-accepted"),
    ],
)
def test_value_evidence_separates_missing_signal_from_bad_signal(goals, human, verdict) -> None:
    assert build_value_evidence(goals, human).verdict == verdict


def test_budget_is_not_judged_from_less_than_a_day_of_spend() -> None:
    readiness = build_launch_readiness(
        unmet_setup=[], operations=_operations(tasks=_outcomes(10)),
        observation_days=0.1, projected_monthly_usd=5000.0, monthly_budget_usd=50.0,
    )
    budget = next(check for check in readiness.checks if check.key == "budget")
    assert (budget.status, budget.reason) == ("warn", "too_early_to_project")
    assert readiness.verdict == "ready"


@pytest.mark.asyncio
async def test_simulation_report_measures_each_surface_inside_the_window(db_session) -> None:
    now = datetime.now(timezone.utc)
    installed_at = now - timedelta(days=3)
    before_window = installed_at - timedelta(days=1)
    workspace = Workspace(
        id=generate_ulid(), entity_id=generate_ulid(), name="Launch review",
        status="active", monthly_budget_usd=Decimal("10000"),
        settings={"sandbox": True, "_blueprint": {
            "installed_at": installed_at.isoformat(), "live_setup_requirements": [],
        }},
    )
    other_workspace_id = generate_ulid()
    agent = Agent(id=generate_ulid(), entity_id=workspace.entity_id, name="Writer")
    workflow = WorkflowDefinition(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        name="Weekly digest",
    )
    db_session.add_all([
        Entity(id=workspace.entity_id, name="Launch review entity",
               settings={"approval_defaults": account_defaults()}),
        workspace, agent, workflow,
    ])
    await db_session.flush()
    await initialize_workspace_approval(db_session, workspace)

    def task(status: str, **overrides) -> Task:
        values = {
            "id": generate_ulid(), "entity_id": workspace.entity_id,
            "workspace_id": workspace.id, "title": status, "status": status,
            "details": {}, "created_at": now - timedelta(days=1),
        }
        values.update(overrides)
        return Task(**values)

    # Task Coordinator V3 parks a failed execution as ``blocked``.
    parked = task("blocked")
    # The Plan/Step engine retirement blocked in-flight Tasks itself.
    migrated = task("blocked")
    db_session.add_all([
        migrated,
        task("completed", started_at=now - timedelta(hours=3), completed_at=now - timedelta(hours=1)),
        task("failed"),
        parked,
        task("in_progress", status_changed_at=now - timedelta(hours=72)),
        task("proposed"),
        task("completed", created_at=before_window),
        task("completed", workspace_id=other_workspace_id),
    ])
    await db_session.flush()
    db_session.add_all([
        TaskLog(task_id=parked.id, log_type="ai_execution_failed", content="old reason",
                created_at=now - timedelta(hours=5)),
        TaskLog(task_id=parked.id, log_type="ai_execution_failed", content="Search tool quota exhausted",
                created_at=now - timedelta(hours=4)),
        TaskLog(task_id=migrated.id, log_type="ai_execution_failed", content="Stopped by the runtime migration",
                meta={"error": "runtime_migrated", "reason": "plan_dag_retired"}, created_at=now - timedelta(hours=4)),
    ])

    plan_id = generate_ulid()

    def step(status: str, **overrides) -> ExecutionStep:
        values = {
            "id": generate_ulid(), "plan_id": plan_id, "entity_id": workspace.entity_id,
            "workspace_id": workspace.id, "step_key": generate_ulid(), "kind": "subagent",
            "resolved_agent_id": agent.id, "step_status": status,
            "started_at": now - timedelta(hours=2),
        }
        values.update(overrides)
        return ExecutionStep(**values)

    db_session.add_all([
        step("done", finished_at=now - timedelta(hours=1), attempt_count=2),
        step("failed", error={"type": "ToolError", "message": "rate limited"}),
        step("failed", error={"type": "ToolError", "message": "rate limited"}),
        step("skipped"),
        step("done", params={COORDINATOR_PARAM_KEY: True}),
        step("done", started_at=before_window),
    ])

    # The Task agent loop records agent work as delegated child runs.
    editor_id = generate_ulid()

    def child(phase: str, outcome_status: str | None, **overrides) -> TaskAgentChildRun:
        root_run_id = generate_ulid()
        values = {
            "id": generate_ulid(), "task_id": generate_ulid(),
            "entity_id": workspace.entity_id, "workspace_id": workspace.id,
            "root_run_id": root_run_id, "parent_ref": root_run_id,
            "parent_tool_call_id": "call-1", "item_index": 0, "batch_size": 1, "depth": 1,
            "agent_subscription_id": generate_ulid(), "agent_id": editor_id,
            "agent_label": "Editor", "prompt": "Edit the draft.", "answer_max_chars": 4000,
            "lifecycle_generation": "lifecycle-1", "phase": phase,
            "outcome_status": outcome_status, "state_version": 0,
            "created_at": now - timedelta(hours=3),
        }
        values.update(overrides)
        return TaskAgentChildRun(**values)

    db_session.add_all([
        child("completed", "completed", terminal_at=now - timedelta(hours=2)),
        child("failed", "failed", result={"status": "failed", "message": "Browser session expired"}),
        child("completed", "completed", created_at=before_window),
    ])

    def workflow_run(status: str, **overrides) -> WorkflowRun:
        values = {
            "id": generate_ulid(), "workflow_id": workflow.id, "entity_id": workspace.entity_id,
            "workspace_id": workspace.id, "status": status, "created_at": now - timedelta(days=1),
        }
        values.update(overrides)
        return WorkflowRun(**values)

    db_session.add_all([
        workflow_run("completed"),
        workflow_run("failed", error="Publisher returned 500\nTraceback ..."),
        workflow_run("running"),
        workflow_run(
            "paused", created_at=now - timedelta(hours=60), started_at=now - timedelta(hours=60),
        ),
        workflow_run("completed", workspace_id=other_workspace_id),
    ])

    def approval(status: str, created_at: datetime, **overrides) -> HitlRequest:
        values = {
            "id": generate_ulid(), "entity_id": workspace.entity_id, "workspace_id": workspace.id,
            "origin_kind": "plan_step", "dedup_key": generate_ulid(), "status": status,
            "created_at": created_at,
        }
        values.update(overrides)
        return HitlRequest(**values)

    decided_at = now - timedelta(hours=10)
    db_session.add_all([
        approval("consumed", decided_at - timedelta(minutes=30),
                 decided_at=decided_at, decided_by_user_id=generate_ulid()),
        approval("denied", decided_at - timedelta(minutes=90),
                 decided_at=decided_at, decided_by_user_id=generate_ulid()),
        approval("pending", now - timedelta(hours=50)),
    ])

    healthy_job = ScheduledJob(
        id=generate_ulid(), job_id=f"job-{generate_ulid()}", entity_id=workspace.entity_id,
        workspace_id=workspace.id, name="Daily scan", enabled=True, consecutive_errors=0,
    )
    paused_job = ScheduledJob(
        id=generate_ulid(), job_id=f"job-{generate_ulid()}", entity_id=workspace.entity_id,
        workspace_id=workspace.id, name="Hourly sync", enabled=False, consecutive_errors=3,
    )
    db_session.add_all([healthy_job, paused_job])
    db_session.add_all([
        ScheduledJobRun(id=generate_ulid(), job_id=healthy_job.job_id, status="success", duration_ms=2000),
        ScheduledJobRun(id=generate_ulid(), job_id=healthy_job.job_id, status="skipped"),
        *[
            ScheduledJobRun(id=generate_ulid(), job_id=paused_job.job_id, status="error", error="timeout")
            for _ in range(5)
        ],
    ])

    db_session.add_all([
        CreditUsageLog(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            total_credit=900, created_at=now - timedelta(days=1),
        ),
        CreditUsageLog(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=other_workspace_id,
            total_credit=5000, created_at=now - timedelta(days=1),
        ),
    ])
    await db_session.flush()

    report = await simulate_report(db_session, workspace.id)
    ops = report.operations

    assert (ops.tasks.total, ops.tasks.succeeded, ops.tasks.failed, ops.tasks.cancelled, ops.tasks.in_flight) == (5, 1, 2, 1, 1)
    assert {(e.message, e.count) for e in ops.tasks.top_errors} == {
        ("Search tool quota exhausted", 1), ("Task failed", 1),
    }
    assert ops.tasks.avg_duration_seconds == pytest.approx(7200)
    assert ops.stalled_tasks == 1
    assert len(ops.tasks.daily) == 4  # install day through today
    assert sum(day.succeeded for day in ops.tasks.daily) == 1

    assert (ops.agents.total, ops.agents.succeeded, ops.agents.failed, ops.agents.retried) == (5, 2, 3, 1)
    assert [(e.message, e.count) for e in ops.agents.top_errors] == [
        ("rate limited", 2), ("Browser session expired", 1),
    ]
    assert [(row.label, row.failed) for row in ops.agents.breakdown] == [("Writer", 2), ("Editor", 1)]

    assert (ops.workflows.total, ops.workflows.succeeded, ops.workflows.failed) == (4, 1, 1)
    assert ops.workflows.success_rate == pytest.approx(0.5)
    assert ops.workflows.top_errors[0].message == "Publisher returned 500 Traceback ..."
    assert ops.workflows.breakdown[0].label == "Weekly digest"
    assert ops.stalled_workflow_runs == 1

    assert (ops.automations.total, ops.automations.succeeded, ops.automations.failed) == (6, 1, 5)
    assert ops.auto_paused_automations == ["Hourly sync"]

    human = ops.human
    assert (human.requested, human.approved, human.denied, human.pending) == (3, 1, 1, 1)
    assert human.median_wait_minutes == pytest.approx(60)
    assert human.oldest_pending_hours == pytest.approx(50, abs=0.2)

    assert report.cost.billed_credits == 900
    assert report.cost.projected_monthly_credits == round(900 / report.cost.simulation_days * 30)
    assert report.cost.credits_per_outcome == pytest.approx(450)  # 1 task + 1 workflow run

    checks = {check.key: check for check in report.readiness.checks}
    assert report.readiness.verdict == "at_risk"
    assert checks["setup"].status == "pass"
    assert checks["connections"].status == "na"  # no viewer to resolve accounts for
    # Three judged task runs are too few to call a rate (minimum five).
    assert (checks["tasks"].status, checks["tasks"].reason) == ("warn", "too_few_runs")
    assert (checks["agents"].status, checks["agents"].reason) == ("fail", "below_target")
    assert checks["automation_health"].status == "fail"
    assert checks["automation_health"].details == ["Hourly sync"]
    # Judged per item with at least five judged runs: Writer's three are too few.
    assert checks["item_success"].status == "fail"
    assert checks["item_success"].details == ["Hourly sync — 0% (0/5)"]
    assert "goals" not in checks  # value is judged apart from readiness

    # People decided two requests: one approved, one denied — too few to judge.
    assert (human.accepted, human.rejected) == (1, 1)
    value_checks = {check.key: check for check in report.value.checks}
    assert report.value.verdict == "unproven"
    assert value_checks["goal_progress"].reason == "no_goals"
    assert value_checks["output_acceptance"].reason == "too_few_reviews"
    assert report.promoted_at is None
    assert checks["stalled_work"].values == {"tasks": 1, "workflow_runs": 1, "hours": 48}
    assert (checks["human_load"].status, checks["human_load"].reason) == ("warn", "approval_backlog")
    assert checks["budget"].values["projected_usd"] == pytest.approx(
        round(credits_to_usd(report.cost.projected_monthly_credits), 2),
    )

    # Re-judging only the last two days drops the older paused run and
    # approval, without moving the simulation start.
    recent = await simulate_report(db_session, workspace.id, recent_days=2)
    assert recent.simulation_started_at == report.simulation_started_at
    assert recent.window_start > report.window_start
    assert recent.operations.workflows.total == 3
    assert recent.operations.stalled_workflow_runs == 0
    assert recent.operations.human.pending == 0
