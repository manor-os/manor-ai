from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.services import task_acceptance_review as acceptance
from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim
from packages.core.workers import task_coordinator as worker
from test_task_coordinator_worker import _install_delivery_inspection_fakes


@pytest.mark.asyncio
async def test_internal_checkpoint_failure_preserves_evidence_without_human_wait(monkeypatch):
    claim = TaskCoordinatorClaim(
        "run", "task", "event", "wake", "claim",
        datetime.now(timezone.utc) + timedelta(minutes=5), 1,
    )
    task = SimpleNamespace(id="task", entity_id="entity", workspace_id="workspace", status="in_progress")
    run = SimpleNamespace(
        id="run", phase="running", current_plan_id="plan", state_version=1,
        claim_token=claim.claim_token, claim_generation=claim.wake_generation,
        wake_generation=claim.wake_generation, claim_expires_at=claim.expires_at,
        checkpoint={"messages": [{"role": "tool", "content": "original receipt"}]},
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, query):
            if len(query.column_descriptions) > 1:
                return SimpleNamespace(one_or_none=lambda: task)
            row = {Task: task, TaskCoordinatorRun: run}[query.column_descriptions[0]["entity"]]
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def commit(self):
            pass

    async def transition(row, status, **_):
        row.status = status

    log = AsyncMock()
    monkeypatch.setattr(worker, "create_worker_session", lambda: Session)
    monkeypatch.setattr(worker, "lock_workspace_access_boundary", AsyncMock())
    monkeypatch.setattr(worker, "apply_task_status_transition", transition)
    monkeypatch.setattr(worker, "add_task_log", log)
    monkeypatch.setattr(worker, "settle_task_coordinator_wake", AsyncMock(return_value=True))

    assert await worker._block_repair_required(claim, reason="Child identity mismatch")
    assert task.status == "blocked"
    assert run.phase == "failed"
    assert run.checkpoint["messages"] == [{"role": "tool", "content": "original receipt"}]
    outcome = run.checkpoint["coordinator_outcome"]
    assert outcome["contract"] == "tool_error"
    assert outcome["error"] == "coordinator_checkpoint_invalid"
    assert outcome["details"]["requires_human"] is False
    assert outcome["details"]["replayed_operations"] is False
    assert log.await_args.kwargs["metadata"]["coordinator_outcome"] == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["inspect_task_delivery", "submit_task"])
async def test_exhausted_read_backoff_returns_diagnostic_to_same_loop(monkeypatch, tool_name):
    class Observed(Exception):
        pass

    prepare = AsyncMock(side_effect=acceptance.TaskAcceptanceError("File writer holds lock", code="artifact_busy"))
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "save", "result": {"text": "Saved output"}}], "artifacts": [],
    })
    public_args = {"kind": "manifest"} if tool_name == "inspect_task_delivery" else {}

    async def loop(**kwargs):
        result = await kwargs["tool_executor"](tool_name, public_args)
        assert isinstance(result, RuntimePreflightValidationError)
        error = json.loads(result)
        assert error["error"] == "artifact_read_retry_exhausted"
        assert error["details"]["requires_human"] is False
        assert error["details"]["action_executed"] is False
        assert error["details"]["arguments"] == public_args
        assert error["details"]["attempts"] == worker._DELIVERY_READ_RETRY_LIMIT
        assert task.status == "in_progress" and run.phase == "running"
        # A later successful observation is accepted in this exact invocation,
        # without a human response, a new Plan or another write.
        prepare.side_effect = None
        prepare.return_value = (snapshot, None)
        observation = json.loads(await kwargs["tool_executor"]("inspect_task_delivery", {"kind": "manifest"}))
        assert observation["status"] == "ok"
        raise Observed

    task, run, _plan, _review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=loop, prepare=prepare, finalize=AsyncMock(),
    )
    run.checkpoint = worker._checkpoint_state(
        messages=[], usage={}, rounds=1, tool_calls_made=[], control=None,
        delivery_read_retry={"plan_id": "plan-1", "tool_name": tool_name,
                             "arguments": public_args, "attempts": worker._DELIVERY_READ_RETRY_LIMIT},
    )
    block = AsyncMock(side_effect=AssertionError("read errors must go back to the model"))
    monkeypatch.setattr(worker, "_block_repair_required", block)
    with pytest.raises(Observed):
        await worker.run_task_coordinator_once(event_id="event-1", task_id=task.id, wake_generation="wake-1")
    block.assert_not_awaited()
    replan.assert_not_awaited()
    delegate.assert_not_awaited()


@pytest.mark.asyncio
async def test_replan_exhaustion_reports_internal_failure_not_human_wait_or_completion(monkeypatch):
    diagnostic = {
        "contract": "tool_error", "status": "error", "tool": "replan_task",
        "error": "task_replan_unavailable", "message": "Recovery budget exhausted",
        "details": {"plan_id": "plan-1", "requires_human": False},
    }

    async def loop(**kwargs):
        result = json.loads(await kwargs["tool_executor"]("replan_task", {"issue": "Missing file receipt"}))
        assert result == diagnostic
        assert any(rule["json_equals"] == {"error": "task_replan_unavailable"}
                   for rule in kwargs["terminal_tool_result_policy"]["terminal_tool_results"])
        return SimpleNamespace(stop_reason="task_replan_unavailable")

    task, _run, _plan, _review, _replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=loop, prepare=AsyncMock(), finalize=AsyncMock(),
    )
    monkeypatch.setattr(worker, "prepare_task_coordinator_replan", AsyncMock(return_value=(
        worker.TaskCoordinatorReplanOutcome.FAILED, diagnostic, None,
    )))
    assert await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    ) == {"status": "failed", "task_id": task.id}
    delegate.assert_not_awaited()
