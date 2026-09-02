"""Chat message feedback model."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ChatMessageFeedback(Base):
    """Thumbs feedback for assistant chat messages.

    Normal replies use the message as their subject. Completion receipts use
    their Plan (or Task for task-only receipts), so projections of one
    completion into multiple conversations still produce one user signal.
    """

    __tablename__ = "chat_message_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_chat_feedback_message_user"),
        Index(
            "ux_chat_feedback_target_user",
            "target_kind",
            "target_id",
            "user_id",
            unique=True,
        ),
        Index("ix_chat_feedback_entity_created", "entity_id", "created_at"),
        Index("ix_chat_feedback_conversation", "conversation_id", "created_at"),
        Index("ix_chat_feedback_user", "user_id"),
        Index("ix_chat_feedback_rating", "rating", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("users.id", name="fk_chat_feedback_user", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey(
            "conversations.id",
            name="fk_chat_feedback_conversation",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("messages.id", name="fk_chat_feedback_message", ondelete="CASCADE"),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    task_id: Mapped[Optional[str]] = mapped_column(String(26))
    plan_id: Mapped[Optional[str]] = mapped_column(String(26))
    rating: Mapped[str] = mapped_column(String(10), nullable=False)
    content_preview: Mapped[Optional[str]] = mapped_column(Text)
    request_preview: Mapped[Optional[str]] = mapped_column(Text)
    mutation_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
