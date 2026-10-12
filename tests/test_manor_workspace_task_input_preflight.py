"""Malformed composite Workspace Task calls remain correctable in Chat."""

from __future__ import annotations

import json

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_intent_scope
from packages.core.ai.tool_pool import ToolPool
from packages.core.ai.tools.manor_tool import MANOR_SCHEMA


@pytest.mark.unit
@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
async def test_workspace_task_name_without_title_corrects_before_effect(monkeypatch, entry):
    """The reported name-only call must not end a stateful Chat turn."""

    invalid = {
        "id": "name-only-task",
        "name": "manor",
        "arguments": {
            "action": "workspace",
            "params": {
                "action": "create_task",
                "params": {
                    "name": "Create Production Plans for Three Original Reel Drafts",
                    "service_key": "production_planning",
                    "agent_subscription_id": "subscription-1",
                },
            },
        },
    }
    corrected = {
        "id": "titled-task",
        "name": "manor",
        "arguments": {
            "action": "workspace",
            "params": {
                "action": "create_task",
                "params": {
                    "title": "Create Production Plans for Three Original Reel Drafts",
                    "description": "Prepare detailed production plans for the three original Reel drafts.",
                    "service_key": "production_planning",
                },
            },
        },
    }
    pool = ToolPool()
    handled = []
    intents = []
    model_calls = 0

    async def handler(**arguments):
        handled.append(arguments)
        return json.dumps({"created": True, "task": {"id": "task-1"}})

    async def execute(name, arguments):
        if arguments.get("_runtime_tool_call_id_from_context") == corrected["id"]:
            public_arguments = {key: value for key, value in arguments.items() if key != "_runtime_tool_call_id_from_context"}
            assert public_arguments == corrected["arguments"]
            return await handler(**public_arguments)
        return await pool.execute(
            name, arguments, entity_id="entity-1", user_id="user-1",
            workspace_id="workspace-1", conversation_id="conversation-1",
        )

    async def model(messages, tools, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if entry == "normal" and model_calls == 1:
            return "", [invalid], {}
        if not handled:
            tool_results = [item for item in messages if item.get("role") == "tool"]
            assert any("$.params.params.title" in str(item.get("content")) for item in tool_results)
            assert intents == []
            return "", [corrected], {}
        return "Created the task.", [], {}

    async def intent(*args, **kwargs):
        intents.append((args, kwargs))

    pool.register("manor", MANOR_SCHEMA, handler)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    extra = {}
    if entry == "forced":
        extra["forced_tool_calls"] = [invalid]
    elif entry == "resume":
        extra["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[
                {"role": "system", "content": "system"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": invalid["id"], "type": "function",
                    "function": {
                        "name": invalid["name"],
                        "arguments": json.dumps(invalid["arguments"]),
                    },
                }]},
            ],
            usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call=invalid, remaining_tool_calls=[],
        ).to_dict()
    with runtime_tool_effect_intent_scope(intent):
        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="可以", tools=[MANOR_SCHEMA],
            tool_executor=execute, max_rounds=4, **extra,
        )

    assert result.content == "Created the task."
    assert result.error is None
    assert len(handled) == 1
    assert handled[0]["params"]["params"]["title"] == corrected["arguments"]["params"]["params"]["title"]
