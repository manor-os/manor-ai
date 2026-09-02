"""Immutable product-growth milestone facts."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ProductGrowthEvent(Base):
    """A durable, idempotent product milestone attributed to one user."""

    __tablename__ = "product_growth_events"
    __table_args__ = (
        Index(
            "uq_product_growth_event_source",
            "entity_id",
            "milestone",
            "source_kind",
            "source_id",
            unique=True,
        ),
        Index(
            "ix_product_growth_user_milestone_occurred",
            "user_id",
            "milestone",
            "occurred_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(26),
        primary_key=True,
        default=generate_ulid,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    milestone: Mapped[str] = mapped_column(String(50), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
