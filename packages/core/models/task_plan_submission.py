"""Immutable, Task-generation-scoped requests for V3 Plan creation."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class TaskPlanSubmissionKind(str, Enum):
    PLANNER = "planner"
    MANUAL = "manual"


class TaskPlanSubmissionState(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


class TaskPlanSubmission(Base, TimestampMixin):
    """One frozen Plan request per Task lifecycle generation.

    A retry can explicitly carry a pending manual request into a new generation
    as a new row. An applied request never supplies a Task replan DAG.
    """

    __tablename__ = "task_plan_submissions"
    __table_args__ = (
        UniqueConstraint("task_id", "lifecycle_generation", name="uq_task_plan_submission_generation"),
        CheckConstraint(
            "(request_kind = 'manual' AND plan_dag IS NOT NULL) OR "
            "(request_kind = 'planner' AND plan_dag IS NULL)",
            name="ck_task_plan_submission_kind_dag",
        ),
    )

    # ``now()`` is transaction-start time. A Task-locked request can begin an
    # earlier transaction but insert after another committed generation;
    # latest-receipt ordering must use the actual DB insertion time instead.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False,
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    task_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    lifecycle_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    approval_required: Mapped[bool] = mapped_column(nullable=False, default=False)
    plan_dag: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_submission_id: Mapped[Optional[str]] = mapped_column(String(26))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=TaskPlanSubmissionState.PENDING.value)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    plan_id: Mapped[Optional[str]] = mapped_column(String(26))
