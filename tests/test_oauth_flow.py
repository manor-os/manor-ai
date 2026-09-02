"""Tests for provider OAuth config + start endpoint.

Covers:
  * Unsupported provider returns 400
  * No client credentials configured → 501
  * Env-only credentials (cloud path) produce a valid authorize URL
  * DB-stored credentials (OSS path) override env
  * Admin POST /oauth-config requires users.manage
  * Callback exchanges code → stores in oauth_accounts (mocked token endpoint)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens


def test_notion_token_request_uses_basic_auth_without_client_secret_body():
    from types import SimpleNamespace

    from packages.core.services.oauth_provider_config import build_token_request_auth

    config = SimpleNamespace(
        server_key="notion",
        client_id="notion-client",
        client_secret="notion-secret",
    )
    headers, body = build_token_request_auth(
        config,
        {
            "grant_type": "authorization_code",
            "code": "auth-code",
            "redirect_uri": "https://manor.example/oauth/callback",
            "client_id": "notion-client",
            "client_secret": "notion-secret",
        },
    )

    import base64

    expected = base64.b64encode(b"notion-client:notion-secret").decode()
    assert headers["Authorization"] == f"Basic {expected}"
    assert body == {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "redirect_uri": "https://manor.example/oauth/callback",
    }


def test_twitter_token_request_uses_basic_auth_without_client_credentials_body():
    from types import SimpleNamespace

    from packages.core.services.oauth_provider_config import build_token_request_auth

    config = SimpleNamespace(
        server_key="twitter_x",
        client_id="x-client",
        client_secret="x-secret",
    )
    headers, body = build_token_request_auth(
        config,
        {
            "grant_type": "authorization_code",
            "code": "auth-code",
            "redirect_uri": "https://staging-dev.manorai.xyz/api/v1/integrations/oauth/twitter_x/callback",
            "client_id": "x-client",
            "client_secret": "x-secret",
        },
    )

    import base64

    expected = base64.b64encode(b"x-client:x-secret").decode()
    assert headers["Authorization"] == f"Basic {expected}"
    assert "client_id" not in body
    assert "client_secret" not in body
    assert body["redirect_uri"].endswith("/oauth/twitter_x/callback")


def test_notion_authorize_request_uses_owner_user_without_generic_parameters():
    from types import SimpleNamespace

    from packages.core.services.oauth_provider_config import (
        apply_authorize_param_conventions,
        oauth_provider_uses_pkce,
    )

    config = SimpleNamespace(server_key="notion")
    params = apply_authorize_param_conventions(
        config,
        {
            "client_id": "notion-client",
            "scope": "",
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
    )

    assert params == {"client_id": "notion-client", "owner": "user"}
    assert oauth_provider_uses_pkce("notion") is False


@pytest.mark.asyncio
async def test_notion_token_exchange_posts_json_with_basic_auth(monkeypatch):
    from types import SimpleNamespace

    from packages.core.services.oauth_flow import (
        begin_authorization,
        complete_authorization,
    )

    config = SimpleNamespace(
        server_key="notion",
        client_id="notion-client",
        client_secret="notion-secret",
        authorize_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        scopes="",
    )
    started = await begin_authorization(
        config=config,
        user_id="user-1",
        redirect_uri="https://manor.example/oauth/callback",
    )

    calls: list[dict] = []

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"access_token": "notion-access-token", "bot_id": "bot-1"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: _Client())
    _, tokens = await complete_authorization(
        server_key="notion",
        code="auth-code",
        state=started.state,
        redirect_uri="https://manor.example/oauth/callback",
        config=config,
    )

    assert tokens.access_token == "notion-access-token"
    assert len(calls) == 1
    request = calls[0]
    assert request["url"] == config.token_url
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Authorization"].startswith("Basic ")
    assert "json" in request and "data" not in request
    assert "client_id" not in request["json"]
    assert "client_secret" not in request["json"]


def _leased_tokens(account) -> dict[str, str]:
    return lease_oauth_account_tokens(
        account,
        requester_id="test_oauth_flow",
        requester_kind="test",
        reason="verify encrypted OAuth token persistence",
    )


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    return headers, data["user_id"], me.json()["entity_id"]


async def _seed_mcp_server(db, key: str):
    """Ensure a mcp_servers row exists so default_config can be read/written."""
    from sqlalchemy import select
    from packages.core.models.mcp import MCPServer
    from packages.core.models.base import generate_ulid

    row = (await db.execute(select(MCPServer).where(MCPServer.server_key == key))).scalar_one_or_none()
    if not row:
        row = MCPServer(
            id=generate_ulid(),
            server_key=key,
            name=key.title(),
            transport="builtin",
            auth_type="oauth2",
            status="active",
        )
        db.add(row)
        await db.flush()
    return row


# ── /oauth/{server_key}/start ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_oauth_start_unsupported_provider(client: AsyncClient):
    headers, _, _ = await _register_owner(client, "oauth_not_supported")
    resp = await client.get(
        "/api/v1/integrations/oauth/not_a_real_provider/start",
        headers=headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oauth_start_missing_config_returns_501(client: AsyncClient, monkeypatch):
    """Provider supported, but no env + no DB creds → 501."""
    headers, _, _ = await _register_owner(client, "oauth_missing")

    # Ensure no env vars leak in
    for v in ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)

    resp = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
    )
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_oauth_start_with_env_credentials(client: AsyncClient, monkeypatch):
    """Env-only (cloud path) produces a valid Slack authorize URL."""
    headers, user_id, _ = await _register_owner(client, "oauth_env")

    monkeypatch.setenv("SLACK_CLIENT_ID", "cloud_cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "cloud_csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    resp = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["server_key"] == "slack"
    assert data["source"] == "env"
    assert "slack.com/oauth/v2/authorize" in data["authorize_url"]
    assert "client_id=cloud_cid" in data["authorize_url"]
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A3010" in data["authorize_url"]
    assert "state=" in data["authorize_url"]


@pytest.mark.asyncio
async def test_oauth_start_tiktok_uses_client_key(client: AsyncClient, monkeypatch):
    """TikTok is non-standard: the authorize URL must carry ``client_key``
    (not ``client_id``) and comma-separated scopes."""
    headers, _, _ = await _register_owner(client, "oauth_tiktok")

    monkeypatch.setenv("TIKTOK_CLIENT_ID", "tt_key_123")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "tt_secret_456")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    resp = await client.get(
        "/api/v1/integrations/oauth/tiktok/start",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["server_key"] == "tiktok"
    url = data["authorize_url"]
    assert "tiktok.com/v2/auth/authorize" in url
    # client_key, never client_id
    assert "client_key=tt_key_123" in url
    assert "client_id=" not in url
    # comma-separated scopes survive urlencoding as %2C
    assert "video.publish" in url


@pytest.mark.asyncio
async def test_oauth_start_facebook_uses_reviewed_scopes_only(
    client: AsyncClient, monkeypatch
):
    """Facebook Login must request exactly the permissions backed by tools and
    omit Google/PKCE parameters that are not part of Meta's server-side flow."""
    from urllib.parse import parse_qs, urlparse

    headers, _, _ = await _register_owner(client, "oauth_facebook")
    monkeypatch.setenv("FACEBOOK_CLIENT_ID", "fb_cid")
    monkeypatch.setenv("FACEBOOK_CLIENT_SECRET", "fb_csec")
    monkeypatch.setenv("APP_URL", "https://app.manorai.xyz")

    resp = await client.get(
        "/api/v1/integrations/oauth/facebook/start",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    query = parse_qs(urlparse(data["authorize_url"]).query)
    assert query["client_id"] == ["fb_cid"]
    assert query["redirect_uri"] == [
        "https://app.manorai.xyz/api/v1/integrations/oauth/facebook/callback"
    ]
    assert set(query["scope"][0].split(",")) == {
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
    for unsupported in (
        "email",
        "access_type",
        "prompt",
        "code_challenge",
        "code_challenge_method",
    ):
        assert unsupported not in query


@pytest.mark.asyncio
async def test_facebook_callback_replaces_unreadable_credential(
    client: AsyncClient, monkeypatch
):
    """A fresh Meta grant replaces unreadable credentials without leasing them."""
    headers, user_id, _ = await _register_owner(client, "oauth_facebook_callback")
    monkeypatch.setenv("FACEBOOK_CLIENT_ID", "fb_cid")
    monkeypatch.setenv("FACEBOOK_CLIENT_SECRET", "fb_csec")
    monkeypatch.setenv("APP_URL", "https://app.manorai.xyz")

    start = await client.get(
        "/api/v1/integrations/oauth/facebook/start",
        headers=headers,
    )
    state = start.json()["state"]
    calls: list[dict] = []

    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount

    async with dbmod.async_session() as db:
        db.add(OAuthAccount(
            id=generate_ulid(),
            user_id=user_id,
            provider="facebook",
            provider_user_id="fb-user-1",
            credential_ref="dev:v1:unreadable",
            credential_scheme="dev_fernet",
            profile={"is_default": True},
        ))
        await db.commit()

    class _MockResp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, _url, *, params=None, headers=None):
            calls.append(dict(params or {}))
            if (params or {}).get("grant_type") == "fb_exchange_token":
                return _MockResp({
                    "access_token": "fb-long-lived",
                    "token_type": "bearer",
                    "expires_in": 5_184_000,
                })
            if (params or {}).get("fields") == "id,name":
                return _MockResp({"id": "fb-user-1", "name": "Meta Tester"})
            return _MockResp({
                "access_token": "fb-short-lived",
                "token_type": "bearer",
                "expires_in": 3_600,
            })

    with patch("httpx.AsyncClient", lambda *args, **kwargs: _MockClient()):
        resp = await client.get(
            f"/api/v1/integrations/oauth/facebook/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert any(call.get("fb_exchange_token") == "fb-short-lived" for call in calls)

    async with dbmod.async_session() as db:
        row = (
            await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.provider == "facebook",
                )
            )
        ).scalar_one()
    assert row.access_token is None
    assert _leased_tokens(row)["access_token"] == "fb-long-lived"
    assert row.provider_user_id == "fb-user-1"
    assert row.token_expires_at is not None


@pytest.mark.asyncio
async def test_oauth_callback_keeps_the_entity_that_started_the_flow(
    client: AsyncClient,
    monkeypatch,
):
    headers, user_id, starting_entity_id = await _register_owner(
        client,
        "oauth_callback_entity_scope",
    )
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    start = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
    )
    assert start.status_code == 200, start.text
    state = start.json()["state"]

    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity, User, UserMembership

    switched_entity_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(Entity(id=switched_entity_id, name="Switched OAuth Entity"))
        db.add(UserMembership(
            id=generate_ulid(),
            user_id=user_id,
            entity_id=switched_entity_id,
            role="owner",
            status="active",
        ))
        user = await db.get(User, user_id)
        assert user is not None
        user.entity_id = switched_entity_id
        await db.commit()

    from packages.core.services import oauth_flow

    async def _complete_authorization(**_kwargs):
        await oauth_flow._pop_pending(state, server_key="slack")
        return user_id, oauth_flow.TokenSet(
            access_token="scoped-slack-access",
            refresh_token="scoped-slack-refresh",
            expires_at=None,
            provider_user_id="slack-scoped-user",
            raw={},
        )

    async def _resolve_oauth_identity(*_args, **_kwargs):
        return "slack:A-SCOPED:team:T-SCOPED", {
            "app_id": "A-SCOPED",
            "team_id": "T-SCOPED",
            "team_name": "Scoped Slack Team",
        }

    monkeypatch.setattr(
        oauth_flow,
        "complete_authorization",
        _complete_authorization,
    )
    monkeypatch.setattr(
        oauth_flow,
        "resolve_oauth_identity",
        _resolve_oauth_identity,
    )

    callback = await client.get(
        f"/api/v1/integrations/oauth/slack/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text

    from packages.core.models.channel import ChannelConfig
    from packages.core.models.user import OAuthAccount

    async with dbmod.async_session() as db:
        oauth_account = (
            await db.execute(
                select(OAuthAccount).where(OAuthAccount.user_id == user_id)
            )
        ).scalar_one()
        channel_config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.credential_source_id == oauth_account.id
                )
            )
        ).scalar_one()
        user = await db.get(User, user_id)
        assert user is not None
        preferences = user.preferences

    assert channel_config.entity_id == starting_entity_id
    assert preferences["integration_account_defaults"][starting_entity_id][
        "slack"
    ]["account_id"] == oauth_account.id
    assert switched_entity_id not in preferences["integration_account_defaults"]

    second_start = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
    )
    assert second_start.status_code == 200, second_start.text
    state = second_start.json()["state"]
    async with dbmod.async_session() as db:
        membership = (
            await db.execute(
                select(UserMembership).where(
                    UserMembership.user_id == user_id,
                    UserMembership.entity_id == starting_entity_id,
                )
            )
        ).scalar_one()
        membership.status = "inactive"
        await db.commit()

    rejected_callback = await client.get(
        f"/api/v1/integrations/oauth/slack/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert rejected_callback.status_code == 403


@pytest.mark.asyncio
async def test_oauth_callback_reencrypts_alias_provider_before_preserving_refresh(
    client: AsyncClient,
    monkeypatch,
):
    headers, user_id, _entity_id = await _register_owner(
        client,
        "oauth_callback_provider_alias",
    )
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    monkeypatch.setenv("X_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount
    from packages.core.services.oauth_account_credentials import (
        store_oauth_account_tokens,
    )

    connection_id = generate_ulid()
    async with dbmod.async_session() as db:
        account = OAuthAccount(
            id=connection_id,
            user_id=user_id,
            provider="twitter",
            provider_user_id="twitter-alias-user",
            profile={"is_default": True},
        )
        store_oauth_account_tokens(
            account,
            access_token="old-twitter-access",
            refresh_token="old-twitter-refresh",
            preserve_existing_refresh=False,
        )
        db.add(account)
        await db.commit()

    start = await client.get(
        "/api/v1/integrations/oauth/twitter_x/start"
        f"?connection_id={connection_id}",
        headers=headers,
    )
    assert start.status_code == 200, start.text
    state = start.json()["state"]

    from packages.core.services import oauth_flow

    async def _complete_authorization(**_kwargs):
        await oauth_flow._pop_pending(state, server_key="twitter_x")
        return user_id, oauth_flow.TokenSet(
            access_token="new-twitter-access",
            refresh_token=None,
            expires_at=None,
            provider_user_id="twitter-alias-user",
            raw={},
        )

    async def _resolve_oauth_identity(*_args, **_kwargs):
        return "twitter-alias-user", {"username": "alias-user"}

    monkeypatch.setattr(
        oauth_flow,
        "complete_authorization",
        _complete_authorization,
    )
    monkeypatch.setattr(
        oauth_flow,
        "resolve_oauth_identity",
        _resolve_oauth_identity,
    )

    callback = await client.get(
        f"/api/v1/integrations/oauth/twitter_x/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text

    async with dbmod.async_session() as db:
        account = await db.get(OAuthAccount, connection_id)
        assert account is not None

    assert account.provider == "twitter_x"
    assert _leased_tokens(account) == {
        "access_token": "new-twitter-access",
        "refresh_token": "old-twitter-refresh",
    }


def test_apply_authorize_param_conventions_google_select_account():
    """Google providers upgrade ``prompt`` to ``select_account consent`` so a
    second connect lets the user pick a different Google account, while
    keeping ``consent`` (refresh_token) and leaving non-Google flows alone."""
    from types import SimpleNamespace

    from packages.core.services.oauth_provider_config import (
        apply_authorize_param_conventions,
        is_google_provider,
    )

    base = {"prompt": "consent", "access_type": "offline", "client_id": "cid"}

    for key in ("gmail", "google_calendar", "google_drive", "youtube"):
        assert is_google_provider(key)
        out = apply_authorize_param_conventions(SimpleNamespace(server_key=key), dict(base))
        prompt_values = out["prompt"].split()
        assert "select_account" in prompt_values, key
        assert "consent" in prompt_values, key  # refresh_token still forced
        assert out["access_type"] == "offline"
        # No accidental duplication if select_account is already present.
        out2 = apply_authorize_param_conventions(
            SimpleNamespace(server_key=key), {**base, "prompt": "select_account consent"}
        )
        assert out2["prompt"].split().count("select_account") == 1

    # Non-Google providers keep prompt=consent, never gain select_account.
    for key in ("slack", "github", "twitter_x"):
        assert not is_google_provider(key)
        out = apply_authorize_param_conventions(SimpleNamespace(server_key=key), dict(base))
        assert out["prompt"] == "consent"
        assert "select_account" not in out["prompt"]


def test_google_oauth_scopes_are_exactly_the_reviewed_minimum_set():
    """Keep the runtime manifest byte-for-byte aligned with the Google Cloud
    Data Access screen. Alias scopes such as ``email`` / ``profile`` and
    redundant broader scopes have caused verification discrepancies before.
    """
    from packages.core.services.oauth_provider_config import _PROVIDER_OAUTH_META

    identity = {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
    expected = {
        "gmail": identity | {
            "https://www.googleapis.com/auth/gmail.modify",
        },
        "google_calendar": identity | {
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            "https://www.googleapis.com/auth/calendar.events.freebusy",
        },
        "google_drive": identity | {
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
        },
        "youtube": identity | {
            "https://www.googleapis.com/auth/youtube.force-ssl",
        },
    }

    for provider, scopes in expected.items():
        assert set(_PROVIDER_OAUTH_META[provider]["scopes"].split()) == scopes


def test_google_mcp_catalog_scopes_match_runtime_service_scopes():
    from packages.core.services.mcp_seed import _MCP_CATALOG
    from packages.core.services.oauth_provider_config import _PROVIDER_OAUTH_META

    identity = {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
    catalog = {
        row[0]: set((row[6] or "").split(",")) - {""}
        for row in _MCP_CATALOG
    }
    for provider in ("gmail", "google_calendar", "google_drive", "youtube"):
        runtime_service_scopes = set(_PROVIDER_OAUTH_META[provider]["scopes"].split()) - identity
        assert catalog[provider] == runtime_service_scopes


@pytest.mark.asyncio
async def test_oauth_start_google_uses_select_account_prompt(
    client: AsyncClient, monkeypatch
):
    """The gmail authorize URL must carry ``prompt=select_account`` (plus
    ``consent`` + ``access_type=offline``) so a second connect can target a
    different Google account. A non-Google provider must be unaffected."""
    headers, _, _ = await _register_owner(client, "oauth_google_select")

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "g_cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "g_csec")
    monkeypatch.setenv("SLACK_CLIENT_ID", "s_cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "s_csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    resp = await client.get(
        "/api/v1/integrations/oauth/gmail/start",
        headers=headers,
    )
    assert resp.status_code == 200
    url = resp.json()["authorize_url"]
    # urlencode() renders the space as '+': prompt=select_account+consent
    assert "prompt=select_account" in url
    assert "consent" in url
    assert "access_type=offline" in url

    # A non-Google provider keeps the plain consent prompt.
    slack = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
    )
    assert slack.status_code == 200
    assert "select_account" not in slack.json()["authorize_url"]


@pytest.mark.asyncio
async def test_oauth_start_db_overrides_env(client: AsyncClient, monkeypatch):
    """DB-stored client_id beats env."""
    headers, _, _ = await _register_owner(client, "oauth_db")

    monkeypatch.setenv("GITHUB_CLIENT_ID", "env_cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "env_csec")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        server = await _seed_mcp_server(db, "github")
        cfg = dict(server.default_config or {})
        cfg["oauth_client_id"] = "oss_cid"
        cfg["oauth_client_secret"] = "oss_csec"
        server.default_config = cfg
        await db.commit()

    resp = await client.get(
        "/api/v1/integrations/oauth/github/start",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "db"
    assert "client_id=oss_cid" in data["authorize_url"]


def test_discord_oauth_is_guild_install_with_minimum_permissions() -> None:
    from packages.core.services.oauth_provider_config import _PROVIDER_OAUTH_META

    assert set(_PROVIDER_OAUTH_META["discord"]["scopes"].split()) == {
        "bot",
        "applications.commands",
    }


def test_linkedin_oauth_requests_only_member_scopes() -> None:
    """The default LinkedIn app must not request Community Management scopes."""
    from packages.core.services.oauth_provider_config import _PROVIDER_OAUTH_META

    assert set(_PROVIDER_OAUTH_META["linkedin"]["scopes"].split()) == {
        "openid",
        "profile",
        "email",
        "w_member_social",
    }


def test_linkedin_oauth_does_not_use_pkce() -> None:
    from packages.core.services.oauth_provider_config import oauth_provider_uses_pkce

    assert oauth_provider_uses_pkce("linkedin") is False


@pytest.mark.asyncio
async def test_discord_oauth_start_builds_guild_install_url(
    client: AsyncClient,
    monkeypatch,
) -> None:
    headers, _, _ = await _register_owner(client, "discord_guild_install")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "discord-app-id")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "discord-client-secret")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-bot-token")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "11" * 32)
    monkeypatch.setattr(
        "apps.api.routers.integrations.validate_discord_app_config",
        AsyncMock(return_value=True),
    )

    response = await client.get(
        "/api/v1/integrations/oauth/discord/start",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["authorize_url"]).query)
    assert set(query["scope"][0].split()) == {"bot", "applications.commands"}
    assert query["integration_type"] == ["0"]
    assert query["permissions"] == ["68672"]
    assert "access_type" not in query
    assert "prompt" not in query
    assert "code_challenge" not in query
    assert "code_challenge_method" not in query

    from packages.core.services.oauth_flow import get_pending_state

    pending = await get_pending_state(query["state"][0], server_key="discord")
    assert pending["code_verifier"] == ""


@pytest.mark.asyncio
async def test_discord_oauth_start_rejects_incomplete_runtime_config(
    client: AsyncClient,
    monkeypatch,
) -> None:
    headers, _, _ = await _register_owner(client, "discord_incomplete_app")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "discord-app-id")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "discord-client-secret")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_PUBLIC_KEY", raising=False)

    response = await client.get(
        "/api/v1/integrations/oauth/discord/start",
        headers=headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Discord App runtime configuration is invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (401, {}, False),
        (200, {"id": "other-app", "verify_key": "11" * 32}, False),
        (200, {"id": "discord-app-id", "verify_key": "22" * 32}, False),
        (200, {"id": "discord-app-id", "verify_key": "11" * 32}, True),
    ],
)
async def test_discord_app_runtime_validation_matches_deployment_app(
    monkeypatch,
    status_code: int,
    payload: dict,
    expected: bool,
) -> None:
    from packages.core.services.discord_app_config import (
        DiscordAppConfig,
        validate_discord_app_config,
    )

    class _Response:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: _Client())
    app = DiscordAppConfig(
        application_id="discord-app-id",
        bot_token="deployment-bot-token",
        public_key="11" * 32,
    )

    assert await validate_discord_app_config(app) is expected


# ── Admin OAuth config endpoint ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_save_oauth_config(client: AsyncClient):
    headers, _, _ = await _register_owner(client, "oauth_save")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        await _seed_mcp_server(db, "github")
        await db.commit()

    resp = await client.post(
        "/api/v1/integrations/mcp-servers/github/oauth-config",
        headers=headers,
        json={
            "client_id": "gh_oss_cid",
            "client_secret": "gh_oss_csec",
            "scopes": "repo,user",
        },
    )
    assert resp.status_code == 204

    # Verify it was stored
    from packages.core.models.mcp import MCPServer
    from packages.core.credentials import Requester, get_credential_service

    async with dbmod.async_session() as db:
        row = (await db.execute(select(MCPServer).where(MCPServer.server_key == "github"))).scalar_one()
        assert row.default_config["oauth_client_id"] == "gh_oss_cid"
        assert "oauth_client_secret" not in row.default_config
        assert row.default_config["oauth_scopes"] == "repo,user"
        assert row.credential_ref
        assert row.credential_scheme
        plaintext = get_credential_service().lease_mcp_server(
            row,
            requester=Requester(kind="test", id="oauth_save"),
            reason="assert_oauth_config_secret_saved",
        )
    assert plaintext["oauth_client_secret"] == "gh_oss_csec"


@pytest.mark.asyncio
async def test_member_cannot_save_oauth_config(client: AsyncClient):
    owner_headers, user_id, _ = await _register_owner(client, "oauth_member")

    # Downgrade to member
    await client.put(
        f"/api/v1/auth/users/{user_id}/role",
        headers=owner_headers,
        json={"role": "member"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "oauth_member@test.com",
            "password": "pass123",
        },
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.post(
        "/api/v1/integrations/mcp-servers/github/oauth-config",
        headers=member_headers,
        json={"client_id": "x", "client_secret": "y"},
    )
    assert resp.status_code == 403


# ── Callback code exchange (mocked provider) ───────────────────────────────


@pytest.mark.asyncio
async def test_quickbooks_callback_persists_realm_id(client: AsyncClient, monkeypatch):
    headers, user_id, _ = await _register_owner(client, "oauth_quickbooks_realm")
    monkeypatch.setenv("QUICKBOOKS_CLIENT_ID", "cid")
    monkeypatch.setenv("QUICKBOOKS_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    start = await client.get(
        "/api/v1/integrations/oauth/quickbooks/start",
        headers=headers,
    )
    assert start.status_code == 200, start.text
    state = start.json()["state"]

    from packages.core.services import oauth_flow

    async def _complete_authorization(**_kwargs):
        await oauth_flow._pop_pending(state, server_key="quickbooks")
        return user_id, oauth_flow.TokenSet(
            access_token="qb-access",
            refresh_token="qb-refresh",
            expires_at=None,
            provider_user_id="",
            raw={},
        )

    async def _resolve_oauth_identity(*_args, **_kwargs):
        return "quickbooks-company-owner", {"display_name": "QuickBooks owner"}

    monkeypatch.setattr(oauth_flow, "complete_authorization", _complete_authorization)
    monkeypatch.setattr(oauth_flow, "resolve_oauth_identity", _resolve_oauth_identity)

    response = await client.get(
        f"/api/v1/integrations/oauth/quickbooks/callback"
        f"?code=abc&state={state}&realmId=realm-123",
        follow_redirects=False,
    )

    assert response.status_code == 302, response.text
    import packages.core.database as dbmod
    from packages.core.models.user import OAuthAccount

    async with dbmod.async_session() as db:
        row = (
            await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.provider == "quickbooks",
                )
            )
        ).scalar_one()
    assert row.profile["realm_id"] == "realm-123"


@pytest.mark.asyncio
async def test_paypal_sandbox_identity_uses_sandbox_api(monkeypatch):
    from packages.core.services import oauth_flow

    calls: list[str] = []

    class _Response:
        status_code = 200

        def json(self):
            return {"user_id": "paypal-user", "name": "Sandbox merchant"}

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            calls.append(url)
            return _Response()

    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")
    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", _Client)
    tokens = oauth_flow.TokenSet(
        access_token="paypal-access",
        refresh_token=None,
        expires_at=None,
        provider_user_id="",
        raw={},
    )

    provider_user_id, _profile = await oauth_flow.resolve_oauth_identity(
        "paypal",
        tokens,
    )

    assert provider_user_id == "paypal-user"
    assert calls == ["https://api-m.sandbox.paypal.com/v1/identity/oauth2/userinfo"]


@pytest.mark.asyncio
async def test_callback_exchanges_code_and_stores_token(client: AsyncClient, monkeypatch):
    headers, user_id, entity_id = await _register_owner(client, "oauth_callback")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    # Start flow to populate pending state
    start = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
    )
    state = start.json()["state"]

    class _MockResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "access_token": "xoxb-new-token",
                "refresh_token": "rt-new",
                "expires_in": 3600,
                "app_id": "A-MANOR",
                "team": {"id": "T-MANOR", "name": "Manor QA"},
                "enterprise": {"id": "E-MANOR", "name": "Manor Grid"},
                "bot_user_id": "U-MANOR-BOT",
                "authed_user": {
                    "id": "U-SLACK-OWNER",
                    "access_token": "mock-user-token-not-persisted-in-profile",
                },
            }

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _MockResp()

    with patch("httpx.AsyncClient", lambda *a, **kw: _MockClient()):
        resp = await client.get(
            f"/api/v1/integrations/oauth/slack/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    # 302 redirect back to /integrations
    assert resp.status_code == 302
    assert "/integrations?connected=slack" in resp.headers["location"]

    # oauth_accounts row should now exist
    import packages.core.database as dbmod
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.user import OAuthAccount

    async with dbmod.async_session() as db:
        row = (
            await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.provider == "slack",
                )
            )
        ).scalar_one()
        channel_config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.entity_id == entity_id,
                    ChannelConfig.channel_type == "slack",
                    ChannelConfig.credential_source_kind == "oauth_account",
                    ChannelConfig.credential_source_id == row.id,
                )
            )
        ).scalar_one()
    assert row.access_token is None
    assert row.refresh_token is None
    assert _leased_tokens(row) == {
        "access_token": "xoxb-new-token",
        "refresh_token": "rt-new",
    }
    assert row.token_expires_at is not None
    assert row.provider_user_id == "slack:A-MANOR:team:T-MANOR"
    assert row.profile["app_id"] == "A-MANOR"
    assert row.profile["team_id"] == "T-MANOR"
    assert row.profile["team_name"] == "Manor QA"
    assert row.profile["enterprise_id"] == "E-MANOR"
    assert row.profile["enterprise_name"] == "Manor Grid"
    assert row.profile["bot_user_id"] == "U-MANOR-BOT"
    assert row.profile["authed_user_id"] == "U-SLACK-OWNER"
    assert "access_token" not in json.dumps(row.profile)
    assert channel_config.owner_user_id == user_id
    assert channel_config.credentials == {}
    assert channel_config.config["slack_app_id"] == "A-MANOR"
    assert channel_config.config["slack_team_id"] == "T-MANOR"
    assert channel_config.config["slack_enterprise_id"] == "E-MANOR"
    assert channel_config.config["slack_bot_user_id"] == "U-MANOR-BOT"
    assert "access_token" not in json.dumps(channel_config.config)


@pytest.mark.asyncio
async def test_discord_callback_creates_user_owned_guild_connection(
    client: AsyncClient,
    monkeypatch,
) -> None:
    headers, user_id, entity_id = await _register_owner(
        client,
        "discord_callback",
    )
    monkeypatch.setenv("DISCORD_CLIENT_ID", "discord-app-id")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "discord-client-secret")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "deployment-bot-token")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "11" * 32)
    monkeypatch.setenv("APP_URL", "http://localhost:3010")
    monkeypatch.setattr(
        "apps.api.routers.integrations.validate_discord_app_config",
        AsyncMock(return_value=True),
    )

    start = await client.get(
        "/api/v1/integrations/oauth/discord/start",
        headers=headers,
    )
    assert start.status_code == 200, start.text
    state = start.json()["state"]
    register_request: dict = {}
    token_request: dict = {}

    class _Response:
        status_code = 200
        text = ""
        is_success = True

        @staticmethod
        def json():
            return {
                "access_token": "discord-install-access-token",
                "refresh_token": "discord-install-refresh-token",
                "expires_in": 604800,
                "token_type": "Bearer",
                "scope": "bot applications.commands",
                "guild": {"id": "guild-123", "name": "Manor QA"},
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **kwargs):
            token_request.update(kwargs)
            return _Response()

        async def put(self, url, **kwargs):
            register_request.update(method="PUT", url=url, **kwargs)
            return _Response()

    with patch("httpx.AsyncClient", lambda *_args, **_kwargs: _Client()):
        response = await client.get(
            f"/api/v1/integrations/oauth/discord/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    assert response.status_code == 302, response.text
    assert "/integrations?connected=discord" in response.headers["location"]
    assert "code_verifier" not in token_request["data"]

    import packages.core.database as dbmod
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.user import OAuthAccount

    async with dbmod.async_session() as db:
        account = (
            await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.provider == "discord",
                )
            )
        ).scalar_one()
        channel_config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.entity_id == entity_id,
                    ChannelConfig.channel_type == "discord",
                    ChannelConfig.credential_source_kind == "oauth_account",
                    ChannelConfig.credential_source_id == account.id,
                )
            )
        ).scalar_one()

    assert account.provider_user_id == "discord:discord-app-id:guild:guild-123"
    assert account.user_id == user_id
    assert account.profile["application_id"] == "discord-app-id"
    assert account.profile["guild_id"] == "guild-123"
    assert account.profile["guild_name"] == "Manor QA"
    assert channel_config.owner_user_id == user_id
    assert channel_config.credential_source_kind == "oauth_account"
    assert channel_config.credential_source_id == account.id
    assert channel_config.discord_application_id == "discord-app-id"
    assert channel_config.discord_guild_id == "guild-123"
    assert channel_config.name == "Manor QA"
    assert channel_config.credentials == {}
    assert "deployment-bot-token" not in json.dumps(channel_config.config)
    assert register_request["method"] == "PUT"
    assert register_request["url"].endswith(
        "/applications/discord-app-id/guilds/guild-123/commands"
    )
    assert register_request["headers"]["Authorization"] == (
        "Bot deployment-bot-token"
    )
    assert register_request["json"] == [{
        "name": "manor",
        "description": "Send a message to your bound Manor Agent",
        "type": 1,
        "options": [{
            "name": "message",
            "description": "Message for Manor",
            "type": 3,
            "required": True,
        }],
    }]


@pytest.mark.asyncio
async def test_discord_callback_rejects_duplicate_guild_installation(
    client: AsyncClient,
    monkeypatch,
) -> None:
    first_headers, _, _ = await _register_owner(client, "discord_guild_first")
    second_headers, _, _ = await _register_owner(client, "discord_guild_second")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "discord-app-id")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "discord-client-secret")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "deployment-bot-token")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "11" * 32)
    monkeypatch.setenv("APP_URL", "http://localhost:3010")
    monkeypatch.setattr(
        "apps.api.routers.integrations.validate_discord_app_config",
        AsyncMock(return_value=True),
    )

    first_start = await client.get(
        "/api/v1/integrations/oauth/discord/start",
        headers=first_headers,
    )
    second_start = await client.get(
        "/api/v1/integrations/oauth/discord/start",
        headers=second_headers,
    )
    assert first_start.status_code == 200
    assert second_start.status_code == 200

    class _Response:
        status_code = 200
        text = ""
        is_success = True

        @staticmethod
        def json():
            return {
                "access_token": "discord-install-access-token",
                "token_type": "Bearer",
                "scope": "bot applications.commands",
                "guild": {"id": "guild-duplicate", "name": "Shared QA"},
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return _Response()

        async def put(self, *_args, **_kwargs):
            return _Response()

    with patch("httpx.AsyncClient", lambda *_args, **_kwargs: _Client()):
        first = await client.get(
            "/api/v1/integrations/oauth/discord/callback"
            f"?code=first&state={first_start.json()['state']}",
            follow_redirects=False,
        )
        second = await client.get(
            "/api/v1/integrations/oauth/discord/callback"
            f"?code=second&state={second_start.json()['state']}",
            follow_redirects=False,
        )

    assert first.status_code == 302, first.text
    assert second.status_code == 409, second.text
    assert "already connected" in second.text

    import packages.core.database as dbmod
    from packages.core.models.channel import ChannelConfig

    async with dbmod.async_session() as db:
        configs = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.discord_application_id == "discord-app-id",
                    ChannelConfig.discord_guild_id == "guild-duplicate",
                )
            )
        ).scalars().all()
    assert len(configs) == 1


def test_slack_oauth_scopes_cover_mentions_and_direct_messages():
    from packages.core.services.mcp_seed import _MCP_CATALOG
    from packages.core.services.oauth_provider_config import _PROVIDER_OAUTH_META

    expected = {
        "chat:write",
        "channels:read",
        "channels:history",
        "users:read",
        "app_mentions:read",
        "im:history",
    }
    runtime = set(_PROVIDER_OAUTH_META["slack"]["scopes"].split(","))
    catalog_scopes = next(row[6] for row in _MCP_CATALOG if row[0] == "slack")

    assert runtime == expected
    assert set((catalog_scopes or "").split(",")) == expected


@pytest.mark.asyncio
async def test_slack_identity_never_falls_back_to_installer_user_id():
    from packages.core.services.oauth_flow import TokenSet, resolve_oauth_identity

    class _UnavailableProfileClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            raise RuntimeError("Slack profile unavailable")

    tokens = TokenSet(
        access_token="mock-installation-token-without-routing-metadata",
        refresh_token=None,
        expires_at=None,
        provider_user_id="U-INSTALLER-MUST-NOT-BE-IDENTITY",
        raw={"user_id": "U-INSTALLER-MUST-NOT-BE-IDENTITY"},
    )
    with patch("httpx.AsyncClient", lambda *args, **kwargs: _UnavailableProfileClient()):
        provider_user_id, _profile = await resolve_oauth_identity("slack", tokens)

    assert provider_user_id.startswith("token:")
    assert provider_user_id != "U-INSTALLER-MUST-NOT-BE-IDENTITY"


@pytest.mark.asyncio
async def test_callback_clears_stale_failed_health_on_reconnect(client: AsyncClient, monkeypatch):
    headers, user_id, _ = await _register_owner(client, "oauth_callback_reconnect")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.user import OAuthAccount, User

    connection_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=connection_id,
                user_id=user_id,
                provider="slack",
                provider_user_id="old-provider-user",
                access_token=None,
                refresh_token=None,
                profile={
                    "app_id": "A-OLD",
                    "team_id": "T-OLD",
                    "team_name": "Old workspace",
                    "enterprise_id": "E-STALE",
                    "enterprise_name": "Stale Grid",
                    "bot_user_id": "U-OLD-BOT",
                    "oauth_refresh": {"reauth_required": True},
                    "last_health_check": {
                        "ok": False,
                        "detail": "OAuth refresh token rejected by provider; reconnect.",
                        "latency_ms": 0.0,
                        "checked_at": "2026-01-01T00:00:00+00:00",
                    },
                },
            )
        )
        db.add(ChannelConfig(
            entity_id=(await db.get(User, user_id)).entity_id,
            owner_user_id=user_id,
            channel_type="slack",
            provider="slack_app",
            credential_source_kind="oauth_account",
            credential_source_id=connection_id,
            config={
                "connection_kind": "oauth_account",
                "connection_id": connection_id,
                "slack_app_id": "A-OLD",
                "slack_team_id": "T-OLD",
                "slack_enterprise_id": "E-STALE",
                "slack_bot_user_id": "U-OLD-BOT",
            },
            credentials={},
            status="active",
        ))
        await db.commit()

    start = await client.get(
        f"/api/v1/integrations/oauth/slack/start?connection_id={connection_id}",
        headers=headers,
    )
    state = start.json()["state"]

    class _MockResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "access_token": "xoxb-reconnected-token",
                "refresh_token": "rt-reconnected",
                "expires_in": 3600,
                "app_id": "A-NEW",
                "team": {"id": "T-NEW", "name": "New workspace"},
                "authed_user": {"id": "U-INSTALLER"},
            }

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _MockResp()

    with patch("httpx.AsyncClient", lambda *a, **kw: _MockClient()):
        resp = await client.get(
            f"/api/v1/integrations/oauth/slack/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    assert resp.status_code == 302

    async with dbmod.async_session() as db:
        row = (
            await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.provider == "slack",
                )
            )
        ).scalar_one()
        channel_config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.credential_source_id == connection_id,
                )
            )
        ).scalar_one()
    assert row.access_token is None
    assert row.refresh_token is None
    assert _leased_tokens(row) == {
        "access_token": "xoxb-reconnected-token",
        "refresh_token": "rt-reconnected",
    }
    assert "last_health_check" not in row.profile
    assert "oauth_refresh" not in row.profile
    assert row.provider_user_id == "slack:A-NEW:team:T-NEW"
    assert row.profile["team_id"] == "T-NEW"
    assert "enterprise_id" not in row.profile
    assert "bot_user_id" not in row.profile
    assert channel_config.config["slack_team_id"] == "T-NEW"
    assert "slack_enterprise_id" not in channel_config.config
    assert "slack_bot_user_id" not in channel_config.config


@pytest.mark.asyncio
async def test_oauth_callback_keeps_multiple_external_accounts(
    client: AsyncClient,
    monkeypatch,
):
    """Connecting a second provider identity creates a sibling connection."""
    headers, user_id, _ = await _register_owner(client, "oauth_multi_callback")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    token_payloads = {
        "first": {
            "access_token": "token-first",
            "app_id": "A-MANOR",
            "authed_user": {"id": "U-SAME-INSTALLER"},
            "team": {"id": "T-FIRST", "name": "First workspace"},
        },
        "second": {
            "access_token": "token-second",
            "app_id": "A-MANOR",
            "authed_user": {"id": "U-SAME-INSTALLER"},
            "team": {"id": "T-SECOND", "name": "Second workspace"},
        },
    }

    class _MockResp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, _url, *, data, **_kwargs):
            return _MockResp(token_payloads[data["code"]])

    for code in ("first", "second"):
        start = await client.get(
            "/api/v1/integrations/oauth/slack/start",
            headers=headers,
        )
        state = start.json()["state"]
        with patch("httpx.AsyncClient", lambda *args, **kwargs: _MockClient()):
            response = await client.get(
                f"/api/v1/integrations/oauth/slack/callback?code={code}&state={state}",
                follow_redirects=False,
            )
        assert response.status_code == 302

    import packages.core.database as dbmod
    from packages.core.models.user import OAuthAccount

    async with dbmod.async_session() as db:
        rows = list((await db.execute(
            select(OAuthAccount).where(
                OAuthAccount.user_id == user_id,
                OAuthAccount.provider == "slack",
            )
        )).scalars().all())

    assert {row.provider_user_id for row in rows} == {
        "slack:A-MANOR:team:T-FIRST",
        "slack:A-MANOR:team:T-SECOND",
    }
    assert all(row.access_token is None for row in rows)
    assert {_leased_tokens(row)["access_token"] for row in rows} == {
        "token-first",
        "token-second",
    }
    assert sum(bool((row.profile or {}).get("is_default")) for row in rows) == 1


@pytest.mark.asyncio
async def test_callback_redirects_to_safe_return_to(client: AsyncClient, monkeypatch):
    headers, _, _ = await _register_owner(client, "oauth_return_to")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    start = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
        params={"return_to": "/settings?tab=calendar"},
    )
    state = start.json()["state"]

    class _MockResp:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "xoxb-token"}

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _MockResp()

    with patch("httpx.AsyncClient", lambda *a, **kw: _MockClient()):
        resp = await client.get(
            f"/api/v1/integrations/oauth/slack/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:3010/settings?tab=calendar&connected=slack"


@pytest.mark.asyncio
async def test_callback_ignores_external_return_to(client: AsyncClient, monkeypatch):
    headers, _, _ = await _register_owner(client, "oauth_external_return_to")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    start = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
        params={"return_to": "https://evil.example/steal"},
    )
    state = start.json()["state"]

    class _MockResp:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "xoxb-token"}

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _MockResp()

    with patch("httpx.AsyncClient", lambda *a, **kw: _MockClient()):
        resp = await client.get(
            f"/api/v1/integrations/oauth/slack/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:3010/integrations?connected=slack"


@pytest.mark.asyncio
async def test_callback_rejects_bad_state(client: AsyncClient):
    resp = await client.get(
        "/api/v1/integrations/oauth/slack/callback?code=x&state=fake-state",
        follow_redirects=False,
    )
    assert resp.status_code == 400
