"""Live-call cancellation at the shared speech providers' HTTP boundary."""

import asyncio
import io
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from apps.api import chat_audio
from packages.core.ai.tools import extended_tools
from packages.core.services.voice.speech_request import SpeechRequestState, speech_request_scope
from tests.test_browser_voice import eventually
from tests.test_gateway_voice import gateway_session, utterance


@pytest.mark.parametrize("stage", ["stt", "tts"])
@pytest.mark.parametrize("phase", ["preparation", "submitted"])
async def test_hangup_cancels_preparation_but_commits_submitted_speech(monkeypatch, stage, phase):
    call, ws = gateway_session()
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    db = AsyncMock()
    usage = AsyncMock()
    scope = chat_audio.ChatAudioScope("entity", "owner", conversation_id="conversation")

    async def model(*args, **kwargs):
        if phase == "preparation":
            entered.set()
            await release.wait()
        return "openai/whisper-1" if stage == "stt" else ("openai/tts-1", "voice")

    async def provider(request):
        requests.append(request.url.path)
        if phase == "submitted":
            entered.set()
            await release.wait()
        if stage == "stt":
            return httpx.Response(200, json={"text": "Hello", "duration": 1})
        return httpx.Response(200, content=b"audio", headers={"content-type": "audio/mpeg"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(provider), **kw)
    )
    monkeypatch.setattr(chat_audio, "record_media_usage", usage)
    if stage == "stt":
        monkeypatch.setattr(chat_audio, "inspect_upload_content", AsyncMock())
        monkeypatch.setattr(chat_audio, "resolve_model_for_user", model)
        monkeypatch.setattr(
            chat_audio, "resolve_llm_metadata_for_user", AsyncMock(return_value={"llm_api_key": "sk-test"})
        )

        async def transcribe(audio):
            upload = UploadFile(
                file=io.BytesIO(audio), filename="voice.wav", headers=Headers({"content-type": "audio/wav"})
            )
            return (await chat_audio.transcribe_chat_upload(db, scope, upload))["text"]

        call.transcribe.side_effect = transcribe
    else:
        monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", model)
        monkeypatch.setattr(
            extended_tools,
            "_resolve_user_media_credentials",
            AsyncMock(return_value=("sk-test", "https://api.openai.com/v1", True)),
        )

        async def speak(text, _voice):
            return (await chat_audio.chat_speech_response(db, scope, text)).body

        call.speak.side_effect = speak

    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    await ws.incoming.put({"type": "end"})
    await eventually(lambda: call.closed)
    if phase == "submitted":
        assert not task.done()
        release.set()
    await asyncio.wait_for(task, 2)
    release.set()
    assert len(requests) == (1 if phase == "submitted" else 0)
    assert usage.await_count == db.commit.await_count == len(requests)
    assert not any(e["type"] == "audio_clip" for e in ws.events)
    assert not call.pending_audio


@pytest.mark.parametrize("route", ["openrouter", "openai_fallback"])
async def test_hangup_prevents_speech_retry_or_endpoint_fallback(monkeypatch, route):
    call, ws = gateway_session()
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def provider(request):
        requests.append(request.url.path)
        entered.set()
        await release.wait()
        return httpx.Response(503 if route == "openrouter" else 404, text="endpoint not found")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(provider), **kw)
    )
    monkeypatch.setattr(extended_tools, "_audio_provider_retry_delay", lambda *_: 0)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", AsyncMock(return_value=("openai/tts-1", "voice")))
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_media_credentials",
        AsyncMock(
            return_value=(
                "sk-or-test" if route == "openrouter" else "sk-test",
                "https://openrouter.ai/api/v1" if route == "openrouter" else "https://api.openai.com/v1",
                True,
            )
        ),
    )
    usage = AsyncMock()
    monkeypatch.setattr(chat_audio, "record_media_usage", usage)

    async def speak(text, _voice):
        return (
            await chat_audio.chat_speech_response(AsyncMock(), chat_audio.ChatAudioScope("entity", "owner"), text)
        ).body

    call.speak.side_effect = speak
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    await ws.incoming.put({"type": "end"})
    await eventually(lambda: call.closed and ws.closed)
    release.set()
    await asyncio.wait_for(task, 2)
    assert len(requests) == 1
    usage.assert_not_awaited()
    assert not call.pending_audio


@pytest.mark.parametrize("provider", ["openrouter", "vercel", "openai", "google", "zyphra"])
async def test_each_speech_transport_rejects_a_stopped_call_before_http(monkeypatch, provider):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(500)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(transport), **kw)
    )
    arguments = dict(api_key="test", model="openai/tts-1", prompt="Hello", voice="alloy", audio_format="mp3")
    if provider == "openrouter":
        function = extended_tools._openrouter_speech_bytes
    elif provider == "vercel":
        function = extended_tools._vercel_speech_bytes
        arguments["base_url"] = "https://ai-gateway.vercel.sh/v1"
    elif provider == "openai":
        function = extended_tools._openai_compatible_speech_bytes
        arguments["base_url"] = "https://api.openai.com/v1"
    elif provider == "google":
        function = extended_tools._google_speech_bytes
        arguments.update(model="google/gemini-2.5-flash-preview-tts", voice="Kore")
        del arguments["audio_format"]
    else:
        function = extended_tools._zyphra_speech_bytes
        arguments["model"] = "zyphra/zonos-v0.1"
    with speech_request_scope(SpeechRequestState(stopped=True)):
        with pytest.raises(asyncio.CancelledError):
            await function(**arguments)
    assert not requests
