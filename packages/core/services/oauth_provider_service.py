"""OAuth 2.0 provider service — Manor as IdP for downstream apps.

Authorization Code flow only (no implicit, no client_credentials, no PKCE in V1).

Public functions:
  - verify_client_credentials(client_id, client_secret) -> OAuthClientApp | None
  - get_client_by_id(client_id) -> OAuthClientApp | None
  - validate_redirect_uri(client, redirect_uri) -> bool
  - create_authorization_code(client, user, redirect_uri, scope, state) -> str
  - exchange_code_for_token(code, client_id, client_secret, redirect_uri) -> dict
  - seed_clients_from_env(db) -> dict[client_id, action]
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.oauth_provider import OAuthAuthorizationCode, OAuthClientApp
from packages.core.models.user import User
from packages.core.services.auth_service import create_access_token

logger = logging.getLogger(__name__)


# How long an authorization code lives before it can no longer be redeemed
AUTHORIZATION_CODE_TTL_MINUTES = 10


# ── Client credential helpers ──

def hash_client_secret(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()


def _verify_client_secret(plain: str, hashed: str) -> bool:
    if not hashed or not plain:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        logger.warning("Invalid client secret hash format")
        return False


async def get_client_by_id(db: AsyncSession, client_id: str) -> Optional[OAuthClientApp]:
    result = await db.execute(
        select(OAuthClientApp).where(OAuthClientApp.client_id == client_id)
    )
    return result.scalar_one_or_none()


async def verify_client_credentials(
    db: AsyncSession, client_id: str, client_secret: str
) -> Optional[OAuthClientApp]:
    client = await get_client_by_id(db, client_id)
    if not client or not client.active:
        return None
    if not _verify_client_secret(client_secret, client.client_secret_hash):
        return None
    return client


def validate_redirect_uri(client: OAuthClientApp, redirect_uri: str) -> bool:
    """Exact-match redirect URI against the whitelist."""
    allowed = client.redirect_uris or []
    return redirect_uri in allowed


# ── Authorization code ──

def _generate_code() -> str:
    return secrets.token_urlsafe(32)


async def create_authorization_code(
    db: AsyncSession,
    *,
    client: OAuthClientApp,
    user: User,
    redirect_uri: str,
    scope: list[str],
    state: Optional[str],
) -> OAuthAuthorizationCode:
    """Mint a short-lived authorization code bound to (client, user, redirect_uri)."""
    if client.restricted_entity_id and client.restricted_entity_id != user.entity_id:
        raise PermissionError("User's entity is not permitted for this OAuth client")

    code_str = _generate_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=AUTHORIZATION_CODE_TTL_MINUTES)
    code = OAuthAuthorizationCode(
        code=code_str,
        client_id=client.client_id,
        user_id=user.id,
        entity_id=user.entity_id,
        redirect_uri=redirect_uri,
        scope=scope or [],
        state=state,
        expires_at=expires,
    )
    db.add(code)
    await db.flush()
    return code


async def exchange_code_for_token(
    db: AsyncSession,
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    """Redeem an authorization code for a Manor JWT access token.

    Raises ValueError on any mismatch. Returns dict matching OAuth 2.0 token response.
    """
    client = await verify_client_credentials(db, client_id, client_secret)
    if not client:
        raise ValueError("invalid_client")

    result = await db.execute(
        select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code)
    )
    code_row: Optional[OAuthAuthorizationCode] = result.scalar_one_or_none()
    if not code_row:
        raise ValueError("invalid_grant")

    # Single-use: reject already-redeemed codes
    if code_row.redeemed_at is not None:
        raise ValueError("invalid_grant")

    # Must match the requesting client
    if code_row.client_id != client.client_id:
        raise ValueError("invalid_grant")

    # Redirect URI must match exactly
    if code_row.redirect_uri != redirect_uri:
        raise ValueError("invalid_grant")

    # Expiry check
    expires = code_row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise ValueError("invalid_grant")

    # Mark redeemed (single-use)
    code_row.redeemed_at = datetime.now(timezone.utc)

    # Load user
    user_result = await db.execute(select(User).where(User.id == code_row.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise ValueError("invalid_grant")

    # Mint a Manor JWT — use the client's configured TTL if set
    from packages.core.config import get_settings
    settings = get_settings()
    # create_access_token uses JWT_EXPIRE_MINUTES env. For client-specific TTL we override here.
    if client.access_token_ttl_minutes:
        from jose import jwt
        payload = {
            "sub": user.id,
            "entity_id": user.entity_id,
            "role": user.role,
            "email": user.email,
            "name": user.display_name or user.email,
            "aud": client.client_id,
            "iss": "manor",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=client.access_token_ttl_minutes),
        }
        access_token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
        expires_in = client.access_token_ttl_minutes * 60
    else:
        # Default Manor JWT (24h)
        access_token = create_access_token(user.id, user.entity_id, user.role)
        import os
        expires_in = int(os.getenv("JWT_EXPIRE_MINUTES", "1440")) * 60

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": " ".join(code_row.scope or []),
        # Convenience claims so the client doesn't have to decode the JWT
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.display_name or user.email,
            "entity_id": user.entity_id,
            "role": user.role,
        },
    }


# ── Startup bootstrap from env vars ──

# Convention for registering a downstream app as an OAuth client of Manor:
#
#   MANOR_OAUTH_CLIENT_<SLUG>_SECRET=<plaintext bcrypt input>     (required)
#   MANOR_OAUTH_CLIENT_<SLUG>_NAME=<display name>                 (optional)
#   MANOR_OAUTH_CLIENT_<SLUG>_DESCRIPTION=<consent screen text>   (optional)
#   MANOR_OAUTH_CLIENT_<SLUG>_REDIRECT_URIS=<csv>                 (required for new clients)
#   MANOR_OAUTH_CLIENT_<SLUG>_SCOPES=<csv>                        (optional, defaults to openid,profile,email)
#   MANOR_OAUTH_CLIENT_<SLUG>_RESTRICTED_ENTITY_ID=<ulid>         (optional)
#   MANOR_OAUTH_CLIENT_<SLUG>_ACCESS_TOKEN_TTL_MINUTES=<int>      (optional, defaults to 1440)
#
# <SLUG> uses UPPERCASE_WITH_UNDERSCORES; the resulting client_id is
# <slug> lowercased with '_' → '-' (e.g. PMS_MANAGEMENT → pms-management).
# Override with MANOR_OAUTH_CLIENT_<SLUG>_CLIENT_ID if you need a different
# casing/format.
#
# Adding a new app (e.g. mobile portal) requires zero code change — just set
# the env vars in the deploy environment (PROD_ENV_FILE blob or via the
# inject_from_env mechanism in .github/workflows/deploy.yml).

_ENV_PREFIX = "MANOR_OAUTH_CLIENT_"
_ENV_SECRET_SUFFIX = "_SECRET"


def _slug_to_client_id(slug_upper: str) -> str:
    """PMS_MANAGEMENT → pms-management"""
    return slug_upper.lower().replace("_", "-")


def _discover_client_slugs() -> list[str]:
    """Find every <SLUG> from MANOR_OAUTH_CLIENT_<SLUG>_SECRET env vars."""
    slugs: list[str] = []
    for key in os.environ:
        if not key.startswith(_ENV_PREFIX) or not key.endswith(_ENV_SECRET_SUFFIX):
            continue
        slug = key[len(_ENV_PREFIX): -len(_ENV_SECRET_SUFFIX)]
        if slug:
            slugs.append(slug)
    return sorted(set(slugs))


def _read_client_config(slug_upper: str) -> Optional[dict]:
    """Read env-var-backed config for one client. Returns None if secret absent."""
    base = f"{_ENV_PREFIX}{slug_upper}"
    secret = os.getenv(f"{base}{_ENV_SECRET_SUFFIX}")
    if not secret:
        return None

    default_id = _slug_to_client_id(slug_upper)
    client_id = os.getenv(f"{base}_CLIENT_ID") or default_id

    uris_csv = os.getenv(f"{base}_REDIRECT_URIS", "")
    redirect_uris = [u.strip() for u in uris_csv.split(",") if u.strip()]

    scopes_csv = os.getenv(f"{base}_SCOPES", "")
    scopes = [s.strip() for s in scopes_csv.split(",") if s.strip()] or [
        "openid", "profile", "email"
    ]

    ttl_str = os.getenv(f"{base}_ACCESS_TOKEN_TTL_MINUTES", "").strip()
    try:
        ttl = int(ttl_str) if ttl_str else 1440
    except ValueError:
        ttl = 1440

    return {
        "client_id": client_id,
        "secret": secret,
        "name": os.getenv(f"{base}_NAME") or f"OAuth Client {client_id}",
        "description": os.getenv(f"{base}_DESCRIPTION") or None,
        "redirect_uris": redirect_uris,
        "allowed_scopes": scopes,
        "restricted_entity_id": (os.getenv(f"{base}_RESTRICTED_ENTITY_ID") or "").strip() or None,
        "access_token_ttl_minutes": ttl,
    }


async def seed_clients_from_env(db: AsyncSession) -> dict[str, str]:
    """Upsert OAuth clients discovered from env vars on Manor startup.

    Scans every MANOR_OAUTH_CLIENT_<SLUG>_SECRET env var; for each, reads
    the companion config vars and upserts an oauth_client_apps row.

    Idempotent. For each discovered client:
      - row absent → INSERT ('seeded')
      - row exists, all fields match → no-op ('unchanged')
      - row exists, hash matches but other fields differ → UPDATE ('updated')
      - row exists, hash differs → UPDATE ('rotated' — secret was changed)

    Per-client commit so one bad row doesn't roll back the rest. Returns
    a dict mapping client_id → action for logging.
    """
    actions: dict[str, str] = {}

    for slug in _discover_client_slugs():
        config = _read_client_config(slug)
        if config is None:
            continue  # secret env present but empty — _read_client_config returns None

        client_id = config["client_id"]

        # Refuse to seed without redirect URIs (would create a useless row)
        if not config["redirect_uris"]:
            actions[client_id] = (
                f"skipped:no_redirect_uris(set {_ENV_PREFIX}{slug}_REDIRECT_URIS)"
            )
            continue

        try:
            existing = (
                await db.execute(
                    select(OAuthClientApp).where(OAuthClientApp.client_id == client_id)
                )
            ).scalar_one_or_none()

            if existing is None:
                db.add(
                    OAuthClientApp(
                        client_id=client_id,
                        client_secret_hash=hash_client_secret(config["secret"]),
                        name=config["name"],
                        description=config["description"],
                        redirect_uris=config["redirect_uris"],
                        allowed_scopes=config["allowed_scopes"],
                        restricted_entity_id=config["restricted_entity_id"],
                        active=True,
                        access_token_ttl_minutes=config["access_token_ttl_minutes"],
                    )
                )
                actions[client_id] = "seeded"
            else:
                changed_fields: list[str] = []
                # Only rotate hash if the env secret doesn't already verify
                # against the stored hash — avoids regenerating bcrypt
                # (~100ms) on every restart when nothing changed.
                if not _verify_client_secret(config["secret"], existing.client_secret_hash):
                    existing.client_secret_hash = hash_client_secret(config["secret"])
                    changed_fields.append("secret")
                if sorted(existing.redirect_uris or []) != sorted(config["redirect_uris"]):
                    existing.redirect_uris = config["redirect_uris"]
                    changed_fields.append("redirect_uris")
                if sorted(existing.allowed_scopes or []) != sorted(config["allowed_scopes"]):
                    existing.allowed_scopes = config["allowed_scopes"]
                    changed_fields.append("scopes")
                if (existing.name or "") != config["name"]:
                    existing.name = config["name"]
                    changed_fields.append("name")
                if (existing.description or None) != config["description"]:
                    existing.description = config["description"]
                    changed_fields.append("description")
                if (existing.restricted_entity_id or None) != config["restricted_entity_id"]:
                    existing.restricted_entity_id = config["restricted_entity_id"]
                    changed_fields.append("restricted_entity_id")
                if existing.access_token_ttl_minutes != config["access_token_ttl_minutes"]:
                    existing.access_token_ttl_minutes = config["access_token_ttl_minutes"]
                    changed_fields.append("ttl")
                if not existing.active:
                    existing.active = True
                    changed_fields.append("active")
                if changed_fields:
                    actions[client_id] = (
                        "rotated" if "secret" in changed_fields else "updated"
                    ) + f"({','.join(changed_fields)})"
                else:
                    actions[client_id] = "unchanged"

            await db.commit()
        except Exception as e:  # noqa: BLE001 — surface each per-client error
            await db.rollback()
            logger.warning("OAuth client seed failed for %s: %s", client_id, e)
            actions[client_id] = f"error:{e.__class__.__name__}"

    return actions
