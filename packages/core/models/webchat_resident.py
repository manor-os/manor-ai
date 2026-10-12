"""A private Webchat resident session, separate from management OAuth accounts."""
from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from packages.core.models.base import Base, TimestampMixin, generate_ulid


class WebchatResidentBinding(Base, TimestampMixin):
    __tablename__ = "webchat_resident_bindings"
    __table_args__ = (
        Index("uq_webchat_resident_conversation", "conversation_id", unique=True),
        Index("uq_webchat_resident_state", "state_hash", unique=True),
        Index("ix_webchat_resident_workspace", "workspace_id"),
        Index("ix_webchat_resident_contact", "channel_contact_id", unique=True),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_binding_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_config_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_contact_id: Mapped[str] = mapped_column(String(26), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manor_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    target_account_id: Mapped[str] = mapped_column(String(26), nullable=False)
    target_version: Mapped[str] = mapped_column(String(64), nullable=False)
    issuer: Mapped[str] = mapped_column(String(2048), nullable=False)
    pms_entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    subject: Mapped[Optional[str]] = mapped_column(String(255))
    browser_proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_hash: Mapped[Optional[str]] = mapped_column(String(64))
    state_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
