"""Conversation share model — shareable links for conversations."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ConversationShare(Base):
    """Shared conversation link — allows viewing by anyone with the token."""
    __tablename__ = "conversation_shares"
    __table_args__ = (
        Index("ix_conversation_shares_token", "share_token", unique=True),
        Index("ix_conversation_shares_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    conversation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    shared_by: Mapped[str] = mapped_column(String(26), nullable=False)
    share_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
