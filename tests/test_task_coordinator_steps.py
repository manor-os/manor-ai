"""V3 Task coordinator exact Step release and result-inspection fences."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from sqlalchemy import select

from packages.core.constants.execution import (
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    ExecutionStepStatus,
)
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.dispatcher.effect_safety import retry_effect_block_detail
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.services.task_coordinator_steps import (
    TASK_COORDINATOR_CONTROL_KEY,
    _settled_tool_result,
    ack_settled_task_coordinator_batch,
    legacy_blocked_child_result_is_recoverable,
    release_ready_task_coordinator_steps,
)
from packages.core.services.task_coordinator_wakeup import (
    TaskCoordinatorClaim,
    checkpoint_task_coordinator_run,
)
from packages.core.services.task_execution import CoordinatorRuntimeSelection, task_execution_intent_generation


async def _scope(db, *, count: int = 2, chain: bool = False):
    task_id, plan_id, run_id = generate_ulid(), generate_ulid(), generate_ulid()
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    generation = generate_ulid()
    task = Task(
        id=task_id, entity_id=entity_id, workspace_id=workspace_id,
        title="Coordinated Task", status="in_progress", task_type="general", details={},
    )
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        task_id=task_id, status="running", plan_dag={},
        dispatcher_state={
            TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
            TASK_COORDINATOR_RUN_ID_KEY: run_id,
        },
    )
    run = TaskCoordinatorRun(
        id=run_id, task_id=task_id, entity_id=entity_id,
        workspace_id=workspace_id, root_subscription_id=generate_ulid(),
        root_agent_id=generate_ulid(), selection_fingerprint="a" * 64,
        lifecycle_generation=task_execution_intent_generation(task),
        phase=TaskCoordinatorPhase.RUNNING.value,
        current_plan_id=plan_id, checkpoint={"messages": []}, state_version=0,
        wake_generation=generation, claim_token="owner",
        claim_generation=generation, claim_expires_at=expires_at,
    )
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Coordinator fence", status="active")
    agent = Agent(id=run.root_agent_id, entity_id=entity_id, name="Writer", status="active")
    subscription = AgentSubscription(
        id=run.root_subscription_id, entity_id=entity_id, workspace_id=workspace_id,
        agent_id=agent.id, service_key="writer", status="active",
    )
    run.selection_fingerprint = CoordinatorRuntimeSelection.from_subscriptions([subscription]).stable_fingerprint()
    steps = [
        ExecutionStep(
            id=generate_ulid(), plan_id=plan_id, entity_id=entity_id,
            workspace_id=workspace_id, step_key=f"step_{index:02d}",
            kind="llm", step_status=ExecutionStepStatus.PAUSED.value,
            depends_on=[f"step_{index - 1:02d}"] if chain and index else [],
            params={}, evidence_refs=[],
        )
        for index in range(count)
    ]
    db.add_all([workspace, agent, subscription, task, plan, run, *steps])
    await db.flush()
    claim = TaskCoordinatorClaim(
        run_id, task_id, generate_ulid(), generation, "owner", expires_at, 0,
    )
    return task, plan, run, steps, claim


@pytest.mark.asyncio
async def test_releases_exact_ready_batch_once_and_wakes_plan(db_session):
    task, plan, run, steps, claim = await _scope(db_session, count=10)
    released = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=0, tool_call_id="delegate-call", max_parallel=8,
    )
    assert released.status == "released"
    assert released.step_ids == tuple(step.id for step in steps[:8])
    assert [step.step_status for step in steps] == [
        *([ExecutionStepStatus.PENDING.value] * 8),
        *([ExecutionStepStatus.PAUSED.value] * 2),
    ]
    assert run.phase == TaskCoordinatorPhase.WAITING_CHILD.value
    assert run.checkpoint[TASK_COORDINATOR_CONTROL_KEY]["step_ids"] == list(released.step_ids)
    assert released.plan_wake is not None
    assert (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == f"plan-cycle:{plan.id}",
        )
    )).scalar_one().delivered_at is None

    replay = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=released.state_version, tool_call_id="delegate-call",
    )
    assert replay.status == "waiting"
    assert replay.step_ids == released.step_ids
    with pytest.raises(ValueError, match="inspect its prior child batch"):
        await release_ready_task_coordinator_steps(
            db_session, task_id=task.id, plan_id=plan.id, claim=claim,
            expected_state_version=released.state_version, tool_call_id="new-call",
        )


@pytest.mark.asyncio
async def test_child_result_must_be_checkpointed_before_next_release(db_session):
    task, plan, run, steps, claim = await _scope(db_session, count=2, chain=True)
    first = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=0, tool_call_id="first-call",
    )
    assert first.step_ids == (steps[0].id,)
    assert steps[1].step_status == ExecutionStepStatus.PAUSED.value
    steps[0].step_status = ExecutionStepStatus.DONE.value
    steps[0].result = {"ok": True}
    settled = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=first.state_version, tool_call_id="first-call",
    )
    assert settled.status == "settled"
    assert '"ok":true' in settled.tool_result
    with pytest.raises(ValueError, match="has not checkpointed"):
        await ack_settled_task_coordinator_batch(
            db_session, task_id=task.id, plan_id=plan.id, claim=claim,
            expected_state_version=first.state_version,
        )
    result_payload = json.loads(settled.tool_result)
    model_content = json.dumps(result_payload, ensure_ascii=False)
    assert model_content != settled.tool_result
    tampered_result = deepcopy(result_payload)
    tampered_result["steps"][0]["result"]["ok"] = False
    tampered_digest = deepcopy(result_payload)
    tampered_digest["task_coordinator_batch_digest"] = "0" * 64
    version = first.state_version
    for tampered in (tampered_result, tampered_digest):
        version = await checkpoint_task_coordinator_run(
            db_session, task_id=task.id, claim_token=claim.claim_token,
            claim_generation=claim.wake_generation,
            expected_state_version=version,
            checkpoint={
                **run.checkpoint,
                "messages": [{
                    "role": "tool", "tool_call_id": "first-call",
                    "content": json.dumps(tampered, ensure_ascii=False),
                }],
            },
        )
        assert version is not None
        with pytest.raises(ValueError, match="has not checkpointed"):
            await ack_settled_task_coordinator_batch(
                db_session, task_id=task.id, plan_id=plan.id, claim=claim,
                expected_state_version=version,
            )
    version = await checkpoint_task_coordinator_run(
        db_session, task_id=task.id, claim_token=claim.claim_token,
        claim_generation=claim.wake_generation,
        expected_state_version=version,
        checkpoint={
            **run.checkpoint,
            "messages": [{
                "role": "tool", "tool_call_id": "first-call",
                "content": model_content,
            }],
        },
    )
    assert version is not None
    ack_version = await ack_settled_task_coordinator_batch(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=version,
    )
    assert TASK_COORDINATOR_CONTROL_KEY not in run.checkpoint
    assert run.phase == TaskCoordinatorPhase.RUNNING.value
    second = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=ack_version, tool_call_id="second-call",
    )
    assert second.status == "released" and second.step_ids == (steps[1].id,)


@pytest.mark.asyncio
async def test_failed_child_effect_is_observed_and_acked_without_replay(db_session):
    task, plan, run, steps, claim = await _scope(db_session, count=2, chain=True)
    first = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=0, tool_call_id="create-task-call",
    )
    effect = ToolEffectEvidenceFactory.effect("action", {
        "tool": "workspace_create_task",
        "tool_call_id": "child-create-call",
        "intent_state": "returned",
        "created": True,
        "delivery_confirmed": False,
    })
    steps[0].step_status = ExecutionStepStatus.FAILED.value
    steps[0].error = {"type": "StepResultFailed", "message": "File was not saved"}
    steps[0].evidence_refs = [effect]
    settled = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=first.state_version,
        tool_call_id="create-task-call",
    )
    assert settled.status == "settled"
    assert json.loads(settled.tool_result)["steps"][0]["evidence_refs"] == [effect]
    version = await checkpoint_task_coordinator_run(
        db_session, task_id=task.id, claim_token=claim.claim_token,
        claim_generation=claim.wake_generation,
        expected_state_version=first.state_version,
        checkpoint={
            **run.checkpoint,
            "messages": [{
                "role": "tool", "tool_call_id": "create-task-call",
                "content": json.dumps(json.loads(settled.tool_result), ensure_ascii=False),
            }],
        },
    )
    assert version is not None
    ack_version = await ack_settled_task_coordinator_batch(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=version,
    )
    assert run.checkpoint.get(TASK_COORDINATOR_CONTROL_KEY) is None
    assert steps[0].evidence_refs == [effect]
    blocked = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=ack_version, tool_call_id="new-call",
    )
    assert blocked.status == "blocked"
    assert steps[1].step_status == ExecutionStepStatus.PAUSED.value


@pytest.mark.asyncio
async def test_legacy_blocked_observation_only_recovers_exact_terminal_effect_batch(db_session):
    task, plan, run, steps, claim = await _scope(db_session, count=1)
    released = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=0, tool_call_id="pinned-call",
    )
    step = steps[0]
    effect = ToolEffectEvidenceFactory.effect("action", {
        "tool": "workspace_create_task", "tool_call_id": "child-create-call",
        "intent_state": "returned", "created": True,
        "delivery_confirmed": False,
    })
    step.step_status = ExecutionStepStatus.FAILED.value
    step.error = {"type": "StepResultFailed", "message": "File was not saved"}
    step.evidence_refs = [effect]
    reason = await retry_effect_block_detail(db_session, [step])
    assert reason
    assistant = {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "pinned-call", "type": "function",
            "function": {"name": "delegate_ready_steps", "arguments": "{}"},
        }],
    }
    blocked = {
        "role": "tool", "tool_call_id": "pinned-call",
        "content": json.dumps({"status": "blocked", "reason": reason}),
    }
    checkpoint = {**run.checkpoint, "messages": [assistant, blocked]}
    run.checkpoint = checkpoint

    async def recoverable() -> bool:
        return await legacy_blocked_child_result_is_recoverable(
            db_session, task_id=task.id, plan_id=plan.id,
            claim=claim, expected_state_version=released.state_version,
        )

    assert await recoverable()
    wrong_reason = deepcopy(checkpoint)
    wrong_reason["messages"][1]["content"] = json.dumps({
        "status": "blocked", "reason": "different reason",
    })
    run.checkpoint = wrong_reason
    assert not await recoverable()
    wrong_args = deepcopy(checkpoint)
    wrong_args["messages"][0]["tool_calls"][0]["function"]["arguments"] = '{"force":true}'
    run.checkpoint = wrong_args
    assert not await recoverable()
    duplicate = deepcopy(checkpoint)
    duplicate["messages"].append(deepcopy(blocked))
    run.checkpoint = duplicate
    assert not await recoverable()
    run.checkpoint = checkpoint
    step.step_status = ExecutionStepStatus.PENDING.value
    assert not await recoverable()
    step.step_status = ExecutionStepStatus.FAILED.value
    lease = WorkLease(
        id=generate_ulid(), step_id=step.id, plan_id=plan.id,
        entity_id=task.entity_id, workspace_id=task.workspace_id,
        worker_id=generate_ulid(), status="active",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(lease)
    await db_session.flush()
    assert not await recoverable()


@pytest.mark.parametrize("result", [
    {"values": [{"v": index} for index in range(370)]},
    {"text": "x" * 6000},
])
def test_settled_tool_result_stays_bounded_after_agentic_loop_json_format(result):
    plan_id = "p" * 26
    step_id = "s" * 26
    step = ExecutionStep(
        id=step_id, step_key="step_00", step_status=ExecutionStepStatus.DONE.value,
        result=result, error=None, evidence_refs=[],
    )
    full_steps = [{
        "step_id": step_id, "step_key": step.step_key,
        "status": step.step_status, "result": result,
        "error": None, "evidence_refs": [],
    }]
    expected_digest = sha256(json.dumps(
        full_steps, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    full_payload = {
        "plan_id": plan_id, "task_coordinator_batch_digest": expected_digest,
        "steps": full_steps,
    }
    compact_length = len(json.dumps(
        full_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ))
    expanded_length = len(json.dumps(full_payload, ensure_ascii=False))
    assert expanded_length > 3900
    if "values" in result:
        assert compact_length <= 3900
    else:
        assert compact_length > 3900

    tool_result = _settled_tool_result(plan_id, (step_id,), {step_id: step})
    checkpoint_content = json.dumps(json.loads(tool_result), ensure_ascii=False)
    assert len(checkpoint_content) <= 3900
    payload = json.loads(checkpoint_content)
    assert payload["truncated"] is True
    assert payload["task_coordinator_batch_digest"] == expected_digest
    assert payload["steps"][0]["step_id"] == step_id


@pytest.mark.asyncio
async def test_stale_claim_or_failed_step_never_releases_next_batch(db_session):
    task, plan, run, steps, claim = await _scope(db_session, count=2, chain=True)
    with pytest.raises(ValueError, match="claim, version"):
        await release_ready_task_coordinator_steps(
            db_session, task_id=task.id, plan_id=plan.id, claim=claim,
            expected_state_version=1, tool_call_id="call",
        )
    steps[0].step_status = ExecutionStepStatus.FAILED.value
    blocked = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=0, tool_call_id="call",
    )
    assert blocked.status == "blocked"
    assert steps[1].step_status == ExecutionStepStatus.PAUSED.value
    assert run.checkpoint.get(TASK_COORDINATOR_CONTROL_KEY) is None


@pytest.mark.asyncio
async def test_relative_sleep_deadline_is_frozen_at_release(db_session):
    from packages.core.plans.executor import PlanExecutor

    task, plan, _run, steps, claim = await _scope(db_session, count=1)
    steps[0].kind = "sleep"
    steps[0].params = {"seconds": 30}
    before = datetime.now(timezone.utc)
    released = await release_ready_task_coordinator_steps(
        db_session, task_id=task.id, plan_id=plan.id, claim=claim,
        expected_state_version=0, tool_call_id="sleep-call",
    )
    assert released.status == "released"
    deadline = datetime.fromisoformat(
        steps[0].params[COORDINATOR_CHILD_WAKE_AT_PARAM_KEY]
    )
    assert before + timedelta(seconds=29) <= deadline <= before + timedelta(seconds=31)
    assert 29 <= PlanExecutor._sleep_seconds(steps[0].params) <= 30
