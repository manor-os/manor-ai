"""Blueprint purchase — entitlement + receipt for a paid marketplace install.

`payload_snapshot` freezes the blueprint content at purchase time so later
seller edits/repricing/archival never change what the buyer owns (there is
no blueprint versioning until M12.3). One live (non-refunded) entitlement
per (blueprint, buyer entity): the partial unique index also blocks a second
purchase while a 'pending' row exists — intentional, the checkout service
layer must reuse/complete the pending row rather than inserting a new one.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class BlueprintPurchase(Base, TimestampMixin):
    __tablename__ = "blueprint_purchases"
    __table_args__ = (
        Index("ix_blueprint_purchases_blueprint", "blueprint_id"),
        Index("ix_blueprint_purchases_buyer", "buyer_entity_id"),
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
    order_id: Mapped[Optional[str]] = mapped_column(String(26))

    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default="usd")
    platform_fee_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    seller_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    stripe_checkout_session_id: Mapped[Optional[str]] = mapped_column(String(255))
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String(255))

    payload_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    blueprint_title: Mapped[str] = mapped_column(String(200), nullable=False)

    # 'pending' | 'completed' | 'refunded'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", default="pending",
    )
    purchased_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
