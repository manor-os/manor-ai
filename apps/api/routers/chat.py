"""Chat endpoints — SSE streaming, synchronous chat, conversations, messages, export, sharing."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.constants.agents import is_master_agent
from packages.core.constants.conversation import ConversationSurfaceKind
from packages.core.ai.runtime import (
    ChatSurface,
    EditorCurrentDocumentTooLargeError,
    render_editor_current_document_user_context,
    runtime_parse_editor_context,
    runtime_run_chat_turn,
    runtime_stream_chat_turn,
)
from packages.core.ai.runtime.surfaces import infer_chat_surface
from packages.core.ai.runtime.output_policy import (
    runtime_assistant_stream_error_content,
    runtime_public_assistant_message_content,
    runtime_public_failure_payload,
    runtime_public_tool_calls,
    runtime_public_tool_payload,
)
from packages.core.models.task import Conversation, Message
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.user import User
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.models.workflow import WorkflowRun
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
from packages.core.services.conversation_messages import (
    ORIGIN_USER_MESSAGE_ID_META_KEY,
    add_message,
    assistant_message_origin_meta,
    create_assistant_stream_placeholder,
)
from packages.core.services.conversation_visibility import (
    is_internal_file_permission_marker,
)
from packages.core.services.conversation_records import (
    is_channel_history_conversation,
    list_conversations,
    list_messages,
    list_messages_before,
)
from packages.core.services.conversation_surfaces import (
    AiEditTargetIdentity,
    ConversationSurfaceMetadataFactory,
)
from packages.core.services.conversation_export import (
    export_as_markdown, export_as_json, export_as_text,
)
from packages.core.services.chat_approvals import (
    cancel_chat_approvals,
    chat_hitl_action_is_pending,
    resolve_chat_approval_turn,
)
from packages.core.services.chat_manual_skills import (
    ChatManualSkillTurn,
    ManualSkillReferenceError,
    ManualSkillResolutionError,
    prepare_chat_manual_skill_turn,
)
from packages.core.services.chat_feedback import (
    ChatFeedbackIntegrityErrorKind,
    ChatFeedbackMutationStatus,
    ChatFeedbackRating,
    ChatFeedbackTargetDeletedError,
    ChatFeedbackTargetKind,
    ChatFeedbackTargetPolicyFactory,
    build_chat_feedback_content_preview,
    classify_chat_feedback_integrity_error,
    list_chat_message_feedback,
    persist_chat_message_feedback,
    resolve_chat_feedback_request_preview,
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
from packages.core.services.workspace_access import (
    user_can_read_workspace_id,
    user_readable_workspace_ids,
)
from apps.api.chat_audio import SpeechRequest, acquire_audio_lease, authenticated_audio_scope, chat_speech_response
from apps.api.deps import get_current_user, require_plan
from apps.api.streaming_concurrency import acquire_chat_stream_lease
from packages.core.services.runtime_run_service import (
    RuntimeRunNotFoundError,
    cancel_runtime_run_resources,
    project_runtime_run_status,
    request_runtime_run_cancel,
    create_runtime_run,
)
from packages.core.services.response_surfaces import (
    registered_response_surface_template_action,
)
from packages.core.config import get_settings

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

_LOCAL_FS_URL_RE = re.compile(r"/api/v1/fs/[A-Za-z0-9_-]+/[^\s\"'`)<>]+")
_RUNTIME_APPROVAL_REJECTED_RE = re.compile(r"^\[Runtime approval rejected\]", re.IGNORECASE)
_RUNTIME_APPROVAL_REJECTED_REPLY = (
    "The blocked tool call was cancelled and will not access the filesystem. "
    "You can continue chatting or upload the file again if you still want it analyzed."
)
_WORKFLOW_CHAT_META_KEYS = frozenset({
    "workflow_attempt_number",
    "workflow_binding_id",
    "workflow_business_outcome",
    "workflow_current_step_id",
    "workflow_error",
    "workflow_retry_of_run_id",
    "workflow_route_source",
    "workflow_run_id",
    "workflow_status",
    "workflow_steps",
    "workflow_title",
})
_STREAM_STATE_META_KEYS = frozenset({"stream_status"})
_CHAT_MESSAGE_META_KEYS = (
    _WORKFLOW_CHAT_META_KEYS
    | _STREAM_STATE_META_KEYS
    | frozenset({
        "chat_mode",
        "chat_mode_payload",
        ORIGIN_USER_MESSAGE_ID_META_KEY,
        "response_surface_submission",
        "workspace_recommendation",
    })
)
_RESPONSE_SURFACE_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_RESPONSE_SURFACE_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_RESPONSE_SURFACE_EVENT_UNIQUE_INDEX = (
    "uq_messages_conversation_response_surface_event"
)
_RESPONSE_SURFACE_RECEIPT_MAX_CHARS = 250_000
_RESPONSE_SURFACE_PARSED_PAYLOAD_MAX_CHARS = 200_000
_RESPONSE_SURFACE_GENERIC_PAYLOAD_MAX_CHARS = 40_000
_DEFAULT_CODE_LAB_LANGUAGE_IDS = (
    "python",
    "javascript",
    "typescript",
    "java",
    "cpp",
    "go",
    "rust",
)


def _parse_chat_conversation_surface(value: str | None) -> ConversationSurfaceKind:
    normalized = str(value or "").strip()
    if not normalized:
        return ConversationSurfaceKind.ORDINARY_CHAT
    try:
        surface = ConversationSurfaceKind(normalized)
    except ValueError as exc:
        raise HTTPException(422, "Unsupported conversation surface") from exc
    if surface not in {
        ConversationSurfaceKind.ORDINARY_CHAT,
        ConversationSurfaceKind.AI_EDIT,
    }:
        raise HTTPException(422, "Unsupported conversation surface")
    return surface


def _parse_ai_edit_target(editor_context: dict | None) -> AiEditTargetIdentity:
    context = editor_context or {}
    try:
        return ConversationSurfaceMetadataFactory.ai_edit_target(
            str(context.get("target_kind") or ""),
            str(context.get("target_id") or ""),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class ChatMessageFeedbackRequest(BaseModel):
    rating: ChatFeedbackRating
    content_preview: str | None = None
    request_preview: str | None = None


class ChatMessageFeedbackResponse(BaseModel):
    message_id: str
    rating: ChatFeedbackRating
    mutation_sequence: int
    mutation_status: ChatFeedbackMutationStatus
    updated_at: str | None = None
    target_kind: ChatFeedbackTargetKind
    target_id: str
    task_id: str | None = None
    plan_id: str | None = None


class ChatMessageFeedbackSnapshotResponse(BaseModel):
    message_id: str
    rating: ChatFeedbackRating
    mutation_sequence: int
    target_kind: ChatFeedbackTargetKind
    target_id: str
    task_id: str | None = None
    plan_id: str | None = None


class ResolveChatActionRequest(BaseModel):
    choice: str
    note: str | None = None
    payload: dict | None = None


class MessagesPageResponse(BaseModel):
    items: list[MessageResponse]
    has_more: bool
    next_cursor: str | None = None


class GlobalChatFlowEntrypointResponse(BaseModel):
    binding_id: str
    workflow_id: str
    workspace_id: str
    workspace_name: str
    title: str
    description: str
    placeholder: str
    order: int
    inputs: list[dict]


def _reject_non_finite_response_surface_json_constant(value: str):
    raise ValueError(f"Unsupported JSON constant: {value}")


def _parse_response_surface_submission(value: str | None) -> dict | None:
    if value is None:
        return None
    if len(value) > _RESPONSE_SURFACE_RECEIPT_MAX_CHARS:
        raise HTTPException(422, "Response surface submission is too large")
    try:
        raw = json.loads(
            value,
            parse_constant=_reject_non_finite_response_surface_json_constant,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise HTTPException(422, "Invalid response surface submission") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise HTTPException(422, "Invalid response surface submission")
    required_strings = {
        "eventId": 96,
        "recordedAt": 64,
        "sourceMessageId": 128,
        "surfaceId": 96,
        "title": 120,
        "action": 64,
        "actionLabel": 80,
    }
    parsed: dict = {"version": 1}
    for key, maximum in required_strings.items():
        item = raw.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > maximum:
            raise HTTPException(422, "Invalid response surface submission")
        parsed[key] = item.strip()
    if not _RESPONSE_SURFACE_EVENT_ID_RE.fullmatch(parsed["eventId"]):
        raise HTTPException(422, "Invalid response surface event")
    if not _RESPONSE_SURFACE_ACTION_RE.fullmatch(parsed["action"]):
        raise HTTPException(422, "Invalid response surface action")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(422, "Invalid response surface payload")
    try:
        if (
            len(json.dumps(payload, ensure_ascii=False, allow_nan=False))
            > _RESPONSE_SURFACE_PARSED_PAYLOAD_MAX_CHARS
        ):
            raise HTTPException(422, "Response surface payload is too large")
    except (RecursionError, TypeError, ValueError) as exc:
        raise HTTPException(422, "Invalid response surface payload") from exc
    parsed["payload"] = payload
    return parsed


def _canonicalize_response_surface_submission(
    receipt: dict,
    surface: dict,
    action: dict,
) -> dict:
    canonical = {
        "version": 1,
        "eventId": receipt["eventId"],
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "sourceMessageId": receipt["sourceMessageId"],
        "surfaceId": surface["id"],
        "title": str(surface.get("title") or "")[:120],
        "action": action["id"],
        "actionLabel": str(action.get("label") or "")[:80],
        "payload": receipt["payload"],
    }
    render = surface.get("render") if isinstance(surface.get("render"), dict) else {}
    props = render.get("props") if isinstance(render.get("props"), dict) else {}
    if render.get("kind") == "template":
        context: dict = {"templateId": render.get("template_id")}
        instructions = props.get("instructions")
        if isinstance(instructions, str) and instructions:
            context["instructions"] = instructions[:4_000]
        tests = props.get("tests")
        if isinstance(tests, list):
            context["checks"] = [
                item[:2_000]
                for item in tests[:20]
                if isinstance(item, str) and item
            ]
        canonical["context"] = context
    return canonical


def _validated_response_surface_payload(receipt: dict, surface: dict) -> dict:
    payload = receipt.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(422, "Invalid response surface payload")
    render = surface.get("render") if isinstance(surface.get("render"), dict) else {}
    if render.get("kind") != "template":
        if (
            len(json.dumps(payload, ensure_ascii=False))
            > _RESPONSE_SURFACE_GENERIC_PAYLOAD_MAX_CHARS
        ):
            raise HTTPException(422, "Response surface payload is too large")
        return payload
    props = render.get("props") if isinstance(render.get("props"), dict) else {}
    template_id = render.get("template_id")
    if template_id == "response.choice":
        choice = payload.get("choice")
        allowed_choices = {
            option.get("id")
            for option in props.get("options", [])
            if isinstance(option, dict) and isinstance(option.get("id"), str)
        }
        if not isinstance(choice, str) or choice not in allowed_choices:
            raise HTTPException(422, "Invalid response surface choice")
        return {"choice": choice}
    if template_id == "learning.code_lab":
        language = payload.get("language")
        code = payload.get("code")
        if not isinstance(language, str) or not isinstance(code, str) or not code:
            raise HTTPException(422, "Invalid code lab submission")
        if len(code) > 30_000:
            raise HTTPException(422, "Code lab submission is too large")
        language = language.strip().lower()
        configured_languages = [
            item.get("id")
            for item in props.get("languages", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ][:8]
        allowed_languages = configured_languages or list(_DEFAULT_CODE_LAB_LANGUAGE_IDS)
        initial_language = str(props.get("language") or "text").strip().lower()
        if initial_language not in allowed_languages:
            allowed_languages.insert(0, initial_language)
        if language not in allowed_languages[:8]:
            raise HTTPException(422, "Invalid code lab language")
        return {"language": language, "code": code}
    if (
        len(json.dumps(payload, ensure_ascii=False))
        > _RESPONSE_SURFACE_GENERIC_PAYLOAD_MAX_CHARS
    ):
        raise HTTPException(422, "Response surface payload is too large")
    return payload


def _response_surface_submission_message(receipt: dict) -> str:
    payload = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
    code = payload.get("code") if isinstance(payload.get("code"), str) else ""
    if code:
        language = re.sub(r"[^A-Za-z0-9_+.-]", "", str(payload.get("language") or "text"))[:32]
        context = receipt.get("context") if isinstance(receipt.get("context"), dict) else {}
        execution_request = ""
        if context.get("templateId") == "learning.code_lab" and receipt.get("action") == "run":
            requirements = [
                "Execute this submission with the sandbox-backed bash tool using a single non-multiline command. Call search_tools with query select:bash now before execution, even if an earlier turn reported that bash was unavailable. Do not write files to the Workspace. Do not claim any test passed unless the tool output proves it. Evaluate it against the exercise requirements and report concise pass/fail feedback."
            ]
            if context.get("instructions"):
                requirements.append(f"Exercise requirements:\n{context['instructions']}")
            checks = context.get("checks") if isinstance(context.get("checks"), list) else []
            if checks:
                requirements.append("Checks:\n" + "\n".join(f"- {item}" for item in checks))
            execution_request = "\n\n" + "\n\n".join(requirements)
        return (
            f"{receipt['actionLabel']}: {receipt['title']}{execution_request}"
            f"\n\n```{language}\n{code[:30_000]}\n```"
        )
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)[:8_000]
    suffix = f"\n\n{serialized}" if serialized and serialized != "{}" else ""
    return f"{receipt['actionLabel']}: {receipt['title']}{suffix}"


async def _bind_response_surface_submission(
    db: AsyncSession,
    *,
    conversation_id: str,
    receipt: dict | None,
) -> dict | None:
    if receipt is None:
        return None
    source = (await db.execute(
        select(Message).where(
            Message.id == receipt["sourceMessageId"],
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
        )
    )).scalar_one_or_none()
    if source is None:
        raise HTTPException(409, "Response surface source message is unavailable")
    blocks = _message_assistant_blocks(source) or []
    surface = next((
        block for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "surface"
        and block.get("id") == receipt["surfaceId"]
    ), None)
    if surface is None:
        raise HTTPException(409, "Response surface is unavailable")
    actions = surface.get("actions") if isinstance(surface.get("actions"), list) else []
    persisted_action = next((
        item for item in actions
        if isinstance(item, dict)
        and item.get("id") == receipt["action"]
        and item.get("intent") == "submit"
    ), None)
    render = surface.get("render") if isinstance(surface.get("render"), dict) else {}
    registered_action = None
    if render.get("kind") == "template":
        registered_action = registered_response_surface_template_action(
            str(render.get("template_id") or ""),
            actions,
        )
    action = (
        registered_action
        if registered_action is not None
        and (
            receipt["action"] == registered_action["id"]
            or persisted_action is not None
        )
        else persisted_action
    )
    if action is None:
        raise HTTPException(409, "Response surface action is unavailable")
    canonical_receipt = {
        **receipt,
        "payload": _validated_response_surface_payload(receipt, surface),
    }
    return _canonicalize_response_surface_submission(canonical_receipt, surface, action)


@dataclass(frozen=True)
class _ResponseSurfaceSubmissionAgent:
    agent_id: str | None
    agent_subscription_id: str | None


async def _response_surface_submission_agent(
    db: AsyncSession,
    *,
    conversation: Conversation,
    submission: dict,
) -> _ResponseSurfaceSubmissionAgent:
    """Resolve the exact Agent deployment that authored the submitted surface."""

    author_subscription_id = (await db.execute(
        select(Message.author_subscription_id).where(
            Message.id == submission["sourceMessageId"],
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
        )
    )).scalar_one_or_none()
    fallback_subscription_id = getattr(conversation, "agent_subscription_id", None)
    expected_agent_id = None
    if not author_subscription_id and not fallback_subscription_id:
        if (
            conversation.workspace_id
            and conversation.agent_id
            and not is_master_agent(conversation.agent_id)
        ):
            candidate_subscriptions = list((await db.execute(
                select(AgentSubscription)
                .join(Agent, Agent.id == AgentSubscription.agent_id)
                .where(
                    AgentSubscription.entity_id == conversation.entity_id,
                    AgentSubscription.workspace_id == conversation.workspace_id,
                    AgentSubscription.agent_id == conversation.agent_id,
                    AgentSubscription.status == "active",
                    Agent.status == "active",
                    Agent.deleted_at.is_(None),
                )
                .limit(2)
            )).scalars().all())
            if len(candidate_subscriptions) != 1:
                raise HTTPException(409, "Response surface Agent is ambiguous")
            source_subscription = candidate_subscriptions[0]
            return _ResponseSurfaceSubmissionAgent(
                agent_id=str(source_subscription.agent_id),
                agent_subscription_id=str(source_subscription.id),
            )
        return _ResponseSurfaceSubmissionAgent(
            agent_id=conversation.agent_id,
            agent_subscription_id=None,
        )
    if not author_subscription_id:
        author_subscription_id = fallback_subscription_id
        expected_agent_id = conversation.agent_id

    subscription_filters = [
        AgentSubscription.id == author_subscription_id,
        AgentSubscription.entity_id == conversation.entity_id,
        AgentSubscription.workspace_id == conversation.workspace_id,
        AgentSubscription.status == "active",
        Agent.status == "active",
        Agent.deleted_at.is_(None),
    ]
    if expected_agent_id:
        subscription_filters.append(AgentSubscription.agent_id == expected_agent_id)
    source_subscription = (await db.execute(
        select(AgentSubscription)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(*subscription_filters)
    )).scalar_one_or_none()
    if not source_subscription:
        raise HTTPException(409, "Response surface Agent is unavailable")
    return _ResponseSurfaceSubmissionAgent(
        agent_id=str(source_subscription.agent_id),
        agent_subscription_id=str(source_subscription.id),
    )


def _same_response_surface_submission(stored: object, current: dict) -> bool:
    """Compare immutable submission intent while ignoring server timestamps."""

    if not isinstance(stored, dict):
        return False
    immutable_fields_match = all(stored.get(key) == current.get(key) for key in (
        "version",
        "eventId",
        "sourceMessageId",
        "surfaceId",
        "payload",
    ))
    if not immutable_fields_match:
        return False

    def canonical_action(submission: dict) -> object:
        context = submission.get("context")
        template_id = (
            context.get("templateId")
            if isinstance(context, dict)
            else None
        )
        registered_action = registered_response_surface_template_action(
            str(template_id or "")
        )
        return (
            registered_action["id"]
            if registered_action is not None
            else submission.get("action")
        )

    return canonical_action(stored) == canonical_action(current)


async def _find_response_surface_submission_replay(
    db: AsyncSession,
    *,
    conversation_id: str,
    user_id: str,
    submission: dict,
) -> tuple[Message | None, RuntimeRun | None] | None:
    """Return the prior assistant/run for an accepted idempotency event."""

    origin = (await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.role == "user",
            Message.response_surface_event_id == submission["eventId"],
        ).limit(1)
    )).scalar_one_or_none()
    if origin is None:
        return None
    stored_submission = (
        origin.meta.get("response_surface_submission")
        if isinstance(origin.meta, dict)
        else None
    )
    if not _same_response_surface_submission(stored_submission, submission):
        raise HTTPException(409, "Response surface event conflicts with an earlier submission")

    assistant = (await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
            Message.meta[ORIGIN_USER_MESSAGE_ID_META_KEY].astext == origin.id,
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(1)
    )).scalar_one_or_none()
    if assistant is None:
        return (None, None)
    runtime_run = (await db.execute(
        select(RuntimeRun)
        .where(
            RuntimeRun.conversation_id == conversation_id,
            RuntimeRun.assistant_message_id == assistant.id,
            RuntimeRun.parent_run_id.is_(None),
            RuntimeRun.user_id == user_id,
        )
        .order_by(RuntimeRun.created_at.desc(), RuntimeRun.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    return (assistant, runtime_run)


async def _response_surface_submission_replay_stream(
    conversation_id: str,
    assistant: Message | None,
):
    """Replay an already accepted local result without starting another run."""

    message_id = assistant.id if assistant is not None else None
    yield format_sse("stream_start", {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "submission_reused": True,
    })
    content = _message_public_content(assistant) if assistant is not None else ""
    if content:
        yield format_sse("text_delta", {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "content": content,
        })
    terminal = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "usage": assistant.token_usage or {} if assistant is not None else {},
        "tool_calls": (
            runtime_public_tool_calls(assistant.tool_calls or [])
            if assistant is not None
            else []
        ),
        "attachments": assistant.attachments if assistant is not None else None,
        "assistant_blocks": (
            _message_assistant_blocks(assistant) if assistant is not None else None
        ),
        "persisted": assistant is not None,
        "rounds": 0,
        "submission_reused": True,
    }
    yield format_sse("stream_end", terminal)


async def _response_surface_submission_failure_replay_stream(
    conversation_id: str,
    assistant: Message,
    error_message: str,
):
    """Replay a persisted local failure without presenting it as success."""

    public_error_message = runtime_assistant_stream_error_content(error_message)
    yield format_sse("stream_start", {
        "conversation_id": conversation_id,
        "message_id": assistant.id,
        "submission_reused": True,
    })
    yield format_sse("error", {
        "conversation_id": conversation_id,
        "message_id": assistant.id,
        "message": public_error_message,
        "persisted": True,
        "submission_reused": True,
    })
    yield format_sse("stream_end", {
        "conversation_id": conversation_id,
        "message_id": assistant.id,
        "persisted": True,
        "submission_reused": True,
    })


def _response_surface_submission_replay_source(
    conversation_id: str,
    assistant: Message | None,
    runtime_run: RuntimeRun | None,
):
    if (
        runtime_run is not None
        and (
            runtime_run.status != RuntimeRunStatus.COMPLETED.value
            or (
                assistant is not None
                and _is_stream_placeholder_message(assistant)
            )
        )
    ):
        return _durable_runtime_event_stream(runtime_run)
    if assistant is not None and _is_stream_placeholder_message(assistant):
        raise HTTPException(
            status_code=409,
            detail="response_surface_submission_in_progress",
            headers={"Retry-After": "1"},
        )
    if assistant is not None:
        meta = assistant.meta or {}
        stream_status = str(meta.get("stream_status") or "").lower()
        stop_reason = str(meta.get("stop_reason") or "").lower()
        persisted_error = str(meta.get("error") or "").strip()
        limit_detail = meta.get("limit_detail")
        if (
            meta.get("stream_error") is True
            or stream_status == "error"
            or stop_reason in {"error", "credit_exhausted"}
            or bool(persisted_error)
        ):
            error_message = str(
                meta.get("error_message")
                or persisted_error
                or (
                    limit_detail.get("message")
                    if isinstance(limit_detail, dict)
                    else None
                )
                or "Response surface submission failed"
            )
            return _response_surface_submission_failure_replay_stream(
                conversation_id,
                assistant,
                error_message,
            )
        if meta.get("stream_interrupted") is True or stream_status == "interrupted":
            return _response_surface_submission_failure_replay_stream(
                conversation_id,
                assistant,
                "Response surface submission was interrupted",
            )
    return _response_surface_submission_replay_stream(conversation_id, assistant)


async def _response_surface_submission_replay_after_integrity_error(
    db: AsyncSession,
    lease,
    *,
    conversation_id: str,
    user_id: str,
    submission: dict,
):
    """Resolve a uniqueness-race replay while retaining lease ownership."""

    try:
        replay = await _find_response_surface_submission_replay(
            db,
            conversation_id=conversation_id,
            user_id=user_id,
            submission=submission,
        )
        if replay is None:
            return None
        assistant, runtime_run = replay
        return _response_surface_submission_replay_source(
            conversation_id,
            assistant,
            runtime_run,
        )
    except BaseException:
        await lease.release()
        raise


def _integrity_error_constraint_name(exc: IntegrityError) -> str | None:
    current: object | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        constraint_name = getattr(current, "constraint_name", None)
        diag = getattr(current, "diag", None)
        if constraint_name:
            return str(constraint_name)
        if diag is not None and getattr(diag, "constraint_name", None):
            return str(diag.constraint_name)
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


def _encode_message_cursor(message: Message | None) -> str | None:
    if not message or not message.created_at or not message.id:
        return None
    return f"{message.created_at.isoformat()}|{message.id}"


def _decode_message_cursor(value: str | None) -> tuple[datetime | None, str | None]:
    if not value:
        return None, None
    timestamp, separator, message_id = value.partition("|")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise HTTPException(400, "Invalid message cursor") from exc
    return parsed, message_id if separator and message_id else None


def _workflow_run_id_for_message(message: Message) -> str | None:
    pending_action = message.pending_action if isinstance(message.pending_action, dict) else {}
    raw_meta = message.meta if isinstance(message.meta, dict) else {}
    for value in (pending_action.get("workflow_run_id"), raw_meta.get("workflow_run_id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    refs = message.refs if isinstance(message.refs, list) else []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("type") != "workflow_run":
            continue
        run_id = ref.get("id")
        if isinstance(run_id, str) and run_id.strip():
            return run_id.strip()
    return None


async def _workflow_run_accessibility_by_message_id(
    db: AsyncSession,
    user: User,
    messages: list[Message],
) -> dict[str, bool]:
    run_id_by_message_id = {
        message.id: run_id
        for message in messages
        if (run_id := _workflow_run_id_for_message(message))
    }
    if not run_id_by_message_id:
        return {}

    runs = (await db.execute(
        select(WorkflowRun.id, WorkflowRun.workspace_id).where(
            WorkflowRun.id.in_(set(run_id_by_message_id.values())),
            WorkflowRun.entity_id == user.entity_id,
        )
    )).all()
    workspace_id_by_run_id = {
        str(run_id): str(workspace_id) if workspace_id else None
        for run_id, workspace_id in runs
    }
    readable_workspace_ids = await user_readable_workspace_ids(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        workspace_ids={
            workspace_id
            for workspace_id in workspace_id_by_run_id.values()
            if workspace_id
        },
    )
    return {
        message_id: run_id in workspace_id_by_run_id and (
            workspace_id_by_run_id[run_id] is None
            or workspace_id_by_run_id[run_id] in readable_workspace_ids
        )
        for message_id, run_id in run_id_by_message_id.items()
    }


def _to_chat_message_response(
    message: Message,
    *,
    workflow_run_accessible: bool | None = None,
) -> MessageResponse:
    raw_meta = message.meta if isinstance(message.meta, dict) else {}
    response_meta = runtime_public_tool_payload({
        key: raw_meta[key]
        for key in _CHAT_MESSAGE_META_KEYS
        if key in raw_meta
    })
    if workflow_run_accessible is False:
        response_meta["workflow_run_accessible"] = False
    return MessageResponse(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=_message_public_content(message),
        tool_calls=runtime_public_tool_calls(message.tool_calls),
        assistant_blocks=_message_assistant_blocks(message),
        token_usage=message.token_usage,
        attachments=message.attachments,
        hitl_requests=_message_hitl_requests(message),
        message_kind=message.message_kind,
        refs=message.refs,
        meta=response_meta,
        pending_action=(
            message.pending_action
            if isinstance(message.pending_action, dict) and message.pending_action.get("kind")
            else None
        ),
        resolved_at=message.resolved_at.isoformat() if message.resolved_at else None,
        resolution=message.resolution,
        workflow_result=_message_workflow_result(message),
        **_message_limit_meta(message),
        created_at=message.created_at.isoformat() if message.created_at else None,
    )


async def _chat_message_responses(
    db: AsyncSession,
    user: User,
    messages: list[Message],
) -> list[MessageResponse]:
    workflow_run_accessibility = await _workflow_run_accessibility_by_message_id(
        db,
        user,
        messages,
    )
    return [
        _to_chat_message_response(
            message,
            workflow_run_accessible=workflow_run_accessibility.get(message.id),
        )
        for message in messages
    ]


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
_CONVERSATION_ID_HEADER = "X-Conversation-ID"
_RUNTIME_RUN_ID_HEADER = "X-Runtime-Run-ID"
_RESPONSE_SURFACE_EVENT_ID_HEADER = "X-Response-Surface-Event-ID"


async def _chat_streaming_response(
    db: AsyncSession,
    lease,
    source,
    *,
    conversation_id: str | None = None,
    runtime_run_id: str | None = None,
    response_surface_event_id: str | None = None,
) -> StreamingResponse:
    """Release the request transaction before a long-lived SSE response starts."""
    try:
        await db.commit()
        await db.close()
        headers = dict(_SSE_HEADERS)
        if conversation_id:
            headers[_CONVERSATION_ID_HEADER] = conversation_id
        if runtime_run_id:
            headers[_RUNTIME_RUN_ID_HEADER] = runtime_run_id
        if response_surface_event_id:
            headers[_RESPONSE_SURFACE_EVENT_ID_HEADER] = response_surface_event_id
        return StreamingResponse(
            lease.wrap(source),
            media_type="text/event-stream",
            headers=headers,
        )
    except BaseException:
        await lease.release()
        raise


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


def _chat_mode_blocked_tools(
    chat_mode: str | None,
    *,
    surface: ChatSurface,
) -> set[str]:
    """Keep Global Chat Flow execution on the user-facing launcher."""
    if surface != ChatSurface.GLOBAL_OWNER_CHAT:
        return set()
    if _normalize_chat_mode(chat_mode) == "flows":
        from packages.core.ai.runtime.tool_registry import (
            runtime_registered_tool_names,
        )

        allowed = {
            "search_tools",
            "list_workspace_flows",
            "start_workspace_flow",
            "get_workflow_run",
            "cancel_workflow_run",
            "resume_workflow_run",
        }
        return set(runtime_registered_tool_names()) - allowed
    return {"start_workspace_flow", "run_workflow"}


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
    raw_error = meta.get("error")
    return {
        "stop_reason": meta.get("stop_reason"),
        "error": (
            runtime_assistant_stream_error_content(str(raw_error))
            if raw_error
            else None
        ),
        "limit_detail": runtime_public_failure_payload(meta.get("limit_detail")),
    }


def _message_hitl_requests(message) -> list[dict] | None:
    meta = message.meta or {}
    requests = meta.get("hitl_requests")
    return requests if isinstance(requests, list) else None


def _message_public_content(message) -> str:
    return runtime_public_assistant_message_content(
        message.content,
        message.meta,
        message.tool_calls,
    )


def _message_assistant_blocks(message) -> list[dict] | None:
    meta = message.meta or {}
    if (
        meta.get("stream_error") is True
        or meta.get("stream_interrupted") is True
        or str(meta.get("stream_status") or "").lower() in {"error", "interrupted"}
        or str(meta.get("stop_reason") or "").lower() in {"error", "credit_exhausted"}
    ):
        return None
    blocks = meta.get("assistant_blocks")
    return (
        runtime_public_tool_payload(blocks)
        if isinstance(blocks, list)
        else None
    )


def _message_workflow_result(message) -> dict | None:
    meta = message.meta or {}
    result = meta.get("workflow_result")
    return runtime_public_tool_payload(result) if isinstance(result, dict) else None


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
    later_completed_origins: set[str] = set()
    later_unscoped_completed_assistant_seen = False
    for message in reversed(messages):
        is_placeholder = _is_stream_placeholder_message(message)
        if message.role == "user" and is_internal_file_permission_marker(message.content):
            continue
        meta = message.meta or {}
        origin_user_message_id = str(
            meta.get(ORIGIN_USER_MESSAGE_ID_META_KEY) or ""
        ).strip()
        if is_placeholder:
            if origin_user_message_id in later_completed_origins:
                continue
            if (
                not origin_user_message_id
                and later_unscoped_completed_assistant_seen
            ):
                continue
        visible_reversed.append(message)
        if message.role == "assistant" and not is_placeholder:
            if origin_user_message_id:
                later_completed_origins.add(origin_user_message_id)
            else:
                later_unscoped_completed_assistant_seen = True
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


async def _require_chat_budget_unless_pending_approval(
    db: AsyncSession,
    *,
    user: User,
    conversation_id: str | None,
    message: str,
) -> bool:
    """Apply the AI-credit gate unless this turn resolves a live HITL card."""

    pending_approval = await chat_hitl_action_is_pending(
        db,
        conversation_id=conversation_id,
        entity_id=user.entity_id,
        message=message,
    )
    if not pending_approval:
        await require_plan("ai_budget_usd")(user=user, db=db)
    return pending_approval


def _is_workflow_approval_resolution(metadata: dict | None) -> bool:
    return bool(metadata and metadata.get("approval_kind") == "workflow")


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


def _require_user_managed_conversation(conv: Conversation) -> Conversation:
    """Hide host-owned surface sessions from ordinary chat management APIs."""

    if ConversationSurfaceMetadataFactory.is_host_owned(conv.meta):
        raise HTTPException(404, "Conversation not found")
    return conv


def _require_deletable_conversation(conv: Conversation) -> Conversation:
    """Keep host-owned sessions out of generic deletion except AI Edit teardown."""

    if not ConversationSurfaceMetadataFactory.is_deletable_session(conv.meta):
        raise HTTPException(404, "Conversation not found")
    return conv


async def _get_owned_runtime_run(
    db: AsyncSession,
    run_id: str,
    user: User,
) -> RuntimeRun:
    run = (
        await db.execute(
            select(RuntimeRun).where(
                RuntimeRun.id == run_id,
                RuntimeRun.entity_id == user.entity_id,
                RuntimeRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Chat run not found")
    return run


@router.get("/runs/{run_id}")
async def get_chat_runtime_run_status(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _get_owned_runtime_run(db, run_id, user)
    return await project_runtime_run_status(
        db,
        run,
        poll_after_seconds=get_settings().SANDBOX_QUEUE_POLL_SECONDS,
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_chat_runtime_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_runtime_run(db, run_id, user)
    try:
        run = await request_runtime_run_cancel(
            db,
            run_id=run_id,
            entity_id=user.entity_id,
            user_id=user.id,
        )
    except RuntimeRunNotFoundError as exc:
        raise HTTPException(404, "Chat run not found") from exc
    await db.commit()
    await cancel_runtime_run_resources(run)
    return await project_runtime_run_status(
        db,
        run,
        poll_after_seconds=get_settings().SANDBOX_QUEUE_POLL_SECONDS,
    )


async def _durable_runtime_event_stream(
    run: RuntimeRun,
    *,
    after_id: str = "0-0",
):
    from packages.core.database import async_session
    from packages.core.models.runtime_run import RuntimeRunStatus
    from packages.core.services.runtime_event_stream import (
        parse_sse_frame,
        read_runtime_sse_events,
    )

    yield format_sse(
        "runtime_run",
        {
            "run_id": run.id,
            "conversation_id": run.conversation_id,
            "message_id": run.assistant_message_id,
            "status": run.status,
            "poll_after_seconds": get_settings().SANDBOX_QUEUE_POLL_SECONDS,
        },
    )
    cursor = after_id or "0-0"
    terminal_event_seen = False
    stream_end_seen = False
    while True:
        frames = await read_runtime_sse_events(
            run.id,
            after_id=cursor,
        )
        for event_id, frame in frames:
            cursor = event_id
            event_type = parse_sse_frame(frame)[0]
            stream_end_seen = stream_end_seen or event_type == "stream_end"
            terminal_event_seen = terminal_event_seen or event_type in {
                "runtime_status",
                "stream_end",
            }
            yield frame
        if stream_end_seen:
            while True:
                final_frames = await read_runtime_sse_events(
                    run.id,
                    after_id=cursor,
                    block_ms=None,
                )
                if not final_frames:
                    return
                for event_id, frame in final_frames:
                    cursor = event_id
                    yield frame
        async with async_session() as db:
            current = await db.get(RuntimeRun, run.id)
            status = current.status if current is not None else RuntimeRunStatus.FAILED.value
            reason = current.status_reason if current is not None else "runtime_run_missing"
        if status in RuntimeRunStatus.terminal():
            while True:
                final_frames = await read_runtime_sse_events(
                    run.id,
                    after_id=cursor,
                    block_ms=None,
                )
                if not final_frames:
                    break
                for event_id, frame in final_frames:
                    cursor = event_id
                    terminal_event_seen = terminal_event_seen or parse_sse_frame(frame)[0] in {
                        "runtime_status",
                        "stream_end",
                    }
                    yield frame
            if not terminal_event_seen:
                yield format_sse(
                    "runtime_status",
                    {"run_id": run.id, "status": status, "status_reason": reason},
                )
            return
        if not frames:
            yield format_sse("keepalive", {"run_id": run.id})


@router.get("/runs/{run_id}/events")
async def reconnect_chat_runtime_run_events(
    run_id: str,
    request: Request,
    after_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _get_owned_runtime_run(db, run_id, user)
    cursor = after_id or request.headers.get("Last-Event-ID") or "0-0"
    lease = await acquire_chat_stream_lease(scope="chat-reconnect")
    return await _chat_streaming_response(
        db,
        lease,
        _durable_runtime_event_stream(run, after_id=cursor),
    )


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


async def _resolve_task_session_request_agent(
    db: AsyncSession,
    user: User,
    *,
    requested_agent_id: str | None,
    conversation_id: str | None = None,
    workspace_id: str | None,
    thread_ref_kind: str | None,
    thread_ref_id: str | None,
) -> str | None:
    """Apply an interactive Task's Host before request-level Agent work."""

    from packages.core.services.task_session import (
        TaskSessionHostError,
        task_session_host_for_conversation,
        task_session_host_for_thread,
    )

    try:
        if conversation_id:
            host = await task_session_host_for_conversation(
                db,
                conversation_id=conversation_id,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
            )
        else:
            host = await task_session_host_for_thread(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                thread_ref_kind=thread_ref_kind,
                thread_ref_id=thread_ref_id,
            )
    except LookupError as exc:
        raise HTTPException(404, "Conversation not found") from exc
    except TaskSessionHostError as exc:
        raise HTTPException(409, str(exc)) from exc
    return host.agent_id if host else requested_agent_id


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
    manual_skill_refs: str | None = None,
    user_id: str | None = None,
    user_role: str | None = None,
) -> ChatManualSkillTurn:
    try:
        return await prepare_chat_manual_skill_turn(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
            user_id=user_id,
            user_role=user_role,
            message=message,
            manual_skill_ids=manual_skill_ids,
            manual_skill_refs=manual_skill_refs,
        )
    except ManualSkillReferenceError as exc:
        raise HTTPException(400, str(exc)) from exc
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
    "flows": "flows",
}
_VIDEO_ASPECT_RATIO_ALIASES = {
    "auto": "adaptive",
    "smart": "adaptive",
    "adaptive": "adaptive",
    "智能": "adaptive",
    "自适应": "adaptive",
}
_VIDEO_ASPECT_RATIO_CHOICES = {"adaptive", "21:9", "16:9", "4:3", "3:4", "1:1", "9:16"}


class _VideoGenerationMode(StrEnum):
    AUTO = "auto"
    NATIVE_MOTION = "native_motion"
    AI_VIDEO = "ai_video"


class _VideoEditRouteState(StrEnum):
    INACTIVE = "inactive"
    SKILL_SANDBOX = "skill_sandbox"
    EDIT_SESSION = "edit_session"


class _VideoSandboxSkill(StrEnum):
    EDIT_SKILL = "video-edit"
    EDIT_RUNTIME = "video-edit-runtime"


_LIVE_VIDEO_SANDBOX_STATUSES = frozenset({"ready", "executing"})
_VIDEO_GENERATION_MODE_ALIASES = {
    "auto": _VideoGenerationMode.AUTO,
    "native": _VideoGenerationMode.NATIVE_MOTION,
    "motion": _VideoGenerationMode.NATIVE_MOTION,
    "native_motion": _VideoGenerationMode.NATIVE_MOTION,
    "coded_motion": _VideoGenerationMode.NATIVE_MOTION,
    "code_motion": _VideoGenerationMode.NATIVE_MOTION,
    "ai": _VideoGenerationMode.AI_VIDEO,
    "model": _VideoGenerationMode.AI_VIDEO,
    "generated": _VideoGenerationMode.AI_VIDEO,
    "ai_generated": _VideoGenerationMode.AI_VIDEO,
    "ai_video": _VideoGenerationMode.AI_VIDEO,
}
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
        raw_generation_mode = payload.get("generation_mode")
        generation_mode = str(
            raw_generation_mode or _VideoGenerationMode.AUTO
        ).strip().lower().replace("-", "_")
        payload["generation_mode"] = _VIDEO_GENERATION_MODE_ALIASES.get(
            generation_mode,
            _VideoGenerationMode.AUTO,
        )
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


def _video_edit_skill_input(
    prompt_text: str,
    payload: dict,
    attachments: FileAttachments,
) -> str:
    sections = [prompt_text]
    if attachments.text_context:
        sections.append(f"<attached_files>\n{attachments.text_context}\n</attached_files>")

    selected_urls: list[str] = []
    for urls, media_flag in (
        (list(attachments.image_urls or []), "image"),
        (list(attachments.video_urls or []), "video"),
        (list(attachments.audio_urls or []), "audio"),
    ):
        if attachments.attachment_refs:
            urls = _direct_media_selected_urls(
                urls,
                prompt=prompt_text,
                attachments=attachments,
                media_flag=media_flag,
            )
        for url in urls:
            value = str(url or "").strip()
            if value and value not in selected_urls:
                selected_urls.append(value)
    if selected_urls:
        sections.append(
            "<media_references>\n"
            + "\n".join(f"- {url}" for url in selected_urls)
            + "\n</media_references>"
        )

    settings = {
        key: payload.get(key)
        for key in (
            "aspect_ratio",
            "clip_duration_seconds",
            "resolution",
            "audio_policy",
            "reference_policy",
        )
        if payload.get(key) not in {None, ""}
    }
    if settings:
        sections.append(
            "<video_mode_settings>\n"
            + json.dumps(settings, ensure_ascii=False, indent=2)
            + "\n</video_mode_settings>"
        )
    return "\n\n".join(sections)


async def _live_video_sandbox_matches_skill(
    sandbox_id: str,
    expected_skill: _VideoSandboxSkill,
) -> bool:
    """Read Sandbox state without extending its lease."""

    normalized_id = str(sandbox_id or "").strip()
    if not normalized_id:
        return False
    try:
        from packages.core.services.sandbox_sdk import SandboxClient

        sandbox_url = get_settings().SANDBOX_SERVICE_URL.strip()
        if not sandbox_url:
            return False
        client = SandboxClient(base_url=sandbox_url, timeout=10.0)
        try:
            info = await client.status(normalized_id)
        finally:
            await client.close()
        status = getattr(info, "status", "")
        normalized_status = str(getattr(status, "value", status) or "").strip().lower()
        return (
            normalized_status in _LIVE_VIDEO_SANDBOX_STATUSES
            and str(getattr(info, "skill_name", "") or "").strip()
            == expected_skill.value
        )
    except Exception:
        return False


async def _conversation_video_edit_route_state(
    conversation_id: str | None,
    *,
    entity_id: str,
    user_id: str,
) -> _VideoEditRouteState:
    """Resolve Auto-mode continuation from owned, live session state only."""

    normalized_conversation_id = str(conversation_id or "").strip()
    if not normalized_conversation_id:
        return _VideoEditRouteState.INACTIVE

    from packages.core.ai.runtime import (
        runtime_load_sandbox_context,
        runtime_sandbox_context_owner_matches,
    )
    from packages.core.ai.runtime.video_edit_sessions import (
        assert_video_edit_session_owner,
        load_conversation_video_edit_session,
    )

    edit_session = await load_conversation_video_edit_session(
        normalized_conversation_id,
    )
    if isinstance(edit_session, dict):
        try:
            assert_video_edit_session_owner(
                edit_session,
                entity_id=entity_id,
                user_id=user_id,
                conversation_id=normalized_conversation_id,
            )
        except PermissionError:
            edit_session = None
        if edit_session and await _live_video_sandbox_matches_skill(
            str(edit_session.get("sandbox_id") or ""),
            _VideoSandboxSkill.EDIT_RUNTIME,
        ):
            return _VideoEditRouteState.EDIT_SESSION

    skill_context = await runtime_load_sandbox_context(normalized_conversation_id)
    if (
        runtime_sandbox_context_owner_matches(
            skill_context,
            entity_id=entity_id,
            user_id=user_id,
        )
        and str((skill_context or {}).get("skill_id") or "").strip()
        == _VideoSandboxSkill.EDIT_SKILL.value
        and await _live_video_sandbox_matches_skill(
            str((skill_context or {}).get("sandbox_id") or ""),
            _VideoSandboxSkill.EDIT_SKILL,
        )
    ):
        return _VideoEditRouteState.SKILL_SANDBOX

    return _VideoEditRouteState.INACTIVE


def _chat_mode_direct_tool_calls(
    *,
    chat_mode: str | None,
    chat_mode_payload: str | dict | None,
    prompt: str,
    attachments: FileAttachments,
    manual_skill_refs: list[dict] | None = None,
    video_edit_route_state: _VideoEditRouteState = _VideoEditRouteState.INACTIVE,
) -> list[dict]:
    """Build deterministic media tool calls for mode-specific composer sends."""
    mode = _normalize_chat_mode(chat_mode)
    prompt_text = str(prompt or "").strip()
    if not prompt_text or manual_skill_refs:
        return []

    if mode in {None, "auto"} and video_edit_route_state is not _VideoEditRouteState.INACTIVE:
        return [{
            "name": "invoke_skill",
            "arguments": {
                "skill": _VideoSandboxSkill.EDIT_SKILL.value,
                "input": _video_edit_skill_input(prompt_text, {}, attachments),
            },
        }]

    if mode not in {"image", "video", "audio"}:
        return []
    payload = _parse_chat_mode_payload(chat_mode_payload, mode)

    if mode == "image":
        return [_image_chat_mode_tool_call(prompt_text, payload, attachments)]

    if mode == "audio":
        return [_audio_chat_mode_tool_call(prompt_text, payload)]

    output_type = str(payload.get("output_type") or "single_clip").strip().lower()
    if output_type not in _DIRECT_VIDEO_OUTPUT_TYPES:
        return []
    if payload["generation_mode"] in {
        _VideoGenerationMode.AUTO,
        _VideoGenerationMode.NATIVE_MOTION,
    }:
        return [{
            "name": "invoke_skill",
            "arguments": {
                "skill": _VideoSandboxSkill.EDIT_SKILL.value,
                "input": _video_edit_skill_input(prompt_text, payload, attachments),
            },
        }]

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
    editor_context: dict | None = None,
) -> str | list:
    text_sections = [llm_base_message]
    current_document_context = render_editor_current_document_user_context(editor_context)
    if current_document_context:
        text_sections.append(current_document_context)
    if attachments.text_context:
        text_sections.append(
            f"<attached_files>\n{attachments.text_context}\n</attached_files>"
        )
    text_part = "\n\n".join(text_sections)

    if attachments.image_blocks and not _is_direct_media_generation_turn(direct_tool_calls):
        return [
            {"type": "text", "text": text_part},
            *attachments.image_blocks,
        ]
    return text_part


def _parse_editor_context_request(value: str | dict | None) -> dict | None:
    try:
        return runtime_parse_editor_context(value)
    except EditorCurrentDocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


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


def _video_generation_mode_prompt(generation_mode: str | None) -> str:
    try:
        mode = _VideoGenerationMode(generation_mode or _VideoGenerationMode.AUTO)
    except ValueError:
        mode = _VideoGenerationMode.AUTO
    if mode is _VideoGenerationMode.NATIVE_MOTION:
        return (
            "Selected generation mode: native_motion/coded motion. Do not call a video-generation model or "
            "generate_file(kind='video'). Invoke the built-in video-edit skill and let it own the complete "
            "professional editing workflow. It must build an editable coded composition, run strict checks, "
            "generate review snapshots, and stop for explicit user approval before final rendering. Do not substitute "
            "manor.video_edit_recipe or a fixed motion preset unless the video runtime is unavailable and the user "
            "explicitly accepts the lower-fidelity fast/editable fallback. Do not stop at a prose plan."
        )
    if mode is _VideoGenerationMode.AI_VIDEO:
        return (
            "Selected generation mode: ai_video. Generate new footage with generate_file(kind='video'). Video "
            "generation is async and returns status='pending' with a job_id. You MUST call wait_media_jobs with "
            "that job_id, then report the completed video or the real failure reason. Never claim success while a "
            "video job is pending. Keep generated clips replaceable in the final edit when the request is a composed video."
        )
    return (
        "Selected generation mode: auto. Decide per scene instead of routing every video request to a video model. "
        "For video editing, product demos, UI walkthroughs, promos, explainers, captioned videos, or any final containing "
        "UI, typography, diagrams, particles, product animation, or brand motion, invoke the built-in video-edit skill "
        "once and let it own the final composition, quality review, and render. "
        "Use generate_file(kind='video') only for photorealistic people, environments, or footage that cannot be built "
        "efficiently as motion graphics; the Video Edit composition may incorporate those completed clips as replaceable "
        "assets. Do not silently fall back to manor.video_edit_recipe or a fixed motion preset. For every AI-generated clip, "
        "call wait_media_jobs and wait for the real result before continuing."
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
            f"{shared}\nMode: Video creation. Follow the selected generation path below. Preserve available "
            "first_frame_url, last_frame_url, reference_urls, reference_video_urls, or audio_reference_urls exactly "
            "when the user provided them. A single video-model clip is limited to 15 seconds; split longer AI-video "
            "sections into multiple <=15s clips and then compose them.\n"
            f"{_video_generation_mode_prompt(str(payload.get('generation_mode') or 'auto'))}\n"
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
        "flows": (
            f"{shared}\nMode: Workspace Flows. First use search_tools to load list_workspace_flows, "
            "call list_workspace_flows, then use search_tools to load start_workspace_flow. Select the Flow "
            "that matches the user's request and call start_workspace_flow with the complete source brief. "
            "If matching Flow names exist in multiple Workspaces, require the user to name the Workspace; "
            "do not guess, use recency, or start more than one Flow."
        ),
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
    chat_mode: str | None,
    chat_mode_prompt: str | None,
    direct_tool_calls: list[dict] | None,
    intent_routing_metadata: dict | None = None,
    approval_runtime_metadata: dict | None = None,
    origin_user_message_id: str | None = None,
    voice_session_mode: str | None = None,
) -> dict:
    metadata = dict(getattr(file_context_turn, "runtime_metadata", None) or {})
    if intent_routing_metadata:
        metadata.update(intent_routing_metadata)
    metadata["chat_mode"] = _normalize_chat_mode(chat_mode) or "auto"
    if origin_user_message_id:
        metadata["origin_user_message_id"] = origin_user_message_id
    if voice_session_mode == "chat_gateway":
        # Only an internal WebSocket-created Request can set this state. It
        # changes delivery style without weakening the ordinary Chat surface,
        # tool policy, approval boundary, or persistence path.
        metadata["voice_session_mode"] = voice_session_mode
    if chat_mode_prompt:
        metadata["chat_mode_prompt"] = chat_mode_prompt
    if direct_tool_calls:
        metadata["forced_tool_calls"] = direct_tool_calls
    if approval_runtime_metadata:
        metadata.update(approval_runtime_metadata)
    return metadata


# ── SSE Streaming ──


@router.get(
    "/flow-entrypoints",
    response_model=list[GlobalChatFlowEntrypointResponse],
)
async def list_global_chat_flow_entrypoints(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.workspace_workflow_router import (
        list_global_chat_flow_entrypoints as list_entrypoints,
    )

    rows = await list_entrypoints(
        db,
        entity_id=user.entity_id,
        user=user,
        require_control=True,
    )
    return [
        GlobalChatFlowEntrypointResponse(
            **entrypoint.public_dict(),
            workspace_id=workspace.id,
            workspace_name=workspace.name,
        )
        for entrypoint, _binding, _workflow, workspace in rows
    ]


@router.post("/flow-entrypoints/{binding_id}/stream")
async def stream_global_chat_flow_entrypoint(
    binding_id: str,
    message: str = Form(...),
    conversation_id: str | None = Form(None),
    agent_id: str | None = Form(None),
    local_worker_id: str | None = Form(None),
    document_ids: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    _gate=Depends(require_plan("ai_budget_usd")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deterministically invoke one selected Workspace Flow from personal Chat."""
    from apps.api.routers.workspace_chat import _workspace_entrypoint_started_stream
    from packages.core.services.workspace_flow_launcher import launch_workspace_flow
    from packages.core.services.workspace_workflow_router import (
        get_global_chat_flow_entrypoint,
    )

    resolved = await get_global_chat_flow_entrypoint(
        db,
        entity_id=user.entity_id,
        user=user,
        binding_id=binding_id,
    )
    if resolved is None:
        raise HTTPException(404, "Flow not found")
    entrypoint, binding, _workflow, workspace = resolved
    file_context_turn = await _build_attachments(
        message,
        document_ids,
        files,
        user.entity_id,
        db,
        workspace_id=workspace.id,
        user_id=user.id,
    )
    cleaned_message = file_context_turn.cleaned_message.strip()
    if not cleaned_message:
        cleaned_message = entrypoint.title.rstrip(".!?。！？") + "."
    title = cleaned_message.split("\n", 1)[0][:100].strip() if not conversation_id else None
    try:
        conversation = await get_or_create_conversation(
            db,
            user.entity_id,
            user.id,
            agent_id=agent_id,
            workspace_id=None,
            conversation_id=conversation_id,
            title=title,
            conversation_surface=ConversationSurfaceKind.ORDINARY_CHAT,
        )
    except (LookupError, PermissionError):
        raise HTTPException(404, "Conversation not found")
    if conversation.workspace_id:
        raise HTTPException(409, "Use Workspace Chat to run a Flow in this conversation")
    if local_worker_id:
        from packages.core.services.local_worker_targeting import (
            select_conversation_local_worker_target,
        )

        try:
            await select_conversation_local_worker_target(
                db,
                conversation_id=conversation.id,
                entity_id=user.entity_id,
                user_id=user.id,
                worker_id=local_worker_id,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    saved_text = runtime_saved_message_with_file_references(
        cleaned_message,
        file_context_turn.attachments,
    )
    origin_message = await add_message(
        db,
        conversation.id,
        role="user",
        content=saved_text,
        attachments=file_context_turn.attachments.attachment_refs or None,
        refs=[
            {"type": "workflow", "id": entrypoint.workflow_id, "title": entrypoint.title},
            {"type": "workspace", "id": workspace.id, "title": workspace.name},
        ],
        meta={"author_user_id": user.id, "chat_mode": "flows"},
    )
    lease = await acquire_chat_stream_lease(scope="chat")
    try:
        started = await launch_workspace_flow(
            db,
            source="global_chat",
            entrypoint=entrypoint,
            binding=binding,
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            origin_message_id=origin_message.id,
            source_brief=cleaned_message,
            attachments=file_context_turn.attachments,
            starter_policy="always_review",
        )
    except PermissionError as exc:
        await lease.release()
        raise HTTPException(403, str(exc)) from exc
    except (LookupError, ValueError) as exc:
        await lease.release()
        raise HTTPException(409, str(exc)) from exc
    except BaseException:
        await lease.release()
        raise
    return await _chat_streaming_response(
        db,
        lease,
        _workspace_entrypoint_started_stream(started),
    )


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
    manual_skill_refs: str | None = Form(None),
    chat_mode: str | None = Form(None),
    chat_mode_payload: str | None = Form(None),
    response_surface_submission: str | None = Form(None),
    disable_tools: bool = Form(False),
    blocked_tools: str | None = Form(None),
    editor_context: str | None = Form(None),
    conversation_surface: str | None = Form(None),
    ephemeral: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message and stream the AI response via SSE.

    Accepts multipart/form-data with optional file attachments and
    knowledge-base document IDs (comma-separated). File contents are
    extracted and injected into the LLM context automatically.
    """
    requested_conversation_surface = _parse_chat_conversation_surface(
        conversation_surface
    )
    parsed_editor_context = _parse_editor_context_request(editor_context)
    ai_edit_target: AiEditTargetIdentity | None = None
    if requested_conversation_surface is ConversationSurfaceKind.AI_EDIT:
        if not parsed_editor_context:
            raise HTTPException(422, "AI Edit requires editor context")
        ai_edit_target = _parse_ai_edit_target(parsed_editor_context)
        if ephemeral:
            raise HTTPException(409, "AI Edit sessions require persisted context")
        if workspace_context or workspace_id or thread_ref_kind or thread_ref_id:
            raise HTTPException(409, "AI Edit sessions cannot use Workspace chat scope")
    if conversation_id:
        preflight_conversation = await _get_accessible_conversation(
            db,
            user,
            conversation_id,
        )
        if not ConversationSurfaceMetadataFactory.matches(
            preflight_conversation.meta,
            requested_conversation_surface,
        ):
            raise HTTPException(404, "Conversation not found")
        if (
            ai_edit_target is not None
            and not ConversationSurfaceMetadataFactory.matches_ai_edit_target(
                preflight_conversation.meta,
                ai_edit_target,
            )
        ):
            raise HTTPException(404, "Conversation not found")
    parsed_response_surface_submission = _parse_response_surface_submission(
        response_surface_submission
    )
    if parsed_response_surface_submission is not None and (
        ephemeral
        or files
        or document_ids
        or manual_skill_ids
        or manual_skill_refs
        or chat_mode
        or chat_mode_payload
        or disable_tools
        or blocked_tools
        or editor_context
    ):
        raise HTTPException(409, "Response surface submission cannot include chat controls")
    response_surface_bound = False
    response_surface_agent_subscription_id: str | None = None
    if parsed_response_surface_submission is not None:
        if not conversation_id:
            raise HTTPException(422, "Response surface submission requires a conversation")
        preflight_conversation = await _get_accessible_conversation(
            db,
            user,
            conversation_id,
        )
        parsed_response_surface_submission = await _bind_response_surface_submission(
            db,
            conversation_id=preflight_conversation.id,
            receipt=parsed_response_surface_submission,
        )
        response_surface_bound = True
        replay = await _find_response_surface_submission_replay(
            db,
            conversation_id=preflight_conversation.id,
            user_id=user.id,
            submission=parsed_response_surface_submission,
        )
        if replay is not None:
            assistant, runtime_run = replay
            source = _response_surface_submission_replay_source(
                preflight_conversation.id,
                assistant,
                runtime_run,
            )
            replay_lease = await acquire_chat_stream_lease(scope="chat-replay")
            return await _chat_streaming_response(
                db,
                replay_lease,
                source,
                response_surface_event_id=parsed_response_surface_submission["eventId"],
            )
    response_surface_event_id = (
        parsed_response_surface_submission["eventId"]
        if parsed_response_surface_submission is not None
        else None
    )
    pending_approval_turn = await _require_chat_budget_unless_pending_approval(
        db,
        user=user,
        conversation_id=conversation_id,
        message=message,
    )
    workspace_id, thread_ref_kind, thread_ref_id = await _resolve_chat_workspace_scope(
        db,
        user,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        workspace_context=workspace_context,
    )
    if parsed_response_surface_submission is not None:
        task_session_agent_id = await _resolve_task_session_request_agent(
            db,
            user,
            requested_agent_id=None,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
        )
        if task_session_agent_id:
            agent_id = task_session_agent_id
        else:
            response_surface_agent = await _response_surface_submission_agent(
                db,
                conversation=preflight_conversation,
                submission=parsed_response_surface_submission,
            )
            agent_id = response_surface_agent.agent_id
            response_surface_agent_subscription_id = (
                response_surface_agent.agent_subscription_id
            )
    else:
        agent_id = await _resolve_task_session_request_agent(
            db,
            user,
            requested_agent_id=agent_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
        )
    video_edit_route_state = await _conversation_video_edit_route_state(
        conversation_id,
        entity_id=user.entity_id,
        user_id=user.id,
    )
    lease = await acquire_chat_stream_lease(scope="chat")

    try:
        file_context_turn = await _build_attachments(
            message, document_ids, files, user.entity_id, db,
            workspace_id=workspace_id, user_id=user.id,
        )
        message = file_context_turn.cleaned_message
        attachments = file_context_turn.attachments

        # Workspace-configured Workflow Starters may claim an ordinary Chat
        # turn before Manor AI runs. Explicit Chat controls always win, and any
        # uncertain or failed classification falls through to the normal path.
        from packages.core.services.workspace_workflow_router import (
            auto_routing_allowed,
            classify_workspace_intent,
            conversation_message_is_pending_action_reply,
            get_workspace_chat_entrypoint,
            list_workspace_chat_entrypoints,
            start_workspace_chat_entrypoint,
            workflow_intent_attachment_descriptors,
        )

        if parsed_response_surface_submission is None and auto_routing_allowed(
            workspace_id=workspace_id,
            message=message,
            agent_id=agent_id,
            manual_skill_ids=manual_skill_ids,
            manual_skill_refs=manual_skill_refs,
            chat_mode=chat_mode,
            ephemeral=ephemeral,
            editor_context=editor_context,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
            disable_tools=disable_tools,
            blocked_tools=blocked_tools,
        ) and (
            video_edit_route_state is _VideoEditRouteState.INACTIVE
        ) and not await conversation_message_is_pending_action_reply(
            db,
            conversation_id,
            message,
        ):
            entrypoints = await list_workspace_chat_entrypoints(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id or "",
                intent_only=True,
                user=user,
                require_control=True,
            )
            decision = None
            if entrypoints:
                decision = await classify_workspace_intent(
                    entrypoints=entrypoints,
                    message=message,
                    attachment_refs=workflow_intent_attachment_descriptors(attachments),
                    entity_id=user.entity_id,
                    user_id=user.id,
                    workspace_id=workspace_id or "",
                )
            if decision is not None:
                resolved = await get_workspace_chat_entrypoint(
                    db,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id or "",
                    binding_id=decision.entrypoint.binding_id,
                )
                if resolved is not None:
                    entrypoint, binding, _workflow = resolved
                    started = await start_workspace_chat_entrypoint(
                        db,
                        entrypoint=entrypoint,
                        binding=binding,
                        entity_id=user.entity_id,
                        user_id=user.id,
                        workspace_id=workspace_id or "",
                        message=message,
                        attachments=attachments,
                        conversation_id=conversation_id,
                        route_source="intent",
                        confidence=decision.confidence,
                        reason=decision.reason,
                    )
                    from apps.api.routers.workspace_chat import (
                        _workspace_entrypoint_started_stream,
                    )

                    return await _chat_streaming_response(
                        db,
                        lease,
                        _workspace_entrypoint_started_stream(started),
                    )

        manual_skill_turn = await _prepare_manual_skill_turn(
            db,
            entity_id=user.entity_id,
            agent_id=agent_id,
            user_id=user.id,
            user_role=getattr(user, "role", None),
            message=message,
            manual_skill_ids=manual_skill_ids,
            manual_skill_refs=manual_skill_refs,
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
            video_edit_route_state=video_edit_route_state,
        )
        llm_message = _stream_llm_message_with_attachments(
            llm_base_message,
            attachments,
            direct_tool_calls,
            parsed_editor_context,
        )
    except BaseException:
        await lease.release()
        raise

    try:
        if ephemeral:
            turn_surface = _surface_for_chat_request(
                agent_id=agent_id,
                workspace_id=workspace_id,
                ephemeral=True,
                editor_context=parsed_editor_context,
            )
            return await _chat_streaming_response(
                db,
                lease,
                runtime_stream_chat_turn(
                    llm_message,
                    None,
                    surface=turn_surface,
                    entity_id=user.entity_id,
                    user_id=user.id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    manual_skill_refs=manual_skill_refs,
                    disable_tools=disable_tools,
                    blocked_tools=(
                        set(_parse_csv_names(blocked_tools))
                        | _chat_mode_blocked_tools(chat_mode, surface=turn_surface)
                    ),
                    editor_context=parsed_editor_context,
                    runtime_metadata=_runtime_metadata_for_chat_mode(
                        file_context_turn,
                        chat_mode=chat_mode,
                        chat_mode_prompt=chat_mode_prompt,
                        direct_tool_calls=direct_tool_calls,
                    ),
                    persist_messages=False,
                ),
            )
    except BaseException:
        await lease.release()
        raise

    if ephemeral:
        raise AssertionError("unreachable")

    try:
        # Get or create conversation
        # Auto-title from first message when creating a new conversation
        saved_user_base = _message_with_chat_mode_marker(
            manual_skill_turn.saved_user_base,
            chat_mode,
            chat_mode_payload,
        )
        _auto_title = saved_user_base.split("\n")[0][:100].strip() if not conversation_id else None
        conv = await get_or_create_conversation(
            db, user.entity_id, user.id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
            title=_auto_title,
            conversation_surface=requested_conversation_surface,
            ai_edit_target=ai_edit_target,
        )
        agent_id = await _resolve_task_session_request_agent(
            db,
            user,
            requested_agent_id=agent_id,
            conversation_id=conv.id,
            workspace_id=conv.workspace_id,
            thread_ref_kind=conv.thread_ref_kind,
            thread_ref_id=conv.thread_ref_id,
        )
        if not response_surface_bound:
            parsed_response_surface_submission = await _bind_response_surface_submission(
                db,
                conversation_id=conv.id,
                receipt=parsed_response_surface_submission,
            )
        if parsed_response_surface_submission is not None:
            canonical_submission_message = _response_surface_submission_message(
                parsed_response_surface_submission
            )
            saved_user_base = canonical_submission_message
            llm_base_message = canonical_submission_message
            llm_message = canonical_submission_message
    except (LookupError, PermissionError) as exc:
        await lease.release()
        raise HTTPException(404, "Conversation not found") from exc
    except ValueError as exc:
        await lease.release()
        raise HTTPException(409, str(exc)) from exc
    except BaseException:
        await lease.release()
        raise

    if _is_runtime_approval_rejected_message(llm_base_message):
        try:
            assistant_msg = await add_message(db, conv.id, role="assistant", content=_RUNTIME_APPROVAL_REJECTED_REPLY)
            await db.commit()
            return await _chat_streaming_response(
                db,
                lease,
                _runtime_approval_rejected_stream(
                    conv.id,
                    _RUNTIME_APPROVAL_REJECTED_REPLY,
                    assistant_msg.id,
                ),
            )
        except BaseException:
            await lease.release()
            raise

    approval_saved_text: str | None = None
    approval_runtime_metadata: dict | None = None
    intent_routing_metadata: dict | None = None
    save_user_message = True
    durable_run: RuntimeRun | None = None
    resolved_conversation_id = str(conv.id)
    try:
        replacement, resolved_saved_text, save_user_message, approval_runtime_metadata = await resolve_chat_approval_turn(
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
        elif pending_approval_turn:
            # The card changed between the preflight lookup and resolution. Do
            # not let a stale structured action become an ungated AI prompt.
            await require_plan("ai_budget_usd")(user=user, db=db)

        if replacement and _is_workflow_approval_resolution(approval_runtime_metadata):
            if save_user_message:
                await add_message(
                    db,
                    conv.id,
                    role="user",
                    content=resolved_saved_text or saved_user_base,
                    attachments=attachments.attachment_refs or None,
                    meta={"author_user_id": user.id},
                )
            assistant_msg = await add_message(
                db,
                conv.id,
                role="assistant",
                content=replacement,
            )
            return await _chat_streaming_response(
                db,
                lease,
                _runtime_approval_rejected_stream(conv.id, replacement, assistant_msg.id),
            )

        runtime_surface = _surface_for_chat_request(
            agent_id=agent_id,
            workspace_id=workspace_id,
            editor_context=parsed_editor_context,
        )
        from packages.core.services.chat_intent_routing import (
            auto_chat_intent_routing_allowed,
            classify_chat_intent_routing,
        )

        if (
            parsed_response_surface_submission is None
            and
            video_edit_route_state is _VideoEditRouteState.INACTIVE
            and auto_chat_intent_routing_allowed(
                surface=runtime_surface,
                chat_mode=_normalize_chat_mode(chat_mode),
                agent_id=agent_id,
                workspace_id=workspace_id,
                manual_skill_selected=bool(manual_skill_refs),
                editor_context=parsed_editor_context,
                ephemeral=False,
                disable_tools=disable_tools,
                blocked_tools=bool(_parse_csv_names(blocked_tools)),
                approval_turn=bool(replacement or pending_approval_turn),
                has_forced_tool_calls=bool(direct_tool_calls),
                has_attachments=bool(
                    attachments.attachment_refs
                    or attachments.text_context
                    or attachments.image_blocks
                ),
            )
        ):
            intent_routing = await classify_chat_intent_routing(
                db,
                user=user,
                message=llm_base_message,
                conversation_id=conv.id,
                runtime_metadata={
                    "chat_mode": _normalize_chat_mode(chat_mode) or "auto",
                },
            )
            intent_routing_metadata = intent_routing.runtime_metadata(
                request=llm_base_message,
            )
            disable_tools = disable_tools or intent_routing.execution_plan.disable_tools

        # Save user message in DB as plain text. The image bytes are only
        # multimodal for this turn, but the stable /api/v1/fs references must
        # remain in history so follow-up turns can use them for media tools.
        origin_user_message = None
        if save_user_message:
            saved_text = approval_saved_text or saved_user_base
            if not approval_saved_text:
                saved_text = runtime_saved_message_with_file_references(saved_text, attachments)
            # Stash the posting user so workspace chat can attribute the message
            # to its real author. Without this, every user message reads back with
            # no author_user_id and the UI renders all of them as the viewer's own.
            origin_user_message = await add_message(
                db, conv.id, role="user", content=saved_text,
                attachments=attachments.attachment_refs or None,
                response_surface_event_id=(
                    parsed_response_surface_submission["eventId"]
                    if parsed_response_surface_submission is not None
                    else None
                ),
                meta={
                    "author_user_id": user.id,
                    **(
                        {"response_surface_submission": parsed_response_surface_submission}
                        if parsed_response_surface_submission is not None
                        else {}
                    ),
                },
            )
        assistant_placeholder = await create_assistant_stream_placeholder(
            db,
            conv.id,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            author_subscription_id=response_surface_agent_subscription_id,
            meta=(
                assistant_message_origin_meta(
                    origin_user_message.id if origin_user_message else None
                )
                or None
            ),
        )
        blocked_tools_for_turn = (
            set(_parse_csv_names(blocked_tools))
            | _chat_mode_blocked_tools(chat_mode, surface=runtime_surface)
        )
        runtime_metadata_payload = _runtime_metadata_for_chat_mode(
            file_context_turn,
            chat_mode=chat_mode,
            chat_mode_prompt=chat_mode_prompt,
            direct_tool_calls=direct_tool_calls,
            intent_routing_metadata=intent_routing_metadata,
            approval_runtime_metadata=approval_runtime_metadata,
            origin_user_message_id=(
                origin_user_message.id if origin_user_message is not None else None
            ),
        )
        if response_surface_agent_subscription_id:
            runtime_metadata_payload["agent_subscription_id"] = (
                response_surface_agent_subscription_id
            )
        settings = get_settings()
        if (
            settings.MANOR_RUNTIME_EXECUTION_MODE == "durable"
            and settings.SANDBOX_COORDINATION_MODE == "external-runner"
        ):
            durable_run = await create_runtime_run(
                db,
                conversation_id=conv.id,
                assistant_message_id=assistant_placeholder.id,
                entity_id=user.entity_id,
                user_id=user.id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                execution_payload={
                    "message": llm_message,
                    "surface": getattr(runtime_surface, "value", runtime_surface),
                    "manual_skill_refs": manual_skill_refs,
                    "disable_tools": disable_tools,
                    "blocked_tools": sorted(blocked_tools_for_turn),
                    "editor_context": parsed_editor_context,
                    "runtime_metadata": runtime_metadata_payload,
                },
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if (
            parsed_response_surface_submission is not None
            and _integrity_error_constraint_name(exc)
            == _RESPONSE_SURFACE_EVENT_UNIQUE_INDEX
        ):
            source = await _response_surface_submission_replay_after_integrity_error(
                db,
                lease,
                conversation_id=resolved_conversation_id,
                user_id=user.id,
                submission=parsed_response_surface_submission,
            )
            if source is not None:
                return await _chat_streaming_response(
                    db,
                    lease,
                    source,
                    response_surface_event_id=response_surface_event_id,
                )
        await lease.release()
        raise HTTPException(409, "conversation_run_active") from exc
    except BaseException:
        await lease.release()
        raise

    # Stream response — don't pass the request-scoped db session;
    # the generator creates its own short-lived sessions to avoid
    # holding a DB connection for the entire SSE stream duration.
    try:
        if durable_run is not None:
            return await _chat_streaming_response(
                db,
                lease,
                _durable_runtime_event_stream(durable_run),
                conversation_id=resolved_conversation_id,
                runtime_run_id=durable_run.id,
                response_surface_event_id=response_surface_event_id,
            )
        return await _chat_streaming_response(
            db,
            lease,
            runtime_stream_chat_turn(
                llm_message,
                conv.id,
                surface=runtime_surface,
                entity_id=user.entity_id,
                user_id=user.id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                manual_skill_refs=manual_skill_refs,
                disable_tools=disable_tools,
                blocked_tools=blocked_tools_for_turn,
                editor_context=parsed_editor_context,
                assistant_message_id=assistant_placeholder.id,
                runtime_metadata=runtime_metadata_payload,
            ),
            conversation_id=resolved_conversation_id,
            response_surface_event_id=response_surface_event_id,
        )
    except BaseException:
        await lease.release()
        raise



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
    manual_skill_refs: str | None = Form(None),
    chat_mode: str | None = Form(None),
    chat_mode_payload: str | None = Form(None),
    blocked_tools: str | None = Form(None),
    editor_context: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
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
        manual_skill_refs_value = body.get("manual_skill_refs", manual_skill_refs)
        manual_skill_refs = (
            json.dumps(manual_skill_refs_value)
            if isinstance(manual_skill_refs_value, list)
            else manual_skill_refs_value
        )
        chat_mode = body.get("chat_mode", chat_mode)
        chat_mode_payload = body.get("chat_mode_payload", chat_mode_payload)
        blocked_tools = body.get("blocked_tools", blocked_tools)
        editor_context = body.get("editor_context", editor_context)
    if message is None:
        raise HTTPException(422, "message is required")
    parsed_editor_context = _parse_editor_context_request(editor_context)

    pending_approval_turn = await _require_chat_budget_unless_pending_approval(
        db,
        user=user,
        conversation_id=conversation_id,
        message=message,
    )

    workspace_id, thread_ref_kind, thread_ref_id = await _resolve_chat_workspace_scope(
        db,
        user,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        workspace_context=bool(workspace_context),
    )
    agent_id = await _resolve_task_session_request_agent(
        db,
        user,
        requested_agent_id=agent_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
    )
    video_edit_route_state = await _conversation_video_edit_route_state(
        conversation_id,
        entity_id=user.entity_id,
        user_id=user.id,
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
        user_id=user.id,
        user_role=getattr(user, "role", None),
        message=message,
        manual_skill_ids=manual_skill_ids,
        manual_skill_refs=manual_skill_refs,
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
        video_edit_route_state=video_edit_route_state,
    )
    llm_message = _stream_llm_message_with_attachments(
        llm_base_message,
        attachments,
        direct_tool_calls,
        parsed_editor_context,
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
            conversation_surface=ConversationSurfaceKind.ORDINARY_CHAT,
        )
        agent_id = await _resolve_task_session_request_agent(
            db,
            user,
            requested_agent_id=agent_id,
            conversation_id=conv.id,
            workspace_id=conv.workspace_id,
            thread_ref_kind=conv.thread_ref_kind,
            thread_ref_id=conv.thread_ref_id,
        )
    except (LookupError, PermissionError):
        raise HTTPException(404, "Conversation not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    voice_origin_user_message: Message | None = None
    voice_origin_message_id = (
        getattr(request.state, "voice_origin_message_id", None)
        if getattr(request.state, "voice_session_mode", None) == "chat_gateway"
        else None
    )
    if voice_origin_message_id:
        from packages.core.services.voice.work_queue import (
            validate_voice_origin_message,
        )

        candidate = await db.scalar(
            select(Message).where(Message.id == str(voice_origin_message_id))
        )
        try:
            voice_origin_user_message = validate_voice_origin_message(
                candidate,
                conversation_id=conv.id,
                content=llm_base_message,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

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
    approval_runtime_metadata: dict | None = None
    intent_routing_metadata: dict | None = None
    save_user_message = True
    replacement, resolved_saved_text, save_user_message, approval_runtime_metadata = await resolve_chat_approval_turn(
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
    elif pending_approval_turn:
        # The card changed between the preflight lookup and resolution.  Do not
        # let a stale structured action become an ungated AI prompt.
        await require_plan("ai_budget_usd")(user=user, db=db)

    if replacement and _is_workflow_approval_resolution(approval_runtime_metadata):
        if save_user_message and voice_origin_user_message is None:
            await add_message(
                db,
                conv.id,
                role="user",
                content=resolved_saved_text or saved_user_base,
                attachments=attachments.attachment_refs or None,
                meta={"author_user_id": user.id},
            )
        assistant_msg = await add_message(
            db,
            conv.id,
            role="assistant",
            content=replacement,
        )
        await db.commit()
        return ChatMessageResponse(
            conversation_id=conv.id,
            message_id=assistant_msg.id,
            content=replacement,
            tool_calls_made=[],
            usage={},
            rounds=0,
        )

    turn_surface = _surface_for_chat_request(
        agent_id=agent_id,
        workspace_id=workspace_id,
        editor_context=parsed_editor_context,
    )
    from packages.core.services.chat_intent_routing import (
        auto_chat_intent_routing_allowed,
        classify_chat_intent_routing,
    )

    if (
        video_edit_route_state is _VideoEditRouteState.INACTIVE
        and auto_chat_intent_routing_allowed(
            surface=turn_surface,
            chat_mode=_normalize_chat_mode(chat_mode),
            agent_id=agent_id,
            workspace_id=workspace_id,
            manual_skill_selected=bool(manual_skill_refs),
            editor_context=parsed_editor_context,
            ephemeral=False,
            disable_tools=False,
            blocked_tools=bool(_parse_csv_names(blocked_tools)),
            approval_turn=bool(replacement or pending_approval_turn),
            has_forced_tool_calls=bool(direct_tool_calls),
            has_attachments=bool(
                attachments.attachment_refs
                or attachments.text_context
                or attachments.image_blocks
            ),
        )
    ):
        intent_routing = await classify_chat_intent_routing(
            db,
            user=user,
            message=llm_base_message,
            conversation_id=conv.id,
            runtime_metadata={
                "chat_mode": _normalize_chat_mode(chat_mode) or "auto",
                "voice_session_mode": getattr(
                    request.state,
                    "voice_session_mode",
                    None,
                ),
            },
        )
        intent_routing_metadata = intent_routing.runtime_metadata(
            request=llm_base_message,
        )

    # Save user message
    origin_user_message = voice_origin_user_message
    if save_user_message:
        saved_text = approval_saved_text or saved_user_base
        if not approval_saved_text:
            saved_text = runtime_saved_message_with_file_references(saved_text, attachments)
        # Attribute the message to its author so workspace chat can tell who
        # sent it (see /chat/stream above for the full rationale).
        if origin_user_message is None:
            origin_user_message = await add_message(
                db, conv.id, role="user", content=saved_text,
                attachments=attachments.attachment_refs or None,
                meta={"author_user_id": user.id},
            )
    await db.commit()

    # Run agentic loop
    result = await runtime_run_chat_turn(
        llm_message,
        conv.id,
        surface=turn_surface,
        entity_id=user.entity_id,
        user_id=user.id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        db=db,
        manual_skill_refs=manual_skill_refs,
        blocked_tools=(
            set(_parse_csv_names(blocked_tools))
            | _chat_mode_blocked_tools(chat_mode, surface=turn_surface)
        ),
        editor_context=parsed_editor_context,
        runtime_metadata=_runtime_metadata_for_chat_mode(
            file_context_turn,
            chat_mode=chat_mode,
            chat_mode_prompt=chat_mode_prompt,
            direct_tool_calls=direct_tool_calls,
            intent_routing_metadata=intent_routing_metadata,
            approval_runtime_metadata=approval_runtime_metadata,
            origin_user_message_id=(
                origin_user_message.id if origin_user_message is not None else None
            ),
            voice_session_mode=getattr(request.state, "voice_session_mode", None),
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
    _require_user_managed_conversation(
        await _get_accessible_conversation(db, user, conversation_id)
    )
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
    _require_deletable_conversation(
        await _get_accessible_conversation(db, user, conversation_id)
    )
    cancelled_runs: list[RuntimeRun] = []
    ok = await delete_conversation(
        db,
        conversation_id,
        user.entity_id,
        cancelled_runtime_runs=cancelled_runs,
    )
    if not ok:
        raise HTTPException(404, "Conversation not found")
    await db.commit()
    for run in cancelled_runs:
        await cancel_runtime_run_resources(run)


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
    return await _chat_message_responses(db, user, msgs)


@router.get(
    "/conversations/{conversation_id}/messages/page",
    response_model=MessagesPageResponse,
)
async def get_messages_page(
    conversation_id: str,
    limit: int = Query(75, ge=1, le=200),
    before: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_accessible_conversation(db, user, conversation_id)

    before_created_at, before_id = _decode_message_cursor(before)
    raw_limit = min(max((limit + 1) * 4, limit + 1), 800)
    raw_msgs = await list_messages_before(
        db,
        conversation_id,
        limit=raw_limit,
        before_created_at=before_created_at,
        before_id=before_id,
    )
    visible = _visible_chat_messages(raw_msgs)
    has_more = len(visible) > limit
    if has_more:
        visible = visible[-limit:]
    next_cursor = _encode_message_cursor(visible[0]) if has_more and visible else None
    return MessagesPageResponse(
        items=await _chat_message_responses(db, user, visible),
        has_more=has_more,
        next_cursor=next_cursor,
    )


@router.post("/messages/{message_id}/resolve", response_model=MessageResponse)
async def resolve_global_chat_action(
    message_id: str,
    req: ResolveChatActionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = (await db.execute(
        select(Message).where(Message.id == message_id)
    )).scalar_one_or_none()
    if message is None:
        raise HTTPException(404, "message not found")
    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == message.conversation_id,
            Conversation.entity_id == user.entity_id,
            Conversation.user_id == user.id,
        )
    )).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(404, "message not found")
    from packages.core.services.workflow_message_actions import (
        WorkflowMessageActionError,
        resolve_workflow_message_action,
    )

    try:
        result = await resolve_workflow_message_action(
            db,
            message=message,
            conversation=conversation,
            choice=req.choice,
            note=req.note,
            payload=req.payload,
            user=user,
        )
    except WorkflowMessageActionError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    if result is None:
        raise HTTPException(400, "Message is not a Workflow action")
    return _to_chat_message_response(result.message)


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
    conv = await _get_accessible_conversation(db, user, conversation_id)
    msg = (await db.execute(
        select(Message).where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
        )
    )).scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    target_decision = ChatFeedbackTargetPolicyFactory.create(
        ChatFeedbackTargetKind.RESPONSE
    ).evaluate(
        msg,
        workspace_scoped=conv.workspace_id is not None,
    )
    if not target_decision.eligible:
        raise HTTPException(422, target_decision.detail)

    content_preview = build_chat_feedback_content_preview(
        requested_preview=req.content_preview,
        message_content=msg.content,
        assistant_blocks=_message_assistant_blocks(msg),
    )
    request_preview = await resolve_chat_feedback_request_preview(
        db,
        message=msg,
        requested_preview=req.request_preview,
    )
    try:
        result = await persist_chat_message_feedback(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            conversation_id=conversation_id,
            message_id=message_id,
            rating=req.rating,
            content_preview=content_preview,
            request_preview=request_preview,
            target_kind=ChatFeedbackTargetKind.RESPONSE,
            target_id=message_id,
        )
    except ChatFeedbackTargetDeletedError as exc:
        await db.rollback()
        raise HTTPException(404, "Message not found") from exc
    except IntegrityError as exc:
        await db.rollback()
        if (
            classify_chat_feedback_integrity_error(exc)
            == ChatFeedbackIntegrityErrorKind.TARGET_DELETED
        ):
            raise HTTPException(404, "Message not found") from exc
        raise
    return ChatMessageFeedbackResponse(
        message_id=message_id,
        rating=result.rating,
        mutation_sequence=result.mutation_sequence,
        mutation_status=result.mutation_status,
        updated_at=result.updated_at.isoformat(),
        target_kind=ChatFeedbackTargetKind.RESPONSE,
        target_id=message_id,
    )


@router.get(
    "/conversations/{conversation_id}/feedback",
    response_model=list[ChatMessageFeedbackSnapshotResponse],
)
async def get_conversation_feedback(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore the current user's ratings for an accessible conversation."""
    conversation = await _get_accessible_conversation(
        db,
        user,
        conversation_id,
    )
    return await list_chat_message_feedback(
        db,
        user_id=user.id,
        conversation_id=conversation_id,
        workspace_id=conversation.workspace_id,
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
    _require_user_managed_conversation(
        await _get_accessible_conversation(db, user, conversation_id)
    )
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
    _require_user_managed_conversation(
        await _get_accessible_conversation(db, user, conversation_id)
    )
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
    _require_user_managed_conversation(
        await _get_accessible_conversation(db, user, conversation_id)
    )
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
    _require_user_managed_conversation(
        await _get_accessible_conversation(db, user, conversation_id)
    )
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
                role=m.role,
                content=_redact_local_fs_urls(_message_public_content(m)),
                tool_calls=_redact_local_fs_urls(
                    runtime_public_tool_calls(m.tool_calls)
                ),
                assistant_blocks=_redact_local_fs_urls(_message_assistant_blocks(m)),
                token_usage=m.token_usage,
                attachments=_redact_local_fs_urls(m.attachments),
                hitl_requests=_redact_local_fs_urls(_message_hitl_requests(m)),
                workflow_result=_redact_local_fs_urls(_message_workflow_result(m)),
                **_message_limit_meta(m),
                created_at=m.created_at.isoformat() if m.created_at else None,
            )
            for m in messages
            if not (m.role == "user" and is_internal_file_permission_marker(m.content))
        ],
    )


# ── Text-to-Speech ──

@router.post("/tts")
async def text_to_speech(
    body: SpeechRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scope = await authenticated_audio_scope(
        db, user, conversation_id=body.conversation_id, workspace_id=body.workspace_id,
    )
    lease = await acquire_audio_lease()
    try:
        return await chat_speech_response(db, scope, body.text, body.voice)
    finally:
        await lease.release()


# ── Live Voice Session (OpenAI Realtime API) ──
