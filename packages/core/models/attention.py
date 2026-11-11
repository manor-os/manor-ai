"""Attention items: one row per situation that may need a person's notice."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class AttentionItem(Base, TimestampMixin):
    """A situation, kept in step with the platform state it describes.

    Watchers reconcile these rows from source tables (level-triggered): an
    item opens when its situation exists, its stage moves as it ages, and it
    resolves on the sweep after the situation is gone. One open row per
    ``fingerprint`` (kind + subject + Workspace), whatever its stage.
    """

    __tablename__ = "attention_items"
    __table_args__ = (
        Index(
            "uq_attention_items_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
        Index("ix_attention_items_workspace_state", "workspace_id", "state"),
        Index("ix_attention_items_source_state", "source", "state"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # None for an entity-level situation no Workspace depends on.
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # The watcher that owns the row: only it opens, updates or resolves it.
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)

    stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    urgency: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Why this item exists: ids of the source rows it was derived from.
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    assignee_user_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    # When the situation began (the source row's own time), not row creation.
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The highest stage people were told about (attention.delivery); None = never told.
    told_stage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    told_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["AttentionItem"]
