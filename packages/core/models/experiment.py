"""Experiment — bounded, reversible config experiments (M13).

An Experiment applies a temporary ``overlay_patch`` to one automation
target (``scheduled_job`` or ``workflow_binding`` in v1; ``task`` is
deliberately NOT an experiment target — one-off tasks are naturally
bounded and need no guardrails/rollback) WITHOUT bumping the target's
revision: experiments are not formal changes. Guardrails
(consecutive-failure rollback, max_runs, duration) bound the blast
radius; a deterministic evaluator compares the experiment cohort
against the frozen ``baseline_snapshot`` — never an LLM judgment.
Promotion is always a separate, formal ``automation_change`` proposal:
an experiment never silently becomes permanent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        Index("ix_experiments_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # The kind="experiment" proposal item this experiment executes (nullable
    # so ops/tests can create experiments outside the proposal flow).
    proposal_item_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    # {target_kind: scheduled_job|workflow_binding, target_id, max_runs, duration_days}
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {metric_name: {baseline, target}} — declared BEFORE the run; the
    # evaluator only ever compares these pre-declared metrics.
    success_metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {max_cost, rollback_on_consecutive_failures, hard_deadline?}
    guardrails: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Config overlay applied for the experiment's duration (no revision bump).
    overlay_patch: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Frozen at start: deterministic aggregate of the target's recent runs.
    baseline_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # pending|running|stopped_guardrail|completed|evaluated|promoted|rolled_back
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Deterministic evaluator output (per-metric verdicts + cost + violations).
    evaluation: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
