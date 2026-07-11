"""Captured browser-session credentials for non-OAuth integrations.

One row = one Playwright ``storage_state`` blob captured by the operator
through a HITL pairing flow. The blob itself is encrypted via the
configured KeyProvider; the row stores the ref + a coarse health
snapshot. See migration 20260424_05 for column rationale.

The browser adapter (packages/worker_sdk/adapters/browser.py) reads
``session_state_ref`` via ``CredentialService.lease_browser_session``
and hands the decrypted JSON to ``patchright.async_api.Browser.new_context(
storage_state=...)`` so the worker can act as the operator without
re-authenticating.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class IntegrationSession(Base):
    __tablename__ = "integration_sessions"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "provider", "label",
            name="uq_integration_sessions_label",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    label: Mapped[Optional[str]] = mapped_column(String(100))

    # Encrypted storage_state blob (see CredentialService).
    session_state_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="vault_transit",
    )

    # 'pending' | 'active' | 'expired' | 'revoked' — see migration.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending",
    )
    last_validated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    validated_steps: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expired_reason: Mapped[Optional[str]] = mapped_column(String(200))

    # Recipe to ping before issuing the session to a handler. Shape:
    # ``{"url": str, "selector": str?, "expected_text": str?}``. Empty
    # dict ⇒ skip the check (trust last_validated_at).
    health_check: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(),
    )
