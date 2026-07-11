"""Memory model — covers two things via ``scope``:

* **Per-agent memories**       facts an agent learned
  about the user across conversations. ``workspace_id`` null,
  ``scope`` null, identified by ``(entity_id, agent_id, user_id)``.

* **Workspace memories**       (new in goal-driven runtime): the
  workspace's "operating brain" — guidance notes (the SKILL.md role),
  decisions, learnings from past plans, user preferences. Identified
  by ``(workspace_id, scope)``. Strategist + Planner pull these as
  context when reviewing or planning. Embedded for similarity search
  via the ``embedding`` column (raw SQL — pgvector dependency lives
  outside the SQLAlchemy model layer).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class AgentMemory(Base, TimestampMixin):
    """Persistent memory entry — agent-scoped or workspace-scoped."""

    __tablename__ = "agent_memories"
    __table_args__ = (
        Index("ix_agent_memories_entity_agent", "entity_id", "agent_id"),
        Index("ix_agent_memories_importance", "importance"),
        Index("ix_agent_memories_workspace_scope", "workspace_id", "scope"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    # Set for workspace-scope memories; null for the
    # per-agent / per-user memories.

    memory_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # Agent-memory taxonomy: 'fact' | 'preference' | 'context' | 'instruction'
    scope: Mapped[Optional[str]] = mapped_column(String(32))
    # Workspace-runtime taxonomy:
    #   'guidance'   — durable how-we-work notes (SKILL.md role)
    #   'preference' — user preferences ("don't post on weekends")
    #   'decision'   — past explicit decisions worth recalling
    #   'learning'   — outcomes from past plans (Strategist eval)
    #   'fact'       — factual observations
    # Null for per-agent rows.

    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(SmallInteger, default=5)  # 1-10
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )
    # 0-1; Strategist/Planner only inject memories above a threshold
    # (default 0.7) into LLM context — keeps low-confidence learnings
    # from poisoning future decisions until validated.

    source: Mapped[Optional[str]] = mapped_column(String(100))
    # 'conversation:<id>' | 'plan_eval:<id>' | 'user_chat' | 'manual'
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, archived

    # ── Permission-v1 fields ─────────────────────────────────────────────
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, server_default="entity")
    classification: Mapped[str] = mapped_column(String(20), nullable=False, server_default="internal")
    owner_id: Mapped[Optional[str]] = mapped_column(String(26))

    # NOTE: the ``embedding`` column (vector(1024)) is added by the
    # ``20260424_02_goal_driven_runtime`` Alembic migration. We do NOT
    # declare it on the ORM here so importing this module does not
    # require pgvector to be installed at the SQLAlchemy layer (the
    # extension is a Postgres-only feature). Reads/writes go through
    # raw SQL in the workspace_memory service.
