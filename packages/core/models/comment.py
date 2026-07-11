"""Threaded comments on any resource (task, document, etc.)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class Comment(Base, TimestampMixin):
    """Threaded comment on any resource (task, document, etc.)."""
    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comments_resource", "resource_type", "resource_id"),
        Index("ix_comments_parent", "parent_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "task", "document"
    resource_id: Mapped[str] = mapped_column(String(26), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String(26))  # NULL = top-level
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_email: Mapped[Optional[str]] = mapped_column(String(255))  # denormalized for display
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mentions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="[]")
    anchor: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    reactions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    is_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
