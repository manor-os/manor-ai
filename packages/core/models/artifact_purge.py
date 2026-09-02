"""Durable cleanup intents for entity artifact files and trees."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid

# Document paths allow 1000 characters; Runtime rollback backups append an
# atomic PID/time/UUID suffix before their durable cleanup intent is written.
ARTIFACT_CLEANUP_STORAGE_BASE_MAX_LENGTH = 1100


class WorkspaceArtifactPurgeJob(Base, TimestampMixin):
    """Filesystem cleanup that remains retryable after its database deletion."""

    __tablename__ = "workspace_artifact_purge_jobs"
    __table_args__ = (
        Index(
            "uq_workspace_artifact_purge_scope",
            "entity_id",
            "storage_base",
            unique=True,
        ),
        Index(
            "ix_workspace_artifact_purge_due",
            "next_attempt_at",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(26), primary_key=True, default=generate_ulid,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    storage_base: Mapped[str] = mapped_column(
        String(ARTIFACT_CLEANUP_STORAGE_BASE_MAX_LENGTH),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tree", server_default="tree",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
