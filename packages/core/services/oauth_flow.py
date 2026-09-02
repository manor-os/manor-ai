"""Shared OAuth 2.0 flow helpers — start + callback machinery.

Pulled out of ``apps/api/routers/integrations.py`` so the router
endpoints become trivial wrappers and the same primitives can drive
admin-side test flows or CLI utilities later.

What lives here
───────────────
* ``begin_authorization()`` — generates ``state`` + PKCE code verifier
  + code challenge, stores them in the shared pending-state store, returns
  a fully-built authorize URL.
* ``complete_authorization()`` — pops the pending state, validates it,
  exchanges the code for an access token (PKCE-aware), returns a
  typed ``TokenSet``. Raises ``OAuthFlowError`` on every failure mode.
* ``render_oauth_error_page()`` — uniform HTML page when the provider
  redirects back with ``?error=...``. Same look across providers.

State store
───────────
Pending state is stored in Redis with a short TTL and consumed atomically so
callbacks can land on any API replica or worker. Single-process OSS
development falls back to an in-memory store when Redis is unavailable.

PKCE
────
Flows use ``code_challenge_method=S256`` by default. Providers that require it
(Twitter v2 user-context) receive the verifier. Facebook follows Meta's
documented server-side flow and omits PKCE plus Google-only prompt parameters.
See RFC 7636.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi.responses import HTMLResponse

from packages.core.external_api_versions import META_GRAPH

logger = logging.getLogger(__name__)

_OAUTH_STATE_TTL_SECONDS = 10 * 60
_OAUTH_STATE_KEY_PREFIX = "manor:oauth:state:"
_CONSUME_OAUTH_STATE_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if value then
    redis.call('DEL', KEYS[1])
end
return value
"""


# ── Public types ───────────────────────────────────────────────────────────


@dataclass
class AuthorizationStart:
    """Result of ``begin_authorization`` — what the router needs to
    return so the client can pop the provider's consent screen."""
    authorize_url: str
    state: str
    server_key: str


@dataclass
class TokenSet:
    """Normalised view of a successful token-exchange response. Only
    the fields most consumers care about are typed; the raw provider
    response stays on ``raw`` for anything provider-specific."""
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[datetime]
    provider_user_id: str
    raw: Dict[str, Any] = field(default_factory=dict)


class OAuthFlowError(Exception):
    """Surface to the router so it can pick the right HTTP status. A
    400 for client/state errors, 502 for upstream failures."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


# ── State store ────────────────────────────────────────────────────────────


_pending_oauth_states: Dict[str, tuple[float, Dict[str, str]]] = {}


def _load_process_pending(
    state: str,
    *,
    consume: bool,
) -> Dict[str, str] | None:
    now = time.monotonic()
    expired = [
        key
        for key, (expires_at, _pending) in _pending_oauth_states.items()
        if expires_at <= now
    ]
    for key in expired:
        _pending_oauth_states.pop(key, None)
    entry = (
        _pending_oauth_states.pop(state, None)
        if consume
        else _pending_oauth_states.get(state)
    )
    return entry[1] if entry is not None else None


def _shared_state_store_required() -> bool:
    from packages.core.config import get_settings

    settings = get_settings()
    return (
        str(settings.DEPLOYMENT_MODE).strip().lower() == "cloud"
        or settings.API_WORKERS > 1
    )


def _decode_pending_state(value: object) -> Dict[str, str] | None:
    try:
        decoded = json.loads(value) if isinstance(value, (str, bytes)) else value
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    return {
        str(key): str(item)
        for key, item in decoded.items()
        if item is not None
    }


async def _redis_client():
    from packages.core.cache import _get_redis

    return await _get_redis()


async def _store_pending(
    state: str,
    *,
    user_id: str,
    server_key: str,
    code_verifier: str,
    entity_id: str | None = None,
    return_to: str | None = None,
    connection_id: str | None = None,
) -> None:
    pending = {
        "user_id": user_id,
        "server_key": server_key,
        "code_verifier": code_verifier,
    }
    if return_to:
        pending["return_to"] = return_to
    if entity_id:
        pending["entity_id"] = entity_id
    if connection_id:
        pending["connection_id"] = connection_id
    redis = await _redis_client()
    if redis is None:
        if _shared_state_store_required():
            raise OAuthFlowError(503, "OAuth state store is temporarily unavailable")
        _load_process_pending(state, consume=True)
        _pending_oauth_states[state] = (
            time.monotonic() + _OAUTH_STATE_TTL_SECONDS,
            pending,
        )
        return
    try:
        stored = await redis.set(
            f"{_OAUTH_STATE_KEY_PREFIX}{state}",
            json.dumps(pending),
            ex=_OAUTH_STATE_TTL_SECONDS,
            nx=True,
        )
    except Exception as exc:
        logger.exception("Failed to store OAuth state")
        raise OAuthFlowError(
            503,
            "OAuth state store is temporarily unavailable",
        ) from exc
    if not stored:
        raise OAuthFlowError(503, "Could not allocate a unique OAuth state")


async def _load_pending(state: str, *, consume: bool) -> Dict[str, str] | None:
    redis = await _redis_client()
    if redis is None:
        if _shared_state_store_required():
            raise OAuthFlowError(503, "OAuth state store is temporarily unavailable")
        return _load_process_pending(state, consume=consume)
    key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
    try:
        value = (
            await redis.eval(_CONSUME_OAUTH_STATE_SCRIPT, 1, key)
            if consume
            else await redis.get(key)
        )
    except Exception as exc:
        logger.exception("Failed to read OAuth state")
        raise OAuthFlowError(
            503,
            "OAuth state store is temporarily unavailable",
        ) from exc
    pending = _decode_pending_state(value)
    if pending is not None:
        return pending
    # Redis may have become available after an OSS single-process flow began.
    return _load_process_pending(state, consume=consume)


async def _pop_pending(state: str, *, server_key: str) -> Dict[str, str]:
    pending = await _load_pending(state, consume=True)
    if not pending or pending.get("server_key") != server_key:
        raise OAuthFlowError(400, "Invalid or expired OAuth state")
    return pending


async def get_pending_state(state: str, *, server_key: str) -> Dict[str, str]:
    """Read pending callback context without consuming its one-time state."""
    pending = await _load_pending(state, consume=False)
    if not pending or pending.get("server_key") != server_key:
        raise OAuthFlowError(400, "Invalid or expired OAuth state")
    return pending


async def validate_pending_state(state: str, *, server_key: str) -> None:
    """Validate state before any provider/config side effects.

    Callback handlers use this to fail closed on forged states even if
    the deployment has not configured that provider yet. The actual
    completion path still pops the state later to preserve one-time use.
    """
    await get_pending_state(state, server_key=server_key)


async def get_pending_return_to(state: str, *, server_key: str) -> str | None:
    """Return the caller-provided post-OAuth path after validating state."""
    pending = await get_pending_state(state, server_key=server_key)
    return pending.get("return_to")


async def get_pending_connection_id(state: str, *, server_key: str) -> str | None:
    """Return the OAuthAccount row explicitly being reconnected, if any."""
    pending = await get_pending_state(state, server_key=server_key)
    return pending.get("connection_id")


async def get_pending_entity_id(state: str, *, server_key: str) -> str | None:
    """Return the Entity that initiated this OAuth flow, if one was supplied."""
    pending = await get_pending_state(state, server_key=server_key)
    return pending.get("entity_id")


# ── Authorization step ─────────────────────────────────────────────────────


async def begin_authorization(
    *,
    config: Any,           # OAuthProviderConfig — typed loosely to avoid import cycle
    user_id: str,
    redirect_uri: str,
    entity_id: str | None = None,
    return_to: str | None = None,
    connection_id: str | None = None,
) -> AuthorizationStart:
    """Build the provider's authorize URL for ``user_id``.

    Generates a fresh ``state`` and a PKCE pair, stashes the verifier
    against ``state`` in the pending-state store, and returns the URL. The
    caller (router) is unchanged in shape.
    """
    state = secrets.token_urlsafe(24)

    from packages.core.services.oauth_provider_config import (
        oauth_provider_uses_pkce,
    )

    uses_pkce = oauth_provider_uses_pkce(config.server_key)
    # Twitter/X v2 mandates PKCE. Confidential server-side flows must omit
    # both halves of the pair.
    code_verifier = secrets.token_urlsafe(64) if uses_pkce else ""
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        if uses_pkce
        else ""
    )

    await _store_pending(
        state,
        user_id=user_id,
        server_key=config.server_key,
        code_verifier=code_verifier,
        entity_id=entity_id,
        return_to=return_to,
        connection_id=connection_id,
    )

    params: Dict[str, str] = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": config.scopes,
        "state": state,
        "access_type": "offline",     # Google: get refresh_token
        "prompt": "consent",          # force re-consent so refresh_token is returned
    }
    if uses_pkce:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    # Provider-specific param naming (e.g. TikTok wants client_key, not
    # client_id). No-op for standard providers.
    from packages.core.services.oauth_provider_config import (
        apply_authorize_param_conventions,
    )
    params = apply_authorize_param_conventions(config, params)
    authorize_url = f"{config.authorize_url}?{urlencode(params)}"
    return AuthorizationStart(
        authorize_url=authorize_url,
        state=state,
        server_key=config.server_key,
    )


# ── Callback step ──────────────────────────────────────────────────────────


async def complete_authorization(
    *,
    server_key: str,
    code: str,
    state: str,
    redirect_uri: str,
    config: Any,           # OAuthProviderConfig
    timeout: float = 20.0,
) -> tuple[str, TokenSet]:
    """Pop the pending state, exchange ``code`` for tokens, return
    ``(user_id, TokenSet)``. Raises ``OAuthFlowError`` on any failure.

    The router persists the token bundle and renders a redirect; this
    function intentionally does not touch the database so it can be
    reused from CLI tools / tests.
    """
    pending = await _pop_pending(state, server_key=server_key)
    user_id = pending["user_id"]
    code_verifier = pending.get("code_verifier", "")

    body: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }
    from packages.core.services.oauth_provider_config import (
        build_token_request_auth,
        oauth_provider_uses_pkce,
    )

    if code_verifier and oauth_provider_uses_pkce(server_key):
        body["code_verifier"] = code_verifier

    headers, body = build_token_request_auth(config, body)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if server_key == "facebook":
                # Meta documents the server-side code exchange as a GET to
                # /oauth/access_token. Keep secrets out of logs at the caller
                # and let httpx encode the query safely.
                resp = await client.get(
                    config.token_url,
                    params=body,
                    headers=headers,
                )
            elif server_key == "notion":
                resp = await client.post(
                    config.token_url,
                    json=body,
                    headers=headers,
                )
            else:
                resp = await client.post(
                    config.token_url,
                    data=body,
                    headers=headers,
                )
    except Exception as exc:
        raise OAuthFlowError(502, f"Token exchange failed: {exc}") from exc

    if resp.status_code >= 400:
        raise OAuthFlowError(
            400,
            f"{server_key} token exchange returned {resp.status_code}: "
            f"{resp.text[:200]}",
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise OAuthFlowError(502, "Provider returned non-JSON token response") from exc

    access_token = data.get("access_token")
    if not access_token:
        raise OAuthFlowError(
            400, f"Provider did not return an access_token: {data}",
        )

    if server_key == "facebook":
        # The first token is short-lived. Exchange it immediately so Page,
        # Messenger, and Instagram automations remain usable for roughly the
        # full Meta long-lived-token window instead of expiring after an hour.
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                long_resp = await client.get(
                    config.token_url,
                    params={
                        "grant_type": "fb_exchange_token",
                        "client_id": config.client_id,
                        "client_secret": config.client_secret,
                        "fb_exchange_token": access_token,
                    },
                    headers={"Accept": "application/json"},
                )
        except Exception as exc:
            raise OAuthFlowError(
                502, f"Facebook long-lived token exchange failed: {exc}",
            ) from exc
        if long_resp.status_code >= 400:
            raise OAuthFlowError(
                400,
                "Facebook long-lived token exchange was rejected "
                f"(HTTP {long_resp.status_code}).",
            )
        try:
            long_data = long_resp.json()
        except Exception as exc:
            raise OAuthFlowError(
                502, "Facebook returned a non-JSON long-lived token response",
            ) from exc
        if not long_data.get("access_token"):
            raise OAuthFlowError(
                400, "Facebook did not return a long-lived access token.",
            )
        data = {**data, **long_data}
        access_token = data["access_token"]

    expires_in = data.get("expires_in")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        if expires_in else None
    )

    return user_id, TokenSet(
        access_token=access_token,
        refresh_token=data.get("refresh_token"),
        expires_at=expires_at,
        provider_user_id=str(
            data.get("user_id") or data.get("open_id") or data.get("id") or ""
        ),
        raw=data,
    )


_PROFILE_ENDPOINTS: dict[str, tuple[str, dict[str, str] | None]] = {
    "gmail": ("https://openidconnect.googleapis.com/v1/userinfo", None),
    "google_calendar": ("https://openidconnect.googleapis.com/v1/userinfo", None),
    "google_drive": ("https://openidconnect.googleapis.com/v1/userinfo", None),
    "youtube": ("https://openidconnect.googleapis.com/v1/userinfo", None),
    "github": ("https://api.github.com/user", None),
    "facebook": (
        f"https://graph.facebook.com/{META_GRAPH.value}/me",
        {"fields": "id,name"},
    ),
    "discord": ("https://discord.com/api/users/@me", None),
    "linkedin": ("https://api.linkedin.com/v2/userinfo", None),
    "twitter_x": ("https://api.x.com/2/users/me", {"user.fields": "name,username"}),
    "slack": ("https://slack.com/api/auth.test", None),
    "outlook": ("https://graph.microsoft.com/v1.0/me", None),
    "onedrive": ("https://graph.microsoft.com/v1.0/me", None),
    "ms_calendar": ("https://graph.microsoft.com/v1.0/me", None),
    "ms_teams": ("https://graph.microsoft.com/v1.0/me", None),
    "ms_excel": ("https://graph.microsoft.com/v1.0/me", None),
}


def _nested_value(data: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current
    return None


def _identity_from_payload(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    provider_user_id = _nested_value(
        payload,
        ("user_id",),
        ("open_id",),
        ("sub",),
        ("id",),
        ("stripe_user_id",),
        ("authed_user", "id"),
        ("user", "id"),
        ("user", "open_id"),
        ("data", "id"),
        ("data", "user", "open_id"),
    )
    email = _nested_value(
        payload,
        ("email",),
        ("mail",),
        ("userPrincipalName",),
        ("data", "email"),
        ("user", "email"),
    )
    display_name = _nested_value(
        payload,
        ("display_name",),
        ("displayName",),
        ("name",),
        ("username",),
        ("login",),
        ("real_name",),
        ("team", "name"),
        ("data", "name"),
        ("data", "username"),
        ("data", "user", "display_name"),
    )
    profile = {
        key: value
        for key, value in {
            "email": email,
            "display_name": display_name,
        }.items()
        if value not in (None, "")
    }
    return (
        str(provider_user_id).strip() if provider_user_id not in (None, "") else None,
        profile,
    )


def _slack_installation_profile(payload: dict[str, Any]) -> dict[str, str]:
    """Extract non-secret Slack installation fields needed for routing."""
    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    enterprise = (
        payload.get("enterprise")
        if isinstance(payload.get("enterprise"), dict)
        else {}
    )
    authed_user = (
        payload.get("authed_user")
        if isinstance(payload.get("authed_user"), dict)
        else {}
    )
    values = {
        "app_id": payload.get("app_id"),
        "team_id": team.get("id") or payload.get("team_id"),
        "team_name": team.get("name") or (
            payload.get("team") if isinstance(payload.get("team"), str) else None
        ),
        "enterprise_id": enterprise.get("id") or payload.get("enterprise_id"),
        "enterprise_name": enterprise.get("name") or (
            payload.get("enterprise")
            if isinstance(payload.get("enterprise"), str)
            else None
        ),
        "bot_user_id": payload.get("bot_user_id"),
        "authed_user_id": authed_user.get("id"),
    }
    return {
        key: str(value).strip()
        for key, value in values.items()
        if value not in (None, "")
    }


def _slack_installation_identity(profile: dict[str, Any]) -> str | None:
    """Return a stable Slack App installation key, never the installer user."""
    app_id = str(profile.get("app_id") or "unknown-app").strip()
    team_id = str(profile.get("team_id") or "").strip()
    enterprise_id = str(profile.get("enterprise_id") or "").strip()
    if team_id:
        return f"slack:{app_id}:team:{team_id}"
    if enterprise_id:
        return f"slack:{app_id}:enterprise:{enterprise_id}"
    return None


def _discord_installation_profile(
    payload: dict[str, Any],
    *,
    application_id: str,
) -> dict[str, str]:
    """Extract the stable, non-secret Discord Guild installation identity."""
    guild = payload.get("guild") if isinstance(payload.get("guild"), dict) else {}
    guild_id = str(guild.get("id") or "").strip()
    if not guild_id or not application_id:
        return {}
    return {
        "application_id": application_id,
        "guild_id": guild_id,
        "guild_name": str(guild.get("name") or guild_id).strip(),
    }


async def resolve_oauth_identity(
    server_key: str,
    tokens: TokenSet,
    *,
    application_id: str | None = None,
    timeout: float = 10.0,
) -> tuple[str, dict[str, Any]]:
    """Resolve a stable external account id and safe display profile.

    Token responses vary widely. We first inspect the response, then use the
    provider's profile endpoint when available. If a provider exposes neither,
    a non-reversible token fingerprint keeps separate connections distinct;
    explicit reconnect state still targets the original row.
    """
    raw_payload = tokens.raw or {}
    provider_user_id, profile = _identity_from_payload(raw_payload)
    if server_key == "slack":
        profile.update(_slack_installation_profile(raw_payload))
        provider_user_id = _slack_installation_identity(profile)
    elif server_key == "discord":
        profile = _discord_installation_profile(
            raw_payload,
            application_id=str(application_id or "").strip(),
        )
        guild_id = profile.get("guild_id")
        if not guild_id or not application_id:
            raise OAuthFlowError(502, "Discord did not return a Guild installation")
        provider_user_id = f"discord:{application_id}:guild:{guild_id}"
    endpoint = _PROFILE_ENDPOINTS.get(server_key)
    if server_key == "paypal":
        from packages.core.services.official_remote_mcp import OfficialRemoteMCPFactory

        endpoint = (
            OfficialRemoteMCPFactory.paypal_oauth().identity_url,
            {"schema": "paypalv1.1"},
        )
    if endpoint and (not provider_user_id or not profile):
        url, params = endpoint
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {tokens.access_token}"},
                )
            if response.status_code < 400:
                fetched = response.json()
                fetched_id, fetched_profile = _identity_from_payload(fetched)
                if server_key != "slack":
                    provider_user_id = provider_user_id or fetched_id
                else:
                    fetched_profile.update(_slack_installation_profile(fetched))
                profile = {**fetched_profile, **profile}
        except Exception:
            logger.debug("Could not fetch OAuth identity for %s", server_key, exc_info=True)

    if server_key == "slack":
        provider_user_id = _slack_installation_identity(profile)
    elif server_key == "discord":
        guild_id = profile.get("guild_id")
        if not guild_id or not application_id:
            raise OAuthFlowError(502, "Discord did not return a Guild installation")
        provider_user_id = f"discord:{application_id}:guild:{guild_id}"
    else:
        provider_user_id = provider_user_id or tokens.provider_user_id
    if not provider_user_id:
        digest = hashlib.sha256(tokens.access_token.encode("utf-8")).hexdigest()[:24]
        provider_user_id = f"token:{digest}"
    return provider_user_id, profile


# ── User-facing error page ─────────────────────────────────────────────────


def render_oauth_error_page(
    server_key: str,
    error: Optional[str],
    description: Optional[str],
) -> HTMLResponse:
    """Uniform error page for ``?error=...`` redirects. Returned as
    HTML so the popup window the user is staring at goes from "loading"
    to a readable explanation in one round-trip."""
    msg = description or error or (
        "OAuth callback missing both `code` and `error` — provider "
        "redirected here without finishing the flow."
    )
    title = (error or "no_code").replace("_", " ")
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>{server_key} OAuth failed</title></head>
        <body style="font-family:-apple-system,Segoe UI,sans-serif;padding:32px;max-width:560px;margin:auto">
          <h2 style="color:#b91c1c">{server_key.title()} sign-in didn't complete</h2>
          <p style="color:#475569;line-height:1.6"><strong>{title}</strong>:
          {msg}</p>
          <p style="color:#94a3b8;font-size:13px">If you cancelled, just close this window and try again.
          If a scope was rejected, the app's developer needs to enable that product
          on the provider side, then retry. Status: HTTP 400.</p>
          <script>setTimeout(()=>{{try{{window.close()}}catch(e){{}}}}, 30000)</script>
        </body></html>""",
        status_code=400,
    )


__all__ = [
    "AuthorizationStart",
    "TokenSet",
    "OAuthFlowError",
    "begin_authorization",
    "complete_authorization",
    "get_pending_entity_id",
    "get_pending_return_to",
    "get_pending_connection_id",
    "resolve_oauth_identity",
    "validate_pending_state",
    "render_oauth_error_page",
]
