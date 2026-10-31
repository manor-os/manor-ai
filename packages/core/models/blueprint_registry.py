"""Blueprint release history: packages, immutable versions and dist-tags.

``workspace_blueprints.payload`` and ``client_workspace_deliveries.payload``
stay the serving copy of each package's ``latest`` release, so every existing
read path is unchanged. These tables keep what a single overwritten column
could not: every release ever made current, which an installed Workspace can
name exactly. ``packages/core/blueprints/registry.py::record_release`` is the
only writer of a release; see it for the invariants.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class BlueprintPackage(Base, TimestampMixin):
    __tablename__ = "blueprint_packages"
    __table_args__ = (UniqueConstraint("name", name="uq_blueprint_packages_name"),)

    #: The durable identity an install already records as
    #: ``_blueprint.blueprint_id``: ``builtin:<slug>``, a Marketplace ULID or
    #: ``client-delivery:<id>``. The name below is only an alias.
    ref: Mapped[str] = mapped_column(String(160), primary_key=True)
    #: ``@scope/slug``.
    name: Mapped[str] = mapped_column(String(214), nullable=False)
    # 'platform' | 'marketplace' | 'delivery'
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: NULL means the platform owns it.
    owner_entity_id: Mapped[Optional[str]] = mapped_column(String(26), index=True)
    #: The Marketplace row that lists the package (pricing, purchases, reviews).
    listing_id: Mapped[Optional[str]] = mapped_column(String(160))
    delivery_id: Mapped[Optional[str]] = mapped_column(String(26))
    deprecated: Mapped[Optional[str]] = mapped_column(Text)


class BlueprintVersion(Base):
    """One release. Append-only: a row is never updated or deleted."""

    __tablename__ = "blueprint_versions"
    __table_args__ = (
        UniqueConstraint(
            "package_ref", "version", name="uq_blueprint_versions_package_version",
        ),
        Index(
            "ix_blueprint_versions_package_fingerprint",
            "package_ref", "content_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    package_ref: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("blueprint_packages.ref", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Payload format ('1.1').
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    #: Package archive digest. Releases recorded before archives existed have none.
    integrity: Mapped[Optional[str]] = mapped_column(String(100))
    #: ``freshness.blueprint_content_fingerprint`` — what installs record.
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    section_fingerprints: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    #: NULL only for a version that merely identifies an install's baseline.
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    lock: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    changelog: Mapped[Optional[str]] = mapped_column(Text)
    #: 'major' | 'minor' | 'patch' once the platform computes it.
    min_bump: Mapped[Optional[str]] = mapped_column(String(10))
    # 'listed' | 'hidden' (kept only to identify what an install came from)
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="listed", server_default="listed",
    )
    # 'published' | 'backfill' | 'git_history' | 'baseline'
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default="published", server_default="published",
    )
    deprecated: Mapped[Optional[str]] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    published_by: Mapped[Optional[str]] = mapped_column(String(26))


class BlueprintDistTag(Base):
    __tablename__ = "blueprint_dist_tags"

    package_ref: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("blueprint_packages.ref", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(26))


class WorkspaceBlueprintUpgrade(Base):
    """One applied in-place upgrade of a Workspace. Append-only except
    ``reverted_at``, set when that upgrade is undone."""

    __tablename__ = "workspace_blueprint_upgrades"
    __table_args__ = (
        Index(
            "ix_workspace_blueprint_upgrades_workspace",
            "workspace_id", "applied_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    workspace_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    package_ref: Mapped[Optional[str]] = mapped_column(String(160))
    from_version: Mapped[Optional[str]] = mapped_column(String(64))
    to_version: Mapped[Optional[str]] = mapped_column(String(64))
    from_integrity: Mapped[Optional[str]] = mapped_column(String(100))
    to_integrity: Mapped[Optional[str]] = mapped_column(String(100))
    #: Per-item keep_yours / use_blueprint decisions.
    choices: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    #: Same shape as ``WorkspaceBlueprintUpgradeState.restore_point`` minus
    #: ``baseline_before``, which has its own column.
    restore: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    baseline_before: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    applied_by: Mapped[Optional[str]] = mapped_column(String(26))
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    reverted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
