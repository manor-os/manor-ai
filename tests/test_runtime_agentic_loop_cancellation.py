"""Regression tests for cancellation callbacks crossing the runtime boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import agentic_loop
from packages.core.ai.runtime.harness import (
    runtime_execute_agentic_loop,
    runtime_execute_chat_agent_loop,
)


def test_chat_runtime_agent_loop_accepts_and_forwards_is_cancelled() -> None:
    captured: dict[str, object] = {}

    async def fake_route(**_kwargs):
        return "test-model", {}, None

    async def fake_agentic_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(usage={}, rounds=0, tool_calls_made=[])

    async def is_cancelled() -> bool:
        return True

    with (
        patch(
            "packages.core.ai.runtime.completions.runtime_resolve_text_completion_route",
            new=fake_route,
        ),
        patch(
            "packages.core.ai.runtime.billing.runtime_ensure_billing_context",
            new=lambda *_args, **_kwargs: None,
        ),
        patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop),
    ):
        asyncio.run(
            runtime_execute_chat_agent_loop(
                runtime_envelope=None,
                system_prompt="system",
                user_message="stop this turn",
                tools=[],
                entity_id="entity",
                agent_id=None,
                is_cancelled=is_cancelled,
            )
        )

    assert captured["is_cancelled"] is is_cancelled


def test_runtime_omits_policy_for_legacy_agentic_loop_signature() -> None:
    captured: dict[str, object] = {}

    async def fake_route(**_kwargs):
        return "test-model", {}, None

    # A deployed agentic_loop from before the policy rollout accepts the
    # normal runtime kwargs but has no required_tool_completion_policy slot.
    async def legacy_agentic_loop(
        *,
        system_prompt,
        user_message,
        tools,
        tool_executor,
        model=None,
        final_model=None,
        final_metadata=None,
        initial_messages=None,
        on_tool_start=None,
        on_tool_end=None,
        on_llm_call=None,
        on_llm_call_before=None,
        on_llm_usage_settled=None,
        stream_handler=None,
        is_cancelled=None,
        metadata=None,
        tool_schema_resolver=None,
        forced_tool_calls=None,
        terminal_tool_result_policy=None,
        output_schema=None,
    ):
        captured.update(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "tools": tools,
                "tool_executor": tool_executor,
                "model": model,
                "final_model": final_model,
                "final_metadata": final_metadata,
                "initial_messages": initial_messages,
                "on_tool_start": on_tool_start,
                "on_tool_end": on_tool_end,
                "on_llm_call": on_llm_call,
                "on_llm_call_before": on_llm_call_before,
                "on_llm_usage_settled": on_llm_usage_settled,
                "stream_handler": stream_handler,
                "is_cancelled": is_cancelled,
                "metadata": metadata,
                "tool_schema_resolver": tool_schema_resolver,
                "forced_tool_calls": forced_tool_calls,
                "terminal_tool_result_policy": terminal_tool_result_policy,
                "output_schema": output_schema,
            }
        )
        return SimpleNamespace(usage={}, rounds=0, tool_calls_made=[])

    with (
        patch(
            "packages.core.ai.runtime.completions.runtime_resolve_text_completion_route",
            new=fake_route,
        ),
        patch(
            "packages.core.ai.runtime.billing.runtime_ensure_billing_context",
            new=lambda *_args, **_kwargs: None,
        ),
        patch("packages.core.ai.agentic_loop.agentic_loop", new=legacy_agentic_loop),
    ):
        asyncio.run(
            runtime_execute_agentic_loop(
                runtime_envelope=None,
                system_prompt="system",
                user_message="continue",
                tools=[],
                entity_id="entity",
                agent_id=None,
            )
        )

    assert "required_tool_completion_policy" not in captured


@pytest.mark.parametrize("cap", [None, 40_000])
def test_runtime_passes_a_tool_result_cap_only_when_one_is_set(cap) -> None:
    captured: dict[str, object] = {}

    async def fake_route(**_kwargs):
        return "test-model", {}, None

    async def current_agentic_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(usage={}, rounds=0, tool_calls_made=[])

    with (
        patch(
            "packages.core.ai.runtime.completions.runtime_resolve_text_completion_route",
            new=fake_route,
        ),
        patch(
            "packages.core.ai.runtime.billing.runtime_ensure_billing_context",
            new=lambda *_args, **_kwargs: None,
        ),
        patch("packages.core.ai.agentic_loop.agentic_loop", new=current_agentic_loop),
    ):
        asyncio.run(
            runtime_execute_agentic_loop(
                runtime_envelope=None,
                system_prompt="system",
                user_message="continue",
                tools=[],
                entity_id="entity",
                agent_id=None,
                tool_result_max_chars=cap,
            )
        )

    assert captured.get("tool_result_max_chars") == cap


def test_agentic_loop_returns_cancelled_result_before_next_round() -> None:
    async def is_cancelled() -> bool:
        return True

    with patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion",
        new_callable=AsyncMock,
    ) as mock_completion:
        result = asyncio.run(
            agentic_loop(
                system_prompt="system",
                user_message="stop this turn",
                tools=[],
                tool_executor=AsyncMock(),
                is_cancelled=is_cancelled,
            )
        )

    assert result.stop_reason == "cancelled"
    assert result.error == "chat_turn_cancelled"
    mock_completion.assert_not_awaited()


def test_agentic_loop_cancels_inflight_tool_executor() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_tool_completion(messages, tools, **kwargs):
        return (
            "",
            [{
                "id": "call_1",
                "name": "read_file",
                "arguments": {"path": "README.md"},
            }],
            {"prompt": 10, "completion": 1, "total": 11},
        )

    async def hung_executor(name, args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def scenario():
        task = asyncio.create_task(
            agentic_loop(
                system_prompt="system",
                user_message="read the file",
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {"type": "object"},
                    },
                }],
                tool_executor=hung_executor,
                is_cancelled=cancelled.is_set,
                max_rounds=1,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            cancelled.set()
            result = await asyncio.wait_for(asyncio.shield(task), 2)
            assert result.stop_reason == "cancelled"
            assert result.error == "chat_turn_cancelled"
            assert stopped.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        new=fake_tool_completion,
    ):
        asyncio.run(scenario())


def test_agentic_loop_cancels_inflight_serial_skill() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_tool_completion(messages, tools, **kwargs):
        return (
            "",
            [{
                "id": "call_1",
                "name": "invoke_skill",
                "arguments": {"skill": "local-coding-operations", "input": "edit the local file"},
            }],
            {"prompt": 10, "completion": 1, "total": 11},
        )

    async def hung_executor(name, args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def scenario():
        task = asyncio.create_task(
            agentic_loop(
                system_prompt="system",
                user_message="update daily tasks",
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "invoke_skill",
                        "parameters": {"type": "object"},
                    },
                }],
                tool_executor=hung_executor,
                is_cancelled=cancelled.is_set,
                max_rounds=1,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            cancelled.set()
            result = await asyncio.wait_for(asyncio.shield(task), 2)
            assert result.stop_reason == "cancelled"
            assert result.error == "chat_turn_cancelled"
            assert stopped.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        new=fake_tool_completion,
    ):
        asyncio.run(scenario())


def test_agentic_loop_cancels_inflight_model_round() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()
    cancelled = asyncio.Event()

    async def hung_completion(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def scenario():
        task = asyncio.create_task(
            agentic_loop(
                system_prompt="system",
                user_message="summarize this",
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {"type": "object"},
                    },
                }],
                tool_executor=lambda *_args, **_kwargs: None,
                is_cancelled=cancelled.is_set,
                max_rounds=1,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            cancelled.set()
            result = await asyncio.wait_for(asyncio.shield(task), 2)
            assert result.stop_reason == "cancelled"
            assert result.error == "chat_turn_cancelled"
            assert stopped.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        new=hung_completion,
    ):
        asyncio.run(scenario())


def test_cancellation_during_final_provider_response_is_settled_but_not_success() -> None:
    cancelled = False

    async def complete(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True
        return "Late answer", {"prompt": 10, "completion": 5, "total": 15}

    async def check():
        return cancelled

    settled = AsyncMock()
    with patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", new=complete,
    ):
        result = asyncio.run(agentic_loop(
            system_prompt="system", user_message="request", tools=[], tool_executor=AsyncMock(),
            runtime_cancel_checker=check, on_llm_usage_settled=settled,
        ))
    assert result.stop_reason == "cancelled"
    assert result.content == ""
    assert result.usage["total"] == 15
    settled.assert_awaited_once()


def test_tool_pool_forwards_manual_skill_ids_and_step_scope() -> None:
    from packages.core.ai.tool_pool import ToolPool

    captured: dict[str, object] = {}

    async def fake_runtime_execute_registered_tool(**kwargs):
        captured.update(kwargs)
        return "ok"

    with patch(
        "packages.core.ai.tool_pool.runtime_execute_registered_tool",
        new=fake_runtime_execute_registered_tool,
    ):
        result = asyncio.run(
            ToolPool().execute(
                "workspace_search",
                {"query": "recent tasks"},
                entity_id="entity",
                user_id="user",
                step_id="legacy-step",
                manual_skill_ids=["skill-1"],
                manual_skill_slugs=["skill-one"],
            )
        )

    assert result == "ok"
    assert captured["manual_skill_ids"] == ["skill-1"]
    assert captured["manual_skill_slugs"] == ["skill-one"]
    assert captured["step_id"] == "legacy-step"
