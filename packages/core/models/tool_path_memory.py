"""Intent→tool-path memory for tool discovery v2 (spec §A3).

One row = "this tool succeeded for tasks whose intent looks like these
terms, for this user". Suppression = failure evidence outweighs success
recency; suppressed rows stay (reversible; integration-health signal) and
are evicted first at the row cap.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class ToolIntentPath(Base, TimestampMixin):
    __tablename__ = "tool_intent_paths"
    __table_args__ = (
        Index("ix_tool_intent_paths_user", "entity_id", "user_id"),
        Index(
            "uq_tool_intent_paths_key",
            "entity_id", "user_id", "intent_signature", "tool_name",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    intent_signature: Mapped[str] = mapped_column(String(400), nullable=False)
    """Sorted, deduped, space-joined term set (≤12 terms) of the user message."""
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
