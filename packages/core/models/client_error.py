"""Client-side runtime error events.

Stored as append-only browser/admin UI error reports. Aggregation is done
by fingerprint in the platform-admin read surface rather than maintaining
separate issue rows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ClientErrorEvent(Base):
    """One browser-side error report from the web or admin app."""

    __tablename__ = "client_error_events"
    __table_args__ = (
        Index("ix_client_errors_created", "created_at"),
        Index("ix_client_errors_fingerprint_created", "fingerprint", "created_at"),
        Index("ix_client_errors_entity_created", "entity_id", "created_at"),
        Index("ix_client_errors_source_created", "source", "created_at"),
        Index("ix_client_errors_level_created", "level", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))

    source: Mapped[str] = mapped_column(String(40), nullable=False, server_default="web")
    level: Mapped[str] = mapped_column(String(20), nullable=False, server_default="error")
    handled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    name: Mapped[Optional[str]] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack: Mapped[Optional[str]] = mapped_column(Text)
    component_stack: Mapped[Optional[str]] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(96), nullable=False)

    route: Mapped[Optional[str]] = mapped_column(String(500))
    url: Mapped[Optional[str]] = mapped_column(Text)
    release: Mapped[Optional[str]] = mapped_column(String(120))
    environment: Mapped[Optional[str]] = mapped_column(String(80))
    request_id: Mapped[Optional[str]] = mapped_column(String(80))

    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    ip_address: Mapped[Optional[str]] = mapped_column(String(128))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
