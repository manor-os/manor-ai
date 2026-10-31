"""Public (native MCP) OAuth clients against disposable SQLite and the real routes.

Covers what a self-registered client may do: register only loopback/hosted
callbacks (rate-limited per IP), redeem with PKCE and no secret on any loopback
port, receive tokens the REST API rejects, and rotate refresh tokens with reuse,
sign-out and per-connection revocation; plus the sweep of unused clients.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from packages.core.services.oauth_provider_service import MCP_SCOPES
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, MetaData, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value

from apps.api.middleware.rate_limit import RateLimiter
from apps.api.routers import oauth_metadata, oauth_provider as routes
from packages.core.models.oauth_provider import OAuthAuthorizationCode, OAuthClientApp, OAuthRefreshToken
from packages.core.models.staff import Department, Staff, StaffRole
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services import auth_service
from packages.core.services import oauth_provider_service as svc

pytestmark = pytest.mark.integration

ENTITY = "mcp-team"
USER_ID = "mcp-user"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
CALLBACK = "http://localhost:53682/callback"


@pytest.fixture
async def public_oauth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_service.settings, "JWT_SECRET_KEY", "isolated-public-oauth-signing-key-123456")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth.sqlite'}")
    metadata = MetaData()
    for model in (Entity, User, UserMembership, Department, StaffRole, Staff,
                  OAuthClientApp, OAuthAuthorizationCode, OAuthRefreshToken):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, (JSONB, ARRAY)):
                column.type = JSON()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with sessions.begin() as db:
        db.add_all([
            Entity(id=ENTITY, name="Team"),
            User(id=USER_ID, entity_id=ENTITY, email="fde@example.test", password_hash="unused",
                 role="owner", status="active", token_version=1),
            UserMembership(user_id=USER_ID, entity_id=ENTITY, role="owner", status="active"),
        ])

    # Per-test in-memory buckets: registration is limited even without Redis.
    monkeypatch.setattr(routes, "_register_limiter", RateLimiter(redis_enabled=False))
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(oauth_metadata.router)

    async def database():
        async with sessions() as db:
            yield db

    async def user():
        async with sessions() as db:
            row = await db.get(User, USER_ID)
            set_committed_value(row, "entity_id", ENTITY)
            return row

    app.dependency_overrides[routes.get_db] = database
    app.dependency_overrides[routes.get_current_user] = user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manor.test") as http:
            yield http, sessions
    finally:
        await engine.dispose()


async def _register(http, **metadata):
    body = {"client_name": "Claude Code", "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], **metadata}
    return await http.post("/api/v1/oauth/register", json=body)


async def _sign_in(http, client_id, redirect_uri=CALLBACK, scope="mcp"):
    code = (await http.post("/api/v1/oauth/authorize", json={
        "client_id": client_id, "redirect_uri": redirect_uri, "state": "s", "scope": scope,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256",
    }))
    assert code.status_code == 200, code.text
    return await http.post("/api/v1/oauth/token", data={
        "grant_type": "authorization_code", "code": code.json()["code"], "client_id": client_id,
        "redirect_uri": redirect_uri, "code_verifier": VERIFIER,
        "resource": "http://manor.test/api/v1/mcp",
    })


@pytest.mark.parametrize("redirect_uri", [
    "https://evil.example/callback",
    "http://evil.example/callback",
    "http://localhost.evil.example/callback",
    "http://user@localhost/callback",
    "custom-scheme://callback",
])
async def test_registration_rejects_redirects_outside_this_machine(public_oauth, redirect_uri):
    http, sessions = public_oauth
    response = await _register(http, redirect_uris=[redirect_uri])
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    async with sessions() as db:
        assert (await db.scalars(select(OAuthClientApp))).all() == []


async def test_registration_only_issues_public_clients(public_oauth):
    http, _ = public_oauth
    response = await _register(http, token_endpoint_auth_method="client_secret_post")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


async def test_public_flow_any_loopback_port_and_audience_bound_tokens(public_oauth):
    http, sessions = public_oauth
    registered = await _register(http)
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    assert registered.json()["token_endpoint_auth_method"] == "none"

    info = (await http.get(f"/api/v1/oauth/clients/{client_id}",
                           params={"redirect_uri": "http://localhost:61000/callback"})).json()
    assert info["verified"] is False and info["redirect_uri_allowed"] is True
    other_path = (await http.get(f"/api/v1/oauth/clients/{client_id}",
                                 params={"redirect_uri": "http://localhost:61000/other"})).json()
    assert other_path["redirect_uri_allowed"] is False

    # Native apps bind a fresh port per sign-in (RFC 8252 §7.3).
    token = await _sign_in(http, client_id, "http://localhost:61000/callback")
    assert token.status_code == 200, token.text
    body = token.json()
    assert body["scope"] == "mcp"  # the advertised scope is accepted and echoed
    assert token.headers["cache-control"] == "no-store"
    assert body["refresh_token"]
    assert auth_service.decode_token(body["access_token"]) is None  # REST rejects it
    claims = auth_service.decode_token(body["access_token"], audience=svc.MCP_TOKEN_AUDIENCE)
    assert claims["sub"] == USER_ID and claims["entity_id"] == ENTITY




def test_mcp_calls_reach_rest_as_an_agent_with_the_granted_scopes() -> None:
    from apps.api.mcp.manor_api import ManorApi

    outer = {"sub": USER_ID, "entity_id": ENTITY, "role": "owner", "scp": ["mcp"], "azp": "mcp-abc"}
    inner = auth_service.decode_token(ManorApi(None, claims=outer)._token())
    assert inner["act"] == {"sub": "mcp:mcp-abc"} and inner["scp"] == ["mcp"]
    # A token from before scopes existed carries just "mcp".
    legacy = auth_service.decode_token(ManorApi(None, claims={"sub": USER_ID, "entity_id": ENTITY})._token())
    assert legacy["scp"] == ["mcp"] and legacy["act"] == {"sub": "mcp:client"}


async def test_public_client_secret_is_refused_and_pkce_required(public_oauth):
    http, _ = public_oauth
    client_id = (await _register(http)).json()["client_id"]
    no_pkce = await http.post("/api/v1/oauth/authorize", json={
        "client_id": client_id, "redirect_uri": CALLBACK})
    assert no_pkce.status_code == 400
    code = (await http.post("/api/v1/oauth/authorize", json={
        "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256"})).json()["code"]
    with_secret = await http.post("/api/v1/oauth/token", json={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": "guess", "redirect_uri": CALLBACK, "code_verifier": VERIFIER})
    assert with_secret.status_code == 400 and with_secret.json()["error"] == "invalid_client"


async def test_refresh_rotates_and_reuse_revokes_the_family(public_oauth):
    http, sessions = public_oauth
    client_id = (await _register(http)).json()["client_id"]
    first = (await _sign_in(http, client_id)).json()["refresh_token"]

    rotated = await http.post("/api/v1/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first, "client_id": client_id})
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()["refresh_token"]
    assert second != first
    assert auth_service.decode_token(rotated.json()["access_token"], audience=svc.MCP_TOKEN_AUDIENCE)

    # Replaying the rotated token (theft signal) kills every token in the family.
    replay = await http.post("/api/v1/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first, "client_id": client_id})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    after = await http.post("/api/v1/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": second, "client_id": client_id})
    assert after.status_code == 400
    async with sessions() as db:
        rows = (await db.scalars(select(OAuthRefreshToken))).all()
        assert rows and all(row.revoked_at is not None for row in rows)


async def test_sign_out_everywhere_ends_refresh(public_oauth):
    http, sessions = public_oauth
    client_id = (await _register(http)).json()["client_id"]
    refresh = (await _sign_in(http, client_id)).json()["refresh_token"]
    async with sessions.begin() as db:
        await db.execute(update(User).where(User.id == USER_ID).values(token_version=2))
    response = await http.post("/api/v1/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


async def test_discovery_documents_point_clients_at_this_server(public_oauth, monkeypatch):
    http, _ = public_oauth
    monkeypatch.setenv("APP_URL", "")
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "APP_URL", "https://app.manor.test")
    server = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert server["issuer"] == "https://app.manor.test"
    assert server["registration_endpoint"] == "https://app.manor.test/api/v1/oauth/register"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert "none" in server["token_endpoint_auth_methods_supported"]
    assert server["scopes_supported"] == list(MCP_SCOPES) and MCP_SCOPES[0] == "mcp"
    assert server["resource"] == "https://app.manor.test/api/v1/mcp"
    resource = (await http.get("/.well-known/oauth-protected-resource/api/v1/mcp")).json()
    assert resource["resource"] == "https://app.manor.test/api/v1/mcp"
    assert resource["authorization_servers"] == ["https://app.manor.test"]
    assert resource["scopes_supported"] == list(MCP_SCOPES)


async def test_registration_is_rate_limited_per_ip(public_oauth, monkeypatch):
    http, sessions = public_oauth
    monkeypatch.setattr(routes, "REGISTER_RATE_LIMIT", 2)
    for _ in range(2):
        assert (await _register(http)).status_code == 201
    # Invalid attempts count too; the limit applies before the body is read.
    limited = await _register(http, redirect_uris=["https://evil.example/cb"])
    assert limited.status_code == 429
    assert limited.json()["error"] == "too_many_requests"
    assert int(limited.headers["retry-after"]) > 0
    assert (await _register(http)).status_code == 429
    async with sessions() as db:
        assert len((await db.scalars(select(OAuthClientApp))).all()) == 2

    other = ASGITransport(app=http._transport.app, client=("203.0.113.9", 4000))
    async with AsyncClient(transport=other, base_url="http://manor.test") as elsewhere:
        assert (await _register(elsewhere)).status_code == 201


async def _refresh(http, client_id, refresh_token):
    return await http.post("/api/v1/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id})


async def test_revoking_a_connection_ends_only_that_family(public_oauth):
    http, sessions = public_oauth
    claude = (await _register(http, client_name="Claude Code")).json()["client_id"]
    codex = (await _register(http, client_name="Codex")).json()["client_id"]
    claude_refresh = (await _sign_in(http, claude)).json()["refresh_token"]
    claude_second = (await _sign_in(http, claude)).json()["refresh_token"]  # another machine
    codex_refresh = (await _sign_in(http, codex)).json()["refresh_token"]
    async with sessions.begin() as db:
        db.add(OAuthRefreshToken(
            token_hash="f" * 64, family_id="other-users-family", client_id=claude,
            user_id="someone-else", entity_id=ENTITY, scope=[], token_version=0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1)))

    listed = (await http.get("/api/v1/oauth/connections")).json()
    assert sorted(c["name"] for c in listed) == ["Claude Code", "Claude Code", "Codex"]
    assert all(c["verified"] is False for c in listed)
    async with sessions() as db:
        family = (await db.scalar(select(OAuthRefreshToken.family_id).where(
            OAuthRefreshToken.token_hash == svc._hash_refresh_token(claude_refresh))))

    assert (await http.delete(f"/api/v1/oauth/connections/{family}")).status_code == 204
    revoked = await _refresh(http, claude, claude_refresh)
    assert revoked.status_code == 400 and revoked.json()["error"] == "invalid_grant"
    # The same client on another machine and a different client keep refreshing.
    assert (await _refresh(http, claude, claude_second)).status_code == 200
    assert (await _refresh(http, codex, codex_refresh)).status_code == 200
    remaining = (await http.get("/api/v1/oauth/connections")).json()
    assert len(remaining) == 2 and family not in {c["id"] for c in remaining}

    assert (await http.delete(f"/api/v1/oauth/connections/{family}")).status_code == 404
    # Another user's connection is not revocable (nor visible) from this account.
    assert (await http.delete("/api/v1/oauth/connections/other-users-family")).status_code == 404
    async with sessions() as db:
        foreign = await db.scalar(select(OAuthRefreshToken).where(
            OAuthRefreshToken.family_id == "other-users-family"))
        assert foreign.revoked_at is None


async def test_cleanup_removes_only_unused_dynamic_clients(public_oauth, monkeypatch):
    http, sessions = public_oauth
    monkeypatch.setenv("MANOR_OAUTH_DYNAMIC_CLIENT_TTL_HOURS", "24")
    ids = {name: (await _register(http, client_name=name)).json()["client_id"]
           for name in ("abandoned", "in_use", "signing_in", "revoked", "recent")}
    await _sign_in(http, ids["in_use"])
    await http.post("/api/v1/oauth/authorize", json={  # consent given, code not yet redeemed
        "client_id": ids["signing_in"], "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
    await _sign_in(http, ids["revoked"])
    revoked_family = next(c["id"] for c in (await http.get("/api/v1/oauth/connections")).json()
                          if c["client_id"] == ids["revoked"])
    assert (await http.delete(f"/api/v1/oauth/connections/{revoked_family}")).status_code == 204

    now = datetime.now(timezone.utc)
    async with sessions.begin() as db:
        db.add(OAuthClientApp(client_id="seeded-app", client_secret_hash="x", name="Seeded",
                              redirect_uris=[CALLBACK], allowed_scopes=[]))
        await db.flush()
        await db.execute(update(OAuthClientApp).values(created_at=now - timedelta(days=3)))
        await db.execute(update(OAuthClientApp).where(OAuthClientApp.client_id == ids["recent"])
                         .values(created_at=now - timedelta(hours=1)))

    async with sessions.begin() as db:
        assert await svc.cleanup_unused_dynamic_clients(db, now=now) == 2
    async with sessions() as db:
        left = set((await db.scalars(select(OAuthClientApp.client_id))).all())
        assert left == {ids["in_use"], ids["signing_in"], ids["recent"], "seeded-app"}
        orphans = (await db.scalars(select(OAuthRefreshToken.client_id))).all()
        assert ids["revoked"] not in orphans and ids["in_use"] in orphans
    async with sessions.begin() as db:  # idempotent
        assert await svc.cleanup_unused_dynamic_clients(db, now=now) == 0
