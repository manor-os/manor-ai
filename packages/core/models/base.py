"""
Manor AI — SQLAlchemy base, ULID ID generation, and common mixins.

ID strategy: ULID (Universally Unique Lexicographically Sortable Identifier)
  - 26 chars, Crockford base32
  - Time-sorted → B-tree friendly inserts
  - Globally unique without coordination
  - URL-safe, human-readable
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from ulid import ULID


def generate_ulid() -> str:
    """Generate a new ULID string."""
    return str(ULID())


class Base(DeclarativeBase):
    """Base class for all Manor models."""
    pass


class TimestampMixin:
    """created_at + updated_at."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class SoftDeleteMixin:
    """Soft delete via deleted_at."""
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
