"""Authenticated browser-call entry point shared by Chat, Workspace and Webchat."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from collections.abc import Callable

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.datastructures import Headers
from websockets.exceptions import WebSocketException

from apps.api.chat_audio import (
    ChatAudioScope,
    acquire_audio_lease,
    authenticated_audio_scope,
    chat_speech_response,
    enforce_public_audio_budget,
    speech_language,
    speech_text,
    transcribe_chat_upload,
)
from apps.api.deps import get_current_user, security
from packages.core.database import async_session
from packages.core.services.chat_concurrency import ChatConcurrencyExceeded
from packages.core.services.voice.browser import BrowserVoiceSession
from packages.core.services.voice.gateway_call import GatewayVoiceSession
from packages.core.services.voice.profiles import (
    DEFAULT_VOICE_PROFILE,
    VoiceProfile,
)
from packages.core.services.voice.work_queue import (
    VoiceWorkReceipt,
    admit_voice_work,
    claim_voice_work,
    finish_voice_work,
    interrupt_voice_work,
    recover_voice_work,
    voice_work_context,
)
from packages.core.services.voice.work_router import classify_voice_work_followup
from packages.core.services.voice.work_types import VoiceWorkAction, VoiceWorkDecision
from packages.core.services.voice.realtime import (
    VoiceAgentOutcome,
    open_realtime_connection,
    resolve_realtime_route,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/audio", tags=["audio"])
# The browser allows 60s after WS open. Keep all server setup within 45s,
# spending at most 20s on Realtime so the shared speech fallback has time.
CALL_SETUP_TIMEOUT_SECONDS = 45
REALTIME_SETUP_TIMEOUT_SECONDS = 20


def dominant_call_language(messages: list[str]) -> str | None:
    """Infer a stable CJK transcription hint from recent user-authored text."""

    counts = {"zh": 0, "ja": 0, "ko": 0}
    for text in messages:
        han = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
        kana = len(re.findall(r"[\u3040-\u30ff]", text))
        counts["ko"] += len(re.findall(r"[\uac00-\ud7af]", text))
        if kana:
            counts["ja"] += han + kana
        else:
            counts["zh"] += han
    language, count = max(counts.items(), key=lambda item: item[1])
    other = max(value for key, value in counts.items() if key != language)
    return language if count >= 4 and count >= other * 2 else None


async def call_transcription_language(db, scope: ChatAudioScope) -> str | None:
    """Use conversation continuity without sending history to another model."""

    if not scope.conversation_id:
        return None
    from sqlalchemy import select

    from packages.core.models.task import Message

    try:
        messages = (
            await db.execute(
                select(Message.content)
                .where(
                    Message.conversation_id == scope.conversation_id,
                    Message.role == "user",
                    Message.content.is_not(None),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(12)
            )
        ).scalars().all()
    except Exception:
        logger.warning("Could not resolve the optional call language hint")
        return None
    return dominant_call_language([str(message) for message in messages if message])


class CallStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = "start"
    token: str | None = Field(default=None, max_length=8192)
    conversation_id: str | None = Field(default=None, max_length=100)
    workspace_id: str | None = Field(default=None, max_length=100)
    agent_id: str | None = Field(default=None, max_length=100)
    thread_ref_kind: str | None = Field(default=None, max_length=80)
    thread_ref_id: str | None = Field(default=None, max_length=100)
    public_token: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=100)
    voice: VoiceProfile = DEFAULT_VOICE_PROFILE

    @model_validator(mode="after")
    def validate_scope(self):
        if self.type != "start":
            raise ValueError("Call must start with authentication")
        if self.public_token:
            if not self.session_id or any(
                (self.conversation_id, self.workspace_id, self.agent_id, self.thread_ref_kind, self.thread_ref_id)
            ):
                raise ValueError("Invalid public call scope")
        elif not self.token or self.session_id:
            raise ValueError("Authentication is required")
        return self


def call_request(ws: WebSocket, start: CallStart) -> Request:
    # Reuse the complete HTTP authentication boundary, including revocation,
    # membership, suspension and view-only support-session restrictions. Never
    # put bearer tokens or provider credentials in websocket URLs/logs.
    scope = dict(ws.scope)
    scope.update(type="http", method="POST")
    scope["headers"] = [
        (k, v)
        for k, v in ws.scope.get("headers", [])
        if k.lower() not in (b"authorization", b"cookie", b"content-type")
    ]
    if start.token:
        scope["headers"].append((b"authorization", f"Bearer {start.token}".encode()))
    call = Request(scope)
    call.state.voice_session_mode = "chat_gateway"
    return call


async def resolve_call_scope(db, request: Request, start: CallStart, *, create: bool) -> ChatAudioScope:
    if start.public_token:
        from apps.api.routers.public_chat import _public_audio_scope

        result = await _public_audio_scope(db, start.public_token, request, start.session_id)
        await db.commit()
        return result
    user = await get_current_user(request, credentials=await security(request), db=db)
    if create:
        from apps.api.routers.chat import (
            ConversationSurfaceKind,
            _resolve_chat_workspace_scope,
            _resolve_task_session_request_agent,
            get_or_create_conversation,
        )

        workspace_id, ref_kind, ref_id = await _resolve_chat_workspace_scope(
            db,
            user,
            conversation_id=start.conversation_id,
            workspace_id=start.workspace_id,
            workspace_context=bool(start.workspace_id),
            thread_ref_kind=start.thread_ref_kind,
            thread_ref_id=start.thread_ref_id,
        )
        agent_id = await _resolve_task_session_request_agent(
            db,
            user,
            requested_agent_id=start.agent_id,
            conversation_id=start.conversation_id,
            workspace_id=workspace_id,
            thread_ref_kind=ref_kind,
            thread_ref_id=ref_id,
        )
        try:
            conv = await get_or_create_conversation(
                db,
                user.entity_id,
                user.id,
                conversation_id=start.conversation_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                thread_ref_kind=ref_kind,
                thread_ref_id=ref_id,
                title="Voice conversation" if not start.conversation_id else None,
                conversation_surface=ConversationSurfaceKind.ORDINARY_CHAT,
            )
        except (LookupError, PermissionError) as exc:
            raise HTTPException(404, "Conversation not found") from exc
        start.conversation_id = conv.id
        start.workspace_id = conv.workspace_id
        start.agent_id = conv.agent_id
        await db.commit()
    return await authenticated_audio_scope(
        db,
        user,
        conversation_id=start.conversation_id,
        workspace_id=start.workspace_id,
    )


async def call_agent(
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
    text: str,
    *,
    origin_message_id: str | None = None,
) -> VoiceAgentOutcome:
    if start.public_token:
        await enforce_public_audio_budget(
            request,
            start.public_token,
            start.session_id or "",
            "turn",
        )
    async with async_session() as db:
        if await resolve_call_scope(db, request, start, create=False) != scope:
            raise HTTPException(409, "Chat configuration changed; start a new call")
        if start.public_token:
            from apps.api.routers.public_chat import MessageRequest, send_message

            request_state = getattr(request, "state", None)
            previous_origin = getattr(request_state, "voice_origin_message_id", None)
            if request_state is not None:
                request_state.voice_origin_message_id = origin_message_id
            try:
                result = await send_message(
                    start.public_token,
                    MessageRequest(session_id=start.session_id, text=text),
                    request,
                    db,
                )
            finally:
                if request_state is not None:
                    request_state.voice_origin_message_id = previous_origin
            if result.get("conversation_id") not in (None, scope.conversation_id):
                raise HTTPException(409, "Chat configuration changed; start a new call")
            status = result.get("status")
            if status in {"cancelled", "canceled"}:
                return VoiceAgentOutcome(
                    status="cancelled",
                    conversation_id=scope.conversation_id,
                    spoken_reply="",
                )
            if status == "approval_required":
                return VoiceAgentOutcome(
                    status="approval_required",
                    conversation_id=scope.conversation_id,
                    spoken_reply="The reply needs team approval. It will appear in the chat once approved.",
                )
            if status == "error":
                return VoiceAgentOutcome(
                    status="error",
                    conversation_id=scope.conversation_id,
                    spoken_reply="I couldn't finish that request. Please try again.",
                )
            if status != "ok" or result.get("sent") is False:
                return VoiceAgentOutcome(
                    status="no_reply",
                    conversation_id=scope.conversation_id,
                    spoken_reply="Your message was sent. The team will follow up in the chat.",
                )
            return VoiceAgentOutcome(
                status="ok",
                conversation_id=scope.conversation_id,
                spoken_reply=speech_text(str(result.get("reply") or ""))[:12000],
            )
        from apps.api.routers.chat import chat_message

        user = await get_current_user(request, credentials=await security(request), db=db)
        request_state = getattr(request, "state", None)
        previous_origin = getattr(request_state, "voice_origin_message_id", None)
        if request_state is not None:
            request_state.voice_origin_message_id = origin_message_id
        try:
            result = await chat_message(
                request=request,
                message=text,
                conversation_id=scope.conversation_id,
                agent_id=scope.agent_id,
                workspace_id=scope.workspace_id,
                workspace_context=bool(scope.workspace_id),
                thread_ref_kind=start.thread_ref_kind,
                thread_ref_id=start.thread_ref_id,
                document_ids=None,
                manual_skill_ids=None,
                manual_skill_refs=None,
                chat_mode=None,
                chat_mode_payload=None,
                blocked_tools=None,
                editor_context=None,
                files=[],
                user=user,
                db=db,
            )
        finally:
            if request_state is not None:
                request_state.voice_origin_message_id = previous_origin
        return VoiceAgentOutcome(
            status=(
                "cancelled"
                if result.stop_reason in {"cancelled", "canceled"}
                else "error"
                if result.error
                else "ok"
            ),
            conversation_id=result.conversation_id,
            spoken_reply=(
                ""
                if result.stop_reason in {"cancelled", "canceled"}
                else speech_text(result.content)[:12000]
            ),
            agent_id=scope.agent_id,
        )


async def admit_call_work(
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
    text: str,
) -> VoiceWorkReceipt:
    """Durably admit a voice instruction before saying it is queued."""

    async with async_session() as db:
        if await resolve_call_scope(db, request, start, create=False) != scope:
            raise HTTPException(409, "Chat configuration changed; start a new call")
        return await admit_voice_work(
            db,
            conversation_id=scope.conversation_id,
            text=text,
            user_id=scope.user_id,
            public_channel=bool(start.public_token),
        )


async def recover_call_work(
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
) -> list[VoiceWorkReceipt]:
    async with async_session() as db:
        if await resolve_call_scope(db, request, start, create=False) != scope:
            raise HTTPException(409, "Chat configuration changed; start a new call")
        return await recover_voice_work(
            db,
            conversation_id=scope.conversation_id,
        )


async def route_call_followup(
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
    text: str,
    active_receipt: VoiceWorkReceipt,
    requested_action: VoiceWorkAction | None = None,
) -> VoiceWorkDecision:
    """Compare a new utterance with local durable work state."""

    async with async_session() as db:
        if await resolve_call_scope(db, request, start, create=False) != scope:
            raise HTTPException(409, "Chat configuration changed; start a new call")
        context = await voice_work_context(
            db,
            active_receipt,
            conversation_id=scope.conversation_id,
        )
    return classify_voice_work_followup(
        text,
        context,
        requested_action=requested_action,
    )


async def cancel_call_work(
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
    active_receipt: VoiceWorkReceipt,
    superseded_by: VoiceWorkReceipt | None = None,
) -> bool:
    """Publish cooperative Chat cancellation and terminalize the old receipt."""

    async with async_session() as db:
        if await resolve_call_scope(db, request, start, create=False) != scope:
            raise HTTPException(409, "Chat configuration changed; start a new call")
        return await interrupt_voice_work(
            db,
            active_receipt,
            conversation_id=scope.conversation_id,
            reason=(
                "Replaced by a newer voice instruction."
                if superseded_by
                else "Cancelled by an explicit voice instruction."
            ),
            superseded_by=superseded_by.id if superseded_by else None,
        )


async def execute_call_work(
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
    receipt: VoiceWorkReceipt,
) -> VoiceAgentOutcome:
    """Claim and execute one receipt through the authoritative Chat runtime."""

    async with async_session() as db:
        if await resolve_call_scope(db, request, start, create=False) != scope:
            raise HTTPException(409, "Chat configuration changed; start a new call")
        await claim_voice_work(
            db,
            receipt,
            conversation_id=scope.conversation_id,
        )
    try:
        outcome = await call_agent(
            request,
            start,
            scope,
            receipt.text,
            origin_message_id=receipt.message_id,
        )
    except asyncio.CancelledError:
        async with async_session() as db:
            await finish_voice_work(
                db,
                receipt,
                conversation_id=scope.conversation_id,
                state="interrupted",
                error="Voice execution exceeded its settlement deadline.",
            )
        raise
    except BaseException as exc:
        async with async_session() as db:
            await finish_voice_work(
                db,
                receipt,
                conversation_id=scope.conversation_id,
                state="failed",
                error=str(exc),
            )
        raise
    async with async_session() as db:
        await finish_voice_work(
            db,
            receipt,
            conversation_id=scope.conversation_id,
            state=(
                "interrupted"
                if outcome.status == "cancelled"
                else "failed"
                if outcome.status == "error"
                else "completed"
            ),
            error=outcome.spoken_reply if outcome.status == "error" else None,
        )
    return outcome


async def record_call_control_turn(
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
    user_text: str,
    assistant_text: str,
) -> None:
    """Save foreground progress exchanges without invoking the Chat Agent."""

    if not scope.conversation_id:
        raise RuntimeError("Voice control turn requires a conversation")
    if start.public_token:
        await enforce_public_audio_budget(
            request,
            start.public_token,
            start.session_id or "",
            "turn",
        )
    async with async_session() as db:
        if await resolve_call_scope(db, request, start, create=False) != scope:
            raise HTTPException(409, "Chat configuration changed; start a new call")
        if start.public_token:
            from packages.core.models.task import Conversation
            from packages.core.services.channel_conversations import (
                add_channel_assistant_message,
                add_channel_inbound_message,
            )

            conversation = await db.get(Conversation, scope.conversation_id)
            if conversation is None:
                raise HTTPException(404, "Voice conversation not found")
            conversation_meta = dict(conversation.meta or {})
            channel_type = str(conversation.channel or "webchat")
            sender_id = str(
                conversation_meta.get("sender_id")
                or conversation_meta.get("session_id")
                or start.session_id
                or "visitor"
            )
            chat_id = (
                conversation_meta.get("chat_id")
                or conversation_meta.get("session_id")
                or start.session_id
            )
            if user_text:
                await add_channel_inbound_message(
                    db,
                    conversation_id=scope.conversation_id,
                    channel_type=channel_type,
                    sender_id=sender_id,
                    sender_name=conversation_meta.get("sender_name"),
                    chat_id=chat_id,
                    content=user_text,
                    meta={"voice_control": True},
                )
            if assistant_text:
                await add_channel_assistant_message(
                    db,
                    conversation_id=scope.conversation_id,
                    channel_type=channel_type,
                    chat_id=chat_id,
                    content=assistant_text,
                    runtime_meta={"voice_control": True},
                )
        else:
            from packages.core.services.conversation_messages import add_message

            if user_text:
                await add_message(
                    db,
                    scope.conversation_id,
                    role="user",
                    content=user_text,
                    meta={"voice_control": True, "author_user_id": scope.user_id},
                )
            if assistant_text:
                await add_message(
                    db,
                    scope.conversation_id,
                    role="assistant",
                    content=assistant_text,
                    meta={"voice_control": True},
                )
        await db.commit()


async def prepare_gateway_call(scope: ChatAudioScope):
    from packages.core.ai.runtime import runtime_assert_credit_available
    from packages.core.services.model_gateway import resolve_official_model_route
    from packages.core.services.model_provider_handlers import vercel_catalog_model_type
    from packages.core.services.model_resolver import resolve_llm_metadata_for_user, resolve_model_for_user

    managed = False
    models: dict[str, str] = {}
    for role, label in (("stt", "Speech-to-text"), ("voice", "Text-to-speech")):
        metadata = await resolve_llm_metadata_for_user(role, entity_id=scope.entity_id, user_id=scope.user_id)
        if (metadata or {}).get("llm_api_key"):
            models[role] = await resolve_model_for_user(
                role,
                entity_id=scope.entity_id,
                user_id=scope.user_id,
            )
            continue
        managed = True
        route = None
        if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud":
            model = await resolve_model_for_user(
                role,
                entity_id=scope.entity_id,
                user_id=scope.user_id,
            )
            models[role] = model
            route = await resolve_official_model_route(
                model,
                reason=f"chat.voice.{role}.gateway_key",
                provider_chain=("vercel", "openrouter") if vercel_catalog_model_type(role, model) else ("openrouter",),
            )
        if not route:
            raise HTTPException(
                503,
                f"{label} is not configured. Configure Vercel AI Gateway / OpenRouter, or save an official provider BYOK for {label} in Account → Models.",
            )
    if managed:
        await runtime_assert_credit_available(scope.entity_id, source="chat_voice")
    return models


async def transcribe_call_audio(
    scope: ChatAudioScope,
    audio: bytes,
    *,
    language: str | None = None,
) -> str:
    async with async_session() as db:
        upload = UploadFile(
            file=io.BytesIO(audio), filename="voice.wav", headers=Headers({"content-type": "audio/wav"})
        )
        try:
            result = await transcribe_chat_upload(
                db,
                scope,
                upload,
                language=language,
            )
            return result["text"]
        finally:
            await upload.close()


async def speak_call_reply(
    scope: ChatAudioScope,
    text: str,
    voice_profile: VoiceProfile = DEFAULT_VOICE_PROFILE,
) -> bytes:
    async with async_session() as db:
        response = await chat_speech_response(
            db,
            scope,
            text,
            voice_profile=voice_profile,
            language=speech_language(text),
            voice_instructions=(
                "Use a warm, relaxed, spontaneous phone-conversation delivery. "
                "Use natural intonation and a comfortable pace. Avoid an announcer, "
                "formal narration, or robotic delivery."
            ),
        )
        return bytes(response.body)


async def run_voice_session(
    ws: WebSocket,
    request: Request,
    start: CallStart,
    scope: ChatAudioScope,
    *,
    on_ready: Callable[[], None] | None = None,
    transcription_language: str | None = None,
):
    route = await resolve_realtime_route(
        scope.entity_id,
        user_id=scope.user_id,
        required=False,
        allow_managed=os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud",
        respect_audio_role_overrides=True,
    )

    async def check_access():
        async with async_session() as db:
            current = await resolve_call_scope(db, request, start, create=False)
            if current != scope:
                raise HTTPException(409, "Chat configuration changed; start a new call")

    if route:
        call = None
        try:
            async with asyncio.timeout(REALTIME_SETUP_TIMEOUT_SECONDS) as setup:

                def realtime_ready():
                    setup.reschedule(None)
                    if on_ready:
                        on_ready()

                call = BrowserVoiceSession(
                    ws,
                    route=route,
                    **scope.usage_fields(),
                    check_access=check_access,
                    agent=lambda text: call_agent(request, start, scope, text),
                    admit_work=lambda text: admit_call_work(
                        request,
                        start,
                        scope,
                        text,
                    ),
                    execute_work=lambda receipt: execute_call_work(
                        request,
                        start,
                        scope,
                        receipt,
                    ),
                    recover_work=lambda: recover_call_work(
                        request,
                        start,
                        scope,
                    ),
                    route_followup=lambda text, receipt: route_call_followup(
                        request,
                        start,
                        scope,
                        text,
                        receipt,
                    ),
                    route_work_action=lambda text, receipt, action: route_call_followup(
                        request,
                        start,
                        scope,
                        text,
                        receipt,
                        action,
                    ),
                    cancel_work=lambda receipt, superseded_by=None: cancel_call_work(
                        request,
                        start,
                        scope,
                        receipt,
                        superseded_by,
                    ),
                    record_control_turn=lambda user_text, assistant_text: record_call_control_turn(
                        request,
                        start,
                        scope,
                        user_text,
                        assistant_text,
                    ),
                    connection_factory=open_realtime_connection,
                    on_ready=realtime_ready,
                    voice=start.voice,
                    transcription_language=transcription_language,
                )
                await call.run()
            return
        except (RuntimeError, httpx.HTTPError, WebSocketException, OSError):
            # Timeouts and connection failures may happen during setup. Only
            # fail over before ready; never replay an established call.
            if call is not None and call.ready:
                raise
            logger.warning("Realtime setup failed; trying the shared speech gateway")
    # The shared STT/Chat/TTS gateway has no persistent Realtime model. Keep
    # its existing explicit `turn_based` transport mode instead of presenting
    # it as a Live Agent. Managed Vercel/OpenAI and native OpenAI BYOK routes
    # use BrowserVoiceSession above.
    await GatewayVoiceSession(
        ws,
        conversation_id=scope.conversation_id,
        check_access=check_access,
        prepare=lambda: prepare_gateway_call(scope),
        transcribe=lambda audio: transcribe_call_audio(
            scope,
            audio,
            language=transcription_language,
        ),
        speak=lambda text, voice: speak_call_reply(scope, text, voice),
        agent=lambda text: call_agent(request, start, scope, text),
        admit_work=lambda text: admit_call_work(request, start, scope, text),
        execute_work=lambda receipt: execute_call_work(
            request,
            start,
            scope,
            receipt,
        ),
        recover_work=lambda: recover_call_work(request, start, scope),
        route_followup=lambda text, receipt: route_call_followup(
            request,
            start,
            scope,
            text,
            receipt,
        ),
        cancel_work=lambda receipt, superseded_by=None: cancel_call_work(
            request,
            start,
            scope,
            receipt,
            superseded_by,
        ),
        record_control_turn=lambda user_text, assistant_text: record_call_control_turn(
            request,
            start,
            scope,
            user_text,
            assistant_text,
        ),
        on_ready=on_ready,
        voice=start.voice,
    ).run()


async def send_call_error(ws: WebSocket, message: str, status: int | None = None):
    try:
        await ws.send_json({"type": "error", "message": message, **({"status": status} if status else {})})
    except (RuntimeError, WebSocketDisconnect):
        # The browser may already be closed while accepted audio settles.
        pass


async def remove_empty_created_call_conversation(
    conversation_id: str,
    scope: ChatAudioScope,
) -> None:
    """Remove a plain-Chat conversation when its call never became ready."""

    from sqlalchemy import select

    from packages.core.models.task import Conversation, Message

    async with async_session() as db:
        conversation = (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.entity_id == scope.entity_id,
                    Conversation.user_id == scope.user_id,
                    Conversation.workspace_id.is_(None),
                    Conversation.title == "Voice conversation",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversation is None:
            return
        has_message = (
            await db.execute(
                select(Message.id).where(Message.conversation_id == conversation_id).limit(1)
            )
        ).scalar_one_or_none()
        if has_message is not None:
            return
        await db.delete(conversation)
        await db.commit()


@router.websocket("/live")
async def live_voice(ws: WebSocket):
    await ws.accept()
    lease = None
    created_conversation_id = None
    call_ready = False
    scope = None
    try:
        async with asyncio.timeout(CALL_SETUP_TIMEOUT_SECONDS) as setup:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            if len(raw) > 16384:
                raise ValueError("Call request too large")
            start = CallStart.model_validate_json(raw)
            creates_plain_conversation = bool(
                not start.public_token
                and not start.conversation_id
                and not start.workspace_id
                and not start.thread_ref_kind
                and not start.thread_ref_id
            )
            request = call_request(ws, start)
            if start.public_token:
                await enforce_public_audio_budget(
                    request,
                    start.public_token,
                    start.session_id or "",
                    "call",
                )
            async with async_session() as db:
                scope = await resolve_call_scope(db, request, start, create=True)
                transcription_language = await call_transcription_language(db, scope)
            if creates_plain_conversation:
                created_conversation_id = scope.conversation_id
            lease = await acquire_audio_lease()

            def session_ready():
                nonlocal call_ready
                call_ready = True
                setup.reschedule(None)

            await lease.run(
                run_voice_session(
                    ws,
                    request,
                    start,
                    scope,
                    on_ready=session_ready,
                    transcription_language=transcription_language,
                )
            )
    except WebSocketDisconnect:
        pass
    except ChatConcurrencyExceeded as exc:
        await send_call_error(ws, exc.message, exc.status_code)
    except TimeoutError:
        await send_call_error(ws, "Voice connection timed out. Please try again.", 504)
    except HTTPException as exc:
        await send_call_error(
            ws,
            exc.detail if isinstance(exc.detail, str) else "Voice call is unavailable",
            exc.status_code,
        )
    except (ValidationError, ValueError):
        await send_call_error(ws, "Invalid voice call request")
    except Exception:
        logger.exception("Browser voice call ended unexpectedly")
        await send_call_error(ws, "Voice call disconnected. Please try again.")
    finally:
        if lease:
            await lease.release()
        if created_conversation_id and scope is not None and not call_ready:
            try:
                await remove_empty_created_call_conversation(created_conversation_id, scope)
            except Exception:
                logger.exception("Unable to remove empty failed voice conversation")
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
