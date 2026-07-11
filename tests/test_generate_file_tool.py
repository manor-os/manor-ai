from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.tools import extended_tools
from packages.core.ai.tools import generate_file_tool


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
        expected_path = "Workspaces/Image Workspace/images/hero.png"

        db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="Image Workspace"))
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


def test_generate_image_aspect_ratio_defaults_and_crops():
    assert extended_tools._image_size_for_aspect_ratio("16:9") == "1536x1024"
    assert extended_tools._image_size_for_aspect_ratio("9:16") == "1024x1536"
    assert extended_tools._image_size_for_aspect_ratio("16:9", "1024x1024") == "1024x1024"

    import io
    from PIL import Image

    source = Image.new("RGB", (1024, 1024), "red")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")

    normalized, mime, size = extended_tools._normalize_image_bytes_for_aspect_ratio(
        buffer.getvalue(),
        "image/png",
        "16:9",
    )
    assert mime == "image/png"
    assert size == "1024x576"

    result = Image.open(io.BytesIO(normalized))
    assert result.size == (1024, 576)


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
async def test_gemini_tts_uses_pcm_request_and_wav_artifact(monkeypatch):
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
        return b"\x00\x00" * 24

    async def fake_save_audio(**kwargs):
        captured["storage_format"] = kwargs["audio_format"]
        captured["audio_prefix"] = kwargs["audio_bytes"][:4]
        return "/api/v1/fs/entity/audio/narration.wav"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_credentials)
    monkeypatch.setattr(extended_tools, "_platform_native_media_key", lambda _provider: "")
    monkeypatch.setattr(extended_tools, "_openrouter_speech_bytes", fake_speech_bytes)
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
    assert captured["storage_format"] == "wav"
    assert captured["audio_prefix"] == b"RIFF"
    assert result["format"] == "wav"
    assert result["provider_response_format"] == "pcm"


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
        return b"\x00\x00" * 24

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


@pytest.mark.asyncio
async def test_sfx_blocks_speech_response_audio_models(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def fake_audio_output(**kwargs):
        raise AssertionError("speech-response audio models must not generate SFX")

    monkeypatch.setattr(extended_tools, "_openrouter_audio_output_bytes", fake_audio_output)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="",
            prompt="heavy spaceship hatch impact and pressure seal slam",
            purpose="sfx",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "unsupported_nonvoice_audio_model"
    assert result["model"] == "openai/gpt-audio-mini"
    assert result["role"] == "sfx"
    assert "speech/conversational audio model" in result["error"]
    assert "non-voice audio" in result["error"]


@pytest.mark.asyncio
async def test_soundscape_blocks_speech_response_audio_models(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def fake_audio_output(**kwargs):
        raise AssertionError("speech-response audio models must not generate ambience")

    monkeypatch.setattr(extended_tools, "_openrouter_audio_output_bytes", fake_audio_output)

    result = json.loads(
        await extended_tools._generate_audio_handler(
            entity_id="",
            user_id="",
            prompt="Normandy beach soundscape with ocean waves, soldiers charging, bullets, distant explosions",
            purpose="soundscape",
            duration_seconds=15,
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "unsupported_nonvoice_audio_model"
    assert result["purpose"] == "soundscape"
    assert result["role"] == "sfx"


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
    assert captured["file_type"] == "json"
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
