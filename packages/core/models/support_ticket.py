"""Support tickets — two-sided conversations between tenant users and
Manor platform admins.

Distinct from:
  * ``Notification`` / ``Announcement`` — one-way blasts.
  * Tenant-internal channels — for intra-org comms.

A ticket is owned by the user who opened it (``user_id``) and lives at
the tenant level (``entity_id``). Both are nullable so a ticket
outlives PII deletion; ``user_email`` / ``user_display_name`` are
snapshots taken at create time for the admin inbox.

Status lifecycle:

    open ──user replies──▶ open
     │
     ├──admin replies────▶ awaiting_user ──user replies──▶ open
     │                          │
     │                          └──admin resolves────▶ resolved
     │
     └──user closes───────▶ closed
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


# Status enum values — keep in sync with the admin UI filter chips
# and the user-side "active vs closed" partition.
SUPPORT_STATUS_OPEN = "open"
SUPPORT_STATUS_AWAITING_USER = "awaiting_user"
SUPPORT_STATUS_RESOLVED = "resolved"
SUPPORT_STATUS_CLOSED = "closed"
SUPPORT_STATUSES = (
    SUPPORT_STATUS_OPEN,
    SUPPORT_STATUS_AWAITING_USER,
    SUPPORT_STATUS_RESOLVED,
    SUPPORT_STATUS_CLOSED,
)
SUPPORT_ACTIVE_STATUSES = (SUPPORT_STATUS_OPEN, SUPPORT_STATUS_AWAITING_USER)

SUPPORT_PRIORITY_NORMAL = "normal"
SUPPORT_PRIORITY_HIGH = "high"
SUPPORT_PRIORITIES = (SUPPORT_PRIORITY_NORMAL, SUPPORT_PRIORITY_HIGH)

SENDER_KIND_USER = "user"
SENDER_KIND_ADMIN = "admin"
SENDER_KIND_SYSTEM = "system"


class SupportTicket(Base, TimestampMixin):
    """One support conversation."""
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_tickets_status", "status"),
        Index("ix_support_tickets_user", "user_id", "created_at"),
        Index("ix_support_tickets_last_message", "last_message_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_email: Mapped[str] = mapped_column(String(255), nullable=False)
    user_display_name: Mapped[Optional[str]] = mapped_column(String(255))

    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SUPPORT_STATUS_OPEN,
        server_default=SUPPORT_STATUS_OPEN,
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SUPPORT_PRIORITY_NORMAL,
        server_default=SUPPORT_PRIORITY_NORMAL,
    )
    assigned_admin_id: Mapped[Optional[str]] = mapped_column(String(26))

    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_user_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_admin_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Server-maintained counters incremented on each message from the
    # opposite side, cleared when the recipient reads the thread.
    unread_user_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    unread_admin_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_by_admin_id: Mapped[Optional[str]] = mapped_column(String(26))


class SupportMessage(Base):
    """A single message in a support ticket conversation."""
    __tablename__ = "support_messages"
    __table_args__ = (
        Index("ix_support_messages_ticket", "ticket_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    ticket_id: Mapped[str] = mapped_column(String(26), nullable=False)
    sender_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    sender_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    sender_display_name: Mapped[Optional[str]] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False, server_default=func.now(),
    )
