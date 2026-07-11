"""Chat message feedback model."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ChatMessageFeedback(Base):
    """Thumbs feedback for assistant chat messages.

    One active rating per user/message keeps the UI simple while preserving
    the latest signal for admin review.
    """

    __tablename__ = "chat_message_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_chat_feedback_message_user"),
        Index("ix_chat_feedback_entity_created", "entity_id", "created_at"),
        Index("ix_chat_feedback_conversation", "conversation_id", "created_at"),
        Index("ix_chat_feedback_rating", "rating", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    message_id: Mapped[str] = mapped_column(String(26), nullable=False)
    rating: Mapped[str] = mapped_column(String(10), nullable=False)
    content_preview: Mapped[Optional[str]] = mapped_column(Text)
    request_preview: Mapped[Optional[str]] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
