"""Persistent user session / active-time logs."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class UserSessionLog(Base):
    """One browser usage window for a user.

    Created when the first WebSocket connection for a user comes online
    in an API process, refreshed by presence heartbeats, and closed when
    the last connection disconnects. It is intentionally append-only-ish:
    rows are updated only to keep the live duration/last_seen fields
    current for admin analytics.
    """
    __tablename__ = "user_session_logs"
    __table_args__ = (
        Index("ix_user_session_entity_started", "entity_id", "started_at"),
        Index("ix_user_session_user_started", "user_id", "started_at"),
        Index("ix_user_session_entity_status", "entity_id", "status", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False, server_default="web")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    ip_address: Mapped[Optional[str]] = mapped_column(String(128))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    heartbeat_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # In-progress page-view segment. When the presence WS message says
    # the user navigated, the prior segment is flushed to
    # user_page_view_logs and these fields are reopened on the new path.
    current_path: Mapped[Optional[str]] = mapped_column(String(500))
    current_path_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Cached geo derived from ``ip_address`` at session start. Skipped
    # for private IPs. Stored here so admin reads don't fan out to an
    # external API; refresh on next sign-in.
    country_code: Mapped[Optional[str]] = mapped_column(String(2))
    country: Mapped[Optional[str]] = mapped_column(String(80))
    city: Mapped[Optional[str]] = mapped_column(String(120))
    # ip-api.com returns lat/lon on the same lookup as city/country;
    # storing them lets the admin map plot real city points instead
    # of name-based geocoding on the fly.
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)


class UserPageViewLog(Base):
    """One completed page-view segment for a user.

    Append-only. Written when the user navigates away from a page
    (presence WS frame reports a new ``viewing``) or when the session
    closes — never on every heartbeat, so traffic stays proportional
    to actual navigation, not idle time.
    """
    __tablename__ = "user_page_view_logs"
    __table_args__ = (
        Index("ix_page_view_user_path", "user_id", "path"),
        Index("ix_page_view_entity_started", "entity_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    session_id: Mapped[Optional[str]] = mapped_column(String(26))
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
