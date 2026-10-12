"""Shared-loop repair keeps diagnostics and tools, not automatic retries.

These tests use the real AgentLoop and deterministic model/tool adapters. They
do not require a database, provider, sandbox, or deployment configuration.
"""
from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop
import packages.core.ai.runtime.tool_guidance as guidance
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.tool_effect_intents import RuntimeToolEffectPersistenceError
from packages.core.workers.submit_result import SUBMIT_RESULT_TERMINAL_POLICY


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
RETIRED_FLAG = "MANOR_AGENT_TOOL_ERROR_SELF_REPAIR"


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _call(call_id, tool_name="video_edit", **arguments):
    return {"id": call_id, "name": tool_name, "arguments": arguments}


def _error(message="Sandbox connection lost; outcome is unknown."):
    return json.dumps({"status": "failed", "error": "runtime_unavailable", "message": message})


def _entry(entry, batch, *, disable_followup=False, stored_result=None):
    if entry == "live":
        return {}
    if entry == "forced":
        return {"forced_tool_calls": [
            {**call, "disable_followup_tools": disable_followup} for call in batch
        ]}
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "finish the artifact"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"]),
                }} for call in batch
            ]},
        ],
        usage={}, rounds=1, tool_calls_made=[], pending_tool_call=batch[0],
        remaining_tool_calls=batch[1:], disable_followup_tools=disable_followup,
    ).to_dict()
    # These calls resume a model round that already saw the current bundle.
    # Keep the full canonical guide, not merely its hash, in the checkpoint.
    guidance.RuntimeToolGuidance().prepare(
        checkpoint["messages"], {call["name"] for call in batch},
    )
    if stored_result is not None:
        checkpoint["pending_tool_result"] = {"id": batch[0]["id"], "content": stored_result}
    return {"resume_checkpoint": checkpoint}


@pytest.fixture
def completions(monkeypatch):
    # Exercise the real video guide independently of global registry order.
    video_guide = guidance._load_bundled_guidance("video-edit")
    monkeypatch.setattr(guidance, "runtime_tool_guidance", lambda name: video_guide if name == "video_edit" else None)
    summary = AsyncMock(return_value=("forced summary", {}))
    final = AsyncMock(return_value=("budget summary", {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", summary)
    monkeypatch.setattr(loop, "runtime_execute_agentic_final_completion", final)
    return summary, final


@pytest.mark.parametrize("entry", ["live", "forced", "resume", "stored_resume"])
async def test_detailed_error_reaches_model_then_edit_review_render_submit(monkeypatch, completions, entry):
    detail = json.dumps({
        "status": "failed", "error": "render_validation_failed",
        "message": "index.html:42 missing data-start; inspect and edit before rendering.",
        "details": {"path": "index.html", "line": 42, "code": "media_missing_data_start"},
    })
    batch = [_call("initial", operation="review"), _call("discarded", operation="render", marker="must-not-run")]
    operations, checkpoints = [], []
    issued = False
    repairs = [
        _call("edit", operation="write_files", path="index.html", content="<video data-start='0'>"),
        _call("review", operation="review"),
        _call("render", operation="render"),
        _call("submit", "submit_result", result={"artifact": "result.mp4"}),
    ]

    async def model(messages, tools, **_kwargs):
        nonlocal issued
        assert {tool["function"]["name"] for tool in tools} == {"video_edit", "submit_result"}
        if entry == "live" and not issued:
            issued = True
            return "", batch, {}
        transcript = json.dumps(messages)
        if entry == "forced" and not operations:
            # A fresh forced call cannot execute before guide observation.
            # The model reads that feedback before issuing the first review.
            assert "tool_guidance_required" in transcript
            assert guidance.runtime_tool_guidance("video_edit").instructions in messages[0]["content"]
            assert "must-not-run" not in transcript
            return "", [_call("review-after-guidance", operation="review")], {}
        assert "index.html:42" in transcript and "media_missing_data_start" in transcript
        assert "must-not-run" not in transcript
        assert "tool_error" in transcript
        assert repairs, "successful submit must terminate without another model call"
        return "", [repairs.pop(0)], {}

    async def execute(name, arguments):
        operation = arguments.get("operation", name)
        operations.append(operation)
        assert arguments.get("marker") != "must-not-run"
        if operation == "review" and operations == ["review"] and entry != "stored_resume":
            return detail
        return json.dumps({"status": "completed", "ok": True, "operation": operation})

    async def checkpoint(_rounds, messages):
        checkpoints.append(copy.deepcopy(messages))

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="finish the artifact",
        tools=[_tool("video_edit"), _tool("submit_result")], tool_executor=execute,
        terminal_tool_result_policy=SUBMIT_RESULT_TERMINAL_POLICY,
        max_rounds=8, on_round_end=checkpoint, on_round_end_strict=True,
        **_entry(entry, batch, disable_followup=True, stored_result=detail if entry == "stored_resume" else None),
    )
    assert result.stop_reason == "submit_result" and result.error is None
    assert operations == ([] if entry == "stored_resume" else ["review"]) + ["write_files", "review", "render", "submit_result"]
    assert not repairs
    assert checkpoints and "media_missing_data_start" in json.dumps(checkpoints)
    assert "must-not-run" not in json.dumps(result.messages)
    completions[0].assert_not_awaited()
    completions[1].assert_not_awaited()


async def test_more_than_ten_all_error_rounds_can_recover(monkeypatch, completions):
    executed = []

    async def model(messages, tools, **_kwargs):
        assert tools
        if executed:
            assert f"attempt {len(executed)}" in json.dumps(messages)
        if len(executed) == 13:
            return "recovered", [], {}
        return "", [_call(f"attempt-{len(executed)}", "web_fetch", url=f"https://example.com/{len(executed)}")], {}

    async def execute(_name, arguments):
        executed.append(arguments)
        if len(executed) <= 12:
            return _error(f"attempt {len(executed)}: remote temporarily unavailable")
        return json.dumps({"status": "completed", "text": "attempt 13 recovered"})

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(system_prompt="system", user_message="fetch", tools=[_tool("web_fetch")], tool_executor=execute, max_rounds=16)
    assert result.content == "recovered" and result.error is None
    assert len(executed) == 13
    completions[0].assert_not_awaited()
    completions[1].assert_not_awaited()


async def test_three_mcp_failures_interspersed_with_successes_can_recover(monkeypatch, completions):
    provider_tool = "mcp__email__search_messages"
    executed = []
    sequence = [provider_tool, "web_fetch", provider_tool, "web_fetch", provider_tool, "web_fetch", provider_tool]

    async def model(messages, tools, **_kwargs):
        assert {tool["function"]["name"] for tool in tools} == {provider_tool, "web_fetch"}
        if len(executed) == len(sequence):
            assert "recovered provider" in json.dumps(messages)
            return "done", [], {}
        return "", [_call(f"provider-{len(executed)}", sequence[len(executed)], query=str(len(executed)))], {}

    async def execute(name, _arguments):
        executed.append(name)
        if name == provider_tool and len(executed) < 7:
            return _error(f"provider failure {len(executed)}")
        return json.dumps({"status": "completed", "text": "recovered provider" if name == provider_tool else str(len(executed))})

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(system_prompt="system", user_message="find mail", tools=[_tool(name) for name in set(sequence)], tool_executor=execute, max_rounds=10)
    assert result.content == "done" and result.error is None
    assert executed == sequence
    completions[0].assert_not_awaited()


@pytest.mark.parametrize("entry", ["live", "forced", "resume"])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_nested_stop_parent_failure_is_an_observation_not_parent_stop(monkeypatch, completions, entry, uncertain):
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    batch = [_call("child", "invoke_skill", name="render-skill"), _call("child-suffix", operation="render", marker="must-not-run")]
    failure = runtime_format_invoke_skill_result("render-skill", {
        "content": "child detail index.html:42", "stop_parent": True,
        "stop_reason": "uncertain_result", "control": {"terminal": True},
    }) if uncertain else json.dumps({
        "status": "failed", "error": "child_render_failed", "message": "child detail index.html:42",
        "stop_parent": True, "terminal_failure": True,
        "stop_reason": "skill_failed", "control": {"kind": "tool_error", "terminal_failure": True},
    })
    if uncertain:
        payload = json.loads(failure)
        assert payload["status"] == "terminal" and "error" not in payload
        assert payload["terminal_failure"] is True and payload["action_executed"] == "unknown"
    executed, issued = [], False

    async def model(messages, tools, **_kwargs):
        nonlocal issued
        assert tools
        if entry == "live" and not issued:
            issued = True
            return "", batch, {}
        assert "child detail index.html:42" in json.dumps(messages)
        assert "must-not-run" not in json.dumps(messages)
        if len(executed) == 1:
            return "", [_call("inspect", operation="review")], {}
        return "parent repaired", [], {}

    async def execute(name, _arguments):
        executed.append(name)
        return failure if name == "invoke_skill" else '{"status":"completed","ok":true}'

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="finish", tools=[_tool("invoke_skill"), _tool("video_edit")],
        tool_executor=execute, max_rounds=6, **_entry(entry, batch, disable_followup=True),
    )
    assert result.content == "parent repaired" and result.error is None
    assert executed == ["invoke_skill", "video_edit"]
    completions[0].assert_not_awaited()


@pytest.mark.parametrize("leftover", [None, "0"])
async def test_self_repair_is_unconditional_and_ignores_the_retired_flag(monkeypatch, completions, leftover):
    """The deployment switch is gone: an old "0" left in an environment must not
    bring back the forced no-tools summary after an uncertain action."""

    if leftover is None:
        monkeypatch.delenv(RETIRED_FLAG, raising=False)
    else:
        monkeypatch.setenv(RETIRED_FLAG, leftover)
    execute = AsyncMock(return_value=_error())
    calls = 0

    async def model(messages, tools, **_kwargs):
        nonlocal calls
        calls += 1
        assert tools
        if calls == 1:
            return "", [_call("bad", operation="render"), _call("skipped", operation="write_files")], {}
        return "The render failed; nothing was delivered.", [], {}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(system_prompt="system", user_message="render", tools=[_tool("video_edit")], tool_executor=execute, max_rounds=4)
    assert result.stop_reason == "completed" and result.error is None
    # The failing serial call ran once; its dependent sibling was dropped, not replayed.
    assert execute.await_count == 1 and calls == 2
    completions[0].assert_not_awaited()


async def test_cancellation_still_stops_before_another_repair(monkeypatch, completions):
    cancelled = False

    async def execute(_name, _arguments):
        nonlocal cancelled
        cancelled = True
        return _error()

    model = AsyncMock(return_value=("", [_call("first", operation="render")], {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(system_prompt="system", user_message="render", tools=[_tool("video_edit")], tool_executor=execute, max_rounds=6, is_cancelled=lambda: cancelled)
    assert result.stop_reason == "cancelled"
    assert model.await_count == 1 and result.tool_calls_made == ["video_edit"]
    completions[0].assert_not_awaited()
    completions[1].assert_not_awaited()


async def test_hard_round_budget_still_stops_repair(monkeypatch, completions):
    execute = AsyncMock(return_value=_error())

    async def model(_messages, tools, **_kwargs):
        assert tools
        return "", [_call(f"budget-{execute.await_count}", operation="review")], {}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(system_prompt="system", user_message="render", tools=[_tool("video_edit")], tool_executor=execute, max_rounds=20, hard_max_rounds=3)
    assert result.stop_reason == "max_rounds" and execute.await_count == 3
    assert result.content == "budget summary"
    completions[0].assert_not_awaited()
    completions[1].assert_awaited_once()


@pytest.mark.parametrize("barrier", ["executor", "round_observer"])
async def test_strict_durability_failure_is_not_a_correctable_tool_error(monkeypatch, completions, barrier):
    failure = RuntimeToolEffectPersistenceError("durable receipt persistence failed")
    execute = AsyncMock(side_effect=failure) if barrier == "executor" else AsyncMock(return_value=_error())
    observer = AsyncMock(side_effect=failure) if barrier == "round_observer" else AsyncMock()
    model = AsyncMock(return_value=("", [_call("first", operation="render")], {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    with pytest.raises(RuntimeToolEffectPersistenceError, match="durable receipt"):
        await loop.agentic_loop(system_prompt="system", user_message="render", tools=[_tool("video_edit")], tool_executor=execute, max_rounds=5, on_round_end=observer, on_round_end_strict=True)
    assert model.await_count == 1 and execute.await_count == 1
    completions[0].assert_not_awaited()


@pytest.mark.parametrize("entry", ["forced", "resume"])
async def test_successful_approved_call_still_disables_followup_tools(monkeypatch, completions, entry):
    # This tests the successful-call scope boundary, not an unobserved-guide
    # rejection. A guided fresh forced call correctly fails before execution.
    name = "test_approved_write"
    execute = AsyncMock(return_value='{"status":"completed","ok":true}')
    model = AsyncMock(side_effect=AssertionError("followup tools must remain disabled after success"))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="write", tools=[_tool(name)], tool_executor=execute,
        max_rounds=4, **_entry(entry, [_call("approved", name, operation="write")], disable_followup=True),
    )
    assert result.content == "forced summary" and result.error is None
    execute.assert_awaited_once()
    model.assert_not_awaited()
    completions[0].assert_awaited_once()


async def test_each_repair_reenters_admission_and_provider_approval_gates(monkeypatch, completions):
    from packages.core.ai.runtime import tool_execution as execution
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    events, attempted = [], []

    async def prepare(**kwargs):
        attempt = kwargs["arguments"]["attempt"]
        events.append(("admission", attempt))
        assert kwargs["allowed_tool_names"] == {"video_edit"}
        return execution.RuntimePreparedToolExecution(
            arguments=kwargs["arguments"],
            blocked_result=RuntimePreflightValidationError(_error("authorization denied for this attempt")) if attempt == 2 else None,
        )

    async def provider_gate(**kwargs):
        events.append(("provider_approval", kwargs["arguments"]["attempt"]))

    async def settle_rejection(**kwargs):
        return kwargs["result"]

    async def handler(arguments):
        attempt = arguments["attempt"]
        events.append(("handler", attempt))
        return _error("inspect state before repair") if attempt == 1 else '{"status":"completed","ok":true}'

    async def execute(name, arguments):
        attempted.append(arguments["attempt"])
        return await execution.runtime_execute_scoped_dynamic_tool_handler(
            tool_name=name, arguments=arguments, handler=handler,
            allowed_tool_names={"video_edit"}, entity_id="entity", workspace_id="workspace",
        )

    async def model(messages, tools, **_kwargs):
        assert tools
        if attempted == [1, 2, 3]:
            assert "authorization denied" in json.dumps(messages)
            return "done", [], {}
        attempt = len(attempted) + 1
        return "", [_call(f"gated-{attempt}", operation="review", attempt=attempt)], {}

    monkeypatch.setattr(execution, "runtime_prepare_tool_execution", prepare)
    monkeypatch.setattr(execution, "_prepare_provider_approval_call", provider_gate)
    monkeypatch.setattr(execution, "runtime_settle_provider_approval_preflight_failure", settle_rejection)
    monkeypatch.setattr(execution, "runtime_settle_provider_approval_result", AsyncMock())
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(system_prompt="system", user_message="repair", tools=[_tool("video_edit")], tool_executor=execute, max_rounds=5)
    assert result.content == "done" and result.error is None
    assert events == [
        ("admission", 1), ("provider_approval", 1), ("handler", 1),
        ("admission", 2),
        ("admission", 3), ("provider_approval", 3), ("handler", 3),
    ]
    completions[0].assert_not_awaited()


@pytest.mark.parametrize("terminal_match", ["name", "status", "forced_media"])
async def test_real_child_uncertainty_cannot_be_terminal_success(monkeypatch, completions, terminal_match):
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    uncertainty = runtime_format_invoke_skill_result("render-skill", {
        "content": "Render outcome is unknown; inspect the existing job before retrying.",
        "stop_parent": True, "stop_reason": "uncertain_result", "control": {"terminal": True},
    })
    payload = json.loads(uncertainty)
    assert "error" not in payload and payload["status"] == "terminal"
    assert payload["terminal_failure"] is True
    name = "generate_file" if terminal_match == "forced_media" else "invoke_skill"
    call = _call("uncertain", name, **({"kind": "image", "prompt": "scene"} if name == "generate_file" else {"name": "render-skill"}))
    execute = AsyncMock(return_value=uncertainty)
    model_calls = 0

    async def model(messages, tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        assert tools
        if terminal_match != "forced_media" and model_calls == 1:
            return "", [call], {}
        assert "Render outcome is unknown" in json.dumps(messages)
        return "The existing job must be inspected; no successful render is claimed.", [], {}

    rule = {"tool_names": [name], "stop_reason": "must_not_claim_success", "notice": "UNSAFE SUCCESS"}
    if terminal_match == "status":
        rule["statuses"] = ["terminal"]
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="render", tools=[_tool(name)], tool_executor=execute,
        terminal_tool_result_policy=None if terminal_match == "forced_media" else {"terminal_tool_results": [rule]},
        max_rounds=4, **({"forced_tool_calls": [call]} if terminal_match == "forced_media" else {}),
    )
    execute.assert_awaited_once()
    assert result.stop_reason not in {"must_not_claim_success", loop.MEDIA_GENERATION_TERMINAL_STOP_REASON}
    assert "UNSAFE SUCCESS" not in result.content
    assert result.stop_reason == "completed" and result.error is None
    assert "no successful render is claimed" in result.content
    completions[0].assert_not_awaited()
    completions[1].assert_not_awaited()


@pytest.mark.parametrize("entry", ["live", "forced", "resume"])
@pytest.mark.parametrize("wait_kind", ["waiting_resource", "waiting_human", "approval"])
async def test_real_wait_and_approval_are_not_repairable_tool_errors(monkeypatch, completions, entry, wait_kind):
    from packages.core.ai.runtime.control import RuntimeToolSuspension

    batch = [_call("wait", "invoke_skill", name="render-skill"), _call("after-wait", operation="render")]
    if wait_kind == "approval":
        blocking_result = json.dumps({
            "error": "approval_required", "__hitl__": True, "approval_token": "approval-1",
            "hitl": {"id": "approval-1", "type": "approval", "prompt": "Approve this action?", "tool": "invoke_skill"},
        })
    else:
        blocking_result = RuntimeToolSuspension(
            kind=wait_kind, reservation_id="wait-1", poll_after_seconds=5,
            deadline_at="2099-01-01T00:00:00Z",
            reason="awaiting_human_input" if wait_kind == "waiting_human" else "sandbox_capacity",
            payload={"question": "Which source should be used?"} if wait_kind == "waiting_human" else None,
        )
    execute = AsyncMock(return_value=blocking_result)
    model = AsyncMock(return_value=("", batch, {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="render", tools=[_tool("invoke_skill"), _tool("video_edit")],
        tool_executor=execute, max_rounds=5, **_entry(entry, batch),
    )
    execute.assert_awaited_once()
    assert model.await_count == (1 if entry == "live" else 0)
    # The shared loop uses one waiting stop reason; control preserves the
    # concrete human/resource suspension kind for the runtime's scheduler.
    assert result.stop_reason == ("hitl_required" if wait_kind == "approval" else "waiting_resource")
    assert result.error is None
    if wait_kind == "approval":
        assert result.control["kind"] == "approval_required"
        assert result.control["blocking_result"] == blocking_result
        assert [{key: call[key] for key in ("name", "arguments")} for call in result.control["remaining_tool_calls"]] == [
            {"name": "video_edit", "arguments": {"operation": "render"}},
        ]
    else:
        assert result.control["kind"] == wait_kind
        checkpoint = result.control["checkpoint"]
        assert checkpoint["pending_tool_call"]["name"] == "invoke_skill"
        assert [call["name"] for call in checkpoint["remaining_tool_calls"]] == ["video_edit"]
        assert not any(message.get("role") == "tool" for message in result.messages)
    completions[0].assert_not_awaited()
    completions[1].assert_not_awaited()


@pytest.mark.parametrize("boundary", ["startup", "before_tool", "next_model"])
async def test_credit_exhaustion_still_stops_without_extra_tools(monkeypatch, completions, boundary):
    from packages.core.ai.llm_client import CreditExhaustedError

    failure = CreditExhaustedError("Experiment credit budget exhausted", plan="test", limit=1, current=1)
    execute = AsyncMock(return_value=_error())
    credit_checks = 0

    async def credit_check():
        nonlocal credit_checks
        credit_checks += 1
        if boundary == "startup" or (boundary == "before_tool" and credit_checks == 2):
            raise failure

    async def model(_messages, tools, **_kwargs):
        assert tools
        if execute.await_count:
            raise failure
        return "", [_call("credit-call", operation="review")], {}

    model_mock = AsyncMock(side_effect=model)
    monkeypatch.setattr(loop, "_preflight_credit_check", credit_check)
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model_mock)
    result = await loop.agentic_loop(system_prompt="system", user_message="render", tools=[_tool("video_edit")], tool_executor=execute, max_rounds=5)
    assert result.stop_reason == "credit_exhausted" and result.error == str(failure)
    assert execute.await_count == (1 if boundary == "next_model" else 0)
    assert model_mock.await_count == {"startup": 0, "before_tool": 1, "next_model": 2}[boundary]
    completions[0].assert_not_awaited()
    completions[1].assert_not_awaited()
