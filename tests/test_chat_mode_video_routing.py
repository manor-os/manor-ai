from __future__ import annotations

import asyncio
from pathlib import Path

from apps.api.routers.chat import (
    _VideoEditRouteState,
    _VideoGenerationMode,
    _chat_mode_blocked_tools,
    _chat_mode_direct_tool_calls,
    _chat_mode_runtime_prompt,
    _conversation_video_edit_route_state,
    _message_with_chat_mode_marker,
    _normalize_chat_mode,
    _parse_chat_mode_payload,
    _runtime_metadata_for_chat_mode,
    _stream_llm_message_with_attachments,
)
from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA
from packages.core.services.file_context import FileAttachments


def test_video_chat_mode_all_refs_passes_image_video_and_audio_references():
    attachments = FileAttachments(
        image_urls=[
            "/api/v1/fs/entity/uploads/chat/character.png",
            "/api/v1/fs/entity/uploads/chat/style.png",
        ],
        video_urls=["/api/v1/fs/entity/uploads/chat/motion.mp4"],
        audio_urls=["/api/v1/fs/entity/uploads/chat/dialogue.wav"],
    )

    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "reference_policy": "hash_references",
            "aspect_ratio": "16:9",
            "clip_duration_seconds": 4,
            "audio_policy": "separate_stems",
        },
        prompt="Generate a 4s shot using every reference.",
        attachments=attachments,
    )

    assert calls == [
        {
            "name": "generate_file",
            "arguments": {
                "kind": "video",
                "prompt": "Generate a 4s shot using every reference.",
                "params": {
                    "duration": 4,
                    "aspect_ratio": "16:9",
                    "resolution": "720p",
                    "generate_audio": True,
                    "audio_policy": "native_dialogue_reference_only",
                    "reference_urls": [
                        "/api/v1/fs/entity/uploads/chat/character.png",
                        "/api/v1/fs/entity/uploads/chat/style.png",
                    ],
                    "reference_video_urls": [
                        "/api/v1/fs/entity/uploads/chat/motion.mp4",
                    ],
                    "audio_reference_urls": [
                        "/api/v1/fs/entity/uploads/chat/dialogue.wav",
                    ],
                },
            },
        }
    ]


def test_video_chat_mode_drops_kb_video_not_selected_in_raw_prompt():
    attachments = FileAttachments(
        image_urls=["/api/v1/fs/entity/白蛇三视图.png"],
        video_urls=[
            "/api/v1/fs/entity/videos/白蛇-白蛇三视图-png-第三段-52d51577.mp4",
        ],
        attachment_refs=[
            {
                "kind": "knowledge_document",
                "name": "白蛇三视图.png",
                "mime": "image/png",
                "url": "/api/v1/fs/entity/白蛇三视图.png",
                "image": True,
            },
            {
                "kind": "knowledge_document",
                "name": "白蛇-白蛇三视图-png-第三段-52d51577.mp4",
                "mime": "video/mp4",
                "url": "/api/v1/fs/entity/videos/白蛇-白蛇三视图-png-第三段-52d51577.mp4",
                "video": True,
            },
        ],
    )

    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "reference_policy": "hash_references",
        },
        prompt="背景： #第三段 森林背景.png 大汉甲： #大汉三视图.png #白蛇三视图.png",
        attachments=attachments,
    )

    params = calls[0]["arguments"]["params"]
    assert params["reference_urls"] == ["/api/v1/fs/entity/白蛇三视图.png"]
    assert "reference_video_urls" not in params


def test_video_chat_mode_keeps_hash_selected_kb_video():
    video_url = "/api/v1/fs/entity/videos/motion.mp4"
    attachments = FileAttachments(
        video_urls=[video_url],
        attachment_refs=[
            {
                "kind": "knowledge_document",
                "name": "motion.mp4",
                "mime": "video/mp4",
                "url": video_url,
                "video": True,
            }
        ],
    )

    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "reference_policy": "hash_references",
        },
        prompt="参考 #motion.mp4 生成下一段",
        attachments=attachments,
    )

    params = calls[0]["arguments"]["params"]
    assert params["reference_video_urls"] == [video_url]


def test_video_chat_mode_first_last_uses_frame_fields_not_all_refs():
    attachments = FileAttachments(
        image_urls=[
            "/api/v1/fs/entity/uploads/chat/first.png",
            "/api/v1/fs/entity/uploads/chat/last.png",
            "/api/v1/fs/entity/uploads/chat/style.png",
        ],
        video_urls=["/api/v1/fs/entity/uploads/chat/motion.mp4"],
        audio_urls=["/api/v1/fs/entity/uploads/chat/dialogue.wav"],
    )

    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "reference_policy": "first_last_frames",
            "aspect_ratio": "9:16",
            "clip_duration_seconds": 5,
        },
        prompt="Generate a transition between the two frames.",
        attachments=attachments,
    )

    params = calls[0]["arguments"]["params"]
    assert params["first_frame_url"].endswith("/first.png")
    assert params["last_frame_url"].endswith("/last.png")
    assert "reference_urls" not in params
    assert "reference_video_urls" not in params
    assert "audio_reference_urls" not in params


def test_video_chat_mode_audio_reference_can_be_kept_silent():
    attachments = FileAttachments(
        image_urls=["/api/v1/fs/entity/uploads/chat/character.png"],
        audio_urls=["/api/v1/fs/entity/uploads/chat/dialogue.wav"],
    )

    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "reference_policy": "hash_references",
            "clip_duration_seconds": 4,
            "audio_policy": "silent_visual",
        },
        prompt="Generate a silent visual only.",
        attachments=attachments,
    )

    params = calls[0]["arguments"]["params"]
    assert params["generate_audio"] is False
    assert params["audio_policy"] == "silent_picture_only"
    assert params["audio_reference_urls"] == ["/api/v1/fs/entity/uploads/chat/dialogue.wav"]


def test_video_chat_mode_defaults_to_native_generated_audio():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "reference_policy": "hash_references",
            "clip_duration_seconds": 4,
        },
        prompt="Generate a 4s product shot with natural scene audio.",
        attachments=FileAttachments(),
    )

    params = calls[0]["arguments"]["params"]
    assert params["generate_audio"] is True
    assert params["audio_policy"] == "native_audio"


def test_video_chat_mode_duration_is_clamped_to_single_clip_maximum():
    payload = _parse_chat_mode_payload(
        {"clip_duration_seconds": 30, "reference_policy": "hash_references"},
        "video",
    )

    assert payload["clip_duration_seconds"] == 15
    assert payload["max_single_generation_duration_seconds"] == 15


def test_video_chat_mode_resolution_is_normalized_and_passed_to_tool():
    payload = _parse_chat_mode_payload(
        {
            "generation_mode": "ai_video",
            "clip_duration_seconds": 4,
            "reference_policy": "hash_references",
            "resolution": "1080P",
        },
        "video",
    )
    assert payload["resolution"] == "1080p"

    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload=payload,
        prompt="Generate a crisp 4s product shot.",
        attachments=FileAttachments(),
    )

    assert calls[0]["arguments"]["params"]["resolution"] == "1080p"


def test_video_chat_mode_resolution_rejects_unsupported_4k():
    payload = _parse_chat_mode_payload(
        {
            "clip_duration_seconds": 4,
            "reference_policy": "hash_references",
            "resolution": "4k",
        },
        "video",
    )

    assert payload["resolution"] == "720p"


def test_video_chat_mode_generation_modes_are_normalized():
    assert {mode.value for mode in _VideoGenerationMode} == {
        "auto",
        "native_motion",
        "ai_video",
    }
    assert _parse_chat_mode_payload({}, "video")["generation_mode"] is _VideoGenerationMode.AUTO
    assert (
        _parse_chat_mode_payload({"generation_mode": "auto"}, "video")["generation_mode"]
        is _VideoGenerationMode.AUTO
    )
    assert (
        _parse_chat_mode_payload({"generation_mode": "coded-motion"}, "video")["generation_mode"]
        is _VideoGenerationMode.NATIVE_MOTION
    )
    assert (
        _parse_chat_mode_payload({"generation_mode": "ai"}, "video")["generation_mode"]
        is _VideoGenerationMode.AI_VIDEO
    )


def test_video_chat_mode_auto_and_native_motion_force_video_edit():
    auto_calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={"generation_mode": "auto", "output_type": "single_clip"},
        prompt="Create a cinematic product launch animation.",
        attachments=FileAttachments(),
    )
    assert auto_calls[0]["name"] == "invoke_skill"
    assert auto_calls[0]["arguments"]["skill"] == "video-edit"

    native_calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "native_motion",
            "output_type": "single_clip",
            "aspect_ratio": "9:16",
            "clip_duration_seconds": 12,
        },
        prompt="Create a cinematic product launch animation.",
        attachments=FileAttachments(
            image_urls=["/api/v1/fs/entity/product.png"],
        ),
    )
    assert native_calls[0]["name"] == "invoke_skill"
    assert native_calls[0]["arguments"]["skill"] == "video-edit"
    assert "/api/v1/fs/entity/product.png" in native_calls[0]["arguments"]["input"]
    assert '"aspect_ratio": "9:16"' in native_calls[0]["arguments"]["input"]

    native_prompt = _chat_mode_runtime_prompt("video", {"generation_mode": "native_motion"})
    assert native_prompt is not None
    assert "Do not call a video-generation model" in native_prompt
    assert "video-edit" in native_prompt
    assert "explicit user approval" in native_prompt
    assert "Do not substitute manor.video_edit_recipe" in native_prompt

    auto_prompt = _chat_mode_runtime_prompt("video", {"generation_mode": "auto"})
    assert auto_prompt is not None
    assert "Decide per scene" in auto_prompt
    assert "instead of routing every video request" in auto_prompt
    assert "video-edit" in auto_prompt
    assert "video editing" in auto_prompt
    assert "Do not silently fall back to manor.video_edit_recipe" in auto_prompt


def test_auto_chat_does_not_guess_video_mode_from_keywords():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="auto",
        chat_mode_payload=None,
        prompt="做一个需要产品截图和 UI 操作演示的产品 Demo 视频",
        attachments=FileAttachments(
            image_urls=["/api/v1/fs/entity/product-dashboard.png"],
        ),
    )

    assert calls == []


def test_auto_follow_up_routes_by_live_video_session_enum_without_keywords():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="auto",
        chat_mode_payload=None,
        prompt="继续",
        attachments=FileAttachments(),
        video_edit_route_state=_VideoEditRouteState.SKILL_SANDBOX,
    )
    assert calls == [
        {
            "name": "invoke_skill",
            "arguments": {"skill": "video-edit", "input": "继续"},
        }
    ]


def test_live_owned_video_skill_sandbox_sets_structured_route_state(monkeypatch):
    import packages.core.ai.runtime as runtime_module
    import packages.core.ai.runtime.video_edit_sessions as session_module

    async def no_edit_session(_conversation_id):
        return None

    async def load_skill_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-video-1",
            "skill_id": "video-edit",
            "entity_id": "entity-1",
            "user_id": "user-1",
        }

    async def live_skill(_sandbox_id, _expected_skill):
        return True

    monkeypatch.setattr(
        session_module,
        "load_conversation_video_edit_session",
        no_edit_session,
    )
    monkeypatch.setattr(
        runtime_module,
        "runtime_load_sandbox_context",
        load_skill_context,
    )
    monkeypatch.setattr(
        "apps.api.routers.chat._live_video_sandbox_matches_skill",
        live_skill,
    )

    state = asyncio.run(
        _conversation_video_edit_route_state(
            "conversation-1",
            entity_id="entity-1",
            user_id="user-1",
        )
    )

    assert state is _VideoEditRouteState.SKILL_SANDBOX


def test_video_route_rejects_sandbox_owned_by_another_user(monkeypatch):
    import packages.core.ai.runtime as runtime_module
    import packages.core.ai.runtime.video_edit_sessions as session_module

    async def no_edit_session(_conversation_id):
        return None

    async def load_skill_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-video-1",
            "skill_id": "video-edit",
            "entity_id": "entity-1",
            "user_id": "other-user",
        }

    async def must_not_probe(_sandbox_id, _expected_skill):
        raise AssertionError("an unowned sandbox must not be probed")

    monkeypatch.setattr(
        session_module,
        "load_conversation_video_edit_session",
        no_edit_session,
    )
    monkeypatch.setattr(
        runtime_module,
        "runtime_load_sandbox_context",
        load_skill_context,
    )
    monkeypatch.setattr(
        "apps.api.routers.chat._live_video_sandbox_matches_skill",
        must_not_probe,
    )

    state = asyncio.run(
        _conversation_video_edit_route_state(
            "conversation-1",
            entity_id="entity-1",
            user_id="user-1",
        )
    )

    assert state is _VideoEditRouteState.INACTIVE


def test_chat_clients_reset_mode_while_auto_can_continue_server_session():
    root = Path(__file__).resolve().parents[1]
    embedded = (root / "apps/web/src/components/EmbeddedChat.tsx").read_text()
    floating = (root / "apps/web/src/components/FloatingChat.tsx").read_text()
    brief = (root / "apps/web/src/components/ChatModeBriefPanel.tsx").read_text()

    assert "const resetChatModeAfterTurn" in embedded
    assert "const resetChatModeAfterTurn" in floating
    assert 'const requestChatMode = chatMode === "auto" ? undefined : chatMode;' in embedded
    assert '!editorLiveSessionActive && chatMode !== "auto"' in floating
    assert "if (requestChatMode) resetChatModeAfterTurn();" in embedded
    assert "if (requestChatMode) resetChatModeAfterTurn();" in floating
    assert "getPersistedChatModeState" not in embedded
    assert "getPersistedChatModeState" not in floating
    assert "getPersistedChatModeState" not in brief


def test_video_chat_mode_ai_video_keeps_direct_generation_and_wait_contract():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "output_type": "single_clip",
        },
        prompt="Generate a photoreal cinematic tracking shot.",
        attachments=FileAttachments(),
    )
    assert calls[0]["arguments"]["kind"] == "video"

    prompt = _chat_mode_runtime_prompt("video", {"generation_mode": "ai_video"})
    assert prompt is not None
    assert "generate_file(kind='video')" in prompt
    assert "wait_media_jobs" in prompt


def test_chat_mode_saved_marker_does_not_persist_raw_settings_json():
    saved = _message_with_chat_mode_marker(
        "Generate a short product video.",
        "video",
        {
            "output_type": "single_clip",
            "aspect_ratio": "16:9",
            "clip_duration_seconds": 15,
            "reference_policy": "hash_references",
        },
    )

    assert saved == "Generate a short product video.\n[Mode: video]"
    assert "Mode settings" not in saved
    assert "reference_policy" not in saved


def test_image_chat_mode_directly_calls_image_generation_with_references():
    attachments = FileAttachments(
        image_urls=[
            "/api/v1/fs/entity/uploads/chat/source.png",
            "/api/v1/fs/entity/uploads/chat/style.png",
        ],
    )

    calls = _chat_mode_direct_tool_calls(
        chat_mode="image",
        chat_mode_payload={
            "task": "edit",
            "aspect_ratio": "9:16",
            "resolution": "4k",
            "text_policy": "typography",
        },
        prompt="Make this a launch poster with the text Manor AI.",
        attachments=attachments,
    )

    assert calls[0]["name"] == "generate_file"
    assert calls[0]["arguments"]["kind"] == "image"
    assert "Typography/text is intentional" in calls[0]["arguments"]["prompt"]
    assert calls[0]["arguments"]["params"] == {
        "aspect_ratio": "9:16",
        "resolution": "4k",
        "input_image_urls": [
            "/api/v1/fs/entity/uploads/chat/source.png",
            "/api/v1/fs/entity/uploads/chat/style.png",
        ],
        "input_fidelity": "high",
    }


def test_audio_chat_mode_directly_calls_audio_generation_with_duration():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="audio",
        chat_mode_payload={
            "purpose": "dialogue_or_narration",
            "clip_duration_seconds": 30,
            "voice": "alloy",
        },
        prompt="Read a warm welcome for the guest.",
        attachments=FileAttachments(),
    )

    assert calls == [
        {
            "name": "generate_file",
            "arguments": {
                "kind": "audio",
                "prompt": "Read a warm welcome for the guest.",
                "params": {
                    "purpose": "narration",
                    "duration_seconds": 30.0,
                    "voice": "alloy",
                },
            },
        }
    ]


def test_slides_full_page_image_mode_uses_llm_tool_parameter_selection():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="slides",
        chat_mode_payload={"render": "full_page_image"},
        prompt="Create a 5-page hotel industry growth deck.",
        attachments=FileAttachments(),
    )

    assert calls == []
    prompt = _chat_mode_runtime_prompt("slides", {"render": "full_page_image"})
    assert prompt is not None
    assert "invoke_skill" in prompt
    assert "pptx" in prompt
    assert "params.render='full_page_image'" in prompt
    assert "params object" in prompt
    assert "generate_file" not in prompt


def test_generate_file_schema_does_not_expose_presentation_render_mode():
    params_schema = GENERATE_FILE_SCHEMA["function"]["parameters"]["properties"]["params"]
    assert "render" not in params_schema["properties"]


def test_auto_mode_does_not_force_presentation_generation_from_payload():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="auto",
        chat_mode_payload={"kind": "presentation", "render": "full_page_image"},
        prompt="Create a visual market deck from generated slide images.",
        attachments=FileAttachments(),
    )

    assert calls == []


def test_flows_mode_routes_through_workspace_flow_tools_without_forcing_media():
    assert _normalize_chat_mode("flows") == "flows"
    assert _chat_mode_direct_tool_calls(
        chat_mode="flows",
        chat_mode_payload=None,
        prompt="Run Create product video in Product Video Studio.",
        attachments=FileAttachments(),
    ) == []

    prompt = _chat_mode_runtime_prompt("flows")

    assert prompt is not None
    assert "search_tools" in prompt
    assert prompt.index("search_tools") < prompt.index("list_workspace_flows")
    assert "list_workspace_flows" in prompt
    assert "start_workspace_flow" in prompt
    assert "do not guess" in prompt.lower()
    assert _message_with_chat_mode_marker("Run it", "flows") == (
        "Run it\n[Mode: flows]"
    )

    assert _runtime_metadata_for_chat_mode(
        FileAttachments(),
        chat_mode="flows",
        chat_mode_prompt=prompt,
        direct_tool_calls=[],
        origin_user_message_id="message-1",
    )["chat_mode"] == "flows"


def test_global_chat_mode_blocks_legacy_workflow_execution_and_authoring() -> None:
    from packages.core.ai.runtime.surfaces import ChatSurface

    auto_blocked = _chat_mode_blocked_tools(
        "auto",
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
    )
    flows_blocked = _chat_mode_blocked_tools(
        "flows",
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
    )

    assert {"run_workflow", "start_workspace_flow"} <= auto_blocked
    assert "start_workspace_flow" not in flows_blocked
    assert {"run_workflow", "create_workflow", "delete_workflow"} <= flows_blocked
    assert {
        "list_workspace_flows",
        "start_workspace_flow",
        "get_workflow_run",
        "cancel_workflow_run",
        "resume_workflow_run",
    }.isdisjoint(flows_blocked)
    assert "search_tools" not in flows_blocked
    assert "invoke_skill" in flows_blocked
    assert "generate_file" in flows_blocked
    assert _chat_mode_blocked_tools(
        "auto",
        surface=ChatSurface.WORKSPACE_CHAT,
    ) == set()


def test_slides_editable_mode_stays_with_llm_planning():
    calls = _chat_mode_direct_tool_calls(
        chat_mode="slides",
        chat_mode_payload={"render": "editable"},
        prompt="Create an editable 5-page hotel industry growth deck.",
        attachments=FileAttachments(),
    )

    assert calls == []


def test_direct_media_chat_mode_does_not_inline_image_blocks_into_llm_message():
    attachments = FileAttachments(
        text_context="image reference saved at /api/v1/fs/entity/uploads/chat/source.png",
        image_urls=["/api/v1/fs/entity/uploads/chat/source.png"],
        image_blocks=[
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + ("a" * 1024)},
            }
        ],
    )
    calls = _chat_mode_direct_tool_calls(
        chat_mode="video",
        chat_mode_payload={
            "generation_mode": "ai_video",
            "reference_policy": "hash_references",
        },
        prompt="Generate a video from this reference.",
        attachments=attachments,
    )

    llm_message = _stream_llm_message_with_attachments(
        "Generate a video from this reference.",
        attachments,
        calls,
    )

    assert isinstance(llm_message, str)
    assert "data:image/png;base64" not in llm_message
    assert "/api/v1/fs/entity/uploads/chat/source.png" in llm_message


def test_normal_chat_with_images_still_uses_multimodal_blocks():
    attachments = FileAttachments(
        image_blocks=[
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,abc"},
            }
        ],
    )

    llm_message = _stream_llm_message_with_attachments("Describe this.", attachments, [])

    assert isinstance(llm_message, list)
    assert llm_message[1]["image_url"]["url"] == "data:image/png;base64,abc"
