"""Lark's admin client, native user OAuth, and rotating encrypted token contract."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import JSON, MetaData, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.credentials.dev_provider import DevKeyProvider
from packages.core.credentials.service import CredentialService
from packages.core.models.document import Integration
from packages.core.models.mcp import MCPServer
from packages.core.models.user import Entity, OAuthAccount, User, UserMembership
from packages.core.services.lark_oauth import (
    LARK_OAUTH_SCOPES, LARK_USER_INFO_URL, lark_account_matches_config,
    lark_oauth_account_matches, lark_token_response_is_valid,
)
from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens
from packages.core.services.oauth_flow import (
    OAuthFlowError, TokenSet, begin_authorization, complete_authorization,
    resolve_oauth_identity,
)
from packages.core.services.oauth_provider_config import (
    OAuthProviderConfig, _PROVIDER_OAUTH_META, resolve_oauth_config, save_oauth_config,
)
from packages.core.tasks.oauth_refresh import refresh_token_via_provider


CONFIG = OAuthProviderConfig(
    server_key="lark", client_id="cli_fixture", client_secret="fixture-app-secret",
    authorize_url=_PROVIDER_OAUTH_META["lark"]["authorize_url"],
    token_url=_PROVIDER_OAUTH_META["lark"]["token_url"], scopes=LARK_OAUTH_SCOPES,
    redirect_path="/api/v1/integrations/oauth/lark/callback", source="ui",
)
REDIRECT = "https://app.example.test" + CONFIG.redirect_path


def grant(**overrides):
    return {
        "code": "0", "access_token": "user-access", "refresh_token": "user-refresh",
        "expires_in": 3600, "token_type": "Bearer", "scope": LARK_OAUTH_SCOPES,
        **overrides,
    }


@pytest.fixture
def local_state(monkeypatch):
    import packages.core.services.oauth_flow as flow

    monkeypatch.setattr(flow, "_redis_client", AsyncMock(return_value=None))
    monkeypatch.setattr(flow, "_shared_state_store_required", lambda: False)
    monkeypatch.setattr(flow, "_pending_oauth_states", {})


def install_vendor(monkeypatch, responder):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(responder), **kwargs,
    ))


@pytest.mark.asyncio
async def test_lark_authorization_uses_native_json_pkce_one_time_state(local_state, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=grant())

    install_vendor(monkeypatch, respond)
    started = await begin_authorization(
        config=CONFIG, user_id="owner", redirect_uri=REDIRECT,
        entity_id="entity", connection_id="selected-account",
    )
    query = parse_qs(urlsplit(started.authorize_url).query)
    assert started.authorize_url.startswith("https://accounts.larksuite.com/")
    assert query["client_id"] == ["cli_fixture"]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["code_challenge_method"] == ["S256"]
    assert "offline_access" in query["scope"][0].split()
    assert "access_type" not in query and "prompt" not in query
    assert CONFIG.client_secret not in started.authorize_url
    owner, tokens = await complete_authorization(
        server_key="lark", code="one-time-code", state=started.state,
        redirect_uri=REDIRECT, config=CONFIG,
    )
    assert owner == "owner" and tokens.access_token == "user-access"
    import json

    body = json.loads(requests[0].content)
    assert requests[0].headers["content-type"].startswith("application/json")
    assert "authorization" not in requests[0].headers
    assert body["client_id"] == "cli_fixture" and body["client_secret"] == CONFIG.client_secret
    assert body["redirect_uri"] == REDIRECT and len(body["code_verifier"]) >= 43
    with pytest.raises(OAuthFlowError, match="state"):
        await complete_authorization(
            server_key="lark", code="one-time-code", state=started.state,
            redirect_uri=REDIRECT, config=CONFIG,
        )
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_lark_admin_app_change_consumes_state_without_exchange(local_state, monkeypatch):
    started = await begin_authorization(config=CONFIG, user_id="owner", redirect_uri=REDIRECT)
    install_vendor(monkeypatch, lambda _request: pytest.fail("A changed app must not receive this code"))
    with pytest.raises(OAuthFlowError, match="configuration changed"):
        await complete_authorization(
            server_key="lark", code="code", state=started.state, redirect_uri=REDIRECT,
            config=replace(CONFIG, client_id="different-app"),
        )


@pytest.mark.parametrize("changes", [
    {"code": "20050"}, {"access_token": ""}, {"access_token": "bad token"},
    {"refresh_token": None}, {"expires_in": True}, {"expires_in": -1},
    {"token_type": "DPoP"}, {"scope": "contact:user.base:readonly"},
])
def test_lark_incomplete_or_rejected_token_grants_are_unusable(changes):
    assert not lark_token_response_is_valid(grant(**changes), scopes=CONFIG.scopes)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 302, 400])
async def test_lark_admin_health_only_reports_entry_reachability(monkeypatch, status):
    from apps.api.routers import admin_oauth

    monkeypatch.setattr(admin_oauth, "_require_admin", AsyncMock())
    monkeypatch.setattr(admin_oauth, "resolve_oauth_config", AsyncMock(return_value=CONFIG))
    monkeypatch.setenv("APP_URL", "https://app.example.test")

    def respond(request):
        assert str(request.url).startswith(CONFIG.authorize_url)
        params = dict(request.url.params)
        assert params["redirect_uri"] == REDIRECT
        assert params["scope"] == CONFIG.scopes
        assert params["code_challenge_method"] == "S256"
        assert CONFIG.client_secret not in str(request.url)
        return httpx.Response(status)

    install_vendor(monkeypatch, respond)
    health = await admin_oauth.check_oauth_client_health("lark", db=object(), user=object())
    assert health.ok is (status != 400)
    assert "not verified" in health.detail if health.ok else "400" in health.detail


@pytest.mark.asyncio
async def test_lark_identity_is_verified_not_inferred_from_token_response(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {
            "open_id": "ou_verified", "name": "Lark User", "tenant_key": "tenant-key",
            "access_token": "never-copy-this", "app_secret": "never-copy-secret",
        }})

    install_vendor(monkeypatch, respond)
    identity, profile = await resolve_oauth_identity(
        "lark", TokenSet("user-access", "user-refresh", None, "spoofed-id", grant(open_id="spoofed-id")),
        application_id=CONFIG.client_id,
    )
    assert identity == "lark:cli_fixture:user:ou_verified"
    assert profile["platform"] == "lark" and profile["app_id"] == CONFIG.client_id
    assert profile["open_id"] == "ou_verified" and profile["display_name"] == "Lark User"
    assert "access_token" not in profile and "app_secret" not in profile
    assert str(requests[0].url) == LARK_USER_INFO_URL


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"code": 0, "data": {}}, {"code": 99991679}, [], {"code": 0, "data": {"open_id": 3}}])
async def test_lark_identity_failure_never_falls_back_to_token_fingerprint(monkeypatch, payload):
    install_vendor(monkeypatch, lambda _request: httpx.Response(200, json=payload))
    with pytest.raises(OAuthFlowError, match="verify the Lark user identity"):
        await resolve_oauth_identity("lark", TokenSet("access", "refresh", None, "raw", grant()), application_id=CONFIG.client_id)


@pytest.mark.parametrize("changes", [
    {"platform": "feishu"}, {"platform": ""}, {"app_id": "other-app"}, {"open_id": ""},
])
def test_lark_account_binding_rejects_other_platform_or_app(changes):
    row = SimpleNamespace(provider="lark", provider_user_id="lark:cli_fixture:user:ou_verified", profile={
        "platform": "lark", "app_id": CONFIG.client_id, "open_id": "ou_verified", **changes,
    })
    assert not lark_account_matches_config(row, CONFIG)


@pytest.mark.asyncio
async def test_lark_refresh_uses_admin_credentials_and_json(monkeypatch):
    from packages.core.services import oauth_provider_config as providers
    import json

    monkeypatch.setattr(providers, "resolve_oauth_config", AsyncMock(return_value=CONFIG))
    requests = []
    install_vendor(monkeypatch, lambda request: requests.append(request) or httpx.Response(200, json=grant(
        access_token="rotated-access", refresh_token="rotated-refresh",
    )))
    tokens = await refresh_token_via_provider("lark", "single-use-refresh", db=object())
    assert tokens["refresh_token"] == "rotated-refresh"
    assert json.loads(requests[0].content) == {
        "grant_type": "refresh_token", "refresh_token": "single-use-refresh",
        "client_id": CONFIG.client_id, "client_secret": CONFIG.client_secret,
    }
    assert requests[0].headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_lark_refresh_rejection_does_not_echo_secret_provider_text(monkeypatch):
    from packages.core.services import oauth_provider_config as providers

    monkeypatch.setattr(providers, "resolve_oauth_config", AsyncMock(return_value=CONFIG))
    install_vendor(monkeypatch, lambda _request: httpx.Response(400, json={
        "code": "20064", "error": "invalid_grant", "error_description": "leaked fixture-app-secret user-refresh",
    }))
    failed = await refresh_token_via_provider("lark", "user-refresh", db=object())
    assert "_permanent_refresh_error" in failed
    assert "fixture-app-secret" not in str(failed) and "user-refresh" not in str(failed)


@pytest.fixture
async def lark_db(tmp_path, monkeypatch):
    import packages.core.credentials as credentials
    import packages.core.services.oauth_account_credentials as account_credentials

    service = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode()))
    monkeypatch.setattr(credentials, "get_credential_service", lambda: service)
    monkeypatch.setattr(account_credentials, "get_credential_service", lambda: service)
    monkeypatch.delenv("LARK_APP_ID", raising=False)
    monkeypatch.delenv("LARK_APP_SECRET", raising=False)
    monkeypatch.setenv("APP_URL", "https://app.example.test")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lark.sqlite'}")
    metadata = MetaData()
    for model in (Entity, User, UserMembership, OAuthAccount, MCPServer, Integration):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with sessions() as db:
            db.add_all([
                Entity(id="lark-entity", name="Lark test"),
                User(id="owner", entity_id="lark-entity", email="lark@example.test", password_hash="unused", role="owner"),
                UserMembership(user_id="owner", entity_id="lark-entity", status="active"),
                MCPServer(id="lark-server", server_key="lark", name="Lark", transport="builtin", auth_type="oauth2"),
            ])
            await db.commit()
        yield sessions, service
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lark_admin_secret_and_user_tokens_remain_encrypted(lark_db, local_state, monkeypatch):
    from apps.api.routers import integrations as router
    from packages.core.tasks.channel_tasks import health_check_task

    sessions, service = lark_db
    monkeypatch.setattr(router, "lock_runtime_integration_account_scope", AsyncMock())
    monkeypatch.setattr(router, "normalize_runtime_integration_account_defaults", AsyncMock())
    monkeypatch.setattr(router, "route_declared_channels_after_connect", AsyncMock())
    monkeypatch.setattr(health_check_task, "delay", lambda **_kwargs: None)
    async with sessions() as db:
        assert await save_oauth_config(db, "lark", client_id=CONFIG.client_id, client_secret=CONFIG.client_secret)
        await db.commit()
        server = (await db.execute(select(MCPServer))).scalar_one()
        assert "oauth_client_secret" not in server.default_config
        assert CONFIG.client_secret not in server.credential_ref
        assert (await resolve_oauth_config(db, "lark")).client_secret == CONFIG.client_secret
        legacy = Integration(id="legacy", entity_id="lark-entity", owner_user_id="owner", provider="lark", config={"name": "Feishu app"})
        service.store_integration(legacy, {"app_id": "cli_feishu", "app_secret": "legacy-secret", "platform": "feishu"})
        db.add(legacy)
        await db.commit()
        legacy_ref = legacy.credential_ref
        owner = await db.get(User, "owner")
        started = await router.oauth_start("lark", return_to=None, connection_id=None, user=owner, db=db)
        install_vendor(monkeypatch, lambda request: httpx.Response(200, json=(
            grant() if request.method == "POST" else {"code": 0, "data": {"open_id": "ou_verified", "name": "Lark User"}}
        )))
        response = await router.oauth_callback(
            "lark", code="code", state=started.state, error=None, error_description=None, realm_id=None, db=db,
        )
        assert response.status_code == 302
        account = (await db.execute(select(OAuthAccount))).scalar_one()
        assert account.user_id == "owner" and account.provider_user_id == "lark:cli_fixture:user:ou_verified"
        assert account.access_token is None and account.refresh_token is None
        assert "user-access" not in account.credential_ref and "user-refresh" not in account.credential_ref
        assert lease_oauth_account_tokens(account, requester_id="test", requester_kind="test", reason="verify encrypted Lark callback") == {
            "access_token": "user-access", "refresh_token": "user-refresh",
        }
        assert await lark_oauth_account_matches(db, account)
        await db.refresh(legacy)
        assert legacy.credential_ref == legacy_ref and legacy.credentials == {}


@pytest.mark.asyncio
async def test_lark_cancel_consumes_state_without_tokens(lark_db, local_state):
    from apps.api.routers import integrations as router
    from fastapi import HTTPException

    sessions, _ = lark_db
    async with sessions() as db:
        started = await begin_authorization(config=CONFIG, user_id="owner", entity_id="lark-entity", redirect_uri=REDIRECT)
        response = await router.oauth_callback("lark", code=None, state=started.state, error="access_denied", error_description=None, realm_id=None, db=db)
        assert response.status_code == 302 and "oauth_cancelled=lark" in response.headers["location"]
        assert not (await db.execute(select(OAuthAccount))).scalars().all()
        with pytest.raises(HTTPException) as failed:
            await router.oauth_callback("lark", code=None, state=started.state, error="access_denied", error_description=None, realm_id=None, db=db)
        assert failed.value.status_code == 400


def test_lark_cloud_shared_client_cannot_be_changed_by_entity_admin(monkeypatch):
    from apps.api.routers.admin_oauth import _require_local_lark_config_write
    from fastapi import HTTPException

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    with pytest.raises(HTTPException) as failed:
        _require_local_lark_config_write("lark")
    assert failed.value.status_code == 403
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    _require_local_lark_config_write("lark")


@pytest.mark.asyncio
async def test_lark_refresh_serializes_and_encrypts_rotating_grant(lark_db, monkeypatch):
    from packages.core.services import oauth_provider_config as providers
    from packages.core.services.oauth_account_credentials import store_oauth_account_tokens
    from packages.core.tasks.oauth_refresh import _refresh_oauth_accounts

    _, _service = lark_db
    row = OAuthAccount(
        id="refresh-row", user_id="owner", provider="lark", provider_user_id="lark:cli_fixture:user:ou_verified",
        token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        profile={"platform": "lark", "app_id": CONFIG.client_id, "open_id": "ou_verified"},
    )
    store_oauth_account_tokens(row, access_token="old-access", refresh_token="single-use-refresh")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))),
        refresh=AsyncMock(), flush=AsyncMock(),
    )
    monkeypatch.setattr(providers, "resolve_oauth_config", AsyncMock(return_value=CONFIG))
    install_vendor(monkeypatch, lambda request: httpx.Response(200, json=(
        grant(access_token="rotated-access", refresh_token="rotated-refresh")
        if request.method == "POST" else {"code": 0, "data": {"open_id": "ou_verified"}}
    )))
    assert await _refresh_oauth_accounts(db) == 1
    db.refresh.assert_awaited_once_with(row, with_for_update=True)
    assert lease_oauth_account_tokens(row, requester_id="test", requester_kind="test", reason="verify rotated Lark ciphertext") == {
        "access_token": "rotated-access", "refresh_token": "rotated-refresh",
    }
    assert row.access_token is None and row.refresh_token is None
    assert row.token_expires_at > datetime.now(timezone.utc) + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_lark_refresh_rereads_after_lock_before_reusing_refresh_token(monkeypatch):
    from packages.core.tasks.oauth_refresh import _refresh_oauth_accounts

    row = SimpleNamespace(provider="lark", token_expires_at=datetime.now(timezone.utc), profile={})

    async def concurrent_refresh(_row, **kwargs):
        assert kwargs == {"with_for_update": True}
        row.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))),
        refresh=AsyncMock(side_effect=concurrent_refresh), flush=AsyncMock(),
    )
    install_vendor(monkeypatch, lambda _request: pytest.fail("Another worker already refreshed the single-use token"))
    assert await _refresh_oauth_accounts(db) == 0


@pytest.mark.asyncio
async def test_lark_refresh_changed_app_is_reconnect_required_before_user_token_lease(lark_db, monkeypatch):
    from packages.core.services import oauth_provider_config as providers
    from packages.core.tasks.oauth_refresh import _refresh_oauth_accounts

    row = OAuthAccount(
        id="changed-app", user_id="owner", provider="lark", provider_user_id="lark:old-app:user:ou_verified",
        token_expires_at=datetime.now(timezone.utc), credential_ref="vault:v1:must-not-lease",
        profile={"platform": "lark", "app_id": "old-app", "open_id": "ou_verified"},
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))),
        refresh=AsyncMock(), flush=AsyncMock(),
    )
    monkeypatch.setattr(providers, "resolve_oauth_config", AsyncMock(return_value=CONFIG))
    install_vendor(monkeypatch, lambda _request: pytest.fail("A saved token must not reach a changed app"))
    assert await _refresh_oauth_accounts(db) == 0
    assert row.profile["oauth_refresh"]["reauth_required"]
    assert row.profile["oauth_refresh"]["error"] == "oauth_app_changed"
    assert row.token_expires_at is None


@pytest.mark.asyncio
async def test_lark_refresh_rechecks_app_after_binding_snapshot(monkeypatch):
    from packages.core.services import oauth_provider_config as providers

    monkeypatch.setattr(providers, "resolve_oauth_config", AsyncMock(return_value=replace(CONFIG, client_id="replacement-app")))
    install_vendor(monkeypatch, lambda _request: pytest.fail("An old rotating grant cannot go to a replacement app"))
    result = await refresh_token_via_provider("lark", "old-app-refresh", db=object(), expected_client_id=CONFIG.client_id)
    assert result["_permanent_refresh_error"]["error"] == "oauth_app_changed"
