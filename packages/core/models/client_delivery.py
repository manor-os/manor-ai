"""Private client contacts and pinned Workspace deliveries; never Staff."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


#: A delivered Workspace's ``_blueprint.blueprint_id`` is this prefix plus the
#: delivery id: its update source is the delivery row, not a Marketplace row.
DELIVERY_BLUEPRINT_ID_PREFIX = "client-delivery:"


class ClientContact(Base, TimestampMixin):
    __tablename__ = "client_contacts"
    __table_args__ = (UniqueConstraint("entity_id", "client_id", "email", name="uq_client_contact_email"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), index=True)
    client_id: Mapped[str] = mapped_column(String(26), index=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str | None] = mapped_column(String(26))
    phone: Mapped[str | None] = mapped_column(String(50))
    job_title: Mapped[str | None] = mapped_column(String(255))
    # Historical column retained for upgrades; delivery reads User.locale only.
    locale: Mapped[str | None] = mapped_column(String(10))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    invite_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ClientWorkspaceDelivery(Base, TimestampMixin):
    __tablename__ = "client_workspace_deliveries"
    __table_args__ = (
        Index("ix_client_delivery_client", "entity_id", "client_id"),
        # The billing payer lookup reads a Workspace's delivery on every charge.
        Index("ix_client_delivery_target_workspace", "target_workspace_id"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26))
    client_id: Mapped[str] = mapped_column(String(26))
    contact_id: Mapped[str] = mapped_column(String(26))
    contact_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    client_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_workspace_id: Mapped[str] = mapped_column(String(26))
    created_by: Mapped[str] = mapped_column(String(26))
    recipient_email: Mapped[str] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    payload: Mapped[dict] = mapped_column(JSONB)
    target_entity_id: Mapped[str | None] = mapped_column(String(26))
    target_workspace_id: Mapped[str | None] = mapped_column(String(26))
    accepted_by: Mapped[str | None] = mapped_column(String(26))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(32), default="delivered", server_default="delivered")
    acceptance: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    email_delivery: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # The release ``payload`` holds: its content version and the Knowledge
    # documents the provider selected, so a published update re-exports the same
    # selection. Empty on rows from before updates: version 1.0.0, no documents.
    content_release: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
