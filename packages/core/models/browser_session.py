"""Revocable browser login families. Only refresh-token digests are stored."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base, TimestampMixin, generate_ulid


class BrowserSession(Base, TimestampMixin):
    __tablename__ = "browser_sessions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mfa_authenticated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    rotation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
