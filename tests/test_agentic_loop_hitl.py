from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _hitl_payload(request_id: str = "hitl-1") -> str:
    # Marker order and indentation are deliberate: blocking detection must
    # parse the envelope, never prefix-match its serialization.
    return json.dumps(
        {
            "message": "Approval is required before this action.",
            "approval_token": request_id,
            "error": "approval_required",
            "__hitl__": True,
            "hitl": {
                "id": request_id,
                "type": "approval",
                "prompt": "Approve this write?",
                "tool": "write_file",
            },
        },
        indent=2,
    )


def _provider_approval_payload(arguments: dict) -> str:
    return json.dumps(
        {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "chrome-approval-1",
            "action_key": "chrome.action",
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": arguments,
            },
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("gate_name", "gate_arguments"),
    [
        ("write_file", {"path": "result.md", "content": "hello"}),
        ("future_vendor_mutation", {"target": "external-state"}),
    ],
)
async def test_model_tool_round_stops_at_first_hitl_before_later_sibling(
    monkeypatch,
    gate_name: str,
    gate_arguments: dict,
) -> None:
    llm_calls = 0
    tool_calls: list[str] = []
    blocking_result = _hitl_payload()

    async def fake_round(*_args, **_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return (
            "",
            [
                {
                    "id": "call-before",
                    "name": "web_search",
                    "arguments": {"query": "evidence"},
                },
                {
                    "id": "call-gate",
                    "name": gate_name,
                    "arguments": gate_arguments,
                },
                {
                    "id": "call-after",
                    "name": "write_file",
                    "arguments": {"path": "later.md", "content": "must not run"},
                },
            ],
            {},
        )

    async def tool_executor(name: str, _arguments: dict):
        tool_calls.append(name)
        if name == gate_name:
            return blocking_result
        return json.dumps({"ok": True})

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_round,
    )

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="do the work",
        tools=[_tool("web_search"), _tool(gate_name), _tool("write_file")],
        tool_executor=tool_executor,
        max_rounds=3,
    )

    assert llm_calls == 1
    assert tool_calls == ["web_search", gate_name]
    assert result.stop_reason == "hitl_required"
    assert result.content == ""
    assert result.error is None
    assert result.control == {
        "kind": "approval_required",
        "tool_name": gate_name,
        "blocking_result": blocking_result,
        "tool_call_id": "call-gate",
        "remaining_tool_calls": [{
            "id": "call-after",
            "name": "write_file",
            "arguments": {"path": "later.md", "content": "must not run"},
        }],
    }
    assert result.tool_calls_made == ["web_search", gate_name]
    assert [
        message["tool_call_id"]
        for message in result.messages
        if message.get("role") == "tool"
    ] == ["call-before", "call-gate"]
    json.dumps(result.control)


@pytest.mark.unit
async def test_provider_native_approval_stops_before_second_model_call(monkeypatch) -> None:
    arguments = {"tabId": 7, "ref": "e1"}
    blocking_result = _provider_approval_payload(arguments)
    llm_calls = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "", [
            {
                "id": "call-provider-gate",
                "name": "mcp__chrome__click_element",
                "arguments": arguments,
            }
        ], {}

    executor = AsyncMock(return_value=blocking_result)
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_round,
    )

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="publish it",
        tools=[_tool("mcp__chrome__click_element")],
        tool_executor=executor,
        max_rounds=3,
    )

    assert llm_calls == 1
    executor.assert_awaited_once()
    assert result.stop_reason == "hitl_required"
    assert result.control == {
        "kind": "approval_required",
        "tool_name": "mcp__chrome__click_element",
        "blocking_result": blocking_result,
        "tool_call_id": "call-provider-gate",
        "remaining_tool_calls": [],
    }


@pytest.mark.unit
async def test_sequence_provider_gate_keeps_private_owner_out_of_model_and_chat(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime import chrome_sequence
    from packages.core.ai.runtime.provider_approvals import ProviderApprovalCollector
    from packages.core.ai.runtime.streams import runtime_tool_result_for_chat

    private_input = "private-child-input-" + ("x" * 7_000)
    child_arguments = {
        "tabId": 7,
        "target": {"role": "textbox", "name": "Draft", "exact": True},
        "value": private_input,
    }
    sequence_arguments = {
        "steps": [{
            "id": "fill-draft",
            "tool": "fill_or_select",
            "arguments": child_arguments,
        }],
    }
    raw_provider_results: list[str] = []

    async def execute_child(name: str, args: dict, **_kwargs):
        assert name == "mcp__chrome__fill_or_select"
        raw = json.dumps({
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "sequence-private-approval",
            "data_summary": "Approve editing the draft",
            "provider_debug": "d" * 20_000,
            "retry_action": {
                "name": name,
                "arguments": args,
            },
        })
        raw_provider_results.append(raw)
        return raw

    monkeypatch.setattr(chrome_sequence, "runtime_execute_tool", execute_child)
    llm_calls = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "", [{
            "id": "sequence-call-1",
            "name": "mcp__chrome__run_sequence",
            "arguments": sequence_arguments,
        }], {}

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_round,
    )
    returned_results: list[object] = []

    async def tool_executor(name: str, args: dict):
        assert name == "mcp__chrome__run_sequence"
        result = await chrome_sequence.runtime_chrome_run_sequence_handler(**args)
        returned_results.append(result)
        return result

    collector = ProviderApprovalCollector()
    chat_previews: list[str] = []

    def on_tool_end(name: str, result: str, *_args) -> None:
        collector.capture(name, sequence_arguments, result)
        chat_previews.append(runtime_tool_result_for_chat(name, result))

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="publish it",
        tools=[_tool("mcp__chrome__run_sequence")],
        tool_executor=tool_executor,
        on_tool_end=on_tool_end,
        runtime_run_id="runtime-sequence-private",
        max_rounds=3,
    )

    assert llm_calls == 1
    assert result.stop_reason == "hitl_required"
    assert result.control["tool_name"] == "mcp__chrome__fill_or_select"
    request = collector.pending_request()
    assert request is not None
    assert request["retry_arguments"] == child_arguments
    assert len(raw_provider_results[0]) > chrome_sequence.CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS

    tool_message = next(
        message for message in result.messages if message.get("role") == "tool"
    )["content"]
    for public_result in (
        str(returned_results[0]),
        str(result.control["blocking_result"]),
        tool_message,
        chat_previews[0],
    ):
        assert len(public_result) <= chrome_sequence.CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS
        json.loads(public_result)
        assert "__manor_runtime_child_owner" not in public_result
        assert "retry_action" not in public_result
        assert private_input not in public_result
        assert "_runtime_" not in public_result


@pytest.mark.unit
async def test_forced_tools_stop_at_hitl_without_calling_model_or_later_tool(
    monkeypatch,
) -> None:
    blocking_result = _hitl_payload("hitl-forced")
    executed: list[str] = []

    async def tool_executor(name: str, _arguments: dict):
        executed.append(name)
        if name == "write_file":
            return blocking_result
        return "ok"

    model_round = AsyncMock()
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        model_round,
    )

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="write both files",
        tools=[_tool("write_file")],
        tool_executor=tool_executor,
        forced_tool_calls=[
            {
                "name": "write_file",
                "arguments": {"path": "first.md", "content": "first"},
            },
            {
                "name": "write_file",
                "arguments": {"path": "second.md", "content": "second"},
            },
        ],
        max_rounds=3,
    )

    assert executed == ["write_file"]
    model_round.assert_not_awaited()
    assert result.stop_reason == "hitl_required"
    assert result.control["blocking_result"] == blocking_result
    assert result.tool_calls_made == ["write_file"]
    assert len([m for m in result.messages if m.get("role") == "tool"]) == 1


@pytest.mark.unit
async def test_checkpoint_resume_stops_at_hitl_without_running_remaining_calls(
    monkeypatch,
) -> None:
    blocking_result = _hitl_payload("hitl-resumed")
    pending_call = {
        "id": "call-resumed-gate",
        "name": "write_file",
        "arguments": {"path": "first.md", "content": "first"},
    }
    remaining_call = {
        "id": "call-after-gate",
        "name": "write_file",
        "arguments": {"path": "second.md", "content": "second"},
    }
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "write both files"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"]),
                        },
                    }
                    for call in (pending_call, remaining_call)
                ],
            },
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call=pending_call,
        remaining_tool_calls=[remaining_call],
    )
    executed: list[str] = []

    async def tool_executor(name: str, _arguments: dict):
        executed.append(name)
        return blocking_result

    model_round = AsyncMock()
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        model_round,
    )

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="write both files",
        tools=[_tool("write_file")],
        tool_executor=tool_executor,
        resume_checkpoint=checkpoint.to_dict(),
        max_rounds=3,
    )

    assert executed == ["write_file"]
    model_round.assert_not_awaited()
    assert result.stop_reason == "hitl_required"
    assert result.tool_calls_made == ["write_file"]
    assert [
        message["tool_call_id"]
        for message in result.messages
        if message.get("role") == "tool"
    ] == ["call-resumed-gate"]


@pytest.mark.unit
def test_nested_invoke_skill_gate_preserves_leaf_control() -> None:
    leaf_result = _provider_approval_payload({"tabId": 7, "ref": "e1"})
    wrapper = json.dumps(
        {
            "status": "hitl_required",
            "stop_reason": "hitl_required",
            "skill": "chrome",
            "control": {
                "kind": "approval_required",
                "tool_name": "mcp__chrome__click_element",
                "blocking_result": leaf_result,
            },
        }
    )

    control = loop_module.detect_blocking_human_gate(
        "invoke_skill",
        {"skill": "chrome"},
        wrapper,
    )

    assert control == {
        "kind": "approval_required",
        "tool_name": "mcp__chrome__click_element",
        "blocking_result": leaf_result,
    }
    json.dumps(control)


@pytest.mark.unit
def test_serialization_is_fail_closed_but_keeps_reads_parallel_eligible() -> None:
    assert loop_module._requires_serial_tool_execution("web_search", {"query": "x"}) is False
    assert loop_module._requires_serial_tool_execution(
        "write_file", {"path": "x", "content": "y"}
    ) is True
    assert loop_module._requires_serial_tool_execution("future_vendor_mutation", {}) is True
