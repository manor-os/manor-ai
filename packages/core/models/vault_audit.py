"""Vault audit log — read-only model over ``vault_audit_log``.

Writes happen via the synchronous ``DBAuditSink`` in
packages/core/credentials/audit.py — that path bypasses SQLAlchemy ORM
to keep audit independent from the caller's async transaction. This
model exists so other services (admin UI, security review) can query
the table normally.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class VaultAuditLog(Base):
    __tablename__ = "vault_audit_log"
    __table_args__ = (
        Index("ix_vault_audit_ref_time", "credential_ref", "occurred_at"),
        Index("ix_vault_audit_action_time", "action", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    credential_ref: Mapped[Optional[str]] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    requester_kind: Mapped[Optional[str]] = mapped_column(String(32))
    requester_id: Mapped[Optional[str]] = mapped_column(String(64))
    step_id: Mapped[Optional[str]] = mapped_column(String(26))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    ttl_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
