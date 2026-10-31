"""One definition of a run's outcome for every Workspace health surface."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from packages.core.constants.execution import COORDINATOR_PARAM_KEY
from packages.core.constants.task import CancelSource
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.workflow import WorkflowRun
from packages.core.services import workspace_health_metrics as m

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("status", "platform_stopped", "expected"),
    [
        pytest.param("completed", False, m.SUCCEEDED, id="completed"),
        pytest.param("failed", False, m.FAILED, id="failed"),
        # Task Coordinator V3 parks a failed execution as blocked.
        pytest.param("blocked", False, m.FAILED, id="blocked-is-failed"),
        pytest.param("cancelled", False, m.CANCELLED, id="cancelled"),
        pytest.param("in_progress", False, m.IN_FLIGHT, id="in-flight"),
        pytest.param("waiting_on_customer", False, m.IN_FLIGHT, id="waiting-on-customer"),
        # The platform stopping a Task says nothing about the Workspace.
        pytest.param("blocked", True, m.CANCELLED, id="platform-stopped"),
        # A suggestion nobody accepted is not work.
        pytest.param("proposed", False, None, id="proposed-not-work"),
    ],
)
def test_task_outcome(status, platform_stopped, expected) -> None:
    assert m.task_outcome(status, platform_stopped=platform_stopped) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param("success", m.SUCCEEDED, id="success"),
        pytest.param("error", m.FAILED, id="error"),
        pytest.param("cancelled", m.CANCELLED, id="cancelled"),
        pytest.param("running", m.IN_FLIGHT, id="running"),
        # A skipped occurrence (the Workspace was paused) did no work.
        pytest.param("skipped", None, id="skipped-not-work"),
    ],
)
def test_automation_outcome(status, expected) -> None:
    assert m.automation_outcome(status) == expected


@pytest.mark.parametrize(
    ("phase", "outcome_status", "expected"),
    [
        pytest.param("completed", "completed", m.SUCCEEDED, id="delivered"),
        pytest.param("completed", None, m.SUCCEEDED, id="delivered-no-status"),
        pytest.param("completed", "blocked", m.FAILED, id="finished-not-delivered"),
        pytest.param("cancelled", None, m.CANCELLED, id="cancelled"),
        pytest.param("running", None, m.IN_FLIGHT, id="running"),
    ],
)
def test_agent_child_outcome(phase, outcome_status, expected) -> None:
    child = SimpleNamespace(phase=phase, outcome_status=outcome_status)
    assert m.agent_child_outcome(child) == expected


@pytest.mark.asyncio
async def test_agent_child_sql_forms_match_agent_child_outcome(db_session) -> None:
    """Stats counts agent children in SQL; it must judge them the same way."""
    entity_id = generate_ulid()

    def child(phase: str, outcome_status: str | None) -> TaskAgentChildRun:
        root_run_id = generate_ulid()
        return TaskAgentChildRun(
            id=generate_ulid(), task_id=generate_ulid(),
            entity_id=entity_id, workspace_id=generate_ulid(),
            root_run_id=root_run_id, parent_ref=root_run_id,
            parent_tool_call_id="call-1", item_index=0, batch_size=1, depth=1,
            agent_subscription_id=generate_ulid(), agent_id=generate_ulid(),
            prompt="Draft the note.", answer_max_chars=4000,
            lifecycle_generation="lifecycle-1", phase=phase,
            outcome_status=outcome_status, state_version=0,
        )

    rows = [
        child("completed", "completed"),
        child("completed", None),
        child("completed", "blocked"),
        child("cancelled", None),
        child("running", None),
        child("failed", None),
    ]
    db_session.add_all(rows)
    await db_session.flush()

    async def count(*filters) -> int:
        return (await db_session.execute(
            select(func.count()).select_from(TaskAgentChildRun).where(
                TaskAgentChildRun.entity_id == entity_id, *filters,
            )
        )).scalar_one()

    outcomes = [m.agent_child_outcome(row) for row in rows]
    succeeded = await count(m.agent_child_succeeded_clause())
    judged = await count(TaskAgentChildRun.phase.in_(m.AGENT_CHILD_JUDGED_PHASES))

    assert succeeded == outcomes.count(m.SUCCEEDED) == 2
    assert judged == outcomes.count(m.SUCCEEDED) + outcomes.count(m.FAILED) == 4


@pytest.mark.asyncio
async def test_task_platform_stopped_clause_reads_latest_failure_log(db_session) -> None:
    entity_id = generate_ulid()
    stopped = Task(id=generate_ulid(), entity_id=entity_id, title="stopped", status="blocked")
    failed_after = Task(id=generate_ulid(), entity_id=entity_id, title="failed after", status="blocked")
    no_logs = Task(id=generate_ulid(), entity_id=entity_id, title="no logs", status="failed")
    db_session.add_all([stopped, failed_after, no_logs])
    db_session.add_all([
        TaskLog(task_id=stopped.id, log_type="ai_execution_failed", content="Stopped by the runtime migration",
                meta={"error": m.RUNTIME_MIGRATED_ERROR}, created_at=NOW - timedelta(hours=4)),
        TaskLog(task_id=failed_after.id, log_type="ai_execution_failed", content="Stopped by the runtime migration",
                meta={"error": m.RUNTIME_MIGRATED_ERROR}, created_at=NOW - timedelta(hours=5)),
        # A later real failure: the Task failed on its own after the stop.
        TaskLog(task_id=failed_after.id, log_type="ai_execution_failed", content="Search tool quota exhausted",
                meta={"error": "tool_error"}, created_at=NOW - timedelta(hours=2)),
    ])
    await db_session.flush()

    matched = (await db_session.execute(
        select(Task.id).where(Task.entity_id == entity_id, m.task_platform_stopped_clause())
    )).scalars().all()

    assert matched == [stopped.id]

    # The negation keeps every other Task, including one with no logs at all.
    kept = (await db_session.execute(
        select(Task.id).where(Task.entity_id == entity_id, ~m.task_platform_stopped_clause())
    )).scalars().all()

    assert sorted(kept) == sorted([failed_after.id, no_logs.id])

    assert await m.platform_stopped_task_ids(
        db_session, [stopped.id, failed_after.id, no_logs.id],
    ) == {stopped.id}


def test_is_agent_step_excludes_unassigned_skipped_and_coordinator_roots() -> None:
    def step(**overrides):
        values = {"resolved_agent_id": "agent-1", "params": {}, "step_status": "done"}
        values.update(overrides)
        return SimpleNamespace(**values)

    assert m.is_agent_step(step())
    assert not m.is_agent_step(step(resolved_agent_id=None))
    assert not m.is_agent_step(step(step_status="skipped"))
    assert not m.is_agent_step(step(params={COORDINATOR_PARAM_KEY: True}))


@pytest.mark.parametrize(
    ("succeeded", "failed", "expected"),
    [
        pytest.param(0, 0, None, id="nothing-judged"),
        pytest.param(9, 1, 0.9, id="ninety-percent"),
        pytest.param(2, 1, 0.6667, id="rounded-to-4"),
    ],
)
def test_success_rate(succeeded, failed, expected) -> None:
    assert m.success_rate(succeeded, failed) == expected


def test_statuses_with_builds_sql_status_sets() -> None:
    assert m.statuses_with(m.TASK_OUTCOMES, m.FAILED) == {"failed", "blocked"}
    assert m.statuses_with(m.WORKFLOW_RUN_OUTCOMES, m.SUCCEEDED, m.FAILED) == {"completed", "failed"}


@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        pytest.param("in_progress", timedelta(hours=49), True, id="stalled"),
        pytest.param("in_progress", timedelta(hours=47), False, id="recent"),
        pytest.param("pending", timedelta(hours=49), False, id="not-running"),
    ],
)
def test_task_is_stalled(status, age, expected) -> None:
    task = SimpleNamespace(
        status=status, status_changed_at=NOW - age, updated_at=None, created_at=None,
    )
    assert m.task_is_stalled(task, now=NOW) is expected


@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        pytest.param("running", timedelta(hours=49), True, id="stalled"),
        pytest.param("running", timedelta(hours=47), False, id="recent"),
        pytest.param("completed", timedelta(hours=72), False, id="finished"),
    ],
)
def test_workflow_run_is_stalled(status, age, expected) -> None:
    run = SimpleNamespace(status=status, started_at=NOW - age, created_at=None)
    assert m.workflow_run_is_stalled(run, now=NOW) is expected


@pytest.mark.asyncio
async def test_stalled_clauses_match_the_python_rules(db_session) -> None:
    """The SQL clauses and the in-memory checks must agree, including the
    fallbacks (status_changed_at → updated_at, started_at → created_at)."""
    entity_id = generate_ulid()
    rows = [
        Task(id=generate_ulid(), entity_id=entity_id, title="stalled", status="in_progress",
             status_changed_at=NOW - timedelta(hours=49)),
        Task(id=generate_ulid(), entity_id=entity_id, title="fresh", status="in_progress",
             status_changed_at=NOW - timedelta(hours=1)),
        Task(id=generate_ulid(), entity_id=entity_id, title="boundary", status="in_progress",
             status_changed_at=NOW - timedelta(hours=48)),
        Task(id=generate_ulid(), entity_id=entity_id, title="fallback", status="in_progress",
             status_changed_at=None, updated_at=NOW - timedelta(hours=72)),
        Task(id=generate_ulid(), entity_id=entity_id, title="done", status="completed",
             status_changed_at=NOW - timedelta(hours=72)),
    ]
    db_session.add_all(rows)
    await db_session.flush()

    count = (await db_session.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id, m.stalled_task_clause(NOW),
        )
    )).scalar_one()

    assert count == 2
    assert count == sum(m.task_is_stalled(row, now=NOW) for row in rows)

    workflow_runs = [
        WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), entity_id=entity_id,
                    status=status, started_at=started_at, created_at=created_at)
        for status, started_at, created_at in [
            ("running", NOW - timedelta(hours=49), NOW - timedelta(hours=50)),
            ("running", NOW - timedelta(hours=1), NOW - timedelta(hours=72)),
            ("pending", None, NOW - timedelta(hours=72)),          # falls back to created_at
            ("running", NOW - timedelta(hours=48), NOW - timedelta(hours=48)),  # boundary
            ("completed", NOW - timedelta(hours=72), NOW - timedelta(hours=72)),
        ]
    ]
    db_session.add_all(workflow_runs)
    await db_session.flush()

    run_count = (await db_session.execute(
        select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.entity_id == entity_id, m.stalled_workflow_run_clause(NOW),
        )
    )).scalar_one()

    assert run_count == 2
    assert run_count == sum(m.workflow_run_is_stalled(row, now=NOW) for row in workflow_runs)


@pytest.mark.parametrize("source,expected", [
    (None, m.CANCELLED),
    (CancelSource.USER.value, m.CANCELLED),
    (CancelSource.SYSTEM.value, m.CANCELLED),
    (CancelSource.PROPOSAL.value, None),
])
def test_rejected_proposals_are_not_task_outcomes(source, expected):
    assert m.task_outcome("cancelled", cancel_source=source) == expected


@pytest.mark.parametrize("status,hitl_type,decided,expected", [
    ("granted", "review", True, m.REVIEW_ACCEPTED),
    ("consumed", "authorize", True, m.REVIEW_ACCEPTED),
    ("denied", "review", True, m.REVIEW_REJECTED),
    ("granted", "input", True, None),           # information, not a verdict
    ("granted", "review", False, None),         # nobody decided (auto)
    ("expired", "review", False, m.REVIEW_IGNORED),
    ("expired", "input", False, None),
    ("pending", "review", False, None),
])
def test_review_outcome(status, hitl_type, decided, expected):
    assert m.review_outcome(
        status=status, hitl_type=hitl_type, decided_by_user_id="u1" if decided else None,
    ) == expected


@pytest.mark.parametrize("status,expected", [
    ("approved", True), ("executing", True), ("succeeded", True), ("failed", True),
    ("rejected", False), ("proposed", None), ("expired", None), ("cancelled", None),
])
def test_proposal_item_accepted(status, expected):
    assert m.proposal_item_accepted(status) is expected


@pytest.mark.parametrize("seconds,bucket", [
    (0, "le_10"), (10, "le_10"), (11, "le_60"), (86_400, "le_86400"), (86_401, "gt_86400"),
])
def test_duration_bucket(seconds, bucket):
    assert m.duration_bucket(seconds) == bucket


@pytest.mark.parametrize("part,whole,expected", [(4, 4, None), (4, 5, 0.8), (0, 0, None)])
def test_judged_rate_needs_minimum_evidence(part, whole, expected):
    assert m.judged_rate(part, whole) == expected


def _inputs(**overrides):
    base = dict(
        readiness_gaps=[], surfaces={}, quality={}, missed_schedules=0,
        pending_approvals_over_24h=0, service_coverage=None, live_since=None,
        stalled_over_threshold=0, judged_runs_7d=10, now=NOW,
    )
    base.update(overrides)
    return m.HealthInputs(**base)


@pytest.mark.parametrize("overrides,status,rule", [
    ({"readiness_gaps": ["connect_gmail"]}, m.STATUS_NOT_READY, "readiness_gaps"),
    ({"surfaces": {"task": {"success_rate_7d": 0.69, "judged_7d": 5}}}, m.STATUS_FAILING, "success_rate_fail"),
    ({"stalled_over_threshold": 1}, m.STATUS_FAILING, "stalled_work"),
    ({"quality": {"acceptance_rate": 0.69}}, m.STATUS_FAILING, "acceptance_fail"),
    ({"surfaces": {"task": {"success_rate_7d": 0.89, "judged_7d": 5}}}, m.STATUS_DEGRADED, "success_rate_warn"),
    ({"quality": {"acceptance_rate": 0.89}}, m.STATUS_DEGRADED, "acceptance_warn"),
    ({"quality": {"user_cancel_rate": 0.21}}, m.STATUS_DEGRADED, "user_cancel_rate"),
    ({"missed_schedules": 1}, m.STATUS_DEGRADED, "missed_schedules"),
    ({"pending_approvals_over_24h": 1}, m.STATUS_DEGRADED, "approval_backlog"),
    ({"service_coverage": 0.5, "live_since": NOW - timedelta(days=31)}, m.STATUS_DEGRADED, "service_coverage"),
    ({"judged_runs_7d": 0}, m.STATUS_IDLE, "no_runs"),
], ids=lambda value: value if isinstance(value, str) else None)
def test_derive_status_precedence(overrides, status, rule):
    result = m.derive_status(_inputs(**overrides))
    assert result.status == status
    assert result.reasons[0]["rule"] == rule


def test_derive_status_ignores_young_coverage_gaps_and_unknown_surfaces():
    result = m.derive_status(_inputs(
        service_coverage=0.5, live_since=NOW - timedelta(days=29),
        surfaces={"agent": {"unknown": True}},
    ))
    assert result.status == m.STATUS_HEALTHY
    assert result.reasons == []


def test_derive_status_keeps_every_matched_rule():
    result = m.derive_status(_inputs(
        readiness_gaps=["x"], missed_schedules=2,
        surfaces={"task": {"success_rate_7d": 0.5, "judged_7d": 9}},
    ))
    assert result.status == m.STATUS_NOT_READY
    assert {r["rule"] for r in result.reasons} == {"readiness_gaps", "success_rate_fail", "missed_schedules"}
