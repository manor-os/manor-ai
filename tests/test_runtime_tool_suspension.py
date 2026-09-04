from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeToolSuspension


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.mark.unit
async def test_suspension_is_checkpointed_without_becoming_a_model_tool_result(monkeypatch) -> None:
    async def fake_round(*_args, **_kwargs):
        return "", [
            {"id": "call-wait", "name": "invoke_skill", "arguments": {"skill": "code"}}
        ], {}

    seen_args: list[dict] = []

    async def tool_executor(_name: str, args: dict):
        seen_args.append(args)
        return RuntimeToolSuspension(
            kind="waiting_resource",
            reservation_id="reservation-wait",
            poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="run code",
        tools=[_tool("invoke_skill")],
        tool_executor=tool_executor,
        runtime_run_id="runtime-wait",
        max_rounds=3,
    )

    assert result.stop_reason == "waiting_resource"
    assert not any(message.get("role") == "tool" for message in result.messages)
    assert seen_args == [
        {
            "skill": "code",
            "_runtime_run_id_from_context": "runtime-wait",
            "_runtime_tool_call_id_from_context": "call-wait",
            "_runtime_tool_attempt_from_context": 1,
        }
    ]
    checkpoint = result.control["checkpoint"]
    assert checkpoint["pending_tool_call"]["id"] == "call-wait"
    assert checkpoint["pending_attempt"] == 1


@pytest.mark.unit
async def test_resume_executes_pending_tool_once_before_returning_to_the_model(monkeypatch) -> None:
    round_calls = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal round_calls
        round_calls += 1
        if round_calls == 1:
            return "", [
                {"id": "call-resume", "name": "invoke_skill", "arguments": {"skill": "code"}}
            ], {}
        return "done", None, {}

    first_attempts: list[int] = []

    async def suspending_executor(_name: str, args: dict):
        first_attempts.append(args["_runtime_tool_attempt_from_context"])
        return RuntimeToolSuspension(
            kind="waiting_resource",
            reservation_id="reservation-resume",
            poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    suspended = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="run code",
        tools=[_tool("invoke_skill")],
        tool_executor=suspending_executor,
        runtime_run_id="runtime-resume",
        max_rounds=3,
    )

    resumed_args: list[dict] = []

    async def resumed_executor(_name: str, args: dict):
        resumed_args.append(args)
        return "sandbox ready"

    resumed = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="run code",
        tools=[_tool("invoke_skill")],
        tool_executor=resumed_executor,
        runtime_run_id="runtime-resume",
        resume_checkpoint=suspended.control["checkpoint"],
        max_rounds=3,
    )

    assert first_attempts == [1]
    assert resumed_args == [
        {
            "skill": "code",
            "_runtime_run_id_from_context": "runtime-resume",
            "_runtime_tool_call_id_from_context": "call-resume",
            "_runtime_tool_attempt_from_context": 2,
        }
    ]
    assert resumed.stop_reason == "completed"
    assert resumed.content == "done"
    assert [message["content"] for message in resumed.messages if message.get("role") == "tool"] == [
        "sandbox ready"
    ]


@pytest.mark.unit
async def test_serial_suspension_does_not_execute_later_sibling_until_resume(monkeypatch) -> None:
    round_calls = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal round_calls
        round_calls += 1
        if round_calls == 1:
            return "", [
                {"id": "call-before", "name": "read_file", "arguments": {"path": "a"}},
                {"id": "call-skill", "name": "invoke_skill", "arguments": {"skill": "code"}},
                {"id": "call-after", "name": "write_file", "arguments": {"path": "b"}},
            ], {}
        return "finished", None, {}

    calls: list[str] = []

    async def first_executor(name: str, _args: dict):
        calls.append(name)
        if name == "invoke_skill":
            return RuntimeToolSuspension(
                kind="waiting_resource",
                reservation_id="reservation-serial",
                poll_after_seconds=5,
                deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        return f"{name} ok"

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    suspended = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="work",
        tools=[_tool("read_file"), _tool("invoke_skill"), _tool("write_file")],
        tool_executor=first_executor,
        runtime_run_id="runtime-serial",
        max_rounds=3,
    )
    assert calls == ["read_file", "invoke_skill"]
    assert [item["name"] for item in suspended.control["checkpoint"]["remaining_tool_calls"]] == [
        "write_file"
    ]

    resumed_calls: list[str] = []

    async def resumed_executor(name: str, _args: dict):
        resumed_calls.append(name)
        return f"{name} ok"

    resumed = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="work",
        tools=[_tool("read_file"), _tool("invoke_skill"), _tool("write_file")],
        tool_executor=resumed_executor,
        runtime_run_id="runtime-serial",
        resume_checkpoint=suspended.control["checkpoint"],
        max_rounds=3,
    )

    assert resumed_calls == ["invoke_skill", "write_file"]
    assert resumed.content == "finished"
    assert [message["tool_call_id"] for message in resumed.messages if message.get("role") == "tool"] == [
        "call-before",
        "call-skill",
        "call-after",
    ]
