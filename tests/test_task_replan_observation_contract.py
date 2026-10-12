"""Rejected replacement is an observation, not a customer reconciliation request."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai import agentic_loop as loop_module
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.plans import executor
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION, TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.services import task_acceptance_review as acceptance
from packages.core.services import task_coordinator_fence
from packages.core.services.task_execution import task_execution_intent_generation
from packages.core.workers import task_coordinator as worker
from test_task_coordinator_worker import _install_delivery_inspection_fakes


def _scope(monkeypatch, *, unknown=None, receipt_count=1):
    task = SimpleNamespace(
        id="task-1", entity_id="entity-1", workspace_id="workspace-1",
        status="in_progress", status_changed_at=None, updated_at=None,
        created_at=None, details={}, task_type="general", agent_id=None,
        agent_type=None, owner_service_key=None, owner_subscription_id=None,
        delegate_service_keys=[],
    )
    plan = SimpleNamespace(
        id="plan-1", task_id=task.id, status="running", last_error=None,
        plan_dag={},
        dispatcher_state={TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
                          TASK_COORDINATOR_RUN_ID_KEY: "run-1"},
    )
    run = SimpleNamespace(
        id="run-1", task_id=task.id, entity_id=task.entity_id, workspace_id=task.workspace_id,
        root_subscription_id="subscription-1", root_agent_id="agent-1",
        selection_fingerprint="selection", lifecycle_generation=task_execution_intent_generation(task),
        current_plan_id=plan.id, claim_token="claim", claim_generation="wake", wake_generation="wake",
        claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        phase="running", state_version=1, checkpoint={},
    )
    claim = SimpleNamespace(run_id=run.id, task_id=task.id, claim_token="claim", wake_generation="wake")
    selection = SimpleNamespace(
        root=SimpleNamespace(subscription_id=run.root_subscription_id, agent_id=run.root_agent_id),
        stable_fingerprint=lambda: "selection",
    )
    step = SimpleNamespace(
        id="step-1", plan_id=plan.id, step_key="save_report", step_status="done",
        params={}, result={"artifact_evidence": {"fs_path": "report.md"}},
        evidence_refs=[], runtime_checkpoint={},
    )
    for index in range(receipt_count):
        step.evidence_refs.append(ToolEffectEvidenceFactory.effect("action", {
            "tool": "generate_file", "tool_call_id": f"saved-{index}",
            "intent_state": "returned", "document_id": "document-1",
            "authorization": "must-not-expose",
        }))
    leases = []
    if unknown:
        pending = ToolEffectEvidenceFactory.effect("pending", {
            "tool": "patch_file", "tool_call_id": "unknown-write", "intent_state": "pending",
        })
        if unknown == "step":
            step.evidence_refs.append(pending)
        else:
            leases.append(SimpleNamespace(step_id=step.id, status="expired", evidence_refs=[pending]))

    class Session:
        async def execute(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            row = {ExecutionPlan: plan, TaskCoordinatorRun: run}[entity]
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    monkeypatch.setattr(task_coordinator_fence, "lock_task_coordinator_admission", AsyncMock(return_value=(task, selection)))
    monkeypatch.setattr(executor, "_lock_supervisor_review_state", AsyncMock(return_value=(task, plan, [plan], leases, [step])))
    monkeypatch.setattr(executor, "_repair_task_coordinator_failed_branches", AsyncMock(return_value=False))
    return Session(), task, plan, run, step, claim


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_count", [1, 40])
async def test_settled_effect_replan_returns_bounded_diagnostics_without_state_mutation(monkeypatch, receipt_count):
    db, task, plan, run, step, claim = _scope(monkeypatch, receipt_count=receipt_count)
    before = deepcopy([vars(task), vars(plan), vars(run), vars(step)])
    legacy = AsyncMock(side_effect=AssertionError("Settled effects must not enter legacy reconciliation"))
    finalize = AsyncMock(side_effect=AssertionError("Rejected control must not finalize the Task"))
    monkeypatch.setattr(executor.PlanExecutor, "_maybe_replan", legacy)
    monkeypatch.setattr(executor.PlanExecutor, "_finalize", finalize)

    outcome, detail, event = await executor.prepare_task_coordinator_replan(
        db, task_id=task.id, plan_id=plan.id, claim=claim,
        issue="Delivery reference could not be read", expected_state_version=run.state_version,
        tool_call_id="replan-1",
    )

    assert outcome is executor.TaskCoordinatorReplanOutcome.BLOCKED and event is None
    assert detail["contract"] == "tool_error"
    assert detail["error"] == "plan_replacement_would_repeat_effects"
    facts = detail["details"]
    assert facts["action_executed"] is False and facts["delivery_accepted"] is False
    assert facts["effect_state"] == "settled" and facts["requires_human"] is False
    assert facts["do_not_replay"] is True
    assert facts["available_actions"] == ["inspect_task_delivery"]
    assert facts["receipt_count"] == receipt_count
    assert len(facts["receipts"]) == min(receipt_count, 32)
    assert facts["omitted_receipt_count"] == max(0, receipt_count - 32)
    assert "authorization" not in json.dumps(detail)
    assert [vars(task), vars(plan), vars(run), vars(step)] == before
    legacy.assert_not_awaited()
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_unproven_action_receipt_cannot_start_a_new_plan(monkeypatch):
    db, task, plan, run, step, claim = _scope(monkeypatch)
    step.evidence_refs = []
    plan.plan_dag = {"steps": [{
        "key": step.step_key, "kind": "action", "provider": "external",
        "action_key": "publish", "expects": ["publish"],
    }]}
    replan = AsyncMock(side_effect=AssertionError("No blind action replay"))
    monkeypatch.setattr(executor.PlanExecutor, "_maybe_replan", replan)

    outcome, diagnostic, event = await executor.prepare_task_coordinator_replan(
        db, task_id=task.id, plan_id=plan.id, claim=claim,
        issue="The action receipt is missing.",
    )

    assert outcome is executor.TaskCoordinatorReplanOutcome.BLOCKED and event is None
    assert diagnostic["error"] == "external_receipt_missing"
    assert diagnostic["details"]["requires_human"] is False
    assert diagnostic["details"]["do_not_replay"] is True
    assert plan.status == "running" and run.phase == "running"
    replan.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["step", "lease"])
async def test_unknown_effect_retains_existing_reconciliation_path(monkeypatch, unknown):
    db, task, plan, _run, _step, claim = _scope(monkeypatch, unknown=unknown)
    legacy = AsyncMock(side_effect=RuntimeError("existing reconciliation boundary"))
    monkeypatch.setattr(executor.PlanExecutor, "_maybe_replan", legacy)
    with pytest.raises(RuntimeError, match="existing reconciliation boundary"):
        await executor.prepare_task_coordinator_replan(
            db, task_id=task.id, plan_id=plan.id, claim=claim, issue="Inspect prior operation",
        )
    legacy.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_observes_replan_denial_then_inspects_and_submits_in_same_loop(monkeypatch):
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}], "artifacts": [],
    })
    db, task_scope, plan_scope, _run, _step, claim = _scope(monkeypatch)
    denial = await executor.prepare_task_coordinator_replan(
        db, task_id=task_scope.id, plan_id=plan_scope.id, claim=claim,
        issue="Delivery reference could not be read",
    )
    model_calls = 0

    async def model(messages, _tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [{"id": "replan", "name": "replan_task", "arguments": {"issue": "Inspect saved file"}}], {}
        if model_calls == 2:
            observations = [json.loads(item["content"]) for item in messages if item.get("role") == "tool"]
            denied = next(item for item in observations if item.get("error") == "plan_replacement_would_repeat_effects")
            assert denied["details"]["effect_state"] == "settled"
            return "", [{"id": "inspect", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"}}], {}
        assert model_calls == 3
        return "", [{"id": "submit", "name": "submit_task", "arguments": {"assessment": {
            "evidence_digest": snapshot.evidence_digest, "verdict": "accepted",
            "evidence": "The saved output is present.", "issues": [], "checks": [{
                "criterion": "Output delivered", "pointer": "/steps/0/result/text", "quote": "Saved output",
            }],
        }}}], {}

    async def real_loop(**kwargs):
        return await loop_module.agentic_loop(**{key: kwargs[key] for key in (
            "system_prompt", "user_message", "tools", "tool_executor", "max_rounds",
            "on_round_end", "on_round_end_strict", "on_llm_call_before", "runtime_cancel_checker",
            "terminal_tool_result_policy", "tool_schema_resolver",
        )})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    finalize = AsyncMock(return_value=(True, None, None))
    task, run, plan, review, _replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=real_loop, prepare=AsyncMock(return_value=(snapshot, None)), finalize=finalize,
    )
    replan = AsyncMock(return_value=denial)
    monkeypatch.setattr(worker, "prepare_task_coordinator_replan", replan)
    result = await worker.run_task_coordinator_once(event_id="event-1", task_id=task.id, wake_generation="wake-1")
    assert result["status"] == "completed" and model_calls == 3
    replan.assert_awaited_once()
    finalize.assert_awaited_once()
    review.assert_not_awaited()
    delegate.assert_not_awaited()
    assert task.status == "in_progress" and plan.status == "running"
    assert run.checkpoint["task_delivery_inspection"]["pages"] == ["manifest:0", "step:0:0"]
