"""Scheduled jobs, job runs, and agent execution models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.constants.execution import DEFAULT_AGENT_MAX_TURNS

from .base import Base, TimestampMixin, generate_ulid


class ScheduledJob(Base, TimestampMixin):
    """A scheduled job definition."""
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    job_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    name: Mapped[Optional[str]] = mapped_column(String(255))
    job_type: Mapped[str] = mapped_column(String(50), default="cron")
    schedule_kind: Mapped[Optional[str]] = mapped_column(String(20))
    cron_expr: Mapped[Optional[str]] = mapped_column(String(100))
    every_seconds: Mapped[Optional[float]] = mapped_column(Float)
    run_at: Mapped[Optional[str]] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    payload_message: Mapped[Optional[str]] = mapped_column(Text)
    agent_id: Mapped[Optional[str]] = mapped_column(String(100))
    execution_type: Mapped[Optional[str]] = mapped_column(String(50))
    execution_target: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    execution_script: Mapped[Optional[str]] = mapped_column(Text)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(100))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    default_delivery_mode: Mapped[Optional[str]] = mapped_column(String(20))
    goal_id: Mapped[Optional[str]] = mapped_column(String(100))
    goal_step_id: Mapped[Optional[str]] = mapped_column(String(100))
    manor_task_id: Mapped[Optional[str]] = mapped_column(String(26))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delete_after_run: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[Optional[str]] = mapped_column(String(20))
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)


class ScheduledJobRun(Base):
    """Append-only record of a single job execution."""
    __tablename__ = "scheduled_job_runs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    job_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_type: Mapped[Optional[str]] = mapped_column(String(20))
    result: Mapped[Optional[dict]] = mapped_column(JSONB)
    error: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentExecution(Base):
    """Record of an autonomous agent execution run."""
    __tablename__ = "agent_executions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(100))
    task_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    turns_used: Mapped[int] = mapped_column(Integer, default=0)
    max_turns: Mapped[int] = mapped_column(Integer, default=DEFAULT_AGENT_MAX_TURNS)
    supervisor_verdict: Mapped[Optional[str]] = mapped_column(String(20))
    input_message: Mapped[Optional[str]] = mapped_column(Text)
    output_message: Mapped[Optional[str]] = mapped_column(Text)
    tools_used: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    token_usage: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
