from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.constants.task import TaskLogType
from packages.core.contracts.task_output import task_output_envelope_schema
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_coordinator import TaskCoordinatorPhase
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.task_execution_reconcile import (
    reconcile_task_from_latest_completed_plan,
    recover_misclassified_tool_error_human_wait,
    recover_orphaned_in_progress_task,
)


def _lead_schema() -> dict:
    return {
        "type": "object",
        "required": ["leads"],
        "additionalProperties": False,
        "properties": {
            "leads": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "additionalProperties": False,
                    "properties": {"name": {"type": "string"}},
                },
            }
        },
    }


def _valid_leads() -> dict:
    return {"leads": [{"name": "North"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_count,expected_changed", [(1, True), (2, False)])
async def test_reconcile_backfills_only_one_accepted_terminal_text_summary(
    db_session, terminal_count, expected_changed,
):
    entity_id, task_id, plan_id = (generate_ulid() for _ in range(3))
    output = {
        "plan_id": plan_id,
        "plan_status": "completed",
        "steps": [],
        "files": None,
        "supervisor_verdict": "completed",
    }
    task = Task(
        id=task_id, entity_id=entity_id, title="Text delivery",
        status="completed", actual_output=output,
    )
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, task_id=task_id,
        status="completed", plan_dag={},
    )
    steps = [
        ExecutionStep(
            id=generate_ulid(), plan_id=plan_id, entity_id=entity_id,
            step_key=f"deliver_{index}", kind="llm", depends_on=[],
            step_status="done", result={"text": f"Result {index}"},
        )
        for index in range(terminal_count)
    ]
    db_session.add_all([task, plan, *steps])
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is expected_changed
    assert task.actual_output.get("summary") == ("Result 0" if expected_changed else None)
    assert task.actual_output["supervisor_verdict"] == "completed"
    assert await reconcile_task_from_latest_completed_plan(db_session, task) is False


@pytest.mark.asyncio
async def test_reconcile_does_not_complete_task_from_older_plan_while_new_plan_runs(
    db_session,
):
    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    completed_plan_id = generate_ulid()
    running_plan_id = generate_ulid()

    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Retry in progress",
        status="in_progress",
        actual_output=None,
    )
    db_session.add(task)
    db_session.add_all(
        [
            ExecutionPlan(
                id=completed_plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
                created_at=now,
                completed_at=now,
            ),
            ExecutionPlan(
                id=running_plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="running",
                plan_dag={},
                created_at=now + timedelta(seconds=1),
                started_at=now + timedelta(seconds=1),
            ),
        ]
    )
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=completed_plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="publish",
            kind="subagent",
            step_status="done",
            result={"summary": "Older run finished."},
        )
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "in_progress"
    assert task.actual_output is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acceptance",
    [
        None,
        {"verdict": "accepted", "snapshot_digest": "digest", "checks": []},
        {"verdict": "changes_required", "snapshot_digest": "digest", "checks": [{"criterion": "x"}]},
    ],
)
async def test_reconcile_v3_never_completes_without_persisted_content_acceptance(
    db_session, acceptance,
):
    entity_id, task_id, plan_id = (generate_ulid() for _ in range(3))
    task = Task(
        id=task_id, entity_id=entity_id, title="V3 delivery",
        status="in_progress", actual_output=None,
    )
    plan_state = {"task_coordinator_version": 3}
    if acceptance is not None:
        plan_state["task_content_acceptance"] = acceptance
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, task_id=task_id,
        status="completed", plan_dag={}, dispatcher_state=plan_state,
    )
    db_session.add_all([
        task, plan,
        ExecutionStep(
            id=generate_ulid(), plan_id=plan_id, entity_id=entity_id,
            step_key="deliver", kind="llm", depends_on=[],
            step_status="done", result={"text": "Saved output"},
        ),
        TaskLog(
            id=generate_ulid(), task_id=task_id,
            log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
            content="Task accepted", meta={"plan_id": plan_id, "verdict": "completed"},
        ),
    ])
    await db_session.commit()

    assert await reconcile_task_from_latest_completed_plan(db_session, task) is False
    assert task.status == "in_progress"
    assert task.actual_output is None


@pytest.mark.asyncio
@pytest.mark.parametrize("has_manifest", [True, False])
async def test_reconcile_v3_repairs_only_persisted_accepted_delivery(db_session, has_manifest):
    from packages.core.contracts.artifact_delivery import artifact_delivery_step_binding
    entity_id, task_id, plan_id = (generate_ulid() for _ in range(3))
    task = Task(
        id=task_id, entity_id=entity_id, title="V3 accepted delivery",
        status="in_progress", actual_output=None,
    )
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, task_id=task_id,
        status="completed", plan_dag={},
        dispatcher_state={
            "task_coordinator_version": 3,
            "task_content_acceptance": {
                "snapshot_digest": "digest", "verdict": "accepted",
                "evidence": "The delivered output was checked.",
                "checks": [{
                    "criterion": "Deliver output", "pointer": "/steps/0/result/text",
                    "quote": "Saved output",
                }],
            },
        },
    )
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan_id, entity_id=entity_id,
        step_key="deliver", kind="llm", depends_on=[],
        step_status="done", result={"text": "Saved output"},
    )
    db_session.add_all([
        task, plan, step,
        TaskLog(
            id=generate_ulid(), task_id=task_id,
            log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
            content="Task accepted", meta={"plan_id": plan_id, "verdict": "completed"},
        ),
    ])
    await db_session.flush()
    if has_manifest:
        state = plan.dispatcher_state
        plan.dispatcher_state = {**state, "task_content_acceptance": {
            **state["task_content_acceptance"],
            "artifact_manifest": {"plan_id": plan_id, "steps": [{**artifact_delivery_step_binding(step), "refs": []}]},
        }}
    await db_session.commit()

    assert await reconcile_task_from_latest_completed_plan(db_session, task) is has_manifest
    if not has_manifest:
        assert task.status == "in_progress" and task.actual_output is None
        return
    assert task.status == "completed"
    assert task.actual_output["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_reconcile_does_not_revive_a_supervisor_rejected_plan(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    rejected_output = {
        "plan_id": plan_id,
        "plan_status": "completed",
        "steps": [{"key": "deliver", "kind": "subagent", "status": "done"}],
        "files": None,
        "supervisor_verdict": "needs_replan",
        "supervisor_evidence": "the output contract was not satisfied",
    }
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Rejected completed plan",
        status="failed",
        actual_output=rejected_output,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "failed"
    assert task.actual_output == rejected_output


@pytest.mark.asyncio
async def test_reconcile_honors_rejected_supervisor_log_when_output_is_stale(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    stale_accepted_output = {
        "plan_id": plan_id,
        "plan_status": "completed",
        "steps": [],
        "files": None,
        "supervisor_verdict": "completed",
        "supervisor_evidence": "stale acceptance",
    }
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Rejected plan with stale output",
        status="failed",
        actual_output=stale_accepted_output,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            TaskLog(
                id=generate_ulid(),
                task_id=task_id,
                log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
                content="Supervisor rejected the result.",
                meta={
                    "plan_id": plan_id,
                    "verdict": "needs_replan",
                    "evidence": "the deliverable is incomplete",
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "failed"
    assert task.actual_output == stale_accepted_output


@pytest.mark.asyncio
async def test_reconcile_restores_exact_validated_task_result(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Recover structured output",
        status="in_progress",
        expected_output=_lead_schema(),
        actual_output=None,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "recovered one lead",
                    "outputs": {"data": _valid_leads()},
                },
            ),
            TaskLog(
                id=generate_ulid(),
                task_id=task_id,
                log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
                content="Supervisor accepted the result.",
                meta={
                    "plan_id": plan_id,
                    "verdict": "completed",
                    "evidence": "deliver satisfied the contract",
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is True
    assert task.status == "completed"
    assert task.actual_output["result"] == _valid_leads()
    assert task.actual_output["result_step_key"] == "deliver"
    assert task.actual_output["result_contract_source"] == "task.expected_output"
    assert task.actual_output["supervisor_verdict"] == "completed"
    assert task.actual_output["supervisor_evidence"] == "deliver satisfied the contract"
    assert task.actual_output["reconciled_from_plan"] is True


@pytest.mark.asyncio
async def test_reconcile_requires_supervisor_acceptance_for_structured_result(
    db_session,
):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Do not bypass the supervisor",
        status="in_progress",
        expected_output=_lead_schema(),
        actual_output=None,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "one schema-valid lead",
                    "outputs": {"data": _valid_leads()},
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "in_progress"
    assert task.actual_output is None


@pytest.mark.asyncio
async def test_reconcile_does_not_complete_schema_task_without_valid_terminal_result(
    db_session,
):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Do not recover an invalid result",
        status="in_progress",
        expected_output=_lead_schema(),
        actual_output=None,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "claims success but violates the Task contract",
                    "outputs": {"data": {"leads": []}},
                },
            ),
            TaskLog(
                id=generate_ulid(),
                task_id=task_id,
                log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
                content="A stale review claimed acceptance.",
                meta={
                    "plan_id": plan_id,
                    "verdict": "completed",
                    "evidence": "stale acceptance",
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "in_progress"
    assert task.actual_output is None


@pytest.mark.asyncio
async def test_reconcile_upgrades_legacy_completed_output_with_exact_result(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    legacy_output = {
        "plan_id": plan_id,
        "plan_status": "completed",
        "steps": [
            {
                "key": "deliver",
                "kind": "subagent",
                "status": "done",
                "result_summary": "one validated lead",
            }
        ],
        "files": None,
        "supervisor_verdict": "completed",
        "supervisor_evidence": "deliver satisfied the contract",
    }
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Upgrade legacy structured output",
        status="completed",
        expected_output=_lead_schema(),
        actual_output=legacy_output,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "one validated lead",
                    "outputs": {"data": _valid_leads()},
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is True
    assert task.status == "completed"
    assert task.actual_output["result"] == _valid_leads()
    assert task.actual_output["result_step_key"] == "deliver"
    assert task.actual_output["result_contract_source"] == "task.expected_output"
    assert task.actual_output["supervisor_verdict"] == "completed"


def _plan(*, plan_id: str, entity_id: str, workspace_id: str | None, task_id: str, status: str) -> ExecutionPlan:
    return ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        status=status,
        plan_dag={},
    )


def _coordinated_scope(task: Task, *, service_key: str = "content") -> list[object]:
    assert task.workspace_id
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    task.owner_service_key = service_key
    task.owner_subscription_id = subscription_id
    return [
        Workspace(
            id=task.workspace_id,
            entity_id=task.entity_id,
            name="Orphan recovery",
        ),
        Agent(
            id=agent_id,
            entity_id=task.entity_id,
            name="Recovery agent",
            system_prompt="Recover this Task through the coordinated runtime.",
            status="active",
        ),
        AgentSubscription(
            id=subscription_id,
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            agent_id=agent_id,
            service_key=service_key,
            status="active",
        ),
    ]


@pytest.mark.asyncio
async def test_orphaned_in_progress_with_two_replanned_plans_fails_task(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Draft rights checklist",
        status="in_progress",
        details={"workspace_work_batch_id": generate_ulid()},
    )
    db_session.add_all([task, *_coordinated_scope(task)])
    db_session.add_all(
        [
            _plan(
                plan_id=generate_ulid(),
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="replanned",
            ),
            _plan(
                plan_id=generate_ulid(),
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="replanned",
            ),
        ]
    )
    await db_session.commit()

    action = await recover_orphaned_in_progress_task(db_session, task)

    assert action == "failed"
    assert task.status == "failed"
    assert task.details["_orphan_plan_recovery"]["reason"] == "replan_budget_exhausted"


@pytest.mark.asyncio
async def test_orphaned_in_progress_with_replan_budget_redispatches(db_session, monkeypatch):
    monkeypatch.setattr(
        "packages.core.services.task_execution.queue_task_execution_after_commit",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("V3 recovery must use the durable Task intent")
        ),
    )
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Draft campaign assets",
        status="in_progress",
        owner_service_key="content",
    )
    db_session.add_all([task, *_coordinated_scope(task)])
    db_session.add(
        _plan(
            plan_id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="replanned",
        )
    )
    await db_session.commit()

    action = await recover_orphaned_in_progress_task(db_session, task)

    assert action == "redispatched"
    assert task.status == "in_progress"
    assert task.details["_orphan_plan_recovery"]["action"] == "redispatched"
    assert task.details["_orphan_plan_recovery"]["runtime_kind"] == "coordinated_plan"
    from packages.core.services.task_execution import (
        TASK_EXECUTION_OUTBOX_EVENT,
        task_execution_intent_dedupe_key,
        task_execution_intent_generation,
    )

    event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.dedupe_key == task_execution_intent_dedupe_key(
                task_id, task_execution_intent_generation(task),
            ),
        )
    )).scalar_one()
    assert event.delivered_at is None

    # A concurrent/duplicate reconciliation sees the durable intent and does
    # not turn the just-staged recovery into a false planning_lost failure.
    assert await recover_orphaned_in_progress_task(db_session, task) is None
    assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_orphaned_in_progress_planning_lost_after_redispatch_fails(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Lost planning",
        status="in_progress",
        details={"_orphan_plan_recovery": {"action": "redispatched"}},
    )
    db_session.add_all([task, *_coordinated_scope(task)])
    await db_session.commit()

    action = await recover_orphaned_in_progress_task(db_session, task)

    assert action == "failed"
    assert task.status == "failed"
    assert task.details["_orphan_plan_recovery"]["reason"] == "planning_lost"


@pytest.mark.asyncio
async def test_orphaned_legacy_agent_uses_typed_after_commit_dispatch(db_session, monkeypatch):
    from packages.core.constants.task import TaskRuntimeKind
    from packages.core.services.task_execution import TaskExecutionDispatch

    queued: list[tuple[str, TaskExecutionDispatch]] = []
    monkeypatch.setattr(
        "packages.core.services.task_execution.queue_task_execution_after_commit",
        lambda _db, task_id, dispatch: queued.append((task_id, dispatch)),
    )
    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Recover legacy Agent task",
        status="in_progress",
        agent_id=generate_ulid(),
    )
    db_session.add_all([
        task,
        _plan(
            plan_id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=None,
            task_id=task.id,
            status="replanned",
        ),
    ])
    await db_session.commit()

    assert await recover_orphaned_in_progress_task(db_session, task) == "redispatched"
    assert len(queued) == 1
    assert queued[0][0] == task.id
    assert isinstance(queued[0][1], TaskExecutionDispatch)
    assert queued[0][1].runtime_kind is TaskRuntimeKind.LEGACY_AGENT
    assert queued[0][1].target_id == task.agent_id


@pytest.mark.asyncio
async def test_v3_coordinator_without_plan_is_not_orphaned(db_session, monkeypatch):
    from packages.core.services.task_coordinator_wakeup import ensure_task_coordinator_run
    from packages.core.services.task_execution import resolve_task_execution

    async def unexpected_stage(*args, **kwargs):
        raise AssertionError("an active V3 coordinator owns this planning interval")

    monkeypatch.setattr(
        "packages.core.services.task_execution.stage_task_execution_intent",
        unexpected_stage,
    )
    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=generate_ulid(),
        title="Coordinator is planning",
        status="in_progress",
    )
    db_session.add_all([task, *_coordinated_scope(task)])
    await db_session.flush()
    dispatch = await resolve_task_execution(db_session, task)
    assert dispatch.coordinator is not None
    run = await ensure_task_coordinator_run(
        db_session, task=task, selection=dispatch.coordinator,
    )
    run.phase = TaskCoordinatorPhase.WAITING_PLAN.value
    await db_session.commit()

    assert await recover_orphaned_in_progress_task(db_session, task) is None
    assert task.status == "in_progress"
    assert "_orphan_plan_recovery" not in (task.details or {})


@pytest.mark.asyncio
@pytest.mark.parametrize(("engine", "phase", "expected"), [
    ("agent_loop", "waiting_timer", "redispatched"),
    ("agent_loop", "waiting_job", "redispatched"),
    ("agent_loop", "running", "redispatched"),
    ("plan_dag", "running", None),
])
async def test_lost_start_after_resume_redispatches_only_a_rebindable_run(
    db_session, monkeypatch, engine, phase, expected,
):
    """A paused agent loop is rebound by the new lifecycle's start intent; if
    that intent is lost, reconcile re-stages it instead of stranding the Task."""
    from packages.core.services.task_coordinator_wakeup import ensure_task_coordinator_run
    from packages.core.services.task_execution import resolve_task_execution

    monkeypatch.setattr(
        "packages.core.services.task_coordinator_wakeup.initial_task_coordinator_engine",
        _engine(engine),
    )
    task = Task(
        id=generate_ulid(), entity_id=generate_ulid(), workspace_id=generate_ulid(),
        title="Long running Task", status="in_progress",
        status_changed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add_all([task, *_coordinated_scope(task)])
    await db_session.flush()
    dispatch = await resolve_task_execution(db_session, task)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=dispatch.coordinator)
    run.phase = phase
    # Paused and resumed: a new lifecycle generation the run is not bound to.
    task.status_changed_at = datetime.now(timezone.utc)
    await db_session.commit()

    assert await recover_orphaned_in_progress_task(db_session, task) == expected
    assert task.status == "in_progress"


def _engine(value: str):
    async def engine(_db, _task):
        return value

    return engine


@pytest.mark.asyncio
async def test_non_dispatchable_workspace_task_is_not_given_a_fabricated_plan_dispatch(
    db_session, monkeypatch,
):
    async def unexpected_stage(*args, **kwargs):
        raise AssertionError("manual Task has no coordinated runtime")

    monkeypatch.setattr(
        "packages.core.services.task_execution.stage_task_execution_intent",
        unexpected_stage,
    )
    monkeypatch.setattr(
        "packages.core.services.task_execution.queue_task_execution_after_commit",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("manual Task has no legacy Agent runtime")
        ),
    )
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Manual follow-up",
        status="in_progress",
    )
    db_session.add_all([
        task,
        Workspace(id=workspace_id, entity_id=entity_id, name="Manual work"),
        _plan(
            plan_id=generate_ulid(), entity_id=entity_id,
            workspace_id=workspace_id, task_id=task.id, status="replanned",
        ),
    ])
    await db_session.commit()

    assert await recover_orphaned_in_progress_task(db_session, task) is None
    assert "_orphan_plan_recovery" not in (task.details or {})


@pytest.mark.asyncio
async def test_in_progress_with_live_plan_is_not_recovered(db_session, monkeypatch):
    monkeypatch.setattr(
        "packages.core.services.task_execution.queue_task_execution_after_commit",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not redispatch")),
    )
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Running plan",
        status="in_progress",
    )
    db_session.add(task)
    db_session.add(
        _plan(
            plan_id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="running",
        )
    )
    await db_session.commit()

    action = await recover_orphaned_in_progress_task(db_session, task)

    assert action is None
    assert task.status == "in_progress"



def _hitl_request(*, entity_id: str, workspace_id: str, task_id: str, action_key: str, reason: str, payload: dict | None = None):
    from packages.core.models.hitl_request import HitlRequest

    return HitlRequest(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        action_key=action_key,
        risk_level="medium",
        origin_kind="task",
        origin_task_id=task_id,
        status="pending",
        dedup_key=f"{action_key}:{task_id}:{generate_ulid()}",
        reason=reason,
        matched_rule="task.needs_recovery",
        hitl_type="error",
        context={"task_title": "Repair pack"},
        payload=payload or {"why": reason},
    )


@pytest.mark.asyncio
async def test_misclassified_tool_error_human_wait_fails_task_and_expires_hitl(
    db_session, monkeypatch,
):
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.workspace import Workspace, WorkspaceWorkBatch
    from packages.core.tasks import ai_tasks

    calls = []

    class _FakeStrategistTask:
        @staticmethod
        def apply_async(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return None

    monkeypatch.setattr(ai_tasks, "run_strategist_review", _FakeStrategistTask)

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    batch_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Repair existing pack",
        status="waiting_on_customer",
        details={"workspace_work_batch_id": batch_id},
    )
    request = _hitl_request(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        action_key="task.recover",
        reason="patch_file returned unsupported_operation_for_file_type and authorization_receipt_mismatch",
    )
    db_session.add_all(
        [
            Workspace(id=workspace_id, entity_id=entity_id, name="Tool error wait"),
            task,
            request,
            WorkspaceWorkBatch(
                id=batch_id,
                workspace_id=workspace_id,
                entity_id=entity_id,
                summary="One-task wave",
                status="active",
                task_ids=[task_id],
                details={},
            ),
        ]
    )
    await db_session.commit()

    action = await recover_misclassified_tool_error_human_wait(db_session, task)

    assert action == "failed"
    assert task.status == "failed"
    refreshed = await db_session.get(HitlRequest, request.id)
    assert refreshed.status == "expired"
    assert refreshed.resolved_reason == "tool_error_not_human_required"
    batch = await db_session.get(WorkspaceWorkBatch, batch_id)
    assert batch.status == "completed"
    assert calls


@pytest.mark.asyncio
async def test_file_not_found_human_wait_is_not_recovered(db_session):
    from packages.core.models.hitl_request import HitlRequest

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Locate missing file",
        status="waiting_on_customer",
    )
    request = _hitl_request(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        action_key="task.recover",
        reason="File not found: documents/missing.md",
    )
    db_session.add_all([task, request])
    await db_session.commit()

    action = await recover_misclassified_tool_error_human_wait(db_session, task)

    assert action is None
    assert task.status == "waiting_on_customer"
    refreshed = await db_session.get(HitlRequest, request.id)
    assert refreshed.status == "pending"


@pytest.mark.asyncio
async def test_missing_fact_or_request_input_human_wait_is_not_recovered(db_session):
    from packages.core.models.hitl_request import HitlRequest

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    recover_task_id = generate_ulid()
    input_task_id = generate_ulid()
    recover_task = Task(
        id=recover_task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Need operator password",
        status="waiting_on_customer",
    )
    input_task = Task(
        id=input_task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Need operator input",
        status="waiting_on_customer",
    )
    recover_request = _hitl_request(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=recover_task_id,
        action_key="task.recover",
        reason="operator must supply the account password",
    )
    input_request = _hitl_request(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=input_task_id,
        action_key="task.request_input",
        reason="operator must supply the account password",
    )
    db_session.add_all([recover_task, input_task, recover_request, input_request])
    await db_session.commit()

    assert await recover_misclassified_tool_error_human_wait(db_session, recover_task) is None
    assert await recover_misclassified_tool_error_human_wait(db_session, input_task) is None
    assert recover_task.status == "waiting_on_customer"
    assert input_task.status == "waiting_on_customer"
    assert (await db_session.get(HitlRequest, recover_request.id)).status == "pending"
    assert (await db_session.get(HitlRequest, input_request.id)).status == "pending"


@pytest.mark.asyncio
async def test_in_progress_task_is_not_recovered_as_tool_error_human_wait(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Still running",
        status="in_progress",
    )
    request = _hitl_request(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        action_key="task.recover",
        reason="unsupported_operation_for_file_type",
    )
    db_session.add_all([task, request])
    await db_session.commit()

    action = await recover_misclassified_tool_error_human_wait(db_session, task)

    assert action is None
    assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_planning_failure_completes_work_batch_and_dispatches_event_strategist(
    db_session, monkeypatch,
):
    from packages.core.models.workspace import Workspace, WorkspaceWorkBatch
    from packages.core.tasks import ai_tasks
    from packages.core.tasks.ai_tasks import _apply_task_planning_failure

    calls = []

    class _FakeStrategistTask:
        @staticmethod
        def apply_async(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return None

    monkeypatch.setattr(ai_tasks, "run_strategist_review", _FakeStrategistTask)

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    batch_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Score workspace goals",
        status="in_progress",
        details={"workspace_work_batch_id": batch_id},
    )
    db_session.add_all(
        [
            Workspace(id=workspace_id, entity_id=entity_id, name="Planning failure batch"),
            task,
            WorkspaceWorkBatch(
                id=batch_id,
                workspace_id=workspace_id,
                entity_id=entity_id,
                summary="Score goals wave",
                status="active",
                task_ids=[task_id],
                details={},
            ),
        ]
    )
    await db_session.commit()

    result = await _apply_task_planning_failure(
        db_session,
        task_id,
        "Planner failed to produce a valid plan after 8 turns",
        error_type="TaskPlanningExhausted",
    )
    await db_session.commit()

    assert result is not None
    assert result[0] == entity_id
    refreshed = await db_session.get(Task, task_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    batch = await db_session.get(WorkspaceWorkBatch, batch_id)
    assert batch is not None
    assert batch.status == "completed"
    assert calls
    dispatch = calls[0]["kwargs"]
    assert dispatch["args"] == [workspace_id]
    payload = dispatch["kwargs"]
    assert payload["trigger_kind"] == "event"
    assert payload["trigger_detail"] == f"work batch completed: {batch_id}"
    assert dispatch["countdown"] == 1
