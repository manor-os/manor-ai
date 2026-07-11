"""Task template model — reusable blueprints for task creation."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Float, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class TaskTemplate(Base, TimestampMixin):
    """A reusable task template with placeholder support."""
    __tablename__ = "task_templates"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    title_template: Mapped[str] = mapped_column(String(500), nullable=False)
    description_template: Mapped[Optional[str]] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(SmallInteger, default=3)
    task_type: Mapped[str] = mapped_column(String(50), default="general")
    category_id: Mapped[Optional[str]] = mapped_column(String(26))
    default_assignee_id: Mapped[Optional[str]] = mapped_column(String(26))
    default_agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_type: Mapped[Optional[str]] = mapped_column(String(50))
    details_template: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    tags: Mapped[list] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence_rule: Mapped[Optional[str]] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    # SLA / automation fields
    sla_policy_id: Mapped[Optional[str]] = mapped_column(String(26))
    estimated_hours: Mapped[Optional[float]] = mapped_column(Float)
    required_skills: Mapped[Optional[list]] = mapped_column(ARRAY(String), server_default="{}")
    steps: Mapped[Optional[dict]] = mapped_column(JSONB, server_default="[]")
