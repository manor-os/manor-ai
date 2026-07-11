"""Chat endpoints — SSE streaming, synchronous chat, conversations, messages, export, sharing."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.ai.runtime import (
    ChatSurface,
    runtime_parse_editor_context,
    runtime_run_chat_turn,
    runtime_stream_chat_turn,
)
from packages.core.ai.runtime.surfaces import infer_chat_surface
from packages.core.models.chat_feedback import ChatMessageFeedback
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.schemas.chat import (
    ChatMessageResponse,
    ConversationResponse,
    CreateShareRequest,
    MessageResponse,
    RenameConversationRequest,
    SharedConversationResponse,
    ShareResponse,
)
from packages.core.services.conversation_lifecycle import (
    delete_conversation,
    get_or_create_conversation,
    rename_conversation,
)
from packages.core.services.conversation_messages import add_message
from packages.core.services.conversation_messages import create_assistant_stream_placeholder
from packages.core.services.conversation_records import (
    is_channel_history_conversation,
    list_conversations,
    list_messages,
)
from packages.core.services.conversation_export import (
    export_as_markdown, export_as_json, export_as_text,
)
from packages.core.services.chat_approvals import (
    cancel_chat_approvals,
    resolve_chat_approval_turn,
)
from packages.core.services.chat_manual_skills import (
    ChatManualSkillTurn,
    ManualSkillResolutionError,
    prepare_chat_manual_skill_turn,
)
from packages.core.services.sse_events import format_sse
from packages.core.services.runtime_file_context import (
    FileAttachments,
    RuntimeFileContextTurn,
    prepare_runtime_file_context_turn,
    runtime_saved_message_with_file_references,
)
from packages.core.services.share_service import (
    create_share, get_shared_conversation, revoke_share, list_shares,
)
from packages.core.services.workspace_access import user_can_read_workspace_id
from apps.api.deps import get_current_user, require_plan

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

_LOCAL_FS_URL_RE = re.compile(r"/api/v1/fs/[A-Za-z0-9_-]+/[^\s\"'`)<>]+")
_FILE_PERMISSION_MARKER_RE = re.compile(r"^\[File permission(?:\s+[^\]]*)?\]$", re.IGNORECASE)
_RUNTIME_APPROVAL_REJECTED_RE = re.compile(r"^\[Runtime approval rejected\]", re.IGNORECASE)
_RUNTIME_APPROVAL_REJECTED_REPLY = (
    "The blocked tool call was cancelled and will not access the filesystem. "
    "You can continue chatting or upload the file again if you still want it analyzed."
)


class ChatMessageFeedbackRequest(BaseModel):
    rating: str
    content_preview: str | None = None
    request_preview: str | None = None


class ChatMessageFeedbackResponse(BaseModel):
    message_id: str
    rating: str
    updated_at: str | None = None


def _reference_url_variants(ref_url: str | None) -> set[str]:
    raw = str(ref_url or "").strip()
    if not raw:
        return set()
    try:
        decoded = unquote(raw)
        path = urlsplit(decoded).path or decoded
    except Exception:
        decoded = raw
        path = raw
    variants = {raw, decoded, path, os.path.basename(path)}
    return {variant for variant in variants if variant}


def _prompt_selects_reference(prompt: str, *, url: str | None = None, names: list[str] | None = None) -> bool:
    text = str(prompt or "")
    if not text:
        return False
    lowered = text.lower()
    candidates = set(names or [])
    candidates.update(_reference_url_variants(url))
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        if (
            value.startswith("/api/v1/fs/")
            or value.startswith("http://")
            or value.startswith("https://")
        ) and value.lower() in lowered:
            return True
        if re.search(rf"#\s*{re.escape(value)}(?=$|[\s,，。；;])", text, re.IGNORECASE):
            return True
    return False


def _direct_media_selected_urls(
    urls: list[str],
    *,
    prompt: str,
    attachments: FileAttachments,
    media_flag: str,
) -> list[str]:
    refs_by_url: dict[str, list[dict]] = {}
    for ref in attachments.attachment_refs or []:
        url = str(ref.get("url") or "").strip()
        if url:
            refs_by_url.setdefault(url, []).append(ref)

    kept: list[str] = []
    for url in urls:
        value = str(url or "").strip()
        if not value or value in kept:
            continue
        refs = refs_by_url.get(value, [])
        if any(ref.get("kind") == "chat_upload" and ref.get(media_flag) for ref in refs):
            kept.append(value)
            continue
        names: list[str] = []
        for ref in refs:
            if ref.get(media_flag):
                names.extend([
                    str(ref.get("name") or ""),
                    str(ref.get("path") or ""),
                    str(ref.get("url") or ""),
                ])
        if _prompt_selects_reference(prompt, url=value, names=names):
            kept.append(value)
    return kept
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _surface_for_chat_request(
    *,
    agent_id: str | None,
    workspace_id: str | None,
    ephemeral: bool = False,
    editor_context: dict | None = None,
) -> ChatSurface:
    if editor_context:
        return ChatSurface.FILE_EDITOR_CHAT
    return infer_chat_surface(
        workspace_id=workspace_id,
        agent_id=agent_id,
        ephemeral=ephemeral,
    )


class CancelFileApprovalsRequest(BaseModel):
    hitl_ids: list[str] | None = None
    reason: str | None = None


def _redact_local_fs_urls(value):
    """Remove private entity FS URLs from unauthenticated shared-chat output."""
    if isinstance(value, str):
        return _LOCAL_FS_URL_RE.sub("[protected file]", value)
    if isinstance(value, list):
        return [_redact_local_fs_urls(item) for item in value]
    if isinstance(value, dict):
        return {k: _redact_local_fs_urls(v) for k, v in value.items()}
    return value


def _message_limit_meta(message) -> dict:
    meta = message.meta or {}
    return {
        "stop_reason": meta.get("stop_reason"),
        "error": meta.get("error"),
        "limit_detail": meta.get("limit_detail"),
    }


def _message_hitl_requests(message) -> list[dict] | None:
    meta = message.meta or {}
    requests = meta.get("hitl_requests")
    return requests if isinstance(requests, list) else None


def _message_assistant_blocks(message) -> list[dict] | None:
    meta = message.meta or {}
    blocks = meta.get("assistant_blocks")
    return blocks if isinstance(blocks, list) else None


def _is_internal_file_permission_marker(content: str | None) -> bool:
    """Return True for legacy approval-resume markers that should stay hidden."""
    return bool(isinstance(content, str) and _FILE_PERMISSION_MARKER_RE.match(content.strip()))


def _parse_csv_names(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if not value:
        return []
    names = value.split(",") if isinstance(value, str) else value
    return [str(name).strip() for name in names if str(name or "").strip()]


def _is_stream_placeholder_message(message) -> bool:
    meta = message.meta or {}
    return (
        message.role == "assistant"
        and meta.get("stream_status") in {"running", "streaming"}
    )


def _visible_chat_messages(messages: list) -> list:
    """Filter chat transcript rows without letting hidden rows consume the UI limit."""
    visible_reversed = []
    later_completed_assistant_seen = False
    for message in reversed(messages):
        is_placeholder = _is_stream_placeholder_message(message)
        if message.role == "user" and _is_internal_file_permission_marker(message.content):
            continue
        if is_placeholder and later_completed_assistant_seen:
            continue
        visible_reversed.append(message)
        if message.role == "assistant" and not is_placeholder:
            later_completed_assistant_seen = True
    return list(reversed(visible_reversed))


def _is_runtime_approval_rejected_message(content: str | None) -> bool:
    return bool(isinstance(content, str) and _RUNTIME_APPROVAL_REJECTED_RE.match(content.strip()))


async def _runtime_approval_rejected_stream(conversation_id: str, content: str, message_id: str | None = None):
    yield format_sse("stream_start", {"conversation_id": conversation_id, "message_id": message_id})
    yield format_sse("text_delta", {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "content": content,
    })
    yield format_sse(
        "stream_end",
        {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "usage": {},
            "rounds": 0,
            "tool_calls": [],
        },
    )


async def _can_access_conversation(db: AsyncSession, conv: Conversation, user: User) -> bool:
    if conv.entity_id != user.entity_id:
        return False
    # Workspace conversations are shared inside the organization/workspace.
    # Personal Manor AI / agent DM conversations stay private to the owner.
    if conv.workspace_id is not None:
        return await user_can_read_workspace_id(
            db,
            workspace_id=conv.workspace_id,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
        )
    if is_channel_history_conversation(conv):
        return True
    return conv.user_id == user.id


async def _get_accessible_conversation(
    db: AsyncSession,
    user: User,
    conversation_id: str,
) -> Conversation:
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == user.entity_id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv or not await _can_access_conversation(db, conv, user):
        raise HTTPException(404, "Conversation not found")
    return conv


async def _resolve_chat_workspace_scope(
    db: AsyncSession,
    user: User,
    *,
    conversation_id: str | None,
    workspace_id: str | None,
    thread_ref_kind: str | None,
    thread_ref_id: str | None,
    workspace_context: bool,
) -> tuple[str | None, str | None, str | None]:
    """Resolve when generic chat should run as Workspace Chat."""
    workspace_context = workspace_context or bool(workspace_id and not conversation_id)
    requested_workspace_id = workspace_id if workspace_context else None
    requested_thread_ref_kind = thread_ref_kind if workspace_context else None
    requested_thread_ref_id = thread_ref_id if workspace_context else None

    if conversation_id:
        conv = await _get_accessible_conversation(db, user, conversation_id)
        from packages.core.services.workspace_runtime import is_workspace_chat_conversation

        if is_workspace_chat_conversation(conv):
            if requested_workspace_id and requested_workspace_id != conv.workspace_id:
                raise HTTPException(404, "Conversation not found")
            return (
                conv.workspace_id,
                requested_thread_ref_kind or conv.thread_ref_kind,
                requested_thread_ref_id or conv.thread_ref_id,
            )

        if workspace_context:
            raise HTTPException(404, "Conversation not found")
        return None, None, None

    if not workspace_context:
        return None, None, None
    if not workspace_id:
        raise HTTPException(422, "workspace_id is required for workspace chat")
    if not await user_can_read_workspace_id(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    ):
        raise HTTPException(404, "Conversation not found")
    return workspace_id, requested_thread_ref_kind, requested_thread_ref_id


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def _build_attachments(
    message: str,
    document_ids: str | None,
    files: list,
    entity_id: str,
    db,
    *,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> RuntimeFileContextTurn:
    """Prepare upload/KB attachments through the Runtime file-context adapter."""

    return await prepare_runtime_file_context_turn(
        message=message,
        document_ids=document_ids,
        files=files,
        entity_id=entity_id,
        db=db,
        workspace_id=workspace_id,
        user_id=user_id,
    )


async def _prepare_manual_skill_turn(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str | None,
    message: str,
    manual_skill_ids: str | None,
) -> ChatManualSkillTurn:
    try:
        return await prepare_chat_manual_skill_turn(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
            message=message,
            manual_skill_ids=manual_skill_ids,
        )
    except ManualSkillResolutionError as exc:
        raise HTTPException(404, str(exc)) from exc


_CHAT_MODE_ALIASES = {
    "auto": "auto",
    "image": "image",
    "img": "image",
    "picture": "image",
    "video": "video",
    "audio": "audio",
    "sound": "audio",
    "document": "document",
    "doc": "document",
    "slides": "slides",
    "presentation": "slides",
    "sheet": "sheet",
    "spreadsheet": "sheet",
    "website": "website",
    "app": "website",
    "research": "research",
}
_VIDEO_ASPECT_RATIO_ALIASES = {
    "auto": "adaptive",
    "smart": "adaptive",
    "adaptive": "adaptive",
    "智能": "adaptive",
    "自适应": "adaptive",
}
_VIDEO_ASPECT_RATIO_CHOICES = {"adaptive", "21:9", "16:9", "4:3", "3:4", "1:1", "9:16"}
_IMAGE_ASPECT_RATIO_CHOICES = {"21:9", "16:9", "3:2", "4:3", "1:1", "3:4", "2:3", "9:16"}
_IMAGE_TEXT_POLICIES = {"avoid_text", "text_if_requested", "typography"}
_IMAGE_TASKS = {"generate", "edit", "variant"}
_AUDIO_PURPOSE_ALIASES = {
    "dialogue_or_narration": "narration",
    "dialogue": "dialogue",
    "narration": "narration",
    "voice": "speech",
    "speech": "speech",
    "tts": "speech",
    "ambience": "ambience",
    "ambient": "ambience",
    "soundscape": "soundscape",
    "music": "music",
    "bgm": "music",
    "score": "music",
    "sfx": "sfx",
    "sound_effect": "sfx",
    "sound-effect": "sfx",
    "foley": "sfx",
    "transition": "transition",
}
_DIRECT_VIDEO_OUTPUT_TYPES = {"single_clip", "clip", ""}


def _coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _normalize_chat_mode(chat_mode: str | None) -> str | None:
    if not chat_mode:
        return None
    normalized = re.sub(r"[^a-z_ -]", "", chat_mode.strip().lower()).replace(" ", "_").replace("-", "_")
    return _CHAT_MODE_ALIASES.get(normalized)


def _normalize_video_aspect_ratio(value) -> str:
    raw = str(value or "").strip()
    normalized = raw.lower().replace(" ", "")
    mapped = _VIDEO_ASPECT_RATIO_ALIASES.get(normalized, raw)
    return mapped if mapped in _VIDEO_ASPECT_RATIO_CHOICES else "16:9"


def _parse_chat_mode_payload(raw: str | dict | None, chat_mode: str | None) -> dict:
    if not raw:
        payload: dict = {}
    elif isinstance(raw, dict):
        payload = dict(raw)
    else:
        try:
            parsed = json.loads(raw)
            payload = parsed if isinstance(parsed, dict) else {}
        except Exception:
            payload = {}

    if _normalize_chat_mode(chat_mode) == "video":
        duration = payload.get("clip_duration_seconds") or payload.get("duration_seconds") or payload.get("duration")
        try:
            duration_value = int(float(duration))
        except (TypeError, ValueError):
            duration_value = 5
        payload["clip_duration_seconds"] = min(15, max(4, duration_value))
        payload["max_single_generation_duration_seconds"] = 15
        payload["aspect_ratio"] = _normalize_video_aspect_ratio(payload.get("aspect_ratio"))
        resolution = str(payload.get("resolution") or "720p").strip().lower()
        payload["resolution"] = resolution if resolution in {"720p", "1080p"} else "720p"
        reference_policy = (
            payload.get("reference_policy")
            or payload.get("reference_mode")
            or "hash_references"
        )
        reference_policy = str(reference_policy).strip().lower().replace("-", "_")
        if reference_policy not in {"hash_references", "first_last_frames", "smart_multiframe"}:
            reference_policy = "hash_references"
        payload["reference_policy"] = reference_policy
        raw_audio_policy = str(payload.get("audio_policy") or "").strip().lower().replace("-", "_")
        generate_audio = _coerce_bool(
            payload.get("generate_audio"),
            default=(raw_audio_policy != "silent_visual"),
        )
        payload["generate_audio"] = generate_audio
        payload["audio_policy"] = "native_if_supported" if generate_audio else "silent_visual"
        if reference_policy == "first_last_frames":
            payload["reference_slots"] = [
                {"role": "first_frame", "label": "First frame / 首帧", "required": True},
                {"role": "last_frame", "label": "Last frame / 尾帧", "required": True},
            ]
            payload["reference_role_hints"] = {
                "first_frame": "#first_frame / #首帧",
                "last_frame": "#last_frame / #尾帧",
            }
        else:
            payload.pop("reference_slots", None)
            payload.pop("reference_role_hints", None)
    elif _normalize_chat_mode(chat_mode) == "image":
        task = str(payload.get("task") or "generate").strip().lower().replace("-", "_")
        payload["task"] = task if task in _IMAGE_TASKS else "generate"
        aspect_ratio = str(payload.get("aspect_ratio") or "").strip()
        payload["aspect_ratio"] = aspect_ratio if aspect_ratio in _IMAGE_ASPECT_RATIO_CHOICES else "auto"
        resolution = str(payload.get("resolution") or "2k").strip().lower()
        payload["resolution"] = resolution if resolution in {"1k", "2k", "4k"} else "2k"
        reference_policy = str(payload.get("reference_policy") or "smart_references").strip().lower().replace("-", "_")
        payload["reference_policy"] = reference_policy or "smart_references"
        text_policy = str(payload.get("text_policy") or "avoid_text").strip().lower().replace("-", "_")
        payload["text_policy"] = text_policy if text_policy in _IMAGE_TEXT_POLICIES else "avoid_text"
    elif _normalize_chat_mode(chat_mode) == "audio":
        duration = payload.get("duration_seconds") or payload.get("clip_duration_seconds") or payload.get("duration")
        try:
            duration_value = float(duration)
        except (TypeError, ValueError):
            duration_value = 15.0
        payload["duration_seconds"] = max(0.1, min(duration_value, 600.0))
        purpose = str(payload.get("purpose") or "speech").strip().lower().replace("-", "_")
        payload["purpose"] = _AUDIO_PURPOSE_ALIASES.get(purpose, purpose or "speech")
    elif _normalize_chat_mode(chat_mode) == "slides":
        render = str(payload.get("render") or payload.get("presentation_render") or "").strip()
        if render in {"editable", "full_page_image"}:
            payload["render"] = render
        else:
            payload.pop("render", None)
    return payload


def _media_mode_prompt_with_settings(prompt: str, payload: dict, *, mode: str) -> str:
    prompt_text = str(prompt or "").strip()
    notes: list[str] = []
    if mode == "image":
        task = payload.get("task")
        text_policy = payload.get("text_policy")
        if task == "edit":
            notes.append("Edit the provided reference image(s); preserve important identity and composition details.")
        elif task == "variant":
            notes.append("Create a new visual variant based on the provided reference image(s).")
        if text_policy == "avoid_text":
            notes.append("Avoid rendering words, captions, logos, or UI text unless the user explicitly requested text.")
        elif text_policy == "text_if_requested":
            notes.append("Only render text that the user explicitly requested, and keep it exact.")
        elif text_policy == "typography":
            notes.append("Typography/text is intentional; render requested words exactly and make them legible.")
    if not notes:
        return prompt_text
    return f"{prompt_text}\n\nMode settings:\n" + "\n".join(f"- {note}" for note in notes)


def _image_chat_mode_tool_call(prompt_text: str, payload: dict, attachments: FileAttachments) -> dict:
    params: dict = {}
    aspect_ratio = payload.get("aspect_ratio")
    if aspect_ratio and aspect_ratio != "auto":
        params["aspect_ratio"] = aspect_ratio
    if payload.get("resolution"):
        params["resolution"] = payload.get("resolution")

    image_urls = [url for url in (attachments.image_urls or []) if str(url or "").strip()]
    reference_policy = str(payload.get("reference_policy") or "smart_references").strip().lower()
    if image_urls and reference_policy not in {"prompt_only", "none", "no_references"}:
        if payload.get("task") in {"edit", "variant"}:
            params["input_image_urls"] = image_urls[:16]
            params["input_fidelity"] = "high"
        else:
            params["reference_urls"] = image_urls[:16]

    return {
        "name": "generate_file",
        "arguments": {
            "kind": "image",
            "prompt": _media_mode_prompt_with_settings(prompt_text, payload, mode="image"),
            "params": params,
        },
    }


def _audio_chat_mode_tool_call(prompt_text: str, payload: dict) -> dict:
    params: dict = {
        "purpose": payload.get("purpose") or "speech",
    }
    if payload.get("duration_seconds") is not None:
        params["duration_seconds"] = payload.get("duration_seconds")
    if payload.get("voice"):
        params["voice"] = payload.get("voice")
    if payload.get("response_format"):
        params["response_format"] = payload.get("response_format")
    return {
        "name": "generate_file",
        "arguments": {
            "kind": "audio",
            "prompt": prompt_text,
            "params": params,
        },
    }


def _chat_mode_direct_tool_calls(
    *,
    chat_mode: str | None,
    chat_mode_payload: str | dict | None,
    prompt: str,
    attachments: FileAttachments,
    manual_skill_refs: list[dict] | None = None,
) -> list[dict]:
    """Build deterministic media tool calls for mode-specific composer sends."""
    mode = _normalize_chat_mode(chat_mode)
    if mode not in {"image", "video", "audio"} or manual_skill_refs:
        return []
    payload = _parse_chat_mode_payload(chat_mode_payload, mode)
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return []

    if mode == "image":
        return [_image_chat_mode_tool_call(prompt_text, payload, attachments)]

    if mode == "audio":
        return [_audio_chat_mode_tool_call(prompt_text, payload)]

    output_type = str(payload.get("output_type") or "single_clip").strip().lower()
    if output_type not in _DIRECT_VIDEO_OUTPUT_TYPES:
        return []

    image_urls = [url for url in (attachments.image_urls or []) if str(url or "").strip()]
    video_urls = [url for url in (attachments.video_urls or []) if str(url or "").strip()]
    audio_urls = [url for url in (attachments.audio_urls or []) if str(url or "").strip()]
    if attachments.attachment_refs:
        image_urls = _direct_media_selected_urls(
            image_urls,
            prompt=prompt_text,
            attachments=attachments,
            media_flag="image",
        )
        video_urls = _direct_media_selected_urls(
            video_urls,
            prompt=prompt_text,
            attachments=attachments,
            media_flag="video",
        )
        audio_urls = _direct_media_selected_urls(
            audio_urls,
            prompt=prompt_text,
            attachments=attachments,
            media_flag="audio",
        )
    reference_policy = payload.get("reference_policy") or "hash_references"
    generate_audio = _coerce_bool(payload.get("generate_audio"), default=True)
    params: dict = {
        "duration": payload.get("clip_duration_seconds") or 5,
        "aspect_ratio": payload.get("aspect_ratio") or "16:9",
        "generate_audio": generate_audio,
        "audio_policy": (
            "native_dialogue_reference_only"
            if generate_audio and audio_urls and reference_policy != "first_last_frames"
            else ("native_audio" if generate_audio else "silent_picture_only")
        ),
    }
    if payload.get("resolution"):
        params["resolution"] = payload.get("resolution")
    if reference_policy == "first_last_frames":
        if len(image_urls) >= 1:
            params["first_frame_url"] = image_urls[0]
        if len(image_urls) >= 2:
            params["last_frame_url"] = image_urls[1]
    else:
        if image_urls:
            params["reference_urls"] = image_urls[:9]
        if video_urls:
            params["reference_video_urls"] = video_urls[:3]
        if audio_urls:
            params["audio_reference_urls"] = audio_urls[:3]

    return [
        {
            "name": "generate_file",
            "arguments": {
                "kind": "video",
                "prompt": prompt_text,
                "params": params,
            },
        }
    ]


def _is_direct_media_generation_turn(direct_tool_calls: list[dict] | None) -> bool:
    for call in direct_tool_calls or []:
        if not isinstance(call, dict) or call.get("name") != "generate_file":
            continue
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if str(args.get("kind") or "").strip().lower() in {"image", "video", "audio"}:
            return True
    return False


def _stream_llm_message_with_attachments(
    llm_base_message: str,
    attachments: FileAttachments,
    direct_tool_calls: list[dict] | None,
) -> str | list:
    text_part = llm_base_message
    if attachments.text_context:
        text_part = (
            f"{llm_base_message}\n\n<attached_files>\n"
            f"{attachments.text_context}\n</attached_files>"
        )

    if attachments.image_blocks and not _is_direct_media_generation_turn(direct_tool_calls):
        return [
            {"type": "text", "text": text_part},
            *attachments.image_blocks,
        ]
    return text_part


def _chat_mode_payload_summary(payload: dict) -> str:
    if not payload:
        return ""
    parts: list[str] = []
    for key in sorted(payload):
        value = payload.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False)
        else:
            value_text = str(value)
        parts.append(f"- {key}: {value_text}")
    return "\n".join(parts[:20])


def _video_reference_policy_prompt(reference_policy: str | None) -> str:
    if reference_policy == "first_last_frames":
        return (
            "Selected video reference mode: first_last_frames. The input contract is different from general references: "
            "there are exactly two required reference roles: first_frame (First frame / 首帧) and last_frame "
            "(Last frame / 尾帧). Map references labeled #first_frame, #首帧, first frame, or 首帧 to first_frame_url; "
            "map references labeled #last_frame, #尾帧, last frame, or 尾帧 to last_frame_url. If labels are absent, "
            "use the first provided image as first_frame_url and the second provided image as last_frame_url. Do not "
            "collapse these into generic reference_urls. If one of the two frames is missing, explain the missing "
            "input instead of silently switching modes."
        )
    if reference_policy == "smart_multiframe":
        return (
            "Selected video reference mode: smart_multiframe. Treat # references as ordered frame, character, scene, "
            "product, or motion anchors. Preserve their roles in the prompt and pass them as reference_urls or the "
            "specific first_frame_url/last_frame_url fields when the user labels them that way."
        )
    return (
        "Selected video reference mode: hash_references/all_refs. Treat # references and attachments as general "
        "character, scene, product, style, motion, video, or audio references. Preserve all relevant reference URLs in "
        "the generation call, but do not assume the first two images are first/last frames unless the user says so."
    )


def _slides_render_prompt(render: str | None) -> str:
    if str(render or "").strip().lower() == "full_page_image":
        return (
            " The user chose the Full-Page Image render option. When you call invoke_skill for this deck, "
            "select the pptx skill and pass tool parameter params.render='full_page_image' instead "
            "of params.render='editable'. This "
            "means every slide is a single AI-generated full-page image, produced one page at a time and exported as "
            "one image per slide. This deck is intentionally NOT editable in PowerPoint and slide text is rendered by "
            "the image model; the user accepted that trade-off."
        )
    return ""


def _chat_mode_runtime_prompt(chat_mode: str | None, chat_mode_payload: str | dict | None = None) -> str | None:
    mode = _normalize_chat_mode(chat_mode)
    if not mode or mode == "auto":
        return None
    payload = _parse_chat_mode_payload(chat_mode_payload, mode)
    payload_summary = _chat_mode_payload_summary(payload)
    shared = (
        "The user selected a chat box mode. Treat it as routing intent for this turn, "
        "while still obeying the user's actual request and attached file context. "
        "Manor chat references existing files with inline # file tokens; use those attached documents/files as references "
        "instead of asking the user to upload them again."
    )
    prompts = {
        "image": (
            f"{shared}\nMode: Image generation. For requests to create, edit, or derive visual assets, "
            "call generate_file with kind='image'. Preserve and pass attached reference image URLs when present. "
            "Respect image mode settings such as task, aspect_ratio, resolution, reference_policy, and text_policy."
        ),
        "video": (
            f"{shared}\nMode: Video generation. For video output, call generate_file with kind='video' and pass "
            "available first_frame_url, last_frame_url, reference_urls, reference_video_urls, "
            "or audio_reference_urls exactly when the user provided them. A single model video generation is limited "
            "to 15 seconds max; split longer finals into multiple <=15s clips and then compose them.\n"
            "Video generation is async and returns status='pending' with a job_id. You MUST then call "
            "wait_media_jobs with that job_id (it blocks until the video finishes), and report the real "
            "outcome — the completed video, or the failure reason if it failed. Never end your turn while a "
            "video job is still pending; do not claim success before wait_media_jobs confirms completion.\n"
            f"{_video_reference_policy_prompt(str(payload.get('reference_policy') or 'hash_references'))}"
        ),
        "audio": (
            f"{shared}\nMode: Audio generation. For narration, dialogue, ambience, SFX, music beds, or soundscapes, "
            "call generate_file with kind='audio'. Specify purpose, duration_seconds, voice when relevant, and timing intent."
        ),
        "document": f"{shared}\nMode: Document generation. For polished documents, use generate_file with the document kind.",
        "slides": (
            f"{shared}\nMode: Slide deck generation. Use invoke_skill with skill='pptx' for slide decks. "
            "Pass the user's request as input. When the chat box includes a render option, pass it through the "
            "invoke_skill params object, for example params.render='full_page_image'."
            + _slides_render_prompt(str(payload.get("render") or "editable"))
        ),
        "sheet": f"{shared}\nMode: Spreadsheet generation. Use generate_file with kind='spreadsheet'.",
        "website": f"{shared}\nMode: Website/app generation. Use generate_file with kind='code'.",
        "research": f"{shared}\nMode: Research. Prioritize source-backed research, comparisons, citations, and synthesis.",
    }
    prompt = prompts.get(mode)
    if prompt and payload_summary:
        prompt = f"{prompt}\n\nMode settings from the chat box:\n{payload_summary}"
    return prompt


def _message_with_chat_mode_marker(message: str, chat_mode: str | None, chat_mode_payload: str | dict | None = None) -> str:
    mode = _normalize_chat_mode(chat_mode)
    if not mode or mode == "auto":
        return message
    return f"{message}\n[Mode: {mode}]".strip()


def _runtime_metadata_for_chat_mode(
    file_context_turn: RuntimeFileContextTurn,
    *,
    chat_mode_prompt: str | None,
    direct_tool_calls: list[dict] | None,
) -> dict:
    metadata = dict(file_context_turn.runtime_metadata or {})
    if chat_mode_prompt:
        metadata["chat_mode_prompt"] = chat_mode_prompt
    if direct_tool_calls:
        metadata["forced_tool_calls"] = direct_tool_calls
    return metadata


# ── SSE Streaming ──

@router.post("/stream")
async def chat_stream(
    message: str = Form(...),
    conversation_id: str | None = Form(None),
    agent_id: str | None = Form(None),
    workspace_id: str | None = Form(None),
    workspace_context: bool = Form(False),
    thread_ref_kind: str | None = Form(None),
    thread_ref_id: str | None = Form(None),
    document_ids: str | None = Form(None),
    manual_skill_ids: str | None = Form(None),
    chat_mode: str | None = Form(None),
    chat_mode_payload: str | None = Form(None),
    disable_tools: bool = Form(False),
    blocked_tools: str | None = Form(None),
    editor_context: str | None = Form(None),
    ephemeral: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
    _gate=Depends(require_plan("ai_budget_usd")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message and stream the AI response via SSE.

    Accepts multipart/form-data with optional file attachments and
    knowledge-base document IDs (comma-separated). File contents are
    extracted and injected into the LLM context automatically.
    """
    workspace_id, thread_ref_kind, thread_ref_id = await _resolve_chat_workspace_scope(
        db,
        user,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        workspace_context=workspace_context,
    )
    file_context_turn = await _build_attachments(
        message, document_ids, files, user.entity_id, db,
        workspace_id=workspace_id, user_id=user.id,
    )
    message = file_context_turn.cleaned_message
    attachments = file_context_turn.attachments
    manual_skill_turn = await _prepare_manual_skill_turn(
        db,
        entity_id=user.entity_id,
        agent_id=agent_id,
        message=message,
        manual_skill_ids=manual_skill_ids,
    )
    manual_skill_refs = manual_skill_turn.manual_skill_refs
    llm_base_message = manual_skill_turn.llm_base_message

    chat_mode_prompt = _chat_mode_runtime_prompt(chat_mode, chat_mode_payload)
    direct_tool_calls = [] if disable_tools else _chat_mode_direct_tool_calls(
        chat_mode=chat_mode,
        chat_mode_payload=chat_mode_payload,
        prompt=llm_base_message,
        attachments=attachments,
        manual_skill_refs=manual_skill_refs,
    )
    llm_message = _stream_llm_message_with_attachments(
        llm_base_message,
        attachments,
        direct_tool_calls,
    )

    if ephemeral:
        parsed_editor_context = runtime_parse_editor_context(editor_context)
        return StreamingResponse(
            runtime_stream_chat_turn(
                llm_message,
                None,
                surface=_surface_for_chat_request(
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    ephemeral=True,
                    editor_context=parsed_editor_context,
                ),
                entity_id=user.entity_id,
                user_id=user.id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                manual_skill_refs=manual_skill_refs,
                disable_tools=disable_tools,
                blocked_tools=_parse_csv_names(blocked_tools),
                editor_context=parsed_editor_context,
                runtime_metadata=_runtime_metadata_for_chat_mode(
                    file_context_turn,
                    chat_mode_prompt=chat_mode_prompt,
                    direct_tool_calls=direct_tool_calls,
                ),
                persist_messages=False,
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # Get or create conversation
    # Auto-title from first message when creating a new conversation
    saved_user_base = _message_with_chat_mode_marker(
        manual_skill_turn.saved_user_base,
        chat_mode,
        chat_mode_payload,
    )
    _auto_title = saved_user_base.split("\n")[0][:100].strip() if not conversation_id else None
    try:
        conv = await get_or_create_conversation(
            db, user.entity_id, user.id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
            title=_auto_title,
        )
    except (LookupError, PermissionError):
        raise HTTPException(404, "Conversation not found")

    if _is_runtime_approval_rejected_message(llm_base_message):
        assistant_msg = await add_message(db, conv.id, role="assistant", content=_RUNTIME_APPROVAL_REJECTED_REPLY)
        await db.commit()
        return StreamingResponse(
            _runtime_approval_rejected_stream(conv.id, _RUNTIME_APPROVAL_REJECTED_REPLY, assistant_msg.id),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    approval_saved_text: str | None = None
    save_user_message = True
    replacement, resolved_saved_text, save_user_message = await resolve_chat_approval_turn(
        db,
        conversation_id=conv.id,
        entity_id=user.entity_id,
        user_id=user.id,
        message=llm_base_message,
    )
    if replacement:
        llm_message = replacement
        approval_saved_text = resolved_saved_text
        direct_tool_calls = []

    # Save user message in DB as plain text. The image bytes are only
    # multimodal for this turn, but the stable /api/v1/fs references must
    # remain in history so follow-up turns can use them for media tools.
    if save_user_message:
        saved_text = approval_saved_text or saved_user_base
        if not approval_saved_text:
            saved_text = runtime_saved_message_with_file_references(saved_text, attachments)
        # Stash the posting user so workspace chat can attribute the message
        # to its real author. Without this, every user message reads back with
        # no author_user_id and the UI renders all of them as the viewer's own.
        await add_message(
            db, conv.id, role="user", content=saved_text,
            meta={"author_user_id": user.id},
        )
    assistant_placeholder = await create_assistant_stream_placeholder(
        db,
        conv.id,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    await db.commit()

    # Stream response — don't pass the request-scoped db session;
    # the generator creates its own short-lived sessions to avoid
    # holding a DB connection for the entire SSE stream duration.
    parsed_editor_context = runtime_parse_editor_context(editor_context)
    return StreamingResponse(
        runtime_stream_chat_turn(
            llm_message,
            conv.id,
            surface=_surface_for_chat_request(
                agent_id=agent_id,
                workspace_id=workspace_id,
                editor_context=parsed_editor_context,
            ),
            entity_id=user.entity_id,
            user_id=user.id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            manual_skill_refs=manual_skill_refs,
            disable_tools=disable_tools,
            blocked_tools=_parse_csv_names(blocked_tools),
            editor_context=parsed_editor_context,
            assistant_message_id=assistant_placeholder.id,
            runtime_metadata=_runtime_metadata_for_chat_mode(
                file_context_turn,
                chat_mode_prompt=chat_mode_prompt,
                direct_tool_calls=direct_tool_calls,
            ),
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )



# ── Non-streaming chat ──

@router.post("/message", response_model=ChatMessageResponse)
async def chat_message(
    request: Request,
    message: str | None = Form(None),
    conversation_id: str | None = Form(None),
    agent_id: str | None = Form(None),
    workspace_id: str | None = Form(None),
    workspace_context: bool = Form(False),
    thread_ref_kind: str | None = Form(None),
    thread_ref_id: str | None = Form(None),
    document_ids: str | None = Form(None),
    manual_skill_ids: str | None = Form(None),
    chat_mode: str | None = Form(None),
    chat_mode_payload: str | None = Form(None),
    blocked_tools: str | None = Form(None),
    editor_context: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    _gate=Depends(require_plan("ai_budget_usd")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message and get the full AI response (non-streaming).

    Uses the agentic loop for multi-turn tool execution.
    Returns the complete response after all tool calls are resolved.
    """
    if message is None and request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        message = body.get("message")
        conversation_id = body.get("conversation_id", conversation_id)
        agent_id = body.get("agent_id", agent_id)
        workspace_id = body.get("workspace_id", workspace_id)
        workspace_context = _coerce_bool(body.get("workspace_context", workspace_context))
        thread_ref_kind = body.get("thread_ref_kind", thread_ref_kind)
        thread_ref_id = body.get("thread_ref_id", thread_ref_id)
        document_ids = body.get("document_ids", document_ids)
        manual_skill_ids = body.get("manual_skill_ids", manual_skill_ids)
        chat_mode = body.get("chat_mode", chat_mode)
        chat_mode_payload = body.get("chat_mode_payload", chat_mode_payload)
        blocked_tools = body.get("blocked_tools", blocked_tools)
        editor_context = body.get("editor_context", editor_context)
    if message is None:
        raise HTTPException(422, "message is required")

    workspace_id, thread_ref_kind, thread_ref_id = await _resolve_chat_workspace_scope(
        db,
        user,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        workspace_context=bool(workspace_context),
    )
    file_context_turn = await _build_attachments(
        message, document_ids, files, user.entity_id, db,
        workspace_id=workspace_id, user_id=user.id,
    )
    message = file_context_turn.cleaned_message
    attachments = file_context_turn.attachments
    manual_skill_turn = await _prepare_manual_skill_turn(
        db,
        entity_id=user.entity_id,
        agent_id=agent_id,
        message=message,
        manual_skill_ids=manual_skill_ids,
    )
    manual_skill_refs = manual_skill_turn.manual_skill_refs
    llm_base_message = manual_skill_turn.llm_base_message

    chat_mode_prompt = _chat_mode_runtime_prompt(chat_mode, chat_mode_payload)
    direct_tool_calls = _chat_mode_direct_tool_calls(
        chat_mode=chat_mode,
        chat_mode_payload=chat_mode_payload,
        prompt=llm_base_message,
        attachments=attachments,
        manual_skill_refs=manual_skill_refs,
    )
    llm_message = _stream_llm_message_with_attachments(
        llm_base_message,
        attachments,
        direct_tool_calls,
    )

    # Get or create conversation
    # Auto-title from first message when creating a new conversation
    saved_user_base = _message_with_chat_mode_marker(
        manual_skill_turn.saved_user_base,
        chat_mode,
        chat_mode_payload,
    )
    _auto_title = saved_user_base.split("\n")[0][:100].strip() if not conversation_id else None
    try:
        conv = await get_or_create_conversation(
            db, user.entity_id, user.id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
            title=_auto_title,
        )
    except (LookupError, PermissionError):
        raise HTTPException(404, "Conversation not found")

    if _is_runtime_approval_rejected_message(llm_base_message):
        assistant_msg = await add_message(db, conv.id, role="assistant", content=_RUNTIME_APPROVAL_REJECTED_REPLY)
        await db.commit()
        return ChatMessageResponse(
            conversation_id=conv.id,
            message_id=assistant_msg.id,
            content=_RUNTIME_APPROVAL_REJECTED_REPLY,
            tool_calls_made=[],
            usage={},
            rounds=0,
        )

    approval_saved_text: str | None = None
    save_user_message = True
    replacement, resolved_saved_text, save_user_message = await resolve_chat_approval_turn(
        db,
        conversation_id=conv.id,
        entity_id=user.entity_id,
        user_id=user.id,
        message=llm_base_message,
    )
    if replacement:
        llm_message = replacement
        approval_saved_text = resolved_saved_text
        direct_tool_calls = []

    # Save user message
    if save_user_message:
        saved_text = approval_saved_text or saved_user_base
        if not approval_saved_text:
            saved_text = runtime_saved_message_with_file_references(saved_text, attachments)
        # Attribute the message to its author so workspace chat can tell who
        # sent it (see /chat/stream above for the full rationale).
        await add_message(
            db, conv.id, role="user", content=saved_text,
            meta={"author_user_id": user.id},
        )
    await db.commit()

    # Run agentic loop
    parsed_editor_context = runtime_parse_editor_context(editor_context)
    result = await runtime_run_chat_turn(
        llm_message,
        conv.id,
        surface=_surface_for_chat_request(
            agent_id=agent_id,
            workspace_id=workspace_id,
            editor_context=parsed_editor_context,
        ),
        entity_id=user.entity_id,
        user_id=user.id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        db=db,
        manual_skill_refs=manual_skill_refs,
        blocked_tools=_parse_csv_names(blocked_tools),
        editor_context=parsed_editor_context,
        runtime_metadata=_runtime_metadata_for_chat_mode(
            file_context_turn,
            chat_mode_prompt=chat_mode_prompt,
            direct_tool_calls=direct_tool_calls,
        ),
    )

    return ChatMessageResponse(
        conversation_id=result["conversation_id"],
        message_id=result.get("message_id"),
        content=result.get("content", ""),
        tool_calls_made=result.get("tool_calls_made", []),
        usage=result.get("usage", {}),
        rounds=result.get("rounds", 1),
        stop_reason=result.get("stop_reason"),
        error=result.get("error"),
        limit_detail=result.get("limit_detail"),
        hitl_requests=result.get("hitl_requests"),
        attachments=result.get("attachments"),
    )


# ── Conversations ──

@router.get("/conversations", response_model=list[ConversationResponse])
async def list_my_conversations(
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func, select as sa_select
    from packages.core.models.task import Message as MessageModel

    convs = await list_conversations(db, user.entity_id, user.id)
    # Filter by workspace if specified
    if workspace_id and convs:
        convs = [c for c in convs if c.workspace_id == workspace_id]
    if convs:
        convs = [c for c in convs if await _can_access_conversation(db, c, user)]
    if not convs:
        return []

    # Batch-fetch message counts
    conv_ids = [c.id for c in convs]
    count_q = (
        sa_select(MessageModel.conversation_id, func.count().label("cnt"))
        .where(MessageModel.conversation_id.in_(conv_ids))
        .group_by(MessageModel.conversation_id)
    )
    counts_result = await db.execute(count_q)
    counts = {row[0]: row[1] for row in counts_result}

    return [
        ConversationResponse(
            id=c.id, entity_id=c.entity_id, user_id=c.user_id,
            agent_id=c.agent_id, workspace_id=c.workspace_id,
            title=c.title, summary=c.summary, channel=c.channel, status=c.status,
            message_count=counts.get(c.id, 0),
            created_at=c.created_at.isoformat() if c.created_at else None,
            updated_at=c.updated_at.isoformat() if c.updated_at else None,
        )
        for c in convs
    ]


async def _rename_one_conversation(
    conversation_id: str,
    req: RenameConversationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_accessible_conversation(db, user, conversation_id)
    conv = await rename_conversation(db, conversation_id, user.entity_id, req.title)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return ConversationResponse(
        id=conv.id, entity_id=conv.entity_id, user_id=conv.user_id,
        agent_id=conv.agent_id, workspace_id=conv.workspace_id,
        title=conv.title, summary=conv.summary, channel=conv.channel, status=conv.status,
        created_at=conv.created_at.isoformat() if conv.created_at else None,
        updated_at=conv.updated_at.isoformat() if conv.updated_at else None,
    )


@router.put("/conversations/{conversation_id}", response_model=ConversationResponse)
async def rename_one_conversation(
    conversation_id: str,
    req: RenameConversationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _rename_one_conversation(conversation_id, req, user, db)


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
async def patch_rename_one_conversation(
    conversation_id: str,
    req: RenameConversationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _rename_one_conversation(conversation_id, req, user, db)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_one_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_accessible_conversation(db, user, conversation_id)
    ok = await delete_conversation(db, conversation_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Conversation not found")


# ── Messages ──

@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conversation_id: str,
    limit: int = Query(500, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_accessible_conversation(db, user, conversation_id)

    raw_limit = min(max(limit * 4, limit), 2000)
    msgs = _visible_chat_messages(
        await list_messages(db, conversation_id, limit=raw_limit)
    )
    if len(msgs) > limit:
        msgs = msgs[-limit:]
    return [
        MessageResponse(
            id=m.id, conversation_id=m.conversation_id,
            role=m.role, content=m.content,
            tool_calls=m.tool_calls,
            assistant_blocks=_message_assistant_blocks(m),
            token_usage=m.token_usage,
            attachments=m.attachments,
            hitl_requests=_message_hitl_requests(m),
            **_message_limit_meta(m),
            created_at=m.created_at.isoformat() if m.created_at else None,
        )
        for m in msgs
    ]


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=ChatMessageFeedbackResponse,
)
async def record_message_feedback(
    conversation_id: str,
    message_id: str,
    req: ChatMessageFeedbackRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record thumbs feedback for an assistant response."""
    await _get_accessible_conversation(db, user, conversation_id)
    rating = (req.rating or "").strip().lower()
    if rating not in {"up", "down"}:
        raise HTTPException(422, "rating must be 'up' or 'down'")

    msg = (await db.execute(
        select(Message).where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
        )
    )).scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    if msg.role != "assistant":
        raise HTTPException(422, "Only assistant messages can be rated")

    def _preview(value: str | None) -> str | None:
        text = (value or "").strip()
        return text[:1000] if text else None

    existing = (await db.execute(
        select(ChatMessageFeedback).where(
            ChatMessageFeedback.message_id == message_id,
            ChatMessageFeedback.user_id == user.id,
        )
    )).scalar_one_or_none()

    content_preview = _preview(req.content_preview) or _preview(msg.content)
    request_preview = _preview(req.request_preview)
    if existing:
        existing.rating = rating
        existing.content_preview = content_preview
        existing.request_preview = request_preview
        existing.updated_at = datetime.now(timezone.utc)
        entry = existing
    else:
        entry = ChatMessageFeedback(
            entity_id=user.entity_id,
            user_id=user.id,
            conversation_id=conversation_id,
            message_id=message_id,
            rating=rating,
            content_preview=content_preview,
            request_preview=request_preview,
        )
        db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return ChatMessageFeedbackResponse(
        message_id=message_id,
        rating=entry.rating,
        updated_at=entry.updated_at.isoformat() if entry.updated_at else None,
    )


@router.post("/conversations/{conversation_id}/file-approvals/cancel")
async def cancel_conversation_file_approvals(
    conversation_id: str,
    req: CancelFileApprovalsRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel pending approval tokens when the user stops a chat request."""
    await _get_accessible_conversation(db, user, conversation_id)
    cancelled = await cancel_chat_approvals(
        db,
        conversation_id=conversation_id,
        entity_id=user.entity_id,
        user_id=user.id,
        hitl_ids=(req.hitl_ids if req else None),
        reason=(req.reason if req and req.reason else "request_stopped"),
    )
    await db.commit()
    return cancelled


# ── Export ──

@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: str,
    format: str = Query("markdown", pattern="^(markdown|json|text)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a conversation in markdown, json, or text format."""
    await _get_accessible_conversation(db, user, conversation_id)
    if format == "json":
        data = await export_as_json(db, conversation_id, user.entity_id)
        if not data:
            raise HTTPException(404, "Conversation not found")
        return data
    elif format == "text":
        text = await export_as_text(db, conversation_id, user.entity_id)
        if not text:
            raise HTTPException(404, "Conversation not found")
        return PlainTextResponse(text, media_type="text/plain")
    else:
        md = await export_as_markdown(db, conversation_id, user.entity_id)
        if not md:
            raise HTTPException(404, "Conversation not found")
        return PlainTextResponse(md, media_type="text/markdown")


# ── Sharing ──

@router.post("/conversations/{conversation_id}/share", response_model=ShareResponse)
async def share_conversation(
    conversation_id: str,
    req: CreateShareRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a shareable link for a conversation."""
    await _get_accessible_conversation(db, user, conversation_id)
    try:
        share = await create_share(
            db, conversation_id, user.entity_id, user.id,
            expires_hours=req.expires_hours,
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))

    return ShareResponse(
        id=share.id,
        conversation_id=share.conversation_id,
        share_token=share.share_token,
        expires_at=share.expires_at.isoformat() if share.expires_at else None,
        is_active=share.is_active,
        created_at=share.created_at.isoformat() if share.created_at else None,
    )


@router.get("/conversations/{conversation_id}/shares", response_model=list[ShareResponse])
async def list_conversation_shares(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List active shares for a conversation."""
    await _get_accessible_conversation(db, user, conversation_id)
    shares = await list_shares(db, user.entity_id, conversation_id=conversation_id)
    return [
        ShareResponse(
            id=s.id,
            conversation_id=s.conversation_id,
            share_token=s.share_token,
            expires_at=s.expires_at.isoformat() if s.expires_at else None,
            is_active=s.is_active,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in shares
    ]


@router.delete("/conversations/{conversation_id}/share/{share_id}", status_code=204)
async def revoke_conversation_share(
    conversation_id: str,
    share_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a shared link."""
    await _get_accessible_conversation(db, user, conversation_id)
    ok = await revoke_share(
        db, share_id, user.entity_id, conversation_id=conversation_id
    )
    if not ok:
        raise HTTPException(404, "Share not found")
    await db.commit()


@router.get("/shared/{share_token}", response_model=SharedConversationResponse)
async def view_shared_conversation(
    share_token: str,
    db: AsyncSession = Depends(get_db),
):
    """View a shared conversation — no authentication required."""
    result = await get_shared_conversation(db, share_token)
    if not result:
        raise HTTPException(404, "Shared conversation not found or expired")

    share, conv, messages = result
    return SharedConversationResponse(
        conversation=ConversationResponse(
            id=conv.id, entity_id=conv.entity_id, user_id=conv.user_id,
            agent_id=conv.agent_id, workspace_id=conv.workspace_id,
            title=conv.title, summary=conv.summary, channel=conv.channel, status=conv.status,
            created_at=conv.created_at.isoformat() if conv.created_at else None,
            updated_at=conv.updated_at.isoformat() if conv.updated_at else None,
        ),
        messages=[
            MessageResponse(
                id=m.id, conversation_id=m.conversation_id,
                role=m.role, content=_redact_local_fs_urls(m.content),
                tool_calls=_redact_local_fs_urls(m.tool_calls),
                assistant_blocks=_redact_local_fs_urls(_message_assistant_blocks(m)),
                token_usage=m.token_usage,
                attachments=_redact_local_fs_urls(m.attachments),
                hitl_requests=_redact_local_fs_urls(_message_hitl_requests(m)),
                **_message_limit_meta(m),
                created_at=m.created_at.isoformat() if m.created_at else None,
            )
            for m in messages
            if not (m.role == "user" and _is_internal_file_permission_marker(m.content))
        ],
    )


# ── Text-to-Speech ──

@router.post("/tts")
async def text_to_speech(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Convert text to speech audio (MP3). Used by the web voice chat mode."""
    import os
    import httpx

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    voice = body.get("voice", "alloy")

    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        raise HTTPException(503, "TTS not configured")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "tts-1",
                "voice": voice,
                "input": text[:4096],
                "response_format": "mp3",
            },
        )
    if not resp.is_success:
        raise HTTPException(502, f"TTS API error: {resp.status_code}")

    return Response(
        content=resp.content,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ── Live Voice Session (OpenAI Realtime API) ──

@router.post("/voice-session")
async def create_voice_session(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an ephemeral token for OpenAI Realtime API voice chat.

    Returns the ephemeral key, model, and resolved agent instructions so
    the browser can connect directly to OpenAI via WebRTC.
    """
    import os
    import httpx

    body = await request.json()
    agent_id = body.get("agent_id")
    voice = body.get("voice", "alloy")
    conversation_id = body.get("conversation_id")
    workspace_id = body.get("workspace_id")

    # Realtime API requires a direct OpenAI key (not OpenRouter)
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(
            503,
            "Live voice requires an OpenAI API key. "
            "Set OPENAI_API_KEY in your .env file.",
        )

    # Resolve agent system prompt so the realtime model has context
    instructions = "You are a helpful assistant. Be concise and conversational."
    try:
        from packages.core.services.voice_runtime import resolve_voice_chat_instructions
        instructions = await resolve_voice_chat_instructions(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
        )
    except Exception:
        pass  # Fall back to default instructions

    # Get or create conversation for saving transcript later
    conv_id = conversation_id
    if conv_id:
        await _get_accessible_conversation(db, user, conv_id)
    else:
        _auto_title = "Voice conversation"
        conv = await get_or_create_conversation(
            db, user.entity_id, user.id,
            agent_id=agent_id, workspace_id=workspace_id,
            title=_auto_title,
        )
        await db.commit()
        conv_id = str(conv.id)

    realtime_model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-mini-realtime-preview")

    # Request ephemeral token from OpenAI Realtime API
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": realtime_model,
                "voice": voice,
                "instructions": instructions[:32000],
                "modalities": ["text", "audio"],
                "turn_detection": {"type": "server_vad"},
                "input_audio_transcription": {"model": "whisper-1"},
            },
        )

    if not resp.is_success:
        raise HTTPException(502, f"Realtime session error: {resp.status_code}")

    data = resp.json()
    return {
        "ephemeral_key": data.get("client_secret", {}).get("value"),
        "expires_at": data.get("client_secret", {}).get("expires_at"),
        "model": realtime_model,
        "conversation_id": conv_id,
        "session_id": data.get("id"),
    }


@router.post("/voice-save")
async def save_voice_transcript(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save voice conversation transcript to the conversation history."""
    body = await request.json()
    conversation_id = body.get("conversation_id")
    turns = body.get("turns", [])  # [{role, content}]

    if not conversation_id or not turns:
        raise HTTPException(400, "conversation_id and turns are required")

    await _get_accessible_conversation(db, user, conversation_id)
    for turn in turns:
        role = turn.get("role", "user")
        content = (turn.get("content") or "").strip()
        if content:
            await add_message(db, conversation_id, role=role, content=content)

    await db.commit()
    return {"ok": True, "saved": len(turns)}
