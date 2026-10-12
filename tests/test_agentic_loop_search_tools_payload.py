"""search_tools expansion must not turn a later LLM 400 into a dead Sorry.

The grok/xAI path has no native adapter and no payload-size cap. Injecting
discovered schemas can push the next round over the provider budget. That is a
runtime observation, not a terminal user-facing failure, and the loop retries
once with the original tool set.
"""
from __future__ import annotations

import importlib
import json

import pytest

from packages.core.ai.llm_client import (
    _MAX_LLM_TOOL_SCHEMA_CHARS,
    _llm_tool_schema_chars,
    _normalize_tools_for_llm,
)


def _schema(name: str, description: str = "tool") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _search_result(tool_name: str = "discovered_tool") -> str:
    return json.dumps(
        {
            "query": "file",
            "matches": [
                {
                    "name": tool_name,
                    "available": True,
                    "schema": _schema(tool_name, "Discovered after search_tools"),
                }
            ],
            "loaded_tools": [tool_name],
        }
    )


def _tool_names(tools) -> list[str]:
    return [str((tool.get("function") or {}).get("name") or "") for tool in tools]


@pytest.mark.asyncio
async def test_search_tools_payload_error_becomes_observation_and_retries(monkeypatch):
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    rounds_seen: list[list[str]] = []

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        names = _tool_names(tools)
        rounds_seen.append(names)
        if "discovered_tool" in names:
            return (
                "",
                None,
                {"error": "payload too large", "prompt": 0, "completion": 0, "total": 0},
            )
        if any(
            message.get("role") == "user"
            and "[Runtime tool observation]" in str(message.get("content") or "")
            for message in messages
        ):
            return "recovered with original tools", None, {"prompt": 1, "completion": 1, "total": 2}
        return (
            "",
            [{"id": "call_search", "name": "search_tools", "arguments": {"query": "file"}}],
            {"prompt": 1, "completion": 1, "total": 2},
        )

    async def fake_executor(name, args):
        assert name == "search_tools"
        return _search_result()

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_chat_completion_with_tools,
    )

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="find a file tool",
        tools=[_schema("search_tools")],
        tool_executor=fake_executor,
        max_rounds=5,
    )

    assert "Sorry" not in (result.content or "")
    assert result.content == "recovered with original tools"
    assert any(
        message.get("role") == "user"
        and "[Runtime tool observation]" in str(message.get("content") or "")
        for message in result.messages
    )
    assert rounds_seen[0] == ["search_tools"]
    assert "discovered_tool" in rounds_seen[1]
    assert rounds_seen[2] == ["search_tools"]


@pytest.mark.asyncio
async def test_search_tools_payload_retry_happens_only_once(monkeypatch):
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        names = _tool_names(tools)
        if "discovered_tool" in names:
            return (
                "",
                None,
                {"error": "payload too large", "prompt": 0, "completion": 0, "total": 0},
            )
        if any(
            message.get("role") == "user"
            and "[Runtime tool observation]" in str(message.get("content") or "")
            for message in messages
        ):
            return (
                "",
                None,
                {"error": "original set still too large", "prompt": 0, "completion": 0, "total": 0},
            )
        return (
            "",
            [{"id": "call_search", "name": "search_tools", "arguments": {"query": "file"}}],
            {"prompt": 1, "completion": 1, "total": 2},
        )

    async def fake_executor(name, args):
        return _search_result()

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_chat_completion_with_tools,
    )

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="find a file tool",
        tools=[_schema("search_tools")],
        tool_executor=fake_executor,
        max_rounds=5,
    )

    assert result.error == "llm_call_failed"
    assert "Sorry" in (result.content or "")
    assert sum(
        1
        for message in result.messages
        if message.get("role") == "user"
        and "[Runtime tool observation]" in str(message.get("content") or "")
    ) == 1


@pytest.mark.asyncio
async def test_first_round_llm_error_without_search_tools_stays_sorry(monkeypatch):
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        return (
            "",
            None,
            {"error": "provider 400", "prompt": 0, "completion": 0, "total": 0},
        )

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_chat_completion_with_tools,
    )

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="hello",
        tools=[_schema("search_tools")],
        tool_executor=lambda name, args: "",
        max_rounds=3,
    )

    assert result.error == "llm_call_failed"
    assert "Sorry" in (result.content or "")
    assert not any(
        "[Runtime tool observation]" in str(message.get("content") or "")
        for message in result.messages
    )


def test_normalize_tools_caps_serialized_schema_chars() -> None:
    huge = "x" * 20_000
    tools = [_schema(f"tool_{index}", huge) for index in range(10)]
    result = _normalize_tools_for_llm(tools)
    assert _llm_tool_schema_chars(result) <= _MAX_LLM_TOOL_SCHEMA_CHARS
    assert len(result) < len(tools)
    assert result[0]["function"]["name"] == "tool_0"


@pytest.mark.asyncio
async def test_manor_search_results_reach_the_model_with_how_to_call_each_match(monkeypatch):
    """search_tools results are compacted to names because their schemas are
    loaded separately; manor search returns call shapes, which are the content."""
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    seen: list[list[dict]] = []
    manor_result = {
        "mode": "manor", "query": "create workspace",
        "matches": [{"id": "manor_action:start_workspace_draft", "name": "start_workspace_draft",
                     "description": "Start a guided Workspace draft.", "available": True,
                     "call": {"tool": "manor", "arguments": {"action": "start_workspace_draft"}}}],
        "loaded_tools": [],
    }

    async def fake_completion(messages, tools, **kwargs):
        seen.append(list(messages))
        if len(seen) == 1:
            return "", [{"id": "call_1", "name": "manor",
                         "arguments": {"action": "search", "query": "create workspace"}}], {"prompt": 1, "completion": 1, "total": 2}
        return "done", None, {"prompt": 1, "completion": 1, "total": 2}

    async def fake_executor(name, args):
        return json.dumps(manor_result)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_completion)
    result = await loop_module.agentic_loop(
        system_prompt="test", user_message="make me a workspace",
        tools=[_schema("manor"), _schema("search_tools")], tool_executor=fake_executor, max_rounds=3,
    )
    assert result.content == "done"
    tool_message = next(m for m in seen[1] if m.get("role") == "tool")
    delivered = json.loads(tool_message["content"])
    assert delivered["matches"][0]["call"] == {"tool": "manor", "arguments": {"action": "start_workspace_draft"}}
    assert delivered["loaded_tools"] == []
