"""Provider-side OAuth client config (client_id / client_secret per provider).

Single source of truth = ``mcp_servers`` row:

  * ``default_config.oauth_client_id``     — non-secret, plaintext JSONB
  * ``default_config.oauth_scopes``        — optional override
  * ``default_config._oauth_source``       — "env" | "ui"
  * ``credential_ref`` + ``credential_scheme`` — Vault-encrypted secret

Env vars (``<PROVIDER>_CLIENT_ID`` / ``_CLIENT_SECRET``) are
**bootstrap-only**. On API startup, ``seed_oauth_clients_from_env`` reads
env and upserts into the DB. After that, the resolver only consults the
DB. Admin UI ``save_oauth_config`` always wins over env (sets
``_oauth_source = "ui"``).

The split:
  - OSS deploy: admin sets env in .env once, restart, bootstrap writes
    to DB. Same path as cloud.
  - Cloud deploy: Manor team sets env at platform level; bootstrap
    writes to DB on first boot. End-users never touch this layer.

Static per-provider metadata (authorize URL, token URL, scopes) is
hardcoded below — those never change per deployment.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_OAUTH_SECRET_DECRYPT_WARNED: set[str] = set()


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Resolved OAuth config for a provider — ready to drive a flow."""
    server_key: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: str                    # space-separated OAuth scopes
    redirect_path: str             # our callback path, relative to APP_URL
    source: str                    # "db" | "env" — where client creds came from


# ── Static per-provider OAuth endpoints + default scopes ─────────────────────

_PROVIDER_OAUTH_META: dict[str, dict[str, str]] = {
    "gmail": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": (
            "https://www.googleapis.com/auth/gmail.send "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.modify"
        ),
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "google_calendar": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": "https://www.googleapis.com/auth/calendar",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "google_drive": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": (
            "https://www.googleapis.com/auth/drive.file "
            "https://www.googleapis.com/auth/drive.readonly"
        ),
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "youtube": {
        # YouTube Data API rides on Google OAuth — same client creds as the
        # other Google services, just different scopes.
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": (
            "https://www.googleapis.com/auth/youtube.readonly "
            "https://www.googleapis.com/auth/youtube.force-ssl"
        ),
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "slack": {
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "scopes": "chat:write,channels:read,channels:history,users:read",
        "client_id_env": "SLACK_CLIENT_ID",
        "client_secret_env": "SLACK_CLIENT_SECRET",
    },
    "discord": {
        "authorize_url": "https://discord.com/api/oauth2/authorize",
        "token_url": "https://discord.com/api/oauth2/token",
        "scopes": "identify bot messages.read",
        "client_id_env": "DISCORD_CLIENT_ID",
        "client_secret_env": "DISCORD_CLIENT_SECRET",
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "scopes": "repo,read:org,user",
        "client_id_env": "GITHUB_CLIENT_ID",
        "client_secret_env": "GITHUB_CLIENT_SECRET",
    },
    "linkedin": {
        "authorize_url": "https://www.linkedin.com/oauth/v2/authorization",
        "token_url": "https://www.linkedin.com/oauth/v2/accessToken",
        # Member-level scopes always work after standard "Sign In with
        # LinkedIn" + "Share on LinkedIn" product approval.
        # Org-* scopes additionally require LinkedIn's "Community
        # Management API" partner approval — until that lands the
        # OAuth consent screen will silently drop them, but tools that
        # need them will return a clear 403 at call time.
        "scopes": (
            "w_member_social openid profile email "
            "r_organization_admin r_organization_social w_organization_social"
        ),
        "client_id_env": "LINKEDIN_CLIENT_ID",
        "client_secret_env": "LINKEDIN_CLIENT_SECRET",
    },
    "twitter_x": {
        "authorize_url": "https://x.com/i/oauth2/authorize",
        "token_url": "https://api.x.com/2/oauth2/token",
        "scopes": (
            "tweet.read tweet.write users.read like.read like.write "
            "follows.read follows.write offline.access"
        ),
        "client_id_env": "X_CLIENT_ID",
        "client_secret_env": "X_CLIENT_SECRET",
    },
    "tiktok": {
        # TikTok OAuth (Login Kit + Content Posting API). Non-standard:
        # the client identifier param is "client_key" (not "client_id") in
        # BOTH the authorize URL and the token body — see
        # _CLIENT_KEY_PROVIDERS below — and scopes are comma-separated.
        # These scopes back the tiktok MCP module (display reads + posting).
        "authorize_url": "https://www.tiktok.com/v2/auth/authorize/",
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "scopes": (
            "user.info.basic,user.info.profile,user.info.stats,"
            "video.list,video.publish,video.upload"
        ),
        "client_id_env": "TIKTOK_CLIENT_ID",
        "client_secret_env": "TIKTOK_CLIENT_SECRET",
    },
    "notion": {
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "scopes": "",  # Notion has no scopes field — app config drives access
        "client_id_env": "NOTION_CLIENT_ID",
        "client_secret_env": "NOTION_CLIENT_SECRET",
    },
    "quickbooks": {
        "authorize_url": "https://appcenter.intuit.com/connect/oauth2",
        "token_url": "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        "scopes": "com.intuit.quickbooks.accounting",
        "client_id_env": "QUICKBOOKS_CLIENT_ID",
        "client_secret_env": "QUICKBOOKS_CLIENT_SECRET",
    },
    "producthunt": {
        "authorize_url": "https://api.producthunt.com/v2/oauth/authorize",
        "token_url": "https://api.producthunt.com/v2/oauth/token",
        # ``public`` lets agents read posts + comments;
        # ``private`` is needed to post comments / vote as the user.
        "scopes": "public private",
        "client_id_env": "PRODUCTHUNT_CLIENT_ID",
        "client_secret_env": "PRODUCTHUNT_CLIENT_SECRET",
    },
    # ── Remote MCP servers (vendor-hosted, OAuth) ──
    # Stripe OAuth → bearer for mcp.stripe.com. The catalog row is
    # ``stripe`` (transport=http) — there's no longer a separate
    # ``stripe_mcp``; the legacy api_key wrapper has been retired.
    "stripe": {
        "authorize_url": "https://connect.stripe.com/oauth/authorize",
        "token_url": "https://connect.stripe.com/oauth/token",
        "scopes": "read_write",
        "client_id_env": "STRIPE_CLIENT_ID",
        "client_secret_env": "STRIPE_CLIENT_SECRET",
    },
    # ── Microsoft 365 (Graph API, single shared OAuth app) ──
    # All 5 MS modules (outlook / onedrive / ms_calendar / ms_teams /
    # ms_excel) share one Azure AD App Registration. The tenant
    # segment in the URL is configurable via ``MS_TENANT`` env (default
    # 'common' — accepts both AAD work/school + personal MSA accounts).
    **{
        _ms_key: {
            "authorize_url": (
                f"https://login.microsoftonline.com/"
                f"{os.getenv('MS_TENANT', 'common')}/oauth2/v2.0/authorize"
            ),
            "token_url": (
                f"https://login.microsoftonline.com/"
                f"{os.getenv('MS_TENANT', 'common')}/oauth2/v2.0/token"
            ),
            "scopes": _ms_scopes,
            "client_id_env": "MS_CLIENT_ID",
            "client_secret_env": "MS_CLIENT_SECRET",
        }
        for _ms_key, _ms_scopes in (
            (
                "outlook",
                "offline_access User.Read Mail.Read Mail.ReadWrite Mail.Send",
            ),
            (
                "onedrive",
                "offline_access User.Read Files.ReadWrite Files.ReadWrite.All",
            ),
            (
                "ms_calendar",
                "offline_access User.Read Calendars.ReadWrite MailboxSettings.Read",
            ),
            (
                "ms_teams",
                "offline_access User.Read Team.ReadBasic.All Channel.ReadBasic.All "
                "ChannelMessage.Read.All ChannelMessage.Send Chat.ReadWrite "
                "OnlineMeetings.ReadWrite Presence.ReadWrite",
            ),
            (
                "ms_excel",
                "offline_access User.Read Files.ReadWrite Files.ReadWrite.All",
            ),
        )
    },

    # PayPal OAuth — sandbox vs live picked at import time from
    # ``PAYPAL_ENVIRONMENT`` (default: sandbox). Same client_id/secret
    # env names; the URLs differ.
    "paypal": (
        {
            "authorize_url": "https://www.paypal.com/connect",
            "token_url": "https://api-m.paypal.com/v1/oauth2/token",
            "scopes": "openid profile email https://uri.paypal.com/services/payments/realtimepayment",
            "client_id_env": "PAYPAL_CLIENT_ID",
            "client_secret_env": "PAYPAL_CLIENT_SECRET",
        } if os.getenv("PAYPAL_ENVIRONMENT", "sandbox").lower() == "live" else {
            "authorize_url": "https://www.sandbox.paypal.com/connect",
            "token_url": "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            "scopes": "openid profile email https://uri.paypal.com/services/payments/realtimepayment",
            "client_id_env": "PAYPAL_CLIENT_ID",
            "client_secret_env": "PAYPAL_CLIENT_SECRET",
        }
    ),
}


def is_oauth_provider(server_key: str) -> bool:
    return server_key in _PROVIDER_OAUTH_META


def oauth_client_configured(server_key: str, server: object | None = None) -> bool:
    """Return whether a provider appears configured without decrypting secrets.

    Catalog/listing endpoints only need to know whether a Connect CTA should
    be available. Calling ``resolve_oauth_config`` there leases the encrypted
    client secret and can block on Vault for every page load.
    """
    meta = _PROVIDER_OAUTH_META.get(server_key)
    if not meta:
        return False

    env_client_id = os.getenv(meta["client_id_env"], "").strip()
    env_client_secret = os.getenv(meta["client_secret_env"], "").strip()
    if env_client_id and env_client_secret:
        return True

    if server is None:
        return False

    cfg = getattr(server, "default_config", None)
    if not isinstance(cfg, dict):
        cfg = {}
    client_id = str(cfg.get("oauth_client_id") or "").strip()
    secret_present = bool(
        str(getattr(server, "credential_ref", "") or "").strip()
        or str(cfg.get("oauth_client_secret") or "").strip()
    )
    return bool(client_id and secret_present)


def _warn_oauth_secret_decrypt_failed(
    server_key: str,
    exc: Exception,
    *,
    using_env_fallback: bool,
) -> None:
    if server_key in _OAUTH_SECRET_DECRYPT_WARNED:
        return
    _OAUTH_SECRET_DECRYPT_WARNED.add(server_key)
    outcome = (
        "using env OAuth credentials as a fallback"
        if using_env_fallback
        else "treating provider as unconfigured until the secret is refreshed"
    )
    logger.warning(
        "OAuth client secret decrypt failed for %s: %s; %s",
        server_key,
        exc,
        outcome,
    )


def _warn_oauth_secret_lease_failed(
    server_key: str,
    exc: Exception,
    *,
    using_env_fallback: bool,
) -> None:
    if server_key in _OAUTH_SECRET_DECRYPT_WARNED:
        return
    _OAUTH_SECRET_DECRYPT_WARNED.add(server_key)
    outcome = (
        "using env OAuth credentials as a fallback"
        if using_env_fallback
        else "treating provider as unconfigured until the credential backend recovers"
    )
    logger.warning(
        "OAuth client secret lease failed for %s: %s; %s",
        server_key,
        exc,
        outcome,
    )


# Providers whose token endpoint requires HTTP Basic auth for confidential
# clients and rejects body-only credentials with 401 unauthorized_client.
# Twitter/X is the canonical case — its docs state Basic auth is mandatory
# and including ``client_secret`` in the body returns "Missing valid
# authorization header".
_BASIC_AUTH_PROVIDERS: frozenset[str] = frozenset({"twitter_x"})

# Providers that name the client identifier ``client_key`` instead of the
# OAuth-standard ``client_id`` — in BOTH the authorize URL query and the token
# request body. TikTok is the canonical case: its authorize and token
# endpoints reject ``client_id`` with a 400 / invalid_request.
_CLIENT_KEY_PROVIDERS: frozenset[str] = frozenset({"tiktok"})


def apply_authorize_param_conventions(
    config: "OAuthProviderConfig",
    params: dict[str, str],
) -> dict[str, str]:
    """Adjust authorize-URL query params for provider-specific naming.

    For ``_CLIENT_KEY_PROVIDERS`` the standard ``client_id`` key is renamed
    to ``client_key`` (TikTok rejects ``client_id``). Returns a new dict;
    the input is left untouched.
    """
    out = dict(params)
    if config.server_key in _CLIENT_KEY_PROVIDERS and "client_id" in out:
        out["client_key"] = out.pop("client_id")
    return out


def build_token_request_auth(
    config: "OAuthProviderConfig",
    body: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Apply provider-specific auth conventions to an OAuth token request.

    Returns ``(headers, body)`` ready for ``httpx.post(..., data=body,
    headers=headers)``. For providers in ``_BASIC_AUTH_PROVIDERS`` the
    credentials move from the body into an ``Authorization: Basic`` header
    and ``client_secret`` is stripped from the body (X rejects requests
    that include both). For ``_CLIENT_KEY_PROVIDERS`` the body's
    ``client_id`` is renamed to ``client_key`` (TikTok).
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    out_body = dict(body)

    if config.server_key in _BASIC_AUTH_PROVIDERS:
        token = base64.b64encode(
            f"{config.client_id}:{config.client_secret}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {token}"
        out_body.pop("client_secret", None)

    if config.server_key in _CLIENT_KEY_PROVIDERS and "client_id" in out_body:
        out_body["client_key"] = out_body.pop("client_id")

    return headers, out_body


async def resolve_oauth_config(
    db: AsyncSession, server_key: str,
) -> Optional[OAuthProviderConfig]:
    """Return a complete OAuthProviderConfig, or None if the provider
    isn't configured in this deployment.

    DB-first resolution. Env vars are normally bootstrap-only (see
    seed_oauth_clients_from_env), but can still recover env-seeded rows
    whose encrypted ref became unreadable.
    """
    meta = _PROVIDER_OAUTH_META.get(server_key)
    if not meta:
        return None

    from packages.core.models.mcp import MCPServer
    server = (await db.execute(
        select(MCPServer).where(MCPServer.server_key == server_key)
    )).scalar_one_or_none()

    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    secret_decrypt_error: Exception | None = None
    secret_lease_error: Exception | None = None
    source = "env"
    scopes = meta["scopes"]
    env_client_id = os.getenv(meta["client_id_env"], "").strip() or None
    env_client_secret = os.getenv(meta["client_secret_env"], "").strip() or None

    if server:
        cfg = server.default_config if isinstance(server.default_config, dict) else {}
        client_id = cfg.get("oauth_client_id") or None
        scope_override = cfg.get("oauth_scopes")
        if scope_override:
            scopes = str(scope_override)
        if client_id:
            source = (cfg.get("_oauth_source") or "db").strip() or "db"

        # Vault-backed secret (preferred path) → fall through to legacy
        # plaintext in default_config if credential_ref is empty.
        if server.credential_ref:
            from packages.core.credentials import (
                CredentialDecryptError,
                CredentialNotFound,
                get_credential_service,
                Requester,
            )
            try:
                creds = get_credential_service().lease_mcp_server(
                    server,
                    requester=Requester(kind="system", id="oauth_resolve"),
                    reason=f"oauth_resolve:{server_key}",
                )
                client_secret = (creds or {}).get("oauth_client_secret") or None
            except (CredentialDecryptError, CredentialNotFound) as exc:
                secret_decrypt_error = exc
            except Exception as exc:  # noqa: BLE001
                secret_lease_error = exc
        elif isinstance(cfg, dict):
            client_secret = cfg.get("oauth_client_secret") or None

    # Pre-bootstrap/env-recovery fallback: read env as a pair so we never
    # accidentally combine a DB client_id with an unrelated env secret.
    using_env_fallback = False
    if (not client_id or not client_secret) and env_client_id and env_client_secret:
        env_matches_db_id = bool(client_id and env_client_id == client_id)
        db_row_is_env_seeded = bool(server and source == "env")
        can_use_env_pair = (
            not server
            or not client_id
            or db_row_is_env_seeded
            or env_matches_db_id
        )
        if can_use_env_pair:
            client_id = env_client_id
            client_secret = env_client_secret
            source = "env"
            using_env_fallback = True

    if secret_decrypt_error is not None:
        _warn_oauth_secret_decrypt_failed(
            server_key,
            secret_decrypt_error,
            using_env_fallback=using_env_fallback,
        )
    if secret_lease_error is not None:
        _warn_oauth_secret_lease_failed(
            server_key,
            secret_lease_error,
            using_env_fallback=using_env_fallback,
        )

    if not client_id or not client_secret:
        return None

    return OAuthProviderConfig(
        server_key=server_key,
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=meta["authorize_url"],
        token_url=meta["token_url"],
        scopes=scopes,
        redirect_path=f"/api/v1/integrations/oauth/{server_key}/callback",
        source=source,
    )


async def save_oauth_config(
    db: AsyncSession,
    server_key: str,
    *,
    client_id: str,
    client_secret: str,
    scopes: Optional[str] = None,
) -> bool:
    """Persist OAuth client credentials onto the MCPServer row.

    client_id + scopes go in default_config (plaintext); client_secret
    is encrypted via CredentialService into credential_ref. Used by the
    admin UI — sets ``_oauth_source = "ui"`` so future env-bootstrap
    runs don't overwrite it. No-op if server_key isn't a real
    MCPServer row.
    """
    from packages.core.models.mcp import MCPServer
    from packages.core.credentials import get_credential_service

    server = (await db.execute(
        select(MCPServer).where(MCPServer.server_key == server_key)
    )).scalar_one_or_none()
    if not server:
        return False

    cfg = dict(server.default_config or {})
    cfg["oauth_client_id"] = client_id
    cfg["_oauth_source"] = "ui"
    if scopes:
        cfg["oauth_scopes"] = scopes
    cfg.pop("oauth_client_secret", None)  # never store plaintext
    server.default_config = cfg
    get_credential_service().store_mcp_server(
        server, {"oauth_client_secret": client_secret},
    )
    await db.flush()
    return True


async def seed_oauth_clients_from_env(db: AsyncSession) -> dict[str, str]:
    """Bootstrap MCPServer rows from env vars on startup.

    For each provider in ``_PROVIDER_OAUTH_META``: if the env client_id
    + secret are set, upsert them into the MCPServer row with
    ``_oauth_source = "env"``. UI-set rows
    (``_oauth_source = "ui"``) are left alone — admin overrides win.

    Returns a {server_key: action} map for logging:
      "seeded"     — first-time write
      "refreshed"  — env values changed since last seed
      "skipped_ui" — admin override exists, env ignored
      "missing"    — env empty, nothing to do
      "no_row"     — catalog row not present yet (rare race)
      "error: ..." — write failed; remaining providers still attempted

    Each provider is committed independently so a single failure (e.g.
    a schema overflow on one provider's credential_ref) doesn't poison
    the session for everything iterated after it. The dict iteration
    over ``_PROVIDER_OAUTH_META`` is alphabetical; a per-row failure
    used to drop all later providers silently because the outer
    transaction aborted on the first error and the surrounding
    ``try/except`` in ``apps/api/main.py`` ate the partial state.
    """
    from packages.core.models.mcp import MCPServer
    from packages.core.credentials import get_credential_service, Requester

    actions: dict[str, str] = {}
    cs = get_credential_service()
    configured_server_keys = [
        server_key
        for server_key, meta in _PROVIDER_OAUTH_META.items()
        if os.getenv(meta["client_id_env"], "").strip()
        and os.getenv(meta["client_secret_env"], "").strip()
    ]
    if configured_server_keys:
        # Startup self-check: surface the exact callback each provider sends to
        # its OAuth console, so a redirect_uri_mismatch is a copy-paste fix
        # rather than a guess. Each Google-based provider (gmail / youtube /
        # google_drive) uses its own per-key callback and must be whitelisted
        # individually.
        app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
        logger.info(
            "OAuth redirect URIs to whitelist in each provider's console:\n%s",
            "\n".join(
                f"  {key}: {app_url}/api/v1/integrations/oauth/{key}/callback"
                for key in configured_server_keys
            ),
        )
        health = await asyncio.to_thread(cs.health)
        if not health.ok:
            detail = health.detail or "credential backend unavailable"
            for server_key, meta in _PROVIDER_OAUTH_META.items():
                env_id = os.getenv(meta["client_id_env"], "").strip()
                env_secret = os.getenv(meta["client_secret_env"], "").strip()
                if env_id and env_secret:
                    actions[server_key] = f"error: CredentialBackendUnavailable: {detail}"
                else:
                    actions[server_key] = "missing"
            return actions

    for server_key, meta in _PROVIDER_OAUTH_META.items():
        try:
            env_id = os.getenv(meta["client_id_env"], "").strip()
            env_secret = os.getenv(meta["client_secret_env"], "").strip()
            if not env_id or not env_secret:
                actions[server_key] = "missing"
                continue

            server = (await db.execute(
                select(MCPServer).where(MCPServer.server_key == server_key)
            )).scalar_one_or_none()
            if not server:
                # No MCPServer row yet (mcp_seed runs first, but skip
                # gracefully if the catalog hasn't reached this provider).
                actions[server_key] = "no_row"
                continue

            cfg = dict(server.default_config or {})
            existing_source = cfg.get("_oauth_source")
            if existing_source == "ui":
                actions[server_key] = "skipped_ui"
                continue

            # Compare env values against current state. If nothing
            # changed, skip the write to avoid touching credential_ref
            # unnecessarily (Vault writes log audit events).
            existing_id = cfg.get("oauth_client_id")
            existing_secret = None
            if server.credential_ref:
                try:
                    leased = cs.lease_mcp_server(
                        server,
                        requester=Requester(kind="system", id="oauth_seed"),
                        reason="oauth_seed: compare env",
                    )
                    existing_secret = (leased or {}).get("oauth_client_secret")
                except Exception:  # noqa: BLE001
                    existing_secret = None
            else:
                existing_secret = cfg.get("oauth_client_secret")

            if existing_id == env_id and existing_secret == env_secret:
                actions[server_key] = "unchanged"
                continue

            cfg["oauth_client_id"] = env_id
            cfg["_oauth_source"] = "env"
            cfg.pop("oauth_client_secret", None)
            server.default_config = cfg
            cs.store_mcp_server(server, {"oauth_client_secret": env_secret})

            # Commit this provider before the next iteration. Without
            # the per-provider commit the session would stay in a
            # transactional state where one provider's failure aborts
            # autoflush for everyone iterated after it.
            await db.commit()
            actions[server_key] = "refreshed" if existing_id else "seeded"
        except Exception as exc:  # noqa: BLE001
            # Roll back so the next provider starts with a fresh
            # session, then record the failure for the caller to log.
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            actions[server_key] = f"error: {type(exc).__name__}: {exc}"

    return actions
