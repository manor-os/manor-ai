"""Blueprint purchase — entitlement + receipt for a paid marketplace install.

Each ``BlueprintCheckoutAttempt`` freezes the commercial terms and delivery
snapshot for one Stripe Checkout Session. The purchase row projects the paid
attempt after completion. Published installs and upgrades still follow the
live, versioned Blueprint row. One live (non-refunded) entitlement
per (blueprint, buyer entity): the partial unique index also blocks a second
purchase while a 'pending' row exists — intentional, the checkout service
layer must reuse/complete the pending row rather than inserting a new one.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class BlueprintPurchase(Base, TimestampMixin):
    __tablename__ = "blueprint_purchases"
    __table_args__ = (
        Index("ix_blueprint_purchases_blueprint", "blueprint_id"),
        Index("ix_blueprint_purchases_buyer", "buyer_entity_id"),
        Index("ix_blueprint_purchases_seller", "seller_entity_id"),
        Index(
            "ix_blueprint_purchases_seller_recovery",
            "seller_entity_id",
            "seller_recovery_attempted_at",
        ),
        Index(
            "ix_blueprint_purchases_seller_recovery_due",
            "seller_entity_id",
            "seller_recovery_next_attempt_at",
            "seller_recovery_claim_expires_at",
        ),
        Index(
            "ix_blueprint_purchases_allocation_recovery",
            "seller_entity_id",
            "allocation_recovery_attempted_at",
        ),
        Index(
            "ix_blueprint_purchases_allocation_recovery_due",
            "seller_entity_id",
            "allocation_recovery_next_attempt_at",
            "allocation_recovery_claim_expires_at",
        ),
        Index("ix_blueprint_purchases_payment_intent", "stripe_payment_intent_id"),
        Index(
            "ux_blueprint_purchases_checkout_session",
            "stripe_checkout_session_id",
            unique=True,
            postgresql_where=text("stripe_checkout_session_id IS NOT NULL"),
        ),
        Index(
            "ux_blueprint_purchases_live_entitlement",
            "blueprint_id", "buyer_entity_id",
            unique=True,
            postgresql_where=text("status != 'refunded'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    blueprint_id: Mapped[str] = mapped_column(String(26), nullable=False)
    buyer_entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    buyer_user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # Financial ownership is frozen at Checkout. Never infer the payee from
    # the mutable/deletable Blueprint row when rendering a seller ledger.
    seller_entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    seller_recovery_attempted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    seller_recovery_next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    seller_recovery_claim_token: Mapped[Optional[str]] = mapped_column(String(64))
    seller_recovery_claim_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    seller_recovery_retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        default=0,
    )
    seller_recovery_last_error: Mapped[Optional[str]] = mapped_column(Text)
    allocation_recovery_attempted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    allocation_recovery_next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    allocation_recovery_claim_token: Mapped[Optional[str]] = mapped_column(String(64))
    allocation_recovery_claim_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    allocation_recovery_retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        default=0,
    )
    allocation_recovery_last_error: Mapped[Optional[str]] = mapped_column(Text)
    order_id: Mapped[Optional[str]] = mapped_column(String(26))

    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default="usd")
    platform_fee_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    seller_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stripe reports three independent cumulative refund ledgers. A buyer
    # refund does not necessarily reverse the destination transfer or refund
    # the platform application fee, so never derive either from the buyer
    # refund amount.
    refunded_amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0,
    )
    # Nullable means Stripe has not yet confirmed the historical/current
    # Connect allocation. New purchases start at a known zero; migrated rows
    # stay unknown until the seller ledger reconciles them against Stripe.
    transfer_reversed_amount_cents: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, default=0,
    )
    platform_fee_refunded_amount_cents: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, default=0,
    )

    stripe_checkout_session_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_destination_account_id: Mapped[Optional[str]] = mapped_column(String(255))

    payload_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Nullable only for historical purchases whose original version cannot be
    # reconstructed. Every new checkout sets this together with the payload.
    blueprint_content_version: Mapped[Optional[str]] = mapped_column(String(20))
    blueprint_title: Mapped[str] = mapped_column(String(200), nullable=False)

    # BlueprintPurchaseStatus
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", default="pending",
    )
    purchased_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Durable Stripe dispute evidence. A disputed purchase keeps its row and
    # blocks a second checkout, but does not grant install/upgrade entitlement.
    stripe_dispute_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_dispute_status: Mapped[Optional[str]] = mapped_column(String(40))
    disputed_amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0,
    )
    dispute_funds_reinstated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    last_dispute_event_id: Mapped[Optional[str]] = mapped_column(String(255))
    last_dispute_event_created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )


class BlueprintCheckoutAttempt(Base, TimestampMixin):
    """Immutable price, fee, version, and payload for one Checkout Session."""

    __tablename__ = "blueprint_checkout_attempts"
    __table_args__ = (
        Index("ix_blueprint_checkout_attempts_purchase", "purchase_id"),
        Index("ix_blueprint_checkout_attempts_blueprint", "blueprint_id"),
        Index("ix_blueprint_checkout_attempts_seller", "seller_entity_id"),
        Index(
            "ux_blueprint_checkout_attempts_session",
            "stripe_checkout_session_id",
            unique=True,
            postgresql_where=text("stripe_checkout_session_id IS NOT NULL"),
        ),
        Index(
            "ux_blueprint_checkout_attempts_payment_intent",
            "stripe_payment_intent_id",
            unique=True,
            postgresql_where=text("stripe_payment_intent_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    purchase_id: Mapped[str] = mapped_column(String(26), nullable=False)
    blueprint_id: Mapped[str] = mapped_column(String(26), nullable=False)
    buyer_entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    seller_entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    stripe_checkout_session_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String(255))
    # Frozen before Stripe I/O so an ambiguous retry can replay the exact
    # provider request without following later routing or APP_URL changes.
    # Nullable only for historical attempts created before these snapshots.
    stripe_destination_account_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_checkout_success_url: Mapped[Optional[str]] = mapped_column(String(1000))
    stripe_checkout_cancel_url: Mapped[Optional[str]] = mapped_column(String(1000))

    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    platform_fee_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    seller_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    blueprint_content_version: Mapped[Optional[str]] = mapped_column(String(20))
    blueprint_title: Mapped[str] = mapped_column(String(200), nullable=False)

    # BlueprintCheckoutAttemptStatus
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", default="pending",
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class BlueprintCheckoutRefund(Base, TimestampMixin):
    """Durable refund job for a paid Checkout session that cannot be used.

    The PaymentIntent is the provider identity and therefore unique. Purchase
    and attempt are nullable because even a signed payment for an already
    deleted/unknown local purchase must still be refunded and audited.
    """

    __tablename__ = "blueprint_checkout_refunds"
    __table_args__ = (
        Index("ix_blueprint_checkout_refunds_purchase", "purchase_id"),
        Index("ix_blueprint_checkout_refunds_attempt", "attempt_id"),
        Index("ix_blueprint_checkout_refunds_buyer", "buyer_entity_id"),
        Index(
            "ix_blueprint_checkout_refunds_session_buyer",
            "stripe_checkout_session_id",
            "buyer_entity_id",
        ),
        Index("ix_blueprint_checkout_refunds_due", "status", "next_attempt_at"),
        Index(
            "ux_blueprint_checkout_refunds_payment_intent",
            "stripe_payment_intent_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    purchase_id: Mapped[Optional[str]] = mapped_column(String(26))
    attempt_id: Mapped[Optional[str]] = mapped_column(String(26))
    blueprint_id: Mapped[Optional[str]] = mapped_column(String(26))
    buyer_entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    seller_entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    stripe_checkout_session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_payment_intent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_refund_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_refund_status: Mapped[Optional[str]] = mapped_column(String(40))
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    platform_fee_cents: Mapped[Optional[int]] = mapped_column(Integer)
    transfer_reversed_amount_cents: Mapped[Optional[int]] = mapped_column(Integer)
    platform_fee_refunded_amount_cents: Mapped[Optional[int]] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", default="pending",
    )
    refund_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0,
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0,
    )
    refund_evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list,
    )
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[Optional[str]] = mapped_column(String(64))
    claim_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    succeeded_event_id: Mapped[Optional[str]] = mapped_column(String(255))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
