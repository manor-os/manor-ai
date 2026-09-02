"""Robinhood public-client, account binding and financial approval boundaries."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from packages.core.services import oauth_flow, oauth_provider_config as config_service
from packages.core.services.official_remote_mcp import (
    MCPActionEffect,
    OfficialRemoteMCPActionPolicyFactory,
    OfficialRemoteMCPFactory,
)
from packages.core.services.robinhood_oauth import (
    ROBINHOOD_CALLBACK_PATH,
    ROBINHOOD_MCP_ENDPOINT,
    robinhood_registration_payload,
)


def _db(server=None):
    return SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: server)),
        flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
    )


@pytest.fixture
def public_client(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CLIENT_ID", "test-public-client")
    return config_service.OAuthProviderConfig(
        server_key="robinhood", client_id="test-public-client", client_secret="",
        authorize_url="https://robinhood.com/oauth",
        token_url="https://api.robinhood.com/oauth2/token/", scopes="internal",
        redirect_path=ROBINHOOD_CALLBACK_PATH, source="test",
    )


@pytest.mark.parametrize("origin", ["https://app.example.test", "http://localhost:3010"])
def test_registration_uses_exact_callback_without_secret(origin):
    payload = robinhood_registration_payload(origin + "/")
    assert payload["redirect_uris"] == [origin + ROBINHOOD_CALLBACK_PATH]
    assert payload["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in payload


@pytest.mark.parametrize("origin", [
    "http://app.example.test", "https://user:password@app.example.test",
    "https://app.example.test/path", "https://app.example.test?redirect=evil",
    "https://app.example.test#fragment", "javascript:alert(1)", "",
])
def test_registration_rejects_non_origin_and_insecure_urls(origin):
    with pytest.raises(ValueError):
        robinhood_registration_payload(origin)


@pytest.mark.asyncio
async def test_public_client_resolves_without_secret_but_private_clients_do_not(
    monkeypatch, public_client,
):
    assert config_service.oauth_client_configured("robinhood")
    resolved = await config_service.resolve_oauth_config(_db(), "robinhood")
    assert resolved.client_id == public_client.client_id
    assert resolved.client_secret == ""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "private-client")
    monkeypatch.delenv("GITHUB_CLIENT_SECRET", raising=False)
    assert not config_service.oauth_client_configured("github")
    assert await config_service.resolve_oauth_config(_db(), "github") is None


@pytest.mark.asyncio
async def test_public_client_ui_override_does_not_lease_or_store_a_secret(monkeypatch, public_client):
    server = SimpleNamespace(default_config={}, credential_ref=None)
    db = _db(server)
    credentials = Mock(side_effect=AssertionError("public client must not access the vault"))
    monkeypatch.setattr("packages.core.credentials.get_credential_service", credentials)
    assert await config_service.save_oauth_config(
        db, "robinhood", client_id="ui-client", client_secret="must-not-store",
    )
    assert server.default_config == {"oauth_client_id": "ui-client", "_oauth_source": "ui"}
    assert config_service.oauth_client_configured("robinhood", server)
    resolved = await config_service.resolve_oauth_config(db, "robinhood")
    assert resolved.client_id == "ui-client"
    assert resolved.client_secret == ""
    credentials.assert_not_called()


@pytest.mark.asyncio
async def test_public_env_seed_works_when_private_vault_is_unavailable(monkeypatch, public_client):
    monkeypatch.setattr(config_service, "_PROVIDER_OAUTH_META", {
        key: config_service._PROVIDER_OAUTH_META[key] for key in ("robinhood", "github")
    })
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "github-secret")
    credentials = Mock()
    credentials.health.return_value = SimpleNamespace(ok=False, detail="offline")
    monkeypatch.setattr("packages.core.credentials.get_credential_service", lambda: credentials)
    server = SimpleNamespace(default_config={}, credential_ref=None)
    db = _db(server)
    actions = await config_service.seed_oauth_clients_from_env(db)
    assert actions["robinhood"] == "seeded"
    assert actions["github"].startswith("error:")
    assert server.default_config["oauth_client_id"] == public_client.client_id
    credentials.store_mcp_server.assert_not_called()
    db.commit.assert_awaited_once()
    actions = await config_service.seed_oauth_clients_from_env(db)
    assert actions["robinhood"] == "unchanged"


@pytest.mark.asyncio
async def test_pkce_callback_binds_user_and_consumes_state_once(monkeypatch, public_client):
    monkeypatch.setattr(oauth_flow, "_redis_client", AsyncMock(return_value=None))
    monkeypatch.setattr(oauth_flow, "_shared_state_store_required", lambda: False)
    monkeypatch.setattr(oauth_flow, "_pending_oauth_states", {})
    callback = "https://app.example.test" + ROBINHOOD_CALLBACK_PATH
    started = await oauth_flow.begin_authorization(
        config=public_client, user_id="user-a", entity_id="entity-a",
        connection_id="account-a", redirect_uri=callback,
    )
    pending = await oauth_flow.get_pending_state(started.state, server_key="robinhood")
    assert (pending["user_id"], pending["entity_id"], pending["connection_id"]) == (
        "user-a", "entity-a", "account-a",
    )
    query = parse_qs(urlsplit(started.authorize_url).query)
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(pending["code_verifier"].encode()).digest(),
    ).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected_challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [callback]
    assert query["resource"] == [ROBINHOOD_MCP_ENDPOINT]
    assert query["scope"] == ["internal"]
    assert "prompt" not in query and "access_type" not in query
    with pytest.raises(oauth_flow.OAuthFlowError):
        await oauth_flow.get_pending_state(started.state, server_key="github")

    post = AsyncMock(return_value=httpx.Response(200, json={
        "access_token": "test-access", "refresh_token": "test-refresh", "expires_in": 3600,
    }))
    client = AsyncMock()
    client.__aenter__.return_value.post = post
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    user_id, tokens = await oauth_flow.complete_authorization(
        server_key="robinhood", code="test-code", state=started.state,
        redirect_uri=callback, config=public_client,
    )
    assert user_id == "user-a" and tokens.refresh_token == "test-refresh"
    body = post.call_args.kwargs["data"]
    assert body["code_verifier"] == pending["code_verifier"]
    assert body["resource"] == ROBINHOOD_MCP_ENDPOINT
    assert body["client_id"] == public_client.client_id
    assert "client_secret" not in body
    assert "Authorization" not in post.call_args.kwargs["headers"]
    with pytest.raises(oauth_flow.OAuthFlowError):
        await oauth_flow.complete_authorization(
            server_key="robinhood", code="test-code", state=started.state,
            redirect_uri=callback, config=public_client,
        )
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_uses_public_client_resource_without_secret(monkeypatch, public_client):
    from packages.core.tasks.oauth_refresh import _PROVIDER_LOOKUP_KEYS, refresh_token_via_provider

    assert "robinhood" in _PROVIDER_LOOKUP_KEYS
    post = AsyncMock(return_value=httpx.Response(200, json={
        "access_token": "rotated-access", "refresh_token": "rotated-refresh",
    }))
    client = AsyncMock()
    client.__aenter__.return_value.post = post
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    tokens = await refresh_token_via_provider("robinhood", "old-refresh", db=_db())
    assert tokens["refresh_token"] == "rotated-refresh"
    assert post.call_args.args == (public_client.token_url,)
    assert post.call_args.kwargs["data"] == {
        "grant_type": "refresh_token", "refresh_token": "old-refresh",
        "client_id": public_client.client_id, "resource": ROBINHOOD_MCP_ENDPOINT,
    }
    assert "Authorization" not in post.call_args.kwargs["headers"]


@pytest.mark.parametrize("action,is_read", [
    ("get_portfolio", True), ("get_watchlists", True), ("get_equity_quotes", True),
    ("place_equity_order", False), ("cancel_equity_order", False),
    ("transfer_funds", False), ("new_vendor_action", False),
])
def test_financial_writes_and_unknown_tools_cannot_bypass_approval(action, is_read):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool

    effect = OfficialRemoteMCPFactory.resolve_action_effect(
        "robinhood", action, annotations={"readOnlyHint": True},
    )
    assert (effect is MCPActionEffect.READ) is is_read
    policy = OfficialRemoteMCPActionPolicyFactory.create("robinhood", effect)
    classification = classify_runtime_tool(
        f"mcp__robinhood__{action}", {}, declared_effect=effect.value,
    )
    assert policy.required_approval is not is_read
    if not is_read:
        assert policy.risk_level == "high"
        assert classification.action.risk_level == "high"
    else:
        assert classification.action is None


def test_catalog_has_official_endpoint_and_no_callable_fallback():
    from packages.core.services.mcp_seed import _MCP_CATALOG

    row = next(row for row in _MCP_CATALOG if row[0] == "robinhood")
    assert row[3:] == ("http", ROBINHOOD_MCP_ENDPOINT, "oauth2", "internal")
    assert OfficialRemoteMCPFactory.tool_schemas("robinhood") == []
    # Schema is vendor-owned and supplied by authenticated tools/list only.
    schemas = OfficialRemoteMCPFactory.tool_schemas("robinhood", discovered_tools=[{
        "name": "get_portfolio", "description": "Account portfolio",
        "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}},
                        "required": ["account_id"]},
    }])
    assert schemas[0]["parameters"]["required"] == ["account_id"]


@pytest.mark.asyncio
async def test_account_health_only_lists_official_tools_and_redacts_errors(monkeypatch):
    from packages.core.ai.mcp import _remote
    from packages.core.services.integration_health import run_test

    remote = Mock()
    remote.list_tools = AsyncMock(return_value=[])
    factory = Mock(return_value=remote)
    monkeypatch.setattr(_remote, "RemoteMCPClient", factory)
    missing = await run_test("robinhood", {})
    assert missing["ok"] is False
    factory.assert_not_called()
    healthy = await run_test("robinhood", {"access_token": "test-user-token"})
    assert healthy["ok"] is True
    factory.assert_called_once_with(ROBINHOOD_MCP_ENDPOINT, "test-user-token", timeout=10)
    remote.list_tools.assert_awaited_once()
    remote.call_tool.assert_not_called()
    remote.list_tools.side_effect = _remote.RemoteMCPError(code=-32001, message="private-token")
    rejected = await run_test("robinhood", {"access_token": "test-user-token"})
    assert rejected["ok"] is False
    assert "private-token" not in rejected["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["integrations", "admin"])
async def test_configuration_routes_accept_public_client_only_and_still_protect_private_clients(
    monkeypatch, route,
):
    from fastapi import HTTPException
    from apps.api.routers import admin_oauth, integrations

    save = AsyncMock(return_value=True)
    db, user = _db(), SimpleNamespace(id="admin-user")
    if route == "integrations":
        monkeypatch.setattr("packages.core.permissions.check_effective_user_permission", AsyncMock())
        monkeypatch.setattr(config_service, "save_oauth_config", save)
        request_type, call = integrations.OAuthConfigRequest, integrations.set_oauth_config
    else:
        monkeypatch.setattr(admin_oauth, "_require_admin", AsyncMock())
        monkeypatch.setattr(admin_oauth, "save_oauth_config", save)
        request_type, call = admin_oauth.UpdateOAuthClientRequest, admin_oauth.update_oauth_client
    await call("robinhood", request_type(client_id=" public-id "), user=user, db=db)
    assert save.call_args.kwargs["client_id"] == "public-id"
    assert save.call_args.kwargs["client_secret"] == ""
    with pytest.raises(HTTPException) as missing_secret:
        await call("github", request_type(client_id="private-id"), user=user, db=db)
    assert missing_secret.value.status_code == 400
    save.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,ok", [(200, True), (400, False), (401, False)])
async def test_public_client_health_uses_real_callback_and_does_not_accept_invalid_requests(
    monkeypatch, public_client, status, ok,
):
    from apps.api.routers import admin_oauth

    monkeypatch.setenv("APP_URL", "https://app.example.test")
    monkeypatch.setattr(admin_oauth, "_require_admin", AsyncMock())
    get = AsyncMock(return_value=httpx.Response(status))
    client = AsyncMock()
    client.__aenter__.return_value.get = get
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    result = await admin_oauth.check_oauth_client_health(
        "robinhood", db=_db(), user=SimpleNamespace(id="admin"),
    )
    assert result.ok is ok
    params = get.call_args.kwargs["params"]
    assert params["redirect_uri"] == "https://app.example.test" + ROBINHOOD_CALLBACK_PATH
    assert params["resource"] == ROBINHOOD_MCP_ENDPOINT
    assert params["code_challenge_method"] == "S256"
    if ok:
        assert "not verified" in result.detail
