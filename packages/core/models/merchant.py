"""Merchant account — Stripe Connect payout identity for a selling entity.

One row per entity. `charges_enabled` / `payouts_enabled` mirror Stripe's
`account.updated` webhook; a blueprint can only be priced > 0 when
`charges_enabled` is true.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class MerchantAccount(Base, TimestampMixin):
    __tablename__ = "merchant_accounts"
    __table_args__ = (
        Index("ux_merchant_accounts_entity", "entity_id", unique=True),
        Index("ux_merchant_accounts_stripe", "stripe_account_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    stripe_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'pending' | 'complete'
    onboarding_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", default="pending",
    )
    charges_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    payouts_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    country: Mapped[Optional[str]] = mapped_column(String(2))
