"""Audit log model."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class AuditLog(Base):
    """Immutable audit trail for entity actions."""
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_entity", "entity_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(50))
    resource_id: Mapped[Optional[str]] = mapped_column(String(26))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    ip_address: Mapped[Optional[str]] = mapped_column(String(128))
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
