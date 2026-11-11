"""Read the chat records the signed-in person may see, across every conversation."""
from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import chat_history_access as history

router = APIRouter(prefix="/api/v1/chat/history", tags=["chat-history"])


def _reader(user: User) -> history.ChatHistoryReader:
    return history.ChatHistoryReader(user_id=user.id, entity_id=user.entity_id, role=user.role)


async def _answer(call: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await call
    except history.ChatHistoryNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (history.ChatHistoryInvalid, history.ChatHistoryQueryTooBroad) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/conversations")
async def list_conversations(
    kind: str | None = None,
    workspace_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _answer(history.list_conversation_sources(
        db, _reader(user), kind=kind, workspace_id=workspace_id, limit=limit,
    ))


@router.get("/search")
async def search_messages(
    query: str,
    kind: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    sender_user_id: str | None = None,
    after: str | None = None,
    before: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=20),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _answer(history.search_messages(
        db, _reader(user), query=query, kind=kind, conversation_id=conversation_id, workspace_id=workspace_id,
        sender_user_id=sender_user_id, after=after, before=before, cursor=cursor, limit=limit,
    ))


@router.get("/{kind}/{conversation_id}/messages")
async def read_messages(
    kind: str,
    conversation_id: str,
    around_message_id: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _answer(history.read_messages(
        db, _reader(user), kind=kind, conversation_id=conversation_id,
        around_message_id=around_message_id, before=before, after=after, limit=limit,
    ))
