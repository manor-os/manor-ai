from __future__ import annotations

import importlib

import pytest


@pytest.mark.asyncio
async def test_agentic_loop_can_use_separate_final_synthesis_model(monkeypatch):
    observed: dict[str, object] = {}
    reserved_models: list[str | None] = []

    async def fake_tool_round(messages, tools, **kwargs):
        return (
            "",
            [{"id": "search_1", "name": "web_search", "arguments": {"q": "x"}}],
            {"prompt": 20, "completion": 2, "total": 22},
        )

    async def fake_final(messages, **kwargs):
        observed.update(kwargs)
        return "premium synthesis", {"prompt": 30, "completion": 4, "total": 34}

    async def fake_before(round_num, model, messages, max_tokens):
        del round_num, messages, max_tokens
        reserved_models.append(model)

    async def fake_executor(name, args):
        return '{"results": [{"title": "evidence"}]}'

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_tool_round,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_final_completion",
        fake_final,
    )

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="research",
        tools=[{"type": "function", "function": {"name": "web_search"}}],
        tool_executor=fake_executor,
        model="openai/gpt-5.6-terra",
        final_model="openai/gpt-5.5",
        final_metadata={"route": "synthesis"},
        on_llm_call_before=fake_before,
        max_rounds=1,
    )

    assert result.content == "premium synthesis"
    assert observed["model"] == "openai/gpt-5.5"
    assert observed["metadata"] == {"route": "synthesis"}
    assert reserved_models == ["openai/gpt-5.6-terra", "openai/gpt-5.5"]
    assert result.usage["total"] == 56
