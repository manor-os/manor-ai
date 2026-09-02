"""Durable, generic execution leases for replay-prone runtime entrypoints."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class RuntimeExecutionClaim(Base, TimestampMixin):
    """One renewable PostgreSQL fence keyed by runtime kind and resource id."""

    __tablename__ = "runtime_execution_claims"
    __table_args__ = (
        Index("ix_runtime_execution_claims_expires", "expires_at"),
    )

    claim_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    claim_token: Mapped[str] = mapped_column(String(26), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


__all__ = ["RuntimeExecutionClaim"]
