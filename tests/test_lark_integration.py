"""Lark catalog, account credentials and shared runtime boundary contracts."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
import httpx
import pytest

from packages.core.ai.mcp import get_module, lark
from packages.core.ai.runtime.approval_classifier import (
    RuntimeToolEffect,
    classify_runtime_tool,
)
from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link
from packages.core.ai.runtime.integration_skill_registry import (
    INTEGRATION_SKILL_ROUTES,
    integration_provider_keys_for_skill,
    integration_skill_route_for_message,
)
from packages.core.ai.tools import mcp_builtin
from packages.core.credentials.dev_provider import DevKeyProvider
from packages.core.credentials import CredentialError
from packages.core.credentials.service import CredentialService
from packages.core.integrations.registry import canonical_integration_key
from packages.core.services.agent_permission_service import ToolAccessDecision
from packages.core.services.integration_operation_catalog import integration_operation_catalog
from packages.core.services.mcp_seed import _MCP_CATALOG, is_managed_mcp_server_key


_READ_ACTIONS = frozenset({
    "list_chats", "get_chat", "list_messages", "get_document",
    "get_document_content", "list_bitable_tables", "list_bitable_fields",
    "list_bitable_records",
})
_WRITE_ACTIONS = frozenset({
    "send_message", "reply_message", "create_bitable_record", "update_bitable_record",
    "create_document", "append_document_text",
})


def test_lark_catalog_exposes_one_managed_oauth_connector():
    row = next(row for row in _MCP_CATALOG if row[0] == "lark")
    assert row[1] == "Lark"
    assert row[3:6] == ("builtin", "packages.core.ai.mcp.lark", "oauth2")
    assert is_managed_mcp_server_key("lark")
    assert canonical_integration_key("Feishu") == "lark"
    assert get_module("lark") is lark
    assert "lark" in mcp_builtin._JSON_BLOB_PROVIDERS
    assert "lark" not in mcp_builtin._CONTEXT_ONLY_SERVERS


@pytest.mark.parametrize("message", ["Read my Lark messages", "Open this Feishu document", "读取飞书多维表格"])
def test_lark_and_feishu_requests_select_the_same_operational_skill(message):
    route = integration_skill_route_for_message(message)
    assert route == INTEGRATION_SKILL_ROUTES["lark"]
    assert route.child_skill == "mcp_lark"
    assert route.status == "available"
    assert route.parent_skill == "platform-communications"
    assert integration_provider_keys_for_skill("mcp_lark") == ("lark",)
    assert runtime_integration_setup_link("lark")["setup_url"] == "/integrations?provider=lark"


def test_lark_skill_and_deferred_catalog_expose_every_real_operation():
    skill_path = Path(__file__).resolve().parents[1] / "packages/core/ai/skills/mcp_lark/config.json"
    config = json.loads(skill_path.read_text())
    tools = {tool["name"]: tool for tool in lark.list_tools()}
    assert set(tools) == _READ_ACTIONS | _WRITE_ACTIONS
    assert set(config["tools"]) == {f"mcp__lark__{name}" for name in tools}
    registered = {tool["name"]: tool for tool in mcp_builtin._SERVER_TOOL_SCHEMAS["lark"]}
    assert set(registered) == set(tools)
    for name, tool in tools.items():
        assert registered[name]["parameters"] == tool["inputSchema"]


def test_lark_chat_workflow_effects_and_account_fanout_agree():
    operations, source = integration_operation_catalog(
        server_key="lark", transport="builtin", tools_cached=None,
    )
    assert source == "builtin"
    by_name = {operation["name"]: operation for operation in operations}
    assert set(by_name) == _READ_ACTIONS | _WRITE_ACTIONS
    for action, operation in by_name.items():
        full_name = operation["tool_name"]
        classification = classify_runtime_tool(full_name, {})
        tool = next(item for item in mcp_builtin._SERVER_TOOL_SCHEMAS["lark"] if item["name"] == action)
        _, schema, _ = mcp_builtin.MCPToolRegistrationFactory.create("lark", tool)
        parameters = schema["function"]["parameters"]["properties"]
        assert "integration_account_id" in parameters
        if action in _READ_ACTIONS:
            assert operation["effect"] == "read"
            assert classification.effect is RuntimeToolEffect.READ_ONLY
            assert classification.action is None
            assert "integration_account_selection" in parameters
        else:
            assert operation["effect"] == "write"
            assert classification.effect is RuntimeToolEffect.ACTION
            assert classification.action is not None
            assert "integration_account_selection" not in parameters
            if action in {"send_message", "reply_message"}:
                assert classification.action.action_key == "external_message.send"
                assert classification.action.risk_level == "high"
                assert classification.action.capability_id == "external.message"
            else:
                assert classification.action.action_key == f"lark.{action}"
                assert classification.action.capability_id == "mcp.use_personal"


class _Session:
    def __init__(self, row=None):
        self.row = row
        self.statement = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self.row] if self.row else []))


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["lark", "feishu"])
async def test_lark_dispatch_leases_selected_encrypted_app_bundle(monkeypatch, platform):
    import packages.core.credentials as credentials

    service = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode()))
    row = SimpleNamespace(
        id="selected-lark-account", entity_id="entity-lark", owner_user_id="owner-lark",
        provider="lark", config={"is_default": False}, credentials={},
        credential_ref=None, credential_scheme=None,
    )
    bundle = {"app_id": "cli_lark_test", "app_secret": "lark-test-secret", "platform": platform}
    service.store_integration(row, bundle)
    assert row.credentials == {}
    assert "lark-test-secret" not in row.credential_ref
    monkeypatch.setattr(credentials, "get_credential_service", lambda: service)
    session = _Session(row)

    token = await mcp_builtin._resolve_bearer_token(
        server_key="lark", user_id="owner-lark", entity_id="entity-lark", scope="entity",
        integration_account_id=row.id, session_factory=lambda: session,
    )

    assert json.loads(token) == bundle
    query_params = session.statement.compile().params
    assert row.id in query_params.values()
    assert "entity-lark" in query_params.values()
    assert any(set(value) == {"lark", "feishu"} for value in query_params.values() if isinstance(value, list))


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_binding", [True, False])
async def test_lark_user_dispatch_checks_app_binding_before_leasing_and_projects_only_required_fields(monkeypatch, valid_binding):
    import packages.core.credentials as credentials
    from packages.core.services import lark_oauth, oauth_provider_config, oauth_account_credentials

    service = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode()))
    row = SimpleNamespace(
        id="lark-user-account", user_id="owner-lark", provider="lark",
        provider_user_id="lark:cli_saved:user:ou_saved", created_at=None,
        profile={"platform": "lark", "app_id": "cli_saved", "open_id": "ou_saved",
                 "email": "private@example.invalid", "app_secret": "must-not-project"},
        access_token=None, refresh_token=None, credential_ref=None, credential_scheme=None,
    )
    service.store_oauth_account(row, {"access_token": "encrypted-user-token", "refresh_token": "must-not-project-refresh"})
    assert row.access_token is None
    assert "encrypted-user-token" not in row.credential_ref
    lease = Mock(wraps=service.lease_oauth_account)
    monkeypatch.setattr(service, "lease_oauth_account", lease)
    monkeypatch.setattr(credentials, "get_credential_service", lambda: service)
    monkeypatch.setattr(oauth_account_credentials, "get_credential_service", lambda: service)
    monkeypatch.setattr(oauth_provider_config, "resolve_oauth_config", AsyncMock(return_value=SimpleNamespace(
        client_id="cli_saved" if valid_binding else "cli_changed",
    )))

    token = await mcp_builtin._resolve_bearer_token(
        server_key="lark", user_id="owner-lark", entity_id="entity-lark", scope="user",
        integration_account_id=row.id, session_factory=lambda: _Session(row),
    )

    if valid_binding:
        assert json.loads(token) == {
            "access_token": "encrypted-user-token", "platform": "lark", "app_id": "cli_saved",
            "token_type": "user_access_token",
        }
        lease.assert_called_once()
    else:
        assert token is None
        lease.assert_not_called()
    assert lark_oauth.lark_account_matches_config(row, SimpleNamespace(client_id="cli_saved"))


@pytest.mark.asyncio
async def test_lark_permission_denial_prevents_token_lease_and_provider_io(monkeypatch):
    import packages.core.database as database
    from packages.core.services import agent_permission_service

    check = AsyncMock(return_value=ToolAccessDecision(allowed=False, scope="none", reason="Not connected"))
    token = AsyncMock(side_effect=AssertionError("denied account must not be leased"))
    call = AsyncMock(side_effect=AssertionError("denied account must not execute"))
    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(agent_permission_service, "can_use_integration", check)
    monkeypatch.setattr(mcp_builtin, "_resolve_bearer_token", token)
    context = mcp_builtin._MCPResolvedDispatchContext(
        server_row=SimpleNamespace(server_key="lark"), transport="builtin",
        module=SimpleNamespace(call_tool=call),
    )

    result = json.loads(await mcp_builtin._build_handler("lark", "list_chats")(
        entity_id="entity-lark", user_id="owner-lark", integration_account_id="unshared-lark",
        _runtime_mcp_dispatch_context=context,
    ))

    assert result["error"] == "credentials_unavailable"
    assert check.await_args.kwargs["integration_account_id"] == "unshared-lark"
    assert check.await_args.kwargs["allow_env_fallback"] is False
    token.assert_not_awaited()
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403])
async def test_lark_selected_account_dispatch_strips_runtime_arguments_and_preserves_safe_results(monkeypatch, status):
    import packages.core.credentials as credentials
    import packages.core.database as database
    from packages.core.ai.mcp import file_registrar
    from packages.core.services import agent_permission_service

    service = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode()))
    row = SimpleNamespace(
        id="selected-feishu-account", entity_id="entity-lark", provider="lark", config={},
        credentials={}, credential_ref=None, credential_scheme=None,
    )
    service.store_integration(row, {
        "app_id": "cli_dispatch_test", "app_secret": "dispatch-test-secret", "platform": "feishu",
    })
    session = _Session(row)
    check = AsyncMock(return_value=ToolAccessDecision(
        allowed=True, reason="Selected owned app", scope="entity", account_id=row.id,
    ))
    monkeypatch.setattr(credentials, "get_credential_service", lambda: service)
    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(agent_permission_service, "can_use_integration", check)
    monkeypatch.setattr(file_registrar, "register_generated_files", AsyncMock(return_value=0))
    monkeypatch.setattr(lark, "_TOKEN_CACHE", {})
    requests = []
    client_type = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            assert json.loads(request.content) == {"app_id": "cli_dispatch_test", "app_secret": "dispatch-test-secret"}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "selected-tenant-token", "expire": 7200})
        assert request.headers["Authorization"] == "Bearer selected-tenant-token"
        assert dict(request.url.params) == {"page_size": "7"}
        if status == 403:
            return httpx.Response(403, json={"code": 99991672, "msg": "do not echo dispatch-test-secret"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [{"chat_id": "oc_selected"}], "has_more": False}})

    monkeypatch.setattr(lark.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    context = mcp_builtin._MCPResolvedDispatchContext(
        server_row=SimpleNamespace(server_key="lark"), transport="builtin", module=lark,
    )
    result = await mcp_builtin._build_handler("lark", "list_chats")(
        entity_id="entity-lark", user_id="owner-lark", page_size=7,
        integration_account_id=row.id, integration_account_selection="default",
        workspace_id="workspace-lark", conversation_id="conversation-lark",
        _runtime_mcp_dispatch_context=context,
    )

    assert check.await_args.kwargs["integration_account_id"] == row.id
    assert all(request.url.host == "open.feishu.cn" for request in requests)
    assert len(requests) == 2
    assert "dispatch-test-secret" not in result
    assert "selected-tenant-token" not in result
    parsed = json.loads(result)
    if status == 403:
        assert parsed["error"] == "tool_error"
        assert "permission denied" in parsed["detail"]
        assert parsed["details"]["isError"] is True
    else:
        assert parsed == {"items": [{"chat_id": "oc_selected"}], "has_more": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", sorted(_WRITE_ACTIONS))
async def test_lark_writes_reject_all_account_execution_before_provider_io(action):
    call = AsyncMock(side_effect=AssertionError("mutations must select one account"))
    context = mcp_builtin._MCPResolvedDispatchContext(
        server_row=SimpleNamespace(server_key="lark"), transport="builtin",
        module=SimpleNamespace(call_tool=call),
    )
    result = json.loads(await mcp_builtin._build_handler("lark", action)(
        entity_id="entity-lark", user_id="owner-lark", integration_account_selection="all",
        _runtime_mcp_dispatch_context=context,
    ))
    assert result["error"] == "all_accounts_requires_read_only_tool"
    call.assert_not_awaited()


def _preview_row():
    return SimpleNamespace(
        id="encrypted-feishu-account", entity_id="entity-lark", owner_user_id="owner-lark",
        provider="lark", status="active", config={}, credentials={},
        credential_ref=None, credential_scheme=None, created_at=None, updated_at=None,
    )


def test_lark_owner_edit_preview_preserves_encrypted_platform_and_masks_secret(monkeypatch):
    from apps.api.routers import integrations as router

    row = _preview_row()
    service = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode()))
    service.store_integration(row, {
        "app_id": "cli_feishu_edit", "app_secret": "encrypted-feishu-secret", "platform": "feishu",
        "private_metadata": "must-not-appear",
    })
    encrypted_ref = row.credential_ref
    assert row.credentials == {}
    lease = Mock(wraps=service.lease_integration)
    monkeypatch.setattr(service, "lease_integration", lease)
    monkeypatch.setattr(router, "get_credential_service", lambda: service)

    result = router._integration_resp(row, requester_user_id="owner-lark").model_dump()

    assert result["credential_preview"] == {
        "app_id": "cli_feishu_edit", "platform": "feishu", "app_secret": "__unchanged__",
    }
    assert "encrypted-feishu-secret" not in json.dumps(result)
    assert "must-not-appear" not in json.dumps(result)
    assert "credentials" not in result
    assert row.credential_ref == encrypted_ref
    assert row.credentials == {}
    assert lease.call_args.kwargs["requester"].kind == "user"
    assert lease.call_args.kwargs["requester"].id == "owner-lark"
    assert lease.call_args.kwargs["reason"] == "lark_integration_edit_preview"


@pytest.mark.parametrize("requester", ["shared-lark-user", None, ""])
def test_lark_shared_or_missing_actor_never_leases_or_previews_credentials(monkeypatch, requester):
    from apps.api.routers import integrations as router

    row = _preview_row()
    # Legacy plaintext must be protected by the same owner-only preview rule.
    row.credentials = {"app_id": "cli_private", "app_secret": "private-secret", "platform": "feishu"}
    get_service = Mock(side_effect=AssertionError("shared use must not read credentials"))
    monkeypatch.setattr(router, "get_credential_service", get_service)

    result = router._integration_resp(row, requester_user_id=requester).model_dump()

    assert result["credential_preview"] == {}
    assert "cli_private" not in json.dumps(result)
    assert "private-secret" not in json.dumps(result)
    get_service.assert_not_called()


def test_lark_edit_preview_failure_keeps_list_available_and_never_echoes_credential_error(monkeypatch, caplog):
    from apps.api.routers import integrations as router

    row = _preview_row()
    service = SimpleNamespace(lease_integration=Mock(side_effect=CredentialError("secret=failed-decryption-secret")))
    monkeypatch.setattr(router, "get_credential_service", lambda: service)

    result = router._integration_resp(row, requester_user_id="owner-lark").model_dump()

    assert result["credential_preview"] == {}
    assert "failed-decryption-secret" not in json.dumps(result)
    assert "Could not load Lark connection settings" in caplog.text
    assert "CredentialError" in caplog.text
    assert "failed-decryption-secret" not in caplog.text


@pytest.mark.parametrize("encrypted", [False, True])
def test_lark_edit_preview_defaults_successfully_loaded_legacy_bundle_to_lark(monkeypatch, encrypted):
    from apps.api.routers import integrations as router

    row = _preview_row()
    service = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode()))
    credentials = {"app_id": "cli_legacy_lark", "app_secret": "legacy-lark-secret"}
    if encrypted:
        service.store_integration(row, credentials)
    else:
        row.credentials = credentials
    monkeypatch.setattr(router, "get_credential_service", lambda: service)

    result = router._integration_resp(row, requester_user_id="owner-lark").model_dump()

    assert result["credential_preview"] == {
        "app_id": "cli_legacy_lark", "platform": "lark", "app_secret": "__unchanged__",
    }
    assert "legacy-lark-secret" not in json.dumps(result)
