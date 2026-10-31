"""Report objectionable Workspace chat messages to platform support.

Apple App Store guideline 1.2 requires a way to report user-generated content.
A report is delivered by email to the support inbox, which is the record for
v1; nothing is stored in a new table. A report is acknowledged only after the
email is accepted, so the app can ask the person to try again.
"""
from __future__ import annotations

import html
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from packages.core.database import get_db
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.email_service import (
    is_email_delivery_enabled,
    send_email,
    support_inbox_address,
)
from packages.core.services.workspace_access import user_can_read_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/content-reports", tags=["content-reports"])

EXCERPT_MAX_CHARS = 500
DELIVERY_UNAVAILABLE_CODE = "content_report.delivery_unavailable"


class ContentReportRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    reason: Literal["offensive", "spam", "other"]
    note: str | None = Field(default=None, max_length=1000)


class ContentReportReceipt(BaseModel):
    received: bool = True


def _delivery_unavailable() -> CodedError:
    return CodedError(
        503,
        code=DELIVERY_UNAVAILABLE_CODE,
        message="We couldn't send your report right now. Please try again.",
    )


def _author_user_id(message: Message) -> str | None:
    meta = message.meta if isinstance(message.meta, dict) else {}
    value = meta.get("author_user_id")
    return str(value) if value else None


@router.post("", status_code=201, response_model=ContentReportReceipt)
async def submit_content_report(
    req: ContentReportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContentReportReceipt:
    row = (await db.execute(
        select(Message, Conversation)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == req.message_id,
            Conversation.entity_id == user.entity_id,
            Conversation.workspace_id.is_not(None),
        )
    )).one_or_none()
    if row is None:
        raise HTTPException(404, "message not found")
    message, conversation = row
    # Same visibility rule as the Workspace chat read route: the caller must be
    # able to read the workspace, and a missing or foreign one looks the same.
    workspace = (await db.execute(select(Workspace).where(
        Workspace.id == conversation.workspace_id,
        Workspace.entity_id == user.entity_id,
        Workspace.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if workspace is None or not await user_can_read_workspace(db, workspace=workspace, user=user):
        raise HTTPException(404, "message not found")

    author_user_id = _author_user_id(message) or "unknown"
    excerpt = (message.content or "")[:EXCERPT_MAX_CHARS]
    lines = [
        "A Workspace chat message was reported.",
        f"Reporter user id: {user.id}",
        f"Reporter email: {user.email}",
        f"Entity id: {conversation.entity_id}",
        f"Workspace id: {conversation.workspace_id}",
        f"Message id: {message.id}",
        f"Author user id: {author_user_id}",
        f"Reason: {req.reason}",
        f"Note: {req.note or '(none)'}",
        "Message excerpt:",
        excerpt,
    ]
    text_body = "\n".join(lines)
    html_body = f'<pre style="white-space:pre-wrap;font-family:monospace">{html.escape(text_body)}</pre>'

    if not is_email_delivery_enabled():
        logger.warning("content_report_undelivered reason=email_disabled message_id=%s", message.id)
        raise _delivery_unavailable()
    try:
        delivered = await send_email(
            support_inbox_address(),
            f"Manor content report: {req.reason}",
            html_body,
            text_body,
        )
    except Exception:
        logger.exception("content_report_undelivered reason=provider_error message_id=%s", message.id)
        delivered = False
    if not delivered:
        logger.warning("content_report_undelivered reason=send_failed message_id=%s", message.id)
        raise _delivery_unavailable()

    logger.info(
        "content_report_received reporter_user_id=%s entity_id=%s workspace_id=%s "
        "message_id=%s author_user_id=%s reason=%s",
        user.id, conversation.entity_id, conversation.workspace_id,
        message.id, author_user_id, req.reason,
    )
    return ContentReportReceipt(received=True)
