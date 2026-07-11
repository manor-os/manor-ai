"""API key model — entity-level LLM provider credentials."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class ApiKey(Base, TimestampMixin):
    """An LLM provider API key owned by an entity."""
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_entity", "entity_id"),
        Index("ix_api_keys_entity_default", "entity_id", "is_default"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # openrouter, openai, anthropic, custom
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[Optional[str]] = mapped_column(String(20))  # first chars for identification
    base_url: Mapped[Optional[str]] = mapped_column(String(500))
    default_model: Mapped[Optional[str]] = mapped_column(String(100))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
