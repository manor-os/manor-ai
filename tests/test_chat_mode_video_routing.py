from __future__ import annotations

from apps.api.routers.chat import (
    _chat_mode_direct_tool_calls,
    _chat_mode_runtime_prompt,
    _message_with_chat_mode_marker,
    _parse_chat_mode_payload,
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
        chat_mode_payload={"reference_policy": "hash_references"},
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
        chat_mode_payload={"reference_policy": "hash_references"},
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
        chat_mode_payload={"reference_policy": "hash_references"},
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
