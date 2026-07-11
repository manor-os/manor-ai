"""Platform-wide announcements (broadcast banners + email).

Distinct from the existing tenant-side ``Announcement`` model (in
``packages/core/models/channel.py``) which is for org-internal comms.
This is admin → all customers.

Audience targets:
  * ``all``         — every active tenant
  * ``plan:<id>``   — tenants on a specific plan
  * ``tenant:<id>`` — single tenant (rare; usually used for bespoke
                       maintenance windows on enterprise customers)
  * ``trial``       — tenants currently in trial
  * ``user:<id>``   — a single user

Multi-term audiences are stored in ``platform_announcement_audiences``
(one row per term). The legacy ``audience`` column on
``platform_announcements`` is kept and stores the first term for
rollout compatibility with stale clients.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, PrimaryKeyConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class PlatformAnnouncement(Base, TimestampMixin):
    """Admin-authored announcement shown on the user app."""
    __tablename__ = "platform_announcements"
    __table_args__ = (
        Index("ix_platform_announcements_active",
              "starts_at", "ends_at", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    """Markdown — rendered in the user app banner."""

    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, default="info", server_default="info",
    )
    # 'info' | 'warning' | 'critical'

    audience: Mapped[str] = mapped_column(
        String(80), nullable=False, default="all", server_default="all",
    )
    """e.g. 'all' | 'plan:01KX...' | 'tenant:01KX...' | 'trial'"""

    show_in_app: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    send_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_by_admin_id: Mapped[Optional[str]] = mapped_column(String(26))

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active",
    )
    # 'active' | 'archived'

    email_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    """Set once the send_email fanout task has finished. NULL means the
    email job hasn't run yet — the next save/publish with
    ``send_email=True`` will trigger it. Stays set after archive."""


class PlatformAnnouncementDismissal(Base):
    """Per-user dismissal record for a platform announcement.

    Replaces (and supplements) the client-only localStorage flag so a
    user who dismissed a banner doesn't see it again from another
    device or after clearing browser storage.
    """
    __tablename__ = "platform_announcement_dismissals"
    __table_args__ = (
        PrimaryKeyConstraint(
            "announcement_id", "user_id",
            name="pk_platform_announcement_dismissals",
        ),
        Index("ix_platform_announcement_dismissals_user", "user_id"),
    )

    announcement_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False, server_default=func.now(),
    )


class PlatformAnnouncementAudience(Base):
    """One audience term per row for a platform announcement.

    Valid terms: ``all`` | ``trial`` | ``plan:<ulid>`` | ``tenant:<ulid>``
    | ``user:<ulid>``. Replaces the single ``audience`` string column,
    which is kept (first term) for rollout compat and dropped later.
    """
    __tablename__ = "platform_announcement_audiences"
    __table_args__ = (
        PrimaryKeyConstraint(
            "announcement_id", "term",
            name="pk_platform_announcement_audiences",
        ),
        Index("ix_platform_announcement_audiences_term", "term"),
    )

    announcement_id: Mapped[str] = mapped_column(String(26), nullable=False)
    term: Mapped[str] = mapped_column(String(80), nullable=False)
