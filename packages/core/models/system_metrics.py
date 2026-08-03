"""Host resource history samples for the admin Performance dashboard.

One tiny flat row per ops collector tick (30s): CPU / memory / disk
percentages plus 1-minute load average, extracted from the same snapshot
``ops.collect_snapshot`` already writes to Redis. Deliberately minimal —
no per-container breakdown (that stays in the live Redis snapshot on the
Ops page); this table only powers platform-level trend charts. No
``entity_id`` — platform-wide, like the metrics daily rollups.

Volume: one row per 30s ≈ 2,880/day; pruned to 14 days by
``metrics_rollup.run_daily_rollup`` ≈ 40k rows max.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class SystemMetricsSample(Base):
    """One host-resource sample per ops collector tick."""
    __tablename__ = "system_metrics_samples"
    __table_args__ = (
        Index("ix_system_metrics_sampled_at", "sampled_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
