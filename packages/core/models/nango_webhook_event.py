"""Audit log row for inbound Nango webhook events.

Persisted by ``apps.api.routers.nango_webhooks`` so admins can debug
provider events flowing through Nango → Manor even when no dispatcher
is wired for that provider yet.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class NangoWebhookEvent(Base):
    __tablename__ = "nango_webhook_events"
    __table_args__ = (
        Index("ix_nango_webhook_events_received_at", "received_at"),
        Index("ix_nango_webhook_events_connection_id", "connection_id"),
        Index("ix_nango_webhook_events_entity_id", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    # Nango envelope fields
    nango_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(64))
    provider_config_key: Mapped[Optional[str]] = mapped_column(String(64))
    connection_id: Mapped[Optional[str]] = mapped_column(String(120))

    # Resolved Manor IDs (filled if the connection_id matched an Integration)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    integration_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Processing outcome
    processing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="received",
    )
    processing_detail: Mapped[Optional[str]] = mapped_column(Text)

    # Raw event JSON
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )
