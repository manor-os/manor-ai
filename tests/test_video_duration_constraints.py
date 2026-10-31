from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from packages.core.tasks.media_tasks import normalize_video_duration, parse_video_duration


def test_video_duration_is_clamped_to_supported_model_range():
    assert normalize_video_duration(40) == 15
    assert normalize_video_duration(1) == 4
    assert normalize_video_duration(10) == 10


def test_video_duration_parser_accepts_common_seconds_strings():
    assert parse_video_duration("15s") == 15
    assert parse_video_duration("8 seconds") == 8
    assert normalize_video_duration("not a duration") == 5


def test_video_tool_schema_exposes_duration_choices():
    from packages.core.ai.tools.extended_tools import GENERATE_VIDEO_SCHEMA

    props = GENERATE_VIDEO_SCHEMA["function"]["parameters"]["properties"]
    duration = GENERATE_VIDEO_SCHEMA["function"]["parameters"]["properties"]["duration"]
    assert duration["minimum"] == 4
    assert duration["maximum"] == 30
    assert duration["default"] == 5
    assert "audio_reference_url" in props
    assert "audio_reference_urls" in props
    assert "reference_video_urls" in props
    assert "audio_url" in props
    assert "requires_reference_media" in props
    assert props["expected_model"]["type"] == "string"
    assert props["generate_audio"]["default"] is True
    assert props["requires_reference_media"]["default"] is False


def test_generate_file_schema_exposes_video_parameter_choices():
    from packages.core.ai.tools.generate_file_tool import GENERATE_FILE_SCHEMA

    props = GENERATE_FILE_SCHEMA["function"]["parameters"]["properties"]
    assert props["duration"]["enum"] == list(range(4, 31))
    assert props["params"]["properties"]["duration"]["enum"] == props["duration"]["enum"]
    assert "Single clip seconds" in props["duration"]["description"]
    assert "videos/mp4" in GENERATE_FILE_SCHEMA["function"]["description"]
    assert "reference_url" in props["params"]["properties"]
    assert "reference_urls" in props["params"]["properties"]
    assert "reference_video_urls" in props["params"]["properties"]
    assert "audio_reference_url" in props["params"]["properties"]
    assert "audio_reference_urls" in props["params"]["properties"]
    assert "audio_url" in props["params"]["properties"]
    assert "requires_reference_media" in props
    assert "requires_reference_media" in props["params"]["properties"]
    assert props["params"]["properties"]["generate_audio"]["default"] is True
    assert props["params"]["properties"]["requires_reference_media"]["default"] is False
    assert "Set false for a silent clean picture" in props["params"]["properties"]["generate_audio"]["description"]


def test_seedance_25_catalog_limits_and_duration_normalization():
    from packages.core.constants.models import CATALOG, video_model_capabilities
    from packages.core.tasks.media_tasks import normalize_video_duration

    assert any(item["id"] == "bytedance/seedance-2.5" for item in CATALOG["video"])
    caps = video_model_capabilities("bytedance/seedance-2.5")
    assert (caps["max_reference_images"], caps["max_reference_videos"], caps["max_audio_references"]) == (30, 10, 10)
    assert caps["audio_requires_visual_reference"] is False
    assert caps["generation_schema"]["duration"]["text_to_video"]["maximum"] == 30
    assert normalize_video_duration(30, "bytedance/seedance-2.5") == 30
    assert normalize_video_duration(30, "bytedance/seedance-2.0") == 15


def test_seedance_25_snapshot_keeps_all_allowed_references():
    from packages.core.tasks.media_tasks import snapshot_video_reference_urls

    result = snapshot_video_reference_urls(
        entity_id="entity", job_id="job", model="bytedance/seedance-2.5",
        reference_urls=[f"https://example.test/image-{i}.png" for i in range(30)],
        reference_video_urls=[f"https://example.test/video-{i}.mp4" for i in range(10)],
        audio_reference_urls=[f"https://example.test/audio-{i}.wav" for i in range(10)],
    )

    assert len(result["reference_urls"]) == 30
    assert len(result["reference_video_urls"]) == 10
    assert len(result["audio_reference_urls"]) == 10


def test_seedance_25_capability_validation_enforces_limits():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    model = "bytedance/seedance-2.5"
    assert _video_capability_error(model=model, prompt="sound-driven scene", audio_reference_urls=["audio.wav"], duration=30) is None
    assert "at most 30" in _video_capability_error(model=model, prompt="scene", reference_urls=["ref.png"] * 31)
    assert "at most 10" in _video_capability_error(model=model, prompt="scene", reference_video_urls=["ref.mp4"] * 11)
    assert "at most 10" in _video_capability_error(model=model, prompt="scene", audio_reference_urls=["ref.wav"] * 11)
    assert "duration schema" in _video_capability_error(model=model, prompt="scene", duration=31)
    assert "cannot be mixed" in _video_capability_error(model=model, prompt="scene", first_frame_url="frame.png", reference_urls=["ref.png"])


@pytest.mark.asyncio
async def test_video_download_reuses_existing_knowledge_document_for_same_fs_path(
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
        workspace_artifact_storage_base,
    )
    from packages.core.tasks import media_tasks

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"

    class FakeResponse:
        content = b"video-bytes"
        headers = {"content-type": "video/mp4"}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeClient)

    try:
        entity_id = generate_ulid()
        workspace_id = generate_ulid()
        document_id = generate_ulid()
        entity_root = tmp_path / entity_id
        entity_root.mkdir()

        workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Video Workspace")
        db_session.add(workspace)
        await db_session.flush()
        artifact_folder = await ensure_workspace_artifact_folder(db_session, workspace)
        expected_path = f"{workspace_artifact_storage_base(artifact_folder.id)}/launch.mp4"
        db_session.add(
            Document(
                id=document_id,
                entity_id=entity_id,
                name="launch.mp4",
                fs_path=expected_path,
                file_type="mp4",
                mime_type="video/mp4",
                source="filesystem_reconcile",
            )
        )
        await db_session.commit()

        result = await media_tasks._download_and_save(
            "https://provider.example/video.mp4",
            "Launch clip",
            "bytedance/seedance-2.0",
            "job_1",
            entity_id,
            5,
            "720p",
            output_name="launch.mp4",
            workspace_id=workspace_id,
            task_id="task_1",
            agent_id="agent_1",
            conversation_id="conv_1",
            user_id="user_1",
        )

        assert result["document_id"] == document_id
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
        assert docs[0].file_size == len(b"video-bytes")
        assert docs[0].metadata_["origin"]["workspace_id"] == workspace_id
        assert docs[0].metadata_["generation"]["job_id"] == "job_1"
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_video_handler_rejects_long_single_clip_duration(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def fake_resolve_video_model(_user_id, _entity_id):
        return "bytedance/seedance-2.0"

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", fake_resolve_video_model)

    result = json.loads(
        await extended_tools._generate_video_handler(
            entity_id="entity",
            user_id="user",
            prompt="Create a 30s robot battle video.",
            duration=30,
        )
    )

    assert result["status"] == "failed"
    assert "supports only 4-15s clips" in result["error"]
    assert "Segment the total runtime" in result["error"]
    assert "Do not start a shortened clip" in result["error"]


@pytest.mark.asyncio
async def test_video_handler_rejects_prompt_only_atlas_wan_before_creating_job(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def fake_resolve_video_model(_user_id, _entity_id):
        return "atlascloud/wan-2.2-turbo-spicy"

    async def fake_resolve_credentials(_user_id, _entity_id, *, role):
        assert role == "video"
        return "atlas-user-key", "", True

    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_video_model",
        fake_resolve_video_model,
    )
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_media_credentials",
        fake_resolve_credentials,
    )

    result = json.loads(
        await extended_tools._generate_video_handler(
            entity_id="entity",
            user_id="user",
            prompt="A fluffy orange cat dancing in a sunny room.",
            duration=5,
            generate_audio=False,
        )
    )

    assert result["status"] == "failed"
    assert "job_id" not in result
    assert "image-to-video only" in result["error"]
    assert "first_frame_url" in result["error"]
    assert "text-to-video model" in result["error"]


@pytest.mark.asyncio
async def test_video_handler_fails_instead_of_dropping_unselected_first_frame(monkeypatch):
    # Regression: an unselected first frame used to be stripped with only a
    # route warning, so an image-to-video request silently became a billed
    # text-to-video clip that ignored the image.
    from packages.core.ai.tools import extended_tools

    async def fake_resolve_video_model(_user_id, _entity_id):
        return "bytedance/seedance-v1.0-pro-fast"

    async def fail_if_credentials_resolved(*_args, **_kwargs):
        raise AssertionError("must fail before resolving credentials or creating a job")

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", fake_resolve_video_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", fail_if_credentials_resolved)

    result = json.loads(
        await extended_tools._generate_video_handler(
            entity_id="entity",
            user_id="user",
            prompt="The camera slowly pushes forward.",
            first_frame_url="/api/v1/fs/entity/shot01.png",
            duration=4,
            generate_audio=False,
            _active_user_message_from_context="用 shot01.png 作为首帧做图生视频",
        )
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "reference_media_not_selected"
    assert "first_frame_url" in result["error"]
    assert "job_id" not in result


def test_video_reference_url_alias_is_normalized():
    from packages.core.ai.tools.extended_tools import _coerce_video_reference_urls

    refs = _coerce_video_reference_urls(
        reference_urls=["/api/v1/fs/entity/images/style.png"],
        reference_url="/api/v1/fs/entity/images/hero.png",
    )
    assert refs == [
        "/api/v1/fs/entity/images/hero.png",
        "/api/v1/fs/entity/images/style.png",
    ]


def test_bare_video_filename_is_not_enough_to_select_reference():
    from packages.core.ai.tools.extended_tools import _filter_unrequested_media_references

    video_url = "/api/v1/fs/entity/videos/白蛇-白蛇三视图-第三段-52d51577.mp4"
    kept_video, kept_audio, omitted = _filter_unrequested_media_references(
        source_text=(
            "背景里提到白蛇-白蛇三视图-第三段-52d51577.mp4\n"
            "[Image from KB: 刘邦三视图.png → /api/v1/fs/entity/刘邦三视图.png]"
        ),
        reference_video_urls=[video_url],
        audio_reference_urls=[],
    )

    assert kept_video == []
    assert kept_audio == []
    assert omitted == ["reference_video_urls"]


def test_hash_selected_video_reference_is_kept():
    from packages.core.ai.tools.extended_tools import _filter_unrequested_media_references

    video_url = "/api/v1/fs/entity/videos/白蛇-白蛇三视图-第三段-52d51577.mp4"
    kept_video, kept_audio, omitted = _filter_unrequested_media_references(
        source_text=(
            "背景： #白蛇-白蛇三视图-第三段-52d51577.mp4\n"
            "[Image from KB: 刘邦三视图.png → /api/v1/fs/entity/刘邦三视图.png]"
        ),
        reference_video_urls=[video_url],
        audio_reference_urls=[],
    )

    assert kept_video == [video_url]
    assert kept_audio == []
    assert omitted == []


def test_explicit_user_video_and_audio_references_are_kept():
    from packages.core.ai.tools.extended_tools import _filter_unrequested_media_references

    video_url = "/api/v1/fs/entity/uploads/motion.mp4"
    audio_url = "/api/v1/fs/entity/uploads/刘邦声音(2).mp3"
    kept_video, kept_audio, omitted = _filter_unrequested_media_references(
        source_text=("[Video from KB: motion.mp4 → /api/v1/fs/entity/uploads/motion.mp4]\n刘邦声音： #刘邦声音(2).mp3"),
        reference_video_urls=[video_url],
        audio_reference_urls=[audio_url],
    )

    assert kept_video == [video_url]
    assert kept_audio == [audio_url]
    assert omitted == []


def test_unmentioned_visual_reference_is_filtered_from_tool_args():
    from packages.core.ai.tools.extended_tools import _filter_unmentioned_visual_references

    first, last, refs, omitted = _filter_unmentioned_visual_references(
        source_text="[Image from KB: hero.png → /api/v1/fs/entity/uploads/hero.png]",
        first_frame_url="/api/v1/fs/entity/uploads/hero.png",
        last_frame_url="/api/v1/fs/entity/uploads/hidden-end.png",
        reference_urls=[
            "/api/v1/fs/entity/uploads/hero-style.png",
            "/api/v1/fs/entity/uploads/hidden-style.png",
        ],
    )

    assert first == "/api/v1/fs/entity/uploads/hero.png"
    assert last == ""
    assert refs == []
    assert omitted == ["last_frame_url", "reference_urls"]


def test_hash_mention_ends_at_the_file_name_in_chinese_text():
    """A Workflow prompt wrote `#presenter-master.png（主持人定妆照）` and every
    first frame was rejected as unselected."""
    from packages.core.ai.tools.extended_tools import _reference_selected_by_user

    url = "/api/v1/fs/entity/uploads/品牌/presenter-master.png"
    for text in (
        "参考图：#presenter-master.png（固定主持人定妆照）",
        "用 #presenter-master.png、#owner.png",
        "#presenter-master.png：首帧",
        "用#presenter-master.png做首帧",
        "use #presenter-master.png.",
        "#presenter-master.png",
    ):
        assert _reference_selected_by_user(text, url), text
    for text in ("#presenter-master.png.bak", "#presenter-master.pngx", "#presenter-master-v2.png"):
        assert not _reference_selected_by_user(text, url), text


def test_runtime_generated_visual_references_are_kept_without_user_hash_selection():
    from packages.core.ai.tools.extended_tools import _filter_unmentioned_visual_references

    first, last, refs, omitted = _filter_unmentioned_visual_references(
        source_text="生成视频，不要使用其它素材",
        first_frame_url="/api/v1/fs/entity/generated/start.png",
        last_frame_url="/api/v1/fs/entity/generated/end.png",
        reference_urls=[
            "/api/v1/fs/entity/generated/style.png",
            "/api/v1/fs/entity/uploads/not-selected.png",
        ],
        allowed_reference_urls=[
            "https://manor.test/api/v1/fs/entity/generated/start.png",
            "/api/v1/fs/entity/generated/end.png",
            "/api/v1/fs/entity/generated/style.png",
        ],
    )

    assert first == "/api/v1/fs/entity/generated/start.png"
    assert last == "/api/v1/fs/entity/generated/end.png"
    assert refs == ["/api/v1/fs/entity/generated/style.png"]
    assert omitted == ["reference_urls"]


def test_runtime_generated_video_and_audio_references_are_kept_without_user_hash_selection():
    from packages.core.ai.tools.extended_tools import _filter_unrequested_media_references

    video_url = "/api/v1/fs/entity/generated/scene-01.mp4"
    audio_url = "/api/v1/fs/entity/generated/voice.wav"
    kept_video, kept_audio, omitted = _filter_unrequested_media_references(
        source_text="继续做下一段镜头",
        reference_video_urls=[
            video_url,
            "/api/v1/fs/entity/uploads/not-selected.mp4",
        ],
        audio_reference_urls=[audio_url],
        allowed_reference_urls=[
            video_url,
            "https://manor.test/api/v1/fs/entity/generated/voice.wav",
        ],
    )

    assert kept_video == [video_url]
    assert kept_audio == [audio_url]
    assert omitted == ["reference_video_urls"]


def test_inline_image_reference_urls_are_extracted_from_chat_markers():
    from packages.core.ai.tools.extended_tools import _extract_inline_image_reference_urls

    refs = _extract_inline_image_reference_urls(
        "生成视频\n"
        "[Image: start.png -> /api/v1/fs/entity/uploads/chat/start.png]\n"
        "[Image from KB: end.png → https://cdn.example.test/end.png]"
    )

    assert refs == [
        "/api/v1/fs/entity/uploads/chat/start.png",
        "https://cdn.example.test/end.png",
    ]


def test_inline_video_references_ignore_prompt_only_images_when_user_message_exists():
    from packages.core.ai.tools.extended_tools import _apply_inline_video_references

    first, last, refs, inferred = _apply_inline_video_references(
        prompt=("参考隐藏素材生成视频\n[Image from KB: hidden.png → /api/v1/fs/entity/uploads/chat/hidden.png]"),
        active_user_message="生成视频，不要使用其它素材",
    )

    assert first == ""
    assert last == ""
    assert refs == []
    assert inferred is None


def test_inline_video_references_infer_first_last_frames():
    from packages.core.ai.tools.extended_tools import _apply_inline_video_references

    first, last, refs, inferred = _apply_inline_video_references(
        prompt="用首尾帧生成一段海边镜头",
        active_user_message=(
            "[Image: start.png -> /api/v1/fs/entity/uploads/chat/start.png]\n"
            "[Image: end.png -> /api/v1/fs/entity/uploads/chat/end.png]\n"
            "[Image: style.png -> /api/v1/fs/entity/uploads/chat/style.png]"
        ),
    )

    assert first.endswith("start.png")
    assert last.endswith("end.png")
    assert refs == ["/api/v1/fs/entity/uploads/chat/style.png"]
    assert inferred == {
        "source": "active_user_message_inline_images",
        "inline_urls": [
            "/api/v1/fs/entity/uploads/chat/start.png",
            "/api/v1/fs/entity/uploads/chat/end.png",
            "/api/v1/fs/entity/uploads/chat/style.png",
        ],
        "first_frame_url": "/api/v1/fs/entity/uploads/chat/start.png",
        "last_frame_url": "/api/v1/fs/entity/uploads/chat/end.png",
        "reference_urls": ["/api/v1/fs/entity/uploads/chat/style.png"],
    }


def test_inline_video_references_infer_reference_images():
    from packages.core.ai.tools.extended_tools import _apply_inline_video_references

    first, last, refs, inferred = _apply_inline_video_references(
        prompt="参考这些角色素材，保持角色一致生成视频",
        active_user_message=(
            "[Image: hero.png → /api/v1/fs/entity/uploads/chat/hero.png]\n"
            "[Image: armor.png → /api/v1/fs/entity/uploads/chat/armor.png]"
        ),
    )

    assert first == ""
    assert last == ""
    assert refs == [
        "/api/v1/fs/entity/uploads/chat/hero.png",
        "/api/v1/fs/entity/uploads/chat/armor.png",
    ]
    assert inferred and inferred["reference_urls"] == refs


def test_video_reference_intent_without_actual_reference_is_blocked():
    from packages.core.ai.tools.extended_tools import _video_missing_reference_error

    error = _video_missing_reference_error(
        prompt="Use the same character reference and first frame style to make this shot.",
        active_user_message="",
        requires_reference_media=True,
    )

    assert error is not None
    assert "no media reference URL was passed" in error
    assert "reference_urls" in error


def test_text_to_video_reference_words_do_not_require_media_urls():
    from packages.core.ai.tools.extended_tools import _video_missing_reference_error

    error = _video_missing_reference_error(
        prompt=(
            "Create a vertical FIFA World Cup results video from verified reference data. "
            "Use football broadcast style, bold typography, stadium atmosphere, and subtitles."
        ),
        active_user_message="生成世界杯赛果介绍视频，先搜索比分和进球球员。",
    )

    assert error is None


def test_video_prompt_audio_policy_defaults_to_silent_picture():
    from packages.core.ai.tools.extended_tools import _apply_video_audio_policy_to_prompt

    prompt = _apply_video_audio_policy_to_prompt(
        "Camera slowly pushes in on the product hero.",
        generate_audio=False,
        audio_reference_urls=[],
    )

    assert "silent picture only" in prompt
    assert "background music" in prompt
    assert "Final dialogue, BGM, ambience, SFX, and subtitles" in prompt


@pytest.mark.asyncio
async def test_openrouter_local_image_reference_skips_public_url_preflight(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def resolve_model(_user_id, _entity_id):
        return "bytedance/seedance-2.0"

    async def resolve_credentials(_user_id, _entity_id, role):
        assert role == "video"
        return "sk-or-user-key", "", True

    def unexpected_public_preflight(*_args, **_kwargs):
        raise AssertionError("OpenRouter image references should not require PUBLIC_BASE_URL")

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", resolve_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", resolve_credentials)
    monkeypatch.setattr(extended_tools, "_video_reference_public_base_error", unexpected_public_preflight)
    monkeypatch.setattr(
        extended_tools,
        "_video_capability_error",
        lambda **_kwargs: "stop after reference preflight",
    )

    result = json.loads(
        await extended_tools._generate_video_handler(
            entity_id="entity",
            user_id="user",
            prompt="Animate the supplied stick figure.",
            first_frame_url="/api/v1/fs/entity/images/stickman.png",
            duration=5,
            generate_audio=False,
        )
    )

    assert result["status"] == "failed"
    assert result["error"] == "stop after reference preflight"


@pytest.mark.asyncio
async def test_vercel_local_video_reference_requires_public_url_preflight(monkeypatch):
    from packages.core.ai.tools import extended_tools
    from packages.core.services.model_gateway import ModelGatewayRoute

    reference_video_url = "/api/v1/fs/entity/videos/motion.mp4"

    async def resolve_model(_user_id, _entity_id):
        return "bytedance/seedance-2.0"

    async def resolve_credentials(_user_id, _entity_id, role):
        assert role == "video"
        return "sk-or-user-key", "", True

    async def resolve_vercel_route(*_args, **_kwargs):
        return ModelGatewayRoute(
            api_key="vck-managed-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source="official",
            source_detail="test",
        )

    def public_preflight(references, _entity_id):
        assert references == [reference_video_url]
        return "video references need a signed public URL"

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", resolve_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", resolve_credentials)
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", resolve_vercel_route)
    monkeypatch.setattr(extended_tools, "_video_reference_public_base_error", public_preflight)
    monkeypatch.setattr(
        extended_tools,
        "_video_capability_error",
        lambda **_kwargs: "stop after reference preflight",
    )

    result = json.loads(
        await extended_tools._generate_video_handler(
            entity_id="entity",
            user_id="user",
            prompt="Use the motion clip as a video reference.",
            reference_video_urls=[reference_video_url],
            duration=5,
            generate_audio=False,
            route_provider="vercel",
        )
    )

    assert result["status"] == "failed"
    assert result["error"] == "video references need a signed public URL"


def test_video_local_reference_requires_https_public_base(monkeypatch, tmp_path):
    from packages.core import config as core_config
    from packages.core.ai.tools.extended_tools import _video_reference_public_base_error
    from packages.core.services import entity_fs

    entity_root = tmp_path / "entity"
    entity_root.mkdir()
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))
    monkeypatch.setattr(
        core_config,
        "get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="http://localhost:8000"),
    )

    error = _video_reference_public_base_error(
        ["/api/v1/fs/entity/uploads/chat/frame.png"],
        "entity",
    )

    assert error is not None
    assert "PUBLIC_BASE_URL" in error
    assert "HTTPS" in error


def test_video_public_reference_does_not_require_public_base(monkeypatch, tmp_path):
    from packages.core import config as core_config
    from packages.core.ai.tools.extended_tools import _video_reference_public_base_error
    from packages.core.services import entity_fs

    entity_root = tmp_path / "entity"
    entity_root.mkdir()
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))
    monkeypatch.setattr(
        core_config,
        "get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="http://localhost:8000"),
    )

    assert _video_reference_public_base_error(["https://cdn.example.test/frame.png"], "entity") is None


def test_snapshot_trims_overlong_local_reference_video(monkeypatch, tmp_path):
    from packages.core.services import entity_fs
    from packages.core.tasks import media_tasks

    entity_root = tmp_path / "entity"
    source = entity_root / "clips" / "motion.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source-video")
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    source_abs = str(source.resolve())

    def fake_duration(path: str) -> float:
        return 15.09 if str(Path(path).resolve()) == source_abs else 14.9

    captured: dict[str, object] = {}

    def fake_trim(source_path: str, target_path: str, *, target_seconds: float) -> None:
        captured["source_path"] = source_path
        captured["target_path"] = target_path
        captured["target_seconds"] = target_seconds
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        Path(target_path).write_bytes(b"trimmed-video")

    monkeypatch.setattr(media_tasks, "_video_reference_duration_seconds", fake_duration)
    monkeypatch.setattr(media_tasks, "_trim_local_reference_video", fake_trim)

    result = media_tasks.snapshot_video_reference_urls(
        entity_id="entity",
        job_id="job_refs",
        reference_video_urls=["/api/v1/fs/entity/clips/motion.mp4"],
    )

    [ref_url] = result["reference_video_urls"]
    assert ref_url.startswith("/api/v1/fs/entity/uploads/media-references/job_refs/00-reference-video-1-")
    target_rel = ref_url.split("/api/v1/fs/entity/", 1)[1]
    target = entity_root / target_rel
    assert target.read_bytes() == b"trimmed-video"
    assert captured["source_path"] == source_abs
    assert captured["target_seconds"] == media_tasks.VIDEO_REFERENCE_TRIM_SECONDS


def test_video_model_capabilities_are_cataloged():
    from packages.core.constants.models import CATALOG, video_model_capabilities

    seedance = video_model_capabilities("bytedance/seedance-2.0")
    seedance_v1_fast = video_model_capabilities("bytedance/seedance-v1.0-pro-fast")
    kling = video_model_capabilities("kwaivgi/kling-v3.0-pro")

    assert seedance["first_frame"] is True
    assert seedance["last_frame"] is True
    assert seedance["reference_images"] is True
    assert seedance["max_reference_images"] == 9
    assert seedance["reference_videos"] is True
    assert seedance["max_reference_videos"] == 3
    assert seedance["audio_reference"] is True
    assert seedance["max_audio_references"] == 3
    assert seedance_v1_fast["first_frame"] is True
    assert seedance_v1_fast["last_frame"] is False
    assert seedance_v1_fast["reference_images"] is False
    assert seedance_v1_fast["native_audio"] is False
    assert kling["first_frame"] is True
    assert kling["last_frame"] is False
    assert kling["reference_images"] is False
    assert kling["reference_videos"] is False
    assert kling["audio_reference"] is False
    assert all("capabilities" in item for item in CATALOG["video"])


def test_video_model_capabilities_publish_only_known_duration_schemas():
    from packages.core.constants.models import video_model_capabilities

    seedance_2 = video_model_capabilities("bytedance/seedance-2.0")
    seedance_v1 = video_model_capabilities("bytedance/seedance-v1.0-pro-fast")
    unknown = video_model_capabilities("custom/video-model")

    assert "generation_schema" not in seedance_2
    assert "generation_schema" not in unknown
    assert seedance_v1["generation_schema"]["duration"]["image_to_video"] == {
        "type": "integer",
        "enum": [4, 5, 6, 7, 8, 9, 10, 11, 12],
        "default": 5,
    }


@pytest.mark.asyncio
async def test_video_segment_planner_uses_selected_model_schema(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def selected_model(*_args, **_kwargs):
        return "bytedance/seedance-v1.0-pro-fast"

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", selected_model)

    result = json.loads(
        await extended_tools._plan_video_segments_handler(
            entity_id="entity-1",
            user_id="user-1",
            target_duration_seconds=120,
            generation_mode="image_to_video",
        )
    )

    assert result["status"] == "completed"
    assert result["model"] == "bytedance/seedance-v1.0-pro-fast"
    assert result["duration_schema_source"] == "model_schema"
    assert result["duration_schema"]["enum"] == [4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert result["generation_durations_seconds"] == [12] * 10
    assert result["segment_durations_seconds"] == [12] * 10
    assert result["generated_duration_seconds"] == 120
    assert result["trim_seconds"] == 0
    assert result["total_duration_seconds"] == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("target_duration", [None, "not-a-duration", 4.5])
async def test_video_segment_planner_blocks_invalid_target_duration(target_duration):
    from packages.core.ai.tools import extended_tools

    result = json.loads(
        await extended_tools._plan_video_segments_handler(
            target_duration_seconds=target_duration,
        )
    )

    assert result["status"] == "blocked"
    assert "whole number" in result["error"]


def test_video_capability_validation_rejects_duration_outside_selected_model_schema():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    error = _video_capability_error(
        model="bytedance/seedance-v1.0-pro-fast",
        prompt="Animate this accepted scene.",
        first_frame_url="/api/v1/fs/entity/scene.png",
        duration=15,
    )

    assert error is not None
    assert "image_to_video duration schema" in error
    assert "4, 5, 6, 7, 8, 9, 10, 11, 12" in error


@pytest.mark.parametrize("duration", [3, 4.5, "4.5s"])
def test_video_capability_validation_does_not_silently_coerce_schema_duration(duration):
    from packages.core.ai.tools.extended_tools import _video_capability_error

    error = _video_capability_error(
        model="bytedance/seedance-v1.0-pro-fast",
        prompt="Animate this accepted scene.",
        first_frame_url="/api/v1/fs/entity/scene.png",
        duration=duration,
    )

    assert error is not None
    assert f"received {duration}" in error


@pytest.mark.asyncio
async def test_video_segment_planner_uses_common_durations_without_model_schema(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def selected_model(*_args, **_kwargs):
        return "custom/video-model"

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", selected_model)

    result = json.loads(
        await extended_tools._plan_video_segments_handler(
            entity_id="entity-1",
            user_id="user-1",
            target_duration_seconds=121,
            generation_mode="image_to_video",
        )
    )

    assert result["status"] == "completed"
    assert result["model"] == "custom/video-model"
    assert result["duration_schema_source"] == "compatibility_fallback"
    assert result["duration_schema"]["enum"] == [5, 10]
    assert result["generation_durations_seconds"] == [10] * 12 + [5]
    assert result["segment_durations_seconds"] == [10] * 11 + [6, 5]
    assert result["generated_duration_seconds"] == 125
    assert result["trim_seconds"] == 4
    assert sum(result["segment_durations_seconds"]) == 121


@pytest.mark.asyncio
async def test_video_segment_planner_supports_integer_duration_range(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def selected_model(*_args, **_kwargs):
        return "range/video-model"

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", selected_model)
    monkeypatch.setattr(
        extended_tools,
        "_video_duration_schema",
        lambda *_args: {"type": "integer", "minimum": 4, "maximum": 6},
    )

    result = json.loads(
        await extended_tools._plan_video_segments_handler(
            entity_id="entity-1",
            user_id="user-1",
            target_duration_seconds=12,
            generation_mode="image_to_video",
        )
    )

    assert result["duration_schema_source"] == "model_schema"
    assert result["generation_durations_seconds"] == [6, 6]
    assert result["segment_durations_seconds"] == [6, 6]


def test_video_capability_validation_enforces_integer_duration_range(monkeypatch):
    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(
        extended_tools,
        "_video_duration_schema",
        lambda *_args: {"type": "integer", "minimum": 4, "maximum": 6},
    )

    assert extended_tools._video_capability_error(
        model="range/video-model",
        prompt="Animate this accepted scene.",
        first_frame_url="/api/v1/fs/entity/scene.png",
        duration=6,
    ) is None
    error = extended_tools._video_capability_error(
        model="range/video-model",
        prompt="Animate this accepted scene.",
        first_frame_url="/api/v1/fs/entity/scene.png",
        duration=7,
    )
    assert error is not None
    assert "4, 5, 6" in error


@pytest.mark.asyncio
async def test_video_segment_planner_does_not_truncate_decimal_enum_values(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def selected_model(*_args, **_kwargs):
        return "decimal/video-model"

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", selected_model)
    monkeypatch.setattr(
        extended_tools,
        "_video_duration_schema",
        lambda *_args: {"type": "integer", "enum": [4.5, 10.5]},
    )

    result = json.loads(
        await extended_tools._plan_video_segments_handler(
            entity_id="entity-1",
            user_id="user-1",
            target_duration_seconds=11,
            generation_mode="image_to_video",
        )
    )

    assert result["duration_schema_source"] == "compatibility_fallback"
    assert result["duration_schema"]["enum"] == [5, 10]
    assert result["generation_durations_seconds"] == [10, 5]
    assert result["segment_durations_seconds"] == [6, 5]


@pytest.mark.asyncio
async def test_unrecognized_duration_schema_uses_nonbinding_fallback(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def selected_model(*_args, **_kwargs):
        return "unrecognized/video-model"

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", selected_model)
    monkeypatch.setattr(
        extended_tools,
        "_video_duration_schema",
        lambda *_args: {"type": "number", "minimum": 4, "maximum": 12},
    )

    result = json.loads(
        await extended_tools._plan_video_segments_handler(
            entity_id="entity-1",
            user_id="user-1",
            target_duration_seconds=11,
            generation_mode="image_to_video",
        )
    )
    capability_error = extended_tools._video_capability_error(
        model="unrecognized/video-model",
        prompt="Animate this accepted scene.",
        first_frame_url="/api/v1/fs/entity/scene.png",
        duration=7,
    )

    assert result["duration_schema_source"] == "compatibility_fallback"
    assert result["duration_schema"]["enum"] == [5, 10]
    assert capability_error is None


def test_video_capability_validation_does_not_invent_unknown_model_limits():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    assert _video_capability_error(
        model="custom/video-model",
        prompt="Animate this accepted scene.",
        first_frame_url="/api/v1/fs/entity/scene.png",
        duration=7,
    ) is None


@pytest.mark.asyncio
async def test_generate_video_rejects_changed_planned_model_before_credentials(monkeypatch):
    from packages.core.ai.tools import extended_tools

    calls: list[str] = []

    async def selected_model(*_args, **_kwargs):
        return "bytedance/seedance-2.0"

    async def unexpected_credentials(*_args, **_kwargs):
        calls.append("credentials")
        raise AssertionError("model fence must run before credential lookup")

    async def unexpected_credit(*_args, **_kwargs):
        calls.append("credit")
        raise AssertionError("model fence must run before credit admission")

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", selected_model)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_media_credentials",
        unexpected_credentials,
    )
    monkeypatch.setattr(
        extended_tools,
        "runtime_assert_credit_available",
        unexpected_credit,
    )

    result = json.loads(
        await extended_tools._generate_video_handler(
            entity_id="entity-1",
            user_id="user-1",
            prompt="Animate this accepted scene.",
            expected_model="kwaivgi/kling-v3.0-std",
        )
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "video_model_changed_replan_required"
    assert result["expected_model"] == "kwaivgi/kling-v3.0-std"
    assert result["model"] == "bytedance/seedance-2.0"
    assert calls == []


@pytest.mark.asyncio
async def test_generate_video_accepts_matching_planned_model(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def selected_model(*_args, **_kwargs):
        return "current/video-model"

    async def resolve_credentials(*_args, **_kwargs):
        return "", "", False

    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", selected_model)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_media_credentials",
        resolve_credentials,
    )

    result = json.loads(
        await extended_tools._generate_video_handler(
            entity_id="entity-1",
            user_id="user-1",
            prompt="Animate this accepted scene.",
            expected_model="current/video-model",
        )
    )

    assert result["status"] == "failed"
    # Past the planned-model gate it stops at credentials: a model outside the
    # catalog runs only on the Account's own key.
    assert "runs only on your own API key" in result["error"]
    assert result["model"] == "current/video-model"
    assert "error_code" not in result


def test_seedance_prefers_native_official_key_over_platform_openrouter(monkeypatch):
    from packages.core.ai.tools import extended_tools

    def fake_native_key(provider):
        return "ark-native-seedance-key" if provider == "bytedance" else ""

    monkeypatch.setattr(extended_tools, "_platform_native_media_key", fake_native_key)

    api_key, is_byok = extended_tools._prefer_native_video_credentials(
        "sk-or-platform-openrouter-key",
        "bytedance",
        False,
    )

    assert api_key == "ark-native-seedance-key"
    assert is_byok is False


def test_seedance_openrouter_downgrades_native_only_inputs():
    from packages.core.ai.tools.extended_tools import (
        _seedance_openrouter_downgrade_warning,
        _seedance_openrouter_native_only_inputs,
    )

    omitted = _seedance_openrouter_native_only_inputs(
        provider="bytedance",
        api_key="sk-or-platform-openrouter-key",
        reference_video_urls=["/api/v1/fs/entity/motion.mp4"],
        audio_reference_urls=["/api/v1/fs/entity/dialogue.wav"],
        generate_audio=True,
    )

    assert omitted == [
        "reference_video_urls",
        "audio_reference_urls",
        "generate_audio",
    ]
    warning = _seedance_openrouter_downgrade_warning(omitted)
    assert "Current credentials resolve to OpenRouter" in warning
    assert "silent picture clip" in warning


def test_seedance_native_route_keeps_native_only_inputs_available():
    from packages.core.ai.tools.extended_tools import _seedance_openrouter_native_only_inputs

    omitted = _seedance_openrouter_native_only_inputs(
        provider="bytedance",
        api_key="ark-native-seedance-key",
        reference_video_urls=["/api/v1/fs/entity/motion.mp4"],
        audio_reference_urls=["/api/v1/fs/entity/dialogue.wav"],
        generate_audio=True,
    )

    assert omitted == []


def test_video_byok_stays_authoritative_when_native_key_exists(monkeypatch):
    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(
        extended_tools,
        "_platform_native_media_key",
        lambda _provider: "ark-platform-native-key",
    )

    api_key, is_byok = extended_tools._prefer_native_video_credentials(
        "ark-user-byok-key",
        "bytedance",
        True,
    )

    assert api_key == "ark-user-byok-key"
    assert is_byok is True


def test_video_capability_validation_rejects_kling_unsupported_refs():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    error = _video_capability_error(
        model="kwaivgi/kling-v3.0-std",
        prompt="Create a silent beach landing shot.",
        first_frame_url="/api/v1/fs/entity/start.png",
        last_frame_url="/api/v1/fs/entity/end.png",
        reference_urls=["/api/v1/fs/entity/style.png"],
    )

    assert error is not None
    assert "last_frame_url" in error
    assert "reference_urls" in error
    assert "first_frame_url" in error


def test_video_capability_validation_rejects_too_many_seedance_refs():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    error = _video_capability_error(
        model="bytedance/seedance-2.0",
        prompt="Create a silent beach landing shot.",
        reference_urls=[f"/api/v1/fs/entity/ref-{idx}.png" for idx in range(10)],
    )

    assert error is not None
    assert "at most 9" in error


def test_video_capability_validation_allows_seedance_audio_and_video_references():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    error = _video_capability_error(
        model="bytedance/seedance-2.0",
        prompt="Create a palace dialogue shot using this audio reference.",
        reference_urls=["/api/v1/fs/entity/character.png"],
        reference_video_urls=["/api/v1/fs/entity/motion.mp4"],
        audio_reference_urls=["/api/v1/fs/entity/dialogue.wav"],
    )

    assert error is None


def test_video_capability_validation_rejects_seedance_audio_reference_without_visual():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    error = _video_capability_error(
        model="bytedance/seedance-2.0",
        prompt="Create a palace dialogue shot using this audio reference.",
        audio_reference_urls=["/api/v1/fs/entity/dialogue.wav"],
    )

    assert error is not None
    assert "paired with at least one image or video reference" in error


def test_video_capability_validation_allows_narration_for_post_warning():
    from packages.core.ai.tools.extended_tools import (
        _prompt_requests_video_post_asset,
        _video_capability_error,
        _video_post_production_warning,
    )

    error = _video_capability_error(
        model="bytedance/seedance-2.0",
        prompt="Generate a 5 second clip with Chinese narration over the scene.",
    )

    assert error is None
    assert (
        _prompt_requests_video_post_asset("Generate a 5 second clip with Chinese narration over the scene.")
        == "narration"
    )
    assert 'generate_file(kind="audio")' in _video_post_production_warning("narration")


def test_native_dialogue_model_keeps_spoken_lines_in_the_clip():
    """Grok speaks the prompt's lines in lip sync; a "leave dialogue for post"
    note made it mouth gibberish instead."""
    from packages.core.ai.tools.extended_tools import (
        _prompt_requests_video_post_asset,
        _video_native_audio_assets,
    )

    prompt = (
        "The host says in Mandarin: 「你看到一盘没吃完的肉」. "
        "Her lips move in sync with her speech. No text, no subtitles."
    )

    grok_native = _video_native_audio_assets("xai/grok-imagine-video-1.5", True)
    assert _prompt_requests_video_post_asset(prompt, native=grok_native) == ""
    # Native audio off, or a model without native dialogue: dialogue stays post work.
    assert _video_native_audio_assets("xai/grok-imagine-video-1.5", False) == frozenset()
    seedance_native = _video_native_audio_assets("bytedance/seedance-2.0", True)
    assert _prompt_requests_video_post_asset(prompt, native=seedance_native) == "dialogue"
    # Subtitles are never native.
    assert (
        _prompt_requests_video_post_asset("Generate burned-in subtitles.", native=grok_native)
        == "subtitles"
    )


def test_video_capability_validation_allows_silent_negative_audio_prompt():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    assert (
        _video_capability_error(
            model="bytedance/seedance-2.0",
            prompt="Create a silent clip: no music, no narration, no subtitles.",
            generate_audio=False,
        )
        is None
    )


def test_video_capability_validation_allows_positive_audio_for_post_warning():
    from packages.core.ai.tools.extended_tools import (
        _prompt_requests_video_post_asset,
        _video_capability_error,
    )

    error = _video_capability_error(
        model="bytedance/seedance-2.0",
        prompt="Create a silent clip with no music, but add Chinese narration.",
    )

    assert error is None
    assert (
        _prompt_requests_video_post_asset("Create a silent clip with no music, but add Chinese narration.")
        == "narration"
    )


def test_video_capability_validation_kling_native_audio_needs_pro_mode():
    # Kling v3.0 generates sound only in pro mode (@ai-sdk/klingai `sound`).
    from packages.core.ai.tools.extended_tools import _video_capability_error

    std_error = _video_capability_error(
        model="kwaivgi/kling-v3.0-std",
        prompt="Create a cinematic beach landing shot.",
        generate_audio=True,
    )
    pro_error = _video_capability_error(
        model="kwaivgi/kling-v3.0-pro",
        prompt="Create a cinematic beach landing shot.",
        generate_audio=True,
    )

    assert std_error is not None
    assert "native video audio" in std_error
    assert pro_error is None


def test_video_capability_validation_allows_seedance_native_audio_without_reference():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    error = _video_capability_error(
        model="bytedance/seedance-2.0",
        prompt="Create a cinematic dialogue shot.",
        generate_audio=True,
        reference_urls=["/api/v1/fs/entity/character.png"],
    )

    assert error is None


def test_video_capability_validation_allows_bgm_for_post_warning():
    from packages.core.ai.tools.extended_tools import (
        _video_capability_error,
        _video_post_production_warning,
    )

    error = _video_capability_error(
        model="bytedance/seedance-2.0",
        prompt="Create a cinematic shot with background music and ambience.",
        generate_audio=True,
        reference_urls=["/api/v1/fs/entity/character.png"],
        audio_reference_urls=["/api/v1/fs/entity/dialogue.wav"],
    )

    assert error is None
    warning = _video_post_production_warning("music")
    assert 'generate_file(kind="audio")' in warning
    assert "compose_video_timeline" in warning


@pytest.mark.asyncio
async def test_seedance_native_payload_omits_reference_media_when_frames_present(monkeypatch):
    from packages.core.tasks import media_tasks

    captured: dict = {}

    async def fake_ensure_public_url(url, entity_id, **_kwargs):
        return f"https://cdn.example.test/{entity_id}/{url.rsplit('/', 1)[-1]}"

    async def fake_download_and_save(*_args, **_kwargs):
        return {"result_url": "/api/v1/fs/entity/final.mp4"}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"content": {"video_url": "https://cdn.example.test/result.mp4"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_ensure_public_url)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download_and_save)
    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeClient)

    job = SimpleNamespace(
        id="job_1",
        model="bytedance/seedance-2.0",
        prompt="Generate a dialogue shot using the provided references.",
        entity_id="entity",
        params={
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "first_frame_url": "/api/v1/fs/entity/start.png",
            "last_frame_url": "/api/v1/fs/entity/end.png",
            "reference_urls": ["/api/v1/fs/entity/style.png"],
            "reference_video_urls": ["/api/v1/fs/entity/motion.mp4"],
            "audio_reference_urls": ["/api/v1/fs/entity/dialogue.wav"],
            "generate_audio": True,
        },
        user_id="user",
        agent_id=None,
        conversation_id=None,
    )

    result = await media_tasks._call_volcengine_seedance_api(job, "ark-test", None)

    assert result["result_url"] == "/api/v1/fs/entity/final.mp4"
    content = captured["payload"]["content"]
    roles_by_type = [(item["type"], item.get("role")) for item in content]
    assert ("image_url", "first_frame") in roles_by_type
    assert ("image_url", "last_frame") in roles_by_type
    assert ("image_url", "reference_image") not in roles_by_type
    assert ("video_url", "reference_video") not in roles_by_type
    assert ("audio_url", "reference_audio") not in roles_by_type
    assert captured["payload"]["generate_audio"] is False


@pytest.mark.asyncio
async def test_seedance_native_payload_includes_image_video_audio_references_without_frames(monkeypatch):
    from packages.core.tasks import media_tasks

    captured: dict = {}

    async def fake_ensure_public_url(url, entity_id, **_kwargs):
        return f"https://cdn.example.test/{entity_id}/{url.rsplit('/', 1)[-1]}"

    async def fake_download_and_save(*_args, **_kwargs):
        return {"result_url": "/api/v1/fs/entity/final.mp4"}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"content": {"video_url": "https://cdn.example.test/result.mp4"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_ensure_public_url)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download_and_save)
    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeClient)

    job = SimpleNamespace(
        id="job_refs",
        model="bytedance/seedance-2.0",
        prompt="Generate a dialogue shot using the provided references.",
        entity_id="entity",
        params={
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "reference_urls": ["/api/v1/fs/entity/style.png"],
            "reference_video_urls": ["/api/v1/fs/entity/motion.mp4"],
            "audio_reference_urls": ["/api/v1/fs/entity/dialogue.wav"],
            "generate_audio": True,
        },
        user_id="user",
        agent_id=None,
        conversation_id=None,
    )

    result = await media_tasks._call_volcengine_seedance_api(job, "ark-test", None)

    assert result["result_url"] == "/api/v1/fs/entity/final.mp4"
    content = captured["payload"]["content"]
    roles_by_type = [(item["type"], item.get("role")) for item in content]
    assert ("image_url", "reference_image") in roles_by_type
    assert ("video_url", "reference_video") in roles_by_type
    assert ("audio_url", "reference_audio") in roles_by_type
    assert captured["payload"]["generate_audio"] is True


@pytest.mark.asyncio
async def test_video_reference_validation_preflights_every_reference(monkeypatch):
    from packages.core.ai.tools.extended_tools import _validate_video_reference_urls_fetchable
    from packages.core.tasks import media_tasks

    seen = []

    async def fake_ensure_public_url(url, entity_id, **kwargs):
        seen.append((url, entity_id, kwargs))
        return f"https://cdn.example.test/{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_ensure_public_url)

    await _validate_video_reference_urls_fetchable(
        entity_id="entity",
        references=[
            "/api/v1/fs/entity/uploads/media-references/job/00-first.png",
            "",
            "/api/v1/fs/entity/uploads/media-references/job/01-last.png",
            "/api/v1/fs/entity/uploads/media-references/job/02-ref.png",
        ],
        public_base_url="https://app.example.test",
    )

    assert [item[0] for item in seen] == [
        "/api/v1/fs/entity/uploads/media-references/job/00-first.png",
        "/api/v1/fs/entity/uploads/media-references/job/01-last.png",
        "/api/v1/fs/entity/uploads/media-references/job/02-ref.png",
    ]
    assert all(item[1] == "entity" for item in seen)
    assert all(item[2]["allow_data_uri"] is False for item in seen)
    assert all(item[2]["public_base_url"] == "https://app.example.test" for item in seen)


@pytest.mark.asyncio
async def test_video_reference_validation_surfaces_preflight_failure(monkeypatch):
    from packages.core.ai.tools.extended_tools import _validate_video_reference_urls_fetchable
    from packages.core.tasks import media_tasks

    async def fake_ensure_public_url(url, entity_id, **kwargs):
        raise RuntimeError("Media reference image is not fetchable")

    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_ensure_public_url)

    with pytest.raises(RuntimeError, match="not fetchable"):
        await _validate_video_reference_urls_fetchable(
            entity_id="entity",
            references=["/api/v1/fs/entity/uploads/media-references/job/00-first.png"],
            public_base_url="https://app.example.test",
        )


# ── Native audio degrades, never detonates ────────────────────────────
#
# A chat user typed "生成一个stickman视频" and got back "Selected video model
# capability mismatch: … does not support native video audio". They never
# asked for audio — the video composer defaults generate_audio on for every
# send, and the selected model (atlascloud/wan-2.2-turbo-spicy) cannot
# speak. A default must not be able to fail the request: the model that
# can't do audio makes a silent video and says so.


def test_default_audio_on_a_silent_model_downgrades_with_a_note():
    from packages.core.ai.tools.extended_tools import (
        _video_capability_error,
        _video_native_audio_downgrade_warning,
    )

    warning = _video_native_audio_downgrade_warning(
        model="atlascloud/wan-2.2-turbo-spicy",
        generate_audio=True,
    )
    assert warning is not None
    assert "silent video" in warning
    assert "compose_video_timeline" in warning

    # Audio itself no longer causes the rejection. Wan is still correctly
    # blocked because its Atlas route is image-to-video only.
    error = _video_capability_error(
        model="atlascloud/wan-2.2-turbo-spicy",
        prompt="生成一个stickman视频",
        generate_audio=False,
    )
    assert error is not None
    assert "image-to-video only" in error
    assert "first_frame_url" in error


def test_atlas_wan_accepts_video_generation_when_first_frame_is_present():
    from packages.core.ai.tools.extended_tools import _video_capability_error

    assert _video_capability_error(
        model="atlascloud/wan-2.2-turbo-spicy",
        prompt="Animate this cat image.",
        first_frame_url="https://cdn.example.test/cat.png",
        generate_audio=False,
    ) is None


def test_native_audio_capable_models_keep_their_audio():
    from packages.core.ai.tools.extended_tools import (
        _video_native_audio_downgrade_warning,
    )
    from packages.core.constants.models import VIDEO_MODEL_CAPABILITIES

    speaking_models = [
        model for model, caps in VIDEO_MODEL_CAPABILITIES.items()
        if caps.get("native_audio")
    ]
    assert speaking_models, "no native-audio models in the table — update this test"
    for model in speaking_models:
        assert _video_native_audio_downgrade_warning(
            model=model, generate_audio=True,
        ) is None


def test_attached_audio_files_still_get_the_hard_error():
    """audio_reference_urls are files the user actually supplied — silently
    ignoring them would be worse than the error, which carries the lip-sync
    guidance. The downgrade must step aside."""
    from packages.core.ai.tools.extended_tools import (
        _video_capability_error,
        _video_native_audio_downgrade_warning,
    )

    assert _video_native_audio_downgrade_warning(
        model="kwaivgi/kling-v3.0-std",
        generate_audio=True,
        audio_reference_urls=["/api/v1/fs/entity/dialogue.wav"],
    ) is None
    error = _video_capability_error(
        model="kwaivgi/kling-v3.0-std",
        prompt="Lip-sync this take.",
        audio_reference_urls=["/api/v1/fs/entity/dialogue.wav"],
    )
    assert error is not None
    assert "audio" in error


def test_silent_requests_are_untouched():
    from packages.core.ai.tools.extended_tools import (
        _video_native_audio_downgrade_warning,
    )

    assert _video_native_audio_downgrade_warning(
        model="atlascloud/wan-2.2-turbo-spicy", generate_audio=False,
    ) is None
    assert _video_native_audio_downgrade_warning(
        model="atlascloud/wan-2.2-turbo-spicy", generate_audio=None,
    ) is None


def test_the_handler_downgrades_before_it_validates():
    """The wiring, not just the helper: the handler must consult the
    downgrade before the capability validator, or the validator fails the
    request first and the helper never runs."""
    import inspect

    from packages.core.ai.tools import extended_tools

    source = inspect.getsource(extended_tools._generate_video_handler)
    downgrade_at = source.find("_video_native_audio_downgrade_warning(")
    validate_at = source.find("_video_capability_error(")
    assert downgrade_at != -1, "the handler no longer consults the downgrade"
    assert validate_at != -1
    assert downgrade_at < validate_at
