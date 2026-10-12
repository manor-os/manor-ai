"""Explicit completion retries renew a loop window, never writer execution."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.constants.task import TaskRuntimeKind
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.services import task_retry_service as retry

pytestmark = pytest.mark.oss_smoke


def _checkpoint():
    return {
        "rounds": 384, "non_submit_retries": 2,
        "messages": [{"role": "tool", "tool_call_id": "inspection", "content": "retained evidence"}],
        "usage": {"total_tokens": 1234, "total_cost_usd": 0.05},
        "tool_calls_made": ["inspect_task_delivery", "submit_task"],
        "task_delivery_inspection": {"evidence_digest": "digest", "pages": ["step:0:0"]},
        "coordinator_outcome": {
            "contract": "tool_error", "status": "error", "tool": "submit_task",
            "error": "coordinator_acceptance_exhausted",
            "details": {"plan_id": "plan-1", "stop_reason": "max_rounds"},
        },
    }


def test_reset_preserves_transcript_usage_observations_and_last_outcome():
    saved = _checkpoint()
    before = deepcopy(saved)
    updated = retry._reset_coordinator_completion_checkpoint(saved, plan_id="plan-1")
    assert updated == {**before, "rounds": 0, "non_submit_retries": 0}
    assert saved == before


@pytest.mark.parametrize("change", [
    "wrong_plan", "wrong_contract", "wrong_status", "wrong_tool", "wrong_code",
    "pending_call", "pending_result", "control", "missing_outcome", "ordinary_tool_result",
])
def test_only_exact_host_checkpoint_can_renew_a_window(change):
    checkpoint = _checkpoint()
    if change == "wrong_plan":
        checkpoint["coordinator_outcome"]["details"]["plan_id"] = "other-plan"
    elif change in {"wrong_contract", "wrong_status", "wrong_tool"}:
        checkpoint["coordinator_outcome"][change.removeprefix("wrong_")] = "other"
    elif change == "wrong_code":
        checkpoint["coordinator_outcome"]["error"] = "different_failure"
    elif change == "pending_call":
        checkpoint["pending_tool_call"] = {"name": "write_file"}
    elif change == "pending_result":
        checkpoint["pending_tool_result"] = {"status": "unknown"}
    elif change == "control":
        checkpoint["task_coordinator_control"] = {"name": "delegate_ready_steps"}
    elif change == "ordinary_tool_result":
        checkpoint["messages"].append(checkpoint.pop("coordinator_outcome"))
    else:
        checkpoint.pop("coordinator_outcome")
    before = deepcopy(checkpoint)
    assert retry._reset_coordinator_completion_checkpoint(checkpoint, plan_id="plan-1") is None
    assert checkpoint == before


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


def _retry_context(monkeypatch, *, unsafe=None, phase="failed", task_status="blocked"):
    from packages.core.services import event_emitter, workspace_access

    task = SimpleNamespace(
        id="task-1", entity_id="entity-1", workspace_id="workspace-1", status=task_status,
        details={}, expected_output=None, owner_service_key="writer", actual_output=None,
    )
    plan = SimpleNamespace(
        id="plan-1", task_id=task.id, entity_id=task.entity_id, workspace_id=task.workspace_id,
        status="running", last_error=None, approval_required=False,
        plan_dag={"steps": []}, dispatcher_state={
            TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
            TASK_COORDINATOR_RUN_ID_KEY: "run-1",
        },
    )
    step = SimpleNamespace(
        step_key="write", step_status="waiting_human" if unsafe == "waiting_step" else "done",
        attempt_count=2, result={"text": "Saved and read back"},
        evidence_refs=[{"kind": "tool_effect", "effect": "action", "id": "receipt"}],
    )
    run = SimpleNamespace(
        id="run-1", entity_id=task.entity_id, workspace_id=task.workspace_id,
        current_plan_id=plan.id, phase=phase, selection_fingerprint="selection",
        claim_token="live-claim" if unsafe == "live_claim" else None,
        claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        checkpoint=_checkpoint(),
    )
    coordinator = SimpleNamespace(stable_fingerprint=lambda: "selection")
    dispatch = SimpleNamespace(runtime_kind=TaskRuntimeKind.COORDINATED_PLAN,
                               coordinator=coordinator, mode="coordinated_plan")
    leases = [SimpleNamespace(status="active")] if unsafe == "live_lease" else []
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        _Result(task), _Result(run.checkpoint), _Result(plan), _Result([step]), _Result(plan),
        _Result(leases), _Result([step]), _Result(run),
        _Result("pending-input" if unsafe == "pending_hitl" else None),
    ]), flush=AsyncMock())
    monkeypatch.setattr(workspace_access, "lock_workspace_access_boundary", AsyncMock(return_value=SimpleNamespace(
        status="active", deleted_at=None,
    )))
    monkeypatch.setattr(retry, "resolve_task_execution", AsyncMock(return_value=dispatch))
    monkeypatch.setattr(retry, "_persisted_plan_replan_context", lambda *_args, **_kwargs: None)
    ambiguous = AsyncMock(return_value={"unknown": True} if unsafe == "unknown_effect" else None)
    monkeypatch.setattr(retry, "ambiguous_effect_reconciliation_snapshot", ambiguous)
    monkeypatch.setattr(retry, "add_task_log", AsyncMock())
    monkeypatch.setattr(event_emitter, "emit_in_session", AsyncMock())

    async def transition(target, status, **_kwargs):
        target.status = status

    monkeypatch.setattr(retry, "apply_task_status_transition", transition)
    stage = AsyncMock()
    monkeypatch.setattr(retry, "stage_task_execution_intent", stage)
    return task, plan, step, run, db, stage, ambiguous


@pytest.mark.asyncio
async def test_internal_repair_stop_retries_original_plan_without_resetting_steps(monkeypatch):
    task, plan, step, run, db, stage, _ = _retry_context(monkeypatch)
    step.step_status = "failed"
    run.checkpoint["coordinator_outcome"] = {
        "contract": "tool_error", "status": "error", "tool": "replan_task",
        "error": "task_replan_unavailable",
        "details": {"plan_id": plan.id, "requires_human": False},
    }
    checkpoint = deepcopy(run.checkpoint)
    evidence = deepcopy(step.evidence_refs)
    result = await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")
    assert result.plan_id == plan.id
    assert plan.status == "running"
    assert step.step_status == "failed" and step.attempt_count == 2
    assert step.evidence_refs == evidence
    assert run.checkpoint == checkpoint
    stage.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_retry_renews_only_loop_budget_after_existing_safety_checks(monkeypatch):
    task, plan, step, run, db, stage, ambiguous = _retry_context(monkeypatch)
    saved_step, saved_plan, saved_checkpoint = deepcopy(vars(step)), deepcopy(vars(plan)), deepcopy(run.checkpoint)
    result = await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")
    assert result.reset_steps == 0 and result.plan_id == "plan-1"
    assert vars(step) == saved_step and vars(plan) == saved_plan
    assert run.checkpoint == {**saved_checkpoint, "rounds": 0, "non_submit_retries": 0}
    assert task.status == "in_progress"
    stage.assert_awaited_once()
    ambiguous.assert_awaited_once_with(db, [step], plan_id=plan.id)
    # Plan/lease/Step locks precede Run so manual renewal cannot invert settlement.
    locked_queries = [str(call.args[0]) for call in db.execute.call_args_list[4:8]]
    assert all("FOR UPDATE" in sql for sql in locked_queries)
    assert "FROM execution_plans" in locked_queries[0]
    assert "FROM work_leases" in locked_queries[1]
    assert "FROM execution_steps" in locked_queries[2]
    assert "FROM task_coordinator_runs" in locked_queries[3]


def _unconfirmed_outcome():
    return {
        "contract": "tool_error", "status": "error", "tool": "submit_task",
        "error": "coordinator_tool_outcome_unconfirmed",
        "details": {"plan_id": "plan-1", "stop_reason": "tool_error", "effect_state": "unconfirmed",
                    "unconfirmed_calls": [{"tool": "publish_post"}], "do_not_replay": True,
                    "requires_human": True},
    }


def test_unconfirmed_stop_can_renew_a_window_like_an_exhausted_one():
    saved = {**_checkpoint(), "coordinator_outcome": _unconfirmed_outcome()}
    updated = retry._reset_coordinator_completion_checkpoint(saved, plan_id="plan-1")
    assert updated == {**saved, "rounds": 0, "non_submit_retries": 0}


@pytest.mark.asyncio
async def test_manual_retry_after_unconfirmed_stop_runs_the_effect_gate(monkeypatch):
    task, plan, step, run, db, stage, ambiguous = _retry_context(monkeypatch)
    run.checkpoint["coordinator_outcome"] = _unconfirmed_outcome()
    saved_checkpoint = deepcopy(run.checkpoint)
    await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")
    ambiguous.assert_awaited_once_with(db, [step], plan_id=plan.id)
    assert run.checkpoint == {**saved_checkpoint, "rounds": 0, "non_submit_retries": 0}
    stage.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_retry_after_unconfirmed_stop_refuses_an_unknown_effect(monkeypatch):
    task, plan, step, run, db, stage, ambiguous = _retry_context(monkeypatch, unsafe="unknown_effect")
    run.checkpoint["coordinator_outcome"] = _unconfirmed_outcome()
    saved_checkpoint = deepcopy(run.checkpoint)
    with pytest.raises(retry.TaskRetryError, match="uncertain action"):
        await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")
    ambiguous.assert_awaited_once()
    assert run.checkpoint == saved_checkpoint and task.status == "blocked"
    stage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["live_claim", "live_lease", "waiting_step", "pending_hitl", "unknown_effect"])
async def test_new_budget_does_not_override_an_existing_execution_boundary(monkeypatch, unsafe):
    task, plan, step, run, db, stage, _ = _retry_context(monkeypatch, unsafe=unsafe)
    saved_step, saved_checkpoint = deepcopy(vars(step)), deepcopy(run.checkpoint)
    with pytest.raises(retry.TaskRetryError):
        await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")
    assert task.status == "blocked" and plan.status == "running"
    assert run.checkpoint == saved_checkpoint and vars(step) == saved_step
    stage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,task_status", [("waiting_human", "blocked"), ("failed", "waiting_on_customer")])
async def test_other_stopped_coordinator_paths_keep_their_existing_budget(monkeypatch, phase, task_status):
    task, _, _, run, db, _, ambiguous = _retry_context(monkeypatch, phase=phase, task_status=task_status)
    saved = deepcopy(run.checkpoint)
    await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")
    assert run.checkpoint == saved
    ambiguous.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_manual_retry_cannot_renew_running_task(monkeypatch):
    task, _, _, run, db, stage, _ = _retry_context(monkeypatch, task_status="in_progress")
    saved = deepcopy(run.checkpoint)
    with pytest.raises(retry.TaskRetryError, match="already in progress"):
        await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")
    assert run.checkpoint == saved
    stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_newer_runtime_checkpoint_refuses_manual_retry_before_state_changes(monkeypatch):
    from packages.core.ai.runtime.run_state import (
        RUN_STATE_NEWER_RUNTIME_DETAIL,
        RUN_STATE_VERSION_KEY,
        RuntimeRunState,
    )

    task, plan, step, run, db, stage, ambiguous = _retry_context(monkeypatch)
    run.checkpoint[RUN_STATE_VERSION_KEY] = RuntimeRunState.VERSION + 1
    saved = deepcopy([vars(task), vars(plan), vars(step), vars(run)])

    with pytest.raises(retry.TaskRetryError) as raised:
        await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")

    assert raised.value.status_code == 409
    assert raised.value.detail == RUN_STATE_NEWER_RUNTIME_DETAIL
    assert [vars(task), vars(plan), vars(step), vars(run)] == saved
    assert db.execute.await_count == 2
    assert "task_coordinator_runs.checkpoint" in str(db.execute.call_args_list[1].args[0])
    retry.resolve_task_execution.assert_not_awaited()
    ambiguous.assert_not_awaited()
    stage.assert_not_awaited()
