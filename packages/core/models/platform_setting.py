"""Generic platform-level key/value settings.

One row per setting key, JSONB value. Used for cross-tenant operator
controls that don't warrant their own table — first consumer is the
model catalog controls (disabled models + default overrides) managed
from the platform admin portal.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class PlatformSetting(Base, TimestampMixin):
    """A single platform-wide setting document, addressed by key."""

    __tablename__ = "platform_settings"
    __table_args__ = (
        Index("ix_platform_settings_key", "key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    updated_by: Mapped[Optional[str]] = mapped_column(String(26))
