"""User's pinned/favorited/bookmarked items."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class Favorite(Base):
    """User's pinned/favorited/bookmarked items."""
    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "resource_type", "resource_id", "favorite_type",
                         name="uq_favorite_user_resource"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "task", "document", "conversation", "agent", "message"
    resource_id: Mapped[str] = mapped_column(String(26), nullable=False)
    favorite_type: Mapped[str] = mapped_column(String(20), nullable=False, default="star")  # "star", "pin", "bookmark"
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
