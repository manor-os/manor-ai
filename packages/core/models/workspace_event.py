"""WorkspaceEvent — the append-only workspace event ledger (M1).

Business-level facts from every execution kind (Task / Automation / Workflow /
Skill / Agent / human / approval / goal / artifact / evaluation / config) are
appended to this single table, in the same transaction as the business write.
The ledger is the frozen, replayable data source for Review (M2 watermarks
cursor over ``id``, which is a time-ordered ULID) and the correlation skeleton
for Timeline (M14).

Rows are immutable: append-only, never updated or deleted. Idempotency is
enforced by the unique ``(entity_id, idempotency_key)`` index — a duplicate
write is silently dropped by the service layer (``ledger.service.record_event``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class WorkspaceEvent(Base):
    __tablename__ = "workspace_events"
    __table_args__ = (
        # Main review/timeline scans: per-workspace watermark windows.
        Index("ix_workspace_events_workspace_id_id", "workspace_id", "id"),
        Index("ix_workspace_events_workspace_event_type_id", "workspace_id", "event_type", "id"),
        # Correlation skeleton.
        Index("ix_workspace_events_root_execution", "root_execution_id"),
        Index("ix_workspace_events_correlation", "correlation_id"),
        # Idempotent append: at most one row per (entity, idempotency_key).
        Index("uq_workspace_events_idempotency", "entity_id", "idempotency_key", unique=True),
    )

    # ULID → time-sorted → doubles as the watermark cursor.
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # ── what happened ──────────────────────────────────────────────
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # task|scheduled_job|workflow|plan_step|approval|proposal|goal|artifact|
    # human|chat|config|evaluation|experiment
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # succeeded|failed|... — semantics depend on event_type
    status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    # ── correlation skeleton ───────────────────────────────────────
    root_execution_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # execution-chain root
    causation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)       # whose decision triggered this
    correlation_id: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)     # same-goal same-period dedup key

    # ── who ────────────────────────────────────────────────────────
    # user|agent|system|worker|external
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    actor_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── references (large objects live behind refs, never in payload) ──
    goal_refs: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    output_refs: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)     # ["artifact:doc_x", "task:..."]
    evidence_refs: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)   # ["runtime_event:...", "step:..."]
    # {automation_revision, agent_revision, skill_revision, policy_revision, operation_revision}
    config_versions: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)         # small business summary only

    period_key: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # ── time ───────────────────────────────────────────────────────
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
