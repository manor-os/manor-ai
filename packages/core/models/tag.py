"""Tag models — universal tagging system for any resource."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, PrimaryKeyConstraint, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models.base import Base, TimestampMixin, generate_ulid


class Tag(Base, TimestampMixin):
    """Entity-scoped tag definition."""
    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        UniqueConstraint("entity_id", "name", name="uq_tags_entity_name"),
    )


class ResourceTag(Base):
    """Many-to-many link between tags and any resource."""
    __tablename__ = "resource_tags"

    tag_id: Mapped[str] = mapped_column(String(26), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(26), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("tag_id", "resource_type", "resource_id"),
    )
