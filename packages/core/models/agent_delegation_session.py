"""Private, resumable transcripts for Workspace service delegation."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class AgentDelegationSession(Base, TimestampMixin):
    __tablename__ = "agent_delegation_sessions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "user_id", "agent_subscription_id",
            name="uq_agent_delegation_session_scope",
        ),
        Index("ix_agent_delegation_sessions_workspace", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_subscription_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    service_key: Mapped[Optional[str]] = mapped_column(String(100))
    messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    context_usage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    context_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")
    active_run_id: Mapped[Optional[str]] = mapped_column(String(26))
    active_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AgentContextArchive(Base, TimestampMixin):
    """Cold context kept outside the model window, scoped through its session."""
    __tablename__ = "agent_context_archives"
    __table_args__ = (
        UniqueConstraint("session_id", "digest", name="uq_agent_context_archive_digest"),
        UniqueConstraint("runtime_run_id", "digest", name="uq_runtime_context_archive_digest"),
        CheckConstraint("(session_id IS NULL) <> (runtime_run_id IS NULL)", name="ck_context_archive_owner"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    session_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("agent_delegation_sessions.id", ondelete="CASCADE"),
    )
    runtime_run_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("runtime_runs.id", ondelete="CASCADE", name="fk_context_archive_runtime_run"),
    )
    run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    messages: Mapped[list] = mapped_column(JSONB, nullable=False)
    categories: Mapped[list] = mapped_column(JSONB, nullable=False)
