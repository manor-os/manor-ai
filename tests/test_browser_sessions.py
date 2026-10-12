"""30-day persistence and 24-hour access retain live authority gates."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from packages.core.config import get_settings
from sqlalchemy import select
from packages.core.database import async_session
from packages.core.models.browser_session import BrowserSession
from packages.core.models.user import User
from packages.core.services import browser_session as sessions
from packages.core.services.auth_service import create_access_token, decode_token

HEADERS = {"Origin": "http://test", "X-Manor-Session": "1"}
PASSWORD = "session-test-password-123"

pytestmark = pytest.mark.oss_regression

def expired_token(claims):
    claims = dict(claims, exp=datetime.now(timezone.utc) - timedelta(seconds=1))
    settings = get_settings()
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

async def sign_in(client, remember=True, headers=HEADERS):
    registered = await client.post("/api/v1/auth/register", json={"email": "session@example.com", "password": PASSWORD, "entity_name": "Session Test"})
    assert registered.status_code == 200, registered.text
    signed = await client.post("/api/v1/auth/login", headers=headers, json={"email": "session@example.com", "password": PASSWORD, "remember_me": remember})
    assert signed.status_code == 200, signed.text
    return signed

async def refresh(client, cookie=None, headers=HEADERS, entity_id=None):
    if cookie is not None:
        client.cookies.clear()
        client.cookies.set(sessions.COOKIE, cookie, domain="test.local", path=sessions.COOKIE_PATH)
    return await client.post("/api/v1/auth/session/refresh", headers=headers, json={"entity_id": entity_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("remember", [False, True])
@pytest.mark.parametrize("client", [{"CORS_ALLOWED_ORIGINS": "https://staging-dev.manorai.xyz"}], indirect=True)
async def test_same_origin_proxy_login_does_not_require_cors_allowlist(client, remember):
    # Vite keeps the browser Origin but rewrites Host to the internal API host.
    headers = {"Origin": "http://localhost:3010", "X-Manor-Session": "1",
               "Sec-Fetch-Site": "same-origin", "X-Forwarded-Host": "localhost:3010"}
    signed = await sign_in(client, remember=remember, headers=headers)
    assert (sessions.COOKIE in signed.cookies) is remember
    token = signed.json()["access_token"]
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})).status_code == 200
    if remember:
        renewed = await refresh(client, headers=headers)
        assert renewed.status_code == 200, renewed.text
        # Exercise cookie-only logout, which uses the same browser request gate.
        assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 204
        assert (await refresh(client, headers=headers)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [{"CORS_ALLOWED_ORIGINS": "https://app.example"}], indirect=True)
@pytest.mark.parametrize("origin", ["https://attacker.example", "http://test:9999"])
async def test_cross_origin_browser_session_requests_fail_cors_preflight(client, origin):
    for path in ("/api/v1/auth/login", "/api/v1/auth/session/refresh", "/api/v1/auth/logout"):
        result = await client.options(path, headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-manor-session"})
        assert result.status_code == 400
        assert "access-control-allow-origin" not in result.headers
    allowed = await client.options("/api/v1/auth/session/refresh", headers={
        "Origin": "https://app.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-manor-session"})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://app.example"


@pytest.mark.asyncio
async def test_persistent_cookie_rotation_and_expired_access(client):
    signed = await sign_in(client)
    cookie = signed.cookies[sessions.COOKIE]
    claims = decode_token(signed.json()["access_token"])
    assert claims["exp"] - claims["iat"] == 24 * 60 * 60
    header = signed.headers["set-cookie"]
    assert "HttpOnly" in header and "SameSite=strict" in header
    assert "Max-Age=25919" in header and "Path=/api/v1/auth" in header
    sid, secret = cookie.split(".")
    async with async_session() as db:
        row = await db.get(BrowserSession, sid)
        expiry = row.expires_at
        assert row.token_hash == sessions.digest(secret) and row.token_hash != secret
        assert row.previous_hash is None
    renewed = await refresh(client)
    assert renewed.status_code == 200, renewed.text
    rotated = renewed.cookies[sessions.COOKIE]
    assert rotated != cookie
    expired = expired_token(claims)
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})).status_code == 401
    recovered = await refresh(client, cookie)
    assert recovered.status_code == 200, recovered.text
    assert recovered.cookies[sessions.COOKIE] == rotated  # multi-tab duplicate
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + recovered.json()["access_token"]})).status_code == 200
    async with async_session() as db:
        row = await db.get(BrowserSession, sid)
        assert row.rotation == 1 and row.expires_at == expiry

@pytest.mark.asyncio
async def test_used_refresh_secret_replay_revokes_authority(client):
    signed = await sign_in(client); old = signed.cookies[sessions.COOKIE]
    current = await refresh(client)
    async with async_session() as db:
        row = await db.get(BrowserSession, old.split(".")[0]); row.rotated_at = datetime.now(timezone.utc) - timedelta(seconds=11)
        await db.commit()
    assert (await refresh(client, old)).status_code == 401
    assert (await refresh(client, current.cookies[sessions.COOKIE])).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + current.json()["access_token"]})).status_code == 401

@pytest.mark.asyncio
async def test_invalid_secret_does_not_revoke_valid_family(client):
    signed = await sign_in(client); old = signed.cookies[sessions.COOKIE]
    assert (await refresh(client, old.split(".")[0] + "." + "x" * 40)).status_code == 401
    assert (await refresh(client, old)).status_code == 200

@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["epoch", "disabled", "deleted", "expired", "mfa"])
async def test_live_account_revocation(client, revocation):
    signed = await sign_in(client); cookie = signed.cookies[sessions.COOKIE]
    async with async_session() as db:
        user = await db.get(User, signed.json()["user_id"])
        row = await db.get(BrowserSession, cookie.split(".")[0])
        if revocation == "epoch": user.token_version += 1
        if revocation == "disabled": user.status = "disabled"
        if revocation == "deleted": user.deleted_at = datetime.now(timezone.utc)
        if revocation == "expired": row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        if revocation == "mfa": user.totp_enabled = True
        await db.commit()
    result = await refresh(client)
    assert result.status_code == 401 and "Max-Age=0" in result.headers["set-cookie"]

@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Origin": "null", "X-Manor-Session": "1"}, {"Origin": "https://attacker.example"}, {"Origin": "http://test"}, {"Origin": "http://test:9999", "Sec-Fetch-Site": "same-site"}])
async def test_cookie_recovery_rejects_csrf(client, headers):
    signed = await sign_in(client); cookie = signed.cookies[sessions.COOKIE]
    assert (await refresh(client, cookie, headers)).status_code == 403
    assert (await refresh(client, cookie)).status_code == 200

@pytest.mark.asyncio
@pytest.mark.parametrize("with_expired_access", [False, True])
async def test_logout_revokes_cookie_and_access_bearer(client, with_expired_access):
    signed = await sign_in(client); token = signed.json()["access_token"]; cookie = signed.cookies[sessions.COOKIE]
    headers = dict(HEADERS)
    if with_expired_access:
        claims = decode_token(token)
        expired = expired_token(claims)
        headers["Authorization"] = "Bearer " + expired
    logged_out = await client.post("/api/v1/auth/logout", headers=headers)
    assert logged_out.status_code == 204
    assert "Max-Age=0" in logged_out.headers["set-cookie"]
    assert (await refresh(client, cookie)).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})).status_code == 401

@pytest.mark.asyncio
async def test_refresh_cannot_invent_an_entity_membership(client):
    signed = await sign_in(client); cookie = signed.cookies[sessions.COOKIE]
    assert (await refresh(client, entity_id="unrelated-company")).status_code == 403
    assert (await refresh(client, cookie)).status_code == 200
    async with async_session() as db:
        assert (await db.get(User, signed.json()["user_id"])).entity_id == signed.json()["entity_id"]

@pytest.mark.asyncio
@pytest.mark.parametrize("remember,headers", [(False, HEADERS), (True, {})])
async def test_nonpersistent_and_native_login_do_not_create_refresh_sessions(client, remember, headers):
    signed = await sign_in(client, remember, headers)
    assert sessions.COOKIE not in signed.cookies
    assert (await refresh(client)).status_code == 401

@pytest.mark.asyncio
async def test_new_login_without_remember_revokes_prior_browser_family(client):
    signed = await sign_in(client); cookie = signed.cookies[sessions.COOKIE]
    signed = await client.post("/api/v1/auth/login", headers=HEADERS, json={"email": "session@example.com", "password": PASSWORD, "remember_me": False})
    assert signed.status_code == 200
    assert (await refresh(client, cookie)).status_code == 401


def test_https_cookie_and_migration_model_match(monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy.dialects.postgresql import dialect
    request = Request({"type": "http", "headers": [(b"origin", b"https://manor.example")], "scheme": "https", "server": ("manor.example", 443), "path": "/"})
    response = Response()
    sessions.set_cookie(request, response, SimpleNamespace(id="test", expires_at=datetime.now(timezone.utc) + timedelta(days=30)), "secret")
    assert "Secure" in response.headers["set-cookie"] and response.headers["cache-control"] == "no-store"
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20261004_02").module
    tables = {}; indexes = {}
    monkeypatch.setattr(revision.op, "create_table", lambda name, *columns: tables.update({name: columns}))
    monkeypatch.setattr(revision.op, "create_index", lambda name, table, columns: indexes.update({name: columns}))
    revision.upgrade()
    created = {column.name: column for column in tables["browser_sessions"]}
    assert set(created) == set(BrowserSession.__table__.columns.keys())
    for column in BrowserSession.__table__.columns:
        assert created[column.name].nullable == column.nullable
        assert str(created[column.name].type.compile(dialect=dialect())) == str(column.type.compile(dialect=dialect()))
    assert indexes == {index.name: [c.name for c in index.columns] for index in BrowserSession.__table__.indexes}


@pytest.mark.parametrize("remember", [False, True])
@pytest.mark.parametrize("minutes,expected", [(None, 1440), (7 * 24 * 60, 1440), (10, 10)])
def test_access_lifetime_is_24_hours_with_shorter_overrides(remember, minutes, expected):
    token = create_access_token("user", "entity", "owner", remember=remember, expires_minutes=minutes)
    claims = decode_token(token)
    assert claims["exp"] - claims["iat"] == expected * 60
