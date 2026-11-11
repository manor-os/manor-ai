"""OAuth 2.0 provider service — Manor as IdP for downstream apps.

Authorization Code flow only (no implicit, no client_credentials, optional/required S256 PKCE).

Public functions:
  - verify_client_credentials(client_id, client_secret) -> OAuthClientApp | None
  - get_client_by_id(client_id) -> OAuthClientApp | None
  - validate_redirect_uri(client, redirect_uri) -> bool
  - create_authorization_code(client, user, redirect_uri, scope, state) -> str
  - exchange_code_for_token(code, client_id, client_secret, redirect_uri) -> dict
  - refresh_access_token(refresh_token, client_id, client_secret) -> dict
  - revoke_refresh_token(token, client) -> None
  - seed_clients_from_env(db) -> dict[client_id, action]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import unquote

import bcrypt
from urllib.parse import urlsplit
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.oauth_provider import OAuthAuthorizationCode, OAuthClientApp, OAuthRefreshToken
from packages.core.models.user import User
from packages.core.services.actor_authorization import AuthenticatedUserCredential, resolve_current_user_actor
from packages.core.services.auth_service import create_access_token

logger = logging.getLogger(__name__)


# How long an authorization code lives before it can no longer be redeemed
AUTHORIZATION_CODE_TTL_MINUTES = 10

# Clients keep a session alive with rotating refresh tokens.
REFRESH_TOKEN_TTL_DAYS = 30

# Audience of tokens issued to dynamically registered clients: the hosted MCP
# endpoint accepts it, every REST route rejects it.
MCP_TOKEN_AUDIENCE = "manor-mcp"

# Scopes of the MCP resource. ``mcp`` reaches every tool; ``mcp:read`` only the
# read-only ones. A token without a scope claim predates scopes and means ``mcp``.
MCP_SCOPE = "mcp"
MCP_READ_SCOPE = "mcp:read"

# Some clients ask for this to be given a refresh token. Every grant already gets
# one, so it is accepted and never stored.
OFFLINE_ACCESS_SCOPE = "offline_access"

REGISTRATION_SOURCE_CONFIGURED = "configured"
REGISTRATION_SOURCE_DYNAMIC = "dynamic"
# A Client ID Metadata Document client (see oauth_client_metadata).
REGISTRATION_SOURCE_METADATA_DOCUMENT = "metadata_document"
# Clients created on demand and swept once unused.
_ON_DEMAND_SOURCES = (REGISTRATION_SOURCE_DYNAMIC, REGISTRATION_SOURCE_METADATA_DOCUMENT)


def granted_mcp_scopes(requested: list[str]) -> list[str]:
    """The MCP scope a sign-in grants: the widest known one requested, ``mcp`` by default.

    ``mcp`` already allows everything ``mcp:read`` does. Unknown values are
    dropped rather than refused (RFC 6749 §3.3 lets the server grant less).
    """
    base = MCP_SCOPE if MCP_SCOPE in requested or MCP_READ_SCOPE not in requested else MCP_READ_SCOPE
    return list(dict.fromkeys([base, *(value for value in requested
        if value in MCP_SCOPES and value not in (MCP_SCOPE, MCP_READ_SCOPE))]))


def mcp_token_scopes(claims: dict) -> set[str]:
    """Use typed grants, accepting the older resource-scoped string claim too."""
    typed = claims.get("scp")
    if typed:
        return set(typed)
    scope = claims.get("scope")
    return set(scope.split()) if isinstance(scope, str) and scope.strip() else {MCP_SCOPE}


def mcp_read_only(claims: dict) -> bool:
    """True when an MCP token grants only the read-only tools.

    Tokens issued before scopes existed carry none (or an empty one) and keep
    the full ``mcp`` grant their user consented to.
    """
    granted = mcp_token_scopes(claims)
    return MCP_READ_SCOPE in granted and MCP_SCOPE not in granted


def is_public_client(client: OAuthClientApp) -> bool:
    return not client.client_secret_hash


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
    db: AsyncSession, client_id: str, client_secret: Optional[str]
) -> Optional[OAuthClientApp]:
    """Authenticate a token-endpoint client.

    Confidential clients must present their secret. Public clients present none
    (PKCE proves possession instead); a secret sent by a public client fails.
    """
    client = await get_client_by_id(db, client_id)
    if not client or not client.active:
        return None
    if is_public_client(client):
        return None if client_secret else client
    if not _verify_client_secret(client_secret or "", client.client_secret_hash):
        return None
    return client


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _loopback_without_port(uri: str) -> Optional[tuple[str, str, str]]:
    """(host, path, query) of an http loopback redirect URI, else None."""
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme != "http" or parts.fragment or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    if parts.netloc.startswith("[") and host == "::1":
        host = "[::1]"
    if host not in _LOOPBACK_HOSTS:
        return None
    return host, parts.path or "/", parts.query


def is_loopback_redirect(uri: str) -> bool:
    """An http loopback callback: only the user's own machine can receive it."""
    return _loopback_without_port(uri) is not None


def validate_redirect_uri(client: OAuthClientApp, redirect_uri: str) -> bool:
    """Match the redirect URI against the client's whitelist.

    Exact match, except that a public client's loopback URI may use any port
    (RFC 8252 §7.3): native apps bind an ephemeral port per sign-in.
    """
    allowed = client.redirect_uris or []
    if redirect_uri in allowed:
        return True
    if not is_public_client(client):
        return False
    requested = _loopback_without_port(redirect_uri)
    return requested is not None and any(
        _loopback_without_port(uri) == requested for uri in allowed
    )


# ── Dynamic client registration (RFC 7591) ──

# Hosted (non-loopback) callbacks a self-registered client may use: the hosts'
# own sign-in callbacks, so a code issued there only ever reaches that host.
# Loopback http URIs are always allowed: only the user's own machine can
# receive them. MANOR_OAUTH_DYNAMIC_REDIRECT_URIS replaces this list; each
# comma-separated rule is an exact URI, ``prefix:<https URI ending in />`` or
# ``scheme:<name>`` (an RFC 8252 §7.1 private-use scheme of a native app).
_DEFAULT_DYNAMIC_REDIRECT_RULES = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect",
)

# Schemes a ``scheme:`` rule may never name: they are not private to an app.
_SHARED_SCHEMES = frozenset({"http", "https", "javascript", "data", "file", "about", "blob", "vbscript", "ftp"})
_SCHEME_NAME = re.compile(r"[a-z][a-z0-9+.-]*\Z")


@dataclass(frozen=True)
class RedirectRule:
    kind: str  # "exact" | "prefix" | "scheme"
    value: str

    def matches(self, uri: str) -> bool:
        if self.kind == "exact":
            return uri == self.value
        try:
            parts = urlsplit(uri)
            parts.port  # noqa: B018 — raises ValueError on a malformed port
        except ValueError:
            return False
        if parts.fragment or parts.username or parts.password:
            return False
        if self.kind == "scheme":
            return parts.scheme.lower() == self.value
        # A "prefix" callback must not climb out of the allowed path.
        if parts.scheme != "https" or any(seg in (".", "..") for seg in unquote(parts.path).split("/")):
            return False
        return f"https://{parts.netloc.lower()}{parts.path}".startswith(self.value)


def _parse_redirect_rule(text: str) -> Optional[RedirectRule]:
    if text.startswith("scheme:"):
        name = text[len("scheme:"):].strip().lower().rstrip(":/")
        if _SCHEME_NAME.fullmatch(name) and name not in _SHARED_SCHEMES:
            return RedirectRule("scheme", name)
    elif text.startswith("prefix:"):
        prefix = text[len("prefix:"):].strip()
        parts = urlsplit(prefix)
        if parts.scheme == "https" and parts.hostname and prefix.endswith("/") and not (parts.query or parts.fragment):
            return RedirectRule("prefix", f"https://{parts.netloc.lower()}{parts.path}")
    else:
        return RedirectRule("exact", text)
    logger.warning("Ignoring invalid OAuth redirect rule %r", text)
    return None


def dynamic_redirect_rules() -> tuple[RedirectRule, ...]:
    configured = os.getenv("MANOR_OAUTH_DYNAMIC_REDIRECT_URIS")
    texts = _DEFAULT_DYNAMIC_REDIRECT_RULES if configured is None else configured.split(",")
    rules = (_parse_redirect_rule(t.strip()) for t in texts if t.strip())
    return tuple(rule for rule in rules if rule is not None)


def hosted_redirect_allowed(uri: str) -> bool:
    return any(rule.matches(uri) for rule in dynamic_redirect_rules())


def _redirect_origin(uri: str) -> str:
    """scheme://host of a refused callback, for the log (never its path or query)."""
    try:
        parts = urlsplit(uri)
        return f"{parts.scheme}://{(parts.hostname or '')[:100]}"
    except ValueError:
        return "<unparseable>"


def log_refused_redirect(uri: str, client_name) -> None:
    # Logged so a new host's callback can be reviewed and allowed by config.
    logger.info(
        "OAuth registration refused callback %s (client_name=%r)",
        _redirect_origin(uri), str(client_name or "")[:MAX_CLIENT_NAME],
    )


class ClientRegistrationError(ValueError):
    """RFC 7591 §3.2.2 error: ``code`` is the OAuth error, ``description`` safe to echo."""

    def __init__(self, code: str, description: str):
        super().__init__(code)
        self.code = code
        self.description = description


MAX_CLIENT_NAME = 100
MAX_REDIRECT_URIS = 10


async def register_dynamic_client(db: AsyncSession, metadata: dict) -> OAuthClientApp:
    """Register a public PKCE client for the MCP resource (RFC 7591).

    Only public clients are issued (``token_endpoint_auth_method=none``); their
    tokens are bound to :data:`MCP_TOKEN_AUDIENCE`, so a self-registered client
    can never obtain a general REST API token.
    """
    redirect_uris = metadata.get("redirect_uris")
    if (not isinstance(redirect_uris, list) or not redirect_uris
            or len(redirect_uris) > MAX_REDIRECT_URIS
            or not all(isinstance(u, str) and len(u) <= 2048 for u in redirect_uris)):
        raise ClientRegistrationError("invalid_redirect_uri", "redirect_uris must list 1-10 URIs")
    for uri in redirect_uris:
        if not is_loopback_redirect(uri) and not hosted_redirect_allowed(uri):
            log_refused_redirect(uri, metadata.get("client_name"))
            raise ClientRegistrationError(
                "invalid_redirect_uri",
                "redirect URIs must be http loopback (localhost/127.0.0.1) or an allowed hosted callback",
            )

    auth_method = metadata.get("token_endpoint_auth_method", "none")
    if auth_method != "none":
        raise ClientRegistrationError("invalid_client_metadata", "only public clients (token_endpoint_auth_method=none) may register")
    grant_types = metadata.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or not set(grant_types) <= {"authorization_code", "refresh_token"}:
        raise ClientRegistrationError("invalid_client_metadata", "unsupported grant_types")
    response_types = metadata.get("response_types", ["code"])
    if not isinstance(response_types, list) or not set(response_types) <= {"code"}:
        raise ClientRegistrationError("invalid_client_metadata", "unsupported response_types")

    name = metadata.get("client_name")
    name = name.strip()[:MAX_CLIENT_NAME] if isinstance(name, str) and name.strip() else "MCP client"

    client = OAuthClientApp(
        client_id=f"mcp-{secrets.token_urlsafe(18)}",
        client_secret_hash=None,
        name=name,
        description=None,
        redirect_uris=list(dict.fromkeys(redirect_uris)),
        # A self-registered client may ask only for what the MCP server offers.
        allowed_scopes=list(MCP_SCOPES),
        active=True,
        require_pkce=True,
        require_verified_email=False,
        access_token_ttl_minutes=60,
        registration_source=REGISTRATION_SOURCE_DYNAMIC,
        token_audience=MCP_TOKEN_AUDIENCE,
    )
    db.add(client)
    await db.flush()
    return client


# What the MCP resource offers (advertised in its OAuth metadata).
MCP_SCOPES: tuple[str, ...] = (
    MCP_SCOPE, MCP_READ_SCOPE,
)


# RFC 7636: S256 is unpadded base64url(SHA256(verifier)); verifier is 43..128 unreserved ASCII.
_PKCE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_PKCE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z", re.ASCII)


def validate_pkce_authorization(client: OAuthClientApp, challenge: Optional[str], method: Optional[str]) -> None:
    if challenge is None and method is None:
        if client.require_pkce:
            raise ValueError("invalid_request:pkce_required")
        return
    if (method != "S256" or not isinstance(challenge, str)
            or not _PKCE_CHALLENGE.fullmatch(challenge)):
        raise ValueError("invalid_request:unsupported_or_invalid_pkce")


def _verify_pkce(client: OAuthClientApp, code: OAuthAuthorizationCode, verifier: Optional[str]) -> None:
    challenge, method = code.code_challenge, code.code_challenge_method
    if challenge is None and method is None:
        if client.require_pkce or verifier is not None:
            raise ValueError("invalid_grant")
        return
    if (method != "S256" or not isinstance(challenge, str)
            or not _PKCE_CHALLENGE.fullmatch(challenge)
            or not isinstance(verifier, str) or not _PKCE_VERIFIER.fullmatch(verifier)):
        raise ValueError("invalid_grant")
    derived = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(challenge, derived):
        raise ValueError("invalid_grant")


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
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
) -> OAuthAuthorizationCode:
    """Mint a short-lived authorization code bound to (client, user, redirect_uri)."""
    if client.restricted_entity_id and client.restricted_entity_id != user.entity_id:
        raise PermissionError("User's entity is not permitted for this OAuth client")

    from packages.core.services.user_email_proof import email_is_verified

    if client.require_verified_email and not email_is_verified(user):
        raise PermissionError("email_verification_required")

    validate_pkce_authorization(client, code_challenge, code_challenge_method)
    code_str = _generate_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=AUTHORIZATION_CODE_TTL_MINUTES)
    # /authorize echoes client-owned state directly; code redemption does not use it.
    # Leave the legacy nullable column empty: opaque relay state can exceed 255 chars.
    code = OAuthAuthorizationCode(
        code=code_str,
        client_id=client.client_id,
        user_id=user.id,
        entity_id=user.entity_id,
        redirect_uri=redirect_uri,
        scope=scope or [],
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
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
    client_secret: Optional[str],
    redirect_uri: str,
    code_verifier: Optional[str] = None,
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

    # Recheck current client restrictions as well as the original code binding.
    if code_row.redirect_uri != redirect_uri or not validate_redirect_uri(client, redirect_uri):
        raise ValueError("invalid_grant")
    if client.restricted_entity_id and client.restricted_entity_id != code_row.entity_id:
        raise ValueError("invalid_grant")

    # Expiry check
    expires = code_row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise ValueError("invalid_grant")

    _verify_pkce(client, code_row, code_verifier)

    # The code records the selected team, which can differ from User.entity_id.
    # Resolve current membership/Staff admission instead of falling back to the
    # user's primary team's role, including if access changed after consent.
    # Lock identity before actor resolution and the atomic code consume. Keep
    # this lock until the caller commits: email/proof changes must not overtake
    # a token response while membership queries or a code-row lock are awaited.
    # Refresh a User already present in this session's identity map as well.
    user_result = await db.execute(
        select(User).where(User.id == code_row.user_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        raise ValueError("invalid_grant")
    from packages.core.services.user_email_proof import email_is_verified

    if client.require_verified_email and not email_is_verified(user):
        raise ValueError("email_verification_required")
    actor = await resolve_current_user_actor(
        db,
        AuthenticatedUserCredential(
            user_id=user.id,
            entity_id=code_row.entity_id,
            token_version=int(user.token_version or 0),
        ),
    )
    if actor is None:
        raise ValueError("invalid_grant")

    # Competing requests may both have read an unused code. Only the transaction
    # that atomically consumes it may mint a token; the caller commits/rolls back.
    now = datetime.now(timezone.utc)
    redeemed = await db.execute(
        update(OAuthAuthorizationCode)
        .where(
            OAuthAuthorizationCode.id == code_row.id,
            OAuthAuthorizationCode.redeemed_at.is_(None),
            OAuthAuthorizationCode.expires_at > now,
        )
        .values(redeemed_at=now)
        .returning(OAuthAuthorizationCode.id)
        .execution_options(synchronize_session=False)
    )
    if redeemed.scalar_one_or_none() is None:
        raise ValueError("invalid_grant")

    result = _token_response(client, user, actor, code_row.scope or [])
    # Every client keeps its session with a rotating refresh token, whether or
    # not it asked for offline_access: hosts that don't ask would otherwise be
    # signed out when the hour-long access token expires.
    result["refresh_token"] = await _issue_refresh_token(
        db, client=client, user=user, entity_id=actor.entity_id,
        scope=code_row.scope or [], family_id=None,
    )
    return result


def _token_response(client: OAuthClientApp, user: User, actor, scope: list[str]) -> dict:
    from packages.core.services.user_email_proof import email_is_verified

    # OAuth clients receive the same short-lived, revocable API token as the
    # first-party web app, bound to the client's resource audience if it has
    # one. Access-token TTLs are capped at 60 minutes.
    ttl_minutes = min(max(1, client.access_token_ttl_minutes or 60), 60)
    token_scope = None
    if client.token_audience == MCP_TOKEN_AUDIENCE:
        scope = granted_mcp_scopes(scope)
        token_scope = " ".join(scope)
    access_token = create_access_token(
        user.id,
        actor.entity_id,
        actor.display_role,
        token_version=user.token_version,
        expires_minutes=ttl_minutes,
        audience=client.token_audience,
        scopes=scope,
        authorized_party=client.client_id,
        scope=token_scope,
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ttl_minutes * 60,
        "scope": " ".join(scope),
        # Convenience claims so the client doesn't have to decode the JWT
        "user": {
            "id": user.id,
            "email": user.email,
            "email_verified": email_is_verified(user),
            "name": user.display_name or user.email,
            "entity_id": actor.entity_id,
            "role": actor.display_role,
        },
    }


# ── Refresh tokens ──

def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _issue_refresh_token(
    db: AsyncSession,
    *,
    client: OAuthClientApp,
    user: User,
    entity_id: str,
    scope: list[str],
    family_id: Optional[str],
) -> str:
    from packages.core.models.base import generate_ulid

    token = secrets.token_urlsafe(48)
    db.add(OAuthRefreshToken(
        token_hash=_hash_refresh_token(token),
        family_id=family_id or generate_ulid(),
        client_id=client.client_id,
        user_id=user.id,
        entity_id=entity_id,
        scope=list(scope),
        token_version=int(user.token_version or 0),
        expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_TTL_DAYS),
    ))
    await db.flush()
    return token


class RefreshTokenReuse(ValueError):
    """A rotated refresh token was presented again; its family is now revoked.

    The caller must commit (persist the revocation) before reporting invalid_grant.
    """

    def __init__(self):
        super().__init__("invalid_grant")


async def refresh_access_token(
    db: AsyncSession,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: Optional[str] = None,
) -> dict:
    """Rotate a refresh token: revoke-on-reuse, re-admit the user, mint new tokens.

    Confidential clients authenticate with their secret here as at redemption.
    """
    client = await verify_client_credentials(db, client_id, client_secret)
    if not client:
        raise ValueError("invalid_client")

    row = (await db.execute(
        select(OAuthRefreshToken)
        .where(OAuthRefreshToken.token_hash == _hash_refresh_token(refresh_token or ""))
        .with_for_update()
    )).scalar_one_or_none()
    if row is None or row.client_id != client.client_id or row.revoked_at is not None:
        raise ValueError("invalid_grant")
    now = datetime.now(timezone.utc)
    if row.rotated_at is not None:
        await db.execute(
            update(OAuthRefreshToken)
            .where(OAuthRefreshToken.family_id == row.family_id, OAuthRefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        raise RefreshTokenReuse()
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise ValueError("invalid_grant")
    # The client's current restrictions apply to every renewal, as at redemption.
    if client.restricted_entity_id and client.restricted_entity_id != row.entity_id:
        raise ValueError("invalid_grant")

    user = (await db.execute(
        select(User).where(User.id == row.user_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if user is None or user.status != "active" or int(user.token_version or 0) != int(row.token_version):
        raise ValueError("invalid_grant")
    from packages.core.services.user_email_proof import email_is_verified

    if client.require_verified_email and not email_is_verified(user):
        raise ValueError("email_verification_required")
    actor = await resolve_current_user_actor(
        db,
        AuthenticatedUserCredential(
            user_id=user.id,
            entity_id=row.entity_id,
            token_version=int(user.token_version or 0),
        ),
    )
    if actor is None:
        raise ValueError("invalid_grant")

    rotated = await db.execute(
        update(OAuthRefreshToken)
        .where(OAuthRefreshToken.id == row.id, OAuthRefreshToken.rotated_at.is_(None),
               OAuthRefreshToken.revoked_at.is_(None))
        .values(rotated_at=now)
        .returning(OAuthRefreshToken.id)
        .execution_options(synchronize_session=False)
    )
    if rotated.scalar_one_or_none() is None:
        raise ValueError("invalid_grant")

    result = _token_response(client, user, actor, row.scope or [])
    result["refresh_token"] = await _issue_refresh_token(
        db, client=client, user=user, entity_id=actor.entity_id,
        scope=row.scope or [], family_id=row.family_id,
    )
    return result


async def revoke_refresh_token(db: AsyncSession, *, token: str, client: OAuthClientApp) -> None:
    """RFC 7009: revoke the family of a refresh token the client holds. The caller commits.

    A token that is unknown, already dead or another client's is left alone
    without saying so (RFC 7009 §2.2), so the endpoint reveals nothing.
    """
    row = (await db.execute(
        select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == _hash_refresh_token(token or ""))
    )).scalar_one_or_none()
    if row is None or row.client_id != client.client_id:
        return
    await db.execute(
        update(OAuthRefreshToken)
        .where(OAuthRefreshToken.family_id == row.family_id, OAuthRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )


# ── Connected clients (refresh-token families) ──

def _live_refresh_token(now: datetime):
    return and_(
        OAuthRefreshToken.revoked_at.is_(None),
        OAuthRefreshToken.rotated_at.is_(None),
        OAuthRefreshToken.expires_at > now,
    )


async def list_connected_clients(db: AsyncSession, user: User) -> list[dict]:
    """The user's live refresh-token families, one per signed-in client.

    ``connected_at`` is the family's first sign-in; ``last_used_at`` is when the
    client last obtained tokens (sign-in or refresh). Families from before the
    user's last "sign out everywhere" are dead and therefore not listed.
    """
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(OAuthRefreshToken, OAuthClientApp)
        .join(OAuthClientApp, OAuthClientApp.client_id == OAuthRefreshToken.client_id)
        .where(
            OAuthRefreshToken.user_id == user.id,
            OAuthRefreshToken.token_version == int(user.token_version or 0),
            _live_refresh_token(now),
        )
        .order_by(OAuthRefreshToken.created_at.desc())
    )).all()
    if not rows:
        return []
    families = [token.family_id for token, _ in rows]
    first_seen = dict((await db.execute(
        select(OAuthRefreshToken.family_id, func.min(OAuthRefreshToken.created_at))
        .where(OAuthRefreshToken.family_id.in_(families))
        .group_by(OAuthRefreshToken.family_id)
    )).all())
    return [
        {
            "id": token.family_id,
            "client_id": client.client_id,
            "name": client.name,
            # A metadata-document client is identified by the host serving it.
            "verified": client.registration_source != REGISTRATION_SOURCE_DYNAMIC,
            "entity_id": token.entity_id,
            "connected_at": first_seen.get(token.family_id) or token.created_at,
            "last_used_at": token.created_at,
            "expires_at": token.expires_at,
        }
        for token, client in rows
    ]


async def revoke_connected_client(db: AsyncSession, user: User, family_id: str) -> bool:
    """Revoke one of the user's refresh-token families. False when none was live.

    Access tokens already issued to that client stay valid until they expire
    (at most an hour); the client can no longer renew them.
    """
    revoked = await db.execute(
        update(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.family_id == family_id,
            OAuthRefreshToken.user_id == user.id,
            OAuthRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    return bool(revoked.rowcount)


# ── Unused dynamic client cleanup ──

def dynamic_client_unused_ttl_hours() -> int:
    try:
        return max(1, int(os.getenv("MANOR_OAUTH_DYNAMIC_CLIENT_TTL_HOURS", "24")))
    except ValueError:
        return 24


async def cleanup_unused_dynamic_clients(
    db: AsyncSession, *, now: Optional[datetime] = None, limit: int = 500,
) -> int:
    """Delete self-registered clients nobody is using. The caller commits.

    A dynamic or metadata-document client older than the TTL is unused when it
    has no live refresh token and no pending authorization code: it either never
    finished a sign-in or every session it had has expired or been revoked. A
    client that returns later simply registers (or is fetched) again.
    Configured clients are never touched.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=dynamic_client_unused_ttl_hours())
    live_token = select(OAuthRefreshToken.id).where(
        OAuthRefreshToken.client_id == OAuthClientApp.client_id,
        _live_refresh_token(now),
    )
    pending_code = select(OAuthAuthorizationCode.id).where(
        OAuthAuthorizationCode.client_id == OAuthClientApp.client_id,
        OAuthAuthorizationCode.redeemed_at.is_(None),
        OAuthAuthorizationCode.expires_at > now,
    )
    unused = (
        OAuthClientApp.registration_source.in_(_ON_DEMAND_SOURCES),
        OAuthClientApp.created_at < cutoff,
        ~live_token.exists(),
        ~pending_code.exists(),
    )
    candidates = list((await db.execute(
        select(OAuthClientApp.client_id).where(*unused)
        .order_by(OAuthClientApp.created_at).limit(limit)
    )).scalars())
    if not candidates:
        return 0
    # Re-check in the DELETE itself: a sign-in that started after the scan keeps its client.
    deleted = list((await db.execute(
        delete(OAuthClientApp)
        .where(OAuthClientApp.client_id.in_(candidates), *unused)
        .returning(OAuthClientApp.client_id)
        .execution_options(synchronize_session=False)
    )).scalars())
    if deleted:
        for model in (OAuthRefreshToken, OAuthAuthorizationCode):
            await db.execute(
                delete(model).where(model.client_id.in_(deleted))
                .execution_options(synchronize_session=False)
            )
    return len(deleted)


# ── Startup bootstrap from env vars ──

# Convention for registering a downstream app as an OAuth client of Manor:
#
#   MANOR_OAUTH_CLIENT_<SLUG>_SECRET=<plaintext bcrypt input>     (required)
#   MANOR_OAUTH_CLIENT_<SLUG>_NAME=<display name>                 (optional)
#   MANOR_OAUTH_CLIENT_<SLUG>_DESCRIPTION=<consent screen text>   (optional)
#   MANOR_OAUTH_CLIENT_<SLUG>_REDIRECT_URIS=<csv>                 (required for new clients)
#   MANOR_OAUTH_CLIENT_<SLUG>_SCOPES=<csv>                        (optional, defaults to openid,profile,email)
#   MANOR_OAUTH_CLIENT_<SLUG>_RESTRICTED_ENTITY_ID=<ulid>         (optional)
#   MANOR_OAUTH_CLIENT_<SLUG>_REQUIRE_PKCE=true|false            (optional; omitted preserves existing)
#   MANOR_OAUTH_CLIENT_<SLUG>_REQUIRE_VERIFIED_EMAIL=true|false  (optional; default false for new clients)
#   MANOR_OAUTH_CLIENT_<SLUG>_ACCESS_TOKEN_TTL_MINUTES=<int>      (optional, defaults to 1440)
#
# <SLUG> uses UPPERCASE_WITH_UNDERSCORES; the resulting client_id is
# <slug> lowercased with '_' → '-' (e.g. PMS_MANAGEMENT → pms-management).
# Override with MANOR_OAUTH_CLIENT_<SLUG>_CLIENT_ID if you need a different
# casing/format.
#
# Adding a new app (e.g. mobile portal) requires zero code change — just set
# the env vars through the deployment's secret injection mechanism.

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

    pkce_text = os.getenv(f"{base}_REQUIRE_PKCE")
    if pkce_text is not None and pkce_text.strip().lower() not in {"true", "false"}:
        raise ValueError("invalid_require_pkce_configuration")
    require_pkce = None if pkce_text is None else pkce_text.strip().lower() == "true"

    verified_text = os.getenv(f"{base}_REQUIRE_VERIFIED_EMAIL")
    if verified_text is not None and verified_text.strip().lower() not in {"true", "false"}:
        raise ValueError("invalid_require_verified_email_configuration")
    require_verified_email = None if verified_text is None else verified_text.strip().lower() == "true"

    return {
        "require_pkce": require_pkce,
        "require_verified_email": require_verified_email,
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
        try:
            config = _read_client_config(slug)
        except ValueError as exc:
            actions[_slug_to_client_id(slug)] = f"error:{exc}"
            continue
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
                        require_pkce=config["require_pkce"],
                        require_verified_email=config["require_verified_email"] or False,
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
                if config["require_pkce"] is not None and existing.require_pkce != config["require_pkce"]:
                    existing.require_pkce = config["require_pkce"]
                    changed_fields.append("require_pkce")
                if config["require_verified_email"] is not None and existing.require_verified_email != config["require_verified_email"]:
                    existing.require_verified_email = config["require_verified_email"]
                    changed_fields.append("require_verified_email")
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
