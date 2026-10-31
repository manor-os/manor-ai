"""Human chat rooms, membership, invitations, and messages.

These rooms are not Workspace conversations.  An Agent binding, when added,
must establish its own external-audience runtime scope before AI execution.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, false, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class ChatRoom(Base, TimestampMixin):
    __tablename__ = "chat_rooms"
    __table_args__ = (
        Index("ix_chat_rooms_owner_entity", "owner_entity_id"),
        Index("uq_chat_rooms_dm_key", "dm_key", unique=True),
        Index("uq_chat_rooms_public_invite_token", "public_invite_token", unique=True),
        CheckConstraint("kind = 'group' OR public_invite_token IS NULL", name="ck_chat_rooms_public_invite_group"),
        CheckConstraint("kind IN ('dm', 'group')", name="ck_chat_rooms_kind"),
        CheckConstraint("status IN ('active', 'closed')", name="ck_chat_rooms_status"),
        CheckConstraint("ai_tool_execution_revision >= 0", name="ck_chat_rooms_ai_tool_execution_revision"),
        CheckConstraint(
            "kind = 'group' OR ai_tool_execution_enabled = false",
            name="ck_chat_rooms_dm_ai_tool_execution_disabled",
        ),
        CheckConstraint(
            "ai_file_actions_direct_enabled = false OR (kind = 'group' AND ai_tool_execution_enabled = true)",
            name="ck_chat_rooms_direct_requires_enabled_group",
        ),
        CheckConstraint(
            "(kind = 'dm' AND owner_user_id IS NOT NULL AND owner_entity_id IS NULL) OR "
            "(kind = 'group' AND ((owner_user_id IS NULL) <> (owner_entity_id IS NULL)))",
            name="ck_chat_rooms_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # dm | group
    title: Mapped[Optional[str]] = mapped_column(String(255))
    owner_entity_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("entities.id", ondelete="CASCADE"))
    owner_user_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("users.id", ondelete="CASCADE"))
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("users.id", ondelete="SET NULL"))
    dm_key: Mapped[Optional[str]] = mapped_column(String(320))
    # Reusable bearer link, exposed only through the owner/admin management API.
    public_invite_token: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    # A room-wide capability ceiling. Each tool call still needs its own actor
    # and authorization; historical On rooms retain their approval flow.
    ai_tool_execution_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    ai_file_actions_direct_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    ai_tool_execution_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    approval_policy: Mapped[Optional[dict]] = mapped_column(JSONB)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ChatRoomMember(Base):
    __tablename__ = "chat_room_members"
    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_chat_room_members_room_user"),
        Index("ix_chat_room_members_user_status", "user_id", "status"),
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_chat_room_members_role"),
        CheckConstraint("status IN ('active', 'left')", name="ck_chat_room_members_status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="member")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    visible_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notifications_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ChatRoomInvite(Base):
    __tablename__ = "chat_room_invites"
    __table_args__ = (
        Index("uq_chat_room_invites_token", "token_hash", unique=True),
        Index("ix_chat_room_invites_room_status", "room_id", "status"),
        CheckConstraint("status IN ('pending', 'accepted', 'revoked')", name="ck_chat_room_invites_status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    invited_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("users.id", ondelete="SET NULL"))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ChatRoomMessage(Base):
    __tablename__ = "chat_room_messages"
    __table_args__ = (
        Index("ix_chat_room_messages_room_time", "room_id", "created_at", "id"),
        Index("ix_chat_room_messages_content_trgm", "content", postgresql_using="gin", postgresql_ops={"content": "gin_trgm_ops"}),
        CheckConstraint(
            "sender_agent_binding_id IS NULL OR (sender_user_id IS NULL AND sender_agent_name IS NOT NULL)",
            name="ck_chat_room_messages_agent_sender",
        ),
        CheckConstraint(
            "agent_run_status IS NULL OR (sender_agent_binding_id IS NOT NULL AND "
            "agent_run_status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted'))",
            name="ck_chat_room_messages_agent_run_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False)
    sender_user_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("users.id", ondelete="SET NULL"))
    # Snapshots survive removal of a room Agent binding.
    sender_agent_binding_id: Mapped[Optional[str]] = mapped_column(String(26))
    sender_agent_name: Mapped[Optional[str]] = mapped_column(String(255))
    sender_agent_avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    reply_to_message_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("chat_room_messages.id", ondelete="SET NULL")
    )
    agent_run_status: Mapped[Optional[str]] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mentioned_user_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("users.id", ondelete="SET NULL"))


class ChatRoomMessageReaction(Base):
    __tablename__ = "chat_room_message_reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", "emoji", name="uq_chat_room_message_reaction"),
        Index("ix_chat_room_message_reactions_message", "message_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    message_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_room_messages.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatRoomFile(Base):
    """A room-only attachment: inline/chunked bytes or a pinned Knowledge artifact."""
    __tablename__ = "chat_room_files"
    __table_args__ = (
        Index("ix_chat_room_files_message", "message_id"),
        UniqueConstraint("source_run_id", "document_id", name="uq_chat_room_files_run_document"),
        CheckConstraint(
            "size > 0 AND ((content IS NOT NULL AND size <= 104857600 AND chunk_count IS NULL "
            "AND document_id IS NULL AND document_entity_id IS NULL AND content_sha256 IS NULL "
            "AND source_run_id IS NULL AND source_media_job_id IS NULL) "
            "OR (content IS NULL AND chunk_count IS NOT NULL AND chunk_count > 0 AND size <= 1073741824 "
            "AND (chunk_count - 1) * 4194304 < size AND size <= chunk_count * 4194304 "
            "AND document_id IS NULL AND document_entity_id IS NULL AND content_sha256 IS NOT NULL "
            "AND length(content_sha256) = 64 AND source_run_id IS NULL AND source_media_job_id IS NULL) "
            "OR (content IS NULL AND chunk_count IS NULL AND document_id IS NOT NULL "
            "AND document_entity_id IS NOT NULL AND content_sha256 IS NOT NULL "
            "AND length(content_sha256) = 64 AND source_run_id IS NOT NULL))",
            name="ck_chat_room_files_storage",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    message_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_room_messages.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    chunk_count: Mapped[Optional[int]] = mapped_column(Integer)
    document_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("documents.id", ondelete="CASCADE")
    )
    document_entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    content_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    source_run_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("chat_room_agent_runs.id", ondelete="CASCADE")
    )
    source_media_job_id: Mapped[Optional[str]] = mapped_column(String(26))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ChatRoomFileChunk(Base):
    """Bounded room-file bytes, retained and deleted with their parent attachment."""
    __tablename__ = "chat_room_file_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_chat_room_file_chunks_index"),
        CheckConstraint(
            "length(content) > 0 AND length(content) <= 4194304",
            name="ck_chat_room_file_chunks_size",
        ),
    )

    file_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("chat_room_files.id", ondelete="CASCADE"), primary_key=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class ChatRoomAgentBinding(Base, TimestampMixin):
    """An explicit external-audience route to one deployment, not Workspace access."""
    __tablename__ = "chat_room_agent_bindings"
    __table_args__ = (
        UniqueConstraint("room_id", "subscription_id", name="uq_chat_room_agent_binding_subscription"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_chat_room_agent_binding_status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False)
    subscription_id: Mapped[str] = mapped_column(String(26), ForeignKey("agent_subscriptions.id", ondelete="CASCADE"), nullable=False)
    added_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class ChatRoomAgentRun(Base):
    """One durable invocation of a bound Agent from a group message."""

    __tablename__ = "chat_room_agent_runs"
    __table_args__ = (
        UniqueConstraint("source_message_id", "binding_id", name="uq_chat_room_agent_runs_source_binding"),
        Index("ix_chat_room_agent_runs_room_status", "room_id", "status", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting_approval', 'succeeded', 'failed', 'interrupted')",
            name="ck_chat_room_agent_runs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False)
    source_message_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("chat_room_messages.id", ondelete="CASCADE"), nullable=False
    )
    # Not an FK: removing a binding must not erase execution history.
    binding_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    audience_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Non-null only for an owner/admin turn admitted while execution was on.
    # A later enable or re-enable must not upgrade an older queued run.
    ai_tool_execution_revision: Mapped[Optional[int]] = mapped_column(Integer)
    # Durable Runtime checkpoint. A pending approval never replays model output.
    checkpoint: Mapped[Optional[dict]] = mapped_column(JSONB)
    # Public execution events only; reply candidates remain in the checkpoint.
    assistant_blocks: Mapped[Optional[list]] = mapped_column(JSONB)
    response_message_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("chat_room_messages.id", ondelete="SET NULL")
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_enqueued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ChatRoomToolApproval(Base):
    """One exact, room-scoped Agent mutation receipt or pending review."""

    __tablename__ = "chat_room_tool_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_chat_room_tool_approval_call"),
        Index("ix_chat_room_tool_approvals_room_status", "room_id", "status", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'claimed', 'succeeded', 'failed', 'invalidated')",
            name="ck_chat_room_tool_approvals_status",
        ),
        CheckConstraint("ai_tool_execution_revision >= 0", name="ck_chat_room_tool_approvals_revision"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(26), ForeignKey("chat_room_agent_runs.id", ondelete="CASCADE"), nullable=False)
    requester_user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(26), nullable=False)
    audience_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ai_tool_execution_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Applicable requirements when this exact call was presented for review.
    approval_requirements: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    display_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    decided_by_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    # Server-authored result identity, never accepted from the approval API.
    result_document_id: Mapped[Optional[str]] = mapped_column(String(26))
    result_media_job_id: Mapped[Optional[str]] = mapped_column(String(26))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
