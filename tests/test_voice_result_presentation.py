"""Main results are spoken only through a successful Live presentation decision."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from apps.api import chat_voice
from apps.api.chat_audio import ChatAudioScope
from packages.core.ai import llm_client
from packages.core.ai import runtime as ai_runtime
from packages.core.ai.runtime import completions
from packages.core.services.voice.realtime import VoiceAgentOutcome


@pytest.fixture
def presenter(monkeypatch):
    complete = AsyncMock()
    monkeypatch.setattr(llm_client, "chat_completion", complete)
    monkeypatch.setattr(
        chat_voice,
        "resolve_live_call_context",
        AsyncMock(return_value=("You are the current Agent.", [])),
    )
    monkeypatch.setattr(
        completions,
        "runtime_resolve_text_completion_route",
        AsyncMock(return_value=("provider/model", {}, True)),
    )

    @asynccontextmanager
    async def billing(**_kwargs):
        yield

    monkeypatch.setattr(ai_runtime, "runtime_llm_billing_context", billing)
    return complete


async def render():
    return await chat_voice.render_gateway_live_result(
        object(),
        chat_voice.CallStart(token="test"),
        ChatAudioScope("entity", "owner", conversation_id="conversation"),
        "Plan the afternoon.",
        VoiceAgentOutcome(status="ok", spoken_reply="Raw Main result with tool traces."),
    )


@pytest.mark.parametrize("content", ["", " \n ", None])
async def test_live_result_can_choose_silence(presenter, content):
    presenter.return_value = (content, {})
    assert await render() == ""


async def test_live_result_failure_never_falls_back_to_raw_main_text(presenter):
    presenter.return_value = ("", {"error": "provider unavailable"})
    with pytest.raises(RuntimeError, match="result presentation failed"):
        await render()


async def test_only_live_rendered_text_is_returned_for_speech(presenter):
    presenter.return_value = (" I planned a route along the coast. ", {})
    assert await render() == "I planned a route along the coast."
