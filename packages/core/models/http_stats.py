"""Hourly HTTP request counts for the admin Performance dashboard.

One row per (hour, method, route template, status class), flushed from
Redis hash counters every 5 minutes by the ``metrics.http_flush`` Celery
task. The hot path (``apps.api.middleware.http_stats``) only does a
Redis HINCRBY — this table is a periodic snapshot-sync of those
counters, never written per-request. Route *templates* (e.g.
``/api/v1/items/{id}``), never raw paths, keep cardinality bounded;
requests that matched no route are collapsed into a single
``unmatched`` path. Platform-wide (no ``entity_id``), like the metrics
daily rollups.

Volume: bounded by (routes × methods × 4 status classes) per hour —
low hundreds of rows/hour in practice; pruned to 90 days by
``metrics_rollup.run_daily_rollup``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class HttpRequestHourly(Base):
    """One row per (hour, method, path template, status class)."""
    __tablename__ = "http_request_hourly"
    __table_args__ = (
        UniqueConstraint(
            "hour", "method", "path", "status_class",
            name="uq_http_request_hourly_bucket",
        ),
        Index("ix_http_request_hourly_hour", "hour"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    status_class: Mapped[str] = mapped_column(String(3), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
