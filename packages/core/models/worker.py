"""Worker / Lease / Activity / Sublease models — M3 dispatch layer.

These mirror the v2 design. All five tables were added together in the
``20260424_03_workers_and_leases`` Alembic revision. Nothing references
them at import time outside ``packages.core.workers`` and
``packages.core.dispatcher``, so existing flows are unaffected until
the executor is opted into the dispatcher.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


# ── workers ───────────────────────────────────────────────────────────

class Worker(Base, TimestampMixin):
    """An executor that can take leases (internal asyncio task or
    external HTTP heartbeat client)."""

    __tablename__ = "workers"
    __table_args__ = (
        Index("ix_workers_entity_status", "entity_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # 'internal' | 'claude_code' | 'openclaw' | 'paperclip_bridge'
    # | 'custom_http' | 'shell_script' | 'mcp_reverse'

    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[Optional[str]] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)

    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # See migration docstring for the canonical shape.

    secret_hash: Mapped[Optional[str]] = mapped_column(String(255))
    trust_level: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    allowed_ips: Mapped[Optional[list]] = mapped_column(JSONB)

    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    monthly_budget_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    monthly_spent_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0",
    )
    budget_reset_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    auto_pause_on_budget: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_seen_ip: Mapped[Optional[str]] = mapped_column(String(45))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    last_rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ── subscription_workers ──────────────────────────────────────────────

class SubscriptionWorker(Base):
    """M:N AgentSubscription ↔ Worker. Each subscription has at least
    one worker bound (the entity's internal worker by default)."""

    __tablename__ = "subscription_workers"
    __table_args__ = (
        PrimaryKeyConstraint("subscription_id", "worker_id"),
        Index("ix_sub_workers_worker", "worker_id"),
    )

    subscription_id: Mapped[str] = mapped_column(String(26), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(26), nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    is_preferred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ── work_leases ───────────────────────────────────────────────────────

class WorkLease(Base, TimestampMixin):
    """Per-step atomic dispatch record. Issued by the Dispatcher when
    a worker is matched to a runnable step."""

    __tablename__ = "work_leases"
    __table_args__ = (
        Index("ix_leases_worker_status", "worker_id", "status"),
        Index("ix_leases_expiry_scan", "status", "lease_until"),
        Index("ix_leases_step_status", "step_id", "status"),
        # ``uq_one_active_lease_per_step`` is a partial unique index
        # created in the migration — SQLAlchemy can't declare WHERE
        # clauses on UniqueConstraint cleanly, so we leave it
        # migration-only. It still enforces at the DB level.
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    step_id: Mapped[str] = mapped_column(String(26), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    worker_id: Mapped[str] = mapped_column(String(26), nullable=False)
    subscription_id: Mapped[Optional[str]] = mapped_column(String(26))

    leased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    lease_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    extended_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # 'active' | 'completed' | 'failed' | 'expired' | 'released' | 'needs_human'

    budget_limit_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    budget_spent_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default="0",
    )

    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress: Mapped[Optional[float]] = mapped_column(Float)

    result: Mapped[Optional[dict]] = mapped_column(JSONB)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    cost: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[Optional[dict]] = mapped_column(JSONB)

    credential_leases: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")


# ── worker_activity_log ───────────────────────────────────────────────

class WorkerActivityLog(Base):
    """Append-only audit of every worker / lease lifecycle event."""

    __tablename__ = "worker_activity_log"
    __table_args__ = (
        Index("ix_worker_activity_recent", "worker_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(26))
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_id: Mapped[Optional[str]] = mapped_column(String(26))
    ip: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))
    payload_summary: Mapped[Optional[dict]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ── credential_subleases ──────────────────────────────────────────────

class CredentialSublease(Base):
    """Index of short-lived credential leases tied to a WorkLease.

    The plaintext credential never lives here — only the metadata the
    Dispatcher needs to revoke at the Vault when the lease completes
    or expires."""

    __tablename__ = "credential_subleases"
    __table_args__ = (
        Index("ix_csub_lease", "work_lease_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    work_lease_id: Mapped[str] = mapped_column(String(26), nullable=False)
    integration_id: Mapped[str] = mapped_column(String(26), nullable=False)
    vault_lease_id: Mapped[Optional[str]] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
