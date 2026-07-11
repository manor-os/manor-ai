"""Webhook endpoint and delivery log models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class WebhookEndpoint(Base, TimestampMixin):
    """A registered webhook endpoint that receives event notifications."""
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        Index("ix_webhook_endpoints_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    secret: Mapped[Optional[str]] = mapped_column(String(255))
    events: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    headers: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    description: Mapped[Optional[str]] = mapped_column(String(500))
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[Optional[str]] = mapped_column(String(20))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class WebhookDelivery(Base):
    """Log of webhook delivery attempts."""
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_endpoint", "endpoint_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    endpoint_id: Mapped[str] = mapped_column(String(26), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    response_body: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    duration_ms: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
