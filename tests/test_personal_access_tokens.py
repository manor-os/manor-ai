"""Personal access tokens for the hosted MCP endpoint, through the real routes.

Created by a signed-in user (plaintext shown once), admitted only by the MCP
endpoint with the user's live account and team, within their scope; dead once
revoked, expired or the user signs out everywhere; never accepted by REST.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from packages.core.models.oauth_provider import PersonalAccessToken
from packages.core.models.user import User
from packages.core.services import auth_service
from tests.test_mcp_endpoint import ENTITY, USER_ID, _rpc, mcp_http  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


def _web_headers():
    token = auth_service.create_access_token(USER_ID, ENTITY, "owner", token_version=1)
    return {"Authorization": f"Bearer {token}"}


async def _create(http, **body):
    response = await http.post("/api/v1/oauth/personal-tokens", headers=_web_headers(),
                               json={"name": "xAI remote MCP", **body})
    assert response.status_code == 201, response.text
    return response.json()


async def _whoami(http, token):
    return await _rpc(http, "tools/call", {"name": "whoami", "arguments": {}}, token=token)


async def test_token_is_shown_once_acts_as_its_user_and_is_listed_without_its_secret(mcp_http):
    http, sessions, _ = mcp_http
    created = await _create(http)
    token = created["token"]
    assert token.startswith("manor_pat_") and created["token_prefix"] == token[:18]
    assert created["kind"] == "personal_token" and created["scope"] == "mcp"
    expires = datetime.fromisoformat(created["expires_at"])
    assert timedelta(days=89) < expires - datetime.now(timezone.utc) <= timedelta(days=90)
    async with sessions() as db:
        row = await db.scalar(select(PersonalAccessToken))
        assert token not in (row.token_hash, row.token_prefix)

    response = await _whoami(http, token)
    assert response.status_code == 200, response.text
    me = json.loads(response.json()["result"]["content"][0]["text"])
    assert (me["id"], me["entity_id"]) == (USER_ID, ENTITY)

    listed = (await http.get("/api/v1/oauth/connections", headers=_web_headers())).json()
    assert [(c["kind"], c["id"], c["name"]) for c in listed] == [("personal_token", created["id"], "xAI remote MCP")]
    assert "token" not in listed[0] and listed[0]["last_used_at"] is not None


async def test_rest_routes_never_accept_a_personal_access_token(mcp_http):
    http, _, _ = mcp_http
    token = (await _create(http))["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await http.get("/api/v1/auth/me", headers=headers)).status_code == 401
    # Nor can it mint more tokens or see connections.
    assert (await http.get("/api/v1/oauth/connections", headers=headers)).status_code == 401
    assert (await http.post("/api/v1/oauth/personal-tokens", headers=headers, json={"name": "x"})).status_code == 401


async def test_read_only_token_cannot_reach_write_tools(mcp_http):
    http, _, _ = mcp_http
    token = (await _create(http, scope="mcp:read"))["token"]
    tools = {t["name"] for t in (await _rpc(http, "tools/list", token=token)).json()["result"]["tools"]}
    assert "whoami" in tools and "apply_workspace_change" not in tools
    refused = await _rpc(http, "tools/call", {"name": "apply_workspace_change", "arguments": {}}, token=token)
    assert refused.status_code == 403 and 'error="insufficient_scope"' in refused.headers["www-authenticate"]


@pytest.mark.parametrize("scope", [
    "unknown", "mcp mcp:read",
])
async def test_personal_tokens_accept_only_their_own_resource_grants(mcp_http, scope):
    http, sessions, _ = mcp_http
    response = await http.post("/api/v1/oauth/personal-tokens", headers=_web_headers(),
                               json={"name": "Invalid grant", "scope": scope})
    assert response.status_code == 422 and response.json()["detail"] == "invalid_scope", response.text
    async with sessions() as db:
        assert await db.scalar(select(PersonalAccessToken)) is None


async def test_revoked_expired_and_signed_out_tokens_are_refused(mcp_http):
    http, sessions, _ = mcp_http
    revoked, expired, kept = [await _create(http, name=n) for n in ("revoked", "expired", "kept")]

    assert (await http.delete(f"/api/v1/oauth/connections/{revoked['id']}", headers=_web_headers())).status_code == 204
    assert (await http.delete(f"/api/v1/oauth/connections/{revoked['id']}", headers=_web_headers())).status_code == 404
    async with sessions.begin() as db:
        await db.execute(update(PersonalAccessToken).where(PersonalAccessToken.id == expired["id"])
                         .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    for dead in (revoked, expired):
        response = await _whoami(http, dead["token"])
        assert response.status_code == 401 and 'error="invalid_token"' in response.headers["www-authenticate"]
    assert (await _whoami(http, kept["token"])).status_code == 200
    listed = {c["id"] for c in (await http.get("/api/v1/oauth/connections", headers=_web_headers())).json()}
    assert listed == {kept["id"]}

    # Sign out everywhere ends every token, as it ends refresh tokens (decision D2).
    async with sessions.begin() as db:
        await db.execute(update(User).where(User.id == USER_ID).values(token_version=2))
    assert (await _whoami(http, kept["token"])).status_code == 401
    assert (await _whoami(http, "manor_pat_" + "x" * 43)).status_code == 401


async def test_another_users_token_cannot_be_revoked(mcp_http):
    http, sessions, _ = mcp_http
    created = await _create(http)
    async with sessions.begin() as db:
        await db.execute(update(PersonalAccessToken).values(user_id="someone-else"))
    assert (await http.delete(f"/api/v1/oauth/connections/{created['id']}", headers=_web_headers())).status_code == 404
    async with sessions() as db:
        assert (await db.scalar(select(PersonalAccessToken))).revoked_at is None


async def test_last_used_is_written_at_most_every_five_minutes(mcp_http):
    http, sessions, _ = mcp_http
    created = await _create(http)

    async def last_used():
        async with sessions() as db:
            return (await db.scalar(select(PersonalAccessToken))).last_used_at

    assert await last_used() is None
    await _whoami(http, created["token"])
    first = await last_used()
    assert first is not None
    await _whoami(http, created["token"])
    assert await last_used() == first

    stale = datetime.now(timezone.utc) - timedelta(minutes=6)
    async with sessions.begin() as db:
        await db.execute(update(PersonalAccessToken).values(last_used_at=stale))
    await _whoami(http, created["token"])
    assert (await last_used()).replace(tzinfo=timezone.utc) > stale + timedelta(minutes=5)


@pytest.mark.parametrize("body,status", [
    ({"expires_in_days": None}, 201),
    ({"expires_in_days": 365}, 201),
    ({"expires_in_days": 7}, 422),
    ({"scope": "admin"}, 422),
    ({"name": "   "}, 422),
])
async def test_creation_accepts_only_the_offered_lifetimes_and_scopes(mcp_http, body, status):
    http, _, _ = mcp_http
    response = await http.post("/api/v1/oauth/personal-tokens", headers=_web_headers(),
                               json={"name": "CI", **body})
    assert response.status_code == status, response.text
    if status == 201 and body.get("expires_in_days", 0) is None:
        assert response.json()["expires_at"] is None
