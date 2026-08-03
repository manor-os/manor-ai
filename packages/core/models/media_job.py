"""Media generation jobs — async video/image generation tracking."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class MediaJobStatus(str, Enum):
    """Lifecycle of an async media generation job.

    Consumers compare against these members rather than string literals —
    an artifact collector that guessed at the word for "finished" would
    either miss real deliverables or promote a pending job's empty path.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.COMPLETED.value, cls.FAILED.value})

    @classmethod
    def is_completed(cls, value: object) -> bool:
        return str(value or "").strip().lower() == cls.COMPLETED.value


class MediaJob(Base, TimestampMixin):
    """Tracks async media generation jobs (video, image, etc.)."""
    __tablename__ = "media_jobs"
    __table_args__ = (
        Index("ix_media_jobs_entity", "entity_id"),
        Index("ix_media_jobs_status", "entity_id", "status"),
        Index("ix_media_jobs_conversation", "conversation_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Job type and status
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # "video", "image"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending → processing → completed / failed

    # Input parameters
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(100))
    params: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    # params: {duration, resolution, seed, image_url, size, quality, ...}

    # Result
    result_url: Mapped[Optional[str]] = mapped_column(Text)  # local URL after download
    source_url: Mapped[Optional[str]] = mapped_column(Text)  # original provider URL
    error: Mapped[Optional[str]] = mapped_column(Text)
    file_size: Mapped[Optional[int]] = mapped_column(Integer)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)

    # Billing
    cost_usd: Mapped[Optional[float]] = mapped_column(nullable=True)
    credits: Mapped[Optional[int]] = mapped_column(Integer)
    byok: Mapped[bool] = mapped_column(default=False)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
