"""Workspace-scoped statistic definitions and append-only observations.

A WorkspaceStat describes *how* one numeric fact is collected. It is
deliberately independent from Goal: a stat may be displayed without a target,
while a Goal may optionally reference the stat and receive each observation as
its measurement value.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class WorkspaceStat(Base, TimestampMixin):
    """Versioned collection definition for one Workspace statistic."""

    __tablename__ = "workspace_stats"
    __table_args__ = (
        Index("uq_workspace_stats_workspace_key", "workspace_id", "key", unique=True),
        Index("ix_workspace_stats_entity_status", "entity_id", "status"),
        Index("ix_workspace_stats_library_key", "library_key"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)

    key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(24), nullable=False, default="number")
    unit: Mapped[Optional[str]] = mapped_column(String(40))
    window: Mapped[str] = mapped_column(String(40), nullable=False, default="rolling_7d")

    # manual | workspace_internal | integration
    collector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    collector_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    collection_cadence: Mapped[Optional[str]] = mapped_column(String(64))
    freshness_limit_seconds: Mapped[Optional[int]] = mapped_column(Integer)

    # user | ai | blueprint | library
    origin: Mapped[str] = mapped_column(String(24), nullable=False, default="user")
    library_key: Mapped[Optional[str]] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    goal_eligible: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    current_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 6))
    current_value_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_collection_status: Mapped[Optional[str]] = mapped_column(String(24))
    last_collection_error: Mapped[Optional[str]] = mapped_column(Text)

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class WorkspaceStatObservation(Base):
    """Append-only value emitted by a WorkspaceStat collector."""

    __tablename__ = "workspace_stat_observations"
    __table_args__ = (
        Index("ix_workspace_stat_observations_stat_time", "stat_id", "observed_at"),
        Index("ix_workspace_stat_observations_workspace_time", "workspace_id", "observed_at"),
        Index(
            "uq_workspace_stat_observations_idempotency",
            "stat_id",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    stat_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence: Mapped[Optional[dict]] = mapped_column(JSONB)
    collector_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
