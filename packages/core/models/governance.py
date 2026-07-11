"""Workspace governance — operator-controlled policies + revision log.

The `policy` JSONB shape is validated by ``packages.core.governance.policy``;
this module is the storage layer only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class GovernancePolicy(Base):
    """Current rules of engagement for a workspace.

    Mutable in place. Every successful change also writes a
    ``GovernanceRevision`` so the operator can answer "what was the
    policy at lease-time?".
    """

    __tablename__ = "governance_policies"

    workspace_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    policy: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1",
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(26))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(),
    )


class GovernanceRevision(Base):
    """Append-only history of policy changes."""

    __tablename__ = "governance_revisions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "revision",
            name="uq_governance_revisions_workspace_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[Optional[str]] = mapped_column(String(500))
    changed_by: Mapped[Optional[str]] = mapped_column(String(26))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
