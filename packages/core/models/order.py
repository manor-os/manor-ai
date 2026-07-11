"""Business order / commerce models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class BusinessOrder(Base, TimestampMixin):
    """A business/commerce order (service, product, or subscription)."""
    __tablename__ = "business_orders"
    __table_args__ = (
        Index("ix_business_orders_entity_status", "entity_id", "status"),
        Index("ix_business_orders_client", "client_id"),
        Index("ix_business_orders_number", "order_number", unique=True),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    order_number: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    client_id: Mapped[Optional[str]] = mapped_column(String(26))
    assignee_id: Mapped[Optional[str]] = mapped_column(String(26))
    creator_id: Mapped[Optional[str]] = mapped_column(String(26))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # status: pending, confirmed, in_progress, completed, cancelled, refunded
    order_type: Mapped[str] = mapped_column(String(30), nullable=False, default="service")
    # order_type: service, product, subscription

    amount: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default="USD")
    paid_amount: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    payment_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unpaid")
    # payment_status: unpaid, partial, paid, refunded

    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    notes: Mapped[Optional[str]] = mapped_column(Text)

    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class BusinessOrderItem(Base):
    """Line item within a business order."""
    __tablename__ = "business_order_items"
    __table_args__ = (
        Index("ix_business_order_items_order", "order_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    order_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    total_price: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
