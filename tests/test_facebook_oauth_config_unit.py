"""Database-free coverage for Manor's Meta OAuth contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from packages.core.external_api_versions import META_GRAPH
from packages.core.services.oauth_flow import (
    begin_authorization,
    complete_authorization,
)
from packages.core.services.oauth_provider_config import (
    _PROVIDER_OAUTH_META,
    OAuthProviderConfig,
    apply_authorize_param_conventions,
)


EXPECTED_SCOPES = {
    "public_profile",
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "pages_manage_engagement",
    "pages_messaging",
    "pages_manage_metadata",
    "read_insights",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
}


def test_facebook_provider_matches_reviewed_operations() -> None:
    meta = _PROVIDER_OAUTH_META["facebook"]
    assert META_GRAPH.value in meta["authorize_url"]
    assert META_GRAPH.value in meta["token_url"]
    assert set(meta["scopes"].split(",")) == EXPECTED_SCOPES
    assert "email" not in meta["scopes"]


def test_facebook_authorize_params_omit_google_and_pkce_fields() -> None:
    params = apply_authorize_param_conventions(
        SimpleNamespace(server_key="facebook"),
        {
            "client_id": "cid",
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
    )
    assert params == {"client_id": "cid"}


@pytest.mark.asyncio
async def test_facebook_code_exchange_returns_long_lived_token() -> None:
    meta = _PROVIDER_OAUTH_META["facebook"]
    config = OAuthProviderConfig(
        server_key="facebook",
        client_id="cid",
        client_secret="secret",
        authorize_url=meta["authorize_url"],
        token_url=meta["token_url"],
        scopes=meta["scopes"],
        redirect_path="/api/v1/integrations/oauth/facebook/callback",
        source="test",
    )
    started = begin_authorization(
        config=config,
        user_id="user-1",
        redirect_uri="https://app.manorai.xyz/api/v1/integrations/oauth/facebook/callback",
    )
    query = parse_qs(urlparse(started.authorize_url).query)
    assert set(query["scope"][0].split(",")) == EXPECTED_SCOPES
    assert "code_challenge" not in query

    calls: list[dict[str, str]] = []

    class _Response:
        status_code = 200
        text = ""

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def json(self) -> dict[str, object]:
            return self.payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, params=None, headers=None):
            calls.append(dict(params or {}))
            if (params or {}).get("grant_type") == "fb_exchange_token":
                return _Response(
                    {"access_token": "long", "expires_in": 5_184_000}
                )
            return _Response({"access_token": "short", "expires_in": 3_600})

    with patch("httpx.AsyncClient", lambda *args, **kwargs: _Client()):
        user_id, tokens = await complete_authorization(
            server_key="facebook",
            code="code",
            state=started.state,
            redirect_uri=(
                "https://app.manorai.xyz/api/v1/integrations/oauth/facebook/callback"
            ),
            config=config,
        )

    assert user_id == "user-1"
    assert tokens.access_token == "long"
    assert tokens.expires_at is not None
    assert any(call.get("fb_exchange_token") == "short" for call in calls)
