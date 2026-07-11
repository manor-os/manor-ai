"""Persistent business goals — the north-star layer of the runtime.

A Goal is a metric the workspace commits to moving (e.g. "10k Twitter
followers by Oct 24"). The Strategist reads active goals + their pace
to propose weekly tasks; the measurement service appends
``goal_measurements`` rows on a cadence; goal_task_links attribute
impact back to specific tasks.

This is **not** the old "GoalRun" concept. Those are gone. A Goal
persists for months; an execution_plan runs for minutes to days to
move it.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Numeric, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class Goal(Base, TimestampMixin):
    """A tracked business goal."""

    __tablename__ = "goals"
    __table_args__ = (
        Index("ix_goals_workspace_status", "workspace_id", "status"),
        Index("ix_goals_entity_status", "entity_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    # NULL = entity-level goal that transcends any single workspace.
    # Strategist only looks at workspace goals when reviewing a
    # specific workspace — entity-level goals appear on the global
    # dashboard only.

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    metric_key: Mapped[str] = mapped_column(String(100), nullable=False)
    # Canonical key like 'follower_count' | 'mrr' | 'engagement_rate'.
    # Matches the key used by measurement_source + the corresponding
    # integration adapter's result shape.

    target_value: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    baseline_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4))
    current_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4))
    current_value_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    deadline: Mapped[Optional[date]] = mapped_column(Date)
    pace_status: Mapped[Optional[str]] = mapped_column(String(20))
    # 'on_track' | 'behind' | 'ahead' | 'at_risk' | 'achieved' | 'unknown'
    pace_computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # 'active' | 'achieved' | 'abandoned' | 'paused'

    measurement_source: Mapped[Optional[dict]] = mapped_column(JSONB)
    # {provider: 'twitter_x', action: 'get_profile_stats', params: {...}}
    # Consumed by the measurement_service via the integration adapter.
    measurement_cadence: Mapped[Optional[str]] = mapped_column(String(64))
    # 'hourly' | 'daily' | 'weekly' | cron expression.

    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)

    outcome_window_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=7, server_default="7",
    )
    # How long after a Strategist proposal completes we wait before
    # labeling its outcome. Marketing/growth metrics need ~7d; transactional
    # ones (booking confirmed, email sent) can use 1. Read by
    # ``strategist.evaluation.evaluate_strategist_outcomes``.

    achieved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class GoalMeasurement(Base):
    """Append-only time-series of goal metric values.

    The measurement service writes one row per poll. ``source`` records
    where the value came from so simulated / manual values can be
    filtered out when computing pace in production mode.
    """

    __tablename__ = "goal_measurements"

    goal_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    value: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(64))
    # 'integration:twitter_x' | 'manual' | 'imported' | 'simulated'
    meta: Mapped[Optional[dict]] = mapped_column(JSONB)


class GoalTaskLink(Base):
    """Attribution: which tasks contributed to which goal's movement."""

    __tablename__ = "goal_task_links"
    __table_args__ = (
        Index("ix_goal_task_links_task", "task_id"),
    )

    goal_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    contribution: Mapped[str] = mapped_column(String(16), nullable=False, default="direct")
    # 'direct' — task was created explicitly to move this goal (set by
    #            Strategist when it links the task at creation)
    # 'indirect' — task wasn't created for this goal but turned out to
    #              affect it (set by post-hoc analysis)
    # 'discovered' — system noticed a correlation after the fact (set
    #                by a correlation job; lowest confidence)
    estimated_impact: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4))
    # Expected metric delta contributed by this task (what Strategist
    # budgeted for it).
    actual_impact: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4))
    # Measured metric delta during the task's execution window.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
