"""Real OAuth service/ASGI/SQLite PKCE tests; no configured application DB."""
import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value

from apps.api.routers import oauth_provider as routes
from packages.core.models.oauth_provider import OAuthAuthorizationCode, OAuthClientApp
from packages.core.models.user import User, UserMembership
from packages.core.services import oauth_provider_service as svc
from tests.test_oauth_provider_entity_scope import (
    oauth_db, client_secret_hash, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI,
    USER_ID, SELECTED_ENTITY,
)

pytestmark = pytest.mark.integration
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

@pytest.fixture
async def pkce_http(oauth_db):
    sessions, _ = oauth_db
    app = FastAPI()
    app.include_router(routes.router)
    async def database():
        async with sessions() as db:
            yield db
    async def user():
        async with sessions() as db:
            row = await db.get(User, USER_ID)
            set_committed_value(row, "entity_id", SELECTED_ENTITY)
            set_committed_value(row, "role", "member")
            return row
    app.dependency_overrides[routes.get_db] = database
    app.dependency_overrides[routes.get_current_user] = user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, sessions


def authorization(**changes):
    return {"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": "openid profile email", "state": "csrf",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", **changes}


def exchange(code, **changes):
    return {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET, "redirect_uri": REDIRECT_URI, "code_verifier": VERIFIER, **changes}


async def issued(http):
    response = await http.post("/api/v1/oauth/authorize", json=authorization())
    assert response.status_code == 200, response.text
    return response.json()["code"]


@pytest.mark.parametrize("form", [False, True])
async def test_actual_consent_persists_s256_and_requires_verifier_at_real_token_route(pkce_http, form):
    http, sessions = pkce_http
    state = "synthetic-relay:" + "a" * 1024 + "&next=one+two%2F?=end"
    response = await http.post(
        "/api/v1/oauth/authorize", **{"data" if form else "json": authorization(state=state)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    code = body["code"]
    assert body["state"] == state
    assert parse_qs(urlsplit(body["redirect_to"]).query) == {
        "code": [code], "state": [state], "iss": ["http://test"],
    }
    async with sessions() as db:
        row = await db.scalar(select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code))
        assert (row.code_challenge, row.code_challenge_method) == (CHALLENGE, "S256")
        assert row.entity_id == SELECTED_ENTITY and row.user_id == USER_ID
        # SQLite does not enforce VARCHAR(255); require no relay state at rest.
        assert row.state is None
    args = {"data" if form else "json": exchange(code)}
    response = await http.post("/api/v1/oauth/token", **args)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["entity_id"] == SELECTED_ENTITY
    assert response.json()["user"]["role"] == "member"
    assert (await http.post("/api/v1/oauth/token", **args)).status_code == 400


@pytest.mark.parametrize("verifier", [None, "", "wrong" * 10, "a" * 42, "a" * 129, "a" * 42 + "=", "a" * 42 + " ", "é" * 43, 42])
async def test_missing_wrong_or_malformed_verifier_never_mints_or_consumes(pkce_http, verifier, monkeypatch):
    http, sessions = pkce_http
    code = await issued(http)
    minted = []
    monkeypatch.setattr(svc, "create_access_token", lambda *a, **k: minted.append(True))
    response = await http.post("/api/v1/oauth/token", json=exchange(code, code_verifier=verifier))
    assert response.status_code == 400
    assert not minted
    async with sessions() as db:
        row = await db.scalar(select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code))
        assert row.redeemed_at is None


@pytest.mark.parametrize("changes", [
    {"code_challenge_method": "plain"}, {"code_challenge_method": None},
    {"code_challenge": None}, {"code_challenge": ""}, {"code_challenge": "a" * 42},
    {"code_challenge": "a" * 44}, {"code_challenge": "a" * 42 + "="},
    {"code_challenge": "a" * 42 + "."}, {"code_challenge_method": "s256"},
])
async def test_plain_partial_or_malformed_authorization_rejected(pkce_http, changes):
    http, sessions = pkce_http
    async with sessions() as db:
        before = len((await db.scalars(select(OAuthAuthorizationCode))).all())
    response = await http.post("/api/v1/oauth/authorize", json=authorization(**changes))
    assert response.status_code == 400
    async with sessions() as db:
        assert len((await db.scalars(select(OAuthAuthorizationCode))).all()) == before


async def test_required_client_blocks_legacy_code_and_new_downgrade(pkce_http):
    http, sessions = pkce_http
    async with sessions.begin() as db:
        await db.execute(update(OAuthClientApp).values(require_pkce=True))
    response = await http.get(f"/api/v1/oauth/clients/{CLIENT_ID}")
    assert response.json()["require_pkce"] is True
    no_pkce = {k:v for k,v in authorization().items() if not k.startswith("code_challenge")}
    assert (await http.post("/api/v1/oauth/authorize", json=no_pkce)).status_code == 400
    async with sessions() as db:
        legacy = await db.scalar(select(OAuthAuthorizationCode))
    no_verifier = {k:v for k,v in exchange(legacy.code).items() if k != "code_verifier"}
    assert (await http.post("/api/v1/oauth/token", json=no_verifier)).status_code == 400
    code = await issued(http)
    assert (await http.post("/api/v1/oauth/token", json=exchange(code))).status_code == 200


async def test_explicit_legacy_client_still_works_but_unsolicited_verifier_rejected(pkce_http):
    http, sessions = pkce_http
    async with sessions.begin() as db:
        await db.execute(update(OAuthClientApp).values(require_pkce=False))
    no_pkce = {k:v for k,v in authorization().items() if not k.startswith("code_challenge")}
    response = await http.post("/api/v1/oauth/authorize", json=no_pkce)
    code = response.json()["code"]
    assert (await http.post("/api/v1/oauth/token", json=exchange(code))).status_code == 400
    data = {k:v for k,v in exchange(code).items() if k != "code_verifier"}
    assert (await http.post("/api/v1/oauth/token", json=data)).status_code == 200


@pytest.mark.parametrize("endpoint,key", [("authorize", "code_challenge"), ("authorize", "code_challenge_method"), ("authorize", "client_id"), ("token", "code_verifier"), ("token", "client_id")])
@pytest.mark.parametrize("form", [False, True])
async def test_duplicate_protocol_parameters_are_not_last_value_wins(pkce_http, endpoint, key, form):
    from urllib.parse import urlencode
    http, sessions = pkce_http
    code = await issued(http)
    data = authorization() if endpoint == "authorize" else exchange(code)
    if form:
        payload = urlencode(list(data.items()) + [(key, data[key])])
        mime = "application/x-www-form-urlencoded"
    else:
        payload = json.dumps(data)[:-1] + "," + json.dumps(key) + ":" + json.dumps(data[key]) + "}"
        mime = "application/json"
    response = await http.post(f"/api/v1/oauth/{endpoint}", content=payload, headers={"content-type": mime})
    assert response.status_code == 400
    assert "access_token" not in response.text and CLIENT_SECRET not in response.text


async def test_pkce_does_not_replace_current_actor_or_entity_admission(pkce_http):
    http, sessions = pkce_http
    code = await issued(http)
    async with sessions.begin() as db:
        await db.execute(update(UserMembership).where(UserMembership.entity_id == SELECTED_ENTITY).values(status="inactive"))
    assert (await http.post("/api/v1/oauth/token", json=exchange(code))).status_code == 400


def test_seed_config_requires_explicit_valid_boolean_and_keeps_unspecified(monkeypatch):
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_S256_SECRET", CLIENT_SECRET)
    monkeypatch.delenv("MANOR_OAUTH_CLIENT_S256_REQUIRE_PKCE", raising=False)
    assert svc._read_client_config("S256")["require_pkce"] is None
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_S256_REQUIRE_PKCE", "true")
    assert svc._read_client_config("S256")["require_pkce"] is True
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_S256_REQUIRE_PKCE", "false")
    assert svc._read_client_config("S256")["require_pkce"] is False
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_S256_REQUIRE_PKCE", "typo")
    with pytest.raises(ValueError):
        svc._read_client_config("S256")


async def test_two_real_transactions_with_correct_pkce_mint_only_once(pkce_http, monkeypatch):
    import asyncio
    http, sessions = pkce_http
    code = await issued(http)
    barrier = asyncio.Event()
    reads = 0
    minted = []
    original_mint = svc.create_access_token
    def mint(*args, **kwargs):
        minted.append(True)
        return original_mint(*args, **kwargs)
    monkeypatch.setattr(svc, "create_access_token", mint)
    async def redeem():
        async with sessions.begin() as db:
            original_execute = db.execute
            async def execute(statement, *args, **kwargs):
                nonlocal reads
                result = await original_execute(statement, *args, **kwargs)
                if statement.is_select and any(c.get("entity") is OAuthAuthorizationCode for c in statement.column_descriptions):
                    reads += 1
                    if reads == 2:
                        barrier.set()
                    await asyncio.wait_for(barrier.wait(), 5)
                return result
            monkeypatch.setattr(db, "execute", execute)
            return await svc.exchange_code_for_token(db, code=code, client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI, code_verifier=VERIFIER)
    results = await asyncio.wait_for(asyncio.gather(redeem(), redeem(), return_exceptions=True), 10)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ValueError) and str(r) == "invalid_grant" for r in results) == 1
    assert len(minted) == 1


async def test_env_seed_does_not_silently_turn_off_an_existing_pkce_requirement(pkce_http, monkeypatch):
    _, sessions = pkce_http
    monkeypatch.setattr(svc, "_discover_client_slugs", lambda: ["EXISTING"])
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_EXISTING_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_EXISTING_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_EXISTING_REDIRECT_URIS", REDIRECT_URI)
    monkeypatch.delenv("MANOR_OAUTH_CLIENT_EXISTING_REQUIRE_PKCE", raising=False)
    async with sessions.begin() as db:
        await db.execute(update(OAuthClientApp).values(require_pkce=True))
    async with sessions() as db:
        await svc.seed_clients_from_env(db)
    async with sessions() as db:
        assert (await svc.get_client_by_id(db, CLIENT_ID)).require_pkce is True


@pytest.mark.parametrize("extra", ["code_verifier=first&code_verifier=second", "client_id=one&client_id=two"])
async def test_duplicate_query_parameters_rejected_even_with_valid_body(pkce_http, extra):
    http, _ = pkce_http
    code = await issued(http)
    assert (await http.post("/api/v1/oauth/token?" + extra, json=exchange(code))).status_code == 400


def basic(client_id=CLIENT_ID, secret=CLIENT_SECRET):
    return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}


async def test_confidential_client_renews_only_with_its_secret(pkce_http):
    http, sessions = pkce_http
    first = (await http.post("/api/v1/oauth/token", json=exchange(await issued(http)))).json()
    assert first["refresh_token"]  # issued without offline_access, like a public client's

    def renew(token, **changes):
        return http.post("/api/v1/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": token, "client_id": CLIENT_ID, **changes})

    assert (await renew(first["refresh_token"])).json()["error"] == "invalid_client"
    assert (await renew(first["refresh_token"], client_secret="wrong")).json()["error"] == "invalid_client"
    renewed = await renew(first["refresh_token"], client_secret=CLIENT_SECRET)
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["user"]["entity_id"] == SELECTED_ENTITY
    # The client's current team restriction applies to renewals as to redemption.
    async with sessions.begin() as db:
        await db.execute(update(OAuthClientApp).values(restricted_entity_id="another-team"))
    moved = await renew(renewed.json()["refresh_token"], client_secret=CLIENT_SECRET)
    assert moved.status_code == 400 and moved.json()["error"] == "invalid_grant"


async def test_http_basic_client_authentication(pkce_http):
    http, _ = pkce_http
    body = {k: v for k, v in exchange(await issued(http)).items() if k not in ("client_id", "client_secret")}
    wrong = await http.post("/api/v1/oauth/token", data=body, headers=basic(secret="wrong"))
    assert wrong.status_code == 401 and wrong.json()["error"] == "invalid_client"
    assert wrong.headers["www-authenticate"].startswith("Basic")
    # One method per request: Basic plus a body secret, or two different ids, is refused.
    both = await http.post("/api/v1/oauth/token", data={**body, "client_secret": CLIENT_SECRET}, headers=basic())
    assert both.status_code == 400 and both.json()["error"] == "invalid_request"
    mismatch = await http.post("/api/v1/oauth/token", data={**body, "client_id": "other"}, headers=basic())
    assert mismatch.status_code == 400 and mismatch.json()["error"] == "invalid_request"
    malformed = await http.post("/api/v1/oauth/token", data=body, headers={"Authorization": "Basic !!!"})
    assert malformed.status_code == 400
    ok = await http.post("/api/v1/oauth/token", data=body, headers=basic())
    assert ok.status_code == 200, ok.text
    revoked = await http.post("/api/v1/oauth/revoke", data={"token": ok.json()["refresh_token"]}, headers=basic())
    assert revoked.status_code == 200
    renew = await http.post("/api/v1/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": ok.json()["refresh_token"]}, headers=basic())
    assert renew.status_code == 400 and renew.json()["error"] == "invalid_grant"
