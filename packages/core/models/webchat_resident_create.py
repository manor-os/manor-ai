"""Separate resident create delegation and explicit-confirmation draft state."""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class ResidentCreateGrant(Base, TimestampMixin):
    __tablename__ = "webchat_resident_create_grants"
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    binding_id: Mapped[str] = mapped_column(
        ForeignKey("webchat_resident_bindings.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    issuer: Mapped[str] = mapped_column(String(2048), nullable=False)
    credential_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    state_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    state_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    read_generation: Mapped[int] = mapped_column(Integer, nullable=False)


class ResidentMaintenanceDraft(Base, TimestampMixin):
    __tablename__ = "webchat_resident_maintenance_drafts"
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (UniqueConstraint("binding_id", "source_call_id", name="uq_resident_draft_tool_call"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    binding_id: Mapped[str] = mapped_column(
        ForeignKey("webchat_resident_bindings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_call_id: Mapped[Optional[str]] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    location_label: Mapped[Optional[str]] = mapped_column(String(500))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    submission_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    confirmed_payload: Mapped[Optional[dict]] = mapped_column(JSONB)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    attempt_id: Mapped[Optional[str]] = mapped_column(String(64))
    attempt_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    request: Mapped[Optional[dict]] = mapped_column(JSONB)
    error: Mapped[Optional[dict]] = mapped_column(JSONB)
