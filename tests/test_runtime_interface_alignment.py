from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
from packages.core.ai.tool_pool import ToolPool


def test_tool_pool_forwards_manual_skill_ids_without_legacy_step_id() -> None:
    pool = ToolPool()
    seen: dict[str, object] = {}

    async def fake_execute_registered_tool(**kwargs):
        seen.update(kwargs)
        return "ok"

    with patch(
        "packages.core.ai.tool_pool.runtime_execute_registered_tool",
        new=fake_execute_registered_tool,
    ):
        result = asyncio.run(
            pool.execute(
                "search_tools",
                {"query": "workspace"},
                entity_id="entity-1",
                step_id="step-1",
                manual_skill_ids=["skill-1"],
                manual_skill_slugs=["research"],
            )
        )

    assert result == "ok"
    assert seen["manual_skill_ids"] == ["skill-1"]
    assert seen["manual_skill_slugs"] == ["research"]
    assert "step_id" not in seen


def test_runtime_agentic_loop_forwards_cancellation_without_dead_policy_fields() -> None:
    seen: dict[str, object] = {}

    async def fake_resolve_route(**kwargs):
        del kwargs
        return "model-1", {"provider": "test"}, None

    def fake_billing_context(*args, **kwargs):
        del args, kwargs

    async def fake_agentic_loop(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(content="ok", usage={}, rounds=1, tool_calls_made=[])

    async def is_cancelled() -> bool:
        return False

    with (
        patch(
            "packages.core.ai.runtime.completions.runtime_resolve_text_completion_route",
            new=fake_resolve_route,
        ),
        patch(
            "packages.core.ai.runtime.billing.runtime_ensure_billing_context",
            new=fake_billing_context,
        ),
        patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop),
    ):
        result = asyncio.run(
            runtime_execute_agentic_loop(
                runtime_envelope=None,
                system_prompt="system",
                user_message="user",
                tools=[],
                entity_id="entity-1",
                agent_id="agent-1",
                step_id="step-1",
                is_cancelled=is_cancelled,
            )
        )

    assert result.content == "ok"
    assert seen["is_cancelled"] is is_cancelled
    assert "required_tool_completion_policy" not in seen
