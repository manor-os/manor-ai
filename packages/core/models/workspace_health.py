"""Per-Workspace health history and current state.

``metrics_workspace_daily`` holds additive counts per (UTC day, Workspace,
surface) so any date range can be summed; it never stores a rate or a
percentile. ``workspace_health_snapshots`` is the current state, one row per
Workspace. Both are written only by
``packages.core.services.workspace_health_rollup`` /
``workspace_health_snapshot``; definitions live in
``packages.core.services.workspace_health_metrics``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Date, DateTime, Index, Integer, Numeric, String, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class MetricsWorkspaceDaily(Base):
    """One row per (day, workspace_id, surface).

    Run surfaces (task / agent / workflow / automation) fill the outcome
    columns; the ``workspace`` surface fills the quality counts and credits."""

    __tablename__ = "metrics_workspace_daily"
    __table_args__ = (
        UniqueConstraint("day", "workspace_id", "surface", name="uq_metrics_workspace_daily"),
        Index("ix_metrics_workspace_daily_ws_day", "workspace_id", "day"),
        Index("ix_metrics_workspace_daily_blueprint_day", "blueprint_key", "day"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    surface: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    blueprint_key: Mapped[Optional[str]] = mapped_column(String(200))
    blueprint_version: Mapped[Optional[str]] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(12), nullable=False)
    succeeded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cancelled: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retried: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_sum_s: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    duration_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_buckets: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    top_errors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    user_cancelled: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reviews_accepted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reviews_rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reviews_ignored: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    feedback_up: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    feedback_down: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    proposals_accepted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    proposals_rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    credits: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class WorkspaceHealthSnapshot(Base):
    """Current health of one Workspace, refreshed hourly."""

    __tablename__ = "workspace_health_snapshots"
    __table_args__ = (
        Index("ix_workspace_health_snapshots_status", "status"),
        Index("ix_workspace_health_snapshots_blueprint", "blueprint_key", "blueprint_version"),
    )

    workspace_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    blueprint_key: Mapped[Optional[str]] = mapped_column(String(200))
    blueprint_version: Mapped[Optional[str]] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    status_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    """[{"rule": str, "surface": str | None, "value": number | None}] — every rule that matched."""
    surfaces: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """{surface: {"success_rate_24h", "success_rate_7d", "judged_7d", "stalled"} | {"unknown": true}}"""
    quality: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """7-day rates: acceptance_rate, ignored_rate, feedback_rate, retry_rate,
    user_cancel_rate, proposal_acceptance_rate (None when under MIN_JUDGED_RUNS)."""
    readiness_gaps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    missed_schedules: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pending_approvals_over_24h: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    service_coverage: Mapped[Optional[float]] = mapped_column(Numeric(5, 4))
    live_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
