"""Workflow definition and run models for the agent workflow engine."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class WorkflowDefinition(Base, TimestampMixin):
    """A reusable workflow template with ordered steps."""
    __tablename__ = "workflow_definitions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    trigger_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # steps schema:
    # [
    #   {"id": "step1", "type": "agent", "name": "Research", "config": {"skill": "research_topic"}, "next": ["step2"]},
    #   {"id": "step2", "type": "condition", "name": "Check quality", "config": {"expression": "score > 0.7"}, "true_next": ["step3"], "false_next": ["step1"]},
    #   {"id": "step3", "type": "tool", "name": "Send email", "config": {"tool": "send_email"}, "next": []},
    # ]
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    category: Mapped[Optional[str]] = mapped_column(String(50))
    tags: Mapped[list] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class WorkflowRun(Base, TimestampMixin):
    """A single execution of a workflow."""
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workflow_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    current_step_id: Mapped[Optional[str]] = mapped_column(String(100))
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    step_results: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    trigger_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_by: Mapped[Optional[str]] = mapped_column(String(26))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
