"""ReviewRun — one Strategist review cycle over a frozen ledger window (M2).

Each review freezes a snapshot at begin time:

* ``watermark_start`` / ``watermark_end`` — ``workspace_events.id`` cursors
  (ULIDs are time-ordered, so ``start < id <= end`` is the review's window).
  ``watermark_start`` is the previous *succeeded* review's ``watermark_end``
  (``None`` == ledger genesis); ``watermark_end`` is the max event id at
  begin time (``None`` == no events yet).
* ``workspace_revision`` / ``policy_revision`` — config versions frozen at
  begin time so the review's reasoning is attributable to exact config.

Watermark semantics (裁定 C): the watermark only advances when a review
reaches ``status='succeeded'``. Skipped reviews keep a row but force
``watermark_end = watermark_start``; failed reviews are simply ignored by
the next ``begin_review`` (which chains off the latest *succeeded* row),
so the same window is re-consumed.

The partial unique index ``uq_review_runs_one_running`` guarantees at most
one ``running`` review per workspace — concurrent triggers race on the
insert and the loser gets ``ReviewAlreadyRunning``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ReviewRun(Base):
    __tablename__ = "review_runs"
    __table_args__ = (
        # Single running review per workspace (mutual exclusion by insert).
        Index(
            "uq_review_runs_one_running",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_review_runs_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # ``ReviewTriggerKind`` value: scheduled|event|human_requested.
    # Rows written before the enum landed still hold the old free-text
    # trigger (e.g. "user_request: replan Q3"); they are not backfilled and
    # must be read through ``classify_legacy_trigger``.
    trigger_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # Free text for display + audit only. Nothing branches on it.
    trigger_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # running|succeeded|failed|skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    skip_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── frozen snapshot ────────────────────────────────────────────────
    window_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # workspace_events.id cursors (exclusive start, inclusive end)
    watermark_start: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    watermark_end: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    workspace_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    policy_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ── outputs ────────────────────────────────────────────────────────
    briefing: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # M5 product (frozen)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
