"""Shared OAuth 2.0 flow helpers — start + callback machinery.

Pulled out of ``apps/api/routers/integrations.py`` so the router
endpoints become trivial wrappers and the same primitives can drive
admin-side test flows or CLI utilities later.

What lives here
───────────────
* ``begin_authorization()`` — generates ``state`` + PKCE code verifier
  + code challenge, stores them in the in-memory pending map, returns
  a fully-built authorize URL.
* ``complete_authorization()`` — pops the pending state, validates it,
  exchanges the code for an access token (PKCE-aware), returns a
  typed ``TokenSet``. Raises ``OAuthFlowError`` on every failure mode.
* ``render_oauth_error_page()`` — uniform HTML page when the provider
  redirects back with ``?error=...``. Same look across providers.

State store
───────────
``_pending_oauth_states`` is a process-local dict (not Redis-backed).
For single-API-server deployments this is fine; if/when we scale out,
swap the dict for a Redis hash with TTL — the API surface here doesn't
change.

PKCE
────
Every flow gets ``code_challenge_method=S256``. Providers that don't
support PKCE ignore the extra params; providers that require it
(Twitter v2 user-context) need them. There's no harm in always
sending. See RFC 7636.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)


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


# ── State store (process-local) ────────────────────────────────────────────


_pending_oauth_states: Dict[str, Dict[str, str]] = {}


def _store_pending(
    state: str,
    *,
    user_id: str,
    server_key: str,
    code_verifier: str,
    return_to: str | None = None,
) -> None:
    pending = {
        "user_id": user_id,
        "server_key": server_key,
        "code_verifier": code_verifier,
    }
    if return_to:
        pending["return_to"] = return_to
    _pending_oauth_states[state] = pending


def _pop_pending(state: str, *, server_key: str) -> Dict[str, str]:
    pending = _pending_oauth_states.pop(state, None)
    if not pending or pending.get("server_key") != server_key:
        raise OAuthFlowError(400, "Invalid or expired OAuth state")
    return pending


def validate_pending_state(state: str, *, server_key: str) -> None:
    """Validate state before any provider/config side effects.

    Callback handlers use this to fail closed on forged states even if
    the deployment has not configured that provider yet. The actual
    completion path still pops the state later to preserve one-time use.
    """
    pending = _pending_oauth_states.get(state)
    if not pending or pending.get("server_key") != server_key:
        raise OAuthFlowError(400, "Invalid or expired OAuth state")


def get_pending_return_to(state: str, *, server_key: str) -> str | None:
    """Return the caller-provided post-OAuth path after validating state."""
    pending = _pending_oauth_states.get(state)
    if not pending or pending.get("server_key") != server_key:
        raise OAuthFlowError(400, "Invalid or expired OAuth state")
    return pending.get("return_to")


# ── Authorization step ─────────────────────────────────────────────────────


def begin_authorization(
    *,
    config: Any,           # OAuthProviderConfig — typed loosely to avoid import cycle
    user_id: str,
    redirect_uri: str,
    return_to: str | None = None,
) -> AuthorizationStart:
    """Build the provider's authorize URL for ``user_id``.

    Generates a fresh ``state`` and a PKCE pair, stashes the verifier
    against ``state`` in the pending map, and returns the URL. The
    caller (router) is unchanged in shape.
    """
    state = secrets.token_urlsafe(24)

    # PKCE — Twitter/X v2 mandates this; everyone else ignores extra
    # params if they don't support it. ~86 chars, well within RFC's
    # 43–128 range.
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    _store_pending(state,
                   user_id=user_id,
                   server_key=config.server_key,
                   code_verifier=code_verifier,
                   return_to=return_to)

    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": config.scopes,
        "state": state,
        "access_type": "offline",     # Google: get refresh_token
        "prompt": "consent",          # force re-consent so refresh_token is returned
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
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
    pending = _pop_pending(state, server_key=server_key)
    user_id = pending["user_id"]
    code_verifier = pending.get("code_verifier", "")

    body: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }
    if code_verifier:
        body["code_verifier"] = code_verifier

    from packages.core.services.oauth_provider_config import build_token_request_auth
    headers, body = build_token_request_auth(config, body)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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
    "get_pending_return_to",
    "validate_pending_state",
    "render_oauth_error_page",
]
