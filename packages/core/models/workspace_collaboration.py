"""Explicit external Workspace collaboration, separate from company membership."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class WorkspaceCollaboration(Base, TimestampMixin):
    __tablename__ = "workspace_collaborations"
    __table_args__ = (Index("ix_workspace_collaboration_scope", "entity_id", "workspace_id"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26))
    workspace_id: Mapped[str] = mapped_column(String(26))
    delivery_id: Mapped[str | None] = mapped_column(String(26))
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    requested_by: Mapped[str | None] = mapped_column(String(26))
    authorized_by: Mapped[str | None] = mapped_column(String(26))
    user_id: Mapped[str | None] = mapped_column(String(26))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    share_configuration: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    authority_fingerprint: Mapped[str | None] = mapped_column(String(64))


class WorkspaceCollaborationEntry(Base, TimestampMixin):
    __tablename__ = "workspace_collaboration_entries"
    __table_args__ = (
        Index("ix_workspace_collaboration_entry_scope", "workspace_id", "collaboration_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26))
    workspace_id: Mapped[str] = mapped_column(String(26))
    collaboration_id: Mapped[str] = mapped_column(String(26))
    created_by: Mapped[str] = mapped_column(String(26))
    kind: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    reviewed_by: Mapped[str | None] = mapped_column(String(26))
    reply: Mapped[str | None] = mapped_column(String)
    draft_id: Mapped[str | None] = mapped_column(String(26))
    run_id: Mapped[str | None] = mapped_column(String(26))
