"""OAuth 2.0 provider models — Manor acts as IdP for downstream apps (PMS, etc.).

Minimal Authorization Code flow:
  1. Client app redirects user to /api/v1/oauth/authorize with client_id + redirect_uri + state
  2. After user is authenticated in Manor, we create an OAuthAuthorizationCode (10-min TTL)
     and redirect back to redirect_uri?code=<code>&state=<state>
  3. Client app POSTs /api/v1/oauth/token with code + client_id + client_secret;
     we verify and return a Manor access JWT.

The access token is a regular Manor JWT (HS256) — same format the rest of Manor uses.
No separate id_token in V1; the JWT itself carries sub/entity_id/email.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class OAuthClientApp(Base, TimestampMixin):
    """A registered OAuth client app (e.g. PMS) authorised to use Manor as IdP."""
    __tablename__ = "oauth_client_apps"
    __table_args__ = (
        Index("ix_oauth_client_apps_client_id", "client_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)

    # Public identifier shared with the client app
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # Bcrypt hash of the client secret (never stored in plaintext)
    client_secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Human-readable name for admin UI
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Whitelist of allowed redirect URIs (exact match, JSON array of strings)
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    # Scopes this client may request (JSON array). Empty == any default scope.
    allowed_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default='["openid","profile","email"]')

    # If set, this client may only authenticate users belonging to this entity.
    # Null = users from any entity can authenticate.
    restricted_entity_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Whether the client app is currently allowed to issue codes
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # Token TTL in minutes (uses JWT_EXPIRE_MINUTES default if null)
    access_token_ttl_minutes: Mapped[Optional[int]] = mapped_column()


class OAuthAuthorizationCode(Base, TimestampMixin):
    """Short-lived authorisation code issued by /authorize and redeemed at /token."""
    __tablename__ = "oauth_authorization_codes"
    __table_args__ = (
        Index("ix_oauth_codes_code", "code", unique=True),
        Index("ix_oauth_codes_client_user", "client_id", "user_id"),
        Index("ix_oauth_codes_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)

    # The opaque code string (random, URL-safe). 10-minute lifetime.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # Redirect URI presented at /authorize — must match exactly at /token redemption.
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Scopes granted (JSON array)
    scope: Mapped[list] = mapped_column(JSONB, nullable=False, server_default='[]')

    # CSRF state from the client (echoed back at redirect; not strictly required at /token)
    state: Mapped[Optional[str]] = mapped_column(String(255))

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
