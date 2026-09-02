"""Shared, bounded speech I/O for authenticated chat and public webchat."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
    RUNTIME_FLOATING_CHAT_VOICE_SOURCE,
    runtime_assert_credit_available,
    runtime_generate_audio_media,
)
from packages.core.services.model_resolver import resolve_llm_metadata_for_user, resolve_model_for_user
from packages.core.services.upload_security import UploadSecurityError, inspect_upload_content
from packages.core.services.usage_service import record_media_usage
from packages.core.services.voice.profiles import VoiceProfile
from packages.core.services.voice.whisper import WhisperError, transcribe_blob, whisper_cost_usd

logger = logging.getLogger(__name__)
MAX_AUDIO_BYTES = 10 * 1024 * 1024
SPEECH_CHUNK_LENGTH = 3500

# Public Webchat bills speech to the channel owner. Keep a separate distributed
# budget from the broad API middleware so one anonymous session cannot consume
# the owner's credits or all shared audio capacity.
_PUBLIC_AUDIO_LIMITS = {
    "call": ((4, 60), (20, 3600)),
    "turn": ((12, 60), (120, 3600)),
    "transcribe": ((12, 60), (120, 3600)),
    "speech": ((20, 60), (200, 3600)),
}


async def enforce_public_audio_budget(
    request: Request,
    public_token: str,
    session_id: str,
    operation: str,
) -> None:
    from apps.api.middleware.rate_limit import RateLimiter, client_ip

    limits = _PUBLIC_AUDIO_LIMITS.get(operation)
    if limits is None:
        raise ValueError("Unsupported public audio operation")
    limiters = getattr(enforce_public_audio_budget, "_limiters", None)
    if limiters is None:
        # Keep a local fail-safe even when the shared Redis limiter is
        # unavailable. Redis adds cross-process enforcement in normal service.
        limiters = (RateLimiter(redis_enabled=False), RateLimiter(redis_enabled=True))
        enforce_public_audio_budget._limiters = limiters
    scope = hashlib.sha256(
        f"{public_token}\0{session_id}".encode("utf-8")
    ).hexdigest()[:32]
    subjects = (f"session:{scope}", f"ip:{client_ip(request)}")
    for limiter in limiters:
        for subject in subjects:
            for maximum, window in limits:
                result = await limiter.check(
                    f"public-audio:{operation}:{subject}:{window}",
                    maximum,
                    window,
                )
                if not result.allowed:
                    raise HTTPException(
                        429,
                        "Too many voice requests. Please wait and try again.",
                        headers={"Retry-After": str(result.retry_after or window)},
                    )


async def acquire_audio_lease():
    # Audio endpoints run on the normal API role, not the dedicated SSE role.
    from packages.core.services.chat_concurrency import ChatConcurrencyExceeded, acquire_chat_stream_slot

    try:
        return await acquire_chat_stream_slot(scope="chat-audio")
    except ChatConcurrencyExceeded as exc:
        raise HTTPException(
            exc.status_code, exc.message, headers={"Retry-After": str(exc.retry_after_seconds)}
        ) from exc


@dataclass(frozen=True)
class ChatAudioScope:
    entity_id: str
    user_id: str
    workspace_id: str | None = None
    conversation_id: str | None = None
    agent_id: str | None = None

    def usage_fields(self) -> dict:
        return vars(self)


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=SPEECH_CHUNK_LENGTH)
    voice: str | None = Field(default=None, max_length=80)
    conversation_id: str | None = None
    workspace_id: str | None = None


class TranscribeResponse(BaseModel):
    text: str
    duration_seconds: float
    model: str


async def authenticated_audio_scope(db, user, *, conversation_id=None, workspace_id=None):
    agent_id = None
    if conversation_id:
        from apps.api.routers.chat import _get_accessible_conversation

        conv = await _get_accessible_conversation(db, user, conversation_id)
        if workspace_id and workspace_id != conv.workspace_id:
            raise HTTPException(404, "Conversation not found")
        workspace_id = conv.workspace_id
        agent_id = conv.agent_id
    if workspace_id:
        from apps.api.deps import require_workspace_writable

        await require_workspace_writable(db, user, workspace_id)
    return ChatAudioScope(user.entity_id, user.id, workspace_id, conversation_id, agent_id)


async def transcribe_chat_upload(
    db: AsyncSession,
    scope: ChatAudioScope,
    file: UploadFile,
    language: str | None = None,
) -> dict:
    mime = (file.content_type or "").split(";", 1)[0].lower()
    if not (mime.startswith("audio/") or mime == "video/webm"):
        raise HTTPException(400, "Unsupported audio type")
    blob = await file.read(MAX_AUDIO_BYTES + 1)
    if len(blob) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio is too large; record a shorter clip")
    if not blob:
        raise HTTPException(400, "Empty audio upload")
    try:
        await inspect_upload_content(
            blob,
            filename=file.filename,
            declared_content_type=mime,
            allowed_extensions={".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"},
        )
    except UploadSecurityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    model = await resolve_model_for_user("stt", user_id=scope.user_id, entity_id=scope.entity_id, db=db)
    metadata = await resolve_llm_metadata_for_user("stt", user_id=scope.user_id, entity_id=scope.entity_id, db=db)
    metadata = metadata or {}
    key = metadata.get("llm_api_key")
    if not key:
        await runtime_assert_credit_available(
            scope.entity_id,
            source=RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
        )
    try:
        result = await transcribe_blob(
            blob,
            mime=mime,
            filename=file.filename or "voice.webm",
            language=language,
            user_api_key=key,
            user_base_url=metadata.get("llm_base_url"),
            resolved_model=model,
        )
    except WhisperError as exc:
        logger.warning("Chat transcription failed (%s)", type(exc).__name__)
        if isinstance(exc.__cause__, (httpx.NetworkError, httpx.TimeoutException)):
            raise HTTPException(
                503, "Cannot reach the speech provider. Check the server network and try again."
            ) from exc
        raise HTTPException(502, "Transcription failed. Check the speech-to-text provider settings and retry.") from exc
    await record_media_usage(
        db,
        **scope.usage_fields(),
        kind="whisper",
        model=result.model,
        cost_usd=getattr(result, "cost_usd", None)
        if getattr(result, "cost_usd", None) is not None
        else whisper_cost_usd(result.duration_seconds, result.model),
        units=int(result.duration_seconds),
        source=RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
        byok=bool(key),
    )
    await db.commit()
    return {"text": result.text, "duration_seconds": round(result.duration_seconds, 3), "model": result.model}


def speech_text(text: str) -> str:
    """Read prose rather than Markdown URLs, code blocks, or HTML tags."""
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"(?m)^\s{0,3}[#>]+\s*", "", text)
    return re.sub(r"[*_`~]", "", text).strip()


def speech_language(text: str) -> str | None:
    """Infer scripts whose TTS locale materially changes pronunciation."""

    if re.search(r"[\u3040-\u30ff]", text):
        return "ja-JP"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko-KR"
    if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text):
        return "zh-CN"
    return None


async def chat_speech_response(
    db: AsyncSession,
    scope: ChatAudioScope,
    text: str,
    voice: str | None = None,
    voice_profile: VoiceProfile | None = None,
    voice_instructions: str | None = None,
    language: str | None = None,
) -> Response:
    from packages.core.services.model_pricing_gateway import estimate_audio_cost_usd

    text = speech_text(text)
    if not text:
        raise HTTPException(400, "No readable text in this reply")
    if len(text) > SPEECH_CHUNK_LENGTH:
        raise HTTPException(413, "Speech text is too long")
    audio = b""
    mime = "audio/mpeg"

    async def deliver_playback(**result):
        nonlocal audio, mime
        audio = result["audio_bytes"]
        mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg", "flac": "audio/flac"}[
            result["audio_format"]
        ]
        # Preserve media pricing and BYOK, without a filesystem/Knowledge row.
        await record_media_usage(
            db,
            **scope.usage_fields(),
            kind="tts",
            model=result["model"],
            cost_usd=estimate_audio_cost_usd(result["model"], purpose="speech"),
            units=len(text),
            source=RUNTIME_FLOATING_CHAT_VOICE_SOURCE,
            byok=result["is_byok"],
        )
        await db.commit()
        return ""

    result = json.loads(
        await runtime_generate_audio_media(
            **scope.usage_fields(),
            prompt=text,
            name="",
            params={
                "purpose": "speech",
                "format": "mp3",
                "_deliver_audio": deliver_playback,
                **({"voice": voice} if voice else {}),
                **({"_call_voice_profile": voice_profile} if voice_profile else {}),
                **({"voice_instructions": voice_instructions} if voice_instructions else {}),
                **({"language": language} if language else {}),
            },
        )
    )
    if result.get("status") != "completed" or not audio:
        if result.get("code") == "audio_provider_unavailable":
            raise HTTPException(
                503, "The speech provider is unavailable. Check the server network or try again shortly."
            )
        raise HTTPException(502, "Speech generation failed. Check the text-to-speech provider settings and retry.")
    return Response(audio, media_type=mime, headers={"Cache-Control": "private, no-store"})
