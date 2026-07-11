"""Workspace Blueprint — shareable workspace configuration package.

See migration 20260424_08 for the column rationale and
``packages/core/blueprints/payload.py`` for the JSONB payload schema.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class WorkspaceBlueprint(Base):
    __tablename__ = "workspace_blueprints"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "slug",
            name="uq_workspace_blueprints_entity_slug",
        ),
        Index(
            "ux_workspace_blueprints_share_token", "share_token",
            unique=True, postgresql_where=text("share_token IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    source_workspace_id: Mapped[Optional[str]] = mapped_column(String(26))

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    cover_image_url: Mapped[Optional[str]] = mapped_column(String(500))
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_version: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="1.0",
    )

    # 'draft' | 'pending_review' | 'published' | 'archived'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft",
    )
    install_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )

    # ── Marketplace monetization (workspace-marketplace spec) ──
    # NULL or 0 = free. Integer cents, usd-only in v1.
    price_cents: Mapped[Optional[int]] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="usd", default="usd",
    )
    purchase_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0,
    )
    # Revocable link-sharing token (None = sharing off).
    share_token: Mapped[Optional[str]] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(),
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
