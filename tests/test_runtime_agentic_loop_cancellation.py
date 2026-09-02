"""Regression tests for cancellation callbacks crossing the runtime boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


def test_tool_pool_forwards_manual_skill_ids_without_legacy_step_id() -> None:
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
    assert "step_id" not in captured
