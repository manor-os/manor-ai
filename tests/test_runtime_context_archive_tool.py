"""Context archive is checkpoint recovery, not a truncated-tool bait."""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.context_compaction import _checkpoint_message
from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect


def _tool_names(tools: list[dict]) -> set[str]:
    return {
        str((schema.get("function") or {}).get("name") or "")
        for schema in tools
    }


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


async def _allow_runtime_approval_for_unit_test(request):
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware

    return RuntimeApprovalDecision.allow(
        RuntimeApprovalMiddleware().classify_request(request)
    )


def _run_harness(*, initial_messages=None, runtime_checkpoint=None):
    seen: dict[str, object] = {}

    async def fake_agentic_loop(**kwargs):
        seen["tools"] = kwargs["tools"]
        return SimpleNamespace(content="ok", usage={}, rounds=1, tool_calls_made=[])

    async def fake_reader(args: dict) -> str:
        return json.dumps({"archive_id": args.get("archive_id")})

    def fake_ensure_billing_context(entity_id: str, source: str = "system", **kwargs):
        seen["billing"] = {"entity_id": entity_id, "source": source, **kwargs}

    async def fake_execute_tool(name: str, args: dict, **kwargs):
        return "ok"

    with (
        patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop),
        patch("packages.core.ai.llm_client.ensure_billing_context", new=fake_ensure_billing_context),
        patch("packages.core.ai.runtime.tool_registry.runtime_execute_tool", new=fake_execute_tool),
        patch("packages.core.ai.runtime.tool_registry.runtime_tool_schema", new=_schema),
        patch(
            "packages.core.ai.runtime.tool_execution.RuntimeApprovalMiddleware.guard_request",
            side_effect=_allow_runtime_approval_for_unit_test,
        ),
    ):
        asyncio.run(
            runtime_execute_agentic_loop(
                runtime_envelope=None,
                system_prompt="system",
                user_message="user",
                tools=[_schema("search_tools")],
                entity_id="ent_1",
                agent_id="agent_1",
                user_id="user_1",
                conversation_id="conv_1",
                allowed_tool_names={"search_tools"},
                context_archive_reader=fake_reader,
                initial_messages=initial_messages,
                runtime_checkpoint=runtime_checkpoint,
            )
        )
    return seen


def test_harness_hides_read_context_archive_until_checkpoint_has_archive_id() -> None:
    seen = _run_harness()
    assert "read_context_archive" not in _tool_names(seen["tools"])


def test_harness_exposes_read_context_archive_for_checkpoint_archive_id() -> None:
    checkpoint = _checkpoint_message({"records": []}, "01ARCHIVEIDTEST0000000000", "digest")
    seen = _run_harness(initial_messages=[checkpoint])
    assert "read_context_archive" in _tool_names(seen["tools"])

    seen = _run_harness(runtime_checkpoint={"messages": [checkpoint]})
    assert "read_context_archive" in _tool_names(seen["tools"])


def test_read_context_archive_classifies_as_read_only_discovery() -> None:
    classification = classify_runtime_tool("read_context_archive", {"archive_id": "01X"})
    assert classification.effect is RuntimeToolEffect.READ_ONLY
    assert classification.authorization is not None
    assert classification.authorization.capability_id == "runtime.discovery"


@pytest.mark.asyncio
async def test_compacted_tool_result_does_not_bait_read_context_archive(monkeypatch):
    archived = []
    observed = ""

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal observed
        if any(message.get("role") == "tool" for message in messages):
            observed = next(
                message["content"]
                for message in reversed(messages)
                if message.get("role") == "tool"
            )
            return "done", None, {"prompt": 10, "completion": 1, "total": 11}
        return (
            "",
            [{"id": "call_1", "name": "get_entity_info", "arguments": {}}],
            {"prompt": 10, "completion": 1, "total": 11},
        )

    async def fake_executor(name, args):
        return json.dumps({"profile": "x" * 8000, "notes": ["y" * 200 for _ in range(20)]})

    async def archive(messages):
        archived.append(messages)
        return "01ARCHIVEFROMTOOLRESULT000"

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="find tools",
        tools=[_schema("get_entity_info")],
        tool_executor=fake_executor,
        on_context_compacted=archive,
        max_rounds=3,
    )

    assert result.content == "done"
    assert archived, "oversized results should still be archived"
    assert "read_context_archive" not in observed
    assert "_context_archive" not in observed


@pytest.mark.asyncio
async def test_failed_tool_result_archive_is_reported_to_the_model_not_the_user(monkeypatch):
    observed = ""

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal observed
        if any(message.get("role") == "tool" for message in messages):
            observed = next(
                message["content"]
                for message in reversed(messages)
                if message.get("role") == "tool"
            )
            return "done", None, {"prompt": 10, "completion": 1, "total": 11}
        return (
            "",
            [{"id": "call_1", "name": "get_entity_info", "arguments": {}}],
            {"prompt": 10, "completion": 1, "total": 11},
        )

    async def fake_executor(name, args):
        return json.dumps({"profile": "x" * 8000, "notes": ["y" * 200 for _ in range(20)]})

    archive_calls = []

    async def archive(messages):
        archive_calls.append(messages)
        raise RuntimeError("invalid input syntax for type json")

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="find tools",
        tools=[_schema("get_entity_info")],
        tool_executor=fake_executor,
        on_context_compacted=archive,
        max_rounds=3,
    )

    # The turn continues; the model sees what happened and decides.
    assert archive_calls, "the oversized result must reach the archive"
    assert result.content == "done"
    assert not result.error
    assert "could not be kept" in observed
    assert "invalid input syntax" not in observed
