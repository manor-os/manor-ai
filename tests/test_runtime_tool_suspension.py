from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint, RuntimeToolSuspension


@pytest.mark.parametrize("schema_version", [1, 2])
def test_legacy_runtime_checkpoint_remains_resume_compatible(schema_version: int) -> None:
    checkpoint = RuntimeAgentCheckpoint.from_dict({
        "schema_version": schema_version,
        "messages": [],
        "usage": {},
        "rounds": 1,
        "tool_calls_made": [],
        "pending_tool_call": {"id": "call-1", "name": "workspace_search"},
        "remaining_tool_calls": [],
        "pending_attempt": 1,
        "seen_tool_result_digests": {},
        # Retired provider-breaker counter written by older checkpoints.
        "mcp_provider_error_rounds": {"email": 2},
    })

    assert checkpoint.disable_followup_tools is False
    assert checkpoint.tool_continuations == {}
    assert checkpoint.pending_tool_call == {"id": "call-1", "name": "workspace_search"}
    assert "mcp_provider_error_rounds" not in checkpoint.to_dict()


def test_suspension_checkpoint_owner_is_internal_only() -> None:
    suspension = RuntimeToolSuspension(
        kind="waiting_resource",
        reservation_id="reservation-owner",
        poll_after_seconds=5,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    owned = suspension.with_checkpoint_tool_call(
        tool_call_id="child-call-1",
        tool_name="mcp__chrome__wait",
        arguments={"text": "Ready"},
    )

    assert owned.checkpoint_tool_call == {
        "id": "child-call-1",
        "name": "mcp__chrome__wait",
        "arguments": {"text": "Ready"},
    }
    assert "checkpoint_tool_call" not in owned.to_dict()
    with pytest.raises(TypeError):
        RuntimeToolSuspension(
            kind="waiting_resource",
            reservation_id="forged-owner",
            poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc),
            checkpoint_tool_call={"id": "forged", "name": "write_file"},
        )
    with pytest.raises(ValueError, match="checkpoint arguments are too large"):
        suspension.with_checkpoint_tool_call(
            tool_call_id="oversized-child",
            tool_name="mcp__chrome__wait",
            arguments={"text": "x" * 70_000},
        )


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
@pytest.mark.parametrize("checkpoint_fails", [False, True])
async def test_resumed_tool_result_is_checkpointed_before_next_model_call(
    monkeypatch,
    checkpoint_fails: bool,
) -> None:
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "child-call",
                    "type": "function",
                    "function": {"name": "invoke_skill", "arguments": "{}"},
                }],
            },
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": "child-call", "name": "invoke_skill", "arguments": {}},
    ).to_dict()
    events: list[str] = []

    async def tool_executor(_name: str, _args: dict):
        events.append("tool")
        return "durable child receipt"

    async def persist_round(
        _rounds: int,
        messages: list[dict],
        _usage: dict,
        _tool_calls: list[str],
    ) -> None:
        assert any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "child-call"
            and message.get("content") == "durable child receipt"
            for message in messages
        )
        events.append("checkpoint")
        if checkpoint_fails:
            raise RuntimeError("checkpoint unavailable")

    async def fake_round(*_args, **_kwargs):
        events.append("model")
        return "done", None, {}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    run = loop_module.agentic_loop(
        system_prompt="system",
        user_message="run",
        tools=[_tool("invoke_skill")],
        tool_executor=tool_executor,
        resume_checkpoint=checkpoint,
        on_round_end=persist_round,
        on_round_end_strict=True,
        max_rounds=2,
    )
    if checkpoint_fails:
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await run
        assert events == ["tool", "checkpoint"]
    else:
        result = await run
        assert result.stop_reason == "completed"
        assert events[:3] == ["tool", "checkpoint", "model"]


@pytest.mark.unit
async def test_sequence_suspension_resumes_child_without_replaying_prior_steps(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime import chrome_sequence

    sequence_args = {
        "steps": [
            {
                "id": "open",
                "tool": "open_or_reuse",
                "arguments": {"url": "https://example.com"},
            },
            {
                "id": "wait",
                "tool": "wait",
                "arguments": {"text": "Ready"},
            },
        ],
    }
    round_calls = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal round_calls
        round_calls += 1
        if round_calls == 1:
            return "", [{
                "id": "sequence-call-1",
                "name": "mcp__chrome__run_sequence",
                "arguments": sequence_args,
            }], {}
        return "done", None, {}

    mutation_count = 0
    wait_attempts: list[dict] = []

    async def execute_child(name: str, args: dict, **_kwargs):
        nonlocal mutation_count
        if name == "mcp__chrome__open_or_reuse":
            mutation_count += 1
            return json.dumps({"ok": True, "status": "opened", "tabId": 7})
        assert name == "mcp__chrome__wait"
        wait_attempts.append(dict(args))
        if len(wait_attempts) == 1:
            return RuntimeToolSuspension(
                kind="waiting_resource",
                reservation_id="sequence-reservation",
                poll_after_seconds=5,
                deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        return json.dumps({"ok": True, "status": "matched"})

    monkeypatch.setattr(chrome_sequence, "runtime_execute_tool", execute_child)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)

    async def tool_executor(name: str, args: dict):
        if name == "mcp__chrome__run_sequence":
            return await chrome_sequence.runtime_chrome_run_sequence_handler(**args)
        return await execute_child(name, args)

    suspended = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="open and wait",
        tools=[_tool("mcp__chrome__run_sequence"), _tool("mcp__chrome__wait")],
        tool_executor=tool_executor,
        runtime_run_id="runtime-sequence",
        max_rounds=3,
    )

    checkpoint = suspended.control["checkpoint"]
    pending = checkpoint["pending_tool_call"]
    assert pending["name"] == "mcp__chrome__wait"
    assert pending["arguments"]["text"] == "Ready"
    assert pending["id"].startswith("sequence-call-1:sequence:1:")
    assistant_call = checkpoint["messages"][-1]["tool_calls"][0]
    assert assistant_call["id"] == pending["id"]
    assert assistant_call["function"]["name"] == pending["name"]
    assert json.loads(assistant_call["function"]["arguments"]) == {"text": "Ready"}

    resumed = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="open and wait",
        tools=[_tool("mcp__chrome__run_sequence"), _tool("mcp__chrome__wait")],
        tool_executor=tool_executor,
        runtime_run_id="runtime-sequence",
        resume_checkpoint=checkpoint,
        max_rounds=3,
    )

    assert mutation_count == 1
    assert len(wait_attempts) == 2
    assert wait_attempts[1]["_runtime_tool_call_id_from_context"] == pending["id"]
    assert wait_attempts[1]["_runtime_tool_attempt_from_context"] == 2
    assert resumed.stop_reason == "completed"
    assert resumed.content == "done"


@pytest.mark.unit
async def test_provider_errors_across_checkpoint_resume_stay_repairable(
    monkeypatch,
) -> None:
    round_calls = 0

    async def failing_round(*_args, **_kwargs):
        nonlocal round_calls
        round_calls += 1
        return "", [
            {
                "id": f"email-{round_calls}",
                "name": "mcp__email__list_folders",
                "arguments": {},
            }
        ], {}

    attempts = 0

    async def suspending_after_two_failures(_name: str, _args: dict):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            return RuntimeToolSuspension(
                kind="waiting_resource",
                reservation_id="email-reservation",
                poll_after_seconds=5,
                deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        return json.dumps({"status": "failed", "error": "email unavailable"})

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        failing_round,
    )
    suspended = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="list folders",
        tools=[_tool("mcp__email__list_folders")],
        tool_executor=suspending_after_two_failures,
        max_rounds=5,
    )

    # Two provider errors stay repairable: the third call still runs and parks.
    assert suspended.stop_reason == "waiting_resource"
    checkpoint = suspended.control["checkpoint"]
    assert checkpoint["schema_version"] == 4
    assert checkpoint["pending_tool_call"]["id"] == "email-3"
    assert "mcp_provider_error_rounds" not in checkpoint

    resumed_executor = AsyncMock(
        return_value=json.dumps({"status": "failed", "error": "email unavailable"})
    )
    text_completion = AsyncMock(
        side_effect=AssertionError("a provider error must not force a tool-free summary")
    )

    async def answering_round(messages, tools, **_kwargs):
        # The resumed call's error reaches the model with its tools kept.
        assert [tool["function"]["name"] for tool in tools] == ["mcp__email__list_folders"]
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "email-3"
        assert "email unavailable" in messages[-1]["content"]
        return "Email remained unavailable; I stopped retrying.", [], {}

    tool_completion = AsyncMock(side_effect=answering_round)
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_text_completion",
        text_completion,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        tool_completion,
    )

    resumed = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="list folders",
        tools=[_tool("mcp__email__list_folders")],
        tool_executor=resumed_executor,
        resume_checkpoint=checkpoint,
        max_rounds=5,
    )

    assert resumed.stop_reason == "completed" and resumed.error is None
    assert resumed.content == "Email remained unavailable; I stopped retrying."
    # The resumed pending call runs exactly once; nothing replays it.
    resumed_executor.assert_awaited_once()
    tool_completion.assert_awaited_once()
    text_completion.assert_not_awaited()


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


@pytest.mark.unit
async def test_exact_duplicate_stateful_calls_in_one_round_execute_provider_once(
    monkeypatch,
) -> None:
    rounds = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return "", [
                {
                    "id": "send-1",
                    "name": "mcp__email__send_message",
                    "arguments": {"to": "ops@example.com", "body": "Ready"},
                },
                {
                    "id": "send-2",
                    "name": "mcp__email__send_message",
                    "arguments": {"to": "ops@example.com", "body": "Ready"},
                },
            ], {}
        return "sent", None, {}

    provider_calls = 0

    async def tool_executor(_name: str, _args: dict):
        nonlocal provider_calls
        provider_calls += 1
        return json.dumps({"ok": True, "message_id": "message-1"})

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_round,
    )
    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="send once",
        tools=[_tool("mcp__email__send_message")],
        tool_executor=tool_executor,
        runtime_run_id="same-round-effect-dedupe",
        max_rounds=3,
    )

    assert provider_calls == 1
    assert result.content == "sent"
    tool_messages = [
        message for message in result.messages if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "send-1",
        "send-2",
    ]
    duplicate = json.loads(tool_messages[1]["content"])
    assert duplicate["repeated_tool_result"] is True
    assert duplicate["tool"] == "mcp__email__send_message"
    assert "message-1" in duplicate["preview"]


@pytest.mark.unit
async def test_pre_io_checkpoint_keeps_deferred_continuation_from_prior_sibling(
) -> None:
    """Crashing in call B must not lose call A's mandatory provider retry."""
    from packages.core.ai.runtime.tool_effect_intents import (
        runtime_record_tool_effect_intent,
        runtime_tool_effect_intent_scope,
    )

    continuation = {
        "kind": "retry_with_result_token",
        "tool": "mcp__chrome__click_element",
        "arguments": {"tabId": 7, "ref": "e1"},
        "required_status": "approved",
        "result_token_keys": ["approvalToken"],
        "argument_token_key": "approvalToken",
    }

    forced_calls = [
        {
            "name": "mcp__chrome__confirm_action",
            "arguments": {
                "approvalId": "approval-1",
                "__manor_tool_continuation": continuation,
            },
        },
        {
            "name": "mcp__example__write",
            "arguments": {"value": "B"},
        },
    ]

    checkpoints: dict[str, dict] = {}

    async def observe_intent(name, _args, _call_id, _classification, checkpoint):
        checkpoints[name] = checkpoint

    async def tool_executor(name: str, args: dict):
        call_id = str(args["_runtime_tool_call_id_from_context"])
        public_args = {
            key: value for key, value in args.items() if not key.startswith("_runtime_")
        }
        await runtime_record_tool_effect_intent(name, public_args, call_id)
        if name == "mcp__chrome__confirm_action":
            return json.dumps({
                "ok": True,
                "status": "approved",
                "approvalToken": "provider-token",
            })
        return RuntimeToolSuspension(
            kind="waiting_resource",
            reservation_id="write-b",
            poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    with runtime_tool_effect_intent_scope(observe_intent):
        result = await loop_module.agentic_loop(
            system_prompt="system",
            user_message="approve, then write",
            tools=[
                _tool("mcp__chrome__confirm_action"),
                _tool("mcp__chrome__click_element"),
                _tool("mcp__example__write"),
            ],
            tool_executor=tool_executor,
            forced_tool_calls=forced_calls,
            runtime_run_id="deferred-before-write",
            max_rounds=3,
        )

    before_write = checkpoints["mcp__example__write"]
    pending_id = before_write["pending_tool_call"]["id"]
    assert before_write["pending_tool_call"]["name"] == "mcp__example__write"
    assert len(before_write["remaining_tool_calls"]) == 1
    deferred = before_write["remaining_tool_calls"][0]
    assert deferred["name"] == "mcp__chrome__click_element"
    assert deferred["arguments"] == {
        "tabId": 7,
        "ref": "e1",
        "approvalToken": "provider-token",
    }
    declared_call_ids = [
        str(call.get("id") or "")
        for message in before_write["messages"]
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]
    assert declared_call_ids.count(pending_id) == 1
    assert declared_call_ids.count(deferred["id"]) == 1
    assert any(
        message.get("role") == "tool"
        and message.get("tool_call_id") != pending_id
        for message in before_write["messages"]
    )
    assert result.control["checkpoint"]["remaining_tool_calls"] == (
        before_write["remaining_tool_calls"]
    )


@pytest.mark.unit
@pytest.mark.parametrize("max_rounds", [1, 3])
async def test_forced_provider_continuation_survives_suspension_without_duplicate_call(
    monkeypatch,
    max_rounds: int,
) -> None:
    forced_calls = [
        {
            "name": "mcp__chrome__confirm_action",
            "arguments": {
                "approvalId": "approval-1",
                "__manor_tool_continuation": {
                    "kind": "retry_with_result_token",
                    "tool": "mcp__chrome__click_element",
                    "arguments": {"tabId": 7, "ref": "e1"},
                    "required_status": "approved",
                    "result_token_keys": ["approvalToken"],
                    "argument_token_key": "approvalToken",
                },
            },
            "disable_followup_tools": True,
        }
    ]
    first_calls: list[str] = []

    async def suspending_executor(name: str, _args: dict):
        first_calls.append(name)
        return RuntimeToolSuspension(
            kind="waiting_resource",
            reservation_id="provider-reservation",
            poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    suspended = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="approve",
        tools=[
            _tool("mcp__chrome__confirm_action"),
            _tool("mcp__chrome__click_element"),
        ],
        tool_executor=suspending_executor,
        forced_tool_calls=forced_calls,
        runtime_run_id="provider-run",
        max_rounds=max_rounds,
    )

    resumed_calls: list[tuple[str, dict]] = []

    async def resumed_executor(name: str, args: dict):
        resumed_calls.append((name, dict(args)))
        if name == "mcp__chrome__confirm_action":
            return json.dumps({
                "ok": True,
                "status": "approved",
                "approvalToken": "provider-token",
            })
        return json.dumps({"ok": True, "status": "clicked"})

    text_completion = AsyncMock(return_value=("done", {}))
    final_completion = AsyncMock(return_value=("done", {}))
    tool_completion = AsyncMock()
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_text_completion",
        text_completion,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        tool_completion,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_final_completion",
        final_completion,
    )

    resumed = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="approve",
        tools=[
            _tool("mcp__chrome__confirm_action"),
            _tool("mcp__chrome__click_element"),
        ],
        tool_executor=resumed_executor,
        forced_tool_calls=forced_calls,
        runtime_run_id="provider-run",
        resume_checkpoint=suspended.control["checkpoint"],
        max_rounds=max_rounds,
    )

    assert first_calls == ["mcp__chrome__confirm_action"]
    public_resumed_calls = [
        (
            name,
            {
                key: value
                for key, value in args.items()
                if not key.startswith("_runtime_")
            },
        )
        for name, args in resumed_calls
    ]
    assert public_resumed_calls == [
        ("mcp__chrome__confirm_action", {"approvalId": "approval-1"}),
        (
            "mcp__chrome__click_element",
            {
                "tabId": 7,
                "ref": "e1",
                "approvalToken": "provider-token",
            },
        ),
    ]
    assert resumed.content == "done"
    if max_rounds == 1:
        text_completion.assert_not_awaited()
        final_completion.assert_awaited_once()
    else:
        text_completion.assert_awaited_once()
        final_completion.assert_not_awaited()
    tool_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_sandbox_handoff_extends_a_small_round_budget(monkeypatch) -> None:
    """A turn planned for 2 rounds suspends on the skill call; on resume the
    re-executed call hands off a sandbox, and the workflow that follows needs
    more rounds than the plan allowed."""
    round_calls = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal round_calls
        round_calls += 1
        if round_calls == 1:
            return "", [{"id": "call-skill", "name": "invoke_skill", "arguments": {"skill": "xlsx"}}], {}
        if round_calls <= 6:
            return "", [{"id": f"call-{round_calls}", "name": "sandbox", "arguments": {"action": "exec"}}], {}
        return "done", None, {}

    async def suspending_executor(_name: str, args: dict):
        return RuntimeToolSuspension(
            kind="waiting_resource",
            reservation_id="reservation-handoff",
            poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    suspended = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="build the workbook",
        tools=[_tool("invoke_skill"), _tool("sandbox")],
        tool_executor=suspending_executor,
        runtime_run_id="runtime-handoff",
        max_rounds=2,
    )

    handoff = json.dumps(
        {"status": "sandbox_ready", "skill": "xlsx", "sandbox_id": "sb_1", "loaded_tools": ["sandbox"]}
    )

    async def resumed_executor(name: str, args: dict):
        return handoff if name == "invoke_skill" else "ok"

    resumed = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="build the workbook",
        tools=[_tool("invoke_skill"), _tool("sandbox")],
        tool_executor=resumed_executor,
        runtime_run_id="runtime-handoff",
        resume_checkpoint=suspended.control["checkpoint"],
        max_rounds=2,
    )

    assert resumed.stop_reason == "completed"
    assert resumed.content == "done"
    assert round_calls == 7


# ── Park/resume keeps the turn's loop state (2026-10-19 audit) ────────────────
# A durable turn runs in segments: a tool parks, the process exits, a new one
# resumes from the checkpoint. State the loop keeps only in memory is lost at
# that boundary (video_edit's guide check rejected an admitted call on every
# resume). Every field below must survive the round trip, and the resumed loop
# must see the same budget and tool surface the parked one had.

def _populated_checkpoint() -> RuntimeAgentCheckpoint:
    return RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "go"}],
        usage={"total": 1},
        rounds=3,
        tool_calls_made=["look"],
        pending_tool_call={"id": "call-1", "name": "look", "arguments": {}},
        remaining_tool_calls=[{"id": "call-2", "name": "write", "arguments": {}}],
        pending_attempt=2,
        seen_tool_result_digests={"look": "digest"},
        disable_followup_tools=True,
        tool_continuations={"call-1": {"kind": "retry"}},
        pending_tool_result={"id": "call-1", "content": "answer"},
        final_response_evidence={"requires_execution": True},
        tool_evidence={"effect_floor": 0, "observations": []},
        tool_guidance={"look": "a" * 64},
        web_source_slices=[{"url": "https://example.com", "text": "x"}],
        loaded_gateway_tools=["look"],
        max_rounds=9,
        sandbox_budget_extended_for=["code"],
        tool_narrowing=[{"mode": "allowlist", "names": ["look"]}],
    )


def test_every_checkpoint_field_survives_a_park() -> None:
    import dataclasses

    populated = _populated_checkpoint()
    defaults = RuntimeAgentCheckpoint(
        messages=[], usage={}, rounds=0, tool_calls_made=[],
        pending_tool_call={"id": "call-1", "name": "look"},
    )
    names = [field.name for field in dataclasses.fields(RuntimeAgentCheckpoint)]
    # A new field must be added to _populated_checkpoint (and so to this round trip).
    assert [name for name in names if getattr(populated, name) == getattr(defaults, name)] == []

    restored = RuntimeAgentCheckpoint.from_dict(populated.to_dict())
    assert {name: getattr(restored, name) for name in names} == {
        name: getattr(populated, name) for name in names
    }


def _resume_checkpoint(**fields) -> dict:
    pending = {"id": "call-pending", "name": "look", "arguments": {}}
    return RuntimeAgentCheckpoint(**{
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-pending", "type": "function",
                "function": {"name": "look", "arguments": "{}"},
            }]},
        ],
        "usage": {}, "rounds": 3, "tool_calls_made": ["look"],
        "pending_tool_call": pending, "remaining_tool_calls": [],
        **fields,
    }).to_dict()


@pytest.mark.unit
async def test_a_resume_keeps_the_round_budget_the_turn_had(monkeypatch) -> None:
    """A sandbox handoff extended the budget to 9; the resumed worker passes the
    caller's 2. Before: rounds 3 > 2 skipped the loop straight to synthesis."""

    offered: list[list[str]] = []

    async def fake_round(_messages, tools, **_kwargs):
        offered.append(sorted(tool["function"]["name"] for tool in tools))
        return "done", [], {}

    async def tool_executor(_name, _args):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="go", tools=[_tool("look"), _tool("write")],
        tool_executor=tool_executor, max_rounds=2,
        resume_checkpoint=_resume_checkpoint(max_rounds=9, sandbox_budget_extended_for=["code"]),
    )

    assert result.content == "done"
    assert offered == [["look", "write"]]  # a tool round, not a forced tool-free synthesis


@pytest.mark.unit
@pytest.mark.parametrize(("narrowing", "expected"), [
    ([], ["look", "write"]),
    ([{"mode": "read_only"}], ["look"]),
    ([{"mode": "none"}], []),
    ([{"mode": "allowlist", "names": ["write"]}], ["write"]),
])
async def test_a_resume_keeps_the_tool_surface_the_turn_narrowed(monkeypatch, narrowing, expected) -> None:
    """After an approved write failed pre-I/O the turn kept only read-only tools;
    a park on a read-only call must not hand the full list back on resume."""

    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    offered: list[list[str]] = []

    async def fake_round(_messages, tools, **_kwargs):
        offered.append(sorted(tool["function"]["name"] for tool in tools))
        return "done", [], {}

    async def tool_executor(_name, _args):
        return json.dumps({"status": "ok"})

    def classify(name, _arguments):
        effect = RuntimeToolEffect.READ_ONLY if name == "look" else RuntimeToolEffect.ACTION
        return type("Classification", (), {"effect": effect})()

    monkeypatch.setattr(loop_module, "classify_runtime_tool", classify)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="go", tools=[_tool("look"), _tool("write")],
        tool_executor=tool_executor, max_rounds=6,
        resume_checkpoint=_resume_checkpoint(tool_narrowing=narrowing),
    )

    # With no tools left the loop answers on its tool-free path (not this mock).
    assert (offered[0] if offered else []) == expected
    if expected:
        assert result.error is None


@pytest.mark.unit
async def test_a_resumed_run_can_still_unload_schemas_discovery_loaded(monkeypatch) -> None:
    """The restored discovery schemas overflow the provider request: the loop
    must drop them and retry, as it would have before the park."""

    offered: list[list[str]] = []

    async def fake_round(_messages, tools, **_kwargs):
        offered.append(sorted(tool["function"]["name"] for tool in tools))
        if len(offered) == 1:
            return "", [], {"error": "request too large"}
        return "done", [], {}

    async def tool_executor(_name, _args):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="go",
        tools=[_tool("look"), _tool("found_by_search")],
        tool_executor=tool_executor, max_rounds=6,
        resume_checkpoint=_resume_checkpoint(loaded_gateway_tools=["found_by_search"]),
    )

    assert result.content == "done"
    assert offered == [["found_by_search", "look"], ["look"]]
