"""Append-only audit of execution-config revisions (M11).

One row per ``bump_revision`` call: which row (``target_kind`` +
``target_id``) moved to which ``revision``, the field-level ``patch``
that motivated the bump, and who caused it. scheduled_jobs /
workflow_bindings / goals share this table — ``target_kind``
discriminates. No DB-level FKs, matching repo convention.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class AutomationRevision(Base):
    __tablename__ = "automation_revisions"
    __table_args__ = (
        Index(
            "uq_automation_revisions_target_rev",
            "target_kind", "target_id", "revision",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    # scheduled_job | workflow_binding | goal
    target_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[str] = mapped_column(String(26), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    # Field-level change summary ({field: new_value}), never a full dump.
    patch: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # user | agent | system
    changed_by_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    changed_by_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # e.g. the proposal_item id whose approval caused the change.
    causation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
