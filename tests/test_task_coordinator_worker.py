from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.contracts.task_delivery_inspection import DeliveryInspectionError
from packages.core.plans.planner import TaskCapabilityGapError
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.services import task_acceptance_review as acceptance
from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim
from packages.core.workers import task_coordinator as worker
from packages.core.workers.task_coordinator import (
    TASK_COORDINATOR_CONTROL_KEY,
    _checkpoint_state,
    _resume_checkpoint,
    _valid_wait_control,
)


def _control() -> dict:
    return {
        "version": 1,
        "plan_id": "plan-1",
        "step_ids": ["step-1"],
        "tool_call_id": "call-1",
    }


def test_crash_after_step_release_reconstructs_exact_pending_call() -> None:
    checkpoint = _checkpoint_state(
        messages=[
            {"role": "system", "content": "original"},
            {"role": "user", "content": "task"},
        ],
        usage={"total": 5},
        rounds=2,
        tool_calls_made=["plan_task"],
        control=_control(),
    )

    resume = _resume_checkpoint(checkpoint)

    assert resume is not None
    assert resume["pending_tool_call"] == {
        "id": "call-1",
        "name": "delegate_ready_steps",
        "arguments": {},
    }
    assert resume["messages"][-1]["tool_calls"][0]["id"] == "call-1"
    assert resume["usage"] == {"total": 5}
    assert TASK_COORDINATOR_CONTROL_KEY not in resume


def test_saved_pending_call_resumes_without_reconstructing_a_second_call() -> None:
    pending = RuntimeAgentCheckpoint(
        messages=[
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "delegate_ready_steps", "arguments": "{}"},
            }]},
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": "call-1", "name": "delegate_ready_steps", "arguments": {}},
    ).to_dict()
    pending[TASK_COORDINATOR_CONTROL_KEY] = _control()

    resume = _resume_checkpoint(pending)

    assert resume == {
        key: value for key, value in pending.items()
        if key != TASK_COORDINATOR_CONTROL_KEY
    }
    assert len(resume["messages"]) == 1


def test_mismatched_pending_child_call_fails_closed() -> None:
    checkpoint = _checkpoint_state(
        messages=[], usage={}, rounds=1, tool_calls_made=[], control=_control(),
    )
    checkpoint["pending_tool_call"] = {
        "id": "different-call", "name": "delegate_ready_steps", "arguments": {},
    }

    with pytest.raises(ValueError, match="does not match"):
        _resume_checkpoint(checkpoint)


def test_waiting_child_requires_exact_persisted_batch_identity() -> None:
    assert _valid_wait_control(_control(), "plan-1")
    assert not _valid_wait_control(_control(), "another-plan")
    assert not _valid_wait_control({**_control(), "step_ids": []}, "plan-1")
    assert not _valid_wait_control(
        {**_control(), "step_ids": ["step-1", "step-1"]}, "plan-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_claim", [False, True])
async def test_unrecoverable_capability_gap_fails_without_human_setup(monkeypatch, stale_claim) -> None:
    from packages.core.services import task_capability_recovery

    claim = TaskCoordinatorClaim(
        "run-1", "task-1", "event-1", "wake-1", "token-1",
        datetime.now(timezone.utc) + timedelta(minutes=5), 1,
    )
    task = SimpleNamespace(
        id="task-1", title="Create a document", description="", details={},
        expected_output=None, creator_id="user-1", entity_id="entity-1",
        workspace_id="workspace-1", conversation_id=None, status="in_progress",
    )
    prior_messages = [{"role": "tool", "content": "original planning evidence"}]
    run = SimpleNamespace(
        id="run-1", checkpoint={"messages": prior_messages}, phase="running", current_plan_id=None,
        root_agent_id="agent-1", root_subscription_id="subscription-1",
        state_version=1, claim_token="replaced" if stale_claim else claim.claim_token,
        claim_generation=claim.wake_generation, wake_generation=claim.wake_generation,
        claim_expires_at=claim.expires_at,
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query):
            if len(query.column_descriptions) > 1:
                return SimpleNamespace(one_or_none=lambda: task)
            row = {Task: task, TaskCoordinatorRun: run}[query.column_descriptions[0]["entity"]]
            return SimpleNamespace(scalar_one=lambda: row, scalar_one_or_none=lambda: row)

        async def commit(self):
            pass

    @asynccontextmanager
    async def fake_plan_claim(_task_id):
        yield SimpleNamespace()

    async def fake_heartbeat(_claim, _lost):
        await asyncio.Event().wait()

    tool_results = []

    async def fake_agent_loop(**kwargs):
        policy = kwargs["terminal_tool_result_policy"]["terminal_tool_results"]
        assert any(
            rule["tool_names"] == ["plan_task"]
            and rule["json_equals"] == {"error": "task_capability_unavailable"}
            for rule in policy
        )
        # A real Plan approval still owns the existing human-wait contract.
        assert any(
            rule["tool_names"] == ["plan_task"]
            and rule["json_equals"] == {"status": "waiting_human"}
            for rule in policy
        )
        tool_results.append(json.loads(await kwargs["tool_executor"]("plan_task", {})))
        assert json.loads(await kwargs["tool_executor"]("delegate_ready_steps", {})) == {"status": "failed"}
        return SimpleNamespace(stop_reason="task_capability_unavailable")

    affected = {"agent_id": "agent-1", "subscription_id": "subscription-1", "service_key": "research"}
    planner = AsyncMock(side_effect=TaskCapabilityGapError(
        "task-1", ("file.write",), affected_agents=(affected,),
    ))
    recovery = AsyncMock(side_effect=AssertionError("a strict capability failure cannot create a setup card"))
    log = AsyncMock()
    settle = AsyncMock(return_value=True)

    async def transition(row, status, **_kwargs):
        row.status = status

    monkeypatch.setattr(worker, "_admit_and_claim", AsyncMock(return_value=(claim, "claimed")))
    monkeypatch.setattr(worker, "_claim_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_heartbeat", fake_heartbeat)
    monkeypatch.setattr(worker, "create_worker_session", lambda: lambda: FakeSession())
    monkeypatch.setattr(worker, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="", tool_profile=None,
        model=None, llm_metadata={},
    )))
    monkeypatch.setattr(worker, "task_plan_execution_claim", fake_plan_claim)
    monkeypatch.setattr(worker, "plan_task", planner)
    monkeypatch.setattr(worker, "pending_task_plan_submission", AsyncMock(return_value=None))
    monkeypatch.setattr(worker, "lock_workspace_access_boundary", AsyncMock())
    monkeypatch.setattr(worker, "apply_task_status_transition", transition)
    monkeypatch.setattr(worker, "add_task_log", log)
    monkeypatch.setattr(worker, "settle_task_coordinator_wake", settle)
    monkeypatch.setattr(task_capability_recovery, "record_capability_blocker", recovery)
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", fake_agent_loop)
    monkeypatch.setattr(worker, "release_task_coordinator_claim", AsyncMock())

    if stale_claim:
        with pytest.raises(RuntimeError, match="lost its claim"):
            await worker.run_task_coordinator_once(
                event_id="event-1", task_id="task-1", wake_generation="wake-1",
            )
        assert task.status == "in_progress" and run.phase == "running"
        assert run.checkpoint == {"messages": prior_messages}
        log.assert_not_awaited()
        settle.assert_not_awaited()
    else:
        outcome = await worker.run_task_coordinator_once(
            event_id="event-1", task_id="task-1", wake_generation="wake-1",
        )

        assert outcome == {"status": "failed", "task_id": "task-1"}
        assert task.status == "blocked" and run.phase == "failed"
        assert tool_results[0]["error"] == "task_capability_unavailable"
        assert tool_results[0]["status"] == "error"
        assert tool_results[0]["details"] == {
            "missing_capabilities": ["file.write"], "affected_agents": [affected],
            "requires_human": False,
        }
        assert run.checkpoint["messages"] == prior_messages
        failure = run.checkpoint["coordinator_outcome"]
        assert failure["error"] == "task_capability_unavailable"
        assert failure["tool"] == "plan_task"
        assert failure["details"]["missing_capabilities"] == ["file.write"]
        assert failure["details"]["affected_agents"] == [affected]
        assert failure["details"]["requires_human"] is False
        assert failure["details"]["checkpoint_preserved"] is True
        assert failure["details"]["replayed_operations"] is False
        assert log.await_args.kwargs["metadata"]["coordinator_outcome"] == failure
        settle.assert_awaited_once()
    planner.assert_awaited_once()
    recovery.assert_not_awaited()
    assert task.details == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["invalid_citation", "stale_digest", "changes_required"])
async def test_submission_assessment_stays_in_task_agent_loop(
    monkeypatch, scenario: str,
) -> None:
    """The coordinator inspects and corrects its own assessment, without a second reviewer."""
    task = SimpleNamespace(
        id="task-1", title="Create a document", description="", details={},
        expected_output=None, creator_id="user-1", entity_id="entity-1",
        workspace_id="workspace-1", conversation_id=None, status="in_progress",
    )
    run = SimpleNamespace(
        id="run-1", checkpoint={}, phase="running", current_plan_id="plan-1",
        root_agent_id="agent-1", root_subscription_id="subscription-1",
        state_version=1,
    )
    plan = SimpleNamespace(id="plan-1", task_id="task-1", status="running")
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": task.title, "description": task.description},
        "plan_id": plan.id,
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [{
            "document_id": "doc-1", "fs_path": "reference.txt",
            "text_available": True, "text_truncated": False,
            "text": "Reference evidence: " + "A" * 5900 + " end of evidence.",
        }],
    })
    checkpoints = []
    claim = TaskCoordinatorClaim(
        "run-1", "task-1", "event-1", "wake-1", "token-1",
        datetime.now(timezone.utc) + timedelta(minutes=5), 1,
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query):
            if len(query.column_descriptions) > 1:
                scope = SimpleNamespace(entity_id=task.entity_id, workspace_id=task.workspace_id)
                return SimpleNamespace(one_or_none=lambda: scope)
            entity = query.column_descriptions[0].get("entity")
            row = {Task: task, TaskCoordinatorRun: run, ExecutionPlan: plan}[entity]
            return SimpleNamespace(scalar_one=lambda: row, scalar_one_or_none=lambda: row)

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def fake_heartbeat(_claim, _lost):
        await asyncio.Event().wait()

    async def fake_checkpoint(_db, **kwargs):
        assert kwargs["task_id"] == task.id
        assert kwargs["claim_token"] == run.claim_token
        assert kwargs["claim_generation"] == run.claim_generation
        assert kwargs["expected_state_version"] == run.state_version
        checkpoints.append(kwargs)
        run.checkpoint = kwargs["checkpoint"]
        run.state_version = kwargs["expected_state_version"] + 1
        return run.state_version

    async def fake_agent_loop(**kwargs):
        assert kwargs.get("forced_tool_calls") is None
        assert {tool["function"]["name"] for tool in kwargs["tools"]} >= {
            "inspect_task_delivery", "submit_task",
        }
        manifest = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "manifest"},
        ))
        assert manifest["status"] == "ok"
        assert manifest["evidence_digest"] == snapshot.evidence_digest
        assert manifest["step_count"] == 1
        assert manifest["artifact_count"] == 1
        assert manifest["catalog"][0]["result_complete"] is True
        assert manifest["catalog"][0]["result"] == {"text": "Saved output"}
        empty = json.loads(await kwargs["tool_executor"]("submit_task", {}))
        assert empty["status"] == "assessment_required"
        assert empty["evidence_digest"] == snapshot.evidence_digest
        assert isinstance(empty["manifest"], dict)
        assert empty["manifest"]["catalog"] == manifest["catalog"]
        assessment = {
            "evidence_digest": snapshot.evidence_digest,
            "verdict": "accepted", "evidence": "The delivered text is present.",
            "issues": [],
            "checks": [
                {
                    "criterion": "A written result is delivered.",
                    "pointer": "/steps/0/result/text", "quote": "Saved output",
                },
                {
                    "criterion": "The reference artifact was inspected.",
                    "pointer": "/artifacts/0/text", "quote": "end of evidence.",
                },
            ],
        }
        early_result = await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        )
        assert isinstance(early_result, RuntimePreflightValidationError)
        early = json.loads(early_result)
        assert early["contract"] == "tool_error"
        assert early["error"] == "inspection_required"
        # A manifest result returned earlier in this same model tool batch
        # has not yet been appended to the model's transcript. Its inlined
        # Step result therefore cannot count as inspected at submit time.
        assert "step:0:0" in early["details"]["missing_pages"]
        assert "artifact:0:0" in early["details"]["missing_pages"]
        if scenario == "changes_required":
            negative_early = json.loads(await kwargs["tool_executor"](
                "submit_task", {"assessment": {
                    **assessment, "verdict": "changes_required",
                    "evidence": "The requested title is missing.",
                    "issues": ["Add the requested title."], "checks": [],
                }},
            ))
            assert negative_early["error"] == "inspection_required"
            assert "artifact:0:0" in negative_early["details"]["missing_pages"]
        step_page = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "step", "index": 0},
        ))
        assert step_page["status"] == "ok"
        assert step_page["evidence_digest"] == snapshot.evidence_digest
        assert step_page["result_pointer"] == "/steps/0/result"
        assert step_page["citations"] == [{
            "citation_pointer": "/steps/0/result/text", "value_type": "string",
        }]
        assert "Saved output" in step_page["content"]
        raw_page = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {
                "kind": "evidence", "pointer": step_page["citations"][0]["citation_pointer"],
            },
        ))
        assert raw_page["content"] == "Saved output"
        assert raw_page["pointer"] == "/steps/0/result/text"
        invalid_page = await kwargs["tool_executor"](
            "inspect_task_delivery", {
                "kind": "evidence", "pointer": "/steps/0/params/prompt",
            },
        )
        assert isinstance(invalid_page, RuntimePreflightValidationError)
        assert json.loads(invalid_page)["error"] == "invalid_inspection"
        assert json.loads(invalid_page)["contract"] == "tool_error"
        artifact_offset = 0
        inspected_offsets = []
        while True:
            artifact_page = json.loads(await kwargs["tool_executor"](
                "inspect_task_delivery", {"kind": "artifact", "index": 0, "offset": artifact_offset},
            ))
            assert artifact_page["status"] == "ok"
            assert artifact_page["evidence_digest"] == snapshot.evidence_digest
            assert artifact_page["pointer"] == "/artifacts/0/text"
            assert len(artifact_page["content"]) <= 2800
            inspected_offsets.append(artifact_offset)
            if artifact_offset == 0:
                still_missing = json.loads(await kwargs["tool_executor"](
                    "submit_task", {"assessment": assessment},
                ))
                assert still_missing["error"] == "inspection_required"
                assert "artifact:0:2800" in still_missing["details"]["missing_pages"]
            if artifact_page["next_offset"] is None:
                assert "end of evidence." in artifact_page["content"]
                break
            assert artifact_page["next_offset"] > artifact_offset
            artifact_offset = artifact_page["next_offset"]
        assert len(inspected_offsets) >= 3
        same_round = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        ))
        assert same_round["error"] == "inspection_required"
        assert "manifest:0" in same_round["details"]["missing_pages"]
        # Only after the strict round checkpoint will the inspected tool
        # results be part of the next model call's visible transcript.
        await kwargs["on_round_end"](1, [], {}, ["inspect_task_delivery", "submit_task"])
        if scenario == "invalid_citation":
            invalid = {**assessment, "checks": [
                {
                    "criterion": "A written result is delivered.",
                    "pointer": "/steps/0/params/prompt", "quote": "Saved output",
                },
                assessment["checks"][1],
            ]}
        elif scenario == "stale_digest":
            invalid = {**assessment, "evidence_digest": "0" * 64}
        else:
            invalid = {
                **assessment, "verdict": "changes_required",
                "evidence": "The requested title is missing.",
                "issues": ["Add the requested title."], "checks": [],
            }
        first_result = await kwargs["tool_executor"](
            "submit_task", {"assessment": invalid},
        )
        if scenario != "changes_required":
            assert isinstance(first_result, RuntimePreflightValidationError)
        else:
            assert not isinstance(first_result, RuntimePreflightValidationError)
        first = json.loads(first_result)
        if scenario == "changes_required":
            assert first["status"] == "blocked"
            assert first["issues"] == ["Add the requested title."]
            await kwargs["on_round_end"](worker._MAX_ROUNDS, [], {}, ["inspect_task_delivery", "submit_task"])
            return SimpleNamespace(
                stop_reason="model_budget_exhausted", rounds=worker._MAX_ROUNDS, messages=[], usage={},
                tool_calls_made=["inspect_task_delivery", "submit_task"],
            )
        if scenario == "invalid_citation":
            assert first["contract"] == "tool_error"
            assert first["status"] == "error"
            assert first["error"] == "invalid_assessment"
            assert first["details"]["cause"]
            assert "next_action" not in first
        else:
            assert first["contract"] == "tool_error"
            assert first["status"] == "error"
            assert first["error"] == "invalid_assessment"
            assert first["details"]["path"] == "$.assessment.evidence_digest"
            assert first["details"]["evidence_digest"] == snapshot.evidence_digest
        if scenario == "stale_digest":
            assert "digest" in first["message"].lower() or "stale" in first["message"].lower()
            # A stale digest invalidates earlier page observations. The same
            # loop must inspect the current snapshot again before acceptance.
            await kwargs["tool_executor"]("inspect_task_delivery", {"kind": "manifest"})
            await kwargs["tool_executor"]("inspect_task_delivery", {"kind": "step", "index": 0})
            for offset in inspected_offsets:
                await kwargs["tool_executor"](
                    "inspect_task_delivery", {"kind": "artifact", "index": 0, "offset": offset},
                )
        else:
            cause = first["details"]["cause"].lower()
            assert "citation" in cause or "pointer" in cause
        await kwargs["on_round_end"](1, [], {}, ["inspect_task_delivery", "submit_task"])
        corrected = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        ))
        assert corrected["status"] == "completed", corrected
        return SimpleNamespace(
            stop_reason="task_submitted", rounds=2, messages=[], usage={},
            tool_calls_made=["inspect_task_delivery", "submit_task", "submit_task"],
        )

    review = AsyncMock(side_effect=AssertionError("separate review must not run"))
    replan = AsyncMock(side_effect=AssertionError("assessment cannot replan the task"))
    stage = AsyncMock(side_effect=AssertionError("assessment cannot stage a retry wake"))
    finalize = AsyncMock(return_value=(True, None, None))
    recovery_hitl = AsyncMock(side_effect=AssertionError("assessment cannot create user HITL"))
    log = AsyncMock()

    async def fake_admit(**kwargs):
        assert kwargs["wake_generation"] == claim.wake_generation
        run.claim_token = claim.claim_token
        run.claim_generation = claim.wake_generation
        run.wake_generation = claim.wake_generation
        run.state_version = claim.state_version
        return claim, "claimed"

    async def fake_transition(row, status, **_kwargs):
        row.status = status

    monkeypatch.setattr(worker, "_admit_and_claim", fake_admit)
    monkeypatch.setattr(worker, "_claim_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_heartbeat", fake_heartbeat)
    monkeypatch.setattr(worker, "create_worker_session", lambda: lambda: FakeSession())
    monkeypatch.setattr(worker, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="", tool_profile=None,
        model=None, llm_metadata={},
    )))
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", fake_agent_loop)
    monkeypatch.setattr(worker, "checkpoint_task_coordinator_run", fake_checkpoint)
    monkeypatch.setattr(worker, "stage_task_coordinator_wake", stage)
    monkeypatch.setattr(worker, "prepare_task_coordinator_submission", AsyncMock(
        return_value=(snapshot, None),
    ))
    from packages.core.services import artifact_delivery, task_requester_identity
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=task.creator_id),
    )
    monkeypatch.setattr(
        artifact_delivery, "verify_artifact_delivery_refs",
        AsyncMock(return_value=SimpleNamespace(snapshots=[])),
    )
    monkeypatch.setattr(
        artifact_delivery, "verify_artifact_snapshot_access", AsyncMock(),
    )
    monkeypatch.setattr(acceptance, "review_task_acceptance", review, raising=False)
    monkeypatch.setattr(worker, "finalize_task_coordinator_submission", finalize)
    monkeypatch.setattr(worker, "prepare_task_coordinator_replan", replan)
    monkeypatch.setattr(worker, "lock_workspace_access_boundary", AsyncMock())
    monkeypatch.setattr(worker, "apply_task_status_transition", fake_transition)
    monkeypatch.setattr(worker, "add_task_log", log)
    monkeypatch.setattr(worker, "settle_task_coordinator_wake", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "release_task_coordinator_claim", AsyncMock())
    from packages.core.services import task_chat_hitl
    monkeypatch.setattr(task_chat_hitl, "ensure_task_recovery_hitl", recovery_hitl)
    monkeypatch.setattr(worker.PlanExecutor, "_emit_task_event", AsyncMock())
    monkeypatch.setattr(worker.PlanExecutor, "_cleanup_terminal_approvals_after_commit", AsyncMock())

    outcome = await worker.run_task_coordinator_once(
        event_id="event-1", task_id="task-1", wake_generation="wake-1",
    )
    assert outcome["status"] == ("blocked" if scenario == "changes_required" else "completed")
    assert plan.status == "running"
    assert len(checkpoints) == 2
    assert "task_acceptance_retry" not in run.checkpoint
    assert task.status != "waiting_on_customer"
    assert finalize.await_count == (0 if scenario == "changes_required" else 1)
    if scenario == "changes_required":
        assert task.status == "blocked"
        assert run.phase == "failed"
    if scenario != "changes_required":
        assert finalize.await_args.kwargs["acceptance_review"].snapshot_digest == snapshot.digest
        log.assert_not_awaited()
    review.assert_not_awaited()
    replan.assert_not_awaited()
    stage.assert_not_awaited()
    recovery_hitl.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_at", ["inspection", "finalizer"])
async def test_artifact_busy_retries_exact_read_without_replaying_steps(
    monkeypatch, busy_at: str,
) -> None:
    """A file-lock read retry is host-owned and reuses the saved Plan and tool args."""
    task = SimpleNamespace(
        id="task-1", title="Verify output", description="", details={},
        expected_output=None, creator_id="user-1", entity_id="entity-1",
        workspace_id="workspace-1", conversation_id=None, status="in_progress",
    )
    run = SimpleNamespace(
        id="run-1", checkpoint={}, phase="running", current_plan_id="plan-1",
        root_agent_id="agent-1", root_subscription_id="subscription-1",
        state_version=1, wake_generation="wake-1",
    )
    plan = SimpleNamespace(id="plan-1", task_id=task.id, status="running")
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": task.title}, "plan_id": plan.id,
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [],
    })
    assessment = {
        "evidence_digest": snapshot.evidence_digest,
        "verdict": "accepted", "evidence": "Saved output is present.",
        "issues": [], "checks": [{
            "criterion": "The requested output was saved.",
            "pointer": "/steps/0/result/text", "quote": "Saved output",
        }],
    }
    requested_name = "inspect_task_delivery" if busy_at == "inspection" else "submit_task"
    requested_args = (
        {"kind": "step", "index": 0, "offset": 0}
        if busy_at == "inspection" else {"assessment": assessment}
    )
    loop_calls = []
    staged = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query):
            if len(query.column_descriptions) > 1:
                scope = SimpleNamespace(entity_id=task.entity_id, workspace_id=task.workspace_id)
                return SimpleNamespace(one_or_none=lambda: scope)
            entity = query.column_descriptions[0].get("entity")
            row = {Task: task, TaskCoordinatorRun: run, ExecutionPlan: plan}[entity]
            return SimpleNamespace(scalar_one=lambda: row, scalar_one_or_none=lambda: row)

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def fake_heartbeat(_claim, _lost):
        await asyncio.Event().wait()

    async def fake_admit(**kwargs):
        index = int(kwargs["wake_generation"].rsplit("-", 1)[-1])
        claim = TaskCoordinatorClaim(
            "run-1", "task-1", f"event-{index}", f"wake-{index}", f"token-{index}",
            datetime.now(timezone.utc) + timedelta(minutes=5), run.state_version,
        )
        run.claim_token = claim.claim_token
        run.claim_generation = claim.wake_generation
        run.wake_generation = claim.wake_generation
        return claim, "claimed"

    async def fake_checkpoint(_db, **kwargs):
        assert kwargs["task_id"] == task.id
        assert kwargs["claim_token"] == run.claim_token
        assert kwargs["claim_generation"] == run.claim_generation
        assert kwargs["expected_state_version"] == run.state_version
        run.checkpoint = kwargs["checkpoint"]
        run.state_version += 1
        return run.state_version

    async def fake_stage(_db, **kwargs):
        assert kwargs["task_id"] == task.id
        assert kwargs["expected_state_version"] == run.state_version
        assert kwargs["claim_token"] == run.claim_token
        assert kwargs["claim_generation"] == run.claim_generation
        staged.append(kwargs)
        run.state_version += 1
        run.wake_generation = "wake-2"
        return SimpleNamespace(event_id="event-2", wake_generation="wake-2")

    async def fake_agent_loop(**kwargs):
        loop_calls.append(kwargs)
        if len(loop_calls) == 1:
            assert kwargs.get("forced_tool_calls") is None
            if busy_at == "finalizer":
                manifest = json.loads(await kwargs["tool_executor"](
                    "inspect_task_delivery", {"kind": "manifest"},
                ))
                assert manifest["status"] == "ok"
                await kwargs["on_round_end"](1, [], {}, ["inspect_task_delivery"])
            busy = json.loads(await kwargs["tool_executor"](
                requested_name, requested_args,
            ))
            assert busy["status"] == "retry_scheduled"
            return SimpleNamespace(
                stop_reason="delivery_read_retry_scheduled", rounds=1,
                messages=[], usage={}, tool_calls_made=[requested_name],
            )
        assert kwargs["forced_tool_calls"] == [{
            "name": requested_name, "arguments": requested_args,
        }]
        page = json.loads(await kwargs["tool_executor"](
            requested_name, requested_args,
        ))
        if busy_at == "inspection":
            assert page["status"] == "ok"
            assert page["evidence_digest"] == snapshot.evidence_digest
            await kwargs["on_round_end"](1, [], {}, [requested_name])
            manifest = json.loads(await kwargs["tool_executor"]("submit_task", {}))
            assert manifest["status"] == "assessment_required"
            await kwargs["on_round_end"](2, [], {}, [requested_name, "submit_task"])
            accepted = json.loads(await kwargs["tool_executor"](
                "submit_task", {"assessment": assessment},
            ))
            assert accepted["status"] == "completed"
        else:
            # The first wake checkpointed this exact manifest before the
            # finalizer lock failure, so forced submit may safely reuse it.
            assert page["status"] == "completed", page
        return SimpleNamespace(
            stop_reason="task_submitted", rounds=2, messages=[], usage={},
            tool_calls_made=[requested_name, "submit_task"],
        )

    busy = acceptance.TaskAcceptanceError("Artifact is locked", code="artifact_busy")
    prepare = AsyncMock(side_effect=(
        [busy, busy, busy, (snapshot, None), (snapshot, None)]
        if busy_at == "inspection" else [(snapshot, None)] * 3
    ))
    finalize = AsyncMock(side_effect=(
        [busy, (True, None, None)]
        if busy_at == "finalizer" else [(True, None, None)]
    ))
    replan = AsyncMock(side_effect=AssertionError("busy read cannot replan"))
    delegate = AsyncMock(side_effect=AssertionError("busy read cannot replay Steps"))
    review = AsyncMock(side_effect=AssertionError("separate reviewer must not run"))

    monkeypatch.setattr(worker, "_admit_and_claim", fake_admit)
    monkeypatch.setattr(worker, "_claim_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_heartbeat", fake_heartbeat)
    monkeypatch.setattr(worker, "create_worker_session", lambda: lambda: FakeSession())
    monkeypatch.setattr(worker, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="", tool_profile=None,
        model=None, llm_metadata={},
    )))
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", fake_agent_loop)
    monkeypatch.setattr(worker, "checkpoint_task_coordinator_run", fake_checkpoint)
    monkeypatch.setattr(worker, "stage_task_coordinator_wake", fake_stage)
    monkeypatch.setattr(worker, "prepare_task_coordinator_submission", prepare)
    monkeypatch.setattr(worker, "finalize_task_coordinator_submission", finalize)
    monkeypatch.setattr(worker, "prepare_task_coordinator_replan", replan)
    monkeypatch.setattr(worker, "release_ready_task_coordinator_steps", delegate)
    monkeypatch.setattr(acceptance, "review_task_acceptance", review, raising=False)
    monkeypatch.setattr(worker, "settle_task_coordinator_wake", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "release_task_coordinator_claim", AsyncMock())
    monkeypatch.setattr(worker.PlanExecutor, "_emit_task_event", AsyncMock())
    monkeypatch.setattr(worker.PlanExecutor, "_cleanup_terminal_approvals_after_commit", AsyncMock())
    from packages.core.services import task_chat_hitl
    recovery_hitl = AsyncMock(side_effect=AssertionError("busy read cannot ask the user"))
    monkeypatch.setattr(task_chat_hitl, "ensure_task_recovery_hitl", recovery_hitl)

    first = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert first["status"] == "retry_scheduled"
    assert len(staged) == 1
    assert run.checkpoint["task_delivery_read_retry"] == {
        "version": 1, "plan_id": plan.id, "tool_name": requested_name,
        "arguments": requested_args, "attempts": 1,
    }
    assert task.status == "in_progress" and plan.status == "running"

    second = await worker.run_task_coordinator_once(
        event_id="event-2", task_id=task.id, wake_generation="wake-2",
    )
    assert second["status"] == "completed"
    assert len(loop_calls) == 2
    # The final assessment must refresh the snapshot after the cached read.
    assert prepare.await_count == (5 if busy_at == "inspection" else 3)
    if busy_at == "inspection":
        assert "task_delivery_read_retry" not in run.checkpoint
    assert finalize.await_count == (1 if busy_at == "inspection" else 2)
    assert len(staged) == 1
    replan.assert_not_awaited()
    delegate.assert_not_awaited()
    review.assert_not_awaited()
    recovery_hitl.assert_not_awaited()


def _install_delivery_inspection_fakes(
    monkeypatch, *, agent_loop, prepare, finalize,
    verify_access=None, verify_text=None,
):
    """Only the coordinator boundary is real; no provider, database, or child Step runs."""
    task = SimpleNamespace(
        id="task-1", title="Verify output", description="", details={},
        expected_output=None, creator_id="user-1", entity_id="entity-1",
        workspace_id="workspace-1", conversation_id=None, status="in_progress",
    )
    run = SimpleNamespace(
        id="run-1", checkpoint={}, phase="running", current_plan_id="plan-1",
        root_agent_id="agent-1", root_subscription_id="subscription-1",
        state_version=1,
    )
    plan = SimpleNamespace(id="plan-1", task_id=task.id, status="running")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query):
            if len(query.column_descriptions) > 1:
                scope = SimpleNamespace(entity_id=task.entity_id, workspace_id=task.workspace_id)
                return SimpleNamespace(one_or_none=lambda: scope)
            entity = query.column_descriptions[0].get("entity")
            row = {Task: task, TaskCoordinatorRun: run, ExecutionPlan: plan}[entity]
            return SimpleNamespace(scalar_one=lambda: row, scalar_one_or_none=lambda: row)

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def fake_heartbeat(_claim, _lost):
        await asyncio.Event().wait()

    async def fake_admit(**kwargs):
        claim = TaskCoordinatorClaim(
            run.id, task.id, kwargs["event_id"], kwargs["wake_generation"], "token-1",
            datetime.now(timezone.utc) + timedelta(minutes=5), run.state_version,
        )
        run.claim_token = claim.claim_token
        run.claim_generation = claim.wake_generation
        run.wake_generation = claim.wake_generation
        return claim, "claimed"

    async def fake_checkpoint(_db, **kwargs):
        assert kwargs["task_id"] == task.id
        assert kwargs["claim_token"] == run.claim_token
        assert kwargs["claim_generation"] == run.claim_generation
        assert kwargs["expected_state_version"] == run.state_version
        run.checkpoint = kwargs["checkpoint"]
        run.state_version += 1
        return run.state_version

    review = AsyncMock(side_effect=AssertionError("separate reviewer must not run"))
    replan = AsyncMock(side_effect=AssertionError("delivery inspection cannot replan"))
    delegate = AsyncMock(side_effect=AssertionError("delivery inspection cannot rerun Steps"))
    monkeypatch.setattr(worker, "_admit_and_claim", fake_admit)
    monkeypatch.setattr(worker, "_claim_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_heartbeat", fake_heartbeat)
    monkeypatch.setattr(worker, "create_worker_session", lambda: lambda: FakeSession())
    monkeypatch.setattr(worker, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="", tool_profile=None,
        model=None, llm_metadata={},
    )))
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", agent_loop)
    monkeypatch.setattr(worker, "checkpoint_task_coordinator_run", fake_checkpoint)
    monkeypatch.setattr(worker, "prepare_task_coordinator_submission", prepare)
    monkeypatch.setattr(worker, "finalize_task_coordinator_submission", finalize)
    monkeypatch.setattr(worker, "prepare_task_coordinator_replan", replan)
    monkeypatch.setattr(worker, "release_ready_task_coordinator_steps", delegate)
    monkeypatch.setattr(acceptance, "review_task_acceptance", review, raising=False)
    monkeypatch.setattr(worker, "settle_task_coordinator_wake", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "release_task_coordinator_claim", AsyncMock())
    monkeypatch.setattr(worker.PlanExecutor, "_emit_task_event", AsyncMock())
    monkeypatch.setattr(worker.PlanExecutor, "_cleanup_terminal_approvals_after_commit", AsyncMock())
    from packages.core.services import artifact_delivery, task_requester_identity, task_chat_hitl
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=task.creator_id),
    )
    monkeypatch.setattr(
        artifact_delivery, "verify_artifact_delivery_refs",
        verify_text or AsyncMock(return_value=SimpleNamespace(snapshots=[])),
    )
    monkeypatch.setattr(
        artifact_delivery, "verify_artifact_snapshot_access",
        verify_access or AsyncMock(),
    )
    monkeypatch.setattr(
        task_chat_hitl, "ensure_task_recovery_hitl",
        AsyncMock(side_effect=AssertionError("delivery inspection cannot ask the user")),
    )
    return task, run, plan, review, replan, delegate


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["inspect_task_delivery", "submit_task"])
@pytest.mark.parametrize("resumed_busy_read", [False, True])
async def test_nonretryable_delivery_error_keeps_cause_without_scheduling_busy_retry(
    monkeypatch, tool_name, resumed_busy_read,
) -> None:
    error = acceptance.TaskAcceptanceError(
        "The delivered file is missing.", code="artifact_missing",
        details={"cause": "The verified document has no stored content", "document_id": "doc-1"},
    )
    prepare = AsyncMock(side_effect=error)
    finalize = AsyncMock(side_effect=AssertionError("unverified delivery cannot finalize"))
    stage = AsyncMock(side_effect=AssertionError("permanent failure cannot schedule a busy retry"))
    public_args = {"kind": "manifest"} if tool_name == "inspect_task_delivery" else {}

    class Observed(Exception):
        pass

    async def fake_agent_loop(**kwargs):
        assert bool(kwargs.get("forced_tool_calls")) is resumed_busy_read
        result = await kwargs["tool_executor"](tool_name, public_args)
        assert isinstance(result, RuntimePreflightValidationError)
        payload = json.loads(result)
        assert payload == {
            "contract": "tool_error", "status": "error", "tool": tool_name,
            "error": "artifact_missing", "message": str(error), "details": {
                **error.details, "failure_scope": "task_submission",
                "action_executed": False, "committed": False,
            },
        }
        await kwargs["on_round_end"](1, [], {}, [tool_name])
        raise Observed

    task, run, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop, prepare=prepare, finalize=finalize,
    )
    monkeypatch.setattr(worker, "stage_task_coordinator_wake", stage)
    if resumed_busy_read:
        run.checkpoint = _checkpoint_state(
            messages=[], usage={}, rounds=0, tool_calls_made=[], control=None,
            delivery_read_retry={
                "version": 1, "plan_id": "plan-1", "tool_name": tool_name,
                "arguments": public_args, "attempts": 1,
            },
        )
    with pytest.raises(Observed):
        await worker.run_task_coordinator_once(
            event_id="event-1", task_id=task.id, wake_generation="wake-1",
        )
    prepare.assert_awaited_once()
    stage.assert_not_awaited()
    finalize.assert_not_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()
    assert "task_delivery_read_retry" not in run.checkpoint
    assert task.status == "in_progress"
    assert plan.status == "running"


@pytest.mark.asyncio
async def test_invalid_assessment_reaches_next_model_round_without_releasing_steps(
    monkeypatch,
) -> None:
    """A host-proven citation rejection stays inside the real serial Agent loop."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [],
    })
    prepare = AsyncMock(return_value=(snapshot, None))
    finalize = AsyncMock(return_value=(True, None, None))
    assessment = {
        "evidence_digest": snapshot.evidence_digest,
        "verdict": "accepted", "evidence": "Saved output is present.",
        "issues": [], "checks": [{
            "criterion": "Output delivered", "pointer": "/steps/0/result/text",
            "quote": "Saved output",
        }],
    }
    model_calls = 0

    async def model(messages, _tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [
                {"id": "manifest", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"}},
                {"id": "step", "name": "inspect_task_delivery", "arguments": {"kind": "step", "index": 0}},
            ], {}
        if model_calls == 2:
            return "", [
                {"id": "wrong-citation", "name": "submit_task", "arguments": {
                    "assessment": {**assessment, "checks": [{
                        **assessment["checks"][0], "pointer": "/steps/0/result",
                        "quote": "Saved output",
                    }]},
                }},
                {"id": "must-not-run", "name": "delegate_ready_steps", "arguments": {}},
            ], {}
        assert model_calls == 3
        tool_results = [message for message in messages if message.get("role") == "tool"]
        assert any(
            json.loads(item.get("content") or "{}").get("error") == "invalid_assessment"
            for item in tool_results
        )
        assert all(item.get("tool_call_id") != "must-not-run" for item in tool_results)
        return "", [{"id": "corrected", "name": "submit_task", "arguments": {
            "assessment": assessment,
        }}], {}

    async def real_loop(**kwargs):
        return await loop_module.agentic_loop(
            system_prompt=kwargs["system_prompt"],
            user_message=kwargs["user_message"],
            tools=kwargs["tools"],
            tool_executor=kwargs["tool_executor"],
            max_rounds=kwargs["max_rounds"],
            on_round_end=kwargs["on_round_end"],
            on_round_end_strict=kwargs["on_round_end_strict"],
            on_llm_call_before=kwargs["on_llm_call_before"],
            runtime_cancel_checker=kwargs["runtime_cancel_checker"],
            terminal_tool_result_policy=kwargs["terminal_tool_result_policy"],
            tool_schema_resolver=kwargs["tool_schema_resolver"],
            final_response_contract=kwargs["final_response_contract"],
        )

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, run, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=real_loop, prepare=prepare, finalize=finalize,
    )
    result = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert result["status"] == "completed"
    assert model_calls == 3
    assert task.status == "in_progress" and plan.status == "running"
    assert run.checkpoint["task_delivery_inspection"]["pages"] == ["manifest:0", "step:0:0"]
    finalize.assert_awaited_once()
    prepare.assert_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspection_error_and_final_prose_are_corrected_in_original_loop(monkeypatch) -> None:
    """Wrong page arguments are observable constraints, not a reason for human approval."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify the patched document"}, "plan_id": "plan-1",
        "steps": [{"key": "patch", "result": {
            "text": "Existing document patched and verified.",
            "document_id": "doc-1", "fs_path": "existing.md", "sha256": "1" * 64,
        }}],
        "artifacts": [{
            "document_id": "doc-1", "fs_path": "existing.md", "sha256": "1" * 64,
            "text_available": True, "text_truncated": False,
            "text": "Verified replacement: " + "A" * 3000 + " with all required sections.",
        }],
    })
    prepare = AsyncMock(return_value=(snapshot, None))
    finalize = AsyncMock(return_value=(True, None, None))
    assessment = {
        "evidence_digest": snapshot.evidence_digest, "verdict": "accepted",
        "evidence": "The existing document was patched and contains the requested sections.",
        "issues": [], "checks": [{
            "criterion": "The repaired document contains every required section.",
            "pointer": "/artifacts/0/text", "quote": "with all required sections.",
        }],
    }
    model_calls = 0
    read_addresses = []
    loop_results = []

    async def model(messages, tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        assert {tool["function"]["name"] for tool in tools} >= {
            "inspect_task_delivery", "submit_task",
        }
        if model_calls == 1:
            return "", [{
                "id": "manifest", "name": "inspect_task_delivery",
                "arguments": {"kind": "manifest", "pointer": None},
            }], {}
        if model_calls == 2:
            return "", [{
                "id": "wrong-pointer", "name": "inspect_task_delivery",
                "arguments": {"kind": "step", "index": 0, "pointer": "/steps/0"},
            }], {}
        if model_calls == 3:
            error = json.loads(next(
                message["content"] for message in messages
                if message.get("tool_call_id") == "wrong-pointer"
            ))
            assert error["contract"] == "tool_error"
            assert error["error"] == "invalid_inspection"
            assert error["details"] == {
                "path": "$.pointer", "schema_rule": "forbidden_for_kind",
                "expected": {"kind": "step", "pointer": None},
            }
            return "The inspection failed. Please approve replacing the document.", [], {}
        if model_calls == 4:
            diagnostic = json.loads(messages[-1]["content"])
            assert diagnostic["contract"] == "tool_error"
            assert diagnostic["error"] == "task_submission_required"
            details = diagnostic["details"]
            assert details["completion_recorded"] is False
            assert details["execution_state_changed"] is False
            assert details["evidence_digest"] == snapshot.evidence_digest
            assert details["missing_page_count"] == 2
            assert {page["page_id"] for page in details["missing_inspections"]} == {
                "artifact:0:0", "artifact:0:2800",
            }
            read_addresses.extend(page["arguments"] for page in details["missing_inspections"])
            assert all(address["pointer"] is None for address in read_addresses)
            return "", [{
                "id": f"repair-read-{index}", "name": "inspect_task_delivery",
                "arguments": address,
            } for index, address in enumerate(read_addresses)], {}
        assert model_calls == 5
        for index in range(len(read_addresses)):
            page = json.loads(next(
                message["content"] for message in messages
                if message.get("tool_call_id") == f"repair-read-{index}"
            ))
            assert page["status"] == "ok"
            assert page["evidence_digest"] == snapshot.evidence_digest
        return "", [{
            "id": "submit-once", "name": "submit_task", "arguments": {"assessment": assessment},
        }], {}

    async def real_loop(**kwargs):
        keys = (
            "system_prompt", "user_message", "tools", "tool_executor", "max_rounds",
            "on_round_end", "on_round_end_strict", "on_llm_call_before", "runtime_cancel_checker",
            "terminal_tool_result_policy", "tool_schema_resolver", "final_response_contract",
        )
        result = await loop_module.agentic_loop(**{key: kwargs[key] for key in keys})
        loop_results.append(result)
        return result

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, run, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=real_loop, prepare=prepare, finalize=finalize,
    )
    stage = AsyncMock(side_effect=AssertionError("same-loop correction must not schedule another wake"))
    monkeypatch.setattr(worker, "stage_task_coordinator_wake", stage)
    outcome = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert outcome["status"] == "completed"
    assert model_calls == 5
    assert task.status == "in_progress" and plan.status == "running"
    assert loop_results[0].tool_calls_made.count("submit_task") == 1
    assert run.checkpoint["task_delivery_inspection"]["pages"] == [
        "manifest:0", "step:0:0", "artifact:0:0", "artifact:0:2800",
    ]
    finalize.assert_awaited_once()
    assert finalize.await_args.kwargs["acceptance_review"].snapshot_digest == snapshot.digest
    stage.assert_not_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsubmitted_prose_exhausts_bounded_recovery_without_human_wait(monkeypatch) -> None:
    """Repeated prose cannot fake success, create HITL, or trigger another file write."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "patch", "result": {"text": "Already patched"}}],
        "artifacts": [{
            "document_id": "doc-1", "fs_path": "existing.md", "sha256": "1" * 64,
            "text_available": True, "text_truncated": False, "text": "Saved document",
        }],
    })
    model_calls = 0

    async def model(messages, _tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [{
                "id": "manifest", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"},
            }], {}
        if model_calls > 2:
            diagnostic = json.loads(messages[-1]["content"])
            assert diagnostic["error"] == "task_submission_required"
            assert diagnostic["details"]["completion_recorded"] is False
        return "Please approve the already completed patch.", [], {}

    async def real_loop(**kwargs):
        keys = (
            "system_prompt", "user_message", "tools", "tool_executor", "max_rounds",
            "on_round_end", "on_round_end_strict", "on_llm_call_before", "runtime_cancel_checker",
            "terminal_tool_result_policy", "tool_schema_resolver", "final_response_contract",
        )
        return await loop_module.agentic_loop(**{key: kwargs[key] for key in keys})

    async def transition(row, status, **_kwargs):
        row.status = status

    finalize = AsyncMock(side_effect=AssertionError("prose cannot finalize a Task"))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, run, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=real_loop,
        prepare=AsyncMock(return_value=(snapshot, None)), finalize=finalize,
    )
    run.checkpoint = _checkpoint_state(
        messages=[], usage={}, rounds=0, tool_calls_made=[], control=None,
        non_submit_retries=worker._NON_SUBMIT_RETRIES,
    )
    stage = AsyncMock(side_effect=AssertionError("bounded recovery is exhausted"))
    log = AsyncMock()
    monkeypatch.setattr(worker, "stage_task_coordinator_wake", stage)
    monkeypatch.setattr(worker, "lock_workspace_access_boundary", AsyncMock())
    monkeypatch.setattr(worker, "apply_task_status_transition", transition)
    monkeypatch.setattr(worker, "add_task_log", log)

    result = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert result["status"] == "blocked"
    assert model_calls == 5
    assert task.status == "blocked" and run.phase == "failed"
    assert plan.status == "running"
    outcome = run.checkpoint["coordinator_outcome"]
    assert outcome["contract"] == "tool_error"
    assert outcome["error"] == "coordinator_acceptance_exhausted"
    assert outcome["details"]["runtime_error"] == "final_response_validation_failed"
    assert outcome["details"]["continuation_attempts"] == worker._NON_SUBMIT_RETRIES
    completion = outcome["details"]["completion"]
    assert completion["error"] == "task_submission_required"
    assert completion["details"]["evidence_digest"] == snapshot.evidence_digest
    assert completion["details"]["missing_inspections"] == [{
        "page_id": "artifact:0:0",
        "arguments": {"kind": "artifact", "index": 0, "offset": 0, "pointer": None},
    }]
    assert run.checkpoint["messages"][-1]["role"] == "user"
    assert json.loads(run.checkpoint["messages"][-1]["content"])["error"] == "task_submission_required"
    assert log.await_args.kwargs["metadata"]["coordinator_outcome"] == outcome
    finalize.assert_not_awaited()
    stage.assert_not_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "tool_evidence", "calls"),
    [
        ({"kind": "tool_error", "terminal_failure": True, "retryable": False, "source_tool": "publish_post",
          "do_not_replay": True, "effect_state": "unconfirmed"}, {}, [{"tool": "publish_post"}]),
        # max_rounds-style stop: no outcome control, only the receipt ledger.
        # Only tool and operation reach the diagnostic, never arguments.
        (None, {"effect_state": "unconfirmed", "unconfirmed_calls": [{
            "call_id": "c1", "tool": "publish_post", "operation": "create",
            "arguments": {"body": "secret draft"},
        }]}, [{"tool": "publish_post", "operation": "create"}]),
    ],
)
async def test_unconfirmed_effect_blocks_instead_of_continuing(monkeypatch, control, tool_evidence, calls) -> None:
    """A loop that stopped after an unconfirmed effect is never re-woken with tools."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "publish", "result": {"text": "Published"}}], "artifacts": [],
    })
    loop_calls = 0

    async def agent_loop(**_kwargs):
        nonlocal loop_calls
        loop_calls += 1
        return SimpleNamespace(
            content="", stop_reason="tool_error", rounds=1, messages=[], usage={},
            tool_calls_made=["publish_post"], error="tool_outcome_unconfirmed",
            control=control, tool_evidence=tool_evidence,
        )

    async def transition(row, status, **_kwargs):
        row.status = status

    finalize = AsyncMock(side_effect=AssertionError("an unconfirmed stop cannot finalize a Task"))
    task, run, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=agent_loop,
        prepare=AsyncMock(return_value=(snapshot, None)), finalize=finalize,
    )
    stage = AsyncMock(side_effect=AssertionError("an unconfirmed effect must not be continued"))
    log = AsyncMock()
    monkeypatch.setattr(worker, "stage_task_coordinator_wake", stage)
    monkeypatch.setattr(worker, "lock_workspace_access_boundary", AsyncMock())
    monkeypatch.setattr(worker, "apply_task_status_transition", transition)
    monkeypatch.setattr(worker, "add_task_log", log)

    result = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert result["status"] == "blocked"
    assert loop_calls == 1
    assert task.status == "blocked" and run.phase == "failed"
    outcome = run.checkpoint["coordinator_outcome"]
    assert outcome["error"] == "coordinator_tool_outcome_unconfirmed"
    assert outcome["message"] == (
        "Task coordination stopped after a step whose result could not be confirmed. "
        "Check whether it took effect before retrying; it was not repeated automatically."
    )
    details = outcome["details"]
    assert details["continuation_attempts"] == 0
    assert details["effect_state"] == "unconfirmed"
    assert details["unconfirmed_calls"] == calls
    assert details["do_not_replay"] is True
    assert details["requires_human"] is True
    stage.assert_not_awaited()
    finalize.assert_not_awaited()
    delegate.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_verification_and_final_recheck_correct_in_original_loop(monkeypatch):
    """Read/assessment failures cannot turn into a replay or a new supervisor."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}], "artifacts": [],
    })
    reads = 0

    async def prepare_current(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise acceptance.TaskAcceptanceError("Delivery changed during verification", code="artifact_changed")
        return snapshot, None

    prepare = AsyncMock(side_effect=prepare_current)
    finalize = AsyncMock(side_effect=[
        acceptance.TaskAcceptanceError("Assessment is stale", code="invalid_assessment"),
        (True, None, None),
    ])
    assessment = {
        "evidence_digest": snapshot.evidence_digest, "verdict": "accepted",
        "evidence": "Saved output is present.", "issues": [],
        "checks": [{"criterion": "Output delivered", "pointer": "/steps/0/result/text", "quote": "Saved output"}],
    }
    model_calls = 0

    async def model(messages, _tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        assert all(not item.get("tool_call_id", "").startswith("must-not-run") for item in tool_messages)
        if model_calls == 1:
            return "", [
                {"id": "changed-read", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"}},
                {"id": "must-not-run-1", "name": "delegate_ready_steps", "arguments": {}},
            ], {}
        if model_calls in {2, 4}:
            expected = "artifact_changed" if model_calls == 2 else "invalid_assessment"
            assert any(json.loads(item.get("content") or "{}").get("error") == expected for item in tool_messages)
            return "", [
                {"id": f"manifest-{model_calls}", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"}},
                {"id": f"step-{model_calls}", "name": "inspect_task_delivery", "arguments": {"kind": "step", "index": 0}},
            ], {}
        assert model_calls in {3, 5}
        calls = [{"id": f"submit-{model_calls}", "name": "submit_task", "arguments": {"assessment": assessment}}]
        if model_calls == 3:
            calls.append({"id": "must-not-run-2", "name": "delegate_ready_steps", "arguments": {}})
        return "", calls, {}

    async def real_loop(**kwargs):
        keys = ("system_prompt", "user_message", "tools", "tool_executor", "max_rounds", "on_round_end",
                "on_round_end_strict", "on_llm_call_before", "runtime_cancel_checker", "terminal_tool_result_policy", "tool_schema_resolver",
                "final_response_contract")
        return await loop_module.agentic_loop(**{key: kwargs[key] for key in keys})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, _, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=real_loop, prepare=prepare, finalize=finalize,
    )
    result = await worker.run_task_coordinator_once(event_id="event-1", task_id=task.id, wake_generation="wake-1")
    assert result["status"] == "completed" and model_calls == 5
    assert finalize.await_count == 2
    assert task.status == "in_progress" and plan.status == "running"
    replan.assert_not_awaited()
    delegate.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_forced_delivery_retry_can_correct_invalid_assessment(monkeypatch) -> None:
    """A resumed host read returning validation feedback still reaches the model."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [],
    })
    model_calls = 0

    class Observed(Exception):
        pass

    async def model(messages, _tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        errors = [json.loads(item["content"]) for item in messages if item.get("role") == "tool"]
        assert any(
            error.get("contract") == "tool_error"
            and error.get("error") == "tool_input_validation_failed"
            and error["details"].get("path") == "$.assessment"
            for error in errors
        )
        if model_calls == 1:
            return "", [{
                "id": "manifest", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"},
            }, {
                "id": "step", "name": "inspect_task_delivery", "arguments": {"kind": "step", "index": 0},
            }], {}
        assert model_calls == 2
        return "", [{
            "id": "corrected-assessment", "name": "submit_task", "arguments": {"assessment": {
                "evidence_digest": snapshot.evidence_digest, "verdict": "accepted",
                "evidence": "The delivered output is present.", "issues": [],
                "checks": [{
                    "criterion": "Output delivered", "pointer": "/steps/0/result/text", "quote": "Saved output",
                }],
            }},
        }], {}

    async def forced_loop(**kwargs):
        assert kwargs["forced_tool_calls"] == [{
            "name": "submit_task", "arguments": {"assessment": {"evidence_digest": snapshot.evidence_digest}},
        }]
        result = await loop_module.agentic_loop(
            system_prompt=kwargs["system_prompt"], user_message=kwargs["user_message"],
            tools=kwargs["tools"], tool_executor=kwargs["tool_executor"],
            forced_tool_calls=kwargs["forced_tool_calls"],
            on_round_end=kwargs["on_round_end"], on_round_end_strict=True,
            on_llm_call_before=kwargs["on_llm_call_before"],
            terminal_tool_result_policy=kwargs["terminal_tool_result_policy"],
            tool_schema_resolver=kwargs["tool_schema_resolver"],
            final_response_contract=kwargs["final_response_contract"],
            max_rounds=kwargs["max_rounds"],
        )
        assert result.error is None
        assert result.stop_reason == "task_submitted"
        assert model_calls == 2
        raise Observed

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=forced_loop,
        prepare=AsyncMock(return_value=(snapshot, None)),
        finalize=AsyncMock(return_value=(True, None, None)),
    )
    run.checkpoint = _checkpoint_state(
        messages=[], usage={}, rounds=0, tool_calls_made=[], control=None,
        delivery_read_retry={
            "version": 1, "plan_id": "plan-1", "tool_name": "submit_task",
            "arguments": {"assessment": {"evidence_digest": snapshot.evidence_digest}},
            "attempts": 1,
        },
    )
    with pytest.raises(Observed):
        await worker.run_task_coordinator_once(
            event_id="event-1", task_id=task.id, wake_generation="wake-1",
        )
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_submit_task_error_returns_to_model_without_releasing_siblings(monkeypatch) -> None:
    """A public failure is evidence for the model; it never releases sibling work."""
    calls = []
    model_calls = 0

    async def model(messages, tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [
                {"id": "unproven", "name": "submit_task", "arguments": {}},
                {"id": "must-not-run", "name": "delegate_ready_steps", "arguments": {}},
            ], {}
        assert any(
            item.get("role") == "tool"
            and item.get("tool_call_id") == "unproven"
            and "provider failure" in item.get("content", "")
            for item in messages
        )
        assert "must-not-run" not in json.dumps(messages)
        assert {schema["function"]["name"] for schema in tools} == {"submit_task", "delegate_ready_steps"}
        return "The assessment could not be submitted; its outcome needs review.", [], {}

    async def execute(name, _arguments):
        calls.append(name)
        return json.dumps({
            "status": "invalid_assessment", "error": "provider failure",
            "preflight_validation": True,
        })

    summary = AsyncMock(side_effect=AssertionError("a tool error no longer forces a tool-free summary"))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="finish the Task",
        tools=[worker._SCHEMAS["submit_task"], worker._SCHEMAS["delegate_ready_steps"]],
        tool_executor=execute, max_rounds=4,
    )
    # The failed submission was neither replayed nor treated as submitted,
    # and the dependent delegation in the same batch never ran.
    assert result.stop_reason == "completed"
    assert result.content == "The assessment could not be submitted; its outcome needs review."
    assert calls == ["submit_task"]
    assert model_calls == 2
    summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_rejection_can_choose_replan_in_same_loop(monkeypatch) -> None:
    """A negative content verdict is feedback, not a terminal tool failure."""
    calls = []
    model_calls = 0

    async def model(messages, _tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [{"id": "review", "name": "submit_task", "arguments": {}}], {}
        if model_calls == 2:
            assert any(
                '"verdict": "changes_required"' in str(item.get("content"))
                for item in messages if item.get("role") == "tool"
            )
            return "", [{"id": "repair", "name": "replan_task", "arguments": {
                "issue": "The requested title is missing.",
            }}], {}
        return "Await repaired Steps.", [], {}

    async def execute(name, _arguments):
        calls.append(name)
        if name == "submit_task":
            return json.dumps({
                "status": "blocked", "verdict": "changes_required",
                "reason": "The requested title is missing.",
                "issues": ["Add the requested title."],
            })
        return json.dumps({"status": "repaired", "plan_id": "plan-1"})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="verify the Task",
        tools=[worker._SCHEMAS["submit_task"], worker._SCHEMAS["replan_task"]],
        tool_executor=execute, max_rounds=4,
    )
    assert result.content == "Await repaired Steps."
    assert result.error is None
    assert calls == ["submit_task", "replan_task"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inspection_error,correctable", [
    (DeliveryInspectionError(
        "manifest offset exceeds catalog length", path="$.offset",
        schema_rule="maximum", expected=0,
    ), True),
    (acceptance.TaskAcceptanceError("A delivery manifest entry exceeds the safe inspection page size."), False),
])
async def test_inspection_argument_error_does_not_mask_unreviewable_evidence(
    monkeypatch, inspection_error, correctable: bool,
) -> None:
    """Only bad page arguments receive host-proven same-loop correction."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [],
    })

    class Observed(Exception):
        pass

    def failing_inspection(*_args, **_kwargs):
        raise inspection_error

    async def fake_agent_loop(**kwargs):
        result = await kwargs["tool_executor"]("inspect_task_delivery", {"kind": "manifest"})
        assert isinstance(result, RuntimePreflightValidationError) is correctable
        payload = json.loads(result)
        assert payload["status"] == "error"
        assert payload["contract"] == "tool_error"
        assert payload["error"] == ("invalid_inspection" if correctable else inspection_error.code)
        assert payload["message"] == str(inspection_error)
        assert "next_action" not in payload
        raise Observed

    monkeypatch.setattr(acceptance, "inspect_task_delivery", failing_inspection)
    task, _run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop,
        prepare=AsyncMock(return_value=(snapshot, None)),
        finalize=AsyncMock(side_effect=AssertionError("invalid inspection cannot finalize")),
    )
    with pytest.raises(Observed):
        await worker.run_task_coordinator_once(
            event_id="event-1", task_id=task.id, wake_generation="wake-1",
        )
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_batch_inspect_and_submit_cannot_claim_unseen_tool_results(monkeypatch) -> None:
    """Only evidence in the previous strict round checkpoint is model-visible."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [],
    })
    prepare = AsyncMock(return_value=(snapshot, None))
    finalize = AsyncMock(return_value=(True, None, None))

    async def fake_agent_loop(**kwargs):
        manifest = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "manifest"},
        ))
        assert manifest["status"] == "ok"
        assert manifest["catalog"][0]["result_complete"] is True
        assessment = {
            "evidence_digest": snapshot.evidence_digest,
            "verdict": "accepted", "evidence": "Saved output is present.",
            "issues": [], "checks": [{
                "criterion": "Output delivered", "pointer": "/steps/0/result/text",
                "quote": "Saved output",
            }],
        }
        same_batch = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        ))
        assert same_batch["error"] == "inspection_required"
        assert "manifest:0" in same_batch["details"]["missing_pages"]
        assert "step:0:0" in same_batch["details"]["missing_pages"]
        finalize.assert_not_awaited()
        await kwargs["on_round_end"](1, [], {}, ["inspect_task_delivery", "submit_task"])
        next_round = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        ))
        assert next_round["status"] == "completed", next_round
        return SimpleNamespace(stop_reason="task_submitted", rounds=2, messages=[], usage={})

    task, run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop, prepare=prepare, finalize=finalize,
    )
    result = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert result["status"] == "completed"
    assert run.checkpoint["task_delivery_inspection"]["pages"] == [
        "manifest:0", "step:0:0",
    ]
    assert prepare.await_count == 3
    finalize.assert_awaited_once()
    review.assert_not_awaited()
    replan.assert_not_awaited()
    delegate.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_inspection_cannot_mutate_checkpointed_visible_pages(monkeypatch) -> None:
    """New pages in a resumed batch stay invisible until its own strict checkpoint."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [{
            "document_id": "doc-1", "fs_path": "result.txt",
            "text_available": True, "text_truncated": False,
            "text": "artifact proof",
        }],
    })
    prepare = AsyncMock(return_value=(snapshot, None))
    finalize = AsyncMock(return_value=(True, None, None))
    prior_pages = ["manifest:0", "step:0:0"]
    assessment = {
        "evidence_digest": snapshot.evidence_digest,
        "verdict": "accepted", "evidence": "Saved output and artifact proof are present.",
        "issues": [], "checks": [{
            "criterion": "Output delivered", "pointer": "/steps/0/result/text",
            "quote": "Saved output",
        }],
    }

    async def fake_agent_loop(**kwargs):
        assert kwargs.get("forced_tool_calls") is None
        assert kwargs["initial_rounds"] == 1
        artifact = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "artifact", "index": 0},
        ))
        assert artifact["status"] == "ok"
        assert artifact["content"] == "artifact proof"
        # The live marker may grow, but the checkpointed marker is immutable
        # until this round's tool results enter the model's transcript.
        assert run.checkpoint["task_delivery_inspection"]["pages"] == prior_pages
        same_batch = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        ))
        assert same_batch["error"] == "inspection_required"
        assert same_batch["details"]["missing_pages"] == ["artifact:0:0"]
        finalize.assert_not_awaited()
        await kwargs["on_round_end"](2, [], {}, ["inspect_task_delivery", "submit_task"])
        assert run.checkpoint["task_delivery_inspection"]["pages"] == [
            *prior_pages, "artifact:0:0",
        ]
        next_round = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        ))
        assert next_round["status"] == "completed", next_round
        return SimpleNamespace(stop_reason="task_submitted", rounds=3, messages=[], usage={})

    task, run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop, prepare=prepare, finalize=finalize,
    )
    prior_manifest = acceptance.inspect_task_delivery(snapshot)
    run.checkpoint = _checkpoint_state(
        messages=[{
            "role": "tool", "name": "inspect_task_delivery",
            "content": json.dumps(prior_manifest),
        }],
        usage={}, rounds=1, tool_calls_made=["inspect_task_delivery"],
        control=None, delivery_inspection={
            "version": 1, "evidence_digest": snapshot.evidence_digest,
            "pages": prior_pages.copy(),
        },
    )
    result = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert result["status"] == "completed"
    assert prepare.await_count == 3
    finalize.assert_awaited_once()
    review.assert_not_awaited()
    replan.assert_not_awaited()
    delegate.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_submission_digest_invalidates_all_prior_inspection_pages(monkeypatch) -> None:
    """A prior artifact read cannot count toward a changed delivery snapshot."""
    payload = {
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [{
            "document_id": "doc-1", "fs_path": "result.txt",
            "text_available": True, "text_truncated": False, "text": "version A",
        }],
    }
    snapshot_a = acceptance.acceptance_snapshot(payload)
    payload_b = json.loads(json.dumps(payload))
    payload_b["artifacts"][0]["text"] = "version B"
    snapshot_b = acceptance.acceptance_snapshot(payload_b)
    assert snapshot_a.evidence_digest != snapshot_b.evidence_digest
    prepare = AsyncMock(side_effect=[
        (snapshot_a, None), (snapshot_b, None), (snapshot_b, None),
        (snapshot_b, None), (snapshot_b, None),
    ])
    finalize = AsyncMock(return_value=(True, None, None))

    async def fake_agent_loop(**kwargs):
        first = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "manifest"},
        ))
        assert first["evidence_digest"] == snapshot_a.evidence_digest
        read_a = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "artifact", "index": 0},
        ))
        assert read_a["content"] == "version A"
        await kwargs["on_round_end"](1, [], {}, ["inspect_task_delivery"])
        assessment_a = {
            "evidence_digest": snapshot_a.evidence_digest,
            "verdict": "accepted", "evidence": "Saved output was delivered.",
            "issues": [], "checks": [{
                "criterion": "Output delivered", "pointer": "/steps/0/result/text",
                "quote": "Saved output",
            }],
        }
        stale = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment_a},
        ))
        assert stale["error"] == "invalid_assessment"
        assert stale["details"]["evidence_digest"] == snapshot_b.evidence_digest
        current = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "manifest"},
        ))
        assert current["evidence_digest"] == snapshot_b.evidence_digest
        await kwargs["on_round_end"](2, [], {}, ["inspect_task_delivery"])
        assessment_b = {**assessment_a, "evidence_digest": snapshot_b.evidence_digest}
        premature = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment_b},
        ))
        assert premature["error"] == "inspection_required"
        assert "artifact:0:0" in premature["details"]["missing_pages"]
        read_b = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "artifact", "index": 0},
        ))
        assert read_b["evidence_digest"] == snapshot_b.evidence_digest
        assert read_b["content"] == "version B"
        await kwargs["on_round_end"](3, [], {}, ["inspect_task_delivery"])
        accepted = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment_b},
        ))
        assert accepted["status"] == "completed", accepted
        return SimpleNamespace(stop_reason="task_submitted", rounds=6, messages=[], usage={})

    task, _run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop, prepare=prepare, finalize=finalize,
    )
    result = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert result["status"] == "completed"
    assert prepare.await_count == 5
    finalize.assert_awaited_once()
    review.assert_not_awaited()
    replan.assert_not_awaited()
    delegate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at,error_code", [
    ("access", "artifact_unavailable"), ("text", "artifact_changed"),
])
async def test_cached_artifact_recheck_failure_never_exposes_cached_text(
    monkeypatch, failure_at: str, error_code: str,
) -> None:
    """An artifact page read must recheck access and bytes after manifest caching."""
    from packages.core.services.artifact_delivery import ArtifactDeliveryError

    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": [{
            "document_id": "doc-1", "fs_path": "result.txt",
            "text_available": True, "text_truncated": False,
            "text": "private bytes that must not be returned",
        }],
    })
    prepare = AsyncMock(return_value=(snapshot, None))
    failure = ArtifactDeliveryError(error_code, "Document access or content changed")
    verify_access = AsyncMock(side_effect=failure if failure_at == "access" else None)
    verify_text = AsyncMock(side_effect=failure if failure_at == "text" else None)
    finalize = AsyncMock(side_effect=AssertionError("recheck failure cannot finalize"))

    class ObservationComplete(Exception):
        pass

    async def fake_agent_loop(**kwargs):
        manifest = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "manifest"},
        ))
        assert manifest["status"] == "ok"
        rejected = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "artifact", "index": 0},
        ))
        assert rejected["status"] == "delivery_changed"
        assert "content" not in rejected
        assert "private bytes" not in json.dumps(rejected)
        raise ObservationComplete

    task, _run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop, prepare=prepare,
        finalize=finalize, verify_access=verify_access, verify_text=verify_text,
    )
    with pytest.raises(ObservationComplete):
        await worker.run_task_coordinator_once(
            event_id="event-1", task_id=task.id, wake_generation="wake-1",
        )
    assert prepare.await_count == 1
    verify_access.assert_awaited_once()
    assert verify_text.await_count == (1 if failure_at == "text" else 0)
    finalize.assert_not_awaited()
    review.assert_not_awaited()
    replan.assert_not_awaited()
    delegate.assert_not_awaited()


@pytest.mark.asyncio
async def test_more_than_eighty_inline_steps_accept_after_bounded_manifest_pages(monkeypatch) -> None:
    """A large DAG's tiny Step results need only manifest pages, not one call per Step."""
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [
            {"key": f"deliver-{index}", "result": {"text": f"done-{index}"}}
            for index in range(96)
        ],
        "artifacts": [],
    })
    required = acceptance.required_delivery_inspections(snapshot)
    assert "step:95:0" in required
    assert len([page for page in required if page.startswith("step:")]) == 96
    prepare = AsyncMock(return_value=(snapshot, None))
    finalize = AsyncMock(return_value=(True, None, None))

    async def fake_agent_loop(**kwargs):
        offset = 0
        seen = []
        calls = 0
        while True:
            page = json.loads(await kwargs["tool_executor"](
                "inspect_task_delivery", {"kind": "manifest", "offset": offset},
            ))
            calls += 1
            assert page["status"] == "ok"
            assert page["evidence_digest"] == snapshot.evidence_digest
            assert len(json.dumps(page, ensure_ascii=False)) <= 3_400
            seen.extend(entry["index"] for entry in page["catalog"])
            assert all(entry["result_complete"] for entry in page["catalog"])
            if page["next_offset"] is None:
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        assert seen == list(range(96))
        assert 1 < calls < 80
        await kwargs["on_round_end"](calls, [], {}, ["inspect_task_delivery"])
        assessment = {
            "evidence_digest": snapshot.evidence_digest,
            "verdict": "accepted", "evidence": "All 96 Step outputs were inspected.",
            "issues": [], "checks": [{
                "criterion": "First output delivered", "pointer": "/steps/0/result/text",
                "quote": "done-0",
            }],
        }
        accepted = json.loads(await kwargs["tool_executor"](
            "submit_task", {"assessment": assessment},
        ))
        assert accepted["status"] == "completed", accepted
        return SimpleNamespace(
            stop_reason="task_submitted", rounds=calls + 1, messages=[], usage={},
        )

    task, _run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop, prepare=prepare, finalize=finalize,
    )
    result = await worker.run_task_coordinator_once(
        event_id="event-1", task_id=task.id, wake_generation="wake-1",
    )
    assert result["status"] == "completed"
    assert prepare.await_count == 2
    finalize.assert_awaited_once()
    review.assert_not_awaited()
    replan.assert_not_awaited()
    delegate.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_manifest_and_step_pages_do_not_hash_all_artifacts(monkeypatch) -> None:
    """Every cached read rechecks access, but only an opened text page hashes bytes."""
    artifacts = [
        {
            "document_id": f"doc-{index}", "fs_path": f"file-{index}.txt",
            "text_available": True, "text_truncated": False,
            "text": f"artifact text {index}",
        }
        for index in range(32)
    ]
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{"key": "deliver", "result": {"text": "Saved output"}}],
        "artifacts": artifacts,
    })
    prepare = AsyncMock(return_value=(snapshot, None))
    verify_access = AsyncMock()
    verify_text = AsyncMock(return_value=SimpleNamespace(snapshots=[]))
    finalize = AsyncMock(side_effect=AssertionError("inspection-only probe cannot finalize"))

    class ObservationComplete(Exception):
        pass

    async def fake_agent_loop(**kwargs):
        offset = 0
        manifest_pages = 0
        while True:
            page = json.loads(await kwargs["tool_executor"](
                "inspect_task_delivery", {"kind": "manifest", "offset": offset},
            ))
            assert page["status"] == "ok"
            manifest_pages += 1
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert manifest_pages > 1
        for _ in range(2):
            step = json.loads(await kwargs["tool_executor"](
                "inspect_task_delivery", {"kind": "step", "index": 0},
            ))
            assert step["status"] == "ok"
            assert "Saved output" in step["content"]
        verify_text.assert_not_awaited()
        assert verify_access.await_count == manifest_pages - 1 + 2
        artifact = json.loads(await kwargs["tool_executor"](
            "inspect_task_delivery", {"kind": "artifact", "index": 17},
        ))
        assert artifact["status"] == "ok"
        assert artifact["content"] == "artifact text 17"
        assert verify_access.await_count == manifest_pages + 2
        verify_text.assert_awaited_once()
        assert verify_text.await_args.kwargs["refs"] == [{
            "type": "file", "document_id": "doc-17", "fs_path": "file-17.txt",
        }]
        assert verify_text.await_args.kwargs["expected_snapshots"] == [artifacts[17]]
        raise ObservationComplete

    task, _run, _plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=fake_agent_loop, prepare=prepare,
        finalize=finalize, verify_access=verify_access, verify_text=verify_text,
    )
    with pytest.raises(ObservationComplete):
        await worker.run_task_coordinator_once(
            event_id="event-1", task_id=task.id, wake_generation="wake-1",
        )
    prepare.assert_awaited_once()
    finalize.assert_not_awaited()
    review.assert_not_awaited()
    replan.assert_not_awaited()
    delegate.assert_not_awaited()
