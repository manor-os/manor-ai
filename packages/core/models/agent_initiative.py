"""Persistent configuration and runtime records for proactive Agent work."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class AgentInitiative(Base, TimestampMixin):
    """User-owned proactive behavior attached to one Agent deployment."""

    __tablename__ = "agent_initiatives"
    __table_args__ = (
        Index(
            "ix_agent_initiatives_owner_status",
            "entity_id",
            "owner_user_id",
            "status",
        ),
        Index(
            "ix_agent_initiatives_subscription_status",
            "entity_id",
            "agent_subscription_id",
            "status",
        ),
        Index(
            "ix_agent_initiatives_workspace_status",
            "workspace_id",
            "status",
        ),
        Index(
            "ix_agent_initiatives_trigger_status",
            "entity_id",
            "trigger_kind",
            "trigger_source",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_subscription_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey(
            "agent_subscriptions.id",
            name="fk_agent_initiatives_subscription",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(String(26), nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_source: Mapped[Optional[str]] = mapped_column(String(64))
    trigger_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    condition_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    decision_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    delivery_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    interruption_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )


class AgentInitiativeState(Base):
    """High-churn cursor, dedupe, cooldown, and rate-limit state."""

    __tablename__ = "agent_initiative_states"

    initiative_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey(
            "agent_initiatives.id",
            name="fk_agent_initiative_states_initiative",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    window_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notification_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_silent_reason: Mapped[Optional[str]] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )


class AgentInitiativeRun(Base):
    """One admitted, filtered, or executed Initiative occurrence."""

    __tablename__ = "agent_initiative_runs"
    __table_args__ = (
        Index(
            "uq_agent_initiative_runs_occurrence",
            "initiative_id",
            "occurrence_key",
            unique=True,
        ),
        Index(
            "ix_agent_initiative_runs_initiative_created",
            "initiative_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    initiative_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey(
            "agent_initiatives.id",
            name="fk_agent_initiative_runs_initiative",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    scheduled_run_id: Mapped[Optional[str]] = mapped_column(String(26))
    task_id: Mapped[Optional[str]] = mapped_column(String(26))
    occurrence_key: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="admitted", server_default="admitted",
    )
    candidate_decision: Mapped[Optional[dict]] = mapped_column(JSONB)
    final_decision: Mapped[Optional[str]] = mapped_column(String(20))
    reason_code: Mapped[Optional[str]] = mapped_column(String(64))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    message: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class AgentInitiativeDelivery(Base):
    """Idempotent projection of an Initiative decision to one surface."""

    __tablename__ = "agent_initiative_deliveries"
    __table_args__ = (
        Index(
            "uq_agent_initiative_deliveries_run_kind",
            "initiative_run_id",
            "kind",
            unique=True,
        ),
        Index(
            "uq_agent_initiative_deliveries_idempotency",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_agent_initiative_deliveries_due",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_agent_initiative_deliveries_lease",
            "status",
            "locked_until",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    initiative_run_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey(
            "agent_initiative_runs.id",
            name="fk_agent_initiative_deliveries_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending",
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[Optional[str]] = mapped_column(String(26))
    notification_id: Mapped[Optional[str]] = mapped_column(String(26))
    suppressed_reason: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[Optional[str]] = mapped_column(String(26))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


__all__ = [
    "AgentInitiative",
    "AgentInitiativeDelivery",
    "AgentInitiativeRun",
    "AgentInitiativeState",
]
