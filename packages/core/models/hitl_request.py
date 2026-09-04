"""HitlRequest — the single source of truth for "does this need a human?".

Replaces the pre-existing sprawl where an approval decision was scattered across
five stores that did not interoperate (conversation-meta blob, user-preferences
key, ``step.params._governance_approval``, workspace-policy auto-approve lists,
and the ``message.pending_action`` column), enforced by two disjoint planes
(the runtime tool guard and the dispatcher step gate) that never read each
other's grant. A grant made in one plane could not satisfy a gate blocking in
the other — the root cause of the create-automation loop (#289), the publish
Resume loop (#317), the duplicate-card badge inflation, and the orphaned
"no longer attached to a waiting step" cards.

One object now carries the whole lifecycle:

  * ``subject`` — WHAT needs approval, plane-agnostic (action_key, capability_id,
    resource, risk). Two gates for the same underlying action resolve to the
    same subject, so approving once satisfies both.
  * ``origin`` — WHERE it is blocking (conversation / step / channel), so the
    right surface can render and resume it, and so it can be auto-resolved when
    that origin reaches a terminal state (no orphans).
  * ``status`` — pending → granted | denied | expired → consumed, owned here
    rather than smeared across stores.
  * ``dedup_key`` — derived from subject + origin; a partial unique index keeps
    at most one OPEN request per key, so a re-tripped gate reuses the existing
    card instead of minting a duplicate.

The record started life as ``ApprovalRequest``/``approval_requests``. It now
carries a ``hitl_type`` — a CAPTCHA prompt, a connectivity error with a fix
link, and a permission request all live here — so "approval" was a misnomer
for four of the five types; class and table were renamed to match (migration
``20260802_02``). The *lifecycle* vocabulary (``ApprovalStatus``,
``APPROVAL_*_STATUSES``) and the authorization-decision API
(``resolve_approval`` / ``grant_approval`` / …) deliberately keep their
names: they describe approval-style resolution, which is shared across every
type and is a genuinely different concept from the record itself.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


# Lifecycle states live in constants.approvals as ``ApprovalStatus``; they
# are re-exported here because this model is where readers look for them.
from packages.core.constants.approvals import (  # noqa: E402
    APPROVAL_OPEN_STATUSES,
    APPROVAL_TERMINAL_STATUSES,
    GOVERNANCE_HITL_TYPES,
    NON_GOVERNANCE_HITL_TYPES,
    ApprovalOriginKind,
    ApprovalStatus,
    HitlType,
    is_governance_hitl,
)

__all__ = [
    "APPROVAL_OPEN_STATUSES",
    "APPROVAL_TERMINAL_STATUSES",
    "GOVERNANCE_HITL_TYPES",
    "NON_GOVERNANCE_HITL_TYPES",
    "ApprovalOriginKind",
    "ApprovalStatus",
    "HitlRequest",
    "HitlType",
    "governance_hitl_clause",
    "is_governance_hitl",
]


class HitlRequest(Base, TimestampMixin):
    __tablename__ = "hitl_requests"
    __table_args__ = (
        # At most one OPEN request per (entity, dedup_key). The partial index
        # (status='pending') is created in the migration — SQLAlchemy models the
        # plain columns; the dedup guarantee lives in the DB index.
        Index("ix_hitl_requests_entity_status", "entity_id", "status"),
        Index("ix_hitl_requests_workspace_status", "workspace_id", "status"),
        Index("ix_hitl_requests_origin_step", "origin_step_id"),
        Index("ix_hitl_requests_conversation", "origin_conversation_id"),
        # The at-most-one-OPEN-request-per-key guarantee is a PARTIAL unique
        # index (WHERE status='pending'), created in the migration — a plain
        # UniqueConstraint would wrongly reject multiple terminal rows per key.
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)

    # The migration (20260721_01) builds updated_at as NOT NULL DEFAULT now().
    # TimestampMixin.updated_at is nullable with no insert default, so the ORM
    # emits an explicit NULL on INSERT — which tripped the NOT NULL constraint
    # and broke every approval-request creation (e.g. platform-announcement
    # publish) against the migration-built schema. Declaring server_default here
    # makes SQLAlchemy omit the column on INSERT so the DB default populates it,
    # and makes create_all match the migration. Mirrors the affiliate models'
    # fix for the identical drift.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    # ── subject: WHAT ──────────────────────────────────────────────
    action_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    capability_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    resource_kind: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")

    # ── origin: WHERE it is blocking ───────────────────────────────
    #: An ``ApprovalOriginKind`` value. Left a plain ``String(30)`` — the
    #: vocabulary is enforced by the enum at the record layer, not by the
    #: column, so no migration is needed when a new origin appears.
    origin_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    origin_conversation_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    origin_message_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    origin_step_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    origin_plan_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    origin_task_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)

    # ── lifecycle ──────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False)

    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # why approval is needed
    matched_rule: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    decided_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_via: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # chat_card | step_resume | always | ...
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_reason: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # why it left pending (approved / origin_terminal / superseded)

    # audit/render payload (args preview, prompt, plane, etc.) — never load-bearing
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    hitl_type: Mapped[str] = mapped_column(
        String(20), nullable=False,
        server_default=HitlType.AUTHORIZE.value,
        default=HitlType.AUTHORIZE.value,
    )
    """What kind of human involvement this is. Backfills to ``authorize``
    because every row that existed before this column was, semantically,
    an authorization request."""

    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    """Type-specific fields. Required keys per type live in
    ``HITL_REQUIRED_PAYLOAD_FIELDS`` and are validated at mint time."""

    def is_open(self) -> bool:
        return self.status in APPROVAL_OPEN_STATUSES

    def is_governance(self) -> bool:
        """Is a person granting permission here, or supplying information?

        The row's own answer to the question every read surface asks. See
        ``constants.approvals.is_governance_hitl``.
        """
        return is_governance_hitl(self.hitl_type)


def governance_hitl_clause():
    """SQL twin of :func:`is_governance_hitl`, for queries that must filter
    BEFORE a LIMIT (the strategist briefing takes the oldest 10 — filtering
    those 10 in Python would silently shrink the list).

    Written as "not in the non-governance set" rather than "in the governance
    set" so both spellings agree on an unrecognized type: governance, i.e. the
    pre-existing behavior. ``tests/test_hitl_read_surfaces.py`` asserts they
    agree for every ``HitlType`` member — that guard is what keeps this from
    becoming a second, drifting definition.
    """
    return HitlRequest.hitl_type.notin_(tuple(sorted(NON_GOVERNANCE_HITL_TYPES)))
