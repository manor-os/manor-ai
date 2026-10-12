"""Durable Instagram collection quotes and provider settlement receipts."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class ApifyCollection(Base, TimestampMixin):
    __tablename__ = "apify_collections"
    __table_args__ = (
        Index("ix_apify_collections_owner", "entity_id", "user_id", "workspace_id"),
        Index("ix_apify_collections_pending", "billing_status", "next_check_at"),
        Index("ux_apify_collections_provider_run", "provider_run_id", unique=True),
    )

    # The quote id is also the permanent start idempotency key.
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(26))
    agent_id: Mapped[str | None] = mapped_column(String(26))
    conversation_id: Mapped[str | None] = mapped_column(String(26))
    credential_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(100))
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    pricing: Mapped[dict] = mapped_column(JSONB, nullable=False)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 9), nullable=False)
    max_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="quoted")
    billing_status: Mapped[str] = mapped_column(String(30), nullable=False, default="not_started")
    provider_run_id: Mapped[str | None] = mapped_column(String(100))
    provider_status: Mapped[str | None] = mapped_column(String(30))
    dataset_id: Mapped[str | None] = mapped_column(String(100))
    result_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    provider_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 9))
    charged_credits: Mapped[int | None] = mapped_column(Integer)
    uncovered_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 9))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(26))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
