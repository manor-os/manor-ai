"""Human participation data layer (M9).

Three tables:

* ``participant_profiles`` (M9.1) — a human's declared roles,
  capabilities, authority overrides, availability, and capacity
  preferences within an entity (optionally scoped to one workspace;
  ``workspace_id IS NULL`` = the entity-level default profile).
* ``human_commitments`` (M9.2) — open requests for a human to provide a
  decision / review / input / manual work. Created by the execution
  layer (HITL steps), proposal ``human_request`` items (M10), and
  experiments. HitlRequest rows are deliberately NOT mirrored here
  (the Human Participation consolidator counts the two sources
  separately — no double counting).
* ``human_contributions`` (M9.4) — the record that a human actually
  put work in: edits to AI artifacts/tasks, uploads, choices, manual
  work. ``diff_summary`` is a structured summary (field names, size
  deltas) — never full old/new values (M9.6 privacy boundary).

No DB-level FKs — matching repo convention; referential integrity is
owned by the service layer (``packages.core.humans``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ParticipantProfile(Base):
    """M9.1 — who this human is inside the entity/workspace."""

    __tablename__ = "participant_profiles"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "user_id", "workspace_id",
            name="uq_participant_profiles_entity_user_ws",
        ),
        # Postgres treats NULLs as distinct in unique constraints, so the
        # entity-level default profile (workspace_id IS NULL) needs its own
        # partial unique guard (also created in migration 20260726_01).
        Index(
            "uq_participant_profiles_entity_user_default",
            "entity_id", "user_id",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        Index("ix_participant_profiles_user", "user_id"),
        Index("ix_participant_profiles_workspace", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # NULL = entity-level default profile.
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    # ["workspace_owner", "content_reviewer", ...]
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    declared_capabilities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # {"approve_tasks": true, "approve_external_publish": false, ...}
    # Explicit booleans here override the WorkspaceStaff role default map.
    authority: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {timezone, available_windows[], out_of_office} — M9.6: never enters
    # a general-purpose LLM prompt.
    availability: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {max_open_requests, preferred_notification_channel}
    capacity_preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True,
    )


class HumanCommitment(Base):
    """M9.2 — an open request for a human to unblock or inform the system."""

    __tablename__ = "human_commitments"
    __table_args__ = (
        Index("ix_human_commitments_workspace_status", "workspace_id", "status"),
        Index("ix_human_commitments_participant_status", "participant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # decision | review | input | manual_work | approval_followup
    request_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # Either a specific participant, or any holder of role_required.
    participant_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    role_required: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    # proposal_item | execution_step | chat | experiment
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)

    expected_input: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # waiting | fulfilled | declined | expired | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting")

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    expected_by: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fulfilled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Execution ids (task / plan roots) blocked on this commitment.
    blocking_execution_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class HumanContribution(Base):
    """M9.4 — a human actually contributed work to the system."""

    __tablename__ = "human_contributions"
    __table_args__ = (
        Index("ix_human_contributions_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # upload | edit | choice | info | manual_work | override | rating
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Structured change summary — field names + size deltas only, never
    # full old/new values (M9.6 privacy boundary).
    diff_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
