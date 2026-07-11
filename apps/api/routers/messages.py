"""Entity-scoped direct messages for the Messages page."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User


router = APIRouter(prefix="/api/v1/messages", tags=["messages"])


class MessageThreadResponse(BaseModel):
    id: str
    participant_id: str
    participant_name: str
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread: bool = False


class ThreadMessageResponse(BaseModel):
    id: str
    sender_id: str
    sender_name: str
    content: str
    created_at: datetime
    is_own: bool


class SendMessageRequest(BaseModel):
    recipient_id: str
    content: str
    thread_id: Optional[str] = None


def _display_name(user: User | None, fallback: str = "Unknown") -> str:
    if user is None:
        return fallback
    full_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip()
    return user.display_name or full_name or user.email


def _participant_pair(user_id: str, recipient_id: str) -> tuple[str, str]:
    return tuple(sorted([user_id, recipient_id]))  # type: ignore[return-value]


def _direct_message_filter(user: User):
    return (
        Conversation.entity_id == user.entity_id,
        Conversation.scope == "channel",
        Conversation.channel == "direct",
        Conversation.meta["kind"].astext == "direct_message",
        or_(
            Conversation.meta["participant_a"].astext == user.id,
            Conversation.meta["participant_b"].astext == user.id,
        ),
    )


async def _get_entity_user(db: AsyncSession, entity_id: str, user_id: str) -> User:
    recipient = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.entity_id == entity_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if recipient is None:
        raise HTTPException(404, "Recipient not found")
    return recipient


async def _get_direct_conversation(
    db: AsyncSession, user: User, conversation_id: str,
) -> Conversation:
    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                *_direct_message_filter(user),
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(404, "Thread not found")
    return conversation


async def _find_direct_conversation(
    db: AsyncSession, user: User, recipient_id: str,
) -> Conversation | None:
    a, b = _participant_pair(user.id, recipient_id)
    return (
        await db.execute(
            select(Conversation).where(
                Conversation.entity_id == user.entity_id,
                Conversation.scope == "channel",
                Conversation.channel == "direct",
                Conversation.meta["kind"].astext == "direct_message",
                Conversation.meta["participant_a"].astext == a,
                Conversation.meta["participant_b"].astext == b,
            )
        )
    ).scalar_one_or_none()


async def _participant_map(db: AsyncSession, entity_id: str, ids: set[str]) -> dict[str, User]:
    if not ids:
        return {}
    users = (
        await db.execute(
            select(User).where(
                User.entity_id == entity_id,
                User.id.in_(ids),
                User.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return {u.id: u for u in users}


@router.get("/threads", response_model=list[MessageThreadResponse])
async def list_threads(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conversations = (
        await db.execute(
            select(Conversation)
            .where(*_direct_message_filter(user))
            .order_by(desc(Conversation.updated_at))
        )
    ).scalars().all()

    participant_ids: set[str] = set()
    for conv in conversations:
        participant_id = (
            conv.meta.get("participant_b")
            if conv.meta.get("participant_a") == user.id
            else conv.meta.get("participant_a")
        )
        if participant_id:
            participant_ids.add(str(participant_id))
    participants = await _participant_map(db, user.entity_id, participant_ids)

    out: list[MessageThreadResponse] = []
    for conv in conversations:
        participant_id = (
            conv.meta.get("participant_b")
            if conv.meta.get("participant_a") == user.id
            else conv.meta.get("participant_a")
        )
        if not participant_id:
            continue
        latest = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        out.append(
            MessageThreadResponse(
                id=conv.id,
                participant_id=str(participant_id),
                participant_name=_display_name(participants.get(str(participant_id))),
                last_message=latest.content if latest else None,
                last_message_at=latest.created_at if latest else conv.updated_at,
                unread=False,
            )
        )
    return out


@router.get("/threads/{thread_id}", response_model=list[ThreadMessageResponse])
async def get_thread(
    thread_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = await _get_direct_conversation(db, user, thread_id)
    participant_ids = {
        str(conversation.meta.get("participant_a") or ""),
        str(conversation.meta.get("participant_b") or ""),
    } - {""}
    participants = await _participant_map(db, user.entity_id, participant_ids)
    messages = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc())
        )
    ).scalars().all()

    return [
        ThreadMessageResponse(
            id=msg.id,
            sender_id=str((msg.meta or {}).get("sender_id") or conversation.user_id or ""),
            sender_name=_display_name(
                participants.get(str((msg.meta or {}).get("sender_id") or conversation.user_id or ""))
            ),
            content=msg.content or "",
            created_at=msg.created_at,
            is_own=str((msg.meta or {}).get("sender_id") or conversation.user_id or "") == user.id,
        )
        for msg in messages
    ]


@router.post("", response_model=ThreadMessageResponse, status_code=201)
async def send_message(
    req: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "Message content is required")
    if req.thread_id:
        conversation = await _get_direct_conversation(db, user, req.thread_id)
        recipient_id = (
            conversation.meta.get("participant_b")
            if conversation.meta.get("participant_a") == user.id
            else conversation.meta.get("participant_a")
        )
        recipient = await _get_entity_user(db, user.entity_id, str(recipient_id or ""))
    else:
        if req.recipient_id == user.id:
            raise HTTPException(400, "Choose another user")
        recipient = await _get_entity_user(db, user.entity_id, req.recipient_id)
        conversation = await _find_direct_conversation(db, user, recipient.id)
        if conversation is None:
            a, b = _participant_pair(user.id, recipient.id)
            conversation = Conversation(
                entity_id=user.entity_id,
                user_id=user.id,
                title=_display_name(recipient),
                channel="direct",
                status="active",
                scope="channel",
                meta={
                    "kind": "direct_message",
                    "participant_a": a,
                    "participant_b": b,
                },
            )
            db.add(conversation)
            await db.flush()

    msg = Message(
        conversation_id=conversation.id,
        role="user",
        content=content,
        tool_calls=None,
        attachments=None,
        token_usage=None,
        meta={"sender_id": user.id, "recipient_id": recipient.id},
        author_kind="user",
        author_subscription_id=None,
        message_kind="text",
        refs=None,
        pending_action=None,
        resolution=None,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    return ThreadMessageResponse(
        id=msg.id,
        sender_id=user.id,
        sender_name=_display_name(user),
        content=msg.content or "",
        created_at=msg.created_at,
        is_own=True,
    )


@router.post("/threads/{thread_id}/read", status_code=204)
async def mark_thread_read(
    thread_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_direct_conversation(db, user, thread_id)
    return Response(status_code=204)
