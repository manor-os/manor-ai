"""Public (native MCP) OAuth clients against disposable SQLite and the real routes.

Covers what a self-registered client may do: register only loopback/hosted
callbacks (rate-limited per IP), redeem with PKCE and no secret on any loopback
port, receive tokens the REST API rejects, and rotate refresh tokens with reuse,
sign-out and per-connection revocation; plus the sweep of unused clients.
Client ID Metadata Document clients are fetched (behind the public-address,
redirect, size and type guards), validated and cached until stale.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlsplit

import httpx
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
from packages.core.models.oauth_provider import (
    OAuthAuthorizationCode,
    OAuthClientApp,
    OAuthRefreshToken,
    PersonalAccessToken,
)
from packages.core.models.staff import Department, Staff, StaffRole
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services import auth_service
from packages.core.services import oauth_client_metadata as cimd
from packages.core.services import oauth_provider_service as svc

pytestmark = pytest.mark.integration

ENTITY = "mcp-team"
USER_ID = "mcp-user"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
CALLBACK = "http://localhost:53682/callback"
DOCUMENT_URL = "https://client.example.test/oauth/client.json"
HOSTED_CALLBACK = "https://client.example.test/oauth/callback"


@pytest.fixture
async def public_oauth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_service.settings, "JWT_SECRET_KEY", "isolated-public-oauth-signing-key-123456")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth.sqlite'}")
    metadata = MetaData()
    for model in (Entity, User, UserMembership, Department, StaffRole, Staff,
                  OAuthClientApp, OAuthAuthorizationCode, OAuthRefreshToken, PersonalAccessToken):
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
    assert server["revocation_endpoint"] == "https://app.manor.test/api/v1/oauth/revoke"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert {"none", "client_secret_post", "client_secret_basic"} == set(server["token_endpoint_auth_methods_supported"])
    assert server["authorization_response_iss_parameter_supported"] is True
    assert server["client_id_metadata_document_supported"] is True
    assert server["scopes_supported"] == list(MCP_SCOPES) and "mcp:read" in MCP_SCOPES
    assert server["resource"] == "https://app.manor.test/api/v1/mcp"
    resource = (await http.get("/.well-known/oauth-protected-resource/api/v1/mcp")).json()
    assert resource["resource"] == "https://app.manor.test/api/v1/mcp"
    assert resource["authorization_servers"] == ["https://app.manor.test"]
    assert resource["scopes_supported"] == list(MCP_SCOPES)

    # RFC 9207: every authorization response names this same issuer.
    client_id = (await _register(http)).json()["client_id"]
    info = (await http.get(f"/api/v1/oauth/clients/{client_id}")).json()
    assert info["issuer"] == server["issuer"] and info["read_only_available"] is True
    authorized = (await http.post("/api/v1/oauth/authorize", json={
        "client_id": client_id, "redirect_uri": CALLBACK, "state": "s",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256"})).json()
    query = parse_qs(urlsplit(authorized["redirect_to"]).query)
    assert query["iss"] == [server["issuer"]] and query["state"] == ["s"]


@pytest.mark.parametrize("uri", [
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect",
    "https://claude.ai/api/mcp/auth_callback",
])
async def test_registration_accepts_the_default_hosted_callbacks(public_oauth, uri):
    http, _ = public_oauth
    assert (await _register(http, redirect_uris=[uri])).status_code == 201


@pytest.mark.parametrize("uri,allowed", [
    ("https://consent.example.test/redirect/abc123", True),
    ("https://CONSENT.example.test/redirect/abc123", True),
    ("https://consent.example.test/redirect/../steal", False),
    ("https://consent.example.test/redirect/%2e%2e/steal", False),
    ("https://consent.example.test/redirectx", False),
    ("http://consent.example.test/redirect/abc123", False),
    ("https://user@consent.example.test/redirect/abc123", False),
    ("cursor://anysphere.cursor-mcp/oauth/callback", True),
    ("CURSOR://anysphere.cursor-mcp/oauth/callback", True),
    ("javascript://alert/1", False),
    ("https://claude.ai/api/mcp/auth_callback", False),  # the env list replaces the defaults
])
async def test_configured_redirect_rules(public_oauth, monkeypatch, uri, allowed):
    http, _ = public_oauth
    monkeypatch.setenv("MANOR_OAUTH_DYNAMIC_REDIRECT_URIS", ",".join([
        "prefix:https://consent.example.test/redirect/",
        "scheme:cursor",
        "scheme:javascript",  # a shared scheme is never a native app's: ignored
        "prefix:https://no-trailing-slash.example.test/redirect",  # invalid: ignored
    ]))
    response = await _register(http, redirect_uris=[uri])
    assert response.status_code == (201 if allowed else 400), response.text


async def test_refused_callback_is_logged_by_host_only(public_oauth, caplog):
    http, _ = public_oauth
    with caplog.at_level("INFO", logger=svc.__name__):
        response = await _register(http, redirect_uris=["https://new-host.example/cb/secret-path?x=1"])
    assert response.status_code == 400
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "https://new-host.example" in logged and "secret-path" not in logged


async def test_resource_indicator_must_name_this_servers_mcp_resource(public_oauth):
    http, _ = public_oauth
    client_id = (await _register(http)).json()["client_id"]

    def authorize(resource):
        return http.post("/api/v1/oauth/authorize", json={
            "client_id": client_id, "redirect_uri": CALLBACK, "resource": resource,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"})

    for foreign in ("https://other.example/api/v1/mcp", "http://manor.test/api/v1/documents",
                    "http://manor.test/api/v1/mcp#frag", "not a url"):
        response = await authorize(foreign)
        assert response.status_code == 400 and response.json()["detail"] == "invalid_target", foreign
    for own in ("http://manor.test/api/v1/mcp", "http://MANOR.test:80/api/v1/mcp/", "http://manor.test"):
        assert (await authorize(own)).status_code == 200, own

    code = (await authorize("http://manor.test/api/v1/mcp")).json()["code"]
    exchange = {"grant_type": "authorization_code", "code": code, "client_id": client_id,
                "redirect_uri": CALLBACK, "code_verifier": VERIFIER}
    wrong = await http.post("/api/v1/oauth/token", data={**exchange, "resource": "https://other.example/api/v1/mcp"})
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_target"
    # The refused request did not consume the code.
    assert (await http.post("/api/v1/oauth/token", data=exchange)).status_code == 200


async def test_scope_grants_read_only_on_request_and_full_mcp_by_default(public_oauth):
    http, _ = public_oauth
    client_id = (await _register(http)).json()["client_id"]

    async def sign_in(scope):
        code = (await http.post("/api/v1/oauth/authorize", json={
            "client_id": client_id, "redirect_uri": CALLBACK, "scope": scope,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"})).json()["code"]
        return (await http.post("/api/v1/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": CALLBACK, "code_verifier": VERIFIER})).json()

    def claims(body):
        return auth_service.decode_token(body["access_token"], audience=svc.MCP_TOKEN_AUDIENCE)

    read_only = await sign_in("mcp:read offline_access")
    assert read_only["scope"] == "mcp:read" and claims(read_only)["scope"] == "mcp:read"
    assert claims(read_only)["scp"] == ["mcp:read"]
    assert svc.mcp_read_only(claims(read_only))
    renewed = (await _refresh(http, client_id, read_only["refresh_token"])).json()
    assert renewed["scope"] == "mcp:read"  # a renewal never widens the grant
    for scope in ("", "mcp mcp:read", "offline_access"):
        body = await sign_in(scope)
        assert body["scope"] == "mcp" and not svc.mcp_read_only(claims(body)), scope
    for scope in ("unknown", "mcp unknown"):
        refused = await http.post("/api/v1/oauth/authorize", json={
            "client_id": client_id, "redirect_uri": CALLBACK, "scope": scope,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
        assert refused.status_code == 400 and "scope_not_allowed" in refused.text


async def test_revocation_ends_the_family_and_reveals_nothing(public_oauth):
    http, _ = public_oauth
    client_id = (await _register(http)).json()["client_id"]
    other = (await _register(http, client_name="Other")).json()["client_id"]
    first = (await _sign_in(http, client_id)).json()
    second = (await _refresh(http, client_id, first["refresh_token"])).json()["refresh_token"]
    others = (await _sign_in(http, other)).json()["refresh_token"]

    # Another client cannot revoke a token it doesn't hold; the answer is the same.
    assert (await http.post("/api/v1/oauth/revoke", data={"token": second, "client_id": other})).status_code == 200
    assert (await _refresh(http, client_id, second)).status_code == 200
    third = (await _sign_in(http, client_id)).json()["refresh_token"]
    revoked = await http.post("/api/v1/oauth/revoke", data={"token": third, "client_id": client_id})
    assert revoked.status_code == 200
    assert (await _refresh(http, client_id, third)).json()["error"] == "invalid_grant"
    assert (await http.post("/api/v1/oauth/revoke", data={"token": "unknown", "client_id": client_id})).status_code == 200
    jwt_shaped = await http.post("/api/v1/oauth/revoke", data={"token": first["access_token"], "client_id": client_id})
    assert jwt_shaped.status_code == 400 and jwt_shaped.json()["error"] == "unsupported_token_type"
    unknown_client = await http.post("/api/v1/oauth/revoke", data={"token": others, "client_id": "nobody"})
    assert unknown_client.status_code == 400 and unknown_client.json()["error"] == "invalid_client"
    assert (await _refresh(http, other, others)).status_code == 200


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
        db.add(OAuthClientApp(client_id=DOCUMENT_URL, name="Fetched once", redirect_uris=[CALLBACK],
                              allowed_scopes=[], registration_source=svc.REGISTRATION_SOURCE_METADATA_DOCUMENT))
        await db.flush()
        await db.execute(update(OAuthClientApp).values(created_at=now - timedelta(days=3)))
        await db.execute(update(OAuthClientApp).where(OAuthClientApp.client_id == ids["recent"])
                         .values(created_at=now - timedelta(hours=1)))

    async with sessions.begin() as db:
        assert await svc.cleanup_unused_dynamic_clients(db, now=now) == 3
    async with sessions() as db:
        left = set((await db.scalars(select(OAuthClientApp.client_id))).all())
        assert left == {ids["in_use"], ids["signing_in"], ids["recent"], "seeded-app"}
        orphans = (await db.scalars(select(OAuthRefreshToken.client_id))).all()
        assert ids["revoked"] not in orphans and ids["in_use"] in orphans
    async with sessions.begin() as db:  # idempotent
        assert await svc.cleanup_unused_dynamic_clients(db, now=now) == 0


# ── Client ID Metadata Documents ──

def _document(**overrides):
    return {"client_id": DOCUMENT_URL, "client_name": "Example Assistant",
            "redirect_uris": [HOSTED_CALLBACK], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], **overrides}


@pytest.fixture
def documents(monkeypatch):
    """Serve metadata documents by URL without the network; records each fetch."""
    served, fetched = {}, []

    async def fetch(url, **_):
        fetched.append(url)
        if url not in served:
            raise cimd.MetadataDocumentError("client metadata document returned HTTP 404")
        return cimd.FetchedDocument(*served[url])

    monkeypatch.setattr(cimd, "fetch_metadata_document", fetch)
    return served, fetched


def _client_path(client_id):
    return f"/api/v1/oauth/clients/{quote(client_id, safe='')}"


async def test_metadata_document_client_signs_in_and_refetches_only_when_stale(public_oauth, documents):
    http, sessions = public_oauth
    served, fetched = documents
    served[DOCUMENT_URL] = (_document(), 600)

    info = await http.get(_client_path(DOCUMENT_URL), params={"redirect_uri": HOSTED_CALLBACK})
    assert info.status_code == 200, info.text
    body = info.json()
    assert (body["name"], body["client_host"], body["verified"]) == ("Example Assistant", "client.example.test", True)
    assert body["redirect_uri_allowed"] is True and body["read_only_available"] is True

    # Consent and redemption use the fresh cached copy; the token endpoint never fetches.
    token = await _sign_in(http, DOCUMENT_URL, HOSTED_CALLBACK)
    assert token.status_code == 200, token.text
    claims = auth_service.decode_token(token.json()["access_token"], audience=svc.MCP_TOKEN_AUDIENCE)
    assert claims["azp"] == DOCUMENT_URL
    assert auth_service.decode_token(token.json()["access_token"]) is None
    assert fetched == [DOCUMENT_URL]

    # Once stale, the next consent refetches; a refresh still answers from the cache.
    served[DOCUMENT_URL] = (_document(client_name="Renamed Assistant"), 600)
    async with sessions.begin() as db:
        await db.execute(update(OAuthClientApp).where(OAuthClientApp.client_id == DOCUMENT_URL)
                         .values(metadata_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert (await _refresh(http, DOCUMENT_URL, token.json()["refresh_token"])).status_code == 200
    assert fetched == [DOCUMENT_URL]
    assert (await http.get(_client_path(DOCUMENT_URL))).json()["name"] == "Renamed Assistant"
    assert fetched == [DOCUMENT_URL, DOCUMENT_URL]
    async with sessions() as db:
        rows = (await db.scalars(select(OAuthClientApp))).all()
        assert len(rows) == 1 and rows[0].registration_source == svc.REGISTRATION_SOURCE_METADATA_DOCUMENT
        assert rows[0].token_audience == svc.MCP_TOKEN_AUDIENCE and rows[0].client_secret_hash is None


async def test_metadata_document_that_stops_validating_is_not_served_from_cache(public_oauth, documents):
    http, sessions = public_oauth
    served, _ = documents
    served[DOCUMENT_URL] = (_document(), 600)
    assert (await http.get(_client_path(DOCUMENT_URL))).status_code == 200
    # The host now sends codes elsewhere; the stale copy must not keep the old callbacks alive.
    served[DOCUMENT_URL] = (_document(redirect_uris=["https://elsewhere.example/cb"]), 600)
    async with sessions.begin() as db:
        await db.execute(update(OAuthClientApp).values(metadata_expires_at=datetime.now(timezone.utc)))
    refused = await http.post("/api/v1/oauth/authorize", json={
        "client_id": DOCUMENT_URL, "redirect_uri": HOSTED_CALLBACK,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
    assert refused.status_code == 400 and refused.json()["detail"].startswith("invalid_client")
    async with sessions() as db:
        assert (await db.scalars(select(OAuthAuthorizationCode))).all() == []
        assert (await db.scalar(select(OAuthClientApp.redirect_uris))) == [HOSTED_CALLBACK]


async def test_unservable_metadata_document_registers_nothing(public_oauth, documents, monkeypatch):
    http, sessions = public_oauth
    served, fetched = documents
    served[DOCUMENT_URL] = (_document(client_id="https://client.example.test/other.json"), 600)
    mismatch = await http.get(_client_path(DOCUMENT_URL))
    assert mismatch.status_code == 400 and "does not match" in mismatch.json()["detail"]
    async with sessions() as db:
        assert (await db.scalars(select(OAuthClientApp))).all() == []

    # Every fetch counts against the caller's address.
    monkeypatch.setattr(routes, "CLIENT_METADATA_RATE_LIMIT", len(fetched) + 1)
    assert (await http.get(_client_path("https://client.example.test/missing.json"))).status_code == 400
    limited = await http.get(_client_path("https://client.example.test/another.json"))
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    assert len(fetched) == 2


@pytest.mark.parametrize("overrides,error", [
    ({"client_id": DOCUMENT_URL + "?v=2"}, "does not match"),
    ({"redirect_uris": ["https://evil.example/oauth/callback"]}, "redirect URIs"),
    ({"redirect_uris": ["https://client.example.test:8443/cb"]}, "redirect URIs"),
    ({"redirect_uris": ["http://client.example.test/cb"]}, "redirect URIs"),
    ({"redirect_uris": []}, "1-10"),
    ({"redirect_uris": [f"http://localhost/{n}" for n in range(11)]}, "1-10"),
    ({"token_endpoint_auth_method": "private_key_jwt"}, "public clients"),
    ({"grant_types": ["client_credentials"]}, "grant_types"),
])
def test_metadata_document_validation_refuses(overrides, error):
    with pytest.raises(cimd.MetadataDocumentError, match=error) as refused:
        cimd.validate_metadata_document(DOCUMENT_URL, _document(**overrides))
    assert refused.value.code == "invalid_client"


def test_metadata_document_callbacks_on_its_host_loopback_or_trusted_rules_are_accepted():
    accepted = cimd.validate_metadata_document(DOCUMENT_URL, _document(
        client_name="  " + "x" * 300, redirect_uris=[
            HOSTED_CALLBACK, "https://CLIENT.example.test/other", "http://127.0.0.1/cb",
            "https://chatgpt.com/connector_platform_oauth_redirect",
        ]))
    assert len(accepted.name) == 100 and len(accepted.redirect_uris) == 4
    for name in (None, "", "   ", 42):
        with pytest.raises(cimd.MetadataDocumentError, match="client_name"):
            cimd.validate_metadata_document(DOCUMENT_URL, _document(client_name=name))


@pytest.mark.parametrize("client_id,is_document", [
    (DOCUMENT_URL, True),
    ("https://client.example.test/", False),
    ("https://client.example.test", False),
    ("http://client.example.test/client.json", False),
    ("https://client.example.test:8443/client.json", False),
    ("https://user@client.example.test/client.json", False),
    ("https://client.example.test/client.json#x", False),
    ("https://client.example.test/a/../client.json", False),
    ("mcp-abc123", False),
])
def test_only_https_urls_with_a_path_are_metadata_document_client_ids(client_id, is_document):
    assert cimd.is_metadata_document_client_id(client_id) is is_document


@pytest.mark.parametrize("cache_control,seconds", [
    (None, 300), ("no-cache", 300), ("max-age=60", 300), ("public, max-age=3600", 3600),
    ("max-age=999999", 86400),
])
def test_metadata_document_cache_lifetime_is_clamped(cache_control, seconds):
    assert cimd.cache_seconds(cache_control) == seconds


def _resolve_to(monkeypatch, address):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "192.168.0.10", "169.254.169.254", "100.64.0.1", "239.1.1.1", "::1", "fd00:ec2::254",
])
async def test_metadata_document_fetch_refuses_non_public_addresses(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    connected = []
    with pytest.raises(cimd.MetadataDocumentError, match="public address"):
        await cimd.fetch_metadata_document(DOCUMENT_URL, transport_factory=connected.append)
    assert connected == []


async def test_metadata_document_fetch_connects_to_the_vetted_address(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    targets = []

    def transport(target):
        targets.append(target)
        def reply(request):
            assert request.headers["accept-encoding"] == "identity"
            return httpx.Response(200, json=_document(), headers={"Cache-Control": "max-age=7200"})
        return httpx.MockTransport(reply)

    fetched = await cimd.fetch_metadata_document(DOCUMENT_URL, transport_factory=transport)
    assert fetched.document == _document() and fetched.max_age_seconds == 7200
    assert [t.addresses for t in targets] == [("93.184.216.34",)]


@pytest.mark.parametrize("response,error", [
    (httpx.Response(200, content=gzip.compress(b"{}"),
                    headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}), "compressed"),
    (httpx.Response(302, headers={"Location": "http://169.254.169.254/"}), "redirects"),
    (httpx.Response(200, content=b'{"a": "' + b"x" * 17000 + b'"}',
                    headers={"Content-Type": "application/json"}), "16 KB"),
    (httpx.Response(200, text="<html></html>", headers={"Content-Type": "text/html"}), "not JSON"),
    (httpx.Response(200, content=b"[1, 2]", headers={"Content-Type": "application/json"}), "JSON object"),
    (httpx.Response(404, json={}), "HTTP 404"),
])
async def test_metadata_document_fetch_refuses_unusable_responses(monkeypatch, response, error):
    _resolve_to(monkeypatch, "93.184.216.34")
    with pytest.raises(cimd.MetadataDocumentError, match=error):
        await cimd.fetch_metadata_document(
            DOCUMENT_URL, transport_factory=lambda _: httpx.MockTransport(lambda request: response))
