"""Authenticated chat transcription; public chat shares the same service."""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.chat_audio import (
    TranscribeResponse,
    acquire_audio_lease,
    authenticated_audio_scope,
    transcribe_chat_upload,
)
from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.user import User

router = APIRouter(prefix="/api/v1/audio", tags=["audio"])


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    language: str | None = Form(None, max_length=35),
    conversation_id: str | None = Form(None),
    workspace_id: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scope = await authenticated_audio_scope(
        db,
        user,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
    )
    lease = await acquire_audio_lease()
    try:
        return await transcribe_chat_upload(db, scope, file, language)
    finally:
        await lease.release()
