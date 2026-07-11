"""Platform-level official model provider credentials."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class PlatformModelProviderKey(Base, TimestampMixin):
    """Encrypted official API token for one model catalog provider.

    These rows are global platform credentials, not tenant/user BYOK keys.
    Plaintext is stored through CredentialService in ``credential_ref``.
    """

    __tablename__ = "platform_model_provider_keys"
    __table_args__ = (
        Index("ix_platform_model_provider_keys_provider", "provider", unique=True),
        Index("ix_platform_model_provider_keys_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", server_default="active")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    credential_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="legacy_jsonb",
    )

    created_by: Mapped[Optional[str]] = mapped_column(String(26))
    updated_by: Mapped[Optional[str]] = mapped_column(String(26))
    last_rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
