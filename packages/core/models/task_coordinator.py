"""Durable Task-level coordinator state, independent of Plan revisions.

This row is the durable state of the new Workspace Task execution owner.
In-flight V2 Plan coordinators keep their existing Step/Lease checkpoint.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class TaskCoordinatorPhase(str, Enum):
    READY = "ready"
    RUNNING = "running"
    WAITING_PLAN = "waiting_plan"
    WAITING_CHILD = "waiting_child"
    WAITING_HUMAN = "waiting_human"
    # A Task agent loop parked on its own wait_until / media jobs. The worker
    # is released; a delayed wake (or the job completion hook) resumes it.
    WAITING_TIMER = "waiting_timer"
    WAITING_JOB = "waiting_job"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.COMPLETED.value, cls.FAILED.value, cls.CANCELLED.value})


class TaskCoordinatorEngine(str, Enum):
    """Which execution host drives a Task run. Frozen when the run is created."""

    PLAN_DAG = "plan_dag"
    AGENT_LOOP = "agent_loop"


class TaskCoordinatorRun(Base, TimestampMixin):
    """One fenced coordinator loop for a Workspace Task across Plan versions."""

    __tablename__ = "task_coordinator_runs"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_task_coordinator_runs_task"),
        Index("ix_task_coordinator_runs_workspace_phase", "workspace_id", "phase"),
        Index(
            "ix_task_coordinator_runs_recovery",
            "phase", "wake_generation", "updated_at",
        ),
        Index(
            "ix_task_coordinator_runs_terminal_cleanup",
            "phase", "terminal_cleanup_at", "updated_at",
        ),
        CheckConstraint("state_version >= 0", name="ck_task_coordinator_runs_version"),
        CheckConstraint(
            "(claim_token IS NULL AND claim_generation IS NULL AND claim_expires_at IS NULL) "
            "OR (claim_token IS NOT NULL AND claim_generation IS NOT NULL "
            "AND claim_expires_at IS NOT NULL)",
            name="ck_task_coordinator_runs_claim",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    task_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # Frozen factory identity.  A replacement Workspace subscription must
    # never silently take over an existing coordinator checkpoint.
    root_subscription_id: Mapped[str] = mapped_column(String(26), nullable=False)
    root_agent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    selection_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    # Chosen once from the task_agent_loop flag when the row is first created;
    # a flag flip never moves an in-flight run to another engine.
    engine: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TaskCoordinatorEngine.PLAN_DAG.value,
        server_default=TaskCoordinatorEngine.PLAN_DAG.value,
    )

    phase: Mapped[str] = mapped_column(
        String(24), nullable=False, default=TaskCoordinatorPhase.READY.value,
        server_default=TaskCoordinatorPhase.READY.value,
    )
    current_plan_id: Mapped[Optional[str]] = mapped_column(String(26))
    checkpoint: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # A newer wake does not steal a live claim.  Its opaque generation is
    # separate from claim_generation so the old owner cannot ACK the new wake.
    wake_generation: Mapped[Optional[str]] = mapped_column(String(26))
    claim_token: Mapped[Optional[str]] = mapped_column(String(64))
    claim_generation: Mapped[Optional[str]] = mapped_column(String(26))
    claim_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Post-commit HITL cleanup cannot share the Task -> Plan finalization
    # transaction without reversing the approval lock order. Recovery retries
    # terminal cleanup until this marker is durably written.
    terminal_cleanup_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
