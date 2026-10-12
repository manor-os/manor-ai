"""Task Agent tool rounds stop at the first blocking human gate."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.engine import ChatMessage
from packages.core.ai.runtime import task_agent


pytestmark = pytest.mark.unit


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _standard_hitl_payload() -> str:
    return json.dumps(
        {
            "message": "Approval is required before this action.",
            "approval_token": "hitl-task-agent-1",
            "error": "approval_required",
            "__hitl__": True,
            "hitl": {
                "id": "hitl-task-agent-1",
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
            "approvalId": "chrome-task-agent-1",
            "action_key": "chrome.action",
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": arguments,
            },
        }
    )


def _nested_skill_hitl_payload(blocking_result: str) -> str:
    return json.dumps(
        {
            "status": "hitl_required",
            "stop_reason": "hitl_required",
            "skill": "chrome",
            "control": {
                "kind": "approval_required",
                "tool_name": "mcp__chrome__click_element",
                "blocking_result": blocking_result,
            },
        }
    )


@pytest.mark.parametrize(
    "gate_kind",
    ("standard_hitl", "provider_approval", "nested_invoke_skill"),
)
@pytest.mark.asyncio
async def test_task_agent_stops_at_gate_before_later_sibling(
    monkeypatch,
    gate_kind: str,
) -> None:
    provider_arguments = {"tabId": 7, "ref": "button-1"}
    leaf_provider_result = _provider_approval_payload(provider_arguments)
    cases = {
        "standard_hitl": (
            "write_file",
            {"path": "result.md", "content": "hello"},
            _standard_hitl_payload(),
            "write_file",
            _standard_hitl_payload(),
        ),
        "provider_approval": (
            "mcp__chrome__click_element",
            provider_arguments,
            leaf_provider_result,
            "mcp__chrome__click_element",
            leaf_provider_result,
        ),
        "nested_invoke_skill": (
            "invoke_skill",
            {"skill": "chrome", "input": "Click the button"},
            _nested_skill_hitl_payload(leaf_provider_result),
            "mcp__chrome__click_element",
            leaf_provider_result,
        ),
    }
    (
        gate_name,
        gate_arguments,
        gate_result,
        expected_control_tool,
        expected_blocking_result,
    ) = cases[gate_kind]
    tool_calls = [
        {
            "id": "call-before",
            "name": "web_search",
            "arguments": {"query": "supporting evidence"},
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
    ]
    engine = SimpleNamespace(chat=AsyncMock(return_value=ChatMessage(
        role="assistant",
        content="",
        tool_calls=tool_calls,
        usage={"prompt_tokens": 8, "completion_tokens": 3},
    )))
    executed: list[tuple[str, dict]] = []

    async def execute_tool(name: str, arguments: dict, **_kwargs):
        executed.append((name, arguments))
        if name == gate_name:
            return gate_result
        return json.dumps({"ok": True})

    monkeypatch.setattr(task_agent, "runtime_execute_tool", execute_tool)
    messages = [ChatMessage(role="user", content="Complete all requested work")]

    result = await task_agent.runtime_execute_task_agent_turn(
        engine=engine,
        messages=messages,
        tools=[_tool("web_search"), _tool(gate_name), _tool("write_file")],
        loaded_tool_names={"web_search", gate_name, "write_file"},
        system_prompt="Task system",
        runtime_envelope=None,
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        conversation_id="conversation-1",
        task_id="task-1",
    )

    engine.chat.assert_awaited_once()
    assert [name for name, _arguments in executed] == ["web_search", gate_name]
    assert result.blocking_control == {
        "kind": "approval_required",
        "tool_name": expected_control_tool,
        "blocking_result": expected_blocking_result,
    }
    assert result.response_text == ""
    assert result.had_tool_calls is True
    assert result.tool_names == ["web_search", gate_name]
    assert result.blocking_tool_arguments == gate_arguments
    assert result.remaining_tool_calls == [tool_calls[2]]
    assert [item["tool_name"] for item in result.supervisor_evidence] == [
        "web_search",
        gate_name,
    ]
    assert [
        message.tool_call_id
        for message in result.messages
        if message.role == "tool"
    ] == ["call-before", "call-gate"]
    assert result.messages[1].tool_calls == tool_calls[:2]


@pytest.mark.asyncio
async def test_task_agent_sequence_gate_preserves_exact_child_arguments(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.control import runtime_owned_sequence_child_result
    from packages.core.ai.runtime.provider_approvals import normalize_provider_approval

    child_arguments = {
        "tabId": 7,
        "ref": "button-1",
        "snapshot_id": "snapshot-1",
    }
    child_result = _provider_approval_payload(child_arguments)
    sequence_arguments = {
        "steps": [
            {
                "id": "click",
                "tool": "click_element",
                "arguments": child_arguments,
            }
        ]
    }
    engine = SimpleNamespace(chat=AsyncMock(return_value=ChatMessage(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call-sequence",
                "name": "mcp__chrome__run_sequence",
                "arguments": sequence_arguments,
            }
        ],
        usage={},
    )))

    async def execute_tool(name: str, arguments: dict, **_kwargs):
        assert name == "mcp__chrome__run_sequence"
        assert arguments.pop("_performance_run_id_from_context") == "task-1"
        assert arguments.pop("_runtime_tool_call_id_from_context") == "call-sequence"
        assert arguments == sequence_arguments
        return runtime_owned_sequence_child_result(
            tool_name="mcp__chrome__click_element",
            arguments=child_arguments,
            result=child_result,
            public_result=child_result,
        )

    monkeypatch.setattr(task_agent, "runtime_execute_tool", execute_tool)

    result = await task_agent.runtime_execute_task_agent_turn(
        engine=engine,
        messages=[ChatMessage(role="user", content="Click the button")],
        tools=[_tool("mcp__chrome__run_sequence")],
        loaded_tool_names={"mcp__chrome__run_sequence"},
        system_prompt="Task system",
        runtime_envelope=None,
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        task_id="task-1",
    )

    assert result.blocking_control == {
        "kind": "approval_required",
        "tool_name": "mcp__chrome__click_element",
        "blocking_result": child_result,
    }
    assert result.blocking_tool_arguments == child_arguments
    assert "steps" not in result.blocking_tool_arguments
    approval = normalize_provider_approval(
        result.blocking_control["tool_name"],
        result.blocking_tool_arguments,
        result.blocking_control["blocking_result"],
    )
    assert approval is not None
    assert approval["retry_tool"] == "mcp__chrome__click_element"
    assert approval["retry_arguments"] == child_arguments
    assert "steps" not in approval["retry_arguments"]


@pytest.mark.asyncio
async def test_task_agent_stops_at_mutation_uncertainty_before_later_tools(
    monkeypatch,
) -> None:
    uncertainty = {
        "status": "error",
        "error": "mutation_timeout_uncertain",
        "content": "Chrome mutation outcome is unknown; observe and reconcile.",
        "stop_parent": True,
        "stop_reason": "uncertain_result",
        "terminal_failure": True,
        "retryable": False,
        "action_executed": "unknown",
        "do_not_replay": True,
        "recommended_next_action": "observe_and_reconcile",
    }
    tool_calls = [
        {
            "id": "call-sequence",
            "name": "mcp__chrome__run_sequence",
            "arguments": {
                "steps": [
                    {
                        "id": "click",
                        "tool": "click_element",
                        "arguments": {"ref": "e1", "snapshot_id": "s1"},
                    }
                ]
            },
        },
        {
            "id": "call-read",
            "name": "mcp__chrome__read_page",
            "arguments": {},
        },
        {
            "id": "call-later-mutation",
            "name": "mcp__chrome__click_element",
            "arguments": {"ref": "e2", "snapshot_id": "s1"},
        },
    ]
    engine = SimpleNamespace(chat=AsyncMock(return_value=ChatMessage(
        role="assistant",
        content="",
        tool_calls=tool_calls,
        usage={"prompt_tokens": 8, "completion_tokens": 3},
    )))
    executed: list[str] = []

    async def execute_tool(name: str, arguments: dict, **_kwargs):
        executed.append(name)
        if name == "mcp__chrome__run_sequence":
            return json.dumps(uncertainty)
        return json.dumps({"ok": True})

    monkeypatch.setattr(task_agent, "runtime_execute_tool", execute_tool)

    result = await task_agent.runtime_execute_task_agent_turn(
        engine=engine,
        messages=[ChatMessage(role="user", content="Click the button")],
        tools=[
            _tool("mcp__chrome__run_sequence"),
            _tool("mcp__chrome__read_page"),
            _tool("mcp__chrome__click_element"),
        ],
        loaded_tool_names={
            "mcp__chrome__run_sequence",
            "mcp__chrome__read_page",
            "mcp__chrome__click_element",
        },
        system_prompt="Task system",
        runtime_envelope=None,
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        task_id="task-1",
    )

    engine.chat.assert_awaited_once()
    assert executed == ["mcp__chrome__run_sequence"]
    assert result.terminal_control == uncertainty
    assert result.response_text == uncertainty["content"]
    assert result.had_tool_calls is True
    assert result.messages[1].tool_calls == tool_calls[:1]


@pytest.mark.asyncio
async def test_task_agent_executes_every_sibling_when_no_result_is_a_gate(
    monkeypatch,
) -> None:
    tool_calls = [
        {
            "id": "call-one",
            "name": "web_search",
            "arguments": {"query": "evidence"},
        },
        {
            "id": "call-two",
            "name": "read_file",
            "arguments": {"path": "brief.md"},
        },
        {
            "id": "call-three",
            "name": "write_file",
            "arguments": {"path": "result.md", "content": "done"},
        },
    ]
    engine = SimpleNamespace(chat=AsyncMock(return_value=ChatMessage(
        role="assistant",
        content="",
        tool_calls=tool_calls,
        usage={},
    )))
    executed: list[str] = []

    async def execute_tool(name: str, _arguments: dict, **_kwargs):
        executed.append(name)
        return json.dumps({"ok": True, "tool": name})

    monkeypatch.setattr(task_agent, "runtime_execute_tool", execute_tool)

    result = await task_agent.runtime_execute_task_agent_turn(
        engine=engine,
        messages=[ChatMessage(role="user", content="Complete the task")],
        tools=[_tool(call["name"]) for call in tool_calls],
        loaded_tool_names={call["name"] for call in tool_calls},
        system_prompt="Task system",
        runtime_envelope=None,
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
    )

    assert executed == ["web_search", "read_file", "write_file"]
    assert result.blocking_control is None
    assert result.tool_names == executed
    assert [
        message.tool_call_id
        for message in result.messages
        if message.role == "tool"
    ] == ["call-one", "call-two", "call-three"]


@pytest.mark.asyncio
async def test_task_agent_forced_provider_confirmation_retries_exact_action(
    monkeypatch,
) -> None:
    continuation = {
        "kind": "retry_with_result_token",
        "tool": "mcp__chrome__click_element",
        "arguments": {
            "tabId": 7,
            "ref": "button-1",
            "snapshot_id": "snapshot-1",
        },
        "required_status": "approved",
        "result_token_keys": ["approvalToken", "approval_token"],
        "argument_token_key": "approvalToken",
    }
    forced_tool_calls = [
        {
            "name": "mcp__chrome__confirm_action",
            "arguments": {
                "approvalId": "chrome-task-agent-1",
                "__manor_tool_continuation": continuation,
            },
            "disable_followup_tools": True,
        }
    ]
    engine = SimpleNamespace(chat=AsyncMock())
    executed: list[tuple[str, dict]] = []

    async def execute_tool(name: str, arguments: dict, **_kwargs):
        executed.append((name, dict(arguments)))
        if name == "mcp__chrome__confirm_action":
            return json.dumps(
                {
                    "ok": True,
                    "status": "approved",
                    "approvalToken": "provider-token-1",
                }
            )
        return json.dumps({"ok": True, "status": "clicked"})

    monkeypatch.setattr(task_agent, "runtime_execute_tool", execute_tool)
    tools = [
        _tool("mcp__chrome__confirm_action"),
        _tool("mcp__chrome__click_element"),
    ]

    result = await task_agent.runtime_execute_task_agent_turn(
        engine=engine,
        messages=[ChatMessage(role="user", content="Approve the pending action")],
        tools=tools,
        loaded_tool_names={
            "mcp__chrome__confirm_action",
            "mcp__chrome__click_element",
        },
        system_prompt="Task system",
        runtime_envelope=None,
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        forced_tool_calls=forced_tool_calls,
    )

    engine.chat.assert_not_awaited()
    assert executed == [
        (
            "mcp__chrome__confirm_action",
            {"approvalId": "chrome-task-agent-1"},
        ),
        (
            "mcp__chrome__click_element",
            {
                "tabId": 7,
                "ref": "button-1",
                "snapshot_id": "snapshot-1",
                "approvalToken": "provider-token-1",
            },
        ),
    ]
    assert result.blocking_control is None
    assert result.tool_names == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
    ]
    assert result.disable_followup_tools is True
    assert result.tools == []
    assert result.loaded_tool_names == set()
    assert [
        message.tool_call_id
        for message in result.messages
        if message.role == "tool"
    ] == ["scheduled-skill-1", "scheduled-continuation-1"]
    assistant_tool_call_phases = [
        message.tool_calls
        for message in result.messages
        if message.role == "assistant"
    ]
    assert assistant_tool_call_phases == [
        [
            {
                "id": "scheduled-skill-1",
                "name": "mcp__chrome__confirm_action",
                "arguments": {"approvalId": "chrome-task-agent-1"},
            }
        ],
        [
            {
                "id": "scheduled-continuation-1",
                "name": "mcp__chrome__click_element",
                "arguments": {
                    "tabId": 7,
                    "ref": "button-1",
                    "snapshot_id": "snapshot-1",
                },
            }
        ],
    ]
    assert "provider-token-1" not in " ".join(
        str(message.to_dict()) for message in result.messages
    )
    assert "provider-token-1" not in json.dumps(result.supervisor_evidence)
    assert (
        forced_tool_calls[0]["arguments"]["__manor_tool_continuation"]
        == continuation
    )


def test_agentic_loop_keeps_private_provider_continuation_aliases() -> None:
    from packages.core.ai.agentic_loop import (
        _pop_tool_continuation,
        _retry_call_after_tool_continuation,
    )
    from packages.core.ai.runtime.provider_approvals import (
        pop_tool_continuation,
        retry_call_after_tool_continuation,
    )

    assert _pop_tool_continuation is pop_tool_continuation
    assert (
        _retry_call_after_tool_continuation
        is retry_call_after_tool_continuation
    )
    assert _pop_tool_continuation(args={}) is None
