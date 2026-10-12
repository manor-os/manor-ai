"""Append-only model transcript of a conversation or group room, per running principal.

The ``messages`` (or ``chat_room_messages``) table is what people see; this is
what the model saw. Items
are only appended: a compaction appends one item holding the replacement
history, and the working history is the latest compaction (or legacy import)
item plus every item after it.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid

TRANSCRIPT_KIND_MESSAGE = "message"
TRANSCRIPT_KIND_COMPACTION = "compaction"
# History imported once from the messages table when a conversation first
# switches to transcripts; acts as the first replacement history.
TRANSCRIPT_KIND_LEGACY = "legacy"


class ConversationTranscriptItem(Base, TimestampMixin):
    __tablename__ = "conversation_transcript_items"
    __table_args__ = (
        UniqueConstraint("conversation_id", "scope_key", "seq", name="uq_conversation_transcript_seq"),
        UniqueConstraint("room_id", "scope_key", "seq", name="uq_conversation_transcript_room_seq"),
        CheckConstraint(
            "(conversation_id IS NULL) <> (room_id IS NULL)", name="ck_conversation_transcript_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # Exactly one owner: a conversation, or a group room (rooms have no conversation row).
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("conversations.id", ondelete="CASCADE", name="fk_conversation_transcript_conversation"),
    )
    room_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("chat_rooms.id", ondelete="CASCADE", name="fk_conversation_transcript_room"),
    )
    # The running principal (user|agent|workspace, or room|binding|audience);
    # one ordered stream each.
    scope_key: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(String(26))
    # Messages-table row this item mirrors (user requests, final replies).
    source_message_id: Mapped[Optional[str]] = mapped_column(String(26))
    # kind=message: one provider-neutral message. compaction/legacy: {"messages": [...]}.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
