"""Public landing-page waiting-list submissions."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


# Lifecycle a waiting-list entry can move through. The public submit
# endpoint only ever creates rows with status="new" — every other
# transition happens through admin endpoints (PATCH or issue-invite).
WAITLIST_STATUS_NEW = "new"
WAITLIST_STATUS_CONTACTED = "contacted"
WAITLIST_STATUS_INVITED = "invited"
WAITLIST_STATUS_REJECTED = "rejected"
WAITLIST_STATUS_ARCHIVED = "archived"

WAITLIST_STATUSES = (
    WAITLIST_STATUS_NEW,
    WAITLIST_STATUS_CONTACTED,
    WAITLIST_STATUS_INVITED,
    WAITLIST_STATUS_REJECTED,
    WAITLIST_STATUS_ARCHIVED,
)


class WaitingListEntry(Base, TimestampMixin):
    """A contact request submitted from the public landing page."""

    __tablename__ = "waiting_list_entries"
    __table_args__ = (
        Index("ix_waiting_list_entries_created_at", "created_at"),
        Index("ix_waiting_list_entries_email", "email"),
        Index("ix_waiting_list_entries_interested", "interested"),
        Index("ix_waiting_list_entries_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[Optional[str]] = mapped_column(String(255))
    interested: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        String(80), nullable=False, default="landing", server_default="landing"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WAITLIST_STATUS_NEW, server_default=WAITLIST_STATUS_NEW
    )
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)

    # Free-text admin notes (call log, follow-up, "spam from competitor", etc.)
    internal_note: Mapped[Optional[str]] = mapped_column(Text)

    # Set when an admin issues an invitation code via the issue-invite
    # endpoint. ``invited_code`` references ``invitation_codes.code`` —
    # no FK so an admin deleting a code doesn't strand the audit trail.
    invited_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    invited_code: Mapped[Optional[str]] = mapped_column(String(64))
