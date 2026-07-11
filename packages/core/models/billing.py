"""Billing models — subscription plans, payment logs, and orders."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class SubscriptionPlan(Base, TimestampMixin):
    """Billing plan definition with token/credit limits and pricing."""
    __tablename__ = "subscription_plans"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_type: Mapped[str] = mapped_column(String(50), nullable=False, server_default="standard")
    # plan_type: free, starter, standard, enterprise

    # Token/credit limits
    max_tokens: Mapped[int] = mapped_column(BigInteger, server_default="2000000")
    max_credit: Mapped[int] = mapped_column(Integer, server_default="0")
    credit_amount: Mapped[int] = mapped_column(Integer, server_default="100")
    # Conversion rates: how many tokens per credit
    token_per_credit: Mapped[int] = mapped_column(Integer, server_default="20000")
    input_token_per_credit: Mapped[int] = mapped_column(Integer, server_default="40000")
    output_token_per_credit: Mapped[int] = mapped_column(Integer, server_default="10000")

    # Pricing (in cents)
    price_monthly: Mapped[int] = mapped_column(Integer, server_default="0")
    price_yearly: Mapped[int] = mapped_column(Integer, server_default="0")
    # price stored as BigDecimal equivalent for Stripe calculations
    price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))

    features: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    # status: active, archived


class CreditUsageLog(Base):
    """Per-request credit/token usage log, mirrors Java ClientTokensLog.

    Extends the existing TokenUsageLog with credit-based billing fields
    that align with the Java credit system (input/output token rates, direct credits).
    """
    __tablename__ = "credit_usage_logs"
    __table_args__ = (
        Index("ix_credit_usage_entity", "entity_id", "created_at"),
        Index("ix_credit_usage_agent", "entity_id", "agent_id"),
        Index("ix_credit_usage_workspace", "entity_id", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    operation_id: Mapped[Optional[str]] = mapped_column(String(100))

    # Raw token counts
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    total_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")

    # Credit-based billing (derived from token counts + plan rates)
    input_credit: Mapped[int] = mapped_column(Integer, server_default="0")
    output_credit: Mapped[int] = mapped_column(Integer, server_default="0")
    direct_credit: Mapped[int] = mapped_column(Integer, server_default="0")
    total_credit: Mapped[int] = mapped_column(Integer, server_default="0")

    model: Mapped[Optional[str]] = mapped_column(String(100))
    cost_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 6))
    business_type: Mapped[Optional[str]] = mapped_column(String(50))
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CreditUsageAllocation(Base):
    """Per-grant credit consumption trace for a usage log."""
    __tablename__ = "credit_usage_allocations"
    __table_args__ = (
        Index("ix_credit_usage_allocations_entity", "entity_id", "created_at"),
        Index("ix_credit_usage_allocations_usage", "usage_log_id", "priority"),
        Index("ix_credit_usage_allocations_grant", "grant_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    usage_log_id: Mapped[str] = mapped_column(String(26), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(26), nullable=False)
    grant_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaymentLog(Base, TimestampMixin):
    """Stripe payment records — tracks every charge and refund."""
    __tablename__ = "payment_logs"
    __table_args__ = (
        Index("ix_payment_logs_entity", "entity_id"),
        Index("ix_payment_logs_stripe_pi", "stripe_payment_intent_id"),
        Index(
            "ux_payment_logs_entity_stripe_pi",
            "entity_id",
            "stripe_payment_intent_id",
            unique=True,
            postgresql_where=text("stripe_payment_intent_id IS NOT NULL"),
        ),
        Index(
            "ux_payment_logs_entity_checkout_session",
            "entity_id",
            text("(metadata->>'checkoutSessionId')"),
            unique=True,
            postgresql_where=text("metadata ? 'checkoutSessionId'"),
        ),
        Index(
            "ux_payment_logs_entity_invoice",
            "entity_id",
            text("(metadata->>'invoiceId')"),
            unique=True,
            postgresql_where=text("metadata ? 'invoiceId'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # in cents
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default="usd")
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    # status: pending, succeeded, failed, refunded
    event_type: Mapped[Optional[str]] = mapped_column(String(100))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    credit_awarded: Mapped[int] = mapped_column(Integer, server_default="0")
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")


class CreditReservation(Base, TimestampMixin):
    """Credits reserved for asynchronous work before actual usage is known."""
    __tablename__ = "credit_reservations"
    __table_args__ = (
        Index("ix_credit_reservations_entity_status", "entity_id", "status", "expires_at"),
        Index("ix_credit_reservations_source", "source_kind", "source_id"),
        Index(
            "ux_credit_reservations_active_source",
            "source_kind",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))

    amount_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    reason: Mapped[Optional[str]] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Order(Base, TimestampMixin):
    """Agent purchase or subscription order."""
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    order_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # order_type: subscription, one_time, recharge, blueprint
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    plan_id: Mapped[Optional[str]] = mapped_column(String(26))
    blueprint_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # in cents
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default="usd")
    stripe_session_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    # status: pending, paid, cancelled, refunded, expired
