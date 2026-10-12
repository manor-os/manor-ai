"""On-demand configuration baselines for in-place Workspace upgrades.

These receipts are local lifecycle state, never portable Blueprint content.
Keeping them outside Workspace.settings avoids loading full definitions on
every Workspace list/chat request.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class WorkspaceBlueprintUpgradeState(Base, TimestampMixin):
    __tablename__ = "workspace_blueprint_upgrade_states"

    workspace_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    baseline: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    restore_point: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
