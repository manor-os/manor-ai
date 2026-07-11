from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_agentic_tool_completion_streams_by_default(monkeypatch) -> None:
    from packages.core.ai.runtime import agentic_llm

    captured: dict = {}

    async def fake_chat_completion_with_tools(_messages, _tools, **kwargs):
        captured.update(kwargs)
        return "", None, {}

    monkeypatch.delenv("RUNTIME_AGENTIC_TOOL_STREAMING", raising=False)
    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", fake_chat_completion_with_tools)

    await agentic_llm.runtime_execute_agentic_round_tool_completion(
        [{"role": "user", "content": "work"}],
        [],
        temperature=0.2,
    )

    assert callable(captured["stream_handler"])
    assert await captured["stream_handler"]("text_delta", {"content": "x"}) is None


@pytest.mark.asyncio
async def test_agentic_tool_completion_streaming_can_be_disabled(monkeypatch) -> None:
    from packages.core.ai.runtime import agentic_llm

    captured: dict = {}

    async def fake_chat_completion_with_tools(_messages, _tools, **kwargs):
        captured.update(kwargs)
        return "", None, {}

    monkeypatch.setenv("RUNTIME_AGENTIC_TOOL_STREAMING", "0")
    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", fake_chat_completion_with_tools)

    await agentic_llm.runtime_execute_agentic_round_tool_completion(
        [{"role": "user", "content": "work"}],
        [],
        temperature=0.2,
    )

    assert captured["stream_handler"] is None

