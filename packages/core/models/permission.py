"""Permission-v1 ORM models.

Companions to the alembic migration ``20260506_02_permissions_v1_schema``.
The high-volume audit tables (``permission_audit``, ``document_access_log``)
are intentionally written to via raw SQL from the authz layer rather than
the ORM — they are append-only and do not need ORM mapping for read paths.
The models here cover the resources business code reads/writes:

  * ResourceGrant         — row-level capability grant
  * ResourceGrantPending  — access-request queue item
  * Share                 — unified external share token

See docs/PERMISSIONS_DESIGN_ZH.md for design rationale.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text as text_fn
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class ResourceType:
    """String constants for resource_grants.resource_type."""
    DOCUMENT = "document"
    DOCUMENT_FOLDER = "document_folder"
    DOCUMENT_GROUP = "document_group"
    TASK = "task"
    MEMORY = "memory"
    WORKSPACE = "workspace"
    CONVERSATION = "conversation"
    INTEGRATION = "integration"


class SubjectType:
    """String constants for resource_grants.subject_type."""
    USER = "user"
    STAFF_ROLE = "staff_role"
    WORKSPACE_ROLE = "workspace_role"   # subject_id = "{workspace_id}:{role}"
    TEAM = "team"                       # reserved for future
    ANONYMOUS_LINK = "anonymous_link"   # subject_id = share token id


class Capability:
    """Capability strings used in resource_grants.capabilities[] and shares.capabilities[]."""
    VIEW = "view"
    VIEW_REDACTED = "view_redacted"
    COMMENT = "comment"
    EDIT = "edit"
    UPLOAD_TO = "upload_to"             # folder-only
    MANAGE_METADATA = "manage_metadata"
    SHARE_INTERNAL = "share_internal"
    SHARE_EXTERNAL = "share_external"
    DOWNLOAD = "download"
    PRINT = "print"
    RECLASSIFY = "reclassify"
    DELETE = "delete"
    GRANT_ACCESS = "grant_access"
    LEGAL_HOLD = "legal_hold"


class Visibility:
    PRIVATE = "private"
    WORKSPACE = "workspace"
    ENTITY = "entity"
    PUBLIC = "public"


class Classification:
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    # Ordered for >= comparisons in invariant checks
    LEVELS = ("public", "internal", "confidential", "restricted")

    @classmethod
    def rank(cls, value: str) -> int:
        try:
            return cls.LEVELS.index(value)
        except ValueError:
            return -1


class GrantStatus:
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PendingStatus:
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class ResourceGrant(Base, TimestampMixin):
    """Row-level ACL: subject -> (resource, capabilities)."""
    __tablename__ = "resource_grants"
    __table_args__ = (
        Index("ix_resource_grants_resource", "resource_type", "resource_id"),
        Index("ix_resource_grants_subject", "subject_type", "subject_id"),
        Index("ix_resource_grants_entity_status", "entity_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(26), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default="{}"
    )
    granted_by: Mapped[Optional[str]] = mapped_column(String(26))
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=GrantStatus.ACTIVE
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[Optional[str]] = mapped_column(String(26))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")


class ResourceGrantPending(Base, TimestampMixin):
    """Access-request queue: user requests a grant; owner approves/denies."""
    __tablename__ = "resource_grants_pending"
    __table_args__ = (
        Index(
            "ix_resource_grants_pending_resource",
            "resource_type",
            "resource_id",
        ),
        Index(
            "ix_resource_grants_pending_requester",
            "requester_user_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(26), nullable=False)
    requester_user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    requested_capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False
    )
    reason: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=PendingStatus.PENDING
    )
    decided_by: Mapped[Optional[str]] = mapped_column(String(26))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[Optional[str]] = mapped_column(Text)
    granted_grant_id: Mapped[Optional[str]] = mapped_column(String(26))
    # Free-form config snapshot. Used by share-approval rows
    # (resource_type='share') to remember the original CreateShareRequest
    # so the admin sees exactly what's being requested. Empty for
    # plain access-request rows.
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}",
    )


class Share(Base):
    """Unified external share token. Replaces the various ad-hoc tokens
    (ConversationShare, public_task session codes, etc.) — those will be
    migrated to rows in this table in P6.
    """
    __tablename__ = "shares"
    __table_args__ = (
        Index("ix_shares_resource", "resource_type", "resource_id"),
        Index("ix_shares_entity_status", "entity_id", "status"),
        Index("ix_shares_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(26), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)),
        nullable=False,
        server_default="{view}",
    )
    audience: Mapped[Optional[str]] = mapped_column(String(255))
    require_otp: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    watermark: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    allow_download: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_by: Mapped[str] = mapped_column(String(26), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[Optional[int]] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[Optional[str]] = mapped_column(String(26))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active"
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")


# ── Audit tables (append-only; rarely read from the ORM) ─────────────────
#
# These are listed in the ORM only so test fixtures using ``Base.metadata.
# create_all()`` pick them up. Production code writes through the raw-SQL
# helpers in ``packages.core.auth.authz`` and ``apps/api/routers/
# document_permissions.py`` — going through the ORM here would invite N+1
# loads on a hot path. Reads (admin audit panels, owner self-service) can
# use the ORM models for convenience.


class PermissionAudit(Base):
    """Per-decision audit row for authorize() — see docs/PERMISSIONS_DESIGN_ZH.md §9.

    Append-only. Sampled (deny=always, sensitive-verb allow=always, other
    allow=skipped). Partitioned by month in production via a follow-up
    migration; the base table lives here.
    """
    __tablename__ = "permission_audit"
    __table_args__ = (
        Index("ix_permission_audit_ts", "ts"),
        Index("ix_permission_audit_actor", "actor_type", "actor_id", "ts"),
        Index("ix_permission_audit_resource", "resource_type", "resource_id", "ts"),
        Index("ix_permission_audit_decision", "decision", "ts"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text_fn("now()"),
        nullable=False,
    )
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(40))
    resource_id: Mapped[Optional[str]] = mapped_column(String(26))
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(120))
    request_id: Mapped[Optional[str]] = mapped_column(String(80))
    ip: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSONB, server_default="{}")


class DocumentAccessLog(Base):
    """High-volume read-audit specifically for documents (RFC §13.8).

    Logged actions: view | preview | download | search_hit | rag_retrieve |
    export | share_create | share_use | reclassify.
    Retention: per-classification policy (restricted 7y, confidential 3y,
    internal 1y, public skipped).
    """
    __tablename__ = "document_access_log"
    __table_args__ = (
        Index("ix_doc_access_log_doc_ts", "document_id", "ts"),
        Index("ix_doc_access_log_actor_ts", "actor_type", "actor_id", "ts"),
        Index("ix_doc_access_log_entity_ts", "entity_id", "ts"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text_fn("now()"),
        nullable=False,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    document_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    classification_at_access: Mapped[Optional[str]] = mapped_column(String(20))
    ip: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    share_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_session_id: Mapped[Optional[str]] = mapped_column(String(80))
    redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
    )
    watermark_id: Mapped[Optional[str]] = mapped_column(String(80))
    context: Mapped[dict] = mapped_column(JSONB, server_default="{}")
