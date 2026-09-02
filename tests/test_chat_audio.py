"""Speech keeps chat authorization, tenant billing, and artifact boundaries."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers, UploadFile

from apps.api import chat_audio
from apps.api.routers import public_chat
from packages.core.ai.tools import extended_tools
from packages.core.database import get_db
from packages.core.services import agent_subscription_service


def upload(data=b"audio", mime="audio/webm", name="voice.webm"):
    return UploadFile(file=BytesIO(data), filename=name, headers=Headers({"content-type": mime}))


@pytest.fixture
def speech_services(monkeypatch):
    for name in ("inspect_upload_content", "record_media_usage"):
        monkeypatch.setattr(chat_audio, name, AsyncMock())
    monkeypatch.setattr(chat_audio, "resolve_model_for_user", AsyncMock(return_value="openai/whisper-1"))
    monkeypatch.setattr(chat_audio, "resolve_llm_metadata_for_user", AsyncMock(return_value={}))
    monkeypatch.setattr(chat_audio, "runtime_assert_credit_available", AsyncMock())
    monkeypatch.setattr(
        chat_audio,
        "transcribe_blob",
        AsyncMock(return_value=SimpleNamespace(text="Hello", duration_seconds=2, model="whisper-1")),
    )
    return ChatFixture(
        AsyncMock(), chat_audio.ChatAudioScope("business", "owner", "workspace", "conversation", "agent")
    )


class ChatFixture:
    def __init__(self, db, scope):
        self.db, self.scope = db, scope


@pytest.mark.parametrize("byok", [False, True])
async def test_transcription_uses_scoped_credentials_and_credit_gate(speech_services, byok):
    case = speech_services
    chat_audio.resolve_llm_metadata_for_user.return_value = (
        {"llm_api_key": "byok", "llm_base_url": "https://relay.example/v1"} if byok else {}
    )
    result = await chat_audio.transcribe_chat_upload(
        case.db, case.scope, upload(mime="audio/mp4", name="voice.m4a"), "zh"
    )
    assert result["text"] == "Hello"
    assert chat_audio.runtime_assert_credit_available.await_count == (0 if byok else 1)
    call = chat_audio.transcribe_blob.await_args.kwargs
    assert call["language"] == "zh"
    assert call["user_base_url"] == ("https://relay.example/v1" if byok else None)
    usage = chat_audio.record_media_usage.await_args.kwargs
    assert {key: usage[key] for key in vars(case.scope)} == vars(case.scope)
    assert usage["byok"] is byok


async def test_credit_exhaustion_stops_transcription_before_provider(speech_services):
    chat_audio.runtime_assert_credit_available.side_effect = HTTPException(402, "No credits")
    with pytest.raises(HTTPException, match="No credits"):
        await chat_audio.transcribe_chat_upload(speech_services.db, speech_services.scope, upload())
    chat_audio.transcribe_blob.assert_not_awaited()


async def test_speech_network_failure_is_actionable_without_exposing_provider_details(monkeypatch, speech_services):
    async def offline(*args, **kwargs):
        try:
            raise httpx.ConnectError("private upstream connection details")
        except httpx.ConnectError as exc:
            raise chat_audio.WhisperError("Transcription failed") from exc

    monkeypatch.setattr(chat_audio, "transcribe_blob", offline)
    with pytest.raises(HTTPException) as failure:
        await chat_audio.transcribe_chat_upload(speech_services.db, speech_services.scope, upload())
    assert failure.value.status_code == 503
    assert "server network" in failure.value.detail
    assert "private" not in failure.value.detail
    monkeypatch.setattr(
        chat_audio,
        "runtime_generate_audio_media",
        AsyncMock(
            return_value='{"status":"error","code":"audio_provider_unavailable","error":"private upstream details"}'
        ),
    )
    with pytest.raises(HTTPException) as failure:
        await chat_audio.chat_speech_response(speech_services.db, speech_services.scope, "Hello")
    assert failure.value.status_code == 503
    assert "server network" in failure.value.detail
    assert "private" not in failure.value.detail
    chat_audio.record_media_usage.assert_not_awaited()


@pytest.mark.parametrize(
    "data,mime,status",
    [(b"", "audio/webm", 400), (b"x", "text/plain", 400), (b"x" * (chat_audio.MAX_AUDIO_BYTES + 1), "audio/webm", 413)],
)
async def test_invalid_recording_never_calls_provider(speech_services, data, mime, status):
    with pytest.raises(HTTPException) as exc:
        await chat_audio.transcribe_chat_upload(speech_services.db, speech_services.scope, upload(data, mime))
    assert exc.value.status_code == status
    chat_audio.transcribe_blob.assert_not_awaited()


@pytest.mark.parametrize("byok", [False, True])
async def test_playback_reuses_provider_gate_and_billing_without_artifact(monkeypatch, speech_services, byok):
    case = speech_services
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", AsyncMock(return_value=("openai/tts-1", "voice")))
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_media_credentials",
        AsyncMock(return_value=("sk-test", "https://relay.example/v1", byok)),
    )
    monkeypatch.setattr(extended_tools, "_workspace_default_audio_language", AsyncMock(return_value="en-US"))
    monkeypatch.setattr(extended_tools, "runtime_assert_credit_available", AsyncMock())
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", AsyncMock(return_value=b"MP3-audio"))
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", AsyncMock())
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    response = await chat_audio.chat_speech_response(
        case.db,
        case.scope,
        "**Hello** [world](https://example.test)",
        voice_profile="deep",
    )
    assert response.body == b"MP3-audio"
    assert response.headers["cache-control"] == "private, no-store"
    assert extended_tools.runtime_assert_credit_available.await_count == (0 if byok else 1)
    assert extended_tools._openai_compatible_speech_bytes.await_args.kwargs["prompt"] == "Hello world"
    assert extended_tools._openai_compatible_speech_bytes.await_args.kwargs["voice"] == "onyx"
    extended_tools._save_generated_audio_bytes.assert_not_awaited()
    assert chat_audio.record_media_usage.await_args.kwargs["conversation_id"] == "conversation"
    if not byok:
        extended_tools.runtime_assert_credit_available.side_effect = HTTPException(402, "No credits")
        extended_tools._openai_compatible_speech_bytes.reset_mock()
        with pytest.raises(HTTPException):
            await chat_audio.chat_speech_response(case.db, case.scope, "Hello")
        extended_tools._openai_compatible_speech_bytes.assert_not_awaited()


def test_speech_language_uses_the_script_instead_of_an_english_workspace_default():
    assert chat_audio.speech_language("你好，我在这里。") == "zh-CN"
    assert chat_audio.speech_language("こんにちは") == "ja-JP"
    assert chat_audio.speech_language("안녕하세요") == "ko-KR"
    assert chat_audio.speech_language("Hello") is None


async def test_call_delivery_options_are_forwarded_without_changing_spoken_text(monkeypatch, speech_services):
    captured = {}

    async def generate(**kwargs):
        captured.update(kwargs)
        await kwargs["params"]["_deliver_audio"](
            audio_bytes=b"audio",
            audio_format="mp3",
            model="google/gemini-3.1-flash-tts-preview",
            is_byok=False,
        )
        return '{"status":"completed"}'

    monkeypatch.setattr(chat_audio, "runtime_generate_audio_media", generate)
    response = await chat_audio.chat_speech_response(
        speech_services.db,
        speech_services.scope,
        "你好",
        voice="Aoede",
        voice_profile="deep",
        voice_instructions="Warm and natural.",
        language="zh-CN",
    )
    assert response.body == b"audio"
    assert captured["prompt"] == "你好"
    assert captured["params"]["voice"] == "Aoede"
    assert captured["params"]["_call_voice_profile"] == "deep"
    assert captured["params"]["voice_instructions"] == "Warm and natural."
    assert captured["params"]["language"] == "zh-CN"


@pytest.fixture
async def public_audio(monkeypatch):
    db = AsyncMock()
    cc = SimpleNamespace(id="channel", entity_id="business", owner_user_id="owner", config={"login_required": False})
    binding = SimpleNamespace(user_id="owner", config={})
    contact = SimpleNamespace(id="contact", status="active", user_id=None, profile={}, display_name="Visitor")
    conv = SimpleNamespace(id="conversation", meta={})
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(cc, binding)))
    monkeypatch.setattr(public_chat, "_optional_current_user", AsyncMock(return_value=None))
    monkeypatch.setattr(public_chat, "find_public_webchat_conversation_by_session", AsyncMock(return_value=conv))
    monkeypatch.setattr(public_chat, "find_channel_session_contact", AsyncMock(return_value=contact))
    monkeypatch.setattr(public_chat, "channel_workspace_is_routable", AsyncMock(return_value=True))
    monkeypatch.setattr(
        agent_subscription_service,
        "resolve_subscription",
        AsyncMock(return_value=SimpleNamespace(source="channel", workspace_id="workspace", agent_id="agent")),
    )
    monkeypatch.setattr(
        public_chat,
        "list_public_webchat_messages",
        AsyncMock(return_value=[{"id": "reply", "role": "assistant", "content": "Hello world"}]),
    )
    monkeypatch.setattr(public_chat, "transcribe_chat_upload", AsyncMock(return_value={"text": "Hi"}))
    monkeypatch.setattr(public_chat, "enforce_public_audio_budget", AsyncMock())
    monkeypatch.setattr(
        public_chat, "chat_speech_response", AsyncMock(return_value=Response(b"audio", media_type="audio/mpeg"))
    )
    lease = SimpleNamespace(release=AsyncMock())
    monkeypatch.setattr(public_chat, "acquire_audio_lease", AsyncMock(return_value=lease))
    cached_audio = {}

    async def cache_get(key):
        return cached_audio.get(key)

    async def cache_set(key, value, ttl):
        cached_audio[key] = value
        return True

    monkeypatch.setattr(public_chat.cache, "get", cache_get)
    monkeypatch.setattr(public_chat.cache, "set", cache_set)
    app = FastAPI()
    app.include_router(public_chat.router)

    async def test_db():
        yield db

    app.dependency_overrides[get_db] = test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            cc=cc,
            contact=contact,
            lease=lease,
            cached_audio=cached_audio,
        )


async def public_request(case, operation):
    path = f"/api/v1/public/chat/token/audio/{operation}"
    if operation == "speech":
        return await case.client.post(
            path, json={"session_id": "session", "message_id": "reply", "offset": 6, "length": 5}
        )
    return await case.client.post(
        path, data={"session_id": "session"}, files={"file": ("voice.webm", b"audio", "audio/webm")}
    )


@pytest.mark.parametrize("operation", ["transcribe", "speech"])
@pytest.mark.parametrize(
    "boundary,status",
    [("login", 401), ("claimed", 403), ("blocked", 403), ("missing", 404), ("paused", 404), ("owner", 503)],
)
async def test_public_audio_fails_closed(public_audio, operation, boundary, status):
    case = public_audio
    if boundary == "login":
        case.cc.config["login_required"] = True
    if boundary == "claimed":
        case.contact.user_id = "someone-else"
    if boundary == "blocked":
        case.contact.status = "blocked"
    if boundary == "missing":
        public_chat.find_public_webchat_conversation_by_session.return_value = None
    if boundary == "paused":
        public_chat.channel_workspace_is_routable.return_value = False
    if boundary == "owner":
        case.cc.owner_user_id = None
    response = await public_request(case, operation)
    assert response.status_code == status
    public_chat.enforce_public_audio_budget.assert_awaited_once()
    public_chat.transcribe_chat_upload.assert_not_awaited()
    public_chat.chat_speech_response.assert_not_awaited()


@pytest.mark.parametrize("operation", ["transcribe", "speech"])
async def test_public_audio_bills_channel_owner_and_releases_lease(public_audio, operation):
    response = await public_request(public_audio, operation)
    assert response.status_code == 200
    function = public_chat.chat_speech_response if operation == "speech" else public_chat.transcribe_chat_upload
    scope = function.await_args.args[1]
    assert scope == chat_audio.ChatAudioScope("business", "owner", "workspace", "conversation", "agent")
    if operation == "speech":
        assert function.await_args.args[2] == "world"
        assert public_chat.list_public_webchat_messages.await_args.kwargs == {
            "session_id": "session",
            "message_ids": ["reply"],
        }
    public_audio.lease.release.assert_awaited_once()


async def test_public_speech_rejects_other_or_hidden_messages(public_audio):
    public_chat.list_public_webchat_messages.return_value = []
    assert (await public_request(public_audio, "speech")).status_code == 404
    public_chat.chat_speech_response.assert_not_awaited()


async def test_public_speech_rejects_unfinished_reply(public_audio):
    public_chat.list_public_webchat_messages.return_value[0]["stream_status"] = "running"
    assert (await public_request(public_audio, "speech")).status_code == 409
    public_chat.chat_speech_response.assert_not_awaited()


async def test_public_speech_reuses_the_same_generated_chunk(public_audio):
    first = await public_request(public_audio, "speech")
    second = await public_request(public_audio, "speech")

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b"audio"
    public_chat.chat_speech_response.assert_awaited_once()
    public_chat.acquire_audio_lease.assert_awaited_once()
    public_audio.lease.release.assert_awaited_once()
    assert next(iter(public_audio.cached_audio)).startswith("public-voice-speech:v1:")
    assert public_chat.enforce_public_audio_budget.await_count == 2


async def test_public_audio_budget_checks_hashed_session_and_ip(monkeypatch):
    limiter = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True, retry_after=0)))
    monkeypatch.setattr(chat_audio.enforce_public_audio_budget, "_limiters", (limiter,), raising=False)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("203.0.113.5", 1234),
        }
    )

    await chat_audio.enforce_public_audio_budget(request, "public-secret", "visitor-session", "call")

    keys = [call.args[0] for call in limiter.check.await_args_list]
    assert len(keys) == 4
    assert any(":session:" in key for key in keys)
    assert any(":ip:203.0.113.5:" in key for key in keys)
    assert all("public-secret" not in key and "visitor-session" not in key for key in keys)


async def test_public_audio_budget_returns_retry_after(monkeypatch):
    limiter = SimpleNamespace(
        check=AsyncMock(return_value=SimpleNamespace(allowed=False, retry_after=17))
    )
    monkeypatch.setattr(chat_audio.enforce_public_audio_budget, "_limiters", (limiter,), raising=False)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("203.0.113.5", 1234),
        }
    )

    with pytest.raises(HTTPException) as failure:
        await chat_audio.enforce_public_audio_budget(request, "public", "session", "speech")

    assert failure.value.status_code == 429
    assert failure.value.headers == {"Retry-After": "17"}


async def test_audio_conversation_cannot_override_workspace(monkeypatch):
    from apps.api.routers import chat
    from apps.api import deps

    monkeypatch.setattr(
        chat,
        "_get_accessible_conversation",
        AsyncMock(return_value=SimpleNamespace(workspace_id="actual", agent_id="agent")),
    )
    monkeypatch.setattr(deps, "require_workspace_writable", AsyncMock())
    user = SimpleNamespace(entity_id="business", id="owner")
    with pytest.raises(HTTPException) as exc:
        await chat_audio.authenticated_audio_scope(
            AsyncMock(), user, conversation_id="conversation", workspace_id="other"
        )
    assert exc.value.status_code == 404
    deps.require_workspace_writable.assert_not_awaited()
    scope = await chat_audio.authenticated_audio_scope(AsyncMock(), user, conversation_id="conversation")
    assert scope.workspace_id == "actual"
    deps.require_workspace_writable.assert_awaited_once()
