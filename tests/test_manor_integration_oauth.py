"""PMS/Minutes OAuth uses real Manor routes, account ACLs and encrypted storage.

Only the remote application transport and background health enqueue are stubbed.
Database tests require the suite's isolated TEST_DATABASE_URL.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from packages.core.credentials import CredentialService
from packages.core.credentials.dev_provider import DevKeyProvider
from packages.core.models.user import OAuthAccount
from packages.core.services import manor_integration_oauth as manor_oauth
from tests.auth_helpers import register_user_and_get_token


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
def test_deployment_urls_only_allow_https_or_explicit_loopback(monkeypatch, provider, prefix):
    monkeypatch.setenv(f"{prefix}_OAUTH_BASE_URL", "http://127.0.0.1:18411")
    assert manor_oauth.manor_oauth_endpoints(provider)["token_url"] == "http://127.0.0.1:18411/api/oauth/token"
    for bad in ("http://169.254.169.254", "http://external.example", "https://u:p@example.com", "https://example.com/path", "https://example.com?target=x"):
        monkeypatch.setenv(f"{prefix}_OAUTH_BASE_URL", bad)
        with pytest.raises(ValueError):
            manor_oauth.manor_oauth_endpoints(provider)


@pytest.mark.parametrize("url", ["http://external.example/api/mcp", "http://10.0.0.8/api/mcp"])
async def test_pms_oauth_rejects_non_loopback_http_before_identity_request(monkeypatch, url):
    from packages.core.ai.mcp.manor_pms import _mcp_url
    from packages.core.services.oauth_flow import OAuthFlowError

    monkeypatch.setenv("PMS_API_URL", url)
    # Existing manually configured integrations retain their deployment contract.
    assert _mcp_url(url) == url
    with pytest.raises(ValueError, match="HTTPS"):
        manor_oauth.manor_mcp_endpoint("manor_pms")
    transport = Mock(side_effect=AssertionError("Invalid configuration must not send a token"))
    monkeypatch.setattr(httpx, "AsyncClient", transport)
    with pytest.raises(OAuthFlowError, match="deployment configuration") as error:
        await manor_oauth.resolve_manor_oauth_identity("manor_pms", "synthetic-access", user_id="user", entity_id="entity")
    assert error.value.status == 503
    transport.assert_not_called()


@pytest.mark.parametrize("url", ["http://127.0.0.1:18411/api/mcp", "http://localhost:18411/api/mcp", "https://pms.example/api/mcp"])
def test_pms_oauth_mcp_allows_https_and_explicit_loopback(monkeypatch, url):
    monkeypatch.setenv("PMS_API_URL", url)
    assert manor_oauth.manor_mcp_endpoint("manor_pms") == url


async def test_pms_oauth_http_configuration_disables_existing_account(client, monkeypatch, oauth_vault):
    import packages.core.database as db_module
    from packages.core.services.agent_permission_service import can_use_integration
    from packages.core.tasks.oauth_refresh import _refresh_oauth_accounts

    headers, user_id, entity_id = await _owner(client, "pms_mcp_tls")
    seen, _ = _provider_transport(monkeypatch, "manor_pms", "PMS", user_id, entity_id)
    monkeypatch.setenv("PMS_API_URL", "https://pms.example/api/mcp")
    state = await _start(client, "manor_pms", headers)
    connected = await client.get("/api/v1/integrations/oauth/manor_pms/callback", params={"code": "synthetic-code", "state": state})
    assert connected.status_code == 302
    async with db_module.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
        row.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        monkeypatch.setenv("PMS_API_URL", "http://pms.example/api/mcp")
        assert not manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)
        request_count = len(seen)
        denied = await can_use_integration(db, user_id=user_id, entity_id=entity_id, provider="manor_pms", integration_account_id=row.id, allow_env_fallback=False)
        assert not denied.allowed
        assert await _refresh_oauth_accounts(db) == 0
        assert len(seen) == request_count
    start = await client.get("/api/v1/integrations/oauth/manor_pms/start", headers=headers)
    assert start.status_code == 503 and "deployment configuration" in start.json()["detail"]


def test_callback_error_page_escapes_untrusted_provider_text():
    from packages.core.services.oauth_flow import render_oauth_error_page

    body = render_oauth_error_page("manor_pms", "<img onerror=alert(1)>", "<script>alert(1)</script>").body.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


@pytest.mark.parametrize("provider,prefix,path", [("manor_pms", "PMS", "/management"), ("manor_mcp_minutes", "MINUTES", "/")])
def test_application_url_uses_trusted_frontend_origin_without_grant_data(monkeypatch, provider, prefix, path):
    monkeypatch.setenv(f"{prefix}_OAUTH_BASE_URL", "https://private-api.example")
    monkeypatch.delenv(f"{prefix}_OAUTH_AUTHORIZE_URL", raising=False)
    assert manor_oauth.manor_application_url(provider) is None
    monkeypatch.setenv(f"{prefix}_OAUTH_AUTHORIZE_URL", "https://app.example:8443/oauth/authorize")
    assert manor_oauth.manor_application_url(provider) == f"https://app.example:8443{path}"
    monkeypatch.setenv(f"{prefix}_OAUTH_AUTHORIZE_URL", "http://127.0.0.1:18715/oauth/authorize")
    assert manor_oauth.manor_application_url(provider) == f"http://127.0.0.1:18715{path}"
    for unsafe in ("javascript:alert(1)", "https://user:secret@app.example/oauth/authorize", "https://app.example/oauth/authorize?token=secret", "https://app.example/oauth/authorize#secret", "http://external.example/oauth/authorize"):
        monkeypatch.setenv(f"{prefix}_OAUTH_AUTHORIZE_URL", unsafe)
        assert manor_oauth.manor_application_url(provider) is None
    monkeypatch.delenv(f"{prefix}_OAUTH_AUTHORIZE_URL")
    monkeypatch.delenv(f"{prefix}_OAUTH_BASE_URL")
    assert manor_oauth.manor_application_url(provider) == manor_oauth.MANOR_OAUTH_PROVIDERS[provider][1] + path
    assert manor_oauth.manor_application_url("gmail") is None


async def test_catalog_exposes_application_navigation_without_requiring_a_connection(client, monkeypatch):
    headers, _, _ = await _owner(client, "application_navigation")
    monkeypatch.setenv("PMS_OAUTH_AUTHORIZE_URL", "https://pms-web.example/oauth/authorize")
    monkeypatch.setenv("MINUTES_OAUTH_AUTHORIZE_URL", "https://minutes-web.example/oauth/authorize")
    response = await client.get("/api/v1/integrations/mcp-servers", headers=headers)
    assert response.status_code == 200
    rows = {row["server_key"]: row for row in response.json()}
    assert rows["manor_pms"]["app_url"] == "https://pms-web.example/management"
    assert rows["manor_mcp_minutes"]["app_url"] == "https://minutes-web.example/"
    assert rows["manor_pms"]["user_connected"] is False
    assert rows["manor_mcp_minutes"]["user_connected"] is False
    assert rows["gmail"]["app_url"] is None


@pytest.mark.parametrize("failure", ["timeout", "http_error", "invalid_json", "incomplete"])
async def test_ambiguous_rotating_refresh_disables_replay_and_keeps_revoke_material(monkeypatch, failure):
    from packages.core.tasks import oauth_refresh
    from packages.core.services import oauth_provider_config

    async def config(db, provider):
        return SimpleNamespace(server_key=provider, client_id="client", client_secret="secret", token_url="https://application.example/api/oauth/token", scopes="pms:read pms:write")
    monkeypatch.setattr(oauth_provider_config, "resolve_oauth_config", config)
    seen = []
    def handler(request):
        seen.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive upstream data", request=request)
        if failure == "http_error":
            return httpx.Response(503, text="sensitive upstream data")
        if failure == "invalid_json":
            return httpx.Response(200, text="sensitive upstream data")
        return httpx.Response(200, json={"access_token": "new-access"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    result = await oauth_refresh.refresh_token_via_provider("manor_pms", "refresh", db=object())
    assert oauth_refresh._is_permanent_refresh_error(result)
    assert len(seen) == 1 and "sensitive upstream data" not in json.dumps(result)
    row = SimpleNamespace(profile={}, credential_ref="encrypted-ref", access_token=None, refresh_token=None, token_expires_at=datetime.now(timezone.utc))
    oauth_refresh._mark_oauth_account_reauth_required(row, "manor_pms", oauth_refresh._permanent_refresh_error(result))
    assert row.profile["oauth_refresh"]["reauth_required"] and row.token_expires_at is None
    assert row.credential_ref == "encrypted-ref"


@pytest.fixture
def oauth_vault(monkeypatch):
    import packages.core.credentials as credential_module
    import packages.core.services.oauth_account_credentials as account_credentials

    audit = SimpleNamespace(log=lambda event: None)
    vault = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode(), audit_sink=audit), audit_sink=audit)
    monkeypatch.setattr(credential_module, "get_credential_service", lambda: vault)
    monkeypatch.setattr(account_credentials, "get_credential_service", lambda: vault)
    return vault


async def _owner(client, suffix):
    response = await register_user_and_get_token(client, json={
        "username": f"oauth_{suffix}", "email": f"oauth_{suffix}@test.com",
        "password": "TestPassword123!", "entity_name": f"OAuth {suffix}",
    })
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return headers, me.json()["id"], me.json()["entity_id"]


def _provider_transport(monkeypatch, provider, prefix, owner_id, entity_id):
    from packages.core.services import oauth_flow
    from packages.core.tasks.channel_tasks import health_check_task

    monkeypatch.setenv(f"{prefix}_OAUTH_CLIENT_ID", "manor-test")
    monkeypatch.setenv(f"{prefix}_OAUTH_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setenv(f"{prefix}_OAUTH_BASE_URL", "https://application.example")
    monkeypatch.setenv(f"{prefix}_OAUTH_AUTHORIZE_URL", "https://application.example/oauth/authorize")
    monkeypatch.setenv("APP_URL", "https://manor.example")
    async def no_redis():
        return None
    monkeypatch.setattr(oauth_flow, "_redis_client", no_redis)
    monkeypatch.setattr(oauth_flow, "_shared_state_store_required", lambda: False)
    monkeypatch.setattr(health_check_task, "delay", Mock())
    seen = []
    state = {"revoke_status": 200, "identity_entity": entity_id, "token_sequence": 0}
    scope = manor_oauth.manor_oauth_endpoints(provider)["scopes"]
    def handler(request):
        seen.append(request)
        if request.url.path == "/api/oauth/token":
            state["token_sequence"] += 1
            return httpx.Response(200, json={
                "access_token": f"opaque-access-{state['token_sequence']}",
                "refresh_token": f"opaque-refresh-{state['token_sequence']}",
                "expires_in": 3600, "scope": scope,
            })
        if request.url.path == "/api/oauth/userinfo":
            return httpx.Response(200, json={
                "sub": f"{provider}:remote-org:remote-user", "email": "owner@test.com", "name": "Owner",
                "manor_user_id": owner_id, "manor_entity_id": state["identity_entity"],
                "grant_status": state.get("grant_status"),
            })
        if request.url.path == "/api/oauth/revoke":
            return httpx.Response(state["revoke_status"], json={"ok": state["revoke_status"] == 200})
        raise AssertionError(f"Unexpected provider endpoint: {request.url.path}")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return seen, state


async def _start(client, provider, headers, connection_id=None):
    response = await client.get(f"/api/v1/integrations/oauth/{provider}/start", headers=headers, params={"connection_id": connection_id} if connection_id else {})
    assert response.status_code == 200, response.text
    params = parse_qs(urlsplit(response.json()["authorize_url"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in params
    assert "access_type" not in params
    return params["state"][0]


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_connect_refresh_disconnect_and_actor_scoping(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module
    from packages.core.services.agent_permission_service import can_use_integration
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens
    from packages.core.tasks.oauth_refresh import _refresh_oauth_accounts

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    other_headers, other_id, other_entity = await _owner(client, prefix.lower() + "_other")
    seen, remote = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    state = await _start(client, provider, headers)
    callback_url = f"/api/v1/integrations/oauth/{provider}/callback"
    connected = await client.get(callback_url, params={"code": "synthetic-code", "state": state})
    assert connected.status_code == 302, connected.text
    assert "connected=" + provider in connected.headers["location"]
    replay = await client.get(callback_url, params={"code": "synthetic-code", "state": state})
    assert replay.status_code == 400
    assert sum(r.url.path.endswith("/token") for r in seen) == 1
    code_form = parse_qs(seen[0].content.decode())
    assert len(code_form["code_verifier"][0]) >= 43

    async with db_module.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
        row_id = row.id
        assert row.access_token is None and row.refresh_token is None and row.credential_ref
        assert manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)
        assert not manor_oauth.manor_oauth_account_matches(row, entity_id=other_entity)
        allowed = await can_use_integration(db, user_id=user_id, entity_id=entity_id, provider=provider, integration_account_id=row.id, allow_env_fallback=False)
        denied = await can_use_integration(db, user_id=other_id, entity_id=other_entity, provider=provider, integration_account_id=row.id, allow_env_fallback=False)
        assert allowed.allowed and allowed.scope == "user"
        assert not denied.allowed
        row.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    async with db_module.async_session() as db:
        assert await _refresh_oauth_accounts(db) == 1
        await db.commit()
        row = await db.get(OAuthAccount, row_id)
        tokens = lease_oauth_account_tokens(row, requester_id="test", reason="test")
        assert tokens == {"access_token": "opaque-access-2", "refresh_token": "opaque-refresh-2"}
        assert row.access_token is None and row.refresh_token is None
    healthy = await client.post(f"/api/v1/integrations/health-check/connections/{row_id}", headers=headers)
    assert healthy.status_code == 200 and healthy.json()["ok"]
    foreign = await client.post(f"/api/v1/integrations/health-check/connections/{row_id}", headers=other_headers)
    assert foreign.status_code == 404

    delete_url = f"/api/v1/integrations/mcp-servers/{provider}/connections/{row_id}"
    remote["revoke_status"] = 503
    failed = await client.delete(delete_url, headers=headers)
    assert failed.status_code == 502 and "disabled in Manor" in failed.json()["detail"]
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert row and row.profile["oauth_disconnect"]["status"] == "pending"
        assert not manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)
        denied = await can_use_integration(db, user_id=user_id, entity_id=entity_id, provider=provider, integration_account_id=row.id, allow_env_fallback=False)
        assert not denied.allowed
        assert await _refresh_oauth_accounts(db) == 0
    remote["revoke_status"] = 200
    assert (await client.delete(delete_url, headers=headers)).status_code == 204
    async with db_module.async_session() as db:
        assert await db.get(OAuthAccount, row_id) is None


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_callback_rejects_foreign_entity_and_configuration_change(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    seen, remote = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    state = await _start(client, provider, headers)
    remote["identity_entity"] = "foreign-entity"
    callback_url = f"/api/v1/integrations/oauth/{provider}/callback"
    response = await client.get(callback_url, params={"code": "code", "state": state})
    assert response.status_code == 403
    async with db_module.async_session() as db:
        assert not (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalars().all()

    state = await _start(client, provider, headers)
    monkeypatch.setenv(f"{prefix}_OAUTH_BASE_URL", "https://replacement.example")
    before = len(seen)
    response = await client.get(callback_url, params={"code": "code", "state": state})
    assert response.status_code == 400
    assert len(seen) == before


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
@pytest.mark.parametrize("first", ["health", "refresh"])
async def test_health_and_rotating_refresh_serialize_tokens(client, monkeypatch, oauth_vault, provider, prefix, first):
    """A health response for old access must never disable its rotated grant."""
    import packages.core.database as db_module
    from packages.core.services.integration_health import run_and_persist_oauth
    from packages.core.services.oauth_flow import OAuthFlowError
    from packages.core.tasks.oauth_refresh import _refresh_oauth_accounts

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    _, remote = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    state = await _start(client, provider, headers)
    response = await client.get(f"/api/v1/integrations/oauth/{provider}/callback", params={"code": "code", "state": state})
    assert response.status_code == 302
    async with db_module.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
        row_id = row.id
        row.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    first_entered, release_first, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def verify(account, access_token):
        if asyncio.current_task().get_name() == first:
            first_entered.set()
            await asyncio.wait_for(release_first.wait(), timeout=5)
        if access_token != f"opaque-access-{remote['token_sequence']}":
            raise OAuthFlowError(401, "Access token was rotated.")

    monkeypatch.setattr(manor_oauth, "verify_manor_oauth_account", verify)

    async def run(kind):
        if kind != first:
            second_started.set()
        async with db_module.async_session() as db:
            result = await (run_and_persist_oauth(db, row_id) if kind == "health" else _refresh_oauth_accounts(db))
            await db.commit()
            return result

    first_task = asyncio.create_task(run(first), name=first)
    await asyncio.wait_for(first_entered.wait(), timeout=5)
    second = "refresh" if first == "health" else "health"
    second_task = asyncio.create_task(run(second), name=second)
    try:
        await asyncio.wait_for(second_started.wait(), timeout=5)
        # Let the other real DB session reach the held row lock while the
        # provider response is controlled by the barrier above.
        await asyncio.sleep(0.1)
    finally:
        release_first.set()
    results = dict(zip([first, second], await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=10)))
    assert results["health"]["ok"]
    assert results["refresh"] == 1
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)
        assert not (row.profile or {}).get("oauth_refresh", {}).get("reauth_required")


async def test_catalog_upgrade_refreshes_oauth_metadata_and_preserves_saved_accounts(client):
    import packages.core.database as db_module
    from packages.core.models.document import Integration
    from packages.core.models.mcp import MCPServer
    from packages.core.services.mcp_seed import seed_mcp_catalog

    async with db_module.async_session() as db:
        legacy = Integration(entity_id="legacy-test-entity", provider="manor_pms", status="active", credentials={"token": "synthetic-legacy-token"})
        db.add(legacy)
        rows = (await db.execute(select(MCPServer).where(MCPServer.server_key.in_(["manor_pms", "manor_mcp_minutes"])))).scalars().all()
        assert len(rows) == 2
        for row in rows:
            row.auth_type = "api_key" if row.server_key == "manor_pms" else "none"
            row.scopes = None
        await db.commit()
        legacy_id = legacy.id

    await seed_mcp_catalog(db_module.engine)

    async with db_module.async_session() as db:
        rows = (await db.execute(select(MCPServer).where(MCPServer.server_key.in_(["manor_pms", "manor_mcp_minutes"])))).scalars().all()
        for row in rows:
            assert row.auth_type == "oauth2"
            assert set(row.scopes.split()) == set(manor_oauth.manor_oauth_endpoints(row.server_key)["scopes"].split())
        legacy = await db.get(Integration, legacy_id)
        assert legacy and legacy.status == "active"
        assert legacy.credentials == {"token": "synthetic-legacy-token"}


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_reconnect_and_duplicate_add_retire_previous_grants(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    seen, _ = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    async with db_module.async_session() as db:
        row_id = (await db.execute(select(OAuthAccount.id).where(OAuthAccount.user_id == user_id))).scalar_one()
    for sequence, reconnect in [(2, True), (3, False)]:
        state = await _start(client, provider, headers, row_id if reconnect else None)
        response = await client.get(callback, params={"code": "new", "state": state})
        assert response.status_code == 302
        revoked = [parse_qs(r.content.decode())["token"][0] for r in seen if r.url.path.endswith("/revoke")]
        assert revoked[-1] == f"opaque-refresh-{sequence - 1}"
        async with db_module.async_session() as db:
            rows = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalars().all()
            assert len(rows) == 1 and rows[0].id == row_id
            row = rows[0]
            assert manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)
            tokens = lease_oauth_account_tokens(row, requester_id="test", reason="test")
            assert tokens == {"access_token": f"opaque-access-{sequence}", "refresh_token": f"opaque-refresh-{sequence}"}
            assert "pending_revocations" not in json.dumps(row.profile)


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_unreadable_reconnect_requires_live_provider_retirement_proof(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    seen, remote = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    async with db_module.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
        row_id = row.id
        row.credential_ref = "unreadable-original-envelope"
        row.profile = {**row.profile, "oauth_refresh": {"reauth_required": True, "error": "credential_decrypt_failed"}}
        await db.commit()
    # Existing providers without the contract remain disabled; the newly
    # exchanged grant is retired instead of silently orphaned.
    state = await _start(client, provider, headers, row_id)
    failed = await client.get(callback, params={"code": "second", "state": state})
    assert failed.status_code == 502
    assert parse_qs(seen[-1].content.decode())["token"] == ["opaque-refresh-2"]
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert row.credential_ref == "unreadable-original-envelope"
        assert not manor_oauth.manor_oauth_account_matches(row)
    remote["grant_status"] = {"version": 1, "grant_id": "fresh-grant", "sole_active_grant": True}
    state = await _start(client, provider, headers, row_id)
    connected = await client.get(callback, params={"code": "third", "state": state})
    assert connected.status_code == 302
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)
        assert lease_oauth_account_tokens(row, requester_id="test", reason="test") == {
            "access_token": "opaque-access-3", "refresh_token": "opaque-refresh-3",
        }
        assert "grant_status" not in row.profile


async def test_unreadable_reconnect_retains_both_grants_when_cleanup_is_unavailable(client, monkeypatch, oauth_vault):
    import packages.core.database as db_module
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    provider = "manor_mcp_minutes"
    headers, user_id, _ = await _owner(client, "unreadable_cleanup")
    me = await client.get("/api/v1/auth/me", headers=headers)
    _, remote = _provider_transport(monkeypatch, provider, "MINUTES", user_id, me.json()["entity_id"])
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    async with db_module.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
        row_id = row.id
        row.credential_ref = "unreadable-original-envelope"
        await db.commit()
    remote["revoke_status"] = 503
    state = await _start(client, provider, headers, row_id)
    assert (await client.get(callback, params={"code": "second", "state": state})).status_code == 502
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert not manor_oauth.manor_oauth_account_matches(row)
        tokens = lease_oauth_account_tokens(row, requester_id="test", reason="test")
        assert tokens["unreadable_credential"]["ref"] == "unreadable-original-envelope"
        assert tokens["pending_revocations"][0]["refresh_token"] == "opaque-refresh-2"
        assert "opaque-" not in json.dumps(row.profile)
    remote["revoke_status"] = 200
    # Cleaning the readable new grant must not delete the unreadable old one.
    assert (await client.delete(f"/api/v1/integrations/mcp-servers/{provider}/connections/{row_id}", headers=headers)).status_code == 502


async def test_grant_retirement_receipt_rejects_other_identity_and_stale_metadata(monkeypatch):
    row = SimpleNamespace(provider="manor_mcp_minutes", user_id="owner", provider_user_id="subject", profile={
        "manor_oauth": {"version": 1, "issuer": "https://application.example", "subject": "subject", "user_id": "owner", "entity_id": "entity"},
    })
    _, remote = _provider_transport(monkeypatch, row.provider, "MINUTES", row.user_id, "entity")
    row.provider_user_id = row.profile["manor_oauth"]["subject"] = f"{row.provider}:remote-org:remote-user"
    for receipt in (None, {"version": 1, "grant_id": "new", "sole_active_grant": False}, {"version": True, "grant_id": "new", "sole_active_grant": True}):
        remote["grant_status"] = receipt
        assert not await manor_oauth._provider_confirms_previous_grants_retired(row, "fresh-access")
    remote["grant_status"] = {"version": 1, "grant_id": "new", "sole_active_grant": True}
    remote["identity_entity"] = "another-entity"
    assert not await manor_oauth._provider_confirms_previous_grants_retired(row, "fresh-access")
    remote["identity_entity"] = "entity"
    row.profile["manor_oauth"]["issuer"] = "https://old.example"
    assert not await manor_oauth._provider_confirms_previous_grants_retired(row, "fresh-access")
    row.profile["manor_oauth"]["issuer"] = "https://application.example"
    row.profile["oauth_disconnect"] = {"status": "pending"}
    assert not await manor_oauth._provider_confirms_previous_grants_retired(row, "fresh-access")


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_failed_reconnect_keeps_every_grant_for_disconnect(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens
    from packages.core.tasks.oauth_refresh import _refresh_oauth_accounts

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    seen, remote = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    async with db_module.async_session() as db:
        row_id = (await db.execute(select(OAuthAccount.id).where(OAuthAccount.user_id == user_id))).scalar_one()
    remote["revoke_status"] = 503
    state = await _start(client, provider, headers, row_id)
    failed = await client.get(callback, params={"code": "second", "state": state})
    assert failed.status_code == 502 and "both grants" in failed.json()["detail"]
    # A second, already-issued Add-account callback cannot discard its new
    # grant when it encounters the first replacement's pending cleanup.
    state = await _start(client, provider, headers)
    failed_again = await client.get(callback, params={"code": "third", "state": state})
    assert failed_again.status_code == 409
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert not manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)
        assert row.access_token is None and row.refresh_token is None
        assert "opaque-" not in json.dumps(row.profile)
        tokens = lease_oauth_account_tokens(row, requester_id="test", reason="test")
        assert tokens["refresh_token"] == "opaque-refresh-1"
        assert [x["refresh_token"] for x in tokens["pending_revocations"]] == ["opaque-refresh-2", "opaque-refresh-3"]
        assert await _refresh_oauth_accounts(db) == 0
    delete_url = f"/api/v1/integrations/mcp-servers/{provider}/connections/{row_id}"
    assert (await client.delete(delete_url, headers=headers)).status_code == 502
    async with db_module.async_session() as db:
        assert await db.get(OAuthAccount, row_id) is not None
    remote["revoke_status"] = 200
    assert (await client.delete(delete_url, headers=headers)).status_code == 204
    revoked = [parse_qs(r.content.decode())["token"][0] for r in seen if r.url.path.endswith("/revoke")]
    assert revoked[-3:] == ["opaque-refresh-1", "opaque-refresh-2", "opaque-refresh-3"]
    async with db_module.async_session() as db:
        assert await db.get(OAuthAccount, row_id) is None


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_reconnect_activation_exception_preserves_committed_grants(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module
    import packages.core.services.oauth_account_credentials as account_credentials

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    async with db_module.async_session() as db:
        row_id = (await db.execute(select(OAuthAccount.id).where(OAuthAccount.user_id == user_id))).scalar_one()
    def activation_failure(*args, **kwargs):
        raise RuntimeError("synthetic failure after durable staging")
    monkeypatch.setattr(account_credentials, "store_oauth_account_tokens", activation_failure)
    state = await _start(client, provider, headers, row_id)
    with pytest.raises(RuntimeError, match="after durable staging"):
        await client.get(callback, params={"code": "second", "state": state})
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert row.profile["oauth_disconnect"]["status"] == "pending_replacement"
        assert not manor_oauth.manor_oauth_account_matches(row)
        tokens = account_credentials.lease_oauth_account_tokens(row, requester_id="test", reason="test")
        assert tokens["refresh_token"] == "opaque-refresh-1"
        assert tokens["pending_revocations"][0]["refresh_token"] == "opaque-refresh-2"
    assert (await client.delete(f"/api/v1/integrations/mcp-servers/{provider}/connections/{row_id}", headers=headers)).status_code == 204


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_cancel_consumes_bound_state_without_changing_existing_connection(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    seen, _ = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    async with db_module.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
        row_id, reference = row.id, row.credential_ref
    state = await _start(client, provider, headers, row_id)
    before = len(seen)
    params = {"error": "access_denied", "state": state, "error_description": "<script>untrusted</script>"}
    response = await client.get(callback, params=params)
    assert response.status_code == 302
    assert response.headers["location"] == f"https://manor.example/integrations?oauth_cancelled={provider}"
    assert len(seen) == before
    assert (await client.get(callback, params=params)).status_code == 400
    assert (await client.get(callback, params={"error": "access_denied"})).status_code == 400
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        assert row.credential_ref == reference and manor_oauth.manor_oauth_account_matches(row, entity_id=entity_id)


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_concurrent_duplicate_callbacks_do_not_deadlock_or_lose_grants(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    seen, _ = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    states = [await _start(client, provider, headers) for _ in range(2)]
    responses = await asyncio.wait_for(asyncio.gather(*[
        client.get(callback, params={"code": "parallel", "state": item}) for item in states
    ]), timeout=15)
    assert all(response.status_code in {302, 409} for response in responses)
    async with db_module.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
        row_id = row.id
        tokens = lease_oauth_account_tokens(row, requester_id="test", reason="test")
        retained = {tokens["refresh_token"], *[x["refresh_token"] for x in tokens.get("pending_revocations", [])]}
        revoked = {parse_qs(r.content.decode())["token"][0] for r in seen if r.url.path.endswith("/revoke")}
        assert retained | revoked == {"opaque-refresh-1", "opaque-refresh-2", "opaque-refresh-3"}
        if tokens.get("pending_revocations"):
            assert not manor_oauth.manor_oauth_account_matches(row)
    assert (await client.delete(f"/api/v1/integrations/mcp-servers/{provider}/connections/{row_id}", headers=headers)).status_code == 204
    revoked = {parse_qs(r.content.decode())["token"][0] for r in seen if r.url.path.endswith("/revoke")}
    assert revoked == {"opaque-refresh-1", "opaque-refresh-2", "opaque-refresh-3"}


@pytest.mark.parametrize("provider,prefix", [("manor_pms", "PMS"), ("manor_mcp_minutes", "MINUTES")])
async def test_cross_issuer_cleanup_retains_confirmation_without_leaking_tokens(client, monkeypatch, oauth_vault, provider, prefix):
    import packages.core.database as db_module
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    headers, user_id, entity_id = await _owner(client, prefix.lower())
    seen, _ = _provider_transport(monkeypatch, provider, prefix, user_id, entity_id)
    callback = f"/api/v1/integrations/oauth/{provider}/callback"
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "first", "state": state})).status_code == 302
    monkeypatch.setenv(f"{prefix}_OAUTH_BASE_URL", "https://replacement.example")
    state = await _start(client, provider, headers)
    assert (await client.get(callback, params={"code": "second", "state": state})).status_code == 502
    async with db_module.async_session() as db:
        row_id = (await db.execute(select(OAuthAccount.id).where(OAuthAccount.user_id == user_id))).scalar_one()
    delete_url = f"/api/v1/integrations/mcp-servers/{provider}/connections/{row_id}"
    assert (await client.delete(delete_url, headers=headers)).status_code == 502
    async with db_module.async_session() as db:
        row = await db.get(OAuthAccount, row_id)
        tokens = lease_oauth_account_tokens(row, requester_id="test", reason="test")
        assert tokens["revocation_confirmed"] is False
        assert tokens["pending_revocations"][0]["revocation_confirmed"] is True
    monkeypatch.setenv(f"{prefix}_OAUTH_BASE_URL", "https://application.example")
    assert (await client.delete(delete_url, headers=headers)).status_code == 204
    revoked = {(r.url.host, parse_qs(r.content.decode())["token"][0]) for r in seen if r.url.path.endswith("/revoke")}
    assert revoked == {("application.example", "opaque-refresh-1"), ("replacement.example", "opaque-refresh-2")}
