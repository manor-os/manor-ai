"""V3 Plan cycles hand control to the Task loop without a second Supervisor."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.ai.agentic_loop import AgenticResult
from packages.core.constants.supervisor import (
    SupervisorDecision,
    SupervisorDecisionSource,
    SupervisorVerdict,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorPhase
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.plans.executor import (
    PlanExecutor,
    TaskCoordinatorReplanOutcome,
    _accepted_terminal_text_summary,
    finalize_task_coordinator_submission,
    prepare_task_coordinator_replan,
)
from packages.core.services.task_coordinator_steps import (
    TASK_COORDINATOR_CONTROL_KEY,
    _settled_tool_result,
    ack_settled_task_coordinator_batch,
)
from packages.core.services.task_execution_reconcile import _actual_output_from_steps
from packages.core.services.task_coordinator_wakeup import (
    claim_task_coordinator_wake,
    ensure_task_coordinator_run,
    settle_task_coordinator_wake,
    stage_task_coordinator_wake,
    task_coordinator_outbox_dedupe_key,
)
from packages.core.services.task_execution import (
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
)


async def _seed(db, *, step_status: str = "paused", run_phase: str = TaskCoordinatorPhase.WAITING_CHILD.value):
    entity_id, workspace_id, task_id, plan_id = (generate_ulid() for _ in range(4))
    task = Task(
        id=task_id, entity_id=entity_id, workspace_id=workspace_id,
        title="Ship result", status="in_progress", task_type="general", details={},
    )
    binding = TaskPlanServiceBinding(
        subscription_id=generate_ulid(), service_key="writer", agent_id=generate_ulid(),
    )
    selection = CoordinatorRuntimeSelection(root=binding, bindings=(binding,))
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Coordinator fence", status="active")
    agent = Agent(id=binding.agent_id, entity_id=entity_id, name="Writer", status="active")
    subscription = AgentSubscription(
        id=binding.subscription_id, entity_id=entity_id, workspace_id=workspace_id,
        agent_id=agent.id, service_key=binding.service_key, status="active",
    )
    db.add_all([task, workspace, agent, subscription])
    await db.flush()
    run = await ensure_task_coordinator_run(db, task=task, selection=selection)
    plan = ExecutionPlan(
        id=plan_id, task_id=task_id, entity_id=entity_id,
        workspace_id=workspace_id, status="running", execution_mode="live",
        plan_dag={"steps": [{"key": "write", "kind": "llm", "depends_on": []}]},
        dispatcher_state={
            TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
            TASK_COORDINATOR_RUN_ID_KEY: run.id,
        },
    )
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan_id, entity_id=entity_id,
        workspace_id=workspace_id, step_key="write", kind="llm",
        step_status=step_status, depends_on=[], params={"prompt": "Write"},
        result={"text": "Done"} if step_status == "done" else None,
    )
    db.add_all([plan, step])
    run.current_plan_id = plan.id
    run.phase = run_phase
    await db.flush()
    return SimpleNamespace(
        task=task, plan=plan, step=step, run=run, selection=selection,
        workspace=workspace, agent=agent, subscription=subscription,
    )


@pytest.mark.asyncio
async def test_settled_v3_step_wakes_task_without_plan_supervisor(db_session):
    seeded = await _seed(db_session, step_status="done")
    result = await PlanExecutor._handoff_task_coordinator_plan(
        db_session, seeded.plan, [seeded.step],
    )
    assert result == {
        "plan_id": seeded.plan.id,
        "status": "task_coordinator_handoff",
        "next_action": "stop",
    }
    assert seeded.plan.status == "running"
    first_generation = seeded.run.wake_generation
    assert first_generation
    assert (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.dedupe_key
        == task_coordinator_outbox_dedupe_key(seeded.task.id),
    ))).scalar_one().delivered_at is None
    await PlanExecutor._handoff_task_coordinator_plan(
        db_session, seeded.plan, [seeded.step],
    )
    assert seeded.run.wake_generation == first_generation


@pytest.mark.asyncio
async def test_accepted_v3_terminal_text_is_saved_as_task_summary(db_session):
    seeded = await _seed(db_session, step_status="done", run_phase=TaskCoordinatorPhase.RUNNING.value)
    decision = SupervisorDecision(
        verdict=SupervisorVerdict.COMPLETED,
        evidence="All completion gates passed.",
        source=SupervisorDecisionSource.GATE,
    )

    await PlanExecutor._finalize(
        db_session, seeded.plan, "completed", server_decision=decision,
    )
    await db_session.flush()

    assert seeded.task.status == "completed"
    assert seeded.task.actual_output["summary"] == "Done"
    assert seeded.task.actual_output["steps"][0]["result_summary"] == "Done"
    repaired = _actual_output_from_steps(
        seeded.plan, [seeded.step], task=seeded.task,
        supervisor_review={"verdict": "completed"},
    )
    assert repaired["summary"] == seeded.task.actual_output["summary"]


def test_terminal_text_summary_requires_one_uncontracted_accepted_leaf():
    task = SimpleNamespace(expected_output=None)
    first = SimpleNamespace(
        step_key="first", kind="llm", depends_on=[],
        step_status="done", result={"text": "First result"}, params={},
    )
    second = SimpleNamespace(
        step_key="second", kind="subagent", depends_on=[],
        step_status="done", result={"text": "Second result"}, params={},
    )
    assert _accepted_terminal_text_summary(
        task, [first], supervisor_accepted=True,
    ) == "First result"
    assert _accepted_terminal_text_summary(
        task, [first, second], supervisor_accepted=True,
    ) is None
    assert _accepted_terminal_text_summary(
        task, [first], supervisor_accepted=False,
    ) is None
    task.expected_output = {"type": "string"}
    assert _accepted_terminal_text_summary(
        task, [first], supervisor_accepted=True,
    ) is None


@pytest.mark.asyncio
async def test_active_v3_step_stays_with_dispatcher(db_session):
    seeded = await _seed(db_session, step_status="pending")
    assert await PlanExecutor._handoff_task_coordinator_plan(
        db_session, seeded.plan, [seeded.step],
    ) is None
    assert seeded.run.wake_generation is None


@pytest.mark.asyncio
async def test_fast_child_completion_cannot_be_erased_by_parent_wake_ack(db_session):
    seeded = await _seed(db_session, step_status="done")
    old_wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="release_in_progress",
    )
    assert old_wake is not None
    claim, _ = await claim_task_coordinator_wake(
        db_session,
        task_id=seeded.task.id,
        event_id=old_wake.event_id,
        wake_generation=old_wake.wake_generation,
        claim_token="old-parent",
        selection=seeded.selection,
        lease_seconds=60,
    )
    assert claim is not None
    await PlanExecutor._handoff_task_coordinator_plan(
        db_session, seeded.plan, [seeded.step],
    )
    assert seeded.run.wake_generation != old_wake.wake_generation
    assert await settle_task_coordinator_wake(
        db_session,
        task_id=seeded.task.id,
        event_id=old_wake.event_id,
        wake_generation=old_wake.wake_generation,
        claim_token=claim.claim_token,
    ) is False


@pytest.mark.asyncio
async def test_task_submit_requires_server_gate_and_exact_claim(db_session, monkeypatch):
    seeded = await _seed(db_session, step_status="done", run_phase=TaskCoordinatorPhase.RUNNING.value)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="step_settled",
    )
    assert wake is not None
    claim, reason = await claim_task_coordinator_wake(
        db_session,
        task_id=seeded.task.id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token="submit-owner",
        selection=seeded.selection,
        lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None

    called = []

    async def fake_finalize(db, plan, status, *, server_decision):
        called.append(server_decision)
        assert status == "completed"
        plan.status = status
        seeded.task.status = status
        return {"event_type": "task.succeeded"}

    monkeypatch.setattr(PlanExecutor, "_finalize", staticmethod(fake_finalize))
    from packages.core.plans.executor import prepare_task_coordinator_submission
    from packages.core.services.task_acceptance_review import parse_task_acceptance_review
    snapshot, gate = await prepare_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
    )
    assert snapshot is not None and gate is None
    review = parse_task_acceptance_review(
        json.dumps({"verdict": "accepted", "evidence": "The write Step returned the requested result.", "issues": [],
                    "checks": [{"criterion": "Requested result", "pointer": "/steps/0/result/text", "quote": "Done"}]}),
        snapshot,
    )
    accepted, gate, event = await finalize_task_coordinator_submission(
        db_session,
        task_id=seeded.task.id,
        plan_id=seeded.plan.id,
        claim=claim,
        acceptance_review=review,
    )
    assert accepted is True and gate is None
    assert event == {"event_type": "task.succeeded"}
    assert called and called[0].verdict.value == "completed"
    assert seeded.run.phase == TaskCoordinatorPhase.COMPLETED.value
    assert seeded.run.wake_generation is None


@pytest.mark.asyncio
async def test_task_submit_rejects_unsettled_step_before_projection(db_session, monkeypatch):
    seeded = await _seed(db_session, step_status="paused", run_phase=TaskCoordinatorPhase.RUNNING.value)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="initial",
    )
    assert wake is not None
    claim, _ = await claim_task_coordinator_wake(
        db_session,
        task_id=seeded.task.id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token="submit-owner",
        selection=seeded.selection,
        lease_seconds=60,
    )
    assert claim is not None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Task must not be projected completed")

    monkeypatch.setattr(PlanExecutor, "_finalize", staticmethod(forbidden))
    accepted, gate, event = await finalize_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
        claim=claim,
    )
    assert accepted is False and gate is not None and event is None
    assert seeded.task.status == "in_progress"


@pytest.mark.asyncio
async def test_task_submit_cannot_skip_child_result_checkpoint_ack(db_session, monkeypatch):
    seeded = await _seed(db_session, step_status="done")
    seeded.run.checkpoint = {
        "task_coordinator_control": {
            "version": 1,
            "plan_id": seeded.plan.id,
            "step_ids": [seeded.step.id],
            "tool_call_id": "delegate-call",
        },
    }
    wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="step_settled",
    )
    assert wake is not None
    claim, _ = await claim_task_coordinator_wake(
        db_session, task_id=seeded.task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="submit-before-ack",
        selection=seeded.selection, lease_seconds=60,
    )
    assert claim is not None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Unacknowledged Step result cannot complete Task")

    monkeypatch.setattr(PlanExecutor, "_finalize", staticmethod(forbidden))
    accepted, gate, event = await finalize_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
    )
    assert accepted is False and gate is not None and event is None


def test_v3_plan_step_retry_requires_task_coordinator() -> None:
    from fastapi import HTTPException

    from apps.api.routers.plans import (
        _reject_task_coordinator_plan_cancel,
        _reject_task_coordinator_step_retry,
    )

    plan = ExecutionPlan(
        id=generate_ulid(), task_id=generate_ulid(),
        entity_id=generate_ulid(), workspace_id=generate_ulid(),
        status="running", plan_dag={},
        dispatcher_state={TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION},
    )
    with pytest.raises(HTTPException) as exc:
        _reject_task_coordinator_step_retry(plan)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        _reject_task_coordinator_plan_cancel(plan)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_task_loop_can_prepare_a_bounded_successor_plan(db_session):
    seeded = await _seed(db_session, step_status="failed", run_phase=TaskCoordinatorPhase.RUNNING.value)
    seeded.step.error = {"type": "ToolError", "message": "Output schema mismatch"}
    wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="step_failed",
    )
    assert wake is not None
    claim, _ = await claim_task_coordinator_wake(
        db_session,
        task_id=seeded.task.id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token="replan-owner",
        selection=seeded.selection,
        lease_seconds=60,
    )
    assert claim is not None
    outcome, prior_plan_id, event = await prepare_task_coordinator_replan(
        db_session,
        task_id=seeded.task.id,
        plan_id=seeded.plan.id,
        claim=claim,
        issue="The Step produced an invalid output shape; revise the DAG.",
    )
    assert outcome is TaskCoordinatorReplanOutcome.REPLANNED
    assert prior_plan_id == seeded.plan.id and event is None
    assert seeded.plan.status == "replanned"
    assert (seeded.task.details or {})["_replan_context"]["prior_plan_id"] == seeded.plan.id
    assert seeded.run.current_plan_id == seeded.plan.id
    assert seeded.run.phase == TaskCoordinatorPhase.RUNNING.value


@pytest.mark.asyncio
async def test_unrepairable_v3_step_stops_without_inventing_customer_input(db_session):
    from packages.core.models.hitl_request import HitlRequest

    seeded = await _seed(
        db_session, step_status="failed", run_phase=TaskCoordinatorPhase.RUNNING.value,
    )
    seeded.step.error = {
        "type": "RequiredSkillBlocked",
        "message": "The required upstream artifact is unavailable.",
    }
    wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="step_failed",
    )
    assert wake is not None
    claim, _ = await claim_task_coordinator_wake(
        db_session, task_id=seeded.task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="failed-owner",
        selection=seeded.selection, lease_seconds=60,
    )
    assert claim is not None

    outcome, diagnostic, _event = await prepare_task_coordinator_replan(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
        claim=claim, issue="Repair the missing upstream artifact.",
    )
    task_id, plan_id, run_id = seeded.task.id, seeded.plan.id, seeded.run.id
    await db_session.commit()
    db_session.expire_all()
    task = await db_session.get(Task, task_id)
    plan = await db_session.get(ExecutionPlan, plan_id)
    run = await db_session.get(type(seeded.run), run_id)

    assert outcome is TaskCoordinatorReplanOutcome.FAILED
    assert diagnostic["error"] == "task_replan_unavailable"
    assert diagnostic["details"]["requires_human"] is False
    assert task.status == "blocked" and plan.status == "running"
    assert run.phase == TaskCoordinatorPhase.FAILED.value
    assert run.checkpoint["coordinator_outcome"] == diagnostic
    assert run.wake_generation is None and run.claim_token is None
    assert not list((await db_session.execute(select(HitlRequest).where(
        HitlRequest.origin_task_id == task_id,
    ))).scalars())


@pytest.mark.asyncio
async def test_unsafe_effect_replan_holds_original_task_without_claiming_child_artifact(db_session):
    from packages.core.dispatcher.effect_safety import validated_new_plan_effect_block_detail

    seeded = await _seed(db_session, step_status="failed")
    seeded.step.error = {"type": "StepResultFailed", "message": "generate_file unavailable"}
    seeded.step.result = {"status": "failed", "summary": "Knowledge file was not saved"}
    effect = ToolEffectEvidenceFactory.effect("action", {
        "tool": "workspace_create_task",
        "tool_call_id": "created-sibling-task",
        "intent_state": "returned",
        "created": True,
        "delivery_confirmed": False,
    })
    seeded.step.evidence_refs = [effect]
    seeded.run.checkpoint = {
        TASK_COORDINATOR_CONTROL_KEY: {
            "version": 1,
            "plan_id": seeded.plan.id,
            "step_ids": [seeded.step.id],
            "tool_call_id": "settled-child",
        },
        "messages": [{
            "role": "tool",
            "tool_call_id": "settled-child",
            "content": _settled_tool_result(
                seeded.plan.id, (seeded.step.id,), {seeded.step.id: seeded.step},
            ),
        }],
    }
    wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="step_failed",
    )
    assert wake is not None
    claim, _ = await claim_task_coordinator_wake(
        db_session, task_id=seeded.task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="hold-owner",
        selection=seeded.selection, lease_seconds=60,
    )
    assert claim is not None
    acknowledged_version = await ack_settled_task_coordinator_batch(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
        claim=claim, expected_state_version=seeded.run.state_version,
    )
    assert acknowledged_version == seeded.run.state_version
    assert TASK_COORDINATOR_CONTROL_KEY not in seeded.run.checkpoint
    assert seeded.run.phase == TaskCoordinatorPhase.RUNNING.value

    outcome, reason, event = await prepare_task_coordinator_replan(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
        claim=claim, issue="The required Knowledge document was not delivered.",
    )

    assert outcome is TaskCoordinatorReplanOutcome.WAITING_HUMAN
    assert "external action" in reason
    assert seeded.task.status == "waiting_on_customer"
    assert seeded.plan.status == "failed"
    assert seeded.run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
    assert seeded.run.wake_generation is None
    assert seeded.step.evidence_refs == [effect]
    assert seeded.task.actual_output["supervisor_verdict"] == "needs_human"
    assert not seeded.task.actual_output.get("files")
    assert event is not None and event["event_type"] == "task.hitl_requested"
    assert await validated_new_plan_effect_block_detail(db_session, [seeded.step])
    assert len((await db_session.execute(select(ExecutionPlan).where(
        ExecutionPlan.task_id == seeded.task.id,
    ))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_mode", ["pending", "canonical", "legacy_blocked"])
async def test_acked_unsafe_child_holds_without_another_model_round(
    db_session, monkeypatch, checkpoint_mode,
):
    from packages.core.dispatcher.effect_safety import retry_effect_block_detail
    from packages.core.workers import task_coordinator as worker

    seeded = await _seed(db_session, step_status="failed")
    seeded.step.error = {"type": "StepResultFailed", "message": "File was not delivered"}
    effect = ToolEffectEvidenceFactory.effect("action", {
        "tool": "workspace_create_task",
        "tool_call_id": "created-sibling-task",
        "intent_state": "returned",
        "created": True,
        "delivery_confirmed": False,
    })
    seeded.step.evidence_refs = [effect]
    receipt = _settled_tool_result(
        seeded.plan.id, (seeded.step.id,), {seeded.step.id: seeded.step},
    )
    tool_message = {
        "role": "tool", "tool_call_id": "settled-child", "content": receipt,
    }
    legacy_message = {
        "role": "tool", "tool_call_id": "settled-child",
        "content": json.dumps({
            "status": "blocked",
            "reason": await retry_effect_block_detail(db_session, [seeded.step]),
        }),
    }
    assistant_call = {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "settled-child", "type": "function",
            "function": {"name": "delegate_ready_steps", "arguments": "{}"},
        }],
    }
    seeded.run.checkpoint = {
        TASK_COORDINATOR_CONTROL_KEY: {
            "version": 1, "plan_id": seeded.plan.id,
            "step_ids": [seeded.step.id], "tool_call_id": "settled-child",
        },
        "messages": (
            [tool_message] if checkpoint_mode == "canonical"
            else [assistant_call, legacy_message] if checkpoint_mode == "legacy_blocked"
            else []
        ),
    }
    wake = await stage_task_coordinator_wake(
        db_session, task_id=seeded.task.id, reason="step_failed",
    )
    assert wake is not None
    await db_session.commit()
    claim, _ = await claim_task_coordinator_wake(
        db_session, task_id=seeded.task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="worker-owner",
        selection=seeded.selection, lease_seconds=60,
    )
    assert claim is not None
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(worker, "create_worker_session", lambda: session_factory)

    async def admitted(**_kwargs):
        return claim, "claimed"

    async def context(*_args, **_kwargs):
        return SimpleNamespace(
            runtime_envelope=None, system_prompt="", tool_profile=None,
            model="test", llm_metadata={},
        )

    async def no_event(_event):
        return None

    async def no_cleanup(*_args, **_kwargs):
        return None

    async def loop(**kwargs):
        if checkpoint_mode == "canonical":
            raise AssertionError("The ACKed unsafe child must not call the model")
        if checkpoint_mode == "legacy_blocked":
            resumed = kwargs["runtime_checkpoint"]
            assert resumed["pending_tool_call"]["id"] == "settled-child"
            assert not any(
                message.get("role") == "tool"
                and message.get("tool_call_id") == "settled-child"
                for message in resumed["messages"]
            )
            replayed = await kwargs["tool_executor"](
                "delegate_ready_steps",
                {"_runtime_tool_call_id_from_context": "settled-child"},
            )
            assert json.loads(replayed) == json.loads(receipt)
            observed = [*resumed["messages"], {**tool_message, "content": replayed}]
        else:
            observed = [tool_message]
        await kwargs["on_round_end"](1, observed, {}, ["delegate_ready_steps"])
        return AgenticResult(
            content="", messages=observed, usage={}, rounds=1,
            stop_reason="cancelled",
        )

    monkeypatch.setattr(worker, "_admit_and_claim", admitted)
    monkeypatch.setattr(worker, "build_agent_context", context)
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", loop)
    async def claim_is_current(_claim):
        return True

    monkeypatch.setattr(worker, "_claim_is_current", claim_is_current)
    monkeypatch.setattr(PlanExecutor, "_emit_task_event", no_event)
    monkeypatch.setattr(PlanExecutor, "_cleanup_terminal_approvals_after_commit", no_cleanup)

    outcome = await worker.run_task_coordinator_once(
        event_id=wake.event_id, task_id=seeded.task.id,
        wake_generation=wake.wake_generation,
    )

    assert outcome == {"status": "waiting_human", "task_id": seeded.task.id}
    await db_session.refresh(seeded.task)
    await db_session.refresh(seeded.plan)
    await db_session.refresh(seeded.step)
    await db_session.refresh(seeded.run)
    assert seeded.task.status == "waiting_on_customer"
    assert seeded.plan.status == "failed"
    assert seeded.run.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
    assert seeded.run.wake_generation is None
    assert seeded.step.evidence_refs == [effect]
    assert not seeded.task.actual_output.get("files")
    assert len((await db_session.execute(select(Task).where(
        Task.entity_id == seeded.task.entity_id,
    ))).scalars().all()) == 1
