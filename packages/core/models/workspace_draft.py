"""WorkspaceDraft — a conversation-driven, pre-creation draft of a workspace.

The user chats with an LLM that progressively fills in the workspace's
operating model. The session persists across turns so the user can close
the tab and resume. On finalize, the draft is converted to a real Workspace
row + memory dirs + agent subscriptions via workspace_setup_service.finalize_setup.

Lifecycle:
  active     -- conversation in progress, fields incomplete
  ready      -- all required fields collected, awaiting user confirmation
  finalized  -- materialized into a Workspace; finalized_workspace_id is set
  abandoned  -- user gave up; soft-removed
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class WorkspaceDraft(Base, TimestampMixin):
    """A pre-creation, conversational workspace draft."""
    __tablename__ = "workspace_drafts"
    __table_args__ = (
        Index("ix_workspace_drafts_entity_status", "entity_id", "status"),
        Index("ix_workspace_drafts_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(26))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    # Conversation state — mirrors WorkspaceSetupSession dataclass.
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    messages: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    missing: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    ready: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")

    # Optional blueprint hint — the LLM may match the user's intent against
    # a published marketplace blueprint and surface it as a suggestion.
    suggested_blueprint_id: Mapped[Optional[str]] = mapped_column(String(26))
    applied_blueprint_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Set when status='finalized' — the workspace this draft became.
    finalized_workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
