"""Published static sites — snapshot of a workspace folder served publicly.

A Site row maps a source folder (or single HTML file) in the entity filesystem
to a public host: ``{slug}.{MANOR_SITES_DOMAIN}`` and optionally a user-owned
custom domain (CNAME + Caddy on-demand TLS). Snapshots live outside the
user-visible tree at ``.sites/{site_id}/rev{revision}/``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (
        Index("ix_sites_entity", "entity_id"),
        Index("ix_sites_workspace", "workspace_id"),
        Index("ix_sites_created_by_user", "created_by_user_id"),
        Index("ix_sites_slug", "slug", unique=True),
        Index("ix_sites_custom_domain", "custom_domain", unique=True),
        Index("uq_sites_entity_source", "entity_id", "source_path", unique=True),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # Durable management principal.  Source Document ACLs govern publishing
    # content, but cannot revoke or transfer ownership of an existing site.
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # DNS label — max 63 chars
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    # "active" | "offline"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Entity-relative path of the published folder or single HTML file
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    entry: Mapped[str] = mapped_column(String(255), nullable=False, default="index.html")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    custom_domain: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # None | "pending" | "active"
    domain_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Optional Manor workspace that owns the site's customer-facing runtime.
    # Public pages never receive this id; ``site-runtime.js`` resolves the
    # selected connections server-side from the request Host.
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    connections: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SiteEvent(Base):
    """First-party event emitted by a published Site Bridge runtime."""

    __tablename__ = "site_events"
    __table_args__ = (
        Index("ix_site_events_site_created", "site_id", "created_at"),
        Index("ix_site_events_workspace_created", "workspace_id", "created_at"),
        Index("ix_site_events_type", "site_id", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    site_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    referrer: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    properties: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
