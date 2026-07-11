"""Event log model."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class EventLog(Base):
    """Lightweight event log for activity feeds."""
    __tablename__ = "event_logs"
    __table_args__ = (
        Index("ix_event_entity_created", "entity_id", "created_at"),
        Index("ix_event_type", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
