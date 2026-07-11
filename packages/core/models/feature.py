"""Feature gating and subscription package models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Index, Integer, Numeric, String, BigInteger,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class Feature(Base, TimestampMixin):
    """Platform feature / capability."""
    __tablename__ = "features"
    __table_args__ = (
        Index("ix_features_key", "key", unique=True),
        Index("ix_features_category", "category"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    category: Mapped[Optional[str]] = mapped_column(String(100))
    parent_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class FeaturePackage(Base, TimestampMixin):
    """Bundle of features sold as a subscription tier."""
    __tablename__ = "feature_packages"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    max_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    max_credit: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    price_monthly: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    price_yearly: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    features: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # features is a JSON list of feature key strings, e.g. ["chat", "agent_builder"]
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class EntityFeature(Base, TimestampMixin):
    """Entity-level feature override / individual subscription."""
    __tablename__ = "entity_features"
    __table_args__ = (
        Index("ix_entity_features_entity_key", "entity_id", "feature_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
