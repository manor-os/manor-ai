"""Durable cloud Runtime execution and Sandbox coordination state."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class RuntimeRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_RESOURCE = "waiting_resource"
    RESUMING = "resuming"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.CANCELLED.value, cls.COMPLETED.value, cls.FAILED.value})

    @classmethod
    def active_root(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls if member.value not in cls.terminal())


class SandboxReservationStatus(str, Enum):
    PENDING_CHECKPOINT = "pending_checkpoint"
    QUEUED = "queued"
    ALLOCATING = "allocating"
    ALLOCATED = "allocated"
    CONSUMED = "consumed"
    REQUEUED = "requeued"
    RELEASE_PENDING = "release_pending"
    RELEASED = "released"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset(
            {
                cls.RELEASED.value,
                cls.CANCELLED.value,
                cls.EXPIRED.value,
                cls.FAILED.value,
            }
        )


class SandboxRunnerStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DRAINING = "draining"


class RuntimeRun(Base, TimestampMixin):
    """One durable root or delegated Runtime execution."""

    __tablename__ = "runtime_runs"
    __table_args__ = (
        Index("ix_runtime_runs_root_status", "root_run_id", "status"),
        Index("ix_runtime_runs_entity_status", "entity_id", "status"),
        Index("ix_runtime_runs_conversation_created", "conversation_id", "created_at"),
        Index(
            "uq_runtime_runs_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(
                "parent_run_id IS NULL AND status IN "
                "('queued','running','waiting_resource','resuming',"
                "'cancel_requested','cancelling')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    root_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    assistant_message_id: Mapped[Optional[str]] = mapped_column(String(26))
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RuntimeRunStatus.QUEUED.value
    )
    status_reason: Mapped[Optional[str]] = mapped_column(String(80))
    checkpoint: Mapped[Optional[dict]] = mapped_column(JSONB)
    execution_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[Optional[dict]] = mapped_column(JSONB)
    error: Mapped[Optional[dict]] = mapped_column(JSONB)
    reservation_id: Mapped[Optional[str]] = mapped_column(String(26))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    lease_owner: Mapped[Optional[str]] = mapped_column(String(120))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    active_sandbox_id: Mapped[Optional[str]] = mapped_column(String(255))
    active_execution_id: Mapped[Optional[str]] = mapped_column(String(128))
    cancel_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class SandboxRunner(Base, TimestampMixin):
    """Configured private Sandbox runner and its latest observed capacity."""

    __tablename__ = "sandbox_runners"
    __table_args__ = (
        Index("uq_sandbox_runners_base_url", "base_url", unique=True),
        Index("ix_sandbox_runners_status", "status", "last_health_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SandboxRunnerStatus.UNKNOWN.value
    )
    active_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    executing_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    executing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_health_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class SandboxReservation(Base, TimestampMixin):
    """Durable fair-queue ticket for one suspended Runtime tool call."""

    __tablename__ = "sandbox_reservations"
    __table_args__ = (
        UniqueConstraint(
            "runtime_run_id",
            "tool_call_id",
            name="uq_sandbox_reservations_run_tool",
        ),
        Index(
            "ix_sandbox_reservations_queue",
            "priority",
            "enqueued_at",
            "id",
            postgresql_where=text("status IN ('queued','requeued')"),
        ),
        Index("ix_sandbox_reservations_root_status", "root_run_id", "status"),
        Index(
            "uq_sandbox_reservations_sandbox",
            "sandbox_id",
            unique=True,
            postgresql_where=text("sandbox_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    runtime_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    root_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=SandboxReservationStatus.QUEUED.value
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone()
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    runner_id: Mapped[Optional[str]] = mapped_column(String(80))
    sandbox_id: Mapped[Optional[str]] = mapped_column(String(255))
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    allocated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class SandboxInstance(Base, TimestampMixin):
    """Authoritative stateful routing for an allocated Sandbox."""

    __tablename__ = "sandbox_instances"
    __table_args__ = (
        Index("ix_sandbox_instances_root_status", "root_run_id", "status"),
        Index("ix_sandbox_instances_runner_status", "runner_id", "status"),
    )

    sandbox_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(26), nullable=False, unique=True)
    runtime_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    root_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    runner_id: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class RuntimeOutboxEvent(Base):
    """Transactional task delivery written beside run/reservation state."""

    __tablename__ = "runtime_outbox_events"
    __table_args__ = (
        Index(
            "ix_runtime_outbox_pending",
            "available_at",
            "created_at",
            postgresql_where=text("delivered_at IS NULL"),
        ),
        UniqueConstraint("dedupe_key", name="uq_runtime_outbox_dedupe_key"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone()
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone()
    )
