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

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select


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
async def test_oauth_start_db_overrides_env(client: AsyncClient, monkeypatch):
    """DB-stored client_id beats env."""
    headers, _, _ = await _register_owner(client, "oauth_db")

    monkeypatch.setenv("DISCORD_CLIENT_ID", "env_cid")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "env_csec")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        server = await _seed_mcp_server(db, "discord")
        cfg = dict(server.default_config or {})
        cfg["oauth_client_id"] = "oss_cid"
        cfg["oauth_client_secret"] = "oss_csec"
        server.default_config = cfg
        await db.commit()

    resp = await client.get(
        "/api/v1/integrations/oauth/discord/start",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "db"
    assert "client_id=oss_cid" in data["authorize_url"]


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
async def test_callback_exchanges_code_and_stores_token(client: AsyncClient, monkeypatch):
    headers, user_id, _ = await _register_owner(client, "oauth_callback")
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
    assert row.access_token == "xoxb-new-token"
    assert row.refresh_token == "rt-new"
    assert row.token_expires_at is not None


@pytest.mark.asyncio
async def test_callback_clears_stale_failed_health_on_reconnect(client: AsyncClient, monkeypatch):
    headers, user_id, _ = await _register_owner(client, "oauth_callback_reconnect")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")

    start = await client.get(
        "/api/v1/integrations/oauth/slack/start",
        headers=headers,
    )
    state = start.json()["state"]

    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount

    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="slack",
                provider_user_id="old-provider-user",
                access_token=None,
                refresh_token=None,
                profile={
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
        await db.commit()

    class _MockResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "access_token": "xoxb-reconnected-token",
                "refresh_token": "rt-reconnected",
                "expires_in": 3600,
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
    assert row.access_token == "xoxb-reconnected-token"
    assert row.refresh_token == "rt-reconnected"
    assert "last_health_check" not in row.profile
    assert "oauth_refresh" not in row.profile


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
