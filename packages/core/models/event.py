"""Event log model."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class EventLog(Base):
    """Lightweight event log for activity feeds."""
    __tablename__ = "event_logs"
    __table_args__ = (
        Index("ix_event_entity_created", "entity_id", "created_at"),
        Index("ix_event_type", "event_type"),
        Index(
            "ix_event_external_delivery_due",
            "external_delivery_status",
            "external_delivery_available_at",
            postgresql_where=text(
                "external_delivery_status IN ('pending', 'processing')"
            ),
        ),
        Index(
            "ix_event_external_delivery_lease",
            "external_delivery_status",
            "external_delivery_locked_until",
            postgresql_where=text("external_delivery_status = 'processing'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    external_delivery_status: Mapped[Optional[str]] = mapped_column(String(20))
    external_delivery_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    external_delivery_available_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    external_delivery_locked_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    external_delivery_claim_token: Mapped[Optional[str]] = mapped_column(String(26))
    external_delivery_delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    external_delivery_completed_sinks: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    external_delivery_last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
