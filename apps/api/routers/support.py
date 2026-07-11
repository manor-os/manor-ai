"""User-facing support API.

Lets an authenticated tenant user open a conversation with the Manor
platform team, list their tickets, see the full thread, post a reply,
and close their own ticket.

The admin side (cross-tenant inbox, reply, assign, resolve) is part of the
private operator surface.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.support_ticket import (
    SENDER_KIND_ADMIN,
    SENDER_KIND_USER,
    SUPPORT_ACTIVE_STATUSES,
    SUPPORT_STATUS_AWAITING_USER,
    SUPPORT_STATUS_CLOSED,
    SUPPORT_STATUS_OPEN,
    SupportMessage,
    SupportTicket,
)
from packages.core.models.user import User
from packages.core.services.support_notifications import (
    notify_ops_of_new_user_message,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/support", tags=["support"])

MAX_SUBJECT_LEN = 200
MAX_BODY_LEN = 8000


class SupportTicketSummary(BaseModel):
    id: str
    subject: str
    status: str
    priority: str
    created_at: str
    last_message_at: Optional[str]
    last_admin_message_at: Optional[str]
    unread_user_count: int


class SupportMessageOut(BaseModel):
    id: str
    sender_kind: str
    sender_display_name: Optional[str]
    body: str
    created_at: str


class SupportTicketDetail(SupportTicketSummary):
    messages: list[SupportMessageOut]


class CreateTicketRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=MAX_SUBJECT_LEN)
    body: str = Field(..., min_length=1, max_length=MAX_BODY_LEN)


class ReplyRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=MAX_BODY_LEN)


class UnreadCountResponse(BaseModel):
    count: int


def _summary(t: SupportTicket) -> SupportTicketSummary:
    return SupportTicketSummary(
        id=t.id, subject=t.subject, status=t.status, priority=t.priority,
        created_at=t.created_at.isoformat() if t.created_at else "",
        last_message_at=t.last_message_at.isoformat() if t.last_message_at else None,
        last_admin_message_at=(
            t.last_admin_message_at.isoformat() if t.last_admin_message_at else None
        ),
        unread_user_count=t.unread_user_count or 0,
    )


def _message_out(m: SupportMessage) -> SupportMessageOut:
    return SupportMessageOut(
        id=m.id, sender_kind=m.sender_kind,
        sender_display_name=m.sender_display_name, body=m.body,
        created_at=m.created_at.isoformat() if m.created_at else "",
    )


@router.get("/tickets", response_model=list[SupportTicketSummary])
async def list_my_tickets(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = Query(None, description="open|awaiting_user|resolved|closed"),
):
    """Return the user's own tickets, newest activity first.

    Filter optional — passing ``status=open`` etc. narrows the list;
    omitting it returns everything the user has ever opened so they
    can find old resolved threads."""
    stmt = (
        select(SupportTicket)
        .where(SupportTicket.user_id == user.id)
        .order_by(
            desc(func.coalesce(SupportTicket.last_message_at, SupportTicket.created_at))
        )
    )
    if status:
        stmt = stmt.where(SupportTicket.status == status)
    rows = list((await db.execute(stmt)).scalars().all())
    return [_summary(r) for r in rows]


@router.get("/unread-count", response_model=UnreadCountResponse)
async def my_unread_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sum of unread admin messages across all the user's tickets —
    drives the sidebar Support badge."""
    total = (await db.execute(
        select(func.coalesce(func.sum(SupportTicket.unread_user_count), 0)).where(
            SupportTicket.user_id == user.id,
        )
    )).scalar_one()
    return UnreadCountResponse(count=int(total or 0))


@router.post(
    "/tickets", response_model=SupportTicketDetail, status_code=201,
)
async def create_ticket(
    req: CreateTicketRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Open a new ticket with its first message in one shot."""
    subject = req.subject.strip()
    body = req.body.strip()
    if not subject or not body:
        raise HTTPException(400, "subject and body are required")

    now = datetime.now(timezone.utc)
    ticket = SupportTicket(
        id=generate_ulid(),
        entity_id=user.entity_id,
        user_id=user.id,
        user_email=user.email,
        user_display_name=user.display_name,
        subject=subject,
        status=SUPPORT_STATUS_OPEN,
        last_message_at=now,
        last_user_message_at=now,
        unread_admin_count=1,
    )
    msg = SupportMessage(
        id=generate_ulid(),
        ticket_id=ticket.id,
        sender_kind=SENDER_KIND_USER,
        sender_user_id=user.id,
        sender_display_name=user.display_name or user.email,
        body=body,
    )
    db.add(ticket)
    db.add(msg)
    await db.commit()

    background.add_task(
        notify_ops_of_new_user_message,
        ticket_id=ticket.id, subject=subject,
        user_email=user.email,
        user_display_name=user.display_name,
        body=body, is_new_ticket=True,
    )

    return SupportTicketDetail(
        **_summary(ticket).model_dump(),
        messages=[_message_out(msg)],
    )


@router.get("/tickets/{ticket_id}", response_model=SupportTicketDetail)
async def get_ticket(
    ticket_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full thread. Side effect: clears the unread badge for this
    user since they're now looking at the conversation."""
    ticket = (await db.execute(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    messages = list((await db.execute(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket_id)
        .order_by(SupportMessage.created_at.asc())
    )).scalars().all())

    if ticket.unread_user_count:
        ticket.unread_user_count = 0
        await db.commit()

    return SupportTicketDetail(
        **_summary(ticket).model_dump(),
        messages=[_message_out(m) for m in messages],
    )


@router.post(
    "/tickets/{ticket_id}/messages",
    response_model=SupportMessageOut, status_code=201,
)
async def reply_to_ticket(
    ticket_id: str,
    req: ReplyRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Post a user reply. Re-opens a resolved/closed ticket so the
    user can follow up without filing a new one."""
    body = req.body.strip()
    if not body:
        raise HTTPException(400, "body is required")

    ticket = (await db.execute(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    now = datetime.now(timezone.utc)
    msg = SupportMessage(
        id=generate_ulid(),
        ticket_id=ticket.id,
        sender_kind=SENDER_KIND_USER,
        sender_user_id=user.id,
        sender_display_name=user.display_name or user.email,
        body=body,
    )
    db.add(msg)

    ticket.last_message_at = now
    ticket.last_user_message_at = now
    ticket.unread_admin_count = (ticket.unread_admin_count or 0) + 1
    # Re-open if the ticket was previously closed/resolved — a follow-up
    # message means the user still needs help.
    if ticket.status in (SUPPORT_STATUS_AWAITING_USER, "resolved", SUPPORT_STATUS_CLOSED):
        ticket.status = SUPPORT_STATUS_OPEN
        ticket.resolved_at = None
        ticket.resolved_by_admin_id = None
    await db.commit()

    background.add_task(
        notify_ops_of_new_user_message,
        ticket_id=ticket.id, subject=ticket.subject,
        user_email=ticket.user_email,
        user_display_name=ticket.user_display_name,
        body=body, is_new_ticket=False,
    )

    return _message_out(msg)


@router.post("/tickets/{ticket_id}/close", response_model=SupportTicketSummary)
async def close_my_ticket(
    ticket_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """User closes their own ticket. Admins use their own endpoint to
    mark resolved (semantically different — the user might still reply)."""
    ticket = (await db.execute(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.status == SUPPORT_STATUS_CLOSED:
        return _summary(ticket)
    ticket.status = SUPPORT_STATUS_CLOSED
    await db.commit()
    return _summary(ticket)
