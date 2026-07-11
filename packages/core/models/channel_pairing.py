"""Short-lived DM pairing codes — see migration 20260424_07."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ChannelPairingCode(Base):
    __tablename__ = "channel_pairing_codes"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_channel_id: Mapped[Optional[str]] = mapped_column(String(26))
    hint: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
