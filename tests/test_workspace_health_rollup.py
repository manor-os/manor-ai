from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ai.agentic_loop import _llm_call_failed_result
from packages.core.ai.runtime.output_policy import (
    BYOK_PROVIDER_ERROR_CODE,
    LLM_PROVIDER_UNAVAILABLE_ERROR_CODE,
    runtime_public_byok_failure_message,
    runtime_public_provider_unavailable_message,
)
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.ai.runtime.workflow_tools import runtime_workflow_run_context
from packages.core.ai.workflow_failure import settle_workflow_failure, workflow_run_failure_headline
from packages.core.ai.workflow_runner import _agent_loop_failure_message, _runtime_loop_failure_fields
from packages.core.constants.task import TaskLogType
from packages.core.governance.error_signatures import failure_code
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionStep
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import TaskLog
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.models.workspace_health import MetricsWorkspaceDaily
from packages.core.services.scheduled_run_lifecycle import ScheduledRunOutcome, apply_scheduled_run_outcome
from packages.core.services.task_execution_origin import record_task_origin
from packages.core.services.workspace_health_metrics import error_text, latest_task_failure_details, success_rate
from packages.core.services.workspace_health_rollup import rollup_workspace_day
from packages.core.services.workspace_work_units import runs_inside_tasks, tasks_inside_tasks
from packages.core.tasks.scheduler_tasks import _dispatch_job
from packages.core.workers.task_agent_loop import _stop_block
from workspace_health_factories import (
    _credits,
    _feedback,
    _flow_run,
    _job,
    _proposal_items,
    _reviews,
    _step_result,
    _tasks,
    _worker_steps,
    _workspace,
)

DAY = date(2026, 10, 27)
NOON = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


async def _row(db, ws_id, surface):
    return (await db.execute(select(MetricsWorkspaceDaily).where(
        MetricsWorkspaceDaily.day == DAY,
        MetricsWorkspaceDaily.workspace_id == ws_id,
        MetricsWorkspaceDaily.surface == surface,
    ))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_task_surface_counts_shared_outcomes(db_session):
    ws = await _workspace(db_session, blueprint=("builtin:weekly-report", "3"), sandbox=False)
    await _tasks(db_session, ws, [
        ("completed", None, 30), ("completed", None, 400),   # status, cancel_source, seconds
        ("blocked", None, None),                             # failed
        ("cancelled", "user", None), ("cancelled", "proposal", None),
        ("in_progress", None, None),                         # not terminal
    ], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    row = await _row(db_session, ws.id, "task")
    assert (row.succeeded, row.failed, row.cancelled, row.user_cancelled) == (2, 1, 1, 1)
    assert (row.duration_count, row.duration_sum_s) == (2, 430)
    assert row.duration_buckets == {"le_60": 1, "le_900": 1}
    assert row.top_errors == [{"message": "Task blocked", "count": 1}]
    assert (row.blueprint_key, row.blueprint_version, row.mode) == ("builtin:weekly-report", "3", "live")


@pytest.mark.asyncio
async def test_workspace_surface_counts_quality_signals_and_credits(db_session):
    ws = await _workspace(db_session, sandbox=True)
    await _reviews(db_session, ws, [("granted", "review", True), ("denied", "authorize", True),
                                    ("expired", "review", False), ("granted", "input", True)], at=NOON)
    await _feedback(db_session, ws, ["up", "up", "down"], at=NOON)
    await _proposal_items(db_session, ws, ["approved", "succeeded", "rejected", "proposed"], at=NOON)
    await _credits(db_session, ws, [5, 7], at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    row = await _row(db_session, ws.id, "workspace")
    assert (row.reviews_accepted, row.reviews_rejected, row.reviews_ignored) == (1, 1, 1)
    assert (row.feedback_up, row.feedback_down) == (2, 1)
    assert (row.proposals_accepted, row.proposals_rejected) == (2, 1)
    assert row.credits == 12
    assert row.mode == "simulation"


@pytest.mark.asyncio
async def test_run_surfaces_use_each_engine_finish_time_and_classifier(db_session):
    ws = await _workspace(db_session)
    common = {"entity_id": ws.entity_id, "workspace_id": ws.id}
    db_session.add_all([
        WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), status="completed",
                    started_at=NOON - timedelta(seconds=5), completed_at=NOON, **common),
        WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), status="failed",
                    error="Publisher returned 500", attempt_number=2, completed_at=NOON, **common),
        WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), status="cancelled",
                    cancel_source="system", completed_at=NOON, **common),
        ExecutionStep(id=generate_ulid(), plan_id=generate_ulid(), step_key="s1", kind="subagent",
                      resolved_agent_id=generate_ulid(), step_status="done", attempt_count=2,
                      started_at=NOON - timedelta(seconds=20), finished_at=NOON, **common),
        TaskAgentChildRun(
            id=generate_ulid(), task_id=generate_ulid(), root_run_id="r", parent_ref="r",
            parent_tool_call_id="call-1", item_index=0, batch_size=1, depth=1,
            agent_subscription_id=generate_ulid(), agent_id=generate_ulid(), prompt="Edit.",
            answer_max_chars=4000, lifecycle_generation="g1", phase="failed",
            outcome_status="failed", state_version=0, terminal_at=NOON,
            result={"message": "Browser session expired"}, **common,
        ),
    ])
    job = await _job(db_session, ws, True, None)
    db_session.add_all([
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="success",
                        started_at=NOON - timedelta(seconds=2), completed_at=NOON),
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="error", error="timeout",
                        created_at=NOON),
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="skipped", created_at=NOON),
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="running", created_at=NOON),
    ])
    await db_session.flush()

    await rollup_workspace_day(db_session, day=DAY)

    workflow = await _row(db_session, ws.id, "workflow")
    assert (workflow.succeeded, workflow.failed, workflow.cancelled, workflow.retried) == (1, 1, 1, 1)
    assert workflow.user_cancelled == 0
    assert workflow.top_errors == [{"message": "Publisher returned 500", "count": 1}]
    agent = await _row(db_session, ws.id, "agent")
    assert (agent.succeeded, agent.failed, agent.retried) == (1, 1, 1)
    assert agent.top_errors == [{"message": "Browser session expired", "count": 1}]
    automation = await _row(db_session, ws.id, "automation")
    assert (automation.succeeded, automation.failed, automation.cancelled) == (1, 1, 0)


@pytest.mark.asyncio
async def test_rollup_attributes_by_finish_day_and_isolates_workspaces(db_session):
    a, b = await _workspace(db_session), await _workspace(db_session)
    await _tasks(db_session, a, [("completed", None, 10)], finished_at=NOON)
    await _tasks(db_session, a, [("completed", None, 10)], finished_at=NOON + timedelta(days=1))
    await _tasks(db_session, b, [("failed", None, None)], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    assert (await _row(db_session, a.id, "task")).succeeded == 1
    assert (await _row(db_session, b.id, "task")).failed == 1


@pytest.mark.asyncio
async def test_rerolling_a_day_replaces_its_rows(db_session):
    ws = await _workspace(db_session)
    [task] = await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    task.status, task.cancel_source = "cancelled", "user"
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    row = await _row(db_session, ws.id, "task")
    assert (row.succeeded, row.cancelled) == (0, 1)


@pytest.mark.asyncio
async def test_rollup_prunes_rows_past_retention(db_session):
    ws = await _workspace(db_session)
    db_session.add_all([
        MetricsWorkspaceDaily(day=DAY - timedelta(days=181), workspace_id=ws.id,
                              surface="task", entity_id=ws.entity_id, mode="live"),
        MetricsWorkspaceDaily(day=DAY - timedelta(days=179), workspace_id=ws.id,
                              surface="task", entity_id=ws.entity_id, mode="live"),
    ])
    await db_session.flush()
    result = await rollup_workspace_day(db_session, day=DAY, now=NOON)
    # The test DB is shared, so assert on this Workspace's rows, not the count.
    assert result["pruned"] >= 1
    kept = (await db_session.execute(select(MetricsWorkspaceDaily.day).where(
        MetricsWorkspaceDaily.workspace_id == ws.id,
    ))).scalars().all()
    assert kept == [DAY - timedelta(days=179)]


def _agent_loop_failure(
    task_id: str, stop_reason: str, *, error_detail: dict | None = None, at: datetime,
) -> TaskLog:
    """The log ``task_agent_loop._block`` writes for a loop that stopped."""
    code, message = _stop_block(stop_reason, exhausted_rounds=False)
    outcome = tool_error_payload(
        tool_name="task_agent_loop", code=code, message=message,
        details={"error_detail": error_detail, "task_id": task_id, "checkpoint_preserved": True},
    )
    return TaskLog(
        task_id=task_id, log_type=TaskLogType.AI_EXECUTION_FAILED.value, content=message,
        meta={"error": code, "coordinator_outcome": outcome, "engine": "agent_loop"},
        created_at=at,
    )


@pytest.mark.asyncio
async def test_rollup_categorizes_failures_from_their_full_text(db_session):
    # Catches: categorizing the 160-character top_errors text or the task
    # surface's placeholder "Task failed"; reading only ``meta["error"]``, which
    # the agent loop flattens to ``agent_loop_error`` for every runtime error;
    # coding an outdated log; and coding a rejected BYOK key as an outage.
    ws = await _workspace(db_session)
    managed, byok_outage, byok_rejected = await _tasks(
        db_session, ws, [("failed", None, None)] * 3, finished_at=NOON,
    )
    runtime_error = "error"
    db_session.add_all([
        # An older, different failure must not outvote the latest log.
        _agent_loop_failure(managed.id, "credit_exhausted", at=NOON - timedelta(hours=2)),
        _agent_loop_failure(managed.id, runtime_error, at=NOON - timedelta(hours=1), error_detail={
            "code": LLM_PROVIDER_UNAVAILABLE_ERROR_CODE, "transient": True,
            "message": runtime_public_provider_unavailable_message(),
        }),
        _agent_loop_failure(byok_outage.id, runtime_error, at=NOON, error_detail={
            "code": BYOK_PROVIDER_ERROR_CODE, "transient": True,
            "message": runtime_public_byok_failure_message(transient=True),
        }),
        _agent_loop_failure(byok_rejected.id, runtime_error, at=NOON, error_detail={
            "code": BYOK_PROVIDER_ERROR_CODE, "transient": False,
            "message": runtime_public_byok_failure_message(transient=False),
        }),
    ])
    db_session.add_all([
        WorkflowRun(
            id=generate_ulid(), workflow_id=generate_ulid(), entity_id=ws.entity_id,
            workspace_id=ws.id, status="failed", error=error_text, completed_at=NOON,
        )
        for error_text in ("Publisher returned 500", "ACCESS_DENIED")
    ])
    await db_session.flush()

    await rollup_workspace_day(db_session, day=DAY)

    task_row = await _row(db_session, ws.id, "task")
    assert task_row.failed == 3
    assert task_row.error_codes == {"provider_down": 1, "byok_provider_down": 1}
    assert task_row.top_errors == [{"message": "Task failed", "count": 3}]
    workflow_row = await _row(db_session, ws.id, "workflow")
    assert workflow_row.failed == 2
    assert workflow_row.error_codes == {"app_permission_denied": 1}
    # The work surface keeps the same codes and none of the failure text.
    work_row = await _row(db_session, ws.id, "work")
    assert work_row.failed == 5
    assert work_row.error_codes == {"provider_down": 1, "byok_provider_down": 1, "app_permission_denied": 1}
    assert work_row.top_errors == []




_LOOP_FAILURE = dict(messages=[], usage={}, rounds=1, tool_calls_made=[])


def _failed_step(result):
    """A failed agent step's result and headline, as the runner settles them."""
    step = {"id": "write", "name": "Write", "type": "agent"}
    raw = result if isinstance(result, str) else _agent_loop_failure_message(result)
    fields = {} if isinstance(result, str) else _runtime_loop_failure_fields(result)
    settled = settle_workflow_failure(step, {"status": "failed", "error": raw, **fields}, step_results={}, locale="en")
    return settled, workflow_run_failure_headline(step, settled)


@pytest.mark.parametrize(
    ("result", "headline_reads", "expected"),
    [
        pytest.param(_llm_call_failed_result(
            "x", error_code="byok_provider_error", transient=True, **_LOOP_FAILURE,
            public_detail="HTTP 503 from api.acme.com (model gpt-x) after 4 attempts over 90s",
        ), "provider_down", "byok_provider_down", id="byok-outage"),
        pytest.param(_llm_call_failed_result(
            "x", error_code="llm_provider_unavailable", **_LOOP_FAILURE,
            public_detail="HTTP 503 after 4 attempts over 90s",
        ), "provider_down", "provider_down", id="managed-outage"),
        pytest.param(
            "Agent step ended without meeting its completion requirement. Last tool error: sheets: 403 "
            "PERMISSION_DENIED The caller does not have permission for this spreadsheet and the agent "
            "tried three times before giving up on it.",
            None, "app_permission_denied", id="tool-error-past-the-first-sentence",
        ),
    ],
)
@pytest.mark.asyncio
async def test_workflow_failures_are_coded_from_the_step_not_the_shortened_headline(
    db_session, result, headline_reads, expected,
):
    # Catches: coding ``run.error``, whose settled headline calls a BYOK outage a
    # platform outage and cuts a tool's permission error off.
    settled, headline = _failed_step(result)
    assert failure_code(headline) == headline_reads
    ws = await _workspace(db_session)
    await _flow_run(db_session, ws, {"start": "trigger", "write": "agent", "done": "end"}, {
        "start": _step_result("start"), "write": settled,
    }, finished_at=NOON, status="failed", error=headline, current="write")
    await rollup_workspace_day(db_session, day=DAY)
    work, flows = await _row(db_session, ws.id, "work"), await _row(db_session, ws.id, "workflow")
    assert work.error_codes == {expected: 1}
    assert flows.error_codes == {expected: 1}
    # Failure text stays the headline, and stays off the work surface.
    assert flows.top_errors == [{"message": error_text(headline), "count": 1}]
    assert work.top_errors == []


_PARENT_DISPATCH = {"kind": "scheduler_parent", "args": ["job", "2026-10-27T12:00:00+00:00"],
                    "kwargs": {"occurrence_key": "k"}}


def _child_dispatch(kind):
    return {"kind": kind, "args": [generate_ulid()], "kwargs": {}}


@pytest.mark.parametrize(
    ("status", "result", "expected"),
    [
        pytest.param("error", None, (0, 1, 0), id="no-envelope"),
        pytest.param("error", {"dispatch": _PARENT_DISPATCH, "dispatch_status": "published"}, (0, 1, 0),
                     id="tick-handoff-only"),
        pytest.param("error", {"dispatch": _child_dispatch("workflow"), "dispatch_status": "prepared"}, (0, 1, 0),
                     id="child-handoff-never-accepted"),
        pytest.param("error", {"dispatch": {"kind": "workflow"}, "dispatch_status": "quarantined"}, (0, 1, 0),
                     id="child-handoff-quarantined"),
        pytest.param("error", {"dispatch": _child_dispatch("workflow"), "dispatch_status": "published",
                               "scheduled_execution_state": "settled", "workflow_run_id": "r"}, (0, 0, 0),
                     id="started-a-flow"),
        pytest.param("error", {"dispatch": _child_dispatch("agent_task"), "dispatch_status": "published",
                               "scheduled_execution_state": "settled"}, (0, 0, 0), id="started-a-task"),
        # Publishing failed for good, then the sweep marked the terminal run published.
        pytest.param("error", {"dispatch": _child_dispatch("workflow"), "dispatch_status": "published"}, (0, 1, 0),
                     id="swept-to-published-but-never-started"),
        pytest.param("error", {"dispatch": _child_dispatch("agent_task"), "dispatch_status": "prepared",
                               "scheduled_execution_state": "admitted"}, (0, 0, 0),
                     id="started-though-publishing-was-not-recorded"),
        pytest.param("error", {"dispatch": _child_dispatch("strategist_review"), "dispatch_status": "published",
                               "scheduled_execution_state": "settled"}, (0, 0, 0), id="started-a-platform-job"),
        pytest.param("success", None, (0, 0, 0), id="succeeded"),
        pytest.param("cancelled", None, (0, 0, 0), id="cancelled"),
        pytest.param("skipped", None, (0, 0, 0), id="skipped"),
    ],
)
@pytest.mark.asyncio
async def test_a_scheduled_run_that_failed_handing_nothing_over_is_a_failed_unit(db_session, status, result, expected):
    ws = await _workspace(db_session)
    job = await _job(db_session, ws, True, None)
    db_session.add(ScheduledJobRun(
        id=generate_ulid(), job_id=job.job_id, status=status, result=result, created_at=NOON, completed_at=NOON,
        error="Skill s1 not found" if status == "error" else None,
    ))
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == expected


async def _scheduled_job(db, ws, **fields):
    job = ScheduledJob(
        id=generate_ulid(), job_id=f"job-{generate_ulid()}", entity_id=ws.entity_id, workspace_id=ws.id, name="Job",
        schedule_kind="at", payload_message="go", user_id=generate_ulid(), **fields,
    )
    db.add(job)
    await db.flush()
    return job


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        pytest.param("skill", {"skill_id": "gone"}, id="skill-not-found"),
        pytest.param("workflow", {}, id="no-workflow-id"),
    ],
)
@pytest.mark.asyncio
async def test_a_job_broken_by_a_release_reaches_work_through_the_real_dispatcher(db_session, kind, target):
    ws = await _workspace(db_session)
    job = await _scheduled_job(db_session, ws, execution_type=kind, execution_target=target)
    now = datetime.now(timezone.utc)
    assert await _dispatch_job(db_session, job, now, manual=False) is None
    run = (await db_session.execute(select(ScheduledJobRun).where(ScheduledJobRun.job_id == job.job_id))).scalar_one()
    assert run.status == "error"
    await rollup_workspace_day(db_session, day=run.completed_at.date(), now=now)
    row = (await db_session.execute(select(MetricsWorkspaceDaily).where(
        MetricsWorkspaceDaily.workspace_id == ws.id, MetricsWorkspaceDaily.surface == "work",
    ))).scalar_one()
    assert (row.succeeded, row.failed) == (0, 1)
    assert row.top_errors == []


@pytest.mark.asyncio
async def test_a_flow_the_real_dispatcher_started_is_the_unit_not_its_scheduled_run(db_session):
    ws = await _workspace(db_session)
    workflow = WorkflowDefinition(
        id=generate_ulid(), entity_id=ws.entity_id, name="Flow", variables={}, tags=[], steps=[
            {"id": "start", "type": "trigger", "next": ["make"]}, {"id": "make", "type": "agent", "next": []},
        ],
    )
    db_session.add(workflow)
    await db_session.flush()
    job = await _scheduled_job(db_session, ws, execution_type="workflow", execution_target={"workflow_id": workflow.id})
    now = datetime.now(timezone.utc)
    dispatch, run_id = await _dispatch_job(db_session, job, now, manual=False)
    flow = await db_session.get(WorkflowRun, dispatch["args"][0])
    flow.status, flow.error, flow.completed_at, flow.current_step_id = "failed", "No", now, "make"
    flow.step_results = {"make": _step_result("make", "failed", error="No")}
    # What ``_mark_scheduled_dispatch_published`` and the Flow's settlement record.
    scheduled = await db_session.get(ScheduledJobRun, run_id)
    scheduled.result = {**scheduled.result, "dispatch_status": "published"}
    apply_scheduled_run_outcome(scheduled, ScheduledRunOutcome.from_execution(
        error="No", result={"workflow_run_id": flow.id, "status": "failed"},
    ), completed_at=now)
    await db_session.flush()
    assert scheduled.status == "error"
    await rollup_workspace_day(db_session, day=now.date(), now=now)
    row = (await db_session.execute(select(MetricsWorkspaceDaily).where(
        MetricsWorkspaceDaily.workspace_id == ws.id, MetricsWorkspaceDaily.surface == "work",
    ))).scalar_one()
    assert (row.succeeded, row.failed) == (0, 1)


# ── Work surface ─────────────────────────────────────────────────────

AGENT_FLOW = {"start": "trigger", "make": "agent", "done": "end"}
PARENT_FLOW = {"start": "trigger", "run": "subworkflow", "done": "end"}
FOREACH_FLOW = {"start": "trigger", "run": "foreach_subworkflow", "done": "end"}
IDLE_RESULTS = ["start", "lease", "has_item", "idle"]


async def _work(db, ws_id):
    """(succeeded, failed, cancelled) on the work surface."""
    row = await _row(db, ws_id, "work")
    return (row.succeeded, row.failed, row.cancelled) if row else (0, 0, 0)


async def _flows(db, ws_id):
    """(succeeded, failed) on the workflow surface, which counts every execution."""
    row = await _row(db, ws_id, "workflow")
    return (row.succeeded, row.failed) if row else (0, 0)


@pytest.mark.parametrize(
    ("steps", "results", "run", "expected"),
    [
        pytest.param(_worker_steps(), IDLE_RESULTS, {}, (0, 0, 0), id="idle-tick"),
        pytest.param(AGENT_FLOW, ["start", "make", "done"], {}, (1, 0, 0), id="ran-an-agent"),
        pytest.param({"start": "trigger", "when": "code", "due": "condition", "done": "end"},
                     ["start", "when", "due", "done"], {}, (0, 0, 0), id="idle-tick-through-a-code-step"),
        pytest.param({"start": "trigger", "ask": "wait", "done": "end"}, ["start", "ask", "done"], {}, (0, 0, 0),
                     id="only-waited-for-a-person"),
        pytest.param({"start": "trigger", "post": "connector", "done": "end"},
                     {"start": _step_result("start"), "post": _step_result("post", skipped=True),
                      "done": _step_result("done")}, {}, (0, 0, 0), id="its-work-step-was-skipped"),
        pytest.param({"start": "trigger", "next": "a_step_type_added_later"}, ["start", "next"], {}, (1, 0, 0),
                     id="unknown-type-is-work"),
        pytest.param({}, ["start", "gone"], {}, (1, 0, 0), id="no-snapshot-is-work"),
        pytest.param(_worker_steps(), {"start": _step_result("start"), "lease": _step_result(
            "lease", "failed", error="ACCESS_DENIED")}, {"status": "failed", "error": "ACCESS_DENIED",
                                                         "current": "lease"}, (0, 1, 0), id="failed-at-a-control-step"),
        pytest.param(PARENT_FLOW, {"start": _step_result("start"), "run": {
            "status": "failed", "error": "subworkflow make_post not found"}},
            {"status": "failed", "error": "subworkflow make_post not found", "current": "run"}, (0, 1, 0),
            id="subworkflow-step-failed-without-a-child"),
        pytest.param(AGENT_FLOW, ["start"], {"status": "cancelled", "cancel_source": "user", "current": "make"},
                     (0, 0, 1), id="cancelled-during-a-work-step"),
        pytest.param({"start": "trigger", "ask": "wait", "make": "agent"}, ["start", "ask"],
                     {"status": "cancelled", "cancel_source": "user", "current": "ask"}, (0, 0, 0),
                     id="cancelled-before-any-work"),
    ],
)
@pytest.mark.asyncio
async def test_work_surface_counts_a_run_that_did_work_or_failed(db_session, steps, results, run, expected):
    ws = await _workspace(db_session)
    await _flow_run(db_session, ws, steps, results, finished_at=NOON, **run)
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == expected
    # The execution-counting surface still sees every finished run.
    workflow = await _row(db_session, ws.id, "workflow")
    assert workflow.succeeded + workflow.failed + workflow.cancelled == 1


@pytest.mark.asyncio
async def test_work_surface_counts_a_failure_once_whichever_run_of_a_family_reports_it(db_session):
    # Catches: a subworkflow parent and its child each counting, and a parent
    # dropped although its own step was what failed.
    ws = await _workspace(db_session)
    ok_parent = await _flow_run(db_session, ws, PARENT_FLOW, ["start", "run", "done"], finished_at=NOON)
    await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=ok_parent)
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 0, 0)

    ws = await _workspace(db_session)
    bad_parent = await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")},
                                 finished_at=NOON, status="failed", error="Publisher returned 500", current="run")
    child = await _flow_run(db_session, ws, AGENT_FLOW, {"start": _step_result("start"), "make": _step_result(
        "make", "failed", error="Publisher returned 500")}, finished_at=NOON, status="failed",
        error="Publisher returned 500", parent=bad_parent)
    bad_parent.step_results = {**bad_parent.step_results, "run": _step_result(
        "run", "failed", error="Publisher returned 500", subrun_id=child.id)}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (0, 1, 0)
    assert await _flows(db_session, ws.id) == (0, 2)


@pytest.mark.asyncio
async def test_work_surface_counts_each_foreach_item_not_its_parent(db_session):
    # ``continue_on_failure``: the parent completes while one of its items failed.
    ws = await _workspace(db_session)
    parent = await _flow_run(db_session, ws, FOREACH_FLOW, ["start", "run", "done"], finished_at=NOON)
    await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=parent)
    failed = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                             finished_at=NOON, status="failed", error="No", parent=parent)
    parent.step_results = {**parent.step_results, "run": _step_result(
        "run", items=[{"key": "b", "status": "failed", "subrun_id": failed.id}], failed_count=1)}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 1, 0)
    assert await _flows(db_session, ws.id) == (2, 1)

    # Every item failed, so the foreach failed too: still one unit per item.
    ws = await _workspace(db_session)
    parent = await _flow_run(db_session, ws, FOREACH_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                             status="failed", error="No", current="run")
    failed = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                             finished_at=NOON, status="failed", error="No", parent=parent)
    parent.step_results = {**parent.step_results, "run": _step_result(
        "run", "failed", error="No", items=[{"key": "a", "status": "failed", "subrun_id": failed.id}])}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (0, 1, 0)


@pytest.mark.asyncio
async def test_a_parent_interrupted_while_its_child_is_fine_is_its_own_failure(db_session):
    ws = await _workspace(db_session)
    parent = await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                             status="failed", error="Worker lost", current="run")
    child = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=parent)
    parent.step_results = {**parent.step_results, "run": {"status": "paused", "subrun_id": child.id}}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 1, 0)


@pytest.mark.asyncio
async def test_a_flow_inside_a_task_leaves_the_task_as_the_unit(db_session):
    # The trigger shapes are what ``_run_workflow`` and the scheduler write.
    ws = await _workspace(db_session)
    [task] = await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON)
    tool_started = await _flow_run(db_session, ws, PARENT_FLOW, ["start", "run", "done"], finished_at=NOON,
                                   trigger_data={"runtime_context": {"workspace_id": ws.id, "task_id": task.id}})
    scheduled = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON,
                                trigger_data={"_workflow_runtime_context": {"task_id": task.id}})
    for run in (tool_started, scheduled):
        assert runtime_workflow_run_context(run)["task_id"] == task.id
    # A child does not inherit the name: it is found through its parent.
    child = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON,
                            parent=tool_started)
    assert runtime_workflow_run_context(child)["task_id"] is None
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 0, 0)
    assert await _flows(db_session, ws.id) == (3, 0)
    assert (await _row(db_session, ws.id, "task")).succeeded == 1


@pytest.mark.asyncio
async def test_a_task_id_that_names_no_task_of_this_workspace_does_not_hide_the_flow(db_session):
    other = await _workspace(db_session)
    [elsewhere] = await _tasks(db_session, other, [("completed", None, 10)], finished_at=NOON)
    ws = await _workspace(db_session)
    for task_id in (elsewhere.id, "from-a-webhook-payload"):
        await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON,
                        trigger_data={"runtime_context": {"task_id": task_id}})
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (2, 0, 0)


@pytest.mark.parametrize(
    ("container_status", "container_at", "expected"),
    [
        # The scheduler copies a job's last Task into every later run's context.
        pytest.param("completed", NOON - timedelta(days=9), (0, 1, 0), id="stale-id-of-a-task-from-days-ago"),
        # ``run_workflow`` returns for a paused run, so its Task can finish first.
        pytest.param("completed", NOON - timedelta(hours=1), (1, 1, 0), id="task-finished-before-the-flow"),
        pytest.param("completed", NOON, (1, 0, 0), id="task-finished-with-the-flow"),
        pytest.param("completed", NOON + timedelta(hours=1), (1, 0, 0), id="task-finished-after-the-flow"),
        pytest.param("in_progress", NOON - timedelta(days=9), (0, 0, 0), id="open-task-whatever-its-old-stamps"),
    ],
)
@pytest.mark.asyncio
async def test_a_flow_is_covered_only_by_a_task_that_had_not_finished(db_session, container_status, container_at, expected):
    ws = await _workspace(db_session)
    [task] = await _tasks(db_session, ws, [(container_status, None, 10)], finished_at=container_at)
    await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                    finished_at=NOON, status="failed", error="No",
                    trigger_data={"task_id": task.id, "_workflow_runtime_context": {"task_id": task.id}})
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == expected


@pytest.mark.asyncio
async def test_a_persons_retry_of_a_flow_whose_task_has_ended_counts_for_itself(db_session):
    # A retry deep-copies the first attempt's context, Task id included.
    ws = await _workspace(db_session)
    [task] = await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON - timedelta(hours=1))
    context = {"_workflow_runtime_context": {"task_id": task.id}}
    first = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                            finished_at=NOON - timedelta(hours=2), status="failed", error="No", trigger_data=context)
    await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                    finished_at=NOON, status="failed", error="No", trigger_data=context,
                    attempt=2, lineage_root=first.id)
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 1, 0)


@pytest.mark.asyncio
async def test_a_child_is_judged_against_the_task_at_its_own_finish(db_session):
    # Its retried parent still names a Task that ended before this child failed.
    ws = await _workspace(db_session)
    [task] = await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON - timedelta(hours=1))
    parent = await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                             status="failed", error="No", current="run",
                             trigger_data={"runtime_context": {"task_id": task.id}})
    child = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                            finished_at=NOON, status="failed", error="No", parent=parent)
    parent.step_results = {**parent.step_results, "run": _step_result("run", "failed", error="No", subrun_id=child.id)}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 1, 0)


@pytest.mark.parametrize(
    ("parent_status", "parent_at", "grandparent", "task_status", "expected"),
    [
        pytest.param("running", None, False, "in_progress", (0, 0, 0), id="parent-still-running"),
        pytest.param("completed", NOON + timedelta(days=1), False, "in_progress", (0, 0, 0), id="parent-done-next-day"),
        pytest.param("running", None, True, "in_progress", (0, 0, 0), id="named-two-levels-up"),
        pytest.param("running", None, False, None, (1, 0, 0), id="no-task-above-it"),
        pytest.param("running", None, False, "completed-earlier", (2, 0, 0), id="task-above-it-had-ended"),
    ],
)
@pytest.mark.asyncio
async def test_a_child_finds_its_task_through_parents_outside_the_day(
    db_session, parent_status, parent_at, grandparent, task_status, expected,
):
    ws = await _workspace(db_session)
    context = {}
    if task_status:
        ended = task_status == "completed-earlier"
        [task] = await _tasks(db_session, ws, [("completed" if ended else task_status, None, 10)],
                              finished_at=NOON - timedelta(hours=1))
        context = {"runtime_context": {"task_id": task.id}}
    above = None
    if grandparent:
        above = await _flow_run(db_session, ws, PARENT_FLOW, ["start"], finished_at=None, status="running",
                                trigger_data=context)
        context = {}
    parent = await _flow_run(db_session, ws, PARENT_FLOW, ["start"], finished_at=parent_at, status=parent_status,
                             trigger_data=context, parent=above)
    await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=parent)
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == expected


@pytest.mark.asyncio
async def test_more_ids_than_bind_parameters_do_not_fail_the_rollup(db_session):
    # asyncpg accepts 32,767 parameters; one over-long IN would fail every Workspace's day.
    count = 33_000
    task_rows = [SimpleNamespace(id=str(index), workspace_id="w", finished_at=NOON, origin_task_id=f"t{index}")
                 for index in range(count)]
    assert await tasks_inside_tasks(db_session, task_rows) == set()
    run_rows = [SimpleNamespace(id=str(index), workspace_id="w", finished_at=NOON, task_id=None,
                                parent_run_id=f"p{index}") for index in range(count)]
    assert await runs_inside_tasks(db_session, run_rows) == set()
    named_rows = [SimpleNamespace(id=str(index), workspace_id="w", finished_at=NOON, task_id=f"t{index}",
                                  parent_run_id=None) for index in range(count)]
    assert await runs_inside_tasks(db_session, named_rows) == set()
    assert await latest_task_failure_details(db_session, [str(index) for index in range(count)]) == {}


@pytest.mark.parametrize(
    ("origin_status", "origin_at", "inside", "inside_at", "expected"),
    [
        # A coordinator done with the Tasks it started still failing an hour later.
        pytest.param("completed", NOON, ("failed", None, None), NOON + timedelta(hours=1), (1, 1, 0),
                     id="failed-after-its-origin-succeeded"),
        pytest.param("completed", NOON + timedelta(hours=1), ("failed", None, None), NOON, (1, 0, 0),
                     id="failed-before-its-origin-finished"),
        pytest.param("completed", NOON, ("completed", None, 10), NOON, (1, 0, 0), id="finished-together"),
        pytest.param("in_progress", NOON - timedelta(hours=1), ("failed", None, None), NOON, (0, 0, 0),
                     id="origin-still-open"),
        pytest.param("failed", NOON, ("cancelled", "user", None), NOON + timedelta(hours=1), (0, 1, 1),
                     id="cancelled-after-its-origin-failed"),
    ],
)
@pytest.mark.asyncio
async def test_a_task_made_inside_another_task_is_covered_only_while_that_task_is_unfinished(
    db_session, origin_status, origin_at, inside, inside_at, expected,
):
    ws = await _workspace(db_session)
    [origin] = await _tasks(db_session, ws, [(origin_status, None, 10)], finished_at=origin_at)
    [task] = await _tasks(db_session, ws, [inside], finished_at=inside_at)
    record_task_origin(task, origin.id)
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == expected
    # Whatever the verdict, the task surface counts them both when both finished.
    row = await _row(db_session, ws.id, "task")
    assert row.succeeded + row.failed + row.cancelled == (2 if origin_status != "in_progress" else 1)


@pytest.mark.asyncio
async def test_a_task_whose_origin_is_elsewhere_or_gone_counts_for_itself(db_session):
    # Across Workspaces the origin reports only in its own.
    a, b = await _workspace(db_session), await _workspace(db_session)
    [origin] = await _tasks(db_session, a, [("completed", None, 10)], finished_at=NOON + timedelta(hours=1))
    [elsewhere] = await _tasks(db_session, b, [("failed", None, None)], finished_at=NOON)
    record_task_origin(elsewhere, origin.id)
    # A person's subtask has no origin, whatever its parent.
    [subtask] = await _tasks(db_session, a, [("completed", None, 10)], finished_at=NOON + timedelta(hours=2))
    subtask.parent_task_id = origin.id
    # An origin that no longer exists.
    gone = await _workspace(db_session)
    [orphan] = await _tasks(db_session, gone, [("failed", None, None)], finished_at=NOON)
    record_task_origin(orphan, generate_ulid())
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, a.id) == (2, 0, 0)
    assert await _work(db_session, b.id) == (0, 1, 0)
    assert await _work(db_session, gone.id) == (0, 1, 0)


@pytest.mark.asyncio
async def test_a_coordinator_cannot_read_100_percent_over_tasks_failing_after_it(db_session):
    ws = await _workspace(db_session)
    [coordinator] = await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON)
    later = await _tasks(db_session, ws, [("failed", None, None)] * 5, finished_at=NOON + timedelta(hours=1))
    for task in later:
        record_task_origin(task, coordinator.id)
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 5, 0)


@pytest.mark.parametrize(
    ("latest_status", "latest_on", "expected"),
    [
        pytest.param("completed", DAY, (1, 0, 0), id="failed-then-succeeded"),
        pytest.param("failed", DAY, (0, 1, 0), id="failed-twice"),
        pytest.param("cancelled", DAY, (0, 0, 1), id="failed-then-cancelled"),
        pytest.param("running", None, (0, 0, 0), id="retry-still-running"),
        pytest.param("completed", DAY + timedelta(days=1), (0, 0, 0), id="retry-finished-the-next-day"),
    ],
)
@pytest.mark.asyncio
async def test_a_retry_lineage_is_one_unit_read_from_its_latest_attempt(db_session, latest_status, latest_on, expected):
    ws = await _workspace(db_session)
    first = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                            finished_at=NOON, status="failed", error="No")
    # A retry inherits the receipts of the steps it does not redo.
    await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], status=latest_status, attempt=2,
                    finished_at=datetime.combine(latest_on, NOON.timetz()) if latest_on else None,
                    lineage_root=first.id)
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == expected


@pytest.mark.asyncio
async def test_a_run_with_no_lineage_root_is_its_own_lineage_and_a_retry_can_root_to_it(db_session):
    ws = await _workspace(db_session)
    first = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed")},
                            finished_at=NOON, status="failed", error="No")
    await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make"], finished_at=NOON, attempt=2, lineage_root=first.id)
    alone = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make"], finished_at=NOON)
    # Attempt 1 of an old lineage has no root; ``retry_workflow_run`` roots attempt 2 to it.
    legacy_first = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed")},
                                   finished_at=NOON, status="failed", error="No")
    await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make"], finished_at=NOON, attempt=2,
                    lineage_root=legacy_first.id)
    for run in (alone, legacy_first):
        run.lineage_root_run_id, run.lineage_is_legacy = None, None
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    work = await _row(db_session, ws.id, "work")
    assert (work.succeeded, work.failed, work.retried) == (3, 0, 0)
    workflow = await _row(db_session, ws.id, "workflow")
    assert (workflow.succeeded, workflow.failed, workflow.retried) == (3, 2, 2)


async def _failed_family(db, ws, *, parent_steps=PARENT_FLOW):
    """A parent attempt that failed on its child, as a person sees it before retrying."""
    parent = await _flow_run(db, ws, parent_steps, {"start": _step_result("start")}, finished_at=NOON,
                             status="failed", error="No", current="run")
    child = await _flow_run(db, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                            finished_at=NOON, status="failed", error="No", parent=parent)
    return parent, child


@pytest.mark.asyncio
async def test_retrying_a_parent_replaces_the_failed_child_it_started_again(db_session):
    # The retry starts a new child lineage for the same item: one item, one unit.
    ws = await _workspace(db_session)
    parent, child = await _failed_family(db_session, ws)
    parent.step_results = {**parent.step_results, "run": _step_result(
        "run", "failed", error="No", subrun_id=child.id)}
    retry = await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                            attempt=2, lineage_root=parent.id)
    new_child = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=retry)
    retry.step_results = {**retry.step_results, "run": _step_result("run", subrun_id=new_child.id)}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 0, 0)
    assert await _flows(db_session, ws.id) == (2, 2)


@pytest.mark.asyncio
async def test_a_failed_child_still_counts_while_a_parent_retry_keeps_or_has_not_replaced_it(db_session):
    # A foreach retry keeps a failed item it does not restart, and fails on it again.
    ws = await _workspace(db_session)
    parent, child = await _failed_family(db_session, ws, parent_steps=FOREACH_FLOW)
    items = [{"key": "a", "status": "failed", "subrun_id": child.id}]
    parent.step_results = {**parent.step_results, "run": _step_result("run", "failed", error="No", items=items)}
    await _flow_run(db_session, ws, FOREACH_FLOW, {"start": _step_result("start"), "run": _step_result(
        "run", "failed", error="No", items=items)}, finished_at=NOON, status="failed", error="No",
        current="run", attempt=2, lineage_root=parent.id)
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (0, 1, 0)

    # A retry that has not reached the step yet names no child.
    ws = await _workspace(db_session)
    parent, child = await _failed_family(db_session, ws)
    parent.step_results = {**parent.step_results, "run": _step_result(
        "run", "failed", error="No", subrun_id=child.id)}
    await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")}, finished_at=None,
                    status="running", attempt=2, lineage_root=parent.id)
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (0, 1, 0)


@pytest.mark.parametrize(
    ("child_is_a_retry", "slot"),
    [
        # A subworkflow step that handled the failure (``on_error``) is inherited as done.
        pytest.param(False, lambda child, origin: {"subrun_id": child, "continued": True}, id="subrun-names-this-run"),
        pytest.param(True, lambda child, origin: {"subrun_id": origin, "continued": True}, id="subrun-names-its-origin"),
        # A foreach keeps an item it does not restart.
        pytest.param(False, lambda child, origin: {"items": [{"key": "a", "status": "failed", "subrun_id": child}]},
                     id="items-name-this-run"),
        pytest.param(True, lambda child, origin: {"items": [{"key": "a", "status": "failed", "subrun_id": origin}]},
                     id="items-name-its-origin"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_child_the_newest_parent_attempt_still_names_is_not_replaced(db_session, child_is_a_retry, slot):
    ws = await _workspace(db_session)
    parent, origin = await _failed_family(db_session, ws)
    child = origin
    if child_is_a_retry:
        # A person retried the child itself and it failed again; a retry keeps its parent.
        child = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                                finished_at=NOON, status="failed", error="No", parent=parent,
                                attempt=2, lineage_root=origin.id)
    await _flow_run(db_session, ws, PARENT_FLOW, {"run": _step_result("run", **slot(child.id, origin.id))},
                    finished_at=NOON, attempt=2, lineage_root=parent.id)
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (0, 1, 0)


@pytest.mark.asyncio
async def test_a_failed_retry_of_a_reconciled_child_still_counts(db_session):
    # p1 fails on c1; a person retries the child and c2 completes, so reconciliation
    # creates p2 naming c2. Retrying c2 as a revision gives c3, which fails. It keeps
    # parent p1 and p2 is newest, but p2's name for c2 must not read as replacing it.
    ws = await _workspace(db_session)
    p1, c1 = await _failed_family(db_session, ws)
    p1.step_results = {**p1.step_results, "run": _step_result("run", "failed", error="No", subrun_id=c1.id)}
    c2 = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON,
                         attempt=2, lineage_root=c1.id, parent=p1)
    await _flow_run(db_session, ws, PARENT_FLOW, {
        "start": _step_result("start"), "run": _step_result("run", subrun_id=c2.id, resumed=True),
        "done": _step_result("done"),
    }, finished_at=NOON, attempt=2, lineage_root=p1.id)
    await _flow_run(db_session, ws, AGENT_FLOW, {
        "start": _step_result("start"), "make": _step_result("make", "failed", error="No"),
    }, finished_at=NOON, status="failed", error="No", attempt=3, lineage_root=c1.id, parent=p1,
        trigger_data={"_workflow_revision": {"of_run_id": c2.id}})
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (0, 1, 0)
    assert await _flows(db_session, ws.id) == (2, 3)


@pytest.mark.asyncio
async def test_a_completed_child_is_not_replaced_by_a_parent_retry(db_session):
    ws = await _workspace(db_session)
    parent = await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                             status="failed", error="Later step", current="start")
    await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=parent)
    retry = await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                            attempt=2, lineage_root=parent.id)
    new_child = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=retry)
    retry.step_results = {**retry.step_results, "run": _step_result("run", subrun_id=new_child.id)}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (2, 0, 0)


@pytest.mark.asyncio
async def test_three_parent_attempts_read_the_newest_for_replaced_children(db_session):
    # Attempt 2 failed before reaching the step, so only attempt 3 shows the item
    # was started again. Reading the oldest retry would keep the first failed child.
    ws = await _workspace(db_session)
    first, first_child = await _failed_family(db_session, ws)
    first.step_results = {**first.step_results, "run": _step_result(
        "run", "failed", error="No", subrun_id=first_child.id)}
    await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start", "failed", error="No")},
                    finished_at=NOON, status="failed", error="No", current="start", attempt=2, lineage_root=first.id)
    third = await _flow_run(db_session, ws, PARENT_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                            attempt=3, lineage_root=first.id)
    third_child = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON, parent=third)
    third.step_results = {**third.step_results, "run": _step_result("run", subrun_id=third_child.id)}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 0, 0)
    assert await _flows(db_session, ws.id) == (2, 3)


@pytest.mark.asyncio
async def test_a_completed_parent_with_its_own_work_and_a_failed_item_counts_too(db_session):
    # ``continue_on_failure``: finishing with a failed item is not stopping on it.
    ws = await _workspace(db_session)
    steps = {"start": "trigger", "make": "agent", "run": "foreach_subworkflow", "done": "end"}
    parent = await _flow_run(db_session, ws, steps, ["start", "make"], finished_at=NOON, current="run")
    failed = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                             finished_at=NOON, status="failed", error="No", parent=parent)
    parent.step_results = {**parent.step_results, "run": _step_result(
        "run", items=[{"key": "a", "status": "failed", "subrun_id": failed.id}], failed_count=1)}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 1, 0)


@pytest.mark.asyncio
async def test_a_foreach_that_failed_on_a_cancelled_item_leaves_the_item_as_the_unit(db_session):
    ws = await _workspace(db_session)
    parent = await _flow_run(db_session, ws, FOREACH_FLOW, {"start": _step_result("start")}, finished_at=NOON,
                             status="failed", error="Stopped", current="run")
    cancelled = await _flow_run(db_session, ws, AGENT_FLOW, ["start"], finished_at=NOON, status="cancelled",
                                cancel_source="user", current="make", parent=parent)
    parent.step_results = {**parent.step_results, "run": _step_result(
        "run", "failed", error="Stopped", items=[{"key": "a", "status": "cancelled", "subrun_id": cancelled.id}])}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (0, 0, 1)


@pytest.mark.asyncio
async def test_a_snapshot_node_without_an_id_does_not_hide_a_work_step(db_session):
    # A NULL among the control step ids would make ``<> ALL`` NULL for every step.
    ws = await _workspace(db_session)
    run = await _flow_run(db_session, ws, AGENT_FLOW, ["start", "make", "done"], finished_at=NOON)
    nodes = run.definition_snapshot["nodes"]
    run.definition_snapshot = {**run.definition_snapshot, "nodes": [*nodes, {"type": "condition"}]}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    assert await _work(db_session, ws.id) == (1, 0, 0)


@pytest.mark.asyncio
async def test_an_idle_workers_day_does_not_hide_items_that_all_fail(db_session):
    # The incident: a */15 worker's ticks read as successes, so a release failing
    # every real item still looked healthy on the task + workflow sum.
    ws = await _workspace(db_session)
    worker = _worker_steps()
    for _ in range(92):
        await _flow_run(db_session, ws, worker, IDLE_RESULTS, finished_at=NOON)
    for _ in range(4):
        tick = await _flow_run(db_session, ws, worker, {"start": _step_result("start")}, finished_at=NOON,
                               status="failed", error="No", current="run")
        item = await _flow_run(db_session, ws, AGENT_FLOW, {"make": _step_result("make", "failed", error="No")},
                               finished_at=NOON, status="failed", error="No", parent=tick)
        tick.step_results = {**tick.step_results, "run": _step_result(
            "run", "failed", error="No", subrun_id=item.id)}
    await _tasks(db_session, ws, [("completed", None, 10), ("failed", None, None)], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY)

    work = await _row(db_session, ws.id, "work")
    assert (work.succeeded, work.failed) == (1, 5)
    assert success_rate(work.succeeded, work.failed) == 0.1667
    task, flows = await _row(db_session, ws.id, "task"), await _row(db_session, ws.id, "workflow")
    assert (flows.succeeded, flows.failed) == (92, 8)
    old_sum = success_rate(task.succeeded + flows.succeeded, task.failed + flows.failed)
    assert old_sum == 0.9118


@pytest.mark.asyncio
async def test_rerolling_keeps_a_past_days_version_and_today_follows_the_current_one(db_session):
    ws = await _workspace(db_session, blueprint=("builtin:weekly-report", "1.0.0"))
    await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON)
    await _feedback(db_session, ws, ["up"], at=NOON)
    today = NOON + timedelta(days=1)
    await _tasks(db_session, ws, [("completed", None, 10)], finished_at=today)
    await rollup_workspace_day(db_session, day=DAY, now=today)
    await rollup_workspace_day(db_session, day=today.date(), now=today)

    ws.settings = {"sandbox": True, "_blueprint": {"blueprint_id": "builtin:weekly-report", "blueprint_version": "2.0.0"}}
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY, now=today)
    await rollup_workspace_day(db_session, day=today.date(), now=today)

    dimensions = (await db_session.execute(select(
        MetricsWorkspaceDaily.day, MetricsWorkspaceDaily.surface,
        MetricsWorkspaceDaily.blueprint_version, MetricsWorkspaceDaily.mode,
    ).where(MetricsWorkspaceDaily.workspace_id == ws.id))).all()
    assert {(row.surface, row.blueprint_version, row.mode) for row in dimensions if row.day == DAY} == {
        ("task", "1.0.0", "live"), ("work", "1.0.0", "live"), ("workspace", "1.0.0", "live"),
    }
    assert {(row.surface, row.blueprint_version, row.mode) for row in dimensions if row.day == today.date()} == {
        ("task", "2.0.0", "simulation"), ("work", "2.0.0", "simulation"),
    }


@pytest.mark.asyncio
async def test_the_first_rollup_of_a_past_day_uses_current_settings(db_session):
    # Nothing was recorded for that day, so the Workspace's settings are the best evidence.
    ws = await _workspace(db_session, blueprint=("builtin:weekly-report", "1.0.0"))
    await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY, now=NOON + timedelta(days=3))
    assert (await _row(db_session, ws.id, "work")).blueprint_version == "1.0.0"
