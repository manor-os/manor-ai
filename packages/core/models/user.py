"""User and entity models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin, generate_ulid


class Entity(Base, TimestampMixin, SoftDeleteMixin):
    """An organization / company / workspace owner."""
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    address: Mapped[Optional[str]] = mapped_column(String)
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    email: Mapped[Optional[str]] = mapped_column(String(100))
    logo_url: Mapped[Optional[str]] = mapped_column(String(500))
    llm_model: Mapped[Optional[str]] = mapped_column(String(100))
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    # Cloud fields
    plan_id: Mapped[Optional[str]] = mapped_column(String(26), server_default="plan_free")
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(255))
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class User(Base, TimestampMixin, SoftDeleteMixin):
    """A login identity.

    ``entity_id`` remains the user's primary/personal entity for backward
    compatibility. Company access is represented by ``UserMembership`` rows.
    Request auth maps the active membership onto ``user.entity_id``/``role``
    without persisting that overlay back to this row.
    """
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Evidence for this exact address, never inferred from active account status.
    verified_email: Mapped[Optional[str]] = mapped_column(String(255))
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(100))
    last_name: Mapped[Optional[str]] = mapped_column(String(100))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    # Roles: owner, admin, member, viewer, client
    llm_model: Mapped[Optional[str]] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    locale: Mapped[str] = mapped_column(String(10), default="en")
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(128))
    # Server-side revocation epoch for bearer tokens. Every JWT carries the
    # version current at issuance; logout, password changes, and account
    # disablement increment this value so already-issued tokens stop working.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # 2FA / TOTP fields
    totp_secret: Mapped[Optional[str]] = mapped_column(String(255))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    backup_codes: Mapped[Optional[list]] = mapped_column(JSONB)


@event.listens_for(User.email, "set")
def _invalidate_changed_email_proof(user, value, old_value, initiator):
    if value != old_value:
        user.verified_email = None
        user.email_verified_at = None


@event.listens_for(User, "after_insert")
@event.listens_for(User, "after_delete")
def _invalidate_response_locale(mapper, connection, user):
    from sqlalchemy.orm import object_session
    from packages.core.ai.response_language import response_locale_cache_key
    from packages.core.read_cache import invalidate_after_commit

    session = object_session(user)
    if session is not None:
        invalidate_after_commit(session, response_locale_cache_key(user.id))


@event.listens_for(User, "after_update")
def _invalidate_updated_response_locale(mapper, connection, user):
    from sqlalchemy import inspect

    state = inspect(user)
    if state.attrs.locale.history.has_changes() or state.attrs.deleted_at.history.has_changes():
        _invalidate_response_locale(mapper, connection, user)




class UserMembership(Base, TimestampMixin, SoftDeleteMixin):
    """A user's membership in an entity/company."""
    __tablename__ = "user_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "entity_id", name="uq_user_memberships_user_entity"),
        Index("ix_user_memberships_user", "user_id"),
        Index("ix_user_memberships_entity", "entity_id"),
        Index("ix_user_memberships_status", "entity_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    staff_id: Mapped[Optional[str]] = mapped_column(String(26))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class OAuthAccount(Base, TimestampMixin):
    """Third-party OAuth accounts (Google, GitHub, etc.)."""
    __tablename__ = "oauth_accounts"
    __table_args__ = (
        Index("ix_oauth_accounts_user_provider", "user_id", "provider"),
        Index(
            "uq_oauth_accounts_user_provider_external",
            "user_id",
            "provider",
            "provider_user_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token: Mapped[Optional[str]] = mapped_column(String)
    refresh_token: Mapped[Optional[str]] = mapped_column(String)
    # New writes use CredentialService and leave the two legacy plaintext
    # columns above empty. They remain temporarily for a rolling backfill.
    credential_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy_columns",
        server_default="legacy_columns",
    )
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    profile: Mapped[dict] = mapped_column(JSONB, server_default="{}")
