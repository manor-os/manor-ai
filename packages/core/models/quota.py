"""Entity quota model — usage limits and current-period counters."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import BigInteger, Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class EntityQuota(Base, TimestampMixin):
    """Usage quotas and limits for an entity."""
    __tablename__ = "entity_quotas"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), unique=True, nullable=False)
    plan_name: Mapped[str] = mapped_column(String(100), server_default="free")

    # Limits (-1 = unlimited)
    max_users: Mapped[int] = mapped_column(Integer, server_default="5")
    max_agents: Mapped[int] = mapped_column(Integer, server_default="3")
    max_documents: Mapped[int] = mapped_column(Integer, server_default="100")
    max_storage_bytes: Mapped[int] = mapped_column(BigInteger, server_default="1073741824")  # 1 GB
    max_tokens_monthly: Mapped[int] = mapped_column(BigInteger, server_default="1000000")  # 1M
    max_api_calls_daily: Mapped[int] = mapped_column(Integer, server_default="10000")

    # Current period usage (reset monthly / daily)
    tokens_used_this_month: Mapped[int] = mapped_column(BigInteger, server_default="0")
    api_calls_today: Mapped[int] = mapped_column(Integer, server_default="0")
    storage_used_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")

    # Reset tracking
    current_period_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_daily_reset: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
