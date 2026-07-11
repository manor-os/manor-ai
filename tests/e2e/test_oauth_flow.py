"""E2E for ``packages.core.services.oauth_flow``.

Exercises both the in-process primitives (begin_authorization,
complete_authorization, render_oauth_error_page) and their wired-up
endpoints (``/oauth/{server_key}/start`` + ``/oauth/{server_key}/callback``).

Why both layers
───────────────
The router is a thin wrapper over the helpers, but it owns the FastAPI
quirks (optional query param semantics, HTMLResponse handling). A
helper-only test would miss "missing code returns 400 not 422", which
is the bug we just fixed. Catching it requires going through the live
endpoint.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.manual]


# ── In-process: helper primitives ────────────────────────────────────


def test_begin_authorization_emits_pkce_challenge() -> None:
    """``begin_authorization`` must add ``code_challenge`` +
    ``code_challenge_method=S256`` to the URL and stash a verifier in
    the pending map. Twitter v2 enforces it; everyone else ignores
    unknown params."""
    from packages.core.services.oauth_flow import (
        begin_authorization,
        _pending_oauth_states,
    )

    class _Cfg:
        server_key = "twitter_x"
        client_id = "cid"
        client_secret = "cs"
        scopes = "tweet.read users.read offline.access"
        authorize_url = "https://x.com/i/oauth2/authorize"

    start = begin_authorization(
        config=_Cfg(),
        user_id="u_e2e",
        redirect_uri="https://example.com/cb",
    )
    qs = parse_qs(urlparse(start.authorize_url).query)
    assert qs["code_challenge_method"] == ["S256"]
    assert len(qs["code_challenge"][0]) == 43, "S256 challenge should be 43 chars (base64url no pad)"
    assert qs["state"] == [start.state]
    pending = _pending_oauth_states.get(start.state)
    assert pending is not None
    assert pending["user_id"] == "u_e2e"
    assert pending["server_key"] == "twitter_x"
    assert len(pending["code_verifier"]) == 86, "verifier should be 64 random bytes URL-safe (≈86 chars)"


@pytest.mark.asyncio
async def test_complete_authorization_rejects_unknown_state() -> None:
    """An unknown / replayed state must raise ``OAuthFlowError(400)``
    rather than silently match anything."""
    from packages.core.services.oauth_flow import (
        complete_authorization,
        OAuthFlowError,
    )

    class _Cfg:
        server_key = "fake"
        client_id = "x"
        client_secret = "y"
        token_url = "https://example.invalid/oauth/token"

    with pytest.raises(OAuthFlowError) as exc:
        await complete_authorization(
            server_key="fake",
            code="anything",
            state="bogus_state_never_stored",
            redirect_uri="https://example.com/cb",
            config=_Cfg(),
        )
    assert exc.value.status == 400


def test_render_oauth_error_page_quotes_provider_text() -> None:
    """The HTML page must contain both the error code and the
    description — that's the whole point of moving away from FastAPI's
    422 'missing code'."""
    from packages.core.services.oauth_flow import render_oauth_error_page

    resp = render_oauth_error_page(
        "linkedin",
        "unauthorized_scope_error",
        'Scope "openid" is not authorized for your application',
    )
    body = resp.body.decode("utf-8")
    assert resp.status_code == 400
    assert "Linkedin sign-in didn't complete" in body
    assert "openid" in body


# ── HTTP: wired endpoints ────────────────────────────────────────────


def test_oauth_callback_with_provider_error_returns_html(expect_running_api: str, http_get: httpx.Client) -> None:
    """Provider redirected back with ``?error=...`` must render a
    branded 400 HTML page, NOT a 422."""
    r = http_get.get(
        f"{expect_running_api}/api/v1/integrations/oauth/linkedin/callback",
        params={
            "error": "unauthorized_scope_error",
            "error_description": "Scope is not authorized",
            "state": "anything",
        },
    )
    assert r.status_code == 400, r.text
    assert "text/html" in r.headers.get("content-type", "")
    assert "sign-in didn't complete" in r.text


def test_oauth_callback_missing_code_returns_html(expect_running_api: str, http_get: httpx.Client) -> None:
    """The pre-fix bug: missing ``code`` returned 422 with FastAPI's
    default 'Field required' detail. Should return our branded page."""
    r = http_get.get(
        f"{expect_running_api}/api/v1/integrations/oauth/twitter_x/callback",
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"
    assert "sign-in didn't complete" in r.text
