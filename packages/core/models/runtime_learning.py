"""Runtime evidence, event trace, and agent learning candidate models.

These tables form the durable backbone for workspace/agent self-improvement:
append-only runtime observations first, then reviewable learning candidates
(memory, skill, profile patch, or tool-experience) derived from those facts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class RuntimeEvidence(Base, TimestampMixin):
    """A compact, queryable record of one runtime outcome.

    Raw data still lives in messages, tool_call_logs, task_logs, plans, and
    usage logs. This ledger stores the cross-cutting summary an agent or
    Strategist needs to learn from what just happened.
    """

    __tablename__ = "runtime_evidence"
    __table_args__ = (
        Index("ix_runtime_evidence_entity_created", "entity_id", "created_at"),
        Index("ix_runtime_evidence_workspace_created", "entity_id", "workspace_id", "created_at"),
        Index("ix_runtime_evidence_agent_created", "entity_id", "agent_id", "created_at"),
        Index("ix_runtime_evidence_task_created", "task_id", "created_at"),
        Index("ix_runtime_evidence_type_status", "evidence_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))
    message_id: Mapped[Optional[str]] = mapped_column(String(26))
    task_id: Mapped[Optional[str]] = mapped_column(String(26))
    trace_id: Mapped[Optional[str]] = mapped_column(String(64))

    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # chat_run | task_run | tool_summary | user_feedback | strategist_review
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="runtime")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="succeeded")
    # succeeded | failed | blocked | partial | pending
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class RuntimeEventLog(Base, TimestampMixin):
    """Durable, queryable event rows emitted by Manor AI Runtime Harness."""

    __tablename__ = "runtime_event_logs"
    __table_args__ = (
        Index("ix_runtime_event_logs_entity_created", "entity_id", "created_at"),
        Index("ix_runtime_event_logs_conversation_created", "conversation_id", "created_at"),
        Index("ix_runtime_event_logs_task_created", "task_id", "created_at"),
        Index("ix_runtime_event_logs_type_created", "event_type", "created_at"),
        Index("ix_runtime_event_logs_tool_created", "tool_name", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))
    message_id: Mapped[Optional[str]] = mapped_column(String(26))
    task_id: Mapped[Optional[str]] = mapped_column(String(26))
    trace_id: Mapped[Optional[str]] = mapped_column(String(64))

    surface: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[Optional[str]] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="runtime")
    sequence: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    event_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    runtime: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class AgentLearningCandidate(Base, TimestampMixin):
    """Reviewable proposal for improving an agent or workspace runtime.

    Candidates are deliberately separate from applied memories/skills/profile
    edits. The system can notice patterns automatically, while governance and
    UI flows decide what actually changes behavior.
    """

    __tablename__ = "agent_learning_candidates"
    __table_args__ = (
        Index("ix_learning_candidates_entity_status", "entity_id", "status", "created_at"),
        Index("ix_learning_candidates_workspace_status", "entity_id", "workspace_id", "status"),
        Index("ix_learning_candidates_agent_status", "entity_id", "agent_id", "status"),
        Index("ix_learning_candidates_dedupe", "entity_id", "dedupe_key"),
        Index("ix_learning_candidates_type_status", "candidate_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))

    candidate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # memory | skill | profile_patch | rule | tool_experience
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="agent")
    # agent | workspace | user | entity
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(255))

    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    # proposed | accepted | rejected | applied | archived
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5, server_default="0.5")
    created_by: Mapped[str] = mapped_column(String(50), nullable=False, default="runtime")

    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    resolution: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
