"""Exercise Robinhood's real Manor routes with a simulated vendor response."""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_robinhood_callback_stores_only_starting_users_encrypted_grant(client, monkeypatch):
    from packages.core import database
    from packages.core.models.user import OAuthAccount
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    users = []
    for name in ("rh_owner_a", "rh_owner_b"):
        response = await client.post("/api/v1/auth/register", json={
            "username": name, "email": f"{name}@example.test", "password": "pass123",
            "entity_name": name,
        })
        data = response.json()
        users.append((data["user_id"], {"Authorization": f"Bearer {data['access_token']}"}))
    owner_id, owner_headers = users[0]
    _, other_headers = users[1]
    monkeypatch.setenv("ROBINHOOD_CLIENT_ID", "test-public-client")
    monkeypatch.setenv("APP_URL", "https://app.example.test")
    start = await client.get("/api/v1/integrations/oauth/robinhood/start", headers=owner_headers)
    assert start.status_code == 200
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    state = start.json()["state"]
    post = AsyncMock(return_value=httpx.Response(200, json={
        "access_token": "fixture-user-access", "refresh_token": "fixture-user-refresh",
        "expires_in": 3600,
    }))
    upstream = AsyncMock()
    upstream.__aenter__.return_value.post = post
    with monkeypatch.context() as vendor:
        vendor.setattr(httpx, "AsyncClient", lambda **_kwargs: upstream)
        # Callback cookies/auth headers do not change the owner captured in state.
        callback = await client.get(
            "/api/v1/integrations/oauth/robinhood/callback",
            params={"code": "fixture-code", "state": state},
            headers=other_headers, follow_redirects=False,
        )
    assert callback.status_code == 302
    assert "connected=robinhood" in callback.headers["location"]
    assert "client_secret" not in post.call_args.kwargs["data"]
    async with database.async_session() as db:
        accounts = (await db.execute(select(OAuthAccount).where(
            OAuthAccount.provider == "robinhood",
        ))).scalars().all()
        assert len(accounts) == 1
        account = accounts[0]
        assert account.user_id == owner_id
        assert account.access_token is None and account.refresh_token is None
        assert account.credential_ref
        tokens = lease_oauth_account_tokens(
            account, requester_id="robinhood-flow-test", requester_kind="test",
            reason="verify Robinhood callback encrypts user tokens",
        )
        assert tokens["refresh_token"] == "fixture-user-refresh"
        account_id = account.id
    denied = await client.get(
        "/api/v1/integrations/oauth/robinhood/start",
        params={"connection_id": account_id}, headers=other_headers,
    )
    assert denied.status_code == 404
    replay = await client.get(
        "/api/v1/integrations/oauth/robinhood/callback",
        params={"code": "fixture-code", "state": state},
    )
    assert replay.status_code == 400
    post.assert_awaited_once()
