"""An explicit scheduler run-now is independent of Workspace autonomy."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from packages.core.constants.execution import ScheduledChildAdmissionStatus, ScheduledDispatchKind
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.user import Entity, User
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.scheduled_run_lifecycle import admit_scheduled_child
from packages.core.services.task_execution_origin import task_has_manual_execution
from packages.core.tasks import scheduler_tasks


async def _manual_parent(state, key):
    run = ScheduledJobRun(
        job_id=state.job.job_id, status="running", started_at=state.now,
        trigger_type="manual", idempotency_key=key,
        result={
            "manual_request": {"version": 1, "user_id": state.actor.id},
            "dispatch": {
                "kind": "scheduler_parent",
                "args": [state.job.id, state.now.isoformat()],
                "kwargs": {"manual": True, "occurrence_key": key},
            },
            "dispatch_status": "prepared",
            "dispatch_ledger": {"revision": state.job.revision},
        },
    )
    state.db.add(run)
    await state.db.flush()
    return run


@pytest_asyncio.fixture
async def manual_schedule(db_session, monkeypatch):
    now = datetime.now(UTC)
    entity = Entity(id=generate_ulid(), name="Manual schedule")
    actor = User(
        entity_id=entity.id,
        email=f"manual-schedule-{generate_ulid()}@test.example",
        password_hash="unused",
        role="owner",
        status="active",
    )
    workspace = Workspace(
        entity_id=entity.id, name="Paused", status="paused", settings={},
    )
    db_session.add_all([entity, actor, workspace])
    await db_session.flush()
    # Entity attribution alone is not a Workspace management grant.
    db_session.add(WorkspaceStaff(
        workspace_id=workspace.id, staff_id=None, user_id=actor.id,
        role="owner", status="active",
    ))
    job = ScheduledJob(
        job_id=f"manual:{generate_ulid()}",
        entity_id=entity.id,
        workspace_id=workspace.id,
        user_id=actor.id,
        name="Run one task",
        execution_type="agent",
        agent_id="manor-master",
        payload_message="Make a report",
        schedule_kind="every",
        every_seconds=3600,
        enabled=False,
        last_run_at=now - timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.flush()
    monkeypatch.setattr(scheduler_tasks, "_preflight_scheduled_job_credits", AsyncMock())
    return SimpleNamespace(db=db_session, actor=actor, workspace=workspace, job=job, now=now)


@pytest.mark.asyncio
async def test_manual_disabled_schedule_dispatches_exact_child_without_resuming(manual_schedule):
    state = manual_schedule
    original_clock = state.job.last_run_at
    await _manual_parent(state, "manual:one-operator-request")
    dispatch, run_id = await scheduler_tasks._dispatch_job(
        state.db, state.job, state.now, manual=True,
        occurrence_key="manual:one-operator-request",
    )
    task = await state.db.get(Task, dispatch["args"][0])
    run = await state.db.get(ScheduledJobRun, run_id)

    assert state.job.manor_task_id == task.id
    assert task_has_manual_execution(task)
    assert task.details["_manual_execution"]["user_id"] == state.actor.id
    assert task.details["scheduled_run_id"] == run_id
    assert run.trigger_type == "manual"
    admission = await admit_scheduled_child(
        state.db, run_id=run_id, job_id=state.job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK, child_id=task.id,
    )
    assert admission.status is ScheduledChildAdmissionStatus.ADMITTED
    assert state.workspace.status == "paused"
    assert state.job.enabled is False
    assert state.job.last_run_at == original_clock


@pytest.mark.asyncio
async def test_automatic_schedule_still_skips_paused_workspace(manual_schedule):
    state = manual_schedule
    state.job.enabled = True
    await state.db.flush()
    result = await scheduler_tasks._dispatch_job(
        state.db, state.job, state.now, manual=False,
        occurrence_key="every:automatic-occurrence",
    )
    from sqlalchemy import select

    run = await state.db.scalar(select(ScheduledJobRun).where(
        ScheduledJobRun.job_id == state.job.job_id,
    ))
    assert result is None
    assert run.status == "skipped"
    assert run.result["reason"] == "workspace_paused"
    scheduler_tasks._preflight_scheduled_job_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_workflow_schedule_runs_while_workspace_is_paused(
    manual_schedule,
    monkeypatch,
):
    from packages.core.services import workflow_service

    state = manual_schedule
    state.job.execution_type = "workflow"
    state.job.execution_target = {"workflow_id": generate_ulid()}
    state.job.enabled = True
    await state.db.flush()
    start = AsyncMock(return_value=SimpleNamespace(id=generate_ulid()))
    monkeypatch.setattr(workflow_service, "start_workflow", start)

    dispatch, _run_id = await scheduler_tasks._dispatch_job(
        state.db,
        state.job,
        state.now,
        manual=False,
        occurrence_key="every:independent-workflow-occurrence",
    )

    assert dispatch["kind"] == "workflow"
    assert start.await_args.kwargs["trigger_source"] == "schedule"
    assert start.await_args.kwargs["workspace_id"] == state.workspace.id
    scheduler_tasks._preflight_scheduled_job_credits.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_type,key", [("cron", "manual:request"), ("manual", "cron:request")])
async def test_disabled_child_rejects_incomplete_manual_origin(manual_schedule, trigger_type, key):
    state = manual_schedule
    run = ScheduledJobRun(
        job_id=state.job.job_id, status="running", started_at=state.now,
        trigger_type=trigger_type, idempotency_key=key,
    )
    state.db.add(run)
    await state.db.flush()

    admission = await admit_scheduled_child(state.db, run_id=run.id, job_id=state.job.job_id)
    assert admission.status is ScheduledChildAdmissionStatus.CANCELLED
    assert admission.reason == "scheduled_job_disabled"


@pytest.mark.asyncio
async def test_manual_strategist_schedule_keeps_human_requested_trigger(manual_schedule):
    state = manual_schedule
    state.job.execution_type = "strategist_review"
    await state.db.flush()
    await _manual_parent(state, "manual:review")
    dispatch, run_id = await scheduler_tasks._dispatch_job(
        state.db, state.job, state.now, manual=True, occurrence_key="manual:review",
    )
    assert dispatch["kind"] == "strategist_review"
    assert dispatch["kwargs"]["trigger_kind"] == "human_requested"
    admission = await admit_scheduled_child(state.db, run_id=run_id, job_id=state.job.job_id)
    assert admission.admitted


@pytest.mark.asyncio
async def test_manual_scheduled_dispatch_worker_accepts_disabled_job(manual_schedule, monkeypatch):
    from packages.core import database

    state = manual_schedule

    class ExistingSession:
        async def __aenter__(self):
            return state.db

        async def __aexit__(self, *_args):
            return False

    dispatch = AsyncMock(return_value=None)
    monkeypatch.setattr(database, "create_worker_session", lambda: ExistingSession)
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", dispatch)
    await _manual_parent(state, "manual:request")
    await scheduler_tasks._async_dispatch_single(
        state.job.id, state.now.isoformat(), manual=True, occurrence_key="manual:request",
    )
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["manual"] is True
    dispatch.reset_mock()
    await scheduler_tasks._async_dispatch_single(
        state.job.id, state.now.isoformat(), occurrence_key="every:automatic",
    )
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual,revoked", [(False, False), (True, False), (True, True)])
async def test_goal_measurement_child_uses_durable_manual_origin(manual_schedule, monkeypatch, manual, revoked):
    from packages.core import database
    from packages.core.ai import runtime
    from packages.core.goals import measurement
    from packages.core.tasks import ai_tasks

    state = manual_schedule
    goal_id = generate_ulid()
    state.job.execution_type = "goal_measurement"
    state.job.execution_target = {"goal_id": goal_id}
    run = ScheduledJobRun(
        job_id=state.job.job_id, status="running", started_at=state.now,
        trigger_type="manual" if manual else "every",
        idempotency_key="manual:measure" if manual else "every:measure",
        result={"manual_request": {"version": 1, "user_id": state.actor.id}} if manual else {},
    )
    state.db.add(run)
    if revoked:
        state.actor.status = "disabled"
    await state.db.flush()

    class ExistingSession:
        async def __aenter__(self):
            return state.db

        async def __aexit__(self, *_args):
            return False

    billing_scopes = []

    async def collect(*_args, **_kwargs):
        billing_scopes.append(runtime.runtime_current_billing_context())
        return {"goal_id": goal_id}

    measure = AsyncMock(side_effect=collect)
    monkeypatch.setattr(database, "create_worker_session", lambda: ExistingSession)
    monkeypatch.setattr(runtime, "runtime_ensure_goal_measurement_billing_context", AsyncMock())
    monkeypatch.setattr(measurement, "measure_goal", measure)
    monkeypatch.setattr(ai_tasks, "_run_scheduled", lambda _self, _label, _id, body, **_kw: body())

    if revoked:
        with pytest.raises(ValueError, match="no longer authorized"):
            await ai_tasks.run_goal_measurement.run(goal_id, run_id=run.id, job_id_str=state.job.job_id)
        measure.assert_not_awaited()
        return
    await ai_tasks.run_goal_measurement.run(goal_id, run_id=run.id, job_id_str=state.job.job_id)
    measure.assert_awaited_once_with(
        goal_id, require_autonomous_runtime=not manual,
    )
    if manual:
        assert billing_scopes[0].user_id == state.actor.id
        assert billing_scopes[0].workspace_id == state.workspace.id
        assert billing_scopes[0].source == "goal_measurement"


@pytest.mark.asyncio
async def test_manual_workflow_schedule_keeps_manual_launch_origin(manual_schedule, monkeypatch):
    from packages.core.services import workflow_service

    state = manual_schedule
    state.job.execution_type = "workflow"
    state.job.execution_target = {"workflow_id": generate_ulid()}
    await state.db.flush()
    start = AsyncMock(return_value=SimpleNamespace(id=generate_ulid()))
    monkeypatch.setattr(workflow_service, "start_workflow", start)
    await _manual_parent(state, "manual:flow")

    dispatch, _run_id = await scheduler_tasks._dispatch_job(
        state.db, state.job, state.now, manual=True, occurrence_key="manual:flow",
    )
    assert dispatch["kind"] == "workflow"
    assert start.await_args.kwargs["trigger_source"] == "manual"
    assert start.await_args.kwargs["workspace_id"] == state.workspace.id


@pytest.mark.asyncio
async def test_blueprint_clock_override_does_not_authorize_paused_execution(manual_schedule):
    state = manual_schedule
    result = await scheduler_tasks._dispatch_job(
        state.db, state.job, state.now, manual=True, occurrence_key="blueprint:startup",
    )
    assert result is None
    scheduler_tasks._preflight_scheduled_job_credits.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_type", ["briefing", "outcome_evaluation", "chat_insight_extraction"])
async def test_workspace_maintenance_child_uses_exact_manual_run(manual_schedule, monkeypatch, execution_type):
    from packages.core import briefing, database
    from packages.core.ai import runtime
    from packages.core.memory import chat_extractor
    from packages.core.strategist import evaluation
    from packages.core.tasks import ai_tasks

    state = manual_schedule
    state.job.execution_type = execution_type
    run = ScheduledJobRun(
        job_id=state.job.job_id, status="running", started_at=state.now,
        trigger_type="manual", idempotency_key="manual:maintenance",
        result={"manual_request": {"version": 1, "user_id": state.actor.id}},
    )
    state.db.add(run)
    await state.db.flush()

    class ExistingSession:
        async def __aenter__(self):
            return state.db

        async def __aexit__(self, *_args):
            return False

    routes = {
        "briefing": (briefing, "generate_briefing", "morning_briefing"),
        "outcome_evaluation": (evaluation, "evaluate_workspace_outcomes", "outcome_evaluation"),
        "chat_insight_extraction": (chat_extractor, "extract_chat_insights", "chat_insight_extraction"),
    }
    module, method_name, task_name = routes[execution_type]
    body = AsyncMock(return_value={"done": True})
    monkeypatch.setattr(module, method_name, body)
    monkeypatch.setattr(runtime, f"runtime_ensure_{task_name}_billing_context", AsyncMock())
    monkeypatch.setattr(database, "create_worker_session", lambda: ExistingSession)
    monkeypatch.setattr(ai_tasks, "_run_scheduled", lambda _self, _label, _id, body, **_kw: body())

    assert not await ai_tasks._scheduled_workspace_run_is_manual(
        state.db, workspace_id=generate_ulid(), run_id=run.id, job_id_str=state.job.job_id,
    )
    await getattr(ai_tasks, f"run_{task_name}").run(
        state.workspace.id, run_id=run.id, job_id_str=state.job.job_id,
    )
    assert body.await_args.kwargs["require_autonomous_runtime"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,parent_manual", [("workspace_operator", False), ("background_worker", False), ("background_worker", True), (None, False)])
async def test_scheduler_tool_only_records_trusted_manual_requests(manual_schedule, monkeypatch, profile, parent_manual):
    from sqlalchemy import select
    from packages.core import database
    from packages.core.ai.tools.cron_tools import _run_job_now_handler
    from packages.core.services.task_execution_origin import record_manual_task_execution

    state = manual_schedule
    parent = Task(
        entity_id=state.actor.entity_id, workspace_id=state.workspace.id,
        title="Parent", creator_id=state.actor.id,
        details={"strategist_review_id": generate_ulid()},
    )
    if parent_manual:
        record_manual_task_execution(parent, user_id=state.actor.id, action="start")
    state.db.add(parent)
    await state.db.commit()

    class ExistingSession:
        async def __aenter__(self):
            return state.db
        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(database, "async_session", ExistingSession)
    monkeypatch.setattr(database, "create_worker_session", lambda: ExistingSession)
    published = []
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", published.append)
    job_id, actor_id = state.job.job_id, state.actor.id
    await _run_job_now_handler(
        entity_id=state.actor.entity_id, job_id=state.job.job_id,
        _user_id_from_context=state.actor.id, workspace_id=state.workspace.id,
        task_id=parent.id if profile == "background_worker" else None,
        _runtime_envelope_from_context=SimpleNamespace(
            profile=profile, task_id=parent.id if profile == "background_worker" else None,
        ) if profile else None,
    )
    should_run = profile == "workspace_operator" or parent_manual
    runs = list((await state.db.execute(select(ScheduledJobRun).where(
        ScheduledJobRun.job_id == job_id,
    ))).scalars())
    assert len(published) == len(runs) == int(should_run)
    if should_run:
        assert runs[0].result["manual_request"]["user_id"] == actor_id
    await state.db.refresh(state.job)
    assert state.job.enabled is False


@pytest.mark.asyncio
async def test_manual_api_persists_requester_for_ownerless_goal_job(manual_schedule, monkeypatch):
    from sqlalchemy import select
    from apps.api.routers.scheduler import run_job_now
    from packages.core import database
    from packages.core.ai import runtime
    from packages.core.goals import measurement
    from packages.core.tasks import ai_tasks

    state = manual_schedule
    state.job.user_id = None
    state.job.execution_type = "goal_measurement"
    goal_id = generate_ulid()
    state.job.execution_target = {"goal_id": goal_id}
    await state.db.commit()
    original_clock = state.job.last_run_at
    actor_id, job_id = state.actor.id, state.job.job_id

    class ExistingSession:
        async def __aenter__(self):
            return state.db
        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(database, "create_worker_session", lambda: ExistingSession)
    published = []
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", published.append)
    await run_job_now(state.job.id, "ownerless-measure", state.actor, state.db)
    run = (await state.db.execute(select(ScheduledJobRun).where(
        ScheduledJobRun.job_id == job_id,
    ))).scalar_one()
    assert run.result["manual_request"]["user_id"] == actor_id
    billing_scopes = []

    async def collect(*_args, **_kwargs):
        billing_scopes.append(runtime.runtime_current_billing_context())
        return {"goal_id": goal_id}

    measure = AsyncMock(side_effect=collect)
    monkeypatch.setattr(runtime, "runtime_ensure_goal_measurement_billing_context", AsyncMock())
    monkeypatch.setattr(measurement, "measure_goal", measure)
    monkeypatch.setattr(ai_tasks, "_run_scheduled", lambda _self, _label, _id, body, **_kw: body())
    await ai_tasks.run_goal_measurement.run(goal_id, run_id=run.id, job_id_str=job_id)
    measure.assert_awaited_once_with(goal_id, require_autonomous_runtime=False)
    assert billing_scopes[0].user_id == actor_id
    assert billing_scopes[0].workspace_id == state.workspace.id
    assert billing_scopes[0].source == "goal_measurement"
    await state.db.refresh(state.job)
    assert state.job.user_id is None
    assert state.job.enabled is False
    assert state.job.last_run_at == original_clock


@pytest.mark.asyncio
async def test_manual_parent_config_change_cancels_without_child(manual_schedule):
    state = manual_schedule
    run = await _manual_parent(state, "manual:edited")
    state.job.revision += 1
    await state.db.flush()
    assert await scheduler_tasks._dispatch_job(
        state.db, state.job, state.now, manual=True, occurrence_key=run.idempotency_key,
    ) is None
    assert run.status == "cancelled"
    assert run.result["reason"] == "scheduled_job_revision_changed"


@pytest.mark.asyncio
async def test_composite_scheduler_tool_preserves_innermost_flow_context(monkeypatch):
    from packages.core import database
    from packages.core.ai.tools import manor_tool

    class Session:
        async def __aenter__(self):
            return object()
        async def __aexit__(self, *_args):
            return False

    action = AsyncMock(return_value="queued")
    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(manor_tool, "runtime_manor_run_scheduled_job_now", action)
    assert await manor_tool._manor_handler(
        entity_id="entity", action="run_scheduled_job_now", params={"job_id": "job"},
        _user_id_from_context="actor", _workflow_run_id_from_context="flow",
        _runtime_envelope_from_context=SimpleNamespace(profile="workflow_step"),
        workspace_id="workspace", task_id="parent-task",
    ) == "queued"
    context = action.await_args.kwargs["runtime_kwargs"]
    assert context["_workflow_run_id_from_context"] == "flow"
    assert context["task_id"] == "parent-task"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_can_manage", [True, False])
@pytest.mark.parametrize("flow_manual,real_task,forged_task", [(False, True, False), (True, False, False), (None, False, True)])
async def test_composite_scheduler_checks_real_flow_before_task_provenance(
    monkeypatch, flow_manual, real_task, forged_task, actor_can_manage,
):
    from packages.core import database
    from packages.core.ai.tools import manor_tool

    job = SimpleNamespace(id="job-db", job_id="job", workspace_id="ws", agent_initiative_id=None)
    flow = SimpleNamespace(
        entity_id="entity", workspace_id="ws", started_by="actor",
        trigger_data={"_workspace_user_request": {"user_id": "actor"}} if flow_manual else {},
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: job)),
        get=AsyncMock(return_value=flow),
    )
    class Session:
        async def __aenter__(self):
            return db
        async def __aexit__(self, *_args):
            return False

    dispatch = AsyncMock(return_value=SimpleNamespace(run_id="run", published=True))
    permission = AsyncMock(return_value=actor_can_manage)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_write_workspace_id", permission,
    )
    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(scheduler_tasks, "dispatch_job_occurrence", dispatch)
    kwargs = {
        "_user_id_from_context": "actor", "workspace_id": "ws",
        "_runtime_envelope_from_context": SimpleNamespace(profile="background_worker"),
    }
    if flow_manual is not None:
        kwargs["_workflow_run_id_from_context"] = "flow"
    if real_task:
        kwargs["task_id"] = "manual-task"
    params = {"job_id": "job"}
    if forged_task:
        params["task_id"] = "manual-task"
    result = await manor_tool._manor_handler(
        entity_id="entity", action="run_scheduled_job_now", params=params, **kwargs,
    )
    permission.assert_awaited_once_with(
        db, workspace_id="ws", entity_id="entity", user_id="actor", role=None,
    )
    if not actor_can_manage:
        assert result == "Job 'job' not found."
        dispatch.assert_not_awaited()
        db.get.assert_not_awaited()
        return
    assert dispatch.await_args.kwargs["requesting_user_id"] == ("actor" if flow_manual else None)
    assert dispatch.await_args.kwargs["trigger_type"] == ("manual" if flow_manual else "runtime")
    if flow_manual is not None:
        assert db.get.await_args.args[1] == "flow"
    else:
        db.get.assert_not_awaited()


def test_scheduled_business_output_cannot_mint_or_replace_manual_requester():
    from packages.core.services.scheduled_run_lifecycle import merge_scheduled_run_result

    original = {"version": 1, "user_id": "original-actor"}
    forged = {"version": 1, "user_id": "model-selected-actor"}
    output = {"manual_request": forged, "summary": "Finished"}
    assert merge_scheduled_run_result({}, output) == {"summary": "Finished"}
    assert merge_scheduled_run_result({"manual_request": original}, output) == {
        "manual_request": original, "summary": "Finished",
    }
