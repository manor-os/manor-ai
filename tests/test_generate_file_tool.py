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
async def test_generated_audio_save_registers_workspace_logical_folder(
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
        expected_path = f"Workspaces/_by_id/{folder.id}/audio/narration.wav"

        audio_url = await extended_tools._save_generated_audio_bytes(
            entity_id=entity_id,
            user_id="user_1",
            prompt="Verbatim narration",
            model="configured-audio-model",
            purpose="narration",
            audio_bytes=b"audio-bytes",
            audio_format="wav",
            is_byok=True,
            output_name="narration.wav",
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
        assert binding.relative_parts == ("audio",)
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
    assert captured["storage_format"] == "mp3"
    assert captured["audio_prefix"] == b"ID3\x04"
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
    assert captured["voice_instructions"] == "Warm, conversational, with natural pauses."
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

    assert result == {"error": "Self-hosted audio generation requires a matching provider API key."}
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
        return b"\x00\x00" * 24

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
async def test_openai_tts_reuses_compatible_primary_byok_when_voice_key_is_missing(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def fake_resolve_audio_model(_user_id, _entity_id, *, purpose):
        assert purpose == "narration"
        return "openai/gpt-4o-mini-tts", "voice"

    async def fake_voice_credentials(_user_id, _entity_id, *, role):
        assert role == "voice"
        return "sk-or-platform-key", "", False

    async def fake_primary_credentials(_user_id, _entity_id, *, provider):
        assert provider == "openai"
        return "sk-primary-key", "https://apitokengate.com/v1", True

    async def fake_openai_speech_bytes(**kwargs):
        captured.update(kwargs)
        return b"audio"

    async def fake_openrouter_speech_bytes(**_kwargs):  # pragma: no cover - should not be called
        raise AssertionError("Compatible primary BYOK must be used for OpenAI TTS")

    async def fake_save_audio(**kwargs):
        captured["is_byok"] = kwargs["is_byok"]
        return "/api/v1/fs/entity/audio/narration.mp3"

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", fake_resolve_audio_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fake_voice_credentials)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_primary_byok_media_credentials",
        fake_primary_credentials,
        raising=False,
    )
    monkeypatch.setattr(extended_tools, "_openai_compatible_speech_bytes", fake_openai_speech_bytes)
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
    assert captured["base_url"] == "https://apitokengate.com/v1"
    assert captured["is_byok"] is True


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
