"""Platform feature flags — gradual rollouts + per-tenant exceptions.

Two tables:

  * ``feature_flags`` — registry of every flag the platform knows about,
    with a default value and human description. Flags MUST be defined
    here before they're checked at runtime — unknown keys return the
    fallback the caller passes (or False).

  * ``feature_flag_overrides`` — per-tenant / per-user / per-percent
    exceptions to the default. A tenant override beats a percent
    override beats the default.

NOT to be confused with the existing ``Feature`` / ``FeaturePackage`` /
``EntityFeature`` entity-feature system — those are billable add-ons
that show up on the org-side billing page. Platform feature flags are
ops controls invisible to the customer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class FeatureFlag(Base, TimestampMixin):
    """Registry row for a platform-controlled feature toggle."""
    __tablename__ = "feature_flags"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    """Stable identifier, e.g. ``fabrication_check`` or ``new_kanban``.
    Code refers to flags by key, not id."""

    description: Mapped[Optional[str]] = mapped_column(Text)
    default_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active",
    )
    # 'active' | 'archived' (kept for history, no longer evaluated)


class FeatureFlagOverride(Base, TimestampMixin):
    """Targeted override for a specific tenant / user / percentage."""
    __tablename__ = "feature_flag_overrides"
    __table_args__ = (
        UniqueConstraint(
            "flag_key", "scope", "scope_id",
            name="uq_feature_flag_overrides_target",
        ),
        Index("ix_feature_flag_overrides_flag", "flag_key"),
        Index("ix_feature_flag_overrides_scope", "scope", "scope_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    flag_key: Mapped[str] = mapped_column(String(80), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    """One of: ``tenant`` | ``user`` | ``percent``.

    * tenant → scope_id is an Entity.id
    * user   → scope_id is a User.id
    * percent → scope_id is the rollout target string
                ("0"-"100") and the value applies to that fraction of
                tenants (deterministic by tenant id hash)
    """
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)

    set_by_admin_id: Mapped[Optional[str]] = mapped_column(String(26))
    set_reason: Mapped[Optional[str]] = mapped_column(Text)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
