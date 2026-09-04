from __future__ import annotations

import base64
import io
import json
import struct
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from packages.core.ai.tools import extended_tools
from packages.core.ai.tools import generate_file_tool


def _test_pcm_wav(samples: list[int]) -> bytes:
    buffer = io.BytesIO()
    frames = struct.pack(f"<{len(samples)}h", *samples)
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(frames)
    return buffer.getvalue()


class _FakeHTTPStream:
    def __init__(
        self,
        payload=None,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
        chunks: list[bytes] | None = None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body if body is not None else json.dumps(payload).encode("utf-8")
        self.chunks = chunks
        self.iterated = False
        self.chunks_yielded = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_bytes(self):
        self.iterated = True
        for chunk in self.chunks if self.chunks is not None else [self.body]:
            self.chunks_yielded += 1
            yield chunk


def test_upload_text_document_alias_is_not_registered():
    from packages.core.ai.tools import document_tools

    names = [schema["function"]["name"] for schema, _ in document_tools.get_tools()]
    assert "upload_text_document" not in names
    assert "generate_document_file" in names


def test_generate_file_document_capability_mentions_editable_diagram_json():
    assert ".diagram.json" in generate_file_tool._CAPABILITIES["document"]
    assert "diagram" in generate_file_tool._CAPABILITIES
    assert "editable .diagram.json" in generate_file_tool._CAPABILITIES["diagram"]
    assert "code" in generate_file_tool._CAPABILITIES
    assert "multi-file" in generate_file_tool._CAPABILITIES["code"]


def test_generate_file_audio_schema_exposes_task_scoped_narrator_mode():
    from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA

    properties = GENERATE_FILE_SCHEMA["function"]["parameters"]["properties"]
    params_properties = properties["params"]["properties"]

    assert properties["narration_voice_mode"]["enum"] == [
        "random_per_task",
        "fixed_per_workspace",
    ]
    assert "Workspace settings.audio_defaults.language" in properties["language"]["description"]
    assert params_properties["workspace_asset_key"]["type"] == "string"
    assert params_properties["reuse_if_exists"]["type"] == "boolean"


def test_generate_file_forwards_task_scoped_narrator_mode_to_audio_runtime():
    from packages.core.ai.tools.generate_file.common import _merge_params

    assert _merge_params({"narration_voice_mode": "random_per_task"}) == {
        "narration_voice_mode": "random_per_task"
    }
    assert _merge_params({"narration_voice_mode": "fixed_per_workspace"}) == {
        "narration_voice_mode": "fixed_per_workspace"
    }
    assert _merge_params({"language": "zh-CN"}) == {"language": "zh-CN"}


def test_generate_image_schema_exposes_reusable_workspace_asset_contract():
    properties = extended_tools.GENERATE_IMAGE_SCHEMA["function"]["parameters"]["properties"]

    assert properties["aspect_ratio"]["enum"] == ["16:9", "9:16", "1:1"]
    assert properties["workspace_asset_key"]["type"] == "string"
    assert properties["reuse_if_exists"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_generate_image_reuses_workspace_asset_before_provider_resolution(monkeypatch):
    async def fake_existing(**kwargs):
        assert kwargs == {
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
            "asset_key": "stickman_character",
        }
        return {
            "kind": "image",
            "result_url": "/api/v1/fs/entity-1/Workspaces/stickman-character.png",
            "fs_path": "Workspaces/stickman-character.png",
            "prompt": "Canonical Stickman",
            "model": "openai/gpt-image-2",
            "size": "1024x1024",
        }

    async def provider_resolution_must_not_run(*_args, **_kwargs):
        raise AssertionError("A reusable Workspace asset must not start a paid provider call")

    monkeypatch.setattr(extended_tools, "_load_workspace_reusable_image", fake_existing)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_media_credentials",
        provider_resolution_must_not_run,
    )

    result = json.loads(
        await extended_tools._generate_image_handler(
            entity_id="entity-1",
            workspace_id="workspace-1",
            prompt="Create the canonical Stickman",
            workspace_asset_key="stickman_character",
            reuse_if_exists=True,
        )
    )

    assert result["reused_workspace_asset"] is True
    assert result["workspace_asset_key"] == "stickman_character"
    assert result["image_url"].endswith("stickman-character.png")


@pytest.mark.asyncio
async def test_generate_image_enforces_configured_workspace_character_identity(monkeypatch):
    async def fake_studio_profile(**kwargs):
        assert kwargs == {"entity_id": "entity-1", "workspace_id": "workspace-1"}
        return {
            "character_asset_key": "stickman_character",
            "character_asset_path": "brand/stickman-character.png",
        }

    async def fake_existing(**kwargs):
        assert kwargs["asset_key"] == "stickman_character"
        return {
            "kind": "image",
            "result_url": "/api/v1/fs/entity-1/Workspaces/brand/stickman-character.png",
            "fs_path": "Workspaces/brand/stickman-character.png",
            "prompt": "Canonical Stickman",
            "model": "openai/gpt-image-2",
            "size": "1024x1024",
        }

    async def provider_resolution_must_not_run(*_args, **_kwargs):
        raise AssertionError("Workspace identity must be reused before provider resolution")

    monkeypatch.setattr(extended_tools, "_workspace_stickman_studio_profile", fake_studio_profile)
    monkeypatch.setattr(extended_tools, "_load_workspace_reusable_image", fake_existing)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_media_credentials",
        provider_resolution_must_not_run,
    )

    result = json.loads(
        await extended_tools._generate_image_handler(
            entity_id="entity-1",
            workspace_id="workspace-1",
            prompt="Create the canonical Stickman",
            name="Workspaces/_by_id/folder-1/brand/stickman-character.png",
            workspace_asset_key="brand-stickman-character",
            reuse_if_exists=False,
        )
    )

    assert result["workspace_asset_key"] == "stickman_character"
    assert result["reused_workspace_asset"] is True


@pytest.mark.asyncio
async def test_vercel_speech_uses_gateway_v4_protocol_and_decodes_audio(monkeypatch):
    captured: dict = {}
    audio = base64.b64encode(b"gateway-audio").decode("ascii")

    class FakeResponse:
        status_code = 200
        text = ""
        headers = {"content-type": "application/json"}

        def json(self):
            return {"audio": audio, "warnings": []}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await extended_tools._vercel_speech_bytes(
        api_key="vck-test-gateway-key",
        base_url="https://ai-gateway.vercel.sh/v1",
        model="xai/grok-tts",
        prompt="Read this exactly.",
        voice="alloy",
        audio_format="mp3",
        voice_instructions="Warm and deliberate.",
    )

    assert result == b"gateway-audio"
    assert captured["url"] == "https://ai-gateway.vercel.sh/v4/ai/speech-model"
    assert captured["headers"]["ai-gateway-protocol-version"] == "0.0.1"
    assert captured["headers"]["ai-speech-model-specification-version"] == "4"
    assert captured["headers"]["ai-model-id"] == "xai/grok-tts"
    assert captured["json"] == {
        "text": "Read this exactly.",
        "voice": "alloy",
        "outputFormat": "mp3",
        "instructions": "Warm and deliberate.",
    }


def test_legacy_openai_tts_models_do_not_accept_delivery_instructions():
    assert extended_tools._speech_model_supports_instructions("openai/tts-1") is False
    assert extended_tools._speech_model_supports_instructions("openai/tts-1-hd") is False
    assert extended_tools._speech_model_supports_instructions("xai/grok-tts") is True


def test_vercel_speech_endpoint_accepts_sdk_v4_base_url():
    assert (
        extended_tools._vercel_speech_endpoint("https://ai-gateway.vercel.sh/v4/ai")
        == "https://ai-gateway.vercel.sh/v4/ai/speech-model"
    )


@pytest.mark.asyncio
async def test_managed_openai_tts_prefers_vercel_then_falls_back_to_openrouter(monkeypatch):
    calls: list[str] = []
    saved: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/tts-1", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "", "", False

    async def fake_route(model, **kwargs):
        provider = kwargs.get("gateway_provider")
        if provider == "openrouter":
            return type(
                "Route",
                (),
                {
                    "api_key": "sk-or-fallback",
                    "base_url": "https://openrouter.ai/api/v1",
                    "provider": "openrouter",
                },
            )()
        return type(
            "Route",
            (),
            {
                "api_key": "vck-gateway",
                "base_url": "https://ai-gateway.vercel.sh/v1",
                "provider": "vercel",
            },
        )()

    async def fake_vercel(**kwargs):
        calls.append("vercel")
        assert kwargs["api_key"] == "vck-gateway"
        raise RuntimeError("gateway unavailable")

    async def fake_openrouter(**kwargs):
        calls.append("openrouter")
        assert kwargs["api_key"] == "sk-or-fallback"
        return b"audio"

    async def fake_save(**kwargs):
        saved.update(kwargs)
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", fake_route)
    monkeypatch.setattr(extended_tools, "_vercel_speech_bytes", fake_vercel)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_openrouter)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
            response_format="mp3",
            agent_id="forged_agent",
            _agent_id_from_context="trusted_agent",
        )
    )

    assert calls == ["vercel", "openrouter"]
    assert result["status"] == "completed"
    assert result["provider"] == "openrouter"
    assert saved["is_byok"] is False
    assert saved["agent_id"] == "trusted_agent"


@pytest.mark.asyncio
async def test_managed_google_tts_skips_unsupported_vercel_speech_route(monkeypatch):
    calls: list[str] = []
    route_calls: list[str | None] = []
    saved: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "", "", False

    async def fake_platform_native_credential(provider):
        assert provider == "google"
        return "", ""

    async def fake_route(_model, **kwargs):
        route_calls.append(kwargs.get("gateway_provider"))
        if kwargs.get("gateway_provider") != "openrouter":
            return type(
                "Route",
                (),
                {
                    "api_key": "vck-gateway",
                    "base_url": "https://ai-gateway.vercel.sh/v1",
                    "provider": "vercel",
                    "source_detail": "AI_GATEWAY_API_KEY",
                },
            )()
        return type(
            "Route",
            (),
            {
                "api_key": "sk-or-openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "provider": "openrouter",
                "source_detail": "OPENROUTER_API_KEY",
            },
        )()

    async def fake_openrouter(**kwargs):
        calls.append("openrouter")
        assert kwargs["api_key"] == "sk-or-openrouter"
        assert kwargs["model"] == "google/gemini-3.1-flash-tts-preview"
        return b"\x01\x00\x02\x00"

    async def fake_save(**kwargs):
        saved.update(kwargs)
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_credential_async", fake_platform_native_credential)
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", fake_route)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_openrouter)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert calls == ["openrouter"]
    assert route_calls == ["openrouter"]
    assert result["status"] == "completed"
    assert result["provider"] == "openrouter"
    assert result["provider_response_format"] == "pcm"
    assert saved["audio_format"] == "wav"
    assert saved["audio_bytes"].startswith(b"RIFF")


@pytest.mark.asyncio
async def test_managed_google_tts_openrouter_route_wraps_pcm_as_wav(monkeypatch):
    calls: list[str] = []
    saved: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        return "", "", False

    async def fake_platform_native_credential(_provider):
        return "", ""

    async def fake_route(_model, **kwargs):
        if kwargs.get("gateway_provider") == "openrouter":
            return type(
                "Route",
                (),
                {
                    "api_key": "sk-or-fallback",
                    "base_url": "https://openrouter.ai/api/v1",
                    "provider": "openrouter",
                },
            )()
        return type(
            "Route",
            (),
            {
                "api_key": "vck-gateway",
                "base_url": "https://ai-gateway.vercel.sh/v1",
                "provider": "vercel",
            },
        )()

    async def fake_openrouter(**kwargs):
        calls.append("openrouter")
        assert kwargs["api_key"] == "sk-or-fallback"
        assert kwargs["audio_format"] == "pcm"
        return b"\x01\x00\x02\x00"

    async def fake_save(**kwargs):
        saved.update(kwargs)
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_credential_async", fake_platform_native_credential)
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", fake_route)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_openrouter)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert calls == ["openrouter"]
    assert result["status"] == "completed"
    assert result["provider"] == "openrouter"
    assert saved["audio_format"] == "wav"
    assert saved["audio_bytes"].startswith(b"RIFF")


@pytest.mark.asyncio
async def test_task_narrator_profile_is_persisted_and_reused(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task

    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Produce daily Stickman video",
    )
    db_session.add(task)
    await db_session.commit()

    first_profile = await extended_tools._resolve_task_narrator_profile(
        entity_id=entity_id,
        task_id=task.id,
        candidate_model="google/gemini-3.1-flash-tts-preview",
        voice_instructions="Warm, deliberate delivery.",
    )
    reused_profile = await extended_tools._resolve_task_narrator_profile(
        entity_id=entity_id,
        task_id=task.id,
        candidate_model="openai/gpt-4o-mini-tts",
        voice_instructions="A different instruction must not replace the profile.",
    )

    assert first_profile["version"] == 1
    assert first_profile["provider"] == "google"
    assert first_profile["model"] == "google/gemini-3.1-flash-tts-preview"
    assert first_profile["voice"]
    assert first_profile["voice"] != "random"
    assert first_profile["voice_instructions"] == "Warm, deliberate delivery."
    assert reused_profile == first_profile

    await db_session.refresh(task)
    assert task.details["stickman_narrator_profile"] == first_profile


@pytest.mark.asyncio
async def test_workspace_narrator_profile_is_persisted_and_reused_across_tasks(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Stickman Studio",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()

    first_profile = await extended_tools._resolve_workspace_narrator_profile(
        entity_id=entity_id,
        workspace_id=workspace.id,
        candidate_model="openai/tts-1-hd",
        voice_instructions="Friendly, concise, steady pace.",
        preferred_voice="alloy",
    )
    reused_profile = await extended_tools._resolve_workspace_narrator_profile(
        entity_id=entity_id,
        workspace_id=workspace.id,
        candidate_model="google/gemini-3.1-flash-tts-preview",
        voice_instructions="This later task must not replace the Workspace profile.",
        preferred_voice="Puck",
    )

    assert first_profile == {
        "version": 1,
        "provider": "openai",
        "model": "openai/tts-1-hd",
        "voice": "alloy",
        "voice_instructions": "Friendly, concise, steady pace.",
    }
    assert reused_profile == first_profile

    await db_session.refresh(workspace)
    assert workspace.settings["stickman_narrator_profile"] == first_profile


@pytest.mark.asyncio
async def test_media_task_user_resolution_uses_scoped_task_requester(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.user import User
    from packages.core.services.task_requester_identity import TaskRequesterIdentityError

    entity_id = generate_ulid()
    user_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add_all(
        [
            User(
                id=user_id,
                entity_id=entity_id,
                email=f"{user_id}@test.local",
                display_name="Media Task Requester",
                password_hash="test-hash",
                role="member",
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                creator_id=user_id,
                title="Generate scoped media",
            ),
        ]
    )
    await db_session.commit()

    resolved_user_id = await extended_tools._resolve_media_task_user_id(
        "untrusted-runtime-user",
        entity_id,
        task_id,
    )

    assert resolved_user_id == user_id
    with pytest.raises(TaskRequesterIdentityError, match="does not exist in the requested entity scope"):
        await extended_tools._resolve_media_task_user_id(
            "untrusted-runtime-user",
            generate_ulid(),
            task_id,
        )


@pytest.mark.asyncio
async def test_workspace_studio_policy_forces_fixed_narration_when_model_omits_mode(monkeypatch):
    profile = {
        "version": 1,
        "provider": "openai",
        "model": "openai/tts-1-hd",
        "voice": "alloy",
        "voice_instructions": "Calm and clear.",
    }
    resolved: list[dict] = []
    speech_requests: list[dict] = []
    saved_audio: list[dict] = []
    resolve_task_user = AsyncMock(return_value="user")

    async def fake_studio_profile(**_kwargs):
        return {"narration_voice_mode": "fixed_per_workspace"}

    async def fake_audio_language(**kwargs):
        assert kwargs == {"entity_id": "entity", "workspace_id": "workspace-1"}
        return "zh-CN"

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return profile["model"], "voice"

    async def fake_workspace_profile(**kwargs):
        resolved.append(kwargs)
        return profile

    async def task_profile_must_not_run(**_kwargs):
        raise AssertionError("Workspace policy must override task-scoped narration")

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-test", "", False

    async def fake_speech_bytes(**kwargs):
        speech_requests.append(kwargs)
        return b"\x01\x00\x02\x00"

    async def fake_save_audio(**kwargs):
        saved_audio.append(kwargs)
        return "/api/v1/fs/entity/audio/scene-01.wav"

    async def fake_primary_credentials(_user_id, _entity_id, *, provider):
        assert provider == "openai"
        return "", "", False

    async def fake_platform_credential(_provider):
        return "", ""

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", resolve_task_user)
    monkeypatch.setattr(extended_tools, "_workspace_stickman_studio_profile", fake_studio_profile)
    monkeypatch.setattr(extended_tools, "_workspace_default_audio_language", fake_audio_language)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_workspace_narrator_profile", fake_workspace_profile)
    monkeypatch.setattr(extended_tools, "_resolve_task_narrator_profile", task_profile_must_not_run)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_resolve_primary_byok_media_credentials", fake_primary_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_credential_async", fake_platform_credential)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            workspace_id="workspace-1",
            task_id="task-1",
            prompt="One calm breath.",
            purpose="narration",
            narration_voice_mode="random_per_task",
        )
    )

    assert resolved and resolved[0]["workspace_id"] == "workspace-1"
    assert result["narration_profile"] == profile
    assert result["language"] == "zh-CN"
    assert speech_requests[0]["voice_instructions"] == "Speak in zh-CN. Calm and clear."
    assert saved_audio[0]["language"] == "zh-CN"
    resolve_task_user.assert_awaited_once_with("user", "entity", "task-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_model", "provider", "voice"),
    [
        ("google/gemini-2.5-pro", "google", "Puck"),
        ("openai/gpt-4.1", "openai", "alloy"),
    ],
)
async def test_task_narrator_profile_rejects_non_tts_models(
    db_session,
    candidate_model,
    provider,
    voice,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task

    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Produce daily Stickman video",
    )
    db_session.add(task)
    await db_session.commit()

    with pytest.raises(extended_tools._NarrationProfileError) as exc_info:
        await extended_tools._resolve_task_narrator_profile(
            entity_id=entity_id,
            task_id=task.id,
            candidate_model=candidate_model,
            voice_instructions="",
        )

    assert exc_info.value.code.value == "narration_voice_profile_unsupported"
    await db_session.refresh(task)
    assert "stickman_narrator_profile" not in (task.details or {})


@pytest.mark.asyncio
async def test_random_per_task_narration_uses_the_persisted_profile_for_every_segment(monkeypatch):
    profile = {
        "version": 1,
        "provider": "google",
        "model": "google/gemini-3.1-flash-tts-preview",
        "voice": "Puck",
        "voice_instructions": "Warm, deliberate delivery.",
    }
    requests: list[dict] = []
    saved: list[dict] = []

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_resolve_profile(**kwargs):
        assert kwargs["task_id"] == "task-1"
        return profile

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-test", "", False

    async def fake_speech_bytes(**kwargs):
        requests.append(kwargs)
        return b"\x01\x00\x02\x00"

    async def fake_save_audio(**kwargs):
        saved.append(kwargs)
        return "/api/v1/fs/entity/audio/puck/segment.wav"

    async def fake_platform_credential(_provider):
        return "", ""

    async def fake_primary_credentials(_user_id, _entity_id, *, provider):
        assert provider == "google"
        return "", "", False

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_task_narrator_profile", fake_resolve_profile)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_resolve_primary_byok_media_credentials", fake_primary_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_credential_async", fake_platform_credential)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    results = [
        json.loads(
            await extended_tools._generate_audio_handler(
                entity_id="",
                user_id="user",
                task_id="task-1",
                prompt=clause,
                name=f"segment-{index}.wav",
                purpose="narration",
                narration_voice_mode="random_per_task",
            )
        )
        for index, clause in enumerate(("First clause.", "Second clause."), start=1)
    ]

    assert [request["model"] for request in requests] == [profile["model"], profile["model"]]
    assert [request["voice"] for request in requests] == [profile["voice"], profile["voice"]]
    assert [result["narration_profile"] for result in results] == [profile, profile]
    assert [item["output_name"] for item in saved] == [
        "audio/puck/segment-1.wav",
        "audio/puck/segment-2.wav",
    ]
    assert [item["voice"] for item in saved] == [profile["voice"], profile["voice"]]
    assert [item["narration_profile"] for item in saved] == [profile, profile]


@pytest.mark.asyncio
async def test_random_per_task_narration_never_falls_back_to_a_different_model(monkeypatch):
    profile = {
        "version": 1,
        "provider": "google",
        "model": "google/gemini-3.1-flash-tts-preview",
        "voice": "Puck",
        "voice_instructions": "",
    }
    called_models: list[str] = []

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_resolve_profile(**_kwargs):
        return profile

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-test", "", False

    async def fake_primary_credentials(_user_id, _entity_id, *, provider):
        assert provider == "google"
        return "", "", False

    async def fake_platform_credential(_provider):
        return "", ""

    async def fake_speech_bytes(**kwargs):
        called_models.append(kwargs["model"])
        if kwargs["model"] == profile["model"]:
            raise extended_tools._AudioProviderUnavailable(
                provider="openrouter",
                status_code=503,
                attempts=3,
                detail="upstream unavailable",
            )
        return b"\x01\x00\x02\x00"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_task_narrator_profile", fake_resolve_profile)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_resolve_primary_byok_media_credentials", fake_primary_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_credential_async", fake_platform_credential)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            task_id="task-1",
            prompt="Narrate this clause.",
            purpose="narration",
            narration_voice_mode="random_per_task",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "audio_provider_unavailable"
    assert result["model"] == profile["model"]
    assert called_models == [profile["model"]]


@pytest.mark.asyncio
async def test_random_per_task_narration_does_not_fall_back_to_openai_chat_audio(monkeypatch):
    profile = {
        "version": 1,
        "provider": "openai",
        "model": "openai/gpt-4o-mini-tts",
        "voice": "alloy",
        "voice_instructions": "",
    }
    calls = {"chat": 0, "save": 0}
    resolve_task_user = AsyncMock(return_value="user")

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return profile["model"], "voice"

    async def fake_resolve_profile(**_kwargs):
        return profile

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-native-key", "", True

    async def fake_speech_bytes(**kwargs):
        assert kwargs["model"] == profile["model"]
        raise extended_tools._OpenAICompatibleSpeechEndpointUnavailable(
            "OpenAI-compatible speech generation failed (404): Not Found"
        )

    async def fake_chat_audio_bytes(**_kwargs):  # pragma: no cover - should not be called
        calls["chat"] += 1
        raise AssertionError("Task narrator profiles must not switch to chat audio")

    async def fake_save_audio(**_kwargs):  # pragma: no cover - should not be called
        calls["save"] += 1
        return "/api/v1/fs/entity/audio/alloy/segment.wav"

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", resolve_task_user)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_task_narrator_profile", fake_resolve_profile)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_speech_bytes)
    monkeypatch.setattr(
        extended_tools,
        "_openai_compatible_chat_audio_bytes",
        fake_chat_audio_bytes,
    )
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            task_id="task-1",
            prompt="Narrate this clause.",
            purpose="narration",
            narration_voice_mode="random_per_task",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "provider_blocker"
    assert result["model"] == profile["model"]
    assert calls == {"chat": 0, "save": 0}
    resolve_task_user.assert_awaited_once_with("user", "entity", "task-1")


def test_sesame_tts_uses_an_explicit_openrouter_voice():
    assert extended_tools._default_openrouter_voice("sesame/csm-1b") == "alloy"


@pytest.mark.asyncio
async def test_sesame_catalog_model_uses_saved_openrouter_byok_at_runtime(monkeypatch):
    captured: dict = {}

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-user-key", "https://openrouter.ai/api/v1", True

    async def fake_speech(**kwargs):
        captured.update(kwargs)
        return b"ID3\x04sesame"

    async def fake_save(**kwargs):
        captured["saved"] = kwargs
        return "/api/v1/fs/entity/audio/sesame.mp3"

    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Hello from Sesame",
            purpose="narration",
            model="sesame/csm-1b",
        )
    )

    assert result["status"] == "completed"
    assert result["provider"] == "openrouter"
    assert result["model"] == "sesame/csm-1b"
    assert captured["api_key"] == "sk-or-user-key"
    assert captured["model"] == "sesame/csm-1b"
    assert captured["saved"]["is_byok"] is True


def test_kokoro_and_zonos_tts_use_supported_openrouter_voices():
    assert extended_tools._default_openrouter_voice("hexgrad/kokoro-82m") == "alloy"
    assert (
        extended_tools._default_openrouter_voice("zyphra/zonos-v0.1-hybrid")
        == "american_female"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    ["zyphra/zonos-v0.1-hybrid", "zyphra/zonos-v0.1-transformer"],
)
async def test_original_zyphra_catalog_models_use_native_speech_api(monkeypatch, model):
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""
        content = b"ID3\x04zyphra-audio"
        headers = {"content-type": "audio/mpeg"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    audio_bytes = await extended_tools._zyphra_speech_bytes(
        api_key="zyphra-user-key",
        model=model,
        prompt="Hello!",
        voice="american_female",
        audio_format="mp3",
    )

    assert audio_bytes == b"ID3\x04zyphra-audio"
    assert captured == {
        "url": "https://api.zyphracloud.com/api/v1/audio/speech",
        "headers": {
            "Authorization": "Bearer zyphra-user-key",
            "Content-Type": "application/json",
        },
        "json": {
            "input": "Hello!",
            "model": model,
            "response_format": "mp3",
            "voice": "american_female",
        },
    }


@pytest.mark.asyncio
async def test_generated_image_save_reuses_existing_knowledge_document(
    db_session,
    monkeypatch,
    tmp_path,
):
    from sqlalchemy import select

    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.workspace import Workspace

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"

    async def fake_bill_media(**_kwargs):
        return None

    monkeypatch.setattr(extended_tools, "_bill_media", fake_bill_media)

    try:
        entity_id = generate_ulid()
        workspace_id = generate_ulid()
        document_id = generate_ulid()

        workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Image Workspace")
        db_session.add(workspace)
        await db_session.flush()
        from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
        folder = await ensure_workspace_artifact_folder(db_session, workspace)
        expected_path = f"Workspaces/_by_id/{folder.id}/images/hero.png"
        db_session.add(
            Document(
                id=document_id,
                entity_id=entity_id,
                name="hero.png",
                fs_path=expected_path,
                file_type="png",
                mime_type="image/png",
                source="filesystem_reconcile",
            )
        )
        await db_session.commit()

        image_url = await extended_tools._save_generated_image_bytes(
            entity_id=entity_id,
            user_id="user_1",
            prompt="Hero product image",
            model="gpt-image-1",
            size="1024x1024",
            image_bytes=b"image-bytes",
            mime="image/png",
            is_byok=True,
            output_name="hero.png",
            workspace_id=workspace_id,
            task_id="task_1",
            agent_id="agent_1",
            conversation_id="conv_1",
        )

        assert image_url == f"/api/v1/fs/{entity_id}/{expected_path}"
        db_session.expire_all()
        docs = list(
            (
                await db_session.execute(
                    select(Document).where(
                        Document.entity_id == entity_id,
                        Document.fs_path == expected_path,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [doc.id for doc in docs] == [document_id]
        assert docs[0].source == "ai_generated"
        assert docs[0].file_size == len(b"image-bytes")
        assert docs[0].metadata_["origin"]["workspace_id"] == workspace_id
        assert docs[0].metadata_["generation"]["model"] == "gpt-image-1"
        from packages.core.services.workspace_artifacts import resolve_workspace_folder_binding

        binding = await resolve_workspace_folder_binding(
            db_session,
            entity_id=entity_id,
            folder_id=docs[0].folder_id,
        )
        assert binding is not None
        assert binding.workspace_id == workspace_id
        assert binding.relative_parts == ("images",)
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_generated_audio_save_records_actual_task_narrator_profile(
    db_session,
    monkeypatch,
    tmp_path,
):
    from sqlalchemy import select

    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
        resolve_workspace_folder_binding,
    )

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"

    async def fake_bill_media(**_kwargs):
        return None

    monkeypatch.setattr(extended_tools, "_bill_media", fake_bill_media)

    try:
        entity_id = generate_ulid()
        workspace_id = generate_ulid()
        workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Audio Workspace")
        db_session.add(workspace)
        await db_session.flush()
        folder = await ensure_workspace_artifact_folder(db_session, workspace)
        await db_session.commit()
        narrator_profile = {
            "version": 1,
            "provider": "google",
            "model": "google/gemini-3.1-flash-tts-preview",
            "voice": "Puck",
            "voice_instructions": "Warm, deliberate delivery.",
        }
        expected_path = f"Workspaces/_by_id/{folder.id}/audio/puck/narration.wav"

        audio_url = await extended_tools._save_generated_audio_bytes(
            entity_id=entity_id,
            user_id="user_1",
            prompt="Verbatim narration",
            model="configured-audio-model",
            purpose="narration",
            audio_bytes=_test_pcm_wav([0, 1200, -1200, 600, -600]),
            audio_format="wav",
            is_byok=True,
            voice="Puck",
            voice_instructions="Warm, deliberate delivery.",
            narration_profile=narrator_profile,
            output_name="audio/puck/narration.wav",
            workspace_id=workspace_id,
            task_id="task_1",
            agent_id="agent_1",
            conversation_id="conv_1",
        )

        assert audio_url == f"/api/v1/fs/{entity_id}/{expected_path}"
        document = (
            await db_session.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.fs_path == expected_path,
                )
            )
        ).scalar_one()
        binding = await resolve_workspace_folder_binding(
            db_session,
            entity_id=entity_id,
            folder_id=document.folder_id,
        )
        assert binding is not None
        assert binding.workspace_id == workspace_id
        assert binding.relative_parts == ("audio", "puck")
        assert document.metadata_["generation"]["voice"] == "Puck"
        assert document.metadata_["generation"]["narration_profile"] == narrator_profile
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_generate_file_document_with_files_routes_to_code_bundle(monkeypatch):
    from packages.core.ai.tools.generate_file import tool as generate_file_router

    captured: dict = {}

    async def fake_handle_code(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True, "kind": "code"})

    monkeypatch.setattr(generate_file_router, "handle_code", fake_handle_code)

    result = json.loads(
        await generate_file_tool._generate_file_handler(
            entity_id="entity",
            user_id="user",
            conversation_id="conversation",
            kind="document",
            name="demo-site",
            params={
                "entry": "index.html",
                "files": [{"path": "index.html", "content": "<!doctype html>"}],
            },
        )
    )

    assert result == {"created": True, "kind": "code"}
    assert captured["name"] == "demo-site"
    assert captured["params"]["files"][0]["path"] == "index.html"


@pytest.mark.asyncio
async def test_generate_file_accepts_json_string_params(monkeypatch):
    from packages.core.ai.tools.generate_file import tool as generate_file_router

    captured: dict = {}

    async def fake_handle_code(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True, "kind": "code"})

    monkeypatch.setattr(generate_file_router, "handle_code", fake_handle_code)

    result = json.loads(
        await generate_file_tool._generate_file_handler(
            entity_id="entity",
            user_id="user",
            conversation_id="conversation",
            kind="document",
            name="demo-site",
            params=json.dumps(
                {
                    "entry": "index.html",
                    "files": [{"path": "index.html", "content": "<!doctype html>"}],
                }
            ),
        )
    )

    assert result == {"created": True, "kind": "code"}
    assert captured["params"]["entry"] == "index.html"
    assert captured["params"]["files"][0]["path"] == "index.html"


@pytest.mark.asyncio
async def test_generate_file_creates_code_bundle_with_real_file_structure(tmp_path, monkeypatch):
    from packages.core.config import get_settings

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)

    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.guard_ai_file_mutation",
        AsyncMock(return_value=None),
    )

    synced_paths: list[str] = []

    async def fake_sync_file_to_knowledge(**kwargs):
        synced_paths.append(kwargs["abs_path"])
        return SimpleNamespace(synced=True, document_id=f"doc_{len(synced_paths)}", reason=None)

    async def fake_scope_workspace_output_name(**kwargs):
        assert kwargs["default_subdir"] == "code"
        return f"Workspaces/Demo/code/{kwargs['name']}"

    monkeypatch.setattr(
        "packages.core.services.knowledge_sync.sync_file_to_knowledge",
        fake_sync_file_to_knowledge,
    )
    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope_workspace_output_name)

    try:
        result = json.loads(
            await generate_file_tool._generate_file_handler(
                entity_id="entity",
                user_id="user",
                conversation_id="conversation",
                workspace_id="ws_123",
                kind="code",
                name="rental-website",
                prompt="Create a rental website",
                params={
                    "entry": "index.html",
                    "files": [
                        {"path": "index.html", "content": "<!doctype html><link rel='stylesheet' href='styles.css'>"},
                        {"path": "styles.css", "content": "body { color: #123; }"},
                        {"path": "app.js", "content": "console.log('ready');"},
                    ],
                },
            )
        )
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root

    assert result["created"] is True
    assert result["bundle_path"] == "Workspaces/Demo/code/rental-website"
    assert result["entry"] == "Workspaces/Demo/code/rental-website/index.html"
    assert [file["path"] for file in result["files"]] == [
        "Workspaces/Demo/code/rental-website/index.html",
        "Workspaces/Demo/code/rental-website/styles.css",
        "Workspaces/Demo/code/rental-website/app.js",
    ]
    assert (tmp_path / "entity/Workspaces/Demo/code/rental-website/index.html").exists()
    assert (tmp_path / "entity/Workspaces/Demo/code/rental-website/styles.css").exists()
    assert (tmp_path / "entity/Workspaces/Demo/code/rental-website/app.js").exists()
    assert not (tmp_path / "entity/Workspaces/Demo/code/rental-website/style.txt").exists()
    assert len(synced_paths) == 3


@pytest.mark.asyncio
async def test_generate_file_rolls_back_entire_code_bundle_when_projection_fails(
    tmp_path,
    monkeypatch,
):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.guard_ai_file_mutation",
        AsyncMock(return_value=None),
    )

    bundle_dir = tmp_path / "entity" / "Workspaces" / "Demo" / "code" / "demo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "index.html").write_text("old index", encoding="utf-8")
    sync_count = 0

    async def fail_second_projection(**_kwargs):
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            return SimpleNamespace(
                synced=False,
                document_id=None,
                reason="storage_limit",
            )
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
        )

    async def fake_scope_workspace_output_name(**_kwargs):
        return "Workspaces/Demo/code/demo"

    monkeypatch.setattr(
        "packages.core.services.knowledge_sync.sync_file_to_knowledge",
        fail_second_projection,
    )
    monkeypatch.setattr(
        generate_file_tool,
        "_scope_workspace_output_name",
        fake_scope_workspace_output_name,
    )

    result = json.loads(await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        kind="code",
        name="demo",
        params={
            "entry": "index.html",
            "files": [
                {"path": "index.html", "content": "new index"},
                {"path": "styles.css", "content": "body { color: red; }"},
            ],
        },
    ))

    assert result["created"] is False
    assert result["knowledge_sync_reason"] == "storage_limit"
    assert (bundle_dir / "index.html").read_text(encoding="utf-8") == "old index"
    assert not (bundle_dir / "styles.css").exists()
    assert not list(bundle_dir.glob(".*.tmp-*"))


@pytest.mark.asyncio
async def test_code_bundle_projection_failure_rolls_back_document_and_folder_rows(
    db_session,
    tmp_path,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.ai.runtime import file_actions
    from packages.core.ai.tools.generate_file import code as code_tool
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.event import EventLog
    from packages.core.services.knowledge_sync import KnowledgeSyncResult

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.guard_ai_file_mutation",
        AsyncMock(return_value=None),
    )

    entity_id = generate_ulid()
    bundle_dir = tmp_path / entity_id / "code" / "demo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "main.py").write_text("old code", encoding="utf-8")
    original_sync = file_actions.runtime_sync_entity_file_to_knowledge
    sync_count = 0

    async def fail_after_first_real_projection(**kwargs):
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            return KnowledgeSyncResult(False, reason="storage_limit")
        return await original_sync(**kwargs)

    async def fixed_bundle_name(**_kwargs):
        return "demo"

    monkeypatch.setattr(
        file_actions,
        "runtime_sync_entity_file_to_knowledge",
        fail_after_first_real_projection,
    )
    monkeypatch.setattr(
        code_tool.common,
        "_scope_workspace_output_name",
        fixed_bundle_name,
    )

    result = json.loads(await code_tool.handle_code(
        entity_id=entity_id,
        user_id="user_1",
        conversation_id="conversation_1",
        prompt="Build demo",
        name="demo",
        params={
            "files": [
                {"path": "main.py", "content": "print('new')"},
                {"path": "helper.py", "content": "VALUE = 1"},
            ],
        },
        kwargs={},
        agent_id=None,
    ))

    assert result["knowledge_sync_reason"] == "storage_limit"
    assert (bundle_dir / "main.py").read_text(encoding="utf-8") == "old code"
    assert not (bundle_dir / "helper.py").exists()
    documents = list((await db_session.scalars(
        select(Document).where(Document.entity_id == entity_id),
    )).all())
    folders = list((await db_session.scalars(
        select(DocumentFolder).where(DocumentFolder.entity_id == entity_id),
    )).all())
    events = list((await db_session.scalars(
        select(EventLog).where(
            EventLog.entity_id == entity_id,
            EventLog.event_type == "document.uploaded",
        ),
    )).all())
    assert documents == []
    assert folders == []
    assert events == []


@pytest.mark.asyncio
async def test_generate_file_creates_product_video_status_bundle_in_workspace(
    db_session,
    tmp_path,
    monkeypatch,
):
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
    )

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Product Video Studio",
    )
    db_session.add(workspace)
    await db_session.flush()
    folder = await ensure_workspace_artifact_folder(db_session, workspace)
    await db_session.commit()

    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.guard_ai_file_mutation",
        AsyncMock(return_value=None),
    )
    synced_paths: list[str] = []

    async def fake_sync_file_to_knowledge(**kwargs):
        synced_paths.append(kwargs["abs_path"])
        return SimpleNamespace(
            synced=True,
            document_id=f"doc_{len(synced_paths)}",
            reason=None,
        )

    monkeypatch.setattr(
        "packages.core.services.knowledge_sync.sync_file_to_knowledge",
        fake_sync_file_to_knowledge,
    )

    try:
        result = json.loads(
            await generate_file_tool._generate_file_handler(
                entity_id=entity_id,
                user_id="user",
                conversation_id="conversation",
                workspace_id=workspace.id,
                kind="code",
                name="Product Videos/product-video-project-1",
                params={
                    "entry": "00-project-overview.md",
                    "files": [
                        {
                            "path": "00-project-overview.md",
                            "content": "# Project status",
                        },
                        {
                            "path": "technical/run-state.json",
                            "content": {"phase": "discovery"},
                        },
                        {
                            "path": "technical/discovery-report.json",
                            "content": {"ready_for_planning": False},
                        },
                    ],
                },
            )
        )
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root

    expected_bundle = (
        f"Workspaces/_by_id/{folder.id}/"
        "Product Videos/product-video-project-1"
    )
    assert result["bundle_path"] == expected_bundle
    assert [item["path"] for item in result["files"]] == [
        f"{expected_bundle}/00-project-overview.md",
        f"{expected_bundle}/technical/run-state.json",
        f"{expected_bundle}/technical/discovery-report.json",
    ]
    assert len(synced_paths) == 3


def test_generate_image_never_crops_the_model_output():
    """A generated image is a composition, not a texture.

    This used to center-crop whatever came back to force the requested ratio:
    a 1024x1024 poster requested as 9:16 became 576x1024 with the headline
    and side labels sliced off both edges, and nothing said so. Cropping is
    still forbidden — the requested ratio is now reached by padding, so every
    pixel survives and the frame grows around it. See
    tests/test_image_aspect_pads_never_crops.py for the full rule.
    """
    assert extended_tools._image_size_for_aspect_ratio("16:9") == "1536x1024"
    assert extended_tools._image_size_for_aspect_ratio("9:16") == "1024x1536"
    assert extended_tools._image_size_for_aspect_ratio("16:9", "1024x1024") == "1024x1024"

    import io
    from PIL import Image

    source = Image.new("RGB", (1024, 1024), "red")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    original = buffer.getvalue()

    for ratio in ("16:9", "9:16", "1:1", ""):
        delivered, mime, size = extended_tools._normalize_image_bytes_for_aspect_ratio(
            original, "image/png", ratio,
        )
        assert mime == "image/png"
        width, height = (int(part) for part in size.split("x"))
        assert width >= 1024 and height >= 1024, (
            f"{ratio!r} made the picture smaller — that is a crop"
        )
        if ratio in ("1:1", ""):
            # already square, or nothing requested: nothing to do at all
            assert delivered == original, f"{ratio!r} re-encoded an image it need not touch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "api_key", "base_url", "expected_url", "expected_wire_model"),
    [
        (
            "openai/gpt-image-2",
            "sk-openai-user-key",
            "https://api.openai.com/v1",
            "https://api.openai.com/v1/images/generations",
            "gpt-image-2",
        ),
        (
            "google/gemini-3.1-flash-image-preview",
            "AIza-google-user-key",
            "https://generativelanguage.googleapis.com/v1beta",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent",
            "gemini-3.1-flash-image-preview",
        ),
    ],
)
async def test_image_catalog_ids_match_provider_wire_ids_at_runtime(
    monkeypatch, model, api_key, base_url, expected_url, expected_wire_model,
):
    captured: dict = {}
    image_data = base64.b64encode(b"provider-image").decode("ascii")

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "image"
        return api_key, base_url, True

    async def fake_model(*_args, **_kwargs):
        return model

    async def fake_save(**kwargs):
        captured["saved"] = kwargs
        return "/api/v1/fs/entity/images/generated.png"

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            if model.startswith("google/"):
                return {
                    "candidates": [{
                        "content": {"parts": [{"inlineData": {"data": image_data, "mimeType": "image/png"}}]}
                    }]
                }
            return {"data": [{"b64_json": image_data}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, request=kwargs)
            return FakeResponse()

    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_resolve_user_image_model", fake_model)
    monkeypatch.setattr(extended_tools, "_save_generated_image_bytes", fake_save)
    monkeypatch.setattr(
        extended_tools,
        "_normalize_image_bytes_for_aspect_ratio",
        lambda image_bytes, mime, _ratio: (image_bytes, mime, "1024x1024"),
    )
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = json.loads(
        await extended_tools._generate_image_handler(
            entity_id="",
            user_id="user",
            prompt="Product image",
            model=model,
        )
    )

    assert "error" not in result
    assert result["model"] == model
    assert captured["url"] == expected_url
    if model.startswith("google/"):
        assert expected_wire_model in captured["url"]
    else:
        assert captured["request"]["json"]["model"] == expected_wire_model
    assert captured["saved"]["model"] == model
    assert captured["saved"]["is_byok"] is True


def test_aspect_ratio_is_stated_in_the_prompt():
    """The OpenRouter image route is a chat completion with no size field, so
    the prompt is the only channel that can carry the requested shape."""
    hint = extended_tools._aspect_ratio_prompt_hint("9:16")
    assert "9:16" in hint
    assert "portrait" in hint
    assert "past the edges" in hint

    assert "16:9" in extended_tools._aspect_ratio_prompt_hint("16:9")
    assert "landscape" in extended_tools._aspect_ratio_prompt_hint("16:9")
    assert "square" in extended_tools._aspect_ratio_prompt_hint("1:1")
    # nothing requested, nothing appended
    assert extended_tools._aspect_ratio_prompt_hint("") == ""
    assert extended_tools._aspect_ratio_prompt_hint("banana") == ""


@pytest.mark.asyncio
async def test_generate_file_routes_video_to_first_party_video_tool(monkeypatch):
    captured: dict = {}

    async def fake_generate_video_handler(entity_id: str = "", user_id: str = "", **kwargs):
        captured.update({"entity_id": entity_id, "user_id": user_id, "kwargs": kwargs})
        return json.dumps({"status": "pending", "job_id": "job_123"})

    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(extended_tools, "_generate_video_handler", fake_generate_video_handler)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        task_id="task_123",
        _agent_id_from_context="agent_123",
        kind="video",
        prompt="make a stormy mountain scene",
        params={
            "duration": 10,
            "resolution": "1080p",
            "aspect_ratio": "16:9",
            "first_frame_url": "/api/v1/fs/entity/uploads/chat/start.png",
        },
        _active_user_message_from_context="[Image: start.png → /api/v1/fs/entity/uploads/chat/start.png]",
        _runtime_artifact_urls_from_context=["/api/v1/fs/entity/generated/style.png"],
    )

    assert json.loads(result)["job_id"] == "job_123"
    assert captured["entity_id"] == "entity"
    assert captured["user_id"] == "user"
    assert captured["kwargs"]["prompt"] == "make a stormy mountain scene"
    assert captured["kwargs"]["workspace_id"] == "ws_123"
    assert captured["kwargs"]["task_id"] == "task_123"
    assert captured["kwargs"]["agent_id"] == "agent_123"
    assert captured["kwargs"]["conversation_id"] == "conversation"
    assert captured["kwargs"]["duration"] == 10
    assert captured["kwargs"]["first_frame_url"].endswith("start.png")
    assert captured["kwargs"]["_active_user_message_from_context"].startswith("[Image: start.png")
    assert captured["kwargs"]["_runtime_artifact_urls_from_context"] == ["/api/v1/fs/entity/generated/style.png"]


@pytest.mark.asyncio
async def test_generate_file_routes_source_image_video_to_real_generator_as_reference(monkeypatch):
    # A source/title-card image must be used as an image REFERENCE for real
    # video generation — not looped into a near-static clip. It is folded into
    # reference_urls and the real video handler is invoked.
    captured: dict = {}

    async def fake_generate_video_handler(entity_id: str = "", user_id: str = "", **kwargs):
        captured.update({"entity_id": entity_id, "user_id": user_id, **kwargs})
        return json.dumps({"kind": "video", "status": "completed", "video_url": "/api/v1/fs/entity/video/clip.mp4"})

    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(extended_tools, "_generate_video_handler", fake_generate_video_handler)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        task_id="task_123",
        _agent_id_from_context="agent_123",
        kind="video",
        name="项目/openings/op-01-片头/clips/标题卡.mp4",
        prompt="animate this title card into a dynamic intro preserving all text",
        params={
            "source_image_url": "项目/openings/op-01-片头/assets/标题卡.png",
            "duration": 4,
            "resolution": "1080p",
            "aspect_ratio": "9:16",
        },
    )

    assert json.loads(result)["status"] == "completed"
    assert captured["entity_id"] == "entity"
    assert captured["user_id"] == "user"
    refs = captured.get("reference_urls") or []
    assert any(str(r).endswith("标题卡.png") for r in refs), refs
    # the static-path key must not leak through to the generator
    assert "source_image_url" not in captured


@pytest.mark.asyncio
async def test_generate_file_presentation_passes_runtime_artifacts_to_pptx_skill(monkeypatch):
    captured: dict = {}

    async def fake_scope_workspace_output_name(**kwargs):
        return kwargs.get("name") or "deck.pptx"

    async def fake_invoke_builtin_skill(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope_workspace_output_name)
    monkeypatch.setattr(generate_file_tool, "_invoke_builtin_skill", fake_invoke_builtin_skill)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        kind="presentation",
        name="deck.pptx",
        prompt="把这些图拼成 PPT",
        _runtime_artifact_urls_from_context=["/api/v1/fs/entity/Workspaces/Story/images/page_01.png"],
    )

    assert json.loads(result)["status"] == "ok"
    assert captured["skill"] == "pptx"
    assert "## Runtime Artifacts Available For This Run" in captured["prompt"]
    assert "/api/v1/fs/entity/Workspaces/Story/images/page_01.png" in captured["prompt"]
    assert "`/workspace/Workspaces/Story/images/page_01.png`" in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_file_routes_audio_to_openrouter_audio_tool(monkeypatch):
    captured: dict = {}

    async def fake_generate_audio_handler(entity_id: str = "", user_id: str = "", **kwargs):
        captured.update({"entity_id": entity_id, "user_id": user_id, "kwargs": kwargs})
        return json.dumps({"kind": "audio", "status": "completed", "audio_url": "/api/v1/fs/entity/audio/rain.mp3"})

    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(extended_tools, "_generate_audio_handler", fake_generate_audio_handler)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        task_id="task_123",
        _agent_id_from_context="agent_123",
        kind="audio",
        name="project/audio/ambience/rain.mp3",
        prompt="soft night rain ambience loop",
        params={"purpose": "ambience", "duration_seconds": 15, "response_format": "mp3"},
    )

    assert json.loads(result)["kind"] == "audio"
    assert captured["entity_id"] == "entity"
    assert captured["user_id"] == "user"
    assert captured["kwargs"]["prompt"] == "soft night rain ambience loop"
    assert captured["kwargs"]["purpose"] == "ambience"
    assert captured["kwargs"]["duration_seconds"] == 15
    assert captured["kwargs"]["response_format"] == "mp3"
    assert captured["kwargs"]["conversation_id"] == "conversation"
    assert captured["kwargs"]["workspace_id"] == "ws_123"
    assert captured["kwargs"]["task_id"] == "task_123"
    assert captured["kwargs"]["agent_id"] == "agent_123"


@pytest.mark.asyncio
async def test_generate_file_routes_image_with_workspace_provenance(monkeypatch):
    captured: dict = {}

    async def fake_generate_image_handler(entity_id: str = "", user_id: str = "", **kwargs):
        captured.update({"entity_id": entity_id, "user_id": user_id, "kwargs": kwargs})
        return json.dumps({"image_url": "/api/v1/fs/entity/images/cat.png"})

    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(extended_tools, "_generate_image_handler", fake_generate_image_handler)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        task_id="task_123",
        _agent_id_from_context="agent_123",
        kind="image",
        name="images/cat.png",
        prompt="orange cat leasing poster",
    )

    assert json.loads(result)["image_url"].endswith("cat.png")
    assert captured["entity_id"] == "entity"
    assert captured["user_id"] == "user"
    assert captured["kwargs"]["workspace_id"] == "ws_123"
    assert captured["kwargs"]["task_id"] == "task_123"
    assert captured["kwargs"]["agent_id"] == "agent_123"
    assert captured["kwargs"]["conversation_id"] == "conversation"


@pytest.mark.asyncio
async def test_gemini_tts_uses_pcm_request_and_converts_requested_mp3_artifact(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-test", "", False

    async def fake_speech_bytes(**kwargs):
        captured["request_format"] = kwargs["audio_format"]
        return b"\x01\x00" * 24

    async def fake_transcode(audio_bytes, *, source_format, target_format):
        captured["transcode_source"] = source_format
        captured["transcode_target"] = target_format
        assert audio_bytes.startswith(b"RIFF")
        return b"ID3\x04converted-audio"

    async def fake_save_audio(**kwargs):
        captured["storage_format"] = kwargs["audio_format"]
        captured["audio_prefix"] = kwargs["audio_bytes"][:4]
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_key", lambda _provider: "")
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)
    monkeypatch.setattr(extended_tools, "transcode_audio_bytes", fake_transcode)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
            response_format="mp3",
        )
    )

    assert result["status"] == "completed"
    assert captured["request_format"] == "pcm"
    assert captured["transcode_source"] == "wav"
    assert captured["transcode_target"] == "mp3"
    assert captured["storage_format"] == "mp3"
    assert captured["audio_prefix"] == b"ID3\x04"
    assert result["format"] == "mp3"
    assert result["provider_response_format"] == "pcm"


@pytest.mark.asyncio
async def test_openrouter_speech_retries_empty_success_then_succeeds(monkeypatch):
    responses = [
        type(
            "FakeResponse",
            (),
            {"status_code": 200, "text": "", "content": b"", "headers": {}},
        )(),
        type(
            "FakeResponse",
            (),
            {"status_code": 200, "text": "", "content": b"\x01\x00audio", "headers": {}},
        )(),
    ]
    delays: list[float] = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return responses.pop(0)

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools.asyncio, "sleep", fake_sleep)

    audio_bytes = await extended_tools._openrouter_speech_bytes(
        api_key="sk-or-test",
        model="google/gemini-3.1-flash-tts-preview",
        prompt="Narrate this line",
        voice="Zephyr",
        audio_format="pcm",
    )

    assert audio_bytes == b"\x01\x00audio"
    assert delays == [1.0]
    assert responses == []


@pytest.mark.asyncio
async def test_openrouter_speech_reports_empty_success_as_retryable_outage(monkeypatch):
    calls = 0

    class FakeResponse:
        status_code = 200
        text = ""
        content = b""
        headers = {"x-generation-id": "gen-empty-audio"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return FakeResponse()

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools.asyncio, "sleep", fake_sleep)

    with pytest.raises(extended_tools._AudioProviderUnavailable) as exc_info:
        await extended_tools._openrouter_speech_bytes(
            api_key="sk-or-test",
            model="google/gemini-3.1-flash-tts-preview",
            prompt="Narrate this line",
            voice="Zephyr",
            audio_format="pcm",
        )

    assert calls == 3
    assert exc_info.value.provider == "openrouter"
    assert exc_info.value.status_code == 200
    assert exc_info.value.attempts == 3
    assert "did not include audio data" in str(exc_info.value)
    assert "generation_id=gen-empty-audio" in str(exc_info.value)


@pytest.mark.asyncio
async def test_openrouter_speech_rejects_non_audio_success_response(monkeypatch):
    body = b'{"error":"upstream returned no audio"}'

    class FakeResponse:
        status_code = 200
        content = body
        headers = {"content-type": "application/json"}
        text = body.decode("utf-8")

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError, match="instead of audio"):
        await extended_tools._openrouter_speech_bytes(
            api_key="sk-or-test",
            model="google/gemini-3.1-flash-tts-preview",
            prompt="Narrate this line",
            voice="Zephyr",
            audio_format="pcm",
        )


@pytest.mark.asyncio
async def test_openrouter_speech_retries_transient_5xx_then_succeeds(monkeypatch):
    responses = [
        type(
            "FakeResponse",
            (),
            {"status_code": 500, "text": "upstream failed", "content": b"", "headers": {}},
        )(),
        type(
            "FakeResponse",
            (),
            {"status_code": 503, "text": "try later", "content": b"", "headers": {}},
        )(),
        type(
            "FakeResponse",
            (),
            {"status_code": 200, "text": "", "content": b"\x01\x00audio", "headers": {}},
        )(),
    ]
    delays: list[float] = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return responses.pop(0)

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools.asyncio, "sleep", fake_sleep)

    audio_bytes = await extended_tools._openrouter_speech_bytes(
        api_key="sk-or-test",
        model="google/gemini-3.1-flash-tts-preview",
        prompt="Narrate this line",
        voice="Zephyr",
        audio_format="pcm",
    )

    assert audio_bytes == b"\x01\x00audio"
    assert delays == [1.0, 2.0]
    assert responses == []


@pytest.mark.asyncio
async def test_openrouter_speech_reports_retryable_provider_outage(monkeypatch):
    calls = 0

    class FakeResponse:
        status_code = 500
        text = "upstream service error"
        content = b""
        headers = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return FakeResponse()

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools.asyncio, "sleep", fake_sleep)

    with pytest.raises(extended_tools._AudioProviderUnavailable) as exc_info:
        await extended_tools._openrouter_speech_bytes(
            api_key="sk-or-test",
            model="google/gemini-3.1-flash-tts-preview",
            prompt="Narrate this line",
            voice="Zephyr",
            audio_format="pcm",
        )

    assert calls == 3
    assert exc_info.value.provider == "openrouter"
    assert exc_info.value.status_code == 500
    assert exc_info.value.attempts == 3
    assert "changing the requested MP3/WAV format will not help" in str(exc_info.value)


@pytest.mark.asyncio
async def test_audio_handler_surfaces_provider_outage_without_format_retry(monkeypatch):
    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-test", "", False

    async def fake_speech_bytes(**_kwargs):
        raise extended_tools._AudioProviderUnavailable(
            provider="OpenRouter",
            status_code=500,
            attempts=3,
            detail="upstream service error",
        )

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_key", lambda _provider: "")
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
            response_format="mp3",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "audio_provider_unavailable"
    assert result["retryable"] is True
    assert result["audio_generated"] is False
    assert result["format_related"] is False
    assert result["provider_status"] == 500
    assert result["attempts"] == 3
    assert "do not change" in result["retry_advice"]


@pytest.mark.asyncio
async def test_audio_handler_preserves_requested_model_when_default_gemini_tts_is_unavailable(monkeypatch):
    calls: list[dict] = []

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-test", "", False

    async def fake_speech_bytes(**kwargs):
        calls.append(kwargs)
        raise extended_tools._AudioProviderUnavailable(
            provider="OpenRouter",
            status_code=500,
            attempts=3,
            detail="upstream service error",
        )

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_key", lambda _provider: "")
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "audio_provider_unavailable"
    assert result["model"] == "google/gemini-3.1-flash-tts-preview"
    assert [call["model"] for call in calls] == ["google/gemini-3.1-flash-tts-preview"]


@pytest.mark.asyncio
async def test_generated_audio_save_rejects_zero_frame_wav_before_persisting():
    from packages.core.services.audio_conversion import AudioConversionError

    with pytest.raises(AudioConversionError, match="zero audio frames"):
        await extended_tools._save_generated_audio_bytes(
            entity_id="",
            user_id="user",
            prompt="Narrate this line",
            model="google/gemini-3.1-flash-tts-preview",
            purpose="narration",
            audio_bytes=_test_pcm_wav([]),
            audio_format="wav",
            is_byok=False,
        )


@pytest.mark.asyncio
async def test_audio_conversion_transcodes_wav_to_real_mp3():
    import shutil

    from packages.core.services.audio_conversion import transcode_audio_bytes

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    wav_bytes = _test_pcm_wav([0, 1200, -1200, 600, -600] * 480)

    mp3_bytes = await transcode_audio_bytes(
        wav_bytes,
        source_format="wav",
        target_format="mp3",
    )

    assert len(mp3_bytes) > 44
    assert mp3_bytes.startswith(b"ID3") or mp3_bytes.startswith(b"\xff")


@pytest.mark.asyncio
async def test_openrouter_zyphra_tts_uses_provider_supported_mp3(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "zyphra/zonos-v0.1-hybrid", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-test", "", False

    async def fake_platform_credential(_provider):
        return "", ""

    async def fake_speech_bytes(**kwargs):
        captured["request_format"] = kwargs["audio_format"]
        return b"ID3\x04audio"

    async def fake_transcode(audio_bytes, *, source_format, target_format):
        captured["transcode_source"] = source_format
        captured["transcode_target"] = target_format
        assert audio_bytes.startswith(b"ID3")
        return _test_pcm_wav([0, 1200, -1200, 600, -600])

    async def fake_save_audio(**kwargs):
        captured["storage_format"] = kwargs["audio_format"]
        captured["audio_prefix"] = kwargs["audio_bytes"][:4]
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(
        extended_tools,
        "_platform_native_media_credential_async",
        fake_platform_credential,
    )
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)
    monkeypatch.setattr(extended_tools, "transcode_audio_bytes", fake_transcode)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
            response_format="wav",
        )
    )

    assert result["status"] == "completed"
    assert captured["request_format"] == "mp3"
    assert captured["transcode_source"] == "mp3"
    assert captured["transcode_target"] == "wav"
    assert captured["storage_format"] == "wav"
    assert captured["audio_prefix"] == b"RIFF"
    assert result["format"] == "wav"
    assert result["provider_response_format"] == "mp3"


@pytest.mark.asyncio
async def test_openai_tts_uses_custom_byok_base_url_with_relay_shaped_key(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-relay-key", "https://apitokengate.com/v1", True

    async def fake_openai_speech_bytes(**kwargs):
        captured.update(kwargs)
        return b"audio"

    async def fake_openrouter_speech_bytes(**_kwargs):  # pragma: no cover - should not be called
        raise AssertionError("Custom OpenAI-compatible BYOK must not use OpenRouter TTS")

    async def fake_save_audio(**kwargs):
        captured["saved_audio"] = kwargs["audio_bytes"]
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_openai_speech_bytes)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_openrouter_speech_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
            voice_instructions="Warm, conversational, with natural pauses.",
        )
    )

    assert result["status"] == "completed"
    assert captured["base_url"] == "https://apitokengate.com/v1"
    assert captured["model"] == "openai/gpt-4o-mini-tts"
    assert captured["voice_instructions"] == (
        "Speak in en-US. Warm, conversational, with natural pauses."
    )
    assert captured["saved_audio"] == b"audio"


@pytest.mark.asyncio
async def test_openai_tts_excludes_openrouter_base_for_non_openrouter_key(monkeypatch):
    calls = {"speech": 0, "chat": 0, "save": 0}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-native-looking-key", "https://openrouter.ai/api/v1", True

    async def fake_openai_speech_bytes(**_kwargs):  # pragma: no cover - should not be called
        calls["speech"] += 1
        return b"unexpected"

    async def fake_chat_audio_bytes(**_kwargs):  # pragma: no cover - should not be called
        calls["chat"] += 1
        return b"unexpected"

    async def fake_save_audio(**_kwargs):  # pragma: no cover - should not be called
        calls["save"] += 1
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_openai_speech_bytes)
    monkeypatch.setattr(extended_tools, "_openai_compatible_chat_audio_bytes", fake_chat_audio_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result == {
        "kind": "audio",
        "status": "error",
        "code": "provider_key_required",
        "error": "Self-hosted audio generation requires a matching provider API key.",
        "purpose": "narration",
        "provider": "openrouter",
        "retryable": False,
        "audio_generated": False,
        "model": "openai/gpt-4o-mini-tts",
    }
    assert calls == {"speech": 0, "chat": 0, "save": 0}


@pytest.mark.asyncio
async def test_gemini_tts_uses_openrouter_env_without_byok_in_oss(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "", "", False

    async def fake_primary_credentials(*_args, **_kwargs):
        return "", "", False

    async def fake_openrouter_speech_bytes(**kwargs):
        captured.update(kwargs)
        return b"\x01\x00" * 24

    async def fake_save_audio(**kwargs):
        captured["is_byok"] = kwargs["is_byok"]
        captured["storage_format"] = kwargs["audio_format"]
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env-key")
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_primary_byok_media_credentials",
        fake_primary_credentials,
    )
    monkeypatch.setattr(
        extended_tools,
        "_openrouter_speech_bytes",
        fake_openrouter_speech_bytes,
    )
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="",
        user_id="user",
        prompt="Narrate this line",
        purpose="narration",
    ))

    assert result["status"] == "completed"
    assert captured["api_key"] == "sk-or-env-key"
    assert captured["model"] == "google/gemini-3.1-flash-tts-preview"
    assert captured["is_byok"] is False
    assert captured["storage_format"] == "wav"


@pytest.mark.asyncio
async def test_official_openai_tts_ignores_primary_byok_when_voice_key_is_missing(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/tts-1-hd", "voice"

    async def fake_voice_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-platform-key", "", False

    async def fake_primary_credentials(*_args, **_kwargs):
        raise AssertionError("Primary BYOK must not override Official Voice routing")

    async def fake_official_route(model, **kwargs):
        assert model == "openai/tts-1-hd"
        assert kwargs.get("gateway_provider") is None
        return SimpleNamespace(
            api_key="vck-gateway",
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source_detail="platform",
        )

    async def fake_vercel_speech_bytes(**kwargs):
        captured.update(kwargs)
        return b"audio"

    async def fake_openrouter_speech_bytes(**_kwargs):  # pragma: no cover - should not be called
        raise AssertionError("Vercel should satisfy Official OpenAI TTS")

    async def fake_save_audio(**kwargs):
        captured["is_byok"] = kwargs["is_byok"]
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_voice_credentials)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_primary_byok_media_credentials",
        fake_primary_credentials,
        raising=False,
    )
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", fake_official_route)
    monkeypatch.setattr(extended_tools, "_vercel_speech_bytes", fake_vercel_speech_bytes)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_openrouter_speech_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "completed"
    assert captured["base_url"] == "https://ai-gateway.vercel.sh/v1"
    assert captured["api_key"] == "vck-gateway"
    assert captured["is_byok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "response_text"),
    [
        (404, "404 page not found"),
        (404, json.dumps({"detail": "Not Found"})),
        (405, "endpoint unavailable"),
        (501, "endpoint unavailable"),
    ],
)
async def test_openai_tts_marks_only_unavailable_speech_endpoints_for_fallback(
    monkeypatch,
    status_code,
    response_text,
):
    class FakeResponse:
        content = b""

        def __init__(self, status_code, response_text):
            self.status_code = status_code
            self.text = response_text

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse(status_code, response_text)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(extended_tools._OpenAICompatibleSpeechEndpointUnavailable) as exc_info:
        await extended_tools._openai_compatible_speech_bytes(
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            model="openai/gpt-4o-mini-tts",
            prompt="Narrate this line",
            voice="alloy",
            audio_format="mp3",
        )

    assert f"({status_code})" in str(exc_info.value)


@pytest.mark.asyncio
async def test_openai_tts_model_not_found_404_does_not_fallback(monkeypatch):
    calls = {"chat": 0, "save": 0}

    class FakeResponse:
        status_code = 404
        content = b""
        text = json.dumps(
            {
                "error": {
                    "message": "The model gpt-4o-mini-tts does not exist",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            }
        )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-relay-key", "https://apitokengate.com/v1", True

    async def fake_chat_audio_bytes(**_kwargs):  # pragma: no cover - should not be called
        calls["chat"] += 1
        return _test_pcm_wav([0, 1200, -1200])

    async def fake_save_audio(**_kwargs):  # pragma: no cover - should not be called
        calls["save"] += 1
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_chat_audio_bytes", fake_chat_audio_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "error"
    assert "model_not_found" in result["error"]
    assert calls == {"chat": 0, "save": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 503])
async def test_openai_tts_does_not_fallback_on_speech_provider_errors(
    monkeypatch,
    status_code,
):
    calls = {"chat": 0, "save": 0}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-relay-key", "https://apitokengate.com/v1", True

    async def fake_openai_speech_bytes(**_kwargs):
        raise RuntimeError(f"OpenAI-compatible speech generation failed ({status_code}): unavailable")

    async def fake_chat_audio_bytes(**_kwargs):  # pragma: no cover - should not be called
        calls["chat"] += 1
        return b"unexpected"

    async def fake_save_audio(**_kwargs):  # pragma: no cover - should not be called
        calls["save"] += 1
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_openai_speech_bytes)
    monkeypatch.setattr(
        extended_tools,
        "_openai_compatible_chat_audio_bytes",
        fake_chat_audio_bytes,
        raising=False,
    )
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "error"
    assert f"({status_code})" in result["error"]
    assert calls == {"chat": 0, "save": 0}


@pytest.mark.asyncio
async def test_openai_tts_falls_back_to_chat_audio_with_same_byok_route(monkeypatch):
    captured: dict = {}
    wav_bytes = _test_pcm_wav([0, 1200, -1200, 600, -600])

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-relay-key", "https://apitokengate.com/v1", True

    async def fake_openai_speech_bytes(**kwargs):
        captured["speech"] = kwargs
        raise extended_tools._OpenAICompatibleSpeechEndpointUnavailable(
            "OpenAI-compatible speech generation failed (404): Not Found"
        )

    async def fake_chat_audio_bytes(**kwargs):
        captured["chat"] = kwargs
        return wav_bytes

    async def fake_openrouter_speech_bytes(**_kwargs):  # pragma: no cover - should not be called
        raise AssertionError("OpenAI-compatible BYOK fallback must not use OpenRouter")

    async def fake_save_audio(**kwargs):
        captured["saved_audio"] = kwargs["audio_bytes"]
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_openai_speech_bytes)
    monkeypatch.setattr(
        extended_tools,
        "_openai_compatible_chat_audio_bytes",
        fake_chat_audio_bytes,
        raising=False,
    )
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_openrouter_speech_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "completed"
    assert captured["chat"]["api_key"] == "sk-relay-key"
    assert captured["chat"]["base_url"] == "https://apitokengate.com/v1"
    assert captured["chat"]["model"] == captured["speech"]["model"]
    assert captured["chat"]["prompt"] == captured["speech"]["prompt"]
    assert captured["chat"]["voice"] == captured["speech"]["voice"]
    assert captured["speech"]["audio_format"] == "mp3"
    assert captured["chat"]["audio_format"] == "wav"
    assert captured["saved_audio"] == wav_bytes
    assert result["format"] == "wav"
    assert result["provider_response_format"] == "wav"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_model", "advertised_models", "expected_model"),
    [
        (
            "openai/gpt-audio-custom",
            ["gpt-audio-mini", "gpt-4o-audio-preview", "gpt-audio-custom"],
            "gpt-audio-custom",
        ),
        (
            "openai/gpt-4o-mini-tts",
            ["gpt-audio-mini", "gpt-4o-audio-preview"],
            "gpt-4o-audio-preview",
        ),
        (
            "openai/gpt-4o-mini-tts",
            ["text-only-model", "gpt-audio-mini"],
            "gpt-audio-mini",
        ),
    ],
)
async def test_openai_chat_audio_selects_model_in_priority_order_and_returns_wav(
    monkeypatch,
    requested_model,
    advertised_models,
    expected_model,
):
    requests: list[dict] = []
    wav_bytes = _test_pcm_wav([0, 1200, -1200, 600, -600])

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            requests.append({"method": "GET", "url": url, **kwargs})
            return FakeResponse({"data": [{"id": model} for model in advertised_models]})

        def stream(self, method, url, **kwargs):
            requests.append({"method": method, "url": url, **kwargs})
            return _FakeHTTPStream(
                {
                    "choices": [
                        {
                            "message": {
                                "audio": {
                                    "data": base64.b64encode(wav_bytes).decode("ascii")
                                }
                            }
                        }
                    ]
                }
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    audio_bytes = await extended_tools._openai_compatible_chat_audio_bytes(
        api_key="sk-relay-key",
        base_url="https://apitokengate.com/v1",
        model=requested_model,
        prompt="Narrate this line",
        voice="alloy",
        audio_format="wav",
    )

    assert audio_bytes == wav_bytes
    assert [request["url"] for request in requests] == [
        "https://apitokengate.com/v1/models",
        "https://apitokengate.com/v1/chat/completions",
    ]
    assert all(
        request["headers"]["Authorization"] == "Bearer sk-relay-key"
        for request in requests
    )
    assert requests[1]["json"] == {
        "model": expected_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Speak the user's script exactly as written. Do not introduce, "
                    "remove, paraphrase, explain, or comment on it. "
                    "Use a natural, conversational delivery."
                ),
            },
            {"role": "user", "content": "Narrate this line"},
        ],
        "modalities": ["text", "audio"],
        "audio": {"voice": "alloy", "format": "wav"},
        "stream": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_stage", "expected_error"),
    [
        ("models", "model discovery returned invalid JSON"),
        ("chat", "chat audio generation returned invalid JSON"),
    ],
)
async def test_openai_chat_audio_rejects_invalid_json(
    monkeypatch,
    invalid_stage,
    expected_error,
):
    class FakeResponse:
        status_code = 200
        text = "not-json"

        def __init__(self, payload=None, *, invalid_json=False):
            self._payload = payload
            self._invalid_json = invalid_json

        def json(self):
            if self._invalid_json:
                raise ValueError("invalid JSON")
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            if invalid_stage == "models":
                return FakeResponse(invalid_json=True)
            return FakeResponse({"data": [{"id": "gpt-4o-audio-preview"}]})

        def stream(self, *_args, **_kwargs):
            return _FakeHTTPStream(body=b"not-json")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(
        extended_tools._OpenAICompatibleAudioProviderBlocker,
        match=expected_error,
    ):
        await extended_tools._openai_compatible_chat_audio_bytes(
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            model="openai/gpt-4o-mini-tts",
            prompt="Narrate this line",
            voice="alloy",
            audio_format="wav",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("models_payload", "expected_error"),
    [
        ({"data": {}}, "model discovery field 'data' must be a list"),
        (
            {"data": [{"id": "gpt-4o-audio-preview"}, "not-an-object"]},
            "model discovery field 'data' must contain only objects",
        ),
    ],
)
async def test_openai_chat_audio_rejects_invalid_model_list_shapes(
    models_payload,
    expected_error,
):
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return models_payload

    class FakeClient:
        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    with pytest.raises(
        extended_tools._OpenAICompatibleAudioProviderBlocker,
        match=expected_error,
    ):
        await extended_tools._discover_openai_compatible_chat_audio_model(
            client=FakeClient(),
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            requested_model="openai/gpt-4o-mini-tts",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_payload", "expected_error"),
    [
        ({"choices": {}}, "chat response field 'choices' must be a non-empty list"),
        ({"choices": []}, "chat response field 'choices' must be a non-empty list"),
        ({"choices": ["not-an-object"]}, "chat response choice must be an object"),
        ({"choices": [{"message": "not-an-object"}]}, "chat response message must be an object"),
        (
            {"choices": [{"message": {"audio": "not-an-object"}}]},
            "chat response audio must be an object",
        ),
        (
            {"choices": [{"message": {"audio": {"data": 123}}}]},
            "chat response audio.data must be a string",
        ),
    ],
)
async def test_openai_chat_audio_rejects_invalid_chat_response_shapes(
    monkeypatch,
    chat_payload,
    expected_error,
):
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "gpt-4o-audio-preview"}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

        def stream(self, *_args, **_kwargs):
            return _FakeHTTPStream(chat_payload)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(
        extended_tools._OpenAICompatibleAudioProviderBlocker,
        match=expected_error,
    ):
        await extended_tools._openai_compatible_chat_audio_bytes(
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            model="openai/gpt-4o-mini-tts",
            prompt="Narrate this line",
            voice="alloy",
            audio_format="wav",
        )


@pytest.mark.asyncio
async def test_openai_chat_audio_rejects_oversized_content_length_before_read(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "gpt-4o-audio-preview"}]}

    response_limit = extended_tools._MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_RESPONSE_BYTES
    stream_response = _FakeHTTPStream(
        body=b"must not be read",
        headers={"Content-Length": str(response_limit + 1)},
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

        def stream(self, *_args, **_kwargs):
            return stream_response

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(
        extended_tools._OpenAICompatibleAudioProviderBlocker,
        match=rf"response Content-Length exceeded the {response_limit}-byte limit",
    ):
        await extended_tools._openai_compatible_chat_audio_bytes(
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            model="openai/gpt-4o-mini-tts",
            prompt="Narrate this line",
            voice="alloy",
            audio_format="wav",
        )

    assert stream_response.iterated is False


@pytest.mark.asyncio
async def test_openai_chat_audio_rejects_stream_when_incremental_cap_is_exceeded(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "gpt-4o-audio-preview"}]}

    stream_response = _FakeHTTPStream(chunks=[b"1234", b"56789", b"must-not-be-read"])

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

        def stream(self, *_args, **_kwargs):
            return stream_response

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools, "_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_RESPONSE_BYTES", 8)

    with pytest.raises(
        extended_tools._OpenAICompatibleAudioProviderBlocker,
        match="response exceeded the 8-byte limit while streaming",
    ):
        await extended_tools._openai_compatible_chat_audio_bytes(
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            model="openai/gpt-4o-mini-tts",
            prompt="Narrate this line",
            voice="alloy",
            audio_format="wav",
        )

    assert stream_response.chunks_yielded == 2


@pytest.mark.asyncio
async def test_openai_chat_audio_rejects_invalid_base64(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse({"data": [{"id": "gpt-4o-audio-preview"}]})

        def stream(self, *_args, **_kwargs):
            return _FakeHTTPStream({"choices": [{"message": {"audio": {"data": "%%%"}}}]})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(
        extended_tools._OpenAICompatibleAudioProviderBlocker,
        match="invalid base64 audio data",
    ):
        await extended_tools._openai_compatible_chat_audio_bytes(
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            model="openai/gpt-4o-mini-tts",
            prompt="Narrate this line",
            voice="alloy",
            audio_format="wav",
        )


@pytest.mark.asyncio
async def test_openai_chat_audio_rejects_oversized_decoded_audio(monkeypatch):
    assert extended_tools._MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES == 50 * 1024 * 1024

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse({"data": [{"id": "gpt-4o-audio-preview"}]})

        def stream(self, *_args, **_kwargs):
            oversized = base64.b64encode(b"x" * 9).decode("ascii")
            return _FakeHTTPStream({"choices": [{"message": {"audio": {"data": oversized}}}]})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools, "_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES", 8)

    with pytest.raises(
        extended_tools._OpenAICompatibleAudioProviderBlocker,
        match="exceeded the 8-byte limit",
    ):
        await extended_tools._openai_compatible_chat_audio_bytes(
            api_key="sk-relay-key",
            base_url="https://apitokengate.com/v1",
            model="openai/gpt-4o-mini-tts",
            prompt="Narrate this line",
            voice="alloy",
            audio_format="wav",
        )


@pytest.mark.asyncio
async def test_openai_chat_audio_missing_data_returns_error_without_artifact(monkeypatch):
    saved = False

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse({"data": [{"id": "gpt-4o-audio-preview"}]})

        def stream(self, *_args, **_kwargs):
            return _FakeHTTPStream({"choices": [{"message": {"content": "No audio available"}}]})

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-relay-key", "https://apitokengate.com/v1", True

    async def fake_openai_speech_bytes(**_kwargs):
        raise extended_tools._OpenAICompatibleSpeechEndpointUnavailable(
            "OpenAI-compatible speech generation failed (404): Not Found"
        )

    async def fake_save_audio(**_kwargs):  # pragma: no cover - should not be called
        nonlocal saved
        saved = True
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_openai_speech_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "provider_blocker"
    assert "did not include audio data" in result["error"]
    assert saved is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_wav", "expected_error"),
    [
        (b"RIFF", "too short to be a WAV file"),
        (_test_pcm_wav([]), "zero audio frames"),
        (_test_pcm_wav([0, 0, 0, 0]), "digital silence"),
    ],
)
async def test_openai_chat_audio_invalid_wav_blocks_artifact_save(
    monkeypatch,
    invalid_wav,
    expected_error,
):
    saved = False

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-relay-key", "https://apitokengate.com/v1", True

    async def fake_openai_speech_bytes(**_kwargs):
        raise extended_tools._OpenAICompatibleSpeechEndpointUnavailable(
            "OpenAI-compatible speech generation failed (404): 404 page not found"
        )

    async def fake_chat_audio_bytes(**kwargs):
        assert kwargs["audio_format"] == "wav"
        return invalid_wav

    async def fake_save_audio(**_kwargs):  # pragma: no cover - should not be called
        nonlocal saved
        saved = True
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_openai_speech_bytes)
    monkeypatch.setattr(extended_tools, "_openai_compatible_chat_audio_bytes", fake_chat_audio_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="entity",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "provider_blocker"
    assert expected_error in result["error"]
    assert saved is False


@pytest.mark.asyncio
async def test_gemini_tts_uses_native_google_key_when_available(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "google/gemini-3.1-flash-tts-preview", "voice"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "AIza-user-google-key", "", True

    async def fake_google_speech_bytes(**kwargs):
        captured["api_key"] = kwargs["api_key"]
        captured["model"] = kwargs["model"]
        captured["voice"] = kwargs["voice"]
        return b"\x01\x00" * 24

    async def fake_openrouter_speech_bytes(**_kwargs):  # pragma: no cover - should not be called
        raise AssertionError("OpenRouter should not be used for native Google TTS BYOK")

    async def fake_save_audio(**kwargs):
        captured["storage_format"] = kwargs["audio_format"]
        captured["audio_prefix"] = kwargs["audio_bytes"][:4]
        captured["is_byok"] = kwargs["is_byok"]
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_google_speech_bytes", fake_google_speech_bytes)
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_openrouter_speech_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Narrate this line",
            purpose="narration",
        )
    )

    assert result["status"] == "completed"
    assert captured["api_key"] == "AIza-user-google-key"
    assert captured["model"] == "google/gemini-3.1-flash-tts-preview"
    assert captured["storage_format"] == "wav"
    assert captured["audio_prefix"] == b"RIFF"
    assert captured["is_byok"] is True
    assert result["provider_response_format"] == "pcm"


def test_google_music_audio_block_extracts_last_interactions_audio():
    from packages.core.ai.tools import extended_tools

    encoded, mime_type = extended_tools._google_music_audio_block(
        {
            "steps": [
                {"type": "model_output", "content": [{"type": "audio", "data": "Zmlyc3Q=", "mime_type": "audio/mpeg"}]},
                {"type": "model_output", "content": [{"type": "audio", "data": "c2Vjb25k", "mime_type": "audio/wav"}]},
            ]
        }
    )

    assert encoded == "c2Vjb25k"
    assert mime_type == "audio/wav"


@pytest.mark.asyncio
async def test_google_music_bytes_calls_interactions_api_and_decodes_audio(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "audio",
                                "data": base64.b64encode(b"RIFFmusic").decode("ascii"),
                                "mime_type": "audio/wav",
                            }
                        ],
                    }
                ]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, endpoint, *, headers, json):
            captured.update(endpoint=endpoint, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    audio_bytes, audio_format = await extended_tools._google_music_bytes(
        api_key="AIza-google-key",
        model="google/lyria-3-pro-preview",
        prompt="A two-minute cinematic score",
        audio_format="wav",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    )

    assert captured["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert captured["headers"]["x-goog-api-key"] == "AIza-google-key"
    assert captured["payload"] == {
        "model": "lyria-3-pro-preview",
        "input": "A two-minute cinematic score",
        "response_format": {"type": "audio"},
    }
    assert audio_bytes == b"RIFFmusic"
    assert audio_format == "wav"


@pytest.mark.asyncio
async def test_lyria_clip_uses_native_google_music_and_reports_fixed_duration(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "music"
        return "google/lyria-3-clip-preview", "audio"

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == "audio"
        return "AIza-user-google-key", "", True

    async def fake_google_music_bytes(**kwargs):
        captured.update(kwargs)
        return b"ID3\x04music", "mp3"

    async def fake_transcode(audio_bytes, *, source_format, target_format):
        captured["transcode_source"] = source_format
        captured["transcode_target"] = target_format
        assert audio_bytes.startswith(b"ID3")
        return _test_pcm_wav([0, 1200, -1200, 600, -600])

    async def fake_openrouter_audio(**_kwargs):  # pragma: no cover - should not be called
        raise AssertionError("Lyria must use the native Gemini Interactions API")

    async def fake_save_audio(**kwargs):
        captured["saved_format"] = kwargs["audio_format"]
        captured["is_byok"] = kwargs["is_byok"]
        return "/api/v1/fs/entity/audio/score.mp3"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_google_music_bytes", fake_google_music_bytes)
    monkeypatch.setattr(extended_tools, "_openrouter_audio_output_bytes", fake_openrouter_audio)
    monkeypatch.setattr(extended_tools, "transcode_audio_bytes", fake_transcode)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Warm cinematic synth score, instrumental only",
            purpose="music",
            duration_seconds=12,
            response_format="wav",
        )
    )

    assert result["status"] == "completed"
    assert captured["model"] == "google/lyria-3-clip-preview"
    assert captured["audio_format"] == "mp3"
    assert "exactly 12" not in captured["prompt"]
    assert captured["transcode_source"] == "mp3"
    assert captured["transcode_target"] == "wav"
    assert captured["saved_format"] == "wav"
    assert captured["is_byok"] is True
    assert result["format"] == "wav"
    assert result["provider_response_format"] == "mp3"
    assert result["duration_seconds"] == 30.0
    assert result["requested_duration_seconds"] == 12.0


@pytest.mark.asyncio
async def test_lyria_pro_accepts_wav_and_duration_prompt(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        return "google/lyria-3-pro-preview", "audio"

    async def fake_credentials(_user_id, _entity_id, *, role):
        return "AIza-user-google-key", "https://generativelanguage.googleapis.com/v1beta", True

    async def fake_google_music_bytes(**kwargs):
        captured.update(kwargs)
        return b"RIFFmusic", "wav"

    async def fake_save_audio(**kwargs):
        captured["saved_format"] = kwargs["audio_format"]
        return "/api/v1/fs/entity/audio/score.wav"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_google_music_bytes", fake_google_music_bytes)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="Cinematic orchestral score with a restrained final resolve",
            purpose="score",
            duration_seconds=90,
            response_format="wav",
        )
    )

    assert result["status"] == "completed"
    assert captured["audio_format"] == "wav"
    assert captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
    assert "exactly 90 seconds" in captured["prompt"]
    assert captured["saved_format"] == "wav"
    assert result["duration_seconds"] == 90.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "purpose", "expected_role"),
    [
        ("openai/gpt-audio-mini", "sfx", "sfx"),
        ("openai/gpt-audio", "music", "audio"),
    ],
)
async def test_original_openai_audio_catalog_models_use_saved_byok_at_runtime(
    monkeypatch, model, purpose, expected_role,
):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_credentials(_user_id, _entity_id, *, role):
        assert role == expected_role
        return "sk-openai-user-key", "https://api.openai.com/v1", True

    async def fake_chat_audio(**kwargs):
        captured.update(kwargs)
        return _test_pcm_wav([0, 1200, -1200, 600, -600])

    async def fake_openrouter_audio(**_kwargs):  # pragma: no cover
        raise AssertionError("native OpenAI BYOK must not route through OpenRouter")

    async def fake_save_audio(**kwargs):
        captured["saved"] = kwargs
        return "/api/v1/fs/entity/audio/generated.wav"

    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_openai_compatible_chat_audio_bytes", fake_chat_audio)
    monkeypatch.setattr(extended_tools, "_openrouter_audio_output_bytes", fake_openrouter_audio)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", fake_save_audio)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="user",
            prompt="heavy spaceship hatch impact and pressure seal slam",
            purpose=purpose,
            model=model,
        )
    )

    assert result["status"] == "completed"
    assert result["provider"] == "openai"
    assert result["model"] == model
    assert result["format"] == "wav"
    assert captured["api_key"] == "sk-openai-user-key"
    assert captured["base_url"] == "https://api.openai.com/v1"
    assert captured["model"] == model
    assert captured["render_as_speech"] is False
    assert captured["saved"]["is_byok"] is True


def test_nonvoice_audio_prompts_ban_speech():
    from packages.core.ai.tools import extended_tools

    sfx_prompt = extended_tools._audio_prompt_for_purpose("door slam", "sfx")
    transition_prompt = extended_tools._audio_prompt_for_purpose("fast whoosh", "transition")
    ambience_prompt = extended_tools._audio_prompt_for_purpose(
        "ocean waves and distant battle",
        "soundscape",
        15,
    )

    assert "no spoken words" in sfx_prompt
    assert "no speech" in transition_prompt
    assert "no narration" in transition_prompt
    assert "no speech" in ambience_prompt
    assert "no spoken words" in ambience_prompt
    assert "Target duration: exactly 15 seconds" in ambience_prompt


@pytest.mark.asyncio
async def test_generate_file_routes_presentation_to_pptx_skill(monkeypatch):
    captured: dict = {}

    async def fake_invoke_builtin_skill(**kwargs):
        captured.update(kwargs)
        return "sandbox ready"

    monkeypatch.setattr(generate_file_tool, "_invoke_builtin_skill", fake_invoke_builtin_skill)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        kind="presentation",
        prompt="Create a 6-slide investor deck",
        name="deck.pptx",
        params={"style": "cinematic"},
    )

    assert result == "sandbox ready"
    assert captured["skill"] == "pptx"
    assert captured["conversation_id"] == "conversation"
    assert captured["name"] == "deck.pptx"
    assert captured["params"]["style"] == "cinematic"


@pytest.mark.asyncio
async def test_generate_file_scopes_office_skill_output_name_to_workspace(monkeypatch):
    captured: dict = {}

    async def fake_invoke_builtin_skill(**kwargs):
        captured.update(kwargs)
        return "sandbox ready"

    async def fake_scope_workspace_output_name(**kwargs):
        assert kwargs["workspace_id"] == "ws_123"
        assert kwargs["default_subdir"] == "presentations"
        return f"Workspaces/桌面耳机支架工业设计项目/presentations/{kwargs['name']}"

    monkeypatch.setattr(generate_file_tool, "_invoke_builtin_skill", fake_invoke_builtin_skill)
    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope_workspace_output_name)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        task_id="task_123",
        _agent_id_from_context="agent_123",
        kind="presentation",
        prompt="Create a 6-slide investor deck",
        name="deck.pptx",
    )

    assert result == "sandbox ready"
    assert captured["name"] == "Workspaces/桌面耳机支架工业设计项目/presentations/deck.pptx"
    assert captured["workspace_id"] == "ws_123"
    assert captured["task_id"] == "task_123"
    assert captured["agent_id"] == "agent_123"


@pytest.mark.asyncio
async def test_generate_file_routes_quick_document_to_document_generator(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    from packages.core.ai.tools.generate_file import document as document_route

    monkeypatch.setattr(document_route, "runtime_generate_document_file", fake_generate_document_file)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        task_id="task_123",
        _agent_id_from_context="agent_123",
        kind="document",
        name="summary.md",
        content="# Summary\n\nDone.",
        file_type="md",
    )

    assert json.loads(result)["created"] is True
    assert captured["entity_id"] == "entity"
    assert captured["name"] == "summary.md"
    assert captured["content"].startswith("# Summary")
    assert captured["workspace_id"] == "ws_123"
    assert captured["task_id"] == "task_123"
    assert captured["agent_id"] == "agent_123"
    assert captured["conversation_id"] == "conversation"


@pytest.mark.asyncio
async def test_generate_file_routes_diagram_prompt_to_document_generator(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True, "document": {"name": kwargs["name"]}})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        conversation_id="conversation",
        workspace_id="ws_123",
        task_id="task_123",
        _agent_id_from_context="agent_123",
        kind="diagram",
        name="architecture",
        prompt="Layered fuzzy system with Kalman smoothing",
        params={"canvas_width": 2600},
    )

    body = json.loads(result)
    diagram = json.loads(captured["content"])
    assert body["created"] is True
    assert captured["entity_id"] == "entity"
    assert captured["name"] == "architecture.diagram.json"
    assert captured["file_type"] == "diagram.json"
    assert captured["workspace_id"] == "ws_123"
    assert captured["task_id"] == "task_123"
    assert captured["agent_id"] == "agent_123"
    assert captured["conversation_id"] == "conversation"
    assert diagram["version"] == "editable_diagram_v1"
    assert diagram["canvas"]["width"] == 2600
    assert diagram["prompt"] == "Layered fuzzy system with Kalman smoothing"
    assert any(item.get("kind") == "connector" for item in diagram["elements"])
    assert any("Kalman" in item.get("text", "") for item in diagram["elements"])


@pytest.mark.asyncio
async def test_generate_file_rejects_over_limit_diagram_before_persistence(monkeypatch):
    from packages.core.ai.tools.generate_file.diagram import MAX_DIAGRAM_ELEMENTS

    async def unexpected_generate_document_file(**_kwargs):
        pytest.fail("over-limit diagram must not be persisted")

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "runtime_generate_document_file",
        unexpected_generate_document_file,
    )
    prompt = "flowchart TD\n" + "\n".join(
        f"N{index}" for index in range(MAX_DIAGRAM_ELEMENTS)
    )

    with pytest.raises(ValueError, match="too many elements"):
        await generate_file_tool._generate_file_handler(
            entity_id="entity",
            user_id="user",
            conversation_id="conversation",
            kind="diagram",
            name="over-limit.diagram.json",
            prompt=prompt,
        )


def test_mermaid_parser_stops_during_fan_out_expansion(monkeypatch):
    from packages.core.ai.tools.generate_file import diagram as diagram_module

    monkeypatch.setattr(diagram_module, "MAX_DIAGRAM_ELEMENTS", 32)
    sources = " & ".join(f"A{index}" for index in range(10))
    targets = " & ".join(f"B{index}" for index in range(10))

    with pytest.raises(ValueError, match="too many elements"):
        diagram_module._parse_mermaid_flow(
            f"flowchart TD\n{sources} --> {targets}"
        )


@pytest.mark.asyncio
async def test_generate_file_rejects_over_limit_diagram_canvas_before_persistence(monkeypatch):
    async def unexpected_generate_document_file(**_kwargs):
        pytest.fail("over-limit diagram canvas must not be persisted")

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "runtime_generate_document_file",
        unexpected_generate_document_file,
    )
    prompt = "flowchart TD\n" + "\n".join(
        f"N{index} --> N{index + 1}" for index in range(445)
    )

    with pytest.raises(ValueError, match="canvas is too large"):
        await generate_file_tool._generate_file_handler(
            entity_id="entity",
            user_id="user",
            conversation_id="conversation",
            kind="diagram",
            name="too-tall.diagram.json",
            prompt=prompt,
        )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"canvas_width": -1}, "canvas_width must be between"),
        ({"canvas_width": 0}, "canvas_width must be between"),
        ({"canvas_width": 319}, "canvas_width must be between"),
        ({"canvas_height": -2}, "canvas_height must be between"),
        ({"canvas_height": 179}, "canvas_height must be between"),
        ({"canvas_width": "wide"}, "canvas_width must be an integer"),
        ({"canvas_height": 600.5}, "canvas_height must be an integer"),
    ],
)
@pytest.mark.asyncio
async def test_generate_file_rejects_invalid_diagram_canvas_before_persistence(
    monkeypatch,
    params,
    message,
):
    async def unexpected_generate_document_file(**_kwargs):
        pytest.fail("invalid diagram canvas must not be persisted")

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "runtime_generate_document_file",
        unexpected_generate_document_file,
    )

    with pytest.raises(ValueError, match=message):
        await generate_file_tool._generate_file_handler(
            entity_id="entity",
            user_id="user",
            conversation_id="conversation",
            kind="diagram",
            name="invalid.diagram.json",
            prompt="Input -> Output",
            params=params,
        )


@pytest.mark.asyncio
async def test_generate_file_converts_mermaid_subgraphs_into_clean_diagram_stages(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="real-estate-workspace-flow.diagram.json",
        prompt='''flowchart TD
    subgraph SIGNALS["1 · Signals In"]
        A1["MLS listings &amp; price changes"]
    end
    subgraph WORKSPACE["2 · Manor Workspace"]
        B1["Unified records"]
    end
    subgraph AGENTS["3 · AI agents"]
        C1["Lead qualifier"]
    end
    SIGNALS --> WORKSPACE --> AGENTS''',
    )

    diagram = json.loads(captured["content"])
    shape_text = [item["text"] for item in diagram["elements"] if item["kind"] == "shape"]
    assert diagram["title"] == "real estate workspace flow"
    assert set(shape_text) == {
        "1 · Signals In",
        "MLS listings & price\nchanges",
        "2 · Manor Workspace",
        "Unified records",
        "3 · AI agents",
        "Lead qualifier",
    }
    assert all("subgraph" not in text and "]" not in text for text in shape_text)
    assert [item["kind"] for item in diagram["elements"]].count("connector") == 2


@pytest.mark.asyncio
async def test_generate_file_diagram_preserves_mermaid_branch_topology(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="branch-flow.diagram.json",
        prompt="""flowchart TD
    A((Start)) --> B{Left}
    A --> C[Right]""",
    )

    diagram = json.loads(captured["content"])
    node_id_by_text = {
        item["text"]: item["id"]
        for item in diagram["elements"]
        if item["kind"] == "shape"
    }
    links = [
        (item["from"]["bind"]["elementId"], item["to"]["bind"]["elementId"])
        for item in diagram["elements"]
        if item["kind"] == "connector"
    ]
    assert links == [
        (node_id_by_text["Start"], node_id_by_text["Left"]),
        (node_id_by_text["Start"], node_id_by_text["Right"]),
    ]
    node_shape_by_text = {
        item["text"]: item["shape"]
        for item in diagram["elements"]
        if item["kind"] == "shape"
    }
    assert node_shape_by_text["Start"] == "ellipse"
    assert node_shape_by_text["Left"] == "diamond"


@pytest.mark.asyncio
async def test_generate_file_diagram_preserves_mermaid_fan_out(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="fan-out.diagram.json",
        prompt="flowchart LR\nA --> B & C",
    )

    diagram = json.loads(captured["content"])
    node_id_by_text = {
        item["text"]: item["id"]
        for item in diagram["elements"]
        if item["kind"] == "shape"
    }
    links = [
        (item["from"]["bind"]["elementId"], item["to"]["bind"]["elementId"])
        for item in diagram["elements"]
        if item["kind"] == "connector"
    ]
    assert set(node_id_by_text) == {"A", "B", "C"}
    assert links == [
        (node_id_by_text["A"], node_id_by_text["B"]),
        (node_id_by_text["A"], node_id_by_text["C"]),
    ]


@pytest.mark.asyncio
async def test_generate_file_diagram_preserves_isolated_mermaid_nodes(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="mixed-flow.diagram.json",
        prompt="""flowchart TD
    A --> B
    C[Isolated]
    E
    subgraph GROUP[Stage]
        D[Internal detail]
    end""",
    )

    diagram = json.loads(captured["content"])
    shape_text = [item["text"] for item in diagram["elements"] if item["kind"] == "shape"]
    assert set(shape_text) == {"A", "B", "Isolated", "E", "Stage", "Internal detail"}


@pytest.mark.asyncio
async def test_generate_file_diagram_decodes_named_and_numeric_html_entities(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="entities.diagram.json",
        prompt="flowchart LR; A[Space&nbsp;here] --> B[Dash&#x2014;here]",
    )

    diagram = json.loads(captured["content"])
    labels = [
        item["text"]
        for item in diagram["elements"]
        if item["kind"] == "shape"
    ]
    assert labels == ["Space here", "Dash—here"]


@pytest.mark.asyncio
async def test_generate_file_diagram_preserves_mermaid_edge_labels(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="decision-flow.diagram.json",
        prompt="""flowchart TD
    A -- Yes --> B
    A -- No --> C
    D --- E""",
    )

    diagram = json.loads(captured["content"])
    nodes = [item for item in diagram["elements"] if item["kind"] == "shape"]
    assert {node["text"] for node in nodes} == {"A", "B", "C", "D", "E"}
    connectors = [
        item for item in diagram["elements"] if item["kind"] == "connector"
    ]
    assert [
        (connector.get("label"), connector["arrowEnd"])
        for connector in connectors
    ] == [("Yes", True), ("No", True), (None, False)]


@pytest.mark.asyncio
async def test_generate_file_diagram_preserves_mermaid_endpoint_markers(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="marker-flow.diagram.json",
        prompt="""flowchart LR
    A <--> B
    C o--o D
    E x--x F
    G --o H""",
    )

    diagram = json.loads(captured["content"])
    connectors = [
        item for item in diagram["elements"] if item["kind"] == "connector"
    ]
    assert [
        (connector.get("markerStart"), connector.get("markerEnd"))
        for connector in connectors
    ] == [
        ("arrow", "arrow"),
        ("circle", "circle"),
        ("cross", "cross"),
        (None, "circle"),
    ]
    assert [
        (connector["arrowStart"], connector["arrowEnd"])
        for connector in connectors
    ] == [
        (True, True),
        (True, True),
        (True, True),
        (False, True),
    ]


@pytest.mark.asyncio
async def test_generate_file_diagram_respects_single_line_lr_and_punctuation(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="horizontal.diagram.json",
        prompt='flowchart LR; A["Call API (v2); now"] --> B[Done]; B --> C["Users\'"]; C --> D[Owner\'s queue]',
    )

    diagram = json.loads(captured["content"])
    nodes = {
        item["text"]: item
        for item in diagram["elements"]
        if item["kind"] == "shape"
    }
    assert list(nodes) == ["Call API (v2); now", "Done", "Users'", "Owner's queue"]
    assert (
        nodes["Call API (v2); now"]["x"]
        < nodes["Done"]["x"]
        < nodes["Users'"]["x"]
        < nodes["Owner's queue"]["x"]
    )
    assert (
        nodes["Call API (v2); now"]["y"]
        == nodes["Done"]["y"]
        == nodes["Users'"]["y"]
        == nodes["Owner's queue"]["y"]
    )


@pytest.mark.asyncio
async def test_generate_file_diagram_uses_longest_dag_path(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="dag.diagram.json",
        prompt="""flowchart TD
    A --> D
    A --> B
    B --> C
    C --> D
    D --> B""",
    )

    diagram = json.loads(captured["content"])
    nodes = {
        item["text"]: item
        for item in diagram["elements"]
        if item["kind"] == "shape"
    }
    assert nodes["A"]["y"] < nodes["B"]["y"] < nodes["C"]["y"] < nodes["D"]["y"]
    connectors = [
        item for item in diagram["elements"] if item["kind"] == "connector"
    ]
    node_name_by_id = {node["id"]: name for name, node in nodes.items()}
    c_to_d = next(
        connector
        for connector in connectors
        if node_name_by_id[connector["from"]["bind"]["elementId"]] == "C"
        and node_name_by_id[connector["to"]["bind"]["elementId"]] == "D"
    )
    assert c_to_d["routing"] == "straight"
    d_to_b = next(
        connector
        for connector in connectors
        if node_name_by_id[connector["from"]["bind"]["elementId"]] == "D"
        and node_name_by_id[connector["to"]["bind"]["elementId"]] == "B"
    )
    assert d_to_b["routing"] == "curve"


@pytest.mark.asyncio
async def test_generate_file_diagram_wraps_cjk_and_long_tokens(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="diagram",
        name="readable.diagram.json",
        prompt="""flowchart TD
    A[用户输入房源目标客户和品牌资料Workspace生成短视频脚本] --> B[https://example.com/a/very/long/path]""",
    )

    diagram = json.loads(captured["content"])
    labels = [
        item["text"]
        for item in diagram["elements"]
        if item["kind"] == "shape"
    ]
    assert labels and all("\n" in label for label in labels)


@pytest.mark.asyncio
async def test_generate_file_document_diagram_name_uses_prompt_generator(monkeypatch):
    captured: dict = {}

    async def fake_generate_document_file(**kwargs):
        captured.update(kwargs)
        return json.dumps({"created": True})

    import packages.core.ai.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "runtime_generate_document_file", fake_generate_document_file)

    await generate_file_tool._generate_file_handler(
        entity_id="entity",
        user_id="user",
        kind="document",
        name="flow.diagram.json",
        prompt="Input, Validate, Save, Notify",
    )

    diagram = json.loads(captured["content"])
    assert captured["name"] == "flow.diagram.json"
    assert [item["kind"] for item in diagram["elements"]].count("shape") >= 4
    assert diagram["groups"][0]["label"] == "Generated flow"
