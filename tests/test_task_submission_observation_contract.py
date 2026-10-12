"""Precommit delivery observations stay in the Task's real serial AgentLoop."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.services import task_acceptance_review as acceptance
from packages.core.workers import task_coordinator as worker
from test_task_coordinator_worker import _install_delivery_inspection_fakes


class ObservationComplete(Exception):
    pass


def _snapshot():
    return acceptance.acceptance_snapshot({
        "task": {"title": "Verify output"}, "plan_id": "plan-1",
        "steps": [{
            "key": "deliver", "result": {"text": "Saved output"},
            "delivery_contract": {
                "declaration": {
                    "basis": "unique_required_schema", "pointers": ["/text"],
                    "requires_semantic_assessment": True,
                },
                "semantic_acceptance": "not_established_by_file_identity",
            },
        }],
        "artifacts": [],
    })


def _inspection_calls(snapshot):
    calls = []
    for position, page in enumerate(acceptance.required_delivery_inspections(snapshot)):
        parts = page.split(":")
        arguments = {"kind": parts[0], "offset": int(parts[-1])}
        if len(parts) == 3:
            arguments["index"] = int(parts[1])
        calls.append({"id": f"inspect-{position}", "name": "inspect_task_delivery", "arguments": arguments})
    return calls


async def _real_loop(**kwargs):
    keys = (
        "system_prompt", "user_message", "tools", "tool_executor", "max_rounds",
        "on_round_end", "on_round_end_strict", "on_llm_call_before",
        "runtime_cancel_checker", "terminal_tool_result_policy", "tool_schema_resolver",
        "final_response_contract",
    )
    return await loop_module.agentic_loop(**{key: kwargs[key] for key in keys})


def _assert_no_commit_contract(payload, *, code, cause):
    assert payload["contract"] == "tool_error"
    assert payload["status"] == "error" and payload["tool"] == "submit_task"
    assert payload["error"] == code
    assert payload["details"]["failure_scope"] == "task_submission"
    assert payload["details"]["action_executed"] is False
    assert payload["details"]["committed"] is False
    assert payload["details"]["cause"] == cause


@pytest.mark.asyncio
async def test_unreviewable_required_page_keeps_typed_observation_and_read_tools(monkeypatch):
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Verify output", "description": "x" * 1100},
        "plan_id": "plan-1",
        "steps": [{
            "key": "s" * 120, "kind": "agent", "status": "done",
            "delivery_contract": {"declaration": {"basis": "unique_required_schema"}},
            "result": {"text": "y" * 1480},
        }],
        "artifacts": [],
    })
    cause = "A delivery manifest entry exceeds the safe inspection page size."
    with pytest.raises(acceptance.TaskAcceptanceError, match="safe inspection page"):
        acceptance.required_delivery_inspection_requests(snapshot)
    assessment = {
        "evidence_digest": snapshot.evidence_digest, "verdict": "accepted",
        "evidence": "Saved output is present.", "issues": [],
        "checks": [{"criterion": "Output delivered", "pointer": "/steps/0/result/text", "quote": "y" * 20}],
    }
    rounds = 0

    async def model(messages, tools, **_kwargs):
        nonlocal rounds
        rounds += 1
        assert tools
        observed = {item["tool_call_id"]: json.loads(item["content"]) for item in messages if item.get("role") == "tool"}
        if rounds == 1:
            return "", [{"id": "submit", "name": "submit_task", "arguments": {"assessment": assessment}}], {}
        if rounds == 2:
            _assert_no_commit_contract(observed["submit"], code="acceptance_unverifiable", cause=cause)
            return "", [{"id": "read-step", "name": "inspect_task_delivery", "arguments": {"kind": "step", "index": 0}}], {}
        assert rounds == 3 and observed["read-step"]["status"] == "ok"
        raise ObservationComplete

    finalize = AsyncMock(side_effect=AssertionError("unreviewable evidence cannot finalize"))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, run, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=_real_loop, prepare=AsyncMock(return_value=(snapshot, None)), finalize=finalize,
    )
    with pytest.raises(ObservationComplete):
        await worker.run_task_coordinator_once(event_id="event-1", task_id=task.id, wake_generation="wake-1")
    assert task.status == "in_progress" and plan.status == "running" and run.phase == "running"
    finalize.assert_not_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["revoked", "missing", "gate", "unknown_effect_gate"])
async def test_final_recheck_error_keeps_inspection_in_same_loop(monkeypatch, outcome):
    """No error-code whitelist or deployment self-repair flag is needed."""
    snapshot = _snapshot()
    cause = "The submitted artifact is not currently readable in this execution."
    code = "artifact_unavailable"
    if outcome == "missing":
        code, cause = "artifact_missing", "The delivered file has no stored content."
    if outcome in {"gate", "unknown_effect_gate"}:
        code = "completion_gate_failed"
        cause = "An external effect remains unreconciled." if outcome == "unknown_effect_gate" else "Required delivery is missing."
        verdict = "needs_human" if outcome == "unknown_effect_gate" else "needs_replan"
        finalize = AsyncMock(return_value=(False, SimpleNamespace(
            evidence=cause, verdict=SimpleNamespace(value=verdict),
        ), None))
    else:
        finalize = AsyncMock(side_effect=acceptance.TaskAcceptanceError(
            "Delivery could not be verified.", code=code,
            details={"cause": cause, "document_id": "doc-1"},
        ))
    prepare = AsyncMock(return_value=(snapshot, None))
    assessment = {
        "evidence_digest": snapshot.evidence_digest, "verdict": "accepted",
        "evidence": "Saved output is present.", "issues": [],
        "checks": [{"criterion": "Output delivered", "pointer": "/steps/0/result/text", "quote": "Saved output"}],
    }
    rounds = 0

    async def model(messages, tools, **_kwargs):
        nonlocal rounds
        rounds += 1
        assert tools, "The rolled-back delivery observation removed all tools."
        observed = {item["tool_call_id"]: json.loads(item["content"]) for item in messages if item.get("role") == "tool"}
        assert "must-not-replay" not in observed
        if rounds == 1:
            return "", _inspection_calls(snapshot), {}
        if rounds == 2:
            return "", [
                {"id": "submit", "name": "submit_task", "arguments": {"assessment": assessment}},
                {"id": "must-not-replay", "name": "delegate_ready_steps", "arguments": {}},
            ], {}
        if rounds == 3:
            _assert_no_commit_contract(observed["submit"], code=code, cause=cause)
            if outcome in {"gate", "unknown_effect_gate"}:
                assert observed["submit"]["details"]["verdict"] == verdict
            else:
                assert observed["submit"]["details"]["document_id"] == "doc-1"
            return "", [{"id": "fresh-inspection", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"}}], {}
        assert rounds == 4 and observed["fresh-inspection"]["status"] == "ok"
        raise ObservationComplete

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, run, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=_real_loop, prepare=prepare, finalize=finalize,
    )
    with pytest.raises(ObservationComplete):
        await worker.run_task_coordinator_once(event_id="event-1", task_id=task.id, wake_generation="wake-1")
    assert rounds == 4
    finalize.assert_awaited_once()
    assert prepare.await_count == 3  # Initial read, pre-submit read, post-error fresh read.
    assert task.status == "in_progress" and plan.status == "running" and run.phase == "running"
    worker.PlanExecutor._emit_task_event.assert_not_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["artifact_unavailable", "artifact_missing", "artifact_reference_limit"])
async def test_preparatory_submission_failure_keeps_tools_without_replaying(monkeypatch, code):
    snapshot = _snapshot()
    cause = "The current delivery cannot be verified."
    prepare = AsyncMock(side_effect=[acceptance.TaskAcceptanceError(cause, code=code), (snapshot, None)])
    finalize = AsyncMock(side_effect=AssertionError("Unverified delivery cannot finalize"))
    rounds = 0

    async def model(messages, tools, **_kwargs):
        nonlocal rounds
        rounds += 1
        assert tools
        observed = {item["tool_call_id"]: json.loads(item["content"]) for item in messages if item.get("role") == "tool"}
        assert "must-not-replay" not in observed
        if rounds == 1:
            return "", [
                {"id": "submit", "name": "submit_task", "arguments": {}},
                {"id": "must-not-replay", "name": "delegate_ready_steps", "arguments": {}},
            ], {}
        if rounds == 2:
            _assert_no_commit_contract(observed["submit"], code=code, cause=cause)
            return "", [{"id": "fresh-inspection", "name": "inspect_task_delivery", "arguments": {"kind": "manifest"}}], {}
        assert rounds == 3 and observed["fresh-inspection"]["status"] == "ok"
        raise ObservationComplete

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    task, _, plan, review, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=_real_loop, prepare=prepare, finalize=finalize,
    )
    with pytest.raises(ObservationComplete):
        await worker.run_task_coordinator_once(event_id="event-1", task_id=task.id, wake_generation="wake-1")
    assert task.status == "in_progress" and plan.status == "running"
    assert prepare.await_count == 2
    finalize.assert_not_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsettled_effect_gate_is_not_a_no_effect_receipt(monkeypatch):
    gate = SimpleNamespace(evidence="Step has an unreconciled external effect.", verdict=SimpleNamespace(value="needs_human"))
    prepare = AsyncMock(return_value=(None, gate))
    finalize = AsyncMock(side_effect=AssertionError("Unsettled effects cannot finalize"))

    async def observe(**kwargs):
        result = await kwargs["tool_executor"]("submit_task", {})
        assert not isinstance(result, RuntimePreflightValidationError)
        assert json.loads(result) == {"status": "blocked", "reason": gate.evidence, "verdict": "needs_human"}
        raise ObservationComplete

    task, _, _, _, replan, delegate = _install_delivery_inspection_fakes(
        monkeypatch, agent_loop=observe, prepare=prepare, finalize=finalize,
    )
    with pytest.raises(ObservationComplete):
        await worker.run_task_coordinator_once(event_id="event-1", task_id=task.id, wake_generation="wake-1")
    finalize.assert_not_awaited()
    delegate.assert_not_awaited()
    replan.assert_not_awaited()
