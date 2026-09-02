"""Durable Marketplace resource identity links.

Marketplace rows and installed runtime rows deliberately have different ids.
This table is the indexed, typed relationship between them; mutable labels
such as names and slugs are metadata and must never be used as identity.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class MarketplaceResourceLink(Base, TimestampMixin):
    """Link one Marketplace source id to one local resource id.

    ``scope_type``/``scope_id`` allow one Blueprint to be installed into many
    Workspaces while preserving an exact component mapping for each install.
    A ``resource`` scope preserves source provenance after a reusable local
    component loses its original home Workspace. ``relationship`` also
    separates a Workspace installed *from* a Blueprint from the distinct
    Blueprint it may later be published *as*.
    """

    __tablename__ = "marketplace_resource_links"
    __table_args__ = (
        UniqueConstraint(
            "entity_id",
            "marketplace_source",
            "marketplace_resource_type",
            "marketplace_resource_id",
            "relationship",
            "scope_type",
            "scope_id",
            "local_resource_type",
            "component_key",
            name="uq_marketplace_resource_links_source_scope",
        ),
        UniqueConstraint(
            "entity_id",
            "relationship",
            "scope_type",
            "scope_id",
            "local_resource_type",
            "local_resource_id",
            name="uq_marketplace_resource_links_local_scope",
        ),
        Index(
            "ix_marketplace_resource_links_local",
            "entity_id",
            "local_resource_type",
            "local_resource_id",
            "relationship",
        ),
        Index(
            "ix_marketplace_resource_links_source",
            "marketplace_source",
            "marketplace_resource_type",
            "marketplace_resource_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(26), primary_key=True, default=generate_ulid,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    marketplace_source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="platform", server_default="platform",
    )
    marketplace_resource_type: Mapped[str] = mapped_column(
        String(40), nullable=False,
    )
    marketplace_resource_id: Mapped[str] = mapped_column(
        String(160), nullable=False,
    )
    relationship: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(160), nullable=False)
    local_resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    local_resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    component_key: Mapped[str] = mapped_column(
        String(160), nullable=False, default="root", server_default="root",
    )
    marketplace_version: Mapped[Optional[str]] = mapped_column(String(40))
    linked_by: Mapped[Optional[str]] = mapped_column(String(26), index=True)
    link_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
