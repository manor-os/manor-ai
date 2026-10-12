"""Supervisor robustness.

Four rules: the supervisor's LLM call is bounded by a timeout so a hung
provider cannot hold the executor cycle; on a plan that mechanically
COMPLETED, a model verdict of needs_replan / failed is demoted to one step
re-run carrying the verdict as feedback (a replan at finalize time almost
always just fails the task); that re-run revives never-ran dependents the
same way a manual retry does; and the replan budget counts attempts that
actually failed, not every earlier plan.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.constants.execution import (
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY,
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    SUPERVISOR_FEEDBACK_PARAM_KEY,
    UPSTREAM_PARTIAL_RESULT_ERROR_TYPE,
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.constants.supervisor import (
    SUPERVISOR_STEP_RETRY_FLAG,
    SupervisorDecision,
    SupervisorDecisionSource,
    SupervisorVerdict,
)
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task, TaskLog
from packages.core.models.worker import WorkLease, Worker
from packages.core.plans import executor as executor_module
from packages.core.plans.executor import PlanExecutor
from packages.core.plans.wakeup import plan_cycle_outbox_dedupe_key


@pytest.fixture(autouse=True)
def _no_celery(monkeypatch):
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: dispatched.append(plan_id))
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))
    return dispatched


def _model(monkeypatch, reply: dict):
    """The supervisor model answers with ``reply``; records what it was asked."""
    asked: list[dict] = []

    async def fake(**kwargs):
        asked.append(kwargs)
        return SimpleNamespace(content=json.dumps(reply))

    monkeypatch.setattr(executor_module, "runtime_execute_plan_supervisor_completion", fake)
    return asked


_ACTION_PROVIDER = "gmail"
_ACTION_KEY = "send_email"


def _dag_step(spec) -> dict:
    kind = spec[2] if len(spec) > 2 else "subagent"
    entry = {"key": spec[0], "kind": kind}
    if kind == "action":
        entry["provider"] = _ACTION_PROVIDER
        entry["action_key"] = _ACTION_KEY
    return entry


async def _seed(db, step_specs, *, plan_status=ExecutionPlanStatus.COMPLETED.value, prior_plan_statuses=()):
    """``step_specs``: (key, status, kind, params, depends_on, error) tuples; later rows are newer."""
    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(), entity_id=entity_id, title="Summarize the meeting notes",
        description="Write a short text summary of the notes.", status="in_progress",
        priority=3, task_type="general", details={},
    )
    db.add(task)
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i, status in enumerate(prior_plan_statuses):
        db.add(ExecutionPlan(
            id=generate_ulid(), entity_id=entity_id, task_id=task.id, status=status,
            execution_mode="live", plan_dag={"steps": []}, created_at=base + timedelta(minutes=i),
            dispatcher_state={"_response_locale": "en"},
        ))
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=entity_id, task_id=task.id, status=plan_status,
        execution_mode="live", started_at=base + timedelta(minutes=30),
        completed_at=datetime.now(timezone.utc),
        plan_dag={"steps": [_dag_step(spec) for spec in step_specs]},
        dispatcher_state={"_response_locale": "en"},
        created_at=base + timedelta(minutes=40),
    )
    db.add(plan)
    rows: dict[str, ExecutionStep] = {}
    for i, spec in enumerate(step_specs):
        key, status = spec[0], spec[1]
        kind = spec[2] if len(spec) > 2 else "subagent"
        params = dict(spec[3] or {}) if len(spec) > 3 and spec[3] else {}
        depends_on = list(spec[4]) if len(spec) > 4 and spec[4] else []
        error = spec[5] if len(spec) > 5 else None
        params.setdefault("prompt", f"do {key}")
        rows[key] = ExecutionStep(
            id=generate_ulid(), plan_id=plan.id, entity_id=entity_id, step_key=key, kind=kind,
            step_status=status, params=params, depends_on=depends_on, error=error,
            result={"text": f"{key} output"} if status == ExecutionStepStatus.DONE.value else None,
            created_at=base + timedelta(minutes=41, seconds=i),
            provider=_ACTION_PROVIDER if kind == "action" else None,
            action_key=_ACTION_KEY if kind == "action" else None,
            # A done action carries its captured receipt, so the deterministic
            # "no receipt" gate does not fire before the model is consulted.
            evidence_refs=(
                [{"kind": "tool_effect", "effect": "action",
                  "fields": {"provider": _ACTION_PROVIDER, "action_key": _ACTION_KEY, "message_id": f"{key}-1"}}]
                if kind == "action" and status == ExecutionStepStatus.DONE.value else []
            ),
        )
    db.add_all(rows.values())
    await db.commit()
    return task, plan, rows


# ── deterministic acceptance boundary ───────────────────────────────


@pytest.mark.asyncio
async def test_completion_gates_can_reject_a_failed_root_without_model_io(db_session, monkeypatch):
    task, plan, steps = await _seed(
        db_session,
        [("coordinator", ExecutionStepStatus.FAILED.value)],
        plan_status=ExecutionPlanStatus.FAILED.value,
    )
    asked = _model(monkeypatch, {"verdict": "completed", "evidence": "ignore failure"})

    decision = await executor_module.evaluate_task_completion_gates(
        db_session,
        task=task,
        plan=plan,
        steps=list(steps.values()),
        plan_status=ExecutionPlanStatus.FAILED.value,
        locale="en",
    )

    assert decision is not None
    assert decision.source is SupervisorDecisionSource.GATE
    assert decision.verdict is SupervisorVerdict.FAILED
    assert asked == []


@pytest.mark.asyncio
async def test_stale_ambiguous_effect_marker_allows_normal_completion_review(db_session, monkeypatch):
    task, plan, _steps = await _seed(db_session, [("write", ExecutionStepStatus.DONE.value)])
    plan.last_error = {
        "type": "ActionOutcomeReconciliationRequired",
        "message": "the provider outcome is unknown",
    }
    await db_session.commit()
    asked = _model(monkeypatch, {"verdict": "completed", "evidence": "looks finished"})

    decision = await PlanExecutor._supervise_outcome(
        db_session, plan, ExecutionPlanStatus.COMPLETED.value,
    )

    assert decision.verdict is SupervisorVerdict.COMPLETED
    assert asked
    assert executor_module._SUPERVISOR_REVIEW_CLAIM_KEY not in (plan.dispatcher_state or {})


@pytest.mark.asyncio
async def test_fresh_ambiguous_effect_gate_preempts_a_completed_model_verdict(db_session, monkeypatch):
    task, plan, steps = await _seed(db_session, [("write", ExecutionStepStatus.DONE.value)])
    plan.last_error = {
        "type": "ActionOutcomeReconciliationRequired",
        "message": "the provider outcome is unknown",
    }
    steps["write"].evidence_refs = [ToolEffectEvidenceFactory.effect(
        "pending", {"tool": "send_email", "tool_call_id": "pending-email",
                    "intent_state": "pending"},
    )]
    await db_session.commit()
    asked = _model(monkeypatch, {"verdict": "completed", "evidence": "looks finished"})

    decision = await PlanExecutor._supervise_outcome(
        db_session, plan, ExecutionPlanStatus.COMPLETED.value,
    )

    assert decision.source is SupervisorDecisionSource.GATE
    assert decision.verdict is SupervisorVerdict.NEEDS_HUMAN
    assert "provider outcome is unknown" in decision.evidence
    assert asked == []


@pytest.mark.asyncio
async def test_a_hung_supervisor_call_falls_back_instead_of_blocking_the_cycle(db_session, monkeypatch):
    task, plan, steps = await _seed(db_session, [("write", "done")])

    async def hang(**_kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(executor_module, "runtime_execute_plan_supervisor_completion", hang)
    monkeypatch.setattr(executor_module, "SUPERVISOR_LLM_TIMEOUT_SECONDS", 0.05)

    decision = await asyncio.wait_for(
        PlanExecutor._supervise_outcome(db_session, plan, ExecutionPlanStatus.COMPLETED), timeout=5,
    )

    assert decision.source is SupervisorDecisionSource.FALLBACK
    assert decision.verdict is SupervisorVerdict.COMPLETED
    assert executor_module._SUPERVISOR_REVIEW_CLAIM_KEY not in (
        plan.dispatcher_state or {}
    )
    await db_session.commit()
    await db_session.refresh(plan)
    assert executor_module._SUPERVISOR_REVIEW_CLAIM_KEY not in (
        plan.dispatcher_state or {}
    )


@pytest.mark.asyncio
async def test_cancel_during_supervisor_io_is_not_blocked_and_discards_the_stale_verdict(
    db_session,
    monkeypatch,
):
    from packages.core.plans.service import cancel_plan

    task, plan, _steps = await _seed(
        db_session,
        [("write", ExecutionStepStatus.DONE.value)],
        plan_status=ExecutionPlanStatus.RUNNING.value,
    )
    plan.completed_at = None
    await db_session.commit()

    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def blocked_provider(**_kwargs):
        provider_started.set()
        await release_provider.wait()
        return SimpleNamespace(content=json.dumps({
            "verdict": "completed",
            "evidence": "the stale generation looked complete",
        }))

    monkeypatch.setattr(
        executor_module,
        "runtime_execute_plan_supervisor_completion",
        blocked_provider,
    )
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    finalize = asyncio.create_task(
        PlanExecutor._finalize(
            db_session,
            plan,
            ExecutionPlanStatus.COMPLETED.value,
        )
    )
    async def cancel_concurrently() -> None:
        async with session_factory() as cancel_db:
            # This is the same Task -> all Plans lifecycle prefix used by the
            # API mutation surface. A short lock timeout makes holding either
            # row across provider I/O fail the test instead of merely slowing it.
            await cancel_db.execute(text("SET LOCAL lock_timeout = '750ms'"))
            await cancel_db.execute(
                select(Task).where(Task.id == task.id).with_for_update()
            )
            await cancel_db.execute(
                select(ExecutionPlan)
                .where(ExecutionPlan.task_id == task.id)
                .order_by(ExecutionPlan.id)
                .with_for_update()
            )
            await cancel_plan(cancel_db, plan.id, reason="cancelled during review")
            await cancel_db.commit()

    try:
        # Preparing the review can exceed two seconds on a loaded CI runner.
        # The database lock timeout still detects locks held during model I/O.
        await asyncio.wait_for(provider_started.wait(), timeout=10)
        await asyncio.wait_for(cancel_concurrently(), timeout=10)
        release_provider.set()
        with pytest.raises(executor_module._StaleSupervisorVerdict) as stale:
            await asyncio.wait_for(finalize, timeout=10)
    finally:
        release_provider.set()
        if not finalize.done():
            finalize.cancel()
        await asyncio.gather(finalize, return_exceptions=True)
    assert stale.value.plan_status == ExecutionPlanStatus.CANCELLED.value
    assert plan.status == ExecutionPlanStatus.CANCELLED.value

    async with session_factory() as verify_db:
        stored_plan = await verify_db.get(ExecutionPlan, plan.id)
        stored_task = await verify_db.get(Task, task.id)
        verdict_logs = list((await verify_db.execute(
            select(TaskLog).where(
                TaskLog.task_id == task.id,
                TaskLog.log_type == executor_module.SUPERVISOR_VERDICT_LOG_TYPE,
            )
        )).scalars().all())
    assert stored_plan is not None
    assert stored_plan.status == ExecutionPlanStatus.CANCELLED.value
    assert executor_module._SUPERVISOR_REVIEW_CLAIM_KEY not in (
        stored_plan.dispatcher_state or {}
    )
    assert stored_task is not None and stored_task.status == "in_progress"
    assert verdict_logs == []


@pytest.mark.asyncio
async def test_lease_settlement_during_supervisor_io_is_not_blocked_and_fences_the_verdict(
    db_session,
    monkeypatch,
):
    from packages.core.constants.execution import WorkLeaseStatus
    from packages.core.dispatcher.service import Dispatcher

    task, plan, steps = await _seed(
        db_session,
        [
            ("coordinate", ExecutionStepStatus.DONE.value, "subagent"),
            (
                "coordinate__child_1",
                ExecutionStepStatus.RUNNING.value,
                "subagent",
                {
                    COORDINATOR_CHILD_PARAM_KEY: True,
                    COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
                },
            ),
        ],
        plan_status=ExecutionPlanStatus.RUNNING.value,
    )
    # The child is a durable nested-loop row, not part of the authored DAG.
    plan.plan_dag = {"steps": [_dag_step(("coordinate", "done", "subagent"))]}
    plan.completed_at = None
    child = steps["coordinate__child_1"]
    worker = Worker(
        id=generate_ulid(),
        entity_id=task.entity_id,
        kind="internal",
        display_name="Nested agent worker",
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    lease = WorkLease(
        id=generate_ulid(),
        step_id=child.id,
        plan_id=plan.id,
        entity_id=task.entity_id,
        worker_id=worker.id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    child.current_lease_id = lease.id
    child.attempt_count = 1
    child.started_at = datetime.now(timezone.utc)
    db_session.add_all([worker, lease])
    await db_session.commit()

    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def blocked_provider(**_kwargs):
        provider_started.set()
        await release_provider.wait()
        return SimpleNamespace(content=json.dumps({
            "verdict": "completed",
            "evidence": "the stale generation looked complete",
        }))

    monkeypatch.setattr(
        executor_module,
        "runtime_execute_plan_supervisor_completion",
        blocked_provider,
    )
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    finalize = asyncio.create_task(
        PlanExecutor._finalize(
            db_session,
            plan,
            ExecutionPlanStatus.COMPLETED.value,
        )
    )
    async def settle_concurrently() -> None:
        async with session_factory() as settle_db:
            await settle_db.execute(text("SET LOCAL lock_timeout = '750ms'"))
            await Dispatcher().complete_lease(
                settle_db,
                lease.id,
                result={"text": "nested work settled while review was running"},
            )
            await settle_db.commit()

    try:
        # CI can spend several seconds preparing the query under load; the
        # database lock timeout above still detects a held supervisor lock.
        await asyncio.wait_for(provider_started.wait(), timeout=10)
        await asyncio.wait_for(settle_concurrently(), timeout=10)
        release_provider.set()
        with pytest.raises(executor_module._StaleSupervisorVerdict):
            await asyncio.wait_for(finalize, timeout=10)
    finally:
        release_provider.set()
        if not finalize.done():
            finalize.cancel()
        await asyncio.gather(finalize, return_exceptions=True)

    async with session_factory() as verify_db:
        stored_plan = await verify_db.get(ExecutionPlan, plan.id)
        stored_task = await verify_db.get(Task, task.id)
        stored_child = await verify_db.get(ExecutionStep, child.id)
        stored_lease = await verify_db.get(WorkLease, lease.id)
        verdict_logs = list((await verify_db.execute(
            select(TaskLog).where(
                TaskLog.task_id == task.id,
                TaskLog.log_type == executor_module.SUPERVISOR_VERDICT_LOG_TYPE,
            )
        )).scalars().all())
    assert stored_plan is not None
    assert stored_plan.status == ExecutionPlanStatus.RUNNING.value
    assert executor_module._SUPERVISOR_REVIEW_CLAIM_KEY not in (
        stored_plan.dispatcher_state or {}
    )
    assert stored_task is not None and stored_task.status == "in_progress"
    assert stored_child is not None
    assert stored_child.step_status == ExecutionStepStatus.DONE.value
    assert stored_lease is not None
    assert stored_lease.status == WorkLeaseStatus.COMPLETED.value
    assert verdict_logs == []


# ── needs_replan / failed on a completed plan → one step re-run ───────


@pytest.mark.asyncio
async def test_a_replan_verdict_on_a_completed_plan_becomes_one_step_retry_with_the_evidence(db_session, monkeypatch):
    task, plan, steps = await _seed(db_session, [
        ("research", "done", "subagent"),
        ("write", "done", "subagent"),
        ("send", "done", "action"),
        ("write__x_1", "done", "subagent", {COORDINATOR_CHILD_PARAM_KEY: True, COORDINATOR_PARENT_STEP_PARAM_KEY: "write"}),
    ])
    asked = _model(monkeypatch, {"verdict": "needs_replan", "evidence": "the summary is empty"})

    decision = await PlanExecutor._supervise_outcome(db_session, plan, ExecutionPlanStatus.COMPLETED)

    assert decision.verdict is SupervisorVerdict.RETRY_STEP
    assert decision.step_key == "write", "the newest agent step; actions and coordinator children are not candidates"
    assert decision.evidence == "the summary is empty"
    assert decision.source is SupervisorDecisionSource.MODEL
    assert "write__x_1" not in list(asked[0]["retryable_step_keys"]), "a delegated child cannot be re-run on its own"


@pytest.mark.asyncio
async def test_supervisor_runtime_call_carries_full_task_history_and_step_view(db_session, monkeypatch):
    task, plan, steps = await _seed(
        db_session,
        [("write", "done", "subagent")],
        prior_plan_statuses=(ExecutionPlanStatus.FAILED.value,),
    )
    prior_plan = (await db_session.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.task_id == task.id,
            ExecutionPlan.id != plan.id,
        )
    )).scalar_one()
    prior_step = ExecutionStep(
        id=generate_ulid(),
        plan_id=prior_plan.id,
        entity_id=task.entity_id,
        step_key="research",
        kind="subagent",
        step_status=ExecutionStepStatus.FAILED.value,
        error={"type": "TimeoutError", "message": "source timed out"},
    )
    prior_review = TaskLog(
        id=generate_ulid(),
        task_id=task.id,
        log_type=executor_module.SUPERVISOR_VERDICT_LOG_TYPE,
        content="Earlier supervisor review",
        meta={
            "verdict": SupervisorVerdict.RETRY_STEP.value,
            "evidence": "the source was incomplete",
            "step_key": "research",
            "plan_id": prior_plan.id,
        },
    )
    plan.parent_plan_id = prior_plan.id
    plan.plan_dag = {
        "steps": [{
            "key": "write",
            "kind": "subagent",
            "description": "Draft the final meeting summary.",
        }],
        "metadata": {"rationale": "Repair the timed-out research attempt."},
    }
    steps["write"].params = {"prompt": "Include decisions and owners."}
    steps["write"].result = {
        "text": "Summary written.",
        "fs_path": "Artifacts/meeting-summary.md",
    }
    db_session.add_all([prior_step, prior_review])
    await db_session.commit()
    asked = _model(monkeypatch, {"verdict": "completed", "evidence": "the summary is present"})

    decision = await PlanExecutor._supervise_outcome(
        db_session, plan, ExecutionPlanStatus.COMPLETED,
    )

    assert decision.verdict is SupervisorVerdict.COMPLETED
    assert len(asked) == 1
    request = asked[0]
    assert request["plan_rationale"] == "Repair the timed-out research attempt."
    assert request["is_replan"] is True
    assert request["prior_attempts"] == [{
        "status": ExecutionPlanStatus.FAILED.value,
        "steps": [{
            "key": "research",
            "status": ExecutionStepStatus.FAILED.value,
            "error": "TimeoutError: source timed out",
        }],
    }]
    assert request["prior_reviews"] == [{
        "verdict": SupervisorVerdict.RETRY_STEP.value,
        "evidence": "the source was incomplete",
        "step_key": "research",
        "plan_id": prior_plan.id,
    }]
    assert len(request["steps"]) == 1
    step_view = request["steps"][0]
    assert step_view["key"] == "write"
    assert step_view["instruction"] == "Draft the final meeting summary. — Include decisions and owners."
    assert step_view["artifacts"] == ["Artifacts/meeting-summary.md"]


@pytest.mark.asyncio
async def test_finalize_downgrades_a_refused_supervisor_retry_and_logs_it(db_session, monkeypatch):
    task, plan, _steps = await _seed(db_session, [("write", "done", "subagent")])
    decision = SupervisorDecision(
        verdict=SupervisorVerdict.RETRY_STEP,
        evidence="the final draft needs another pass",
        source=SupervisorDecisionSource.MODEL,
        step_key="missing_step",
    )
    replan = AsyncMock(return_value=False)
    monkeypatch.setattr(PlanExecutor, "_maybe_replan", replan)

    event = await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED.value,
        server_decision=decision,
    )
    await db_session.flush()

    assert replan.await_count == 1
    assert task.status == "failed"
    assert task.actual_output["supervisor_verdict"] == SupervisorVerdict.NEEDS_REPLAN.value
    assert event["event_type"] == "task.failed"
    verdict_log = (await db_session.execute(
        select(TaskLog).where(
            TaskLog.task_id == task.id,
            TaskLog.log_type == executor_module.SUPERVISOR_VERDICT_LOG_TYPE,
        )
    )).scalar_one()
    assert verdict_log.meta["verdict"] == SupervisorVerdict.NEEDS_REPLAN.value
    assert "the final draft needs another pass" in verdict_log.meta["evidence"]


@pytest.mark.asyncio
async def test_a_step_already_retried_by_the_supervisor_is_skipped_for_the_next_one(db_session, monkeypatch):
    task, plan, steps = await _seed(db_session, [
        ("research", "done", "subagent"),
        ("write", "done", "subagent", {SUPERVISOR_STEP_RETRY_FLAG: True}),
    ])
    _model(monkeypatch, {"verdict": "failed", "evidence": "still nothing usable"})

    decision = await PlanExecutor._supervise_outcome(db_session, plan, ExecutionPlanStatus.COMPLETED)

    assert decision.verdict is SupervisorVerdict.RETRY_STEP
    assert decision.step_key == "research"


@pytest.mark.asyncio
async def test_a_failed_verdict_with_no_step_left_to_re_run_remains_failed(db_session, monkeypatch):
    task, plan, steps = await _seed(db_session, [
        ("write", "done", "subagent", {SUPERVISOR_STEP_RETRY_FLAG: True}),
        ("send", "done", "action"),
    ])
    _model(monkeypatch, {"verdict": "failed", "evidence": "nothing was delivered"})

    decision = await PlanExecutor._supervise_outcome(db_session, plan, ExecutionPlanStatus.COMPLETED)

    assert decision.verdict is SupervisorVerdict.FAILED
    assert decision.source is SupervisorDecisionSource.MODEL
    assert decision.evidence.startswith("nothing was delivered")
    assert "no step is left to re-run" in decision.evidence


@pytest.mark.asyncio
async def test_model_needs_human_without_a_request_becomes_bounded_step_repair(db_session, monkeypatch):
    _task, plan, _steps = await _seed(db_session, [
        ("write", "done", "subagent"),
    ])
    _model(monkeypatch, {"verdict": "needs_human", "evidence": "The draft needs more detail."})

    decision = await PlanExecutor._supervise_outcome(
        db_session, plan, ExecutionPlanStatus.COMPLETED,
    )

    assert decision.verdict is SupervisorVerdict.RETRY_STEP
    assert decision.step_key == "write"
    assert "no durable input or approval request" in decision.evidence


@pytest.mark.asyncio
async def test_a_failed_plan_keeps_the_models_verdict(db_session, monkeypatch):
    task, plan, steps = await _seed(db_session, [
        ("write", "done", "subagent"),
        ("send", "failed", "action", None, [], {"type": "ProviderError", "message": "500"}),
    ], plan_status=ExecutionPlanStatus.FAILED.value)
    _model(monkeypatch, {"verdict": "failed", "evidence": "the send failed"})

    decision = await PlanExecutor._supervise_outcome(db_session, plan, ExecutionPlanStatus.FAILED)

    assert decision.verdict is SupervisorVerdict.FAILED
    assert decision.evidence == "the send failed"


@pytest.mark.asyncio
async def test_a_completed_verdict_is_untouched(db_session, monkeypatch):
    task, plan, steps = await _seed(db_session, [("write", "done", "subagent")])
    _model(monkeypatch, {"verdict": "completed", "evidence": "the summary reads well"})

    decision = await PlanExecutor._supervise_outcome(db_session, plan, ExecutionPlanStatus.COMPLETED)

    assert decision.verdict is SupervisorVerdict.COMPLETED


# ── the re-run carries feedback and revives its never-ran dependents ──


@pytest.mark.asyncio
async def test_a_supervisor_retry_carries_feedback_and_revives_never_ran_dependents(db_session, _no_celery):
    task, plan, steps = await _seed(db_session, [
        ("write", "done", "subagent"),
        ("publish", "skipped", "subagent", None, ["write"]),
        ("notify", "failed", "action", None, ["publish"], {"type": UPSTREAM_PARTIAL_RESULT_ERROR_TYPE, "message": "held"}),
        ("audit", "failed", "action", None, ["write"], {"type": "ProviderError", "message": "really failed"}),
    ])
    decision = SupervisorDecision(
        verdict=SupervisorVerdict.RETRY_STEP, evidence="the draft is empty",
        source=SupervisorDecisionSource.MODEL, step_key="write",
    )

    applied = await PlanExecutor._apply_supervisor_step_retry(db_session, task, plan, decision)

    assert applied is True
    write = steps["write"]
    assert write.step_status == ExecutionStepStatus.PENDING
    assert write.params[SUPERVISOR_FEEDBACK_PARAM_KEY] == "the draft is empty"
    assert write.params[SUPERVISOR_STEP_RETRY_FLAG] is True
    assert write.params["prompt"] == "do write", "the original instruction is kept"
    for key in ("publish", "notify"):
        assert steps[key].step_status == ExecutionStepStatus.PENDING, key
        assert steps[key].error is None
        assert SUPERVISOR_FEEDBACK_PARAM_KEY not in steps[key].params
    assert steps["audit"].step_status == ExecutionStepStatus.FAILED, "a step that really ran keeps its own decision"
    assert plan.status == ExecutionPlanStatus.RUNNING
    assert _no_celery == []
    wake = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == plan_cycle_outbox_dedupe_key(plan.id)
        )
    )).scalar_one()
    assert wake.delivered_at is None
    assert wake.payload["reason"] == "supervisor_step_retry"


@pytest.mark.asyncio
async def test_a_supervisor_retry_never_targets_a_coordinator_child(db_session, _no_celery):
    task, plan, steps = await _seed(db_session, [
        ("coordinate", "done", "subagent"),
        ("coordinate__x_1", "done", "subagent", {COORDINATOR_CHILD_PARAM_KEY: True, COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate"}),
    ])
    decision = SupervisorDecision(
        verdict=SupervisorVerdict.RETRY_STEP, evidence="x", source=SupervisorDecisionSource.MODEL,
        step_key="coordinate__x_1",
    )

    assert await PlanExecutor._apply_supervisor_step_retry(db_session, task, plan, decision) is False
    assert steps["coordinate__x_1"].step_status == ExecutionStepStatus.DONE
    assert _no_celery == []


@pytest.mark.asyncio
async def test_supervisor_parent_retry_reparks_typed_child_behind_release_gate(
    db_session,
    _no_celery,
):
    task, plan, steps = await _seed(db_session, [
        ("coordinate", "done", "subagent"),
        (
            "coordinate__x_1",
            "failed",
            "subagent",
            {
                COORDINATOR_CHILD_PARAM_KEY: True,
                COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
                COORDINATOR_CHILD_RELEASED_PARAM_KEY: True,
                COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
                COORDINATOR_CHILD_WAKE_AT_PARAM_KEY: "2099-01-01T00:00:00+00:00",
                COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY: {"version": 1},
                COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY: "old-digest",
            },
        ),
    ])
    decision = SupervisorDecision(
        verdict=SupervisorVerdict.RETRY_STEP,
        evidence="the delegated result needs repair",
        source=SupervisorDecisionSource.MODEL,
        step_key="coordinate",
    )

    assert await PlanExecutor._apply_supervisor_step_retry(
        db_session,
        task,
        plan,
        decision,
    ) is True

    child = steps["coordinate__x_1"]
    assert child.step_status == ExecutionStepStatus.PAUSED.value
    assert child.params[COORDINATOR_CHILD_RELEASED_PARAM_KEY] is False
    assert COORDINATOR_CHILD_WAKE_AT_PARAM_KEY not in child.params
    assert COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY not in child.params
    assert COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY not in child.params
    assert steps["coordinate"].step_status == ExecutionStepStatus.PENDING.value


# ── the shared reset module ───────────────────────────────────────────


def test_never_ran_downstream_recognises_skipped_and_boundary_held_steps():
    from packages.core.plans.step_reset import never_ran_downstream

    assert never_ran_downstream(SimpleNamespace(step_status=ExecutionStepStatus.SKIPPED.value, error=None))
    assert never_ran_downstream(SimpleNamespace(
        step_status=ExecutionStepStatus.FAILED.value, error={"type": UPSTREAM_PARTIAL_RESULT_ERROR_TYPE},
    ))
    assert not never_ran_downstream(SimpleNamespace(
        step_status=ExecutionStepStatus.FAILED.value, error={"type": "ProviderError"},
    ))
    assert not never_ran_downstream(SimpleNamespace(step_status=ExecutionStepStatus.DONE.value, error=None))


def test_reset_never_ran_downstream_steps_revives_the_chain_with_the_callers_reset():
    from packages.core.plans.step_reset import reset_never_ran_downstream_steps

    def row(key, status, depends_on=(), error=None):
        return SimpleNamespace(step_key=key, step_status=status, depends_on=list(depends_on), error=error)

    a = row("a", ExecutionStepStatus.PENDING.value)
    b = row("b", ExecutionStepStatus.SKIPPED.value, ["a"])
    c = row("c", ExecutionStepStatus.FAILED.value, ["b"], {"type": UPSTREAM_PARTIAL_RESULT_ERROR_TYPE})
    x = row("x", ExecutionStepStatus.FAILED.value, [], {"type": "ProviderError"})
    d = row("d", ExecutionStepStatus.SKIPPED.value, ["b", "x"])
    done = row("done", ExecutionStepStatus.DONE.value)
    e = row("e", ExecutionStepStatus.SKIPPED.value, ["done"])
    reset_keys: list[str] = []

    def reset(step):
        reset_keys.append(step.step_key)
        step.step_status = ExecutionStepStatus.PENDING.value
        step.error = None

    revived = reset_never_ran_downstream_steps(
        [a, b, c, x, d, done, e], retried_step_keys={"a"}, reset_step=reset,
    )

    assert [s.step_key for s in revived] == ["b", "c"] == reset_keys
    assert d.step_status == ExecutionStepStatus.SKIPPED.value, "one of its dependencies really failed"
    assert e.step_status == ExecutionStepStatus.SKIPPED.value, "not downstream of the retried step"
    assert x.step_status == ExecutionStepStatus.FAILED.value


def test_reset_tolerates_rows_without_a_depends_on_attribute():
    from packages.core.plans.step_reset import reset_never_ran_downstream_steps

    bare = SimpleNamespace(step_key="s", step_status=ExecutionStepStatus.DONE.value, error=None)
    assert reset_never_ran_downstream_steps([bare], retried_step_keys={"s"}, reset_step=lambda s: None) == []


def test_router_retry_helpers_delegate_to_the_shared_module():
    from apps.api.routers import plans as router

    assert "never_ran_downstream(" in inspect.getsource(router._never_ran_downstream)
    assert "reset_never_ran_downstream_steps(" in inspect.getsource(router._reset_skipped_downstream_steps_for_retry)


# ── the replan budget ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replan_budget_ignores_prior_plans_that_did_not_fail(db_session):
    task, plan, steps = await _seed(
        db_session,
        [("write", "done"), ("send", "failed", "action", None, [], {"type": "ProviderError", "message": "500"})],
        plan_status=ExecutionPlanStatus.FAILED.value,
        prior_plan_statuses=(ExecutionPlanStatus.COMPLETED.value, ExecutionPlanStatus.CANCELLED.value),
    )

    assert await PlanExecutor._maybe_replan(db_session, plan, list(steps.values())) is True

    assert plan.status == ExecutionPlanStatus.REPLANNED
    assert task.details["_replan_context"]["attempt"] == 1


@pytest.mark.asyncio
async def test_replan_budget_is_spent_by_failed_and_replanned_plans(db_session):
    task, plan, steps = await _seed(
        db_session,
        [("write", "done"), ("send", "failed", "action", None, [], {"type": "ProviderError", "message": "500"})],
        plan_status=ExecutionPlanStatus.FAILED.value,
        prior_plan_statuses=(ExecutionPlanStatus.FAILED.value, ExecutionPlanStatus.REPLANNED.value),
    )

    assert await PlanExecutor._maybe_replan(db_session, plan, list(steps.values())) is False
    assert plan.status == ExecutionPlanStatus.FAILED
