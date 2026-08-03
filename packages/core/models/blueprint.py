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
        # (entity_id, slug) stops protecting platform rows once entity_id is
        # NULL — Postgres treats NULLs as distinct — so they get their own.
        Index(
            "ux_workspace_blueprints_platform_slug", "slug",
            unique=True, postgresql_where=text("entity_id IS NULL"),
        ),
    )

    #: Wide enough for the platform's own ids (``builtin:<slug>``) alongside
    #: the ULIDs an export mints.
    id: Mapped[str] = mapped_column(String(160), primary_key=True, default=generate_ulid)
    #: NULL means the platform owns this one — an officially published
    #: blueprint, reviewed and versioned through the same admin flow as any
    #: contributor's.
    entity_id: Mapped[Optional[str]] = mapped_column(String(26), index=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    source_workspace_id: Mapped[Optional[str]] = mapped_column(String(26))

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    cover_image_url: Mapped[Optional[str]] = mapped_column(String(500))
    showcase_assets: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    author_user_id: Mapped[Optional[str]] = mapped_column(String(26), index=True)
    author_handle: Mapped[Optional[str]] = mapped_column(String(100))
    author_display_name: Mapped[Optional[str]] = mapped_column(String(200))
    remixed_from_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: Payload SCHEMA version — the installer reads it to pick a migrator.
    #: Not a content version: it sat at "1.1" through a full rewrite of an
    #: embedded skill. Use ``content_version`` to answer "is my install
    #: behind".
    payload_version: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="1.0",
    )

    #: major.minor.patch. Bumped on publish, and only when
    #: ``content_fingerprint`` shows the installable content actually
    #: changed. This is what an installed workspace compares itself against.
    content_version: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="1.0.0",
    )
    content_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))

    # 'draft' | 'pending_review' | 'published' | 'archived'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft",
    )
    install_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )

    # ── Marketplace monetization (workspace-marketplace spec) ──
    # NULL or 0 = free. Integer cents, usd-only in v1.
    # price_cents is always the current checkout price. list_price_cents is
    # optional display-only MSRP and is set only while a sale is active.
    price_cents: Mapped[Optional[int]] = mapped_column(Integer)
    list_price_cents: Mapped[Optional[int]] = mapped_column(Integer)
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


class BlueprintFavorite(Base):
    """A user's cross-tenant Marketplace Blueprint bookmark."""

    __tablename__ = "blueprint_favorites"
    __table_args__ = (
        UniqueConstraint(
            "blueprint_id", "user_id",
            name="uq_blueprint_favorites_blueprint_user",
        ),
        Index("ix_blueprint_favorites_blueprint", "blueprint_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    blueprint_id: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
