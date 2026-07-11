"""Audio endpoints — transcription via OpenAI Whisper.

POST /api/v1/audio/transcribe
  multipart/form-data: file=<audio blob>
  Returns: { text, duration_seconds, model }

Thin route layer over ``packages.core.services.voice.whisper`` —
the actual HTTP+billing logic lives there so chat-attachment audio
uploads (in ``file_context``) share the same code path.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.ai.runtime import (
    RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
    RUNTIME_FLOATING_CHAT_VOICE_SOURCE,
    runtime_assert_credit_available,
)
from packages.core.models.user import User
from packages.core.services.voice.whisper import (
    WhisperError,
    transcribe_blob,
    whisper_cost_usd,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audio", tags=["audio"])


# Browser sometimes labels webm-with-audio as ``video/webm``; accept both.
_ALLOWED_MIME_PREFIXES = ("audio/", "video/")


class TranscribeResponse(BaseModel):
    text: str
    duration_seconds: float
    model: str


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Transcribe an uploaded audio clip via Whisper. Returns the text +
    duration. Bills the call to the user's entity.
    """
    if not file.content_type or not any(
        file.content_type.startswith(p) for p in _ALLOWED_MIME_PREFIXES
    ):
        raise HTTPException(400, f"Unsupported audio type: {file.content_type!r}")

    blob = await file.read()

    # Resolve BYOK key + STT model from tenant settings.
    user_key = None
    stt_model = None
    try:
        from packages.core.services.model_resolver import (
            resolve_llm_metadata_for_user,
            resolve_model_for_user,
        )
        stt_model = await resolve_model_for_user("stt", user_id=user.id, entity_id=user.entity_id, db=db)
        metadata = await resolve_llm_metadata_for_user(
            "stt",
            user_id=user.id,
            entity_id=user.entity_id,
            db=db,
        )
        user_key = (metadata or {}).get("llm_api_key")
    except Exception:
        pass

    if not user_key:
        await runtime_assert_credit_available(
            user.entity_id,
            source=RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
        )

    try:
        result = await transcribe_blob(
            blob,
            mime=file.content_type,
            filename=file.filename or "audio.webm",
            language=language,
            user_api_key=user_key,
            resolved_model=stt_model,
        )
    except WhisperError as exc:
        # Map known errors to clean HTTP statuses. Empty/oversize → 4xx.
        msg = str(exc)
        if "Empty audio" in msg:
            raise HTTPException(400, msg)
        if "too large" in msg.lower():
            raise HTTPException(413, msg)
        if "unavailable" in msg.lower():
            raise HTTPException(503, msg)
        if "rejected the audio" in msg.lower():
            raise HTTPException(400, msg)
        raise HTTPException(502, msg)

    # Bill the call. Best-effort — log and continue if it fails so the
    # user still gets their transcript even when billing's misconfigured.
    try:
        from packages.core.services.usage_service import record_media_usage
        await record_media_usage(
            db,
            entity_id=user.entity_id,
            kind="whisper",
            model=result.model,
            cost_usd=whisper_cost_usd(result.duration_seconds),
            units=int(result.duration_seconds),
            user_id=user.id,
            source=RUNTIME_FLOATING_CHAT_VOICE_SOURCE,
            byok=bool(user_key),
        )
        await db.commit()
    except Exception:
        logger.debug("whisper billing failed (best-effort)", exc_info=True)

    return TranscribeResponse(
        text=result.text,
        duration_seconds=round(result.duration_seconds, 3),
        model=result.model,
    )
