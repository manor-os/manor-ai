from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.manor_actions import runtime_manor_list_integrations
from packages.core.ai.runtime.tool_discovery import (
    runtime_apply_integration_account_options_to_schema,
    runtime_mcp_tool_supports_all_accounts,
)
from packages.core.ai.runtime.tool_availability import (
    runtime_annotate_tool_availability,
    runtime_blocked_mcp_call_result,
    runtime_preflight_mcp_call,
)
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.services.agent_permission_service import can_use_integration
from packages.core.services.integration_account_service import (
    IntegrationAccountScope,
    IntegrationRegistryLoadStatus,
    RuntimeIntegrationBinding,
    RuntimeIntegrationAccountFactory,
    RuntimeIntegrationAccountCallPlanFactory,
    RuntimeIntegrationRegistry,
    load_runtime_integration_registry,
    nango_connection_matches_runtime_scope,
    try_load_runtime_integration_registry,
)


def test_account_call_plan_factory_filters_to_action_supported_accounts() -> None:
    accounts = tuple(
        SimpleNamespace(id=account_id)
        for account_id in ("default", "supported-secondary", "unsupported-third")
    )

    all_plan = RuntimeIntegrationAccountCallPlanFactory.create(
        accounts,
        selection="all",
        allowed_account_ids=("supported-secondary",),
    )
    default_plan = RuntimeIntegrationAccountCallPlanFactory.create(
        accounts,
        selection="default",
        allowed_account_ids=("supported-secondary", "unsupported-third"),
    )

    assert [account.id for account in all_plan.accounts] == ["supported-secondary"]
    assert [account.id for account in default_plan.accounts] == ["supported-secondary"]


class _FakeScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__stripe__retrieve_balance",
        "mcp__stripe__list_invoices",
        "mcp__paypal__list_invoices",
        "mcp__paypal__get_order",
    ],
)
def test_official_remote_read_tools_support_all_accounts(tool_name: str) -> None:
    assert runtime_mcp_tool_supports_all_accounts(tool_name) is True


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__stripe__create_refund",
        "mcp__stripe__create_payment_link",
        "mcp__paypal__create_order",
        "mcp__paypal__pay_invoice",
    ],
)
def test_official_remote_write_tools_reject_all_accounts(tool_name: str) -> None:
    assert runtime_mcp_tool_supports_all_accounts(tool_name) is False


class _FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeDB:
    def __init__(
        self,
        *result_rows: list[object],
        account_preferences: dict | None = None,
    ) -> None:
        self._result_rows = list(result_rows)
        self._account_preferences = account_preferences

    async def execute(self, _query) -> _FakeResult:
        query = str(_query)
        if "resource_grants" in query or "user_memberships" in query:
            return _FakeResult([])
        if "users.preferences" in query:
            return _FakeResult(
                [self._account_preferences]
                if self._account_preferences is not None
                else []
            )
        return _FakeResult(self._result_rows.pop(0))


class _FailingDB:
    async def execute(self, _query) -> _FakeResult:
        raise RuntimeError("database unavailable")


class _FailAfterFirstQueryDB:
    def __init__(self, first_rows: list[object]) -> None:
        self._first_rows = first_rows
        self._calls = 0

    async def execute(self, _query) -> _FakeResult:
        self._calls += 1
        if self._calls == 1:
            return _FakeResult(self._first_rows)
        raise RuntimeError("account registry unavailable")


class _FakeAsyncSessionContext:
    def __init__(self, db: object) -> None:
        self._db = db

    async def __aenter__(self) -> object:
        return self._db

    async def __aexit__(self, *_args) -> None:
        return None


def test_account_aware_tool_schema_keeps_every_account_callable() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "mcp__gmail__list_messages",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    decorated = runtime_apply_integration_account_options_to_schema(
        schema,
        [
            {
                "id": "default-account",
                "display_name": "owner@example.com",
                "scope": "user",
                "is_default": True,
            },
            {
                "id": "secondary-account",
                "display_name": "info@example.com",
                "scope": "user",
                "is_default": False,
            },
        ],
    )

    selector = decorated["function"]["parameters"]["properties"][
        "integration_account_id"
    ]
    assert selector["enum"] == ["default-account", "secondary-account"]
    assert "default only controls" in selector["description"]
    selection = decorated["function"]["parameters"]["properties"][
        "integration_account_selection"
    ]
    assert selection["enum"] == ["default", "all"]
    assert "every listed account" in selection["description"]


def test_mutating_tool_schema_requires_one_explicit_or_default_account() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "mcp__gmail__send_message",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    decorated = runtime_apply_integration_account_options_to_schema(
        schema,
        [{
            "id": "owner-account",
            "display_name": "owner@example.com",
            "scope": "user",
            "is_default": True,
        }],
    )

    properties = decorated["function"]["parameters"]["properties"]
    assert "integration_account_id" in properties
    assert "integration_account_selection" not in properties
    selector_description = properties["integration_account_id"]["description"].lower()
    assert "integration_account_selection" not in selector_description
    assert "across every account" not in selector_description


def test_partial_registry_schema_requires_an_exact_known_account() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "mcp__gmail__list_messages",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    decorated = runtime_apply_integration_account_options_to_schema(
        schema,
        [{
            "id": "healthy-account",
            "display_name": "healthy@example.com",
            "scope": "user",
            "is_default": False,
        }],
        requires_explicit_account=True,
    )

    parameters = decorated["function"]["parameters"]
    assert "integration_account_id" in parameters["required"]
    assert "integration_account_selection" not in parameters["properties"]
    assert "must select" in parameters["properties"][
        "integration_account_id"
    ]["description"]


def test_task_agent_dynamic_schema_preserves_partial_registry_selector(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.task_agent import (
        _load_search_result_tool_schemas,
    )

    schema = {
        "type": "function",
        "function": {
            "name": "mcp__gmail__list_messages",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    monkeypatch.setattr(
        "packages.core.ai.runtime.task_agent.runtime_tool_schema",
        lambda name: schema if name == "mcp__gmail__list_messages" else None,
    )
    tools: list[dict] = []
    loaded_tool_names: set[str] = set()

    _load_search_result_tool_schemas(
        tools=tools,
        loaded_tool_names=loaded_tool_names,
        search_result={
            "loaded_tools": ["mcp__gmail__list_messages"],
            "matches": [{
                "name": "mcp__gmail__list_messages",
                "available": True,
                "requires_explicit_account": True,
                "account_options": [{
                    "id": "healthy-account",
                    "display_name": "healthy@example.com",
                    "scope": "user",
                    "is_default": False,
                }],
            }],
        },
        allowed_tool_names={"mcp__gmail__list_messages"},
    )

    parameters = tools[0]["function"]["parameters"]
    assert parameters["required"] == ["integration_account_id"]
    assert "integration_account_selection" not in parameters["properties"]


def test_task_agent_loads_a_tool_discovered_after_initial_allowlist_resolution(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.task_agent import (
        _load_search_result_tool_schemas,
    )

    dynamic_name = "mcp__stripe__future_vendor_tool"
    dynamic_schema = {
        "type": "function",
        "function": {
            "name": dynamic_name,
            "parameters": {"type": "object", "properties": {}},
        },
    }
    monkeypatch.setattr(
        "packages.core.ai.runtime.task_agent.runtime_tool_schema",
        lambda _name: None,
    )
    tools: list[dict] = []
    loaded_tool_names: set[str] = {"search_tools"}

    _load_search_result_tool_schemas(
        tools=tools,
        loaded_tool_names=loaded_tool_names,
        search_result={
            "loaded_tools": [dynamic_name],
            "matches": [{
                "name": dynamic_name,
                "available": True,
                "schema": dynamic_schema,
            }],
        },
        allowed_tool_names={"search_tools"},
    )

    assert tools == [dynamic_schema]
    assert dynamic_name in loaded_tool_names


@pytest.mark.asyncio
async def test_mutating_all_account_call_is_blocked_before_credentials() -> None:
    payload = json.loads(await runtime_blocked_mcp_call_result(
        "mcp__gmail__send_message",
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        arguments={"integration_account_selection": "all"},
    ))

    assert payload["error"] == "all_accounts_requires_read_only_tool"


@pytest.mark.asyncio
async def test_dynamic_read_all_account_preflight_uses_discovered_effect() -> None:
    result = await runtime_preflight_mcp_call(
        "mcp__stripe__future_read_action",
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        arguments={"integration_account_selection": "all"},
        declared_effect="read",
        allowed_account_ids=(generate_ulid(),),
    )

    assert not result.blocked_result


@pytest.mark.asyncio
async def test_dynamic_read_all_account_preflight_rejects_incompatible_contracts() -> None:
    result = await runtime_preflight_mcp_call(
        "mcp__stripe__future_read_action",
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        arguments={"integration_account_selection": "all"},
        declared_effect="read",
        allowed_account_ids=(generate_ulid(),),
        supports_all_accounts=False,
    )

    payload = json.loads(result.blocked_result or "{}")
    assert payload["error"] == "incompatible_account_contracts"


@pytest.mark.asyncio
async def test_dynamic_partial_registry_preflight_requires_exact_account() -> None:
    account_id = generate_ulid()
    result = await runtime_preflight_mcp_call(
        "mcp__stripe__future_read_action",
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        declared_effect="read",
        allowed_account_ids=(account_id,),
        requires_explicit_account=True,
    )

    payload = json.loads(result.blocked_result or "{}")
    assert payload["error"] == "integration_account_registry_incomplete"
    assert payload["available_account_ids"] == [account_id]


@pytest.mark.asyncio
async def test_dynamic_partial_registry_preflight_rejects_all_accounts() -> None:
    account_id = generate_ulid()
    result = await runtime_preflight_mcp_call(
        "mcp__stripe__future_read_action",
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        arguments={"integration_account_selection": "all"},
        declared_effect="read",
        allowed_account_ids=(account_id,),
        requires_explicit_account=True,
    )

    payload = json.loads(result.blocked_result or "{}")
    assert payload["error"] == "integration_account_registry_incomplete"
    assert payload["available_account_ids"] == [account_id]


@pytest.mark.asyncio
async def test_dynamic_cached_ready_binding_respects_current_partial_registry(
    monkeypatch,
) -> None:
    account_id = generate_ulid()
    registry = RuntimeIntegrationRegistry(
        user_id="user-1",
        entity_id="entity-1",
        integrations=(RuntimeIntegrationBinding(
            provider="stripe",
            accounts=(SimpleNamespace(id=account_id),),
            load_errors=("stripe:oauth_account_load_failed",),
        ),),
        covered_providers=frozenset({"stripe"}),
        load_errors=("stripe:oauth_account_load_failed",),
    )

    async def load_registry(*_args, **_kwargs):
        return registry

    monkeypatch.setattr(
        "packages.core.services.integration_account_service.load_runtime_integration_registry",
        load_registry,
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: _FakeAsyncSessionContext(object()),
    )

    result = await runtime_preflight_mcp_call(
        "mcp__stripe__future_write_action",
        entity_id="entity-1",
        user_id="user-1",
        declared_effect="write",
        allowed_account_ids=(account_id,),
        # Simulate the stale READY binding that triggered the review finding.
        requires_explicit_account=False,
    )

    payload = json.loads(result.blocked_result or "{}")
    assert payload["error"] == "integration_account_registry_incomplete"
    assert payload["registry_status"] == IntegrationRegistryLoadStatus.PARTIAL.value
    assert payload["available_account_ids"] == [account_id]


@pytest.mark.asyncio
async def test_dynamic_preflight_rejects_changed_account_snapshot(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPAccountRegistrySnapshot,
    )

    old_account_id = generate_ulid()
    new_account_id = generate_ulid()
    registry = RuntimeIntegrationRegistry(
        user_id="user-1",
        entity_id="entity-1",
        integrations=(RuntimeIntegrationBinding(
            provider="stripe",
            accounts=(
                SimpleNamespace(id=new_account_id),
                SimpleNamespace(id=old_account_id),
            ),
        ),),
        covered_providers=frozenset({"stripe"}),
    )

    async def load_registry(*_args, **_kwargs):
        return registry

    monkeypatch.setattr(
        "packages.core.services.integration_account_service.load_runtime_integration_registry",
        load_registry,
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: _FakeAsyncSessionContext(object()),
    )

    result = await runtime_preflight_mcp_call(
        "mcp__stripe__list_customers",
        entity_id="entity-1",
        user_id="user-1",
        declared_effect="read",
        allowed_account_ids=(old_account_id,),
        expected_account_registry_snapshot=(
            RuntimeDynamicMCPAccountRegistrySnapshot(
                account_ids=(old_account_id,),
                status=IntegrationRegistryLoadStatus.READY,
                endpoint="https://mcp.stripe.com",
                token_in="header",
            )
        ),
    )

    payload = json.loads(result.blocked_result or "{}")
    assert payload["error"] == "stale_dynamic_mcp_binding"
    assert payload["retryable"] is True


@pytest.mark.asyncio
async def test_dynamic_preflight_rejects_changed_remote_server_snapshot(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPAccountRegistrySnapshot,
    )

    account_id = generate_ulid()
    registry = RuntimeIntegrationRegistry(
        user_id="user-1",
        entity_id="entity-1",
        integrations=(RuntimeIntegrationBinding(
            provider="stripe",
            accounts=(SimpleNamespace(id=account_id),),
        ),),
        covered_providers=frozenset({"stripe"}),
    )

    async def load_registry(*_args, **_kwargs):
        return registry

    class _ServerResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                endpoint="https://replacement.example/mcp",
                default_config={"mcp_token_in": "query"},
                transport="http",
            )

    class _ServerDB:
        async def execute(self, _query):
            return _ServerResult()

    monkeypatch.setattr(
        "packages.core.services.integration_account_service.load_runtime_integration_registry",
        load_registry,
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: _FakeAsyncSessionContext(_ServerDB()),
    )

    result = await runtime_preflight_mcp_call(
        "mcp__stripe__list_customers",
        entity_id="entity-1",
        user_id="user-1",
        declared_effect="read",
        allowed_account_ids=(account_id,),
        expected_account_registry_snapshot=(
            RuntimeDynamicMCPAccountRegistrySnapshot(
                account_ids=(account_id,),
                status=IntegrationRegistryLoadStatus.READY,
                endpoint="https://mcp.stripe.com",
                token_in="header",
            )
        ),
    )

    payload = json.loads(result.blocked_result or "{}")
    assert payload["error"] == "stale_dynamic_mcp_binding"
    assert payload["retryable"] is True


@pytest.mark.asyncio
async def test_list_integrations_exposes_one_fast_account_registry(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    default_id = generate_ulid()
    secondary_id = generate_ulid()
    oauth_rows = [
        OAuthAccount(
            id=default_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="owner@example.com",
            access_token="default-token",
            profile={"email": "owner@example.com", "is_default": True},
        ),
        OAuthAccount(
            id=secondary_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="info@example.com",
            access_token="secondary-token",
            profile={"email": "info@example.com", "is_default": False},
        ),
    ]
    custom_id = generate_ulid()
    entity_rows = [
        Integration(
            id=custom_id,
            entity_id=entity_id,
            owner_user_id=user_id,
            provider="custom_crm",
            status="active",
            config={"name": "Custom CRM", "is_default": True},
            credentials={"api_key": "custom-token"},
        ),
    ]
    db = _FakeDB(
        [SimpleNamespace(server_key="gmail", name="Gmail", auth_type="oauth")],
        oauth_rows,
        entity_rows,
    )

    async def empty_inventory(*_args, **_kwargs) -> dict[str, list]:
        return {"integrations": [], "channels": []}

    monkeypatch.setattr(
        "packages.core.services.integration_service.get_integration_inventory",
        empty_inventory,
    )
    payload = json.loads(await runtime_manor_list_integrations(
        db,
        entity_id=entity_id,
        user_id=user_id,
    ))

    account_registry = {
        item["provider"]: item for item in payload["account_registry"]
    }
    assert account_registry["custom_crm"] == {
        "provider": "custom_crm",
        "default_account_id": custom_id,
        "account_count": 1,
        "account_options": [{
            "id": custom_id,
            "display_name": "Custom CRM",
            "kind": "integration",
            "scope": "entity",
            "ownership": "mine",
            "is_default": True,
        }],
    }
    assert account_registry["gmail"] == {
        "provider": "gmail",
        "default_account_id": default_id,
        "account_count": 2,
        "account_options": [
            {
                "id": default_id,
                "display_name": "owner@example.com",
                "kind": "oauth_account",
                "scope": "user",
                "ownership": "mine",
                "is_default": True,
            },
            {
                "id": secondary_id,
                "display_name": "info@example.com",
                "kind": "oauth_account",
                "scope": "user",
                "ownership": "mine",
                "is_default": False,
            },
        ],
    }
    assert payload["account_registry_status"] == "ready"
    assert payload["account_registry_errors"] == []


@pytest.mark.asyncio
async def test_ready_integrations_excludes_connected_provider_without_call_path(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    gmail_id = generate_ulid()
    custom_id = generate_ulid()
    db = _FakeDB(
        [SimpleNamespace(server_key="gmail", name="Gmail", auth_type="oauth")],
        [OAuthAccount(
            id=gmail_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="owner@example.com",
            access_token="gmail-token",
        )],
        [Integration(
            id=custom_id,
            entity_id=entity_id,
            owner_user_id=user_id,
            provider="custom_crm",
            status="active",
            config={"name": "Custom CRM"},
            credentials={"api_key": "custom-token"},
        )],
    )

    async def inventory_with_credential_only_provider(
        *_args,
        **_kwargs,
    ) -> dict[str, list]:
        return {
            "integrations": [{
                "provider": "custom_crm",
                "type": "entity_credential",
                "status": "active",
                "ready": True,
                "has_credentials": True,
            }],
            "channels": [],
        }

    monkeypatch.setattr(
        "packages.core.services.integration_service.get_integration_inventory",
        inventory_with_credential_only_provider,
    )
    payload = json.loads(await runtime_manor_list_integrations(
        db,
        entity_id=entity_id,
        user_id=user_id,
        ready_action=True,
    ))

    assert [item["provider"] for item in payload["account_registry"]] == [
        "gmail",
    ]
    assert payload["configured_integrations"] == []


@pytest.mark.asyncio
async def test_ready_integrations_keeps_provider_with_registered_channel_path(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    email_id = generate_ulid()
    db = _FakeDB(
        [],
        [],
        [Integration(
            id=email_id,
            entity_id=entity_id,
            owner_user_id=user_id,
            provider="email",
            status="active",
            config={"name": "Support inbox"},
            credentials={"username": "support@example.com", "password": "secret"},
        )],
    )

    async def inventory_with_email_channel(*_args, **_kwargs) -> dict[str, list]:
        return {
            "integrations": [{
                "provider": "email",
                "type": "entity_credential",
                "status": "active",
                "ready": True,
                "has_credentials": True,
            }],
            "channels": [{
                "key": "email",
                "name": "Email",
                "ready": True,
                "required_provider": "email",
                "needs_integration": False,
            }],
        }

    monkeypatch.setattr(
        "packages.core.services.integration_service.get_integration_inventory",
        inventory_with_email_channel,
    )
    payload = json.loads(await runtime_manor_list_integrations(
        db,
        entity_id=entity_id,
        user_id=user_id,
        ready_action=True,
    ))

    assert [item["provider"] for item in payload["configured_integrations"]] == [
        "email",
    ]
    assert [item["provider"] for item in payload["account_registry"]] == [
        "email",
    ]


@pytest.mark.asyncio
async def test_list_integrations_fails_closed_when_account_registry_cannot_load(
    monkeypatch,
) -> None:
    async def empty_inventory(*_args, **_kwargs) -> dict[str, list]:
        return {"integrations": [], "channels": []}

    monkeypatch.setattr(
        "packages.core.services.integration_service.get_integration_inventory",
        empty_inventory,
    )
    db = _FailAfterFirstQueryDB([
        SimpleNamespace(server_key="gmail", name="Gmail", auth_type="oauth"),
    ])

    payload = json.loads(await runtime_manor_list_integrations(
        db,
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    ))

    assert payload["account_registry_status"] == "failed"
    assert payload["account_registry"] == []
    assert payload["mcp_servers"][0]["ready"] is False
    assert "could not be loaded" in payload["mcp_servers"][0]["reason"]


@pytest.mark.asyncio
async def test_registry_failure_does_not_disable_first_party_provider(
    monkeypatch,
) -> None:
    async def empty_inventory(*_args, **_kwargs) -> dict[str, list]:
        return {"integrations": [], "channels": []}

    monkeypatch.setattr(
        "packages.core.services.integration_service.get_integration_inventory",
        empty_inventory,
    )
    db = _FailAfterFirstQueryDB([
        SimpleNamespace(
            server_key="manor_mcp_calendar",
            name="Manor Calendar",
            auth_type="internal",
        ),
    ])

    payload = json.loads(await runtime_manor_list_integrations(
        db,
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    ))

    assert payload["account_registry_status"] == "failed"
    assert payload["mcp_servers"][0]["ready"] is True
    assert payload["mcp_servers"][0]["scope"] == "internal"


@pytest.mark.asyncio
async def test_registry_failure_is_isolated_per_auth_strategy_in_tool_search(
    monkeypatch,
) -> None:
    db = _FailAfterFirstQueryDB([
        SimpleNamespace(
            server_key="gmail",
            name="Gmail",
            auth_type="oauth",
            transport="builtin",
            endpoint=None,
        ),
        SimpleNamespace(
            server_key="manor_mcp_calendar",
            name="Manor Calendar",
            auth_type="internal",
            transport="builtin",
            endpoint=None,
        ),
    ])
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: _FakeAsyncSessionContext(db),
    )

    matches = await runtime_annotate_tool_availability(
        [
            {"name": "mcp__manor_mcp_calendar__list_events"},
            {"name": "mcp__gmail__list_messages"},
        ],
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    )
    by_name = {match["name"]: match for match in matches}

    assert by_name["mcp__manor_mcp_calendar__list_events"]["ready"] is True
    assert by_name["mcp__gmail__list_messages"]["ready"] is False


@pytest.mark.asyncio
async def test_partial_registry_keeps_exact_known_account_discoverable(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    omitted_id = generate_ulid()
    healthy_id = generate_ulid()
    oauth_rows = [
        OAuthAccount(
            id=omitted_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="omitted@example.com",
            access_token="omitted-token",
            profile={"email": "omitted@example.com", "is_default": True},
        ),
        OAuthAccount(
            id=healthy_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="healthy@example.com",
            access_token="healthy-token",
            profile={"email": "healthy@example.com"},
        ),
    ]
    original_from_oauth = RuntimeIntegrationAccountFactory.from_oauth

    def partially_load_oauth(row, *, actor_user_id=None, provider=None):
        if row.id == omitted_id:
            raise ValueError("malformed account metadata")
        return original_from_oauth(
            row,
            actor_user_id=actor_user_id,
            provider=provider,
        )

    db = _FakeDB(
        [SimpleNamespace(
            server_key="gmail",
            name="Gmail",
            auth_type="oauth",
            transport="builtin",
            endpoint=None,
        )],
        oauth_rows,
        [],
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: _FakeAsyncSessionContext(db),
    )
    monkeypatch.setattr(
        RuntimeIntegrationAccountFactory,
        "from_oauth",
        partially_load_oauth,
    )

    matches = await runtime_annotate_tool_availability(
        [{"name": "mcp__gmail__list_messages"}],
        entity_id=entity_id,
        user_id=user_id,
    )

    assert matches[0]["ready"] is True
    assert matches[0]["requires_explicit_account"] is True
    assert matches[0]["default_account_id"] is None
    assert [option["id"] for option in matches[0]["account_options"]] == [
        healthy_id,
    ]


@pytest.mark.asyncio
async def test_ready_integrations_keeps_partial_registry_exact_account(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    omitted_id = generate_ulid()
    healthy_id = generate_ulid()
    oauth_rows = [
        OAuthAccount(
            id=omitted_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="omitted@example.com",
            access_token="omitted-token",
            profile={"email": "omitted@example.com", "is_default": True},
        ),
        OAuthAccount(
            id=healthy_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="healthy@example.com",
            access_token="healthy-token",
            profile={"email": "healthy@example.com"},
        ),
    ]
    original_from_oauth = RuntimeIntegrationAccountFactory.from_oauth

    def partially_load_oauth(row, *, actor_user_id=None, provider=None):
        if row.id == omitted_id:
            raise ValueError("malformed account metadata")
        return original_from_oauth(
            row,
            actor_user_id=actor_user_id,
            provider=provider,
        )

    async def empty_inventory(*_args, **_kwargs) -> dict[str, list]:
        return {"integrations": [], "channels": []}

    monkeypatch.setattr(
        "packages.core.services.integration_service.get_integration_inventory",
        empty_inventory,
    )
    monkeypatch.setattr(
        RuntimeIntegrationAccountFactory,
        "from_oauth",
        partially_load_oauth,
    )
    db = _FakeDB(
        [SimpleNamespace(server_key="gmail", name="Gmail", auth_type="oauth")],
        oauth_rows,
        [],
    )

    payload = json.loads(await runtime_manor_list_integrations(
        db,
        entity_id=entity_id,
        user_id=user_id,
        ready_action=True,
    ))

    assert payload["mcp_servers"][0]["ready"] is True
    assert payload["mcp_servers"][0]["requires_explicit_account"] is True
    assert payload["mcp_servers"][0]["default_account_id"] is None
    assert payload["account_registry"] == [{
        "provider": "gmail",
        "default_account_id": None,
        "requires_explicit_account": True,
        "account_count": 1,
        "account_options": [{
            "id": healthy_id,
            "display_name": "healthy@example.com",
            "kind": "oauth_account",
            "scope": "user",
            "ownership": "mine",
            "is_default": False,
        }],
    }]


@pytest.mark.asyncio
async def test_full_registry_missing_provider_is_authoritative_empty() -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    registry = RuntimeIntegrationRegistry(
        user_id=user_id,
        entity_id=entity_id,
        integrations=(),
        covered_providers=None,
    )

    decision = await can_use_integration(
        _FakeDB(),
        user_id=user_id,
        entity_id=entity_id,
        provider="gmail",
        integration_registry=registry,
        allow_env_fallback=False,
    )

    assert decision.allowed is False
    assert "No gmail integration is connected" in decision.reason


@pytest.mark.asyncio
async def test_runtime_registry_lists_every_callable_account_with_default_first(
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    default_id = generate_ulid()
    secondary_id = generate_ulid()
    shared_id = generate_ulid()
    github_id = generate_ulid()

    oauth_rows = [
        OAuthAccount(
            id=secondary_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="info@example.com",
            access_token="secondary-token",
            profile={"email": "info@example.com", "is_default": False},
        ),
        OAuthAccount(
            id=default_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="owner@example.com",
            access_token="default-token",
            profile={"email": "owner@example.com", "is_default": True},
        ),
        OAuthAccount(
            id=github_id,
            user_id=user_id,
            provider="github",
            provider_user_id="owner-github",
            access_token="github-token",
            profile={"display_name": "Owner GitHub"},
        ),
    ]
    entity_rows = [
        Integration(
            id=shared_id,
            entity_id=entity_id,
            owner_user_id=user_id,
            provider="gmail",
            status="active",
            config={"name": "Shared support", "is_default": True},
            credentials={"access_token": "shared-token"},
        ),
        Integration(
            id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=user_id,
            provider="slack",
            status="active",
            config={"name": "Not connected"},
            credentials={},
        ),
    ]
    db = _FakeDB(oauth_rows, entity_rows)

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
    )

    gmail = registry.integration("gmail")
    assert gmail is not None
    assert gmail.default_account_id == default_id
    assert [account.id for account in gmail.accounts] == [
        default_id,
        secondary_id,
        shared_id,
    ]
    assert [account.display_name for account in gmail.accounts] == [
        "owner@example.com",
        "info@example.com",
        "Shared support",
    ]
    assert registry.integration("github").default_account_id == github_id

    disconnected = registry.integration("slack")
    assert disconnected is not None
    assert disconnected.connected is False
    assert disconnected.configured_entity_account_count == 1

    catalog = {item["provider"]: item for item in registry.public_catalog()}
    assert catalog["gmail"]["account_count"] == 3
    assert catalog["gmail"]["default_account_id"] == default_id
    assert [option["id"] for option in catalog["gmail"]["account_options"]] == [
        default_id,
        secondary_id,
        shared_id,
    ]


@pytest.mark.asyncio
async def test_runtime_registry_preference_can_select_entity_over_oauth() -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    oauth_id = generate_ulid()
    entity_account_id = generate_ulid()
    db = _FakeDB(
        [OAuthAccount(
            id=oauth_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="oauth@example.test",
            access_token="oauth-token",
            profile={"email": "oauth@example.test", "is_default": True},
        )],
        [Integration(
            id=entity_account_id,
            entity_id=entity_id,
            owner_user_id=user_id,
            provider="gmail",
            status="active",
            config={"name": "Entity Gmail", "is_default": True},
            credentials={"access_token": "entity-token"},
        )],
        account_preferences={
            "integration_account_defaults": {
                entity_id: {
                    "gmail": {
                        "kind": "integration",
                        "account_id": entity_account_id,
                    }
                }
            }
        },
    )

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=["gmail"],
    )

    gmail = registry.integration("gmail")
    assert gmail is not None
    assert gmail.default_account_id == entity_account_id
    assert [account.id for account in gmail.accounts] == [
        entity_account_id,
        oauth_id,
    ]
    assert [account.is_default for account in gmail.accounts] == [True, False]


def test_account_factory_builds_typed_credential_free_values() -> None:
    oauth = OAuthAccount(
        id=generate_ulid(),
        user_id=generate_ulid(),
        provider="gmail",
        provider_user_id="owner@example.com",
        access_token="must-not-escape",
        profile={"email": "owner@example.com", "is_default": True},
    )

    account = RuntimeIntegrationAccountFactory.from_oauth(oauth)

    assert account.scope is IntegrationAccountScope.USER
    assert account.public_option()["scope"] == "user"
    assert not hasattr(account, "oauth_account")
    assert not hasattr(account, "integration")
    assert "must-not-escape" not in json.dumps(account.public_option())


def test_entity_account_factory_never_uses_credential_urls_as_labels() -> None:
    secret_config_url = "https://hooks.example.test/path/config-secret"
    secret_credential_url = "https://hooks.example.test/path/credential-secret"
    integration = Integration(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        provider="webhook",
        status="active",
        config={"url": secret_config_url},
        credentials={"webhook_url": secret_credential_url},
    )

    account = RuntimeIntegrationAccountFactory.from_entity(integration)
    serialized = json.dumps(account.public_option())

    assert account.display_name.startswith("webhook account ")
    assert secret_config_url not in serialized
    assert secret_credential_url not in serialized


@pytest.mark.asyncio
async def test_registry_load_failure_is_typed_and_observable() -> None:
    result = await try_load_runtime_integration_registry(
        _FailingDB(),
        user_id=generate_ulid(),
        entity_id=generate_ulid(),
        provider_keys=["gmail"],
    )

    assert result.status is IntegrationRegistryLoadStatus.FAILED
    assert result.registry is None
    assert result.errors == ("integration_account_registry_load_failed",)


@pytest.mark.asyncio
async def test_registry_retains_healthy_providers_when_one_permission_check_fails(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    gmail_id = generate_ulid()

    async def fail_permission(*_args, **_kwargs) -> bool:
        raise RuntimeError("permission backend unavailable")

    monkeypatch.setattr(
        "packages.core.services.integration_account_service.user_has_permission",
        fail_permission,
    )
    db = _FakeDB(
        [OAuthAccount(
            id=gmail_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="owner@example.com",
            access_token="gmail-token",
        )],
        [Integration(
            id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=user_id,
            provider="stripe",
            status="active",
            config={"name": "Finance"},
            credentials={"secret_key": "stripe-token"},
            required_permission="mcp.stripe.use",
        )],
    )

    result = await try_load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
    )

    assert result.status is IntegrationRegistryLoadStatus.PARTIAL
    assert result.registry is not None
    assert result.registry.integration("gmail").default_account_id == gmail_id
    assert result.registry.accounts_for("stripe") == ()
    assert result.errors == ("stripe:permission_check_failed",)
    decision = await can_use_integration(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider="gmail",
        integration_registry=result.registry,
    )
    assert decision.allowed is True
    incomplete_decision = await can_use_integration(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider="stripe",
        integration_registry=result.registry,
    )
    assert incomplete_decision.allowed is False
    assert "registry is incomplete" in incomplete_decision.reason


@pytest.mark.asyncio
async def test_partial_provider_registry_blocks_implicit_default_but_allows_exact(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    omitted_default_id = generate_ulid()
    healthy_id = generate_ulid()
    oauth_rows = [
        OAuthAccount(
            id=omitted_default_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="default@example.com",
            access_token="default-token",
            profile={"email": "default@example.com", "is_default": True},
        ),
        OAuthAccount(
            id=healthy_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="healthy@example.com",
            access_token="healthy-token",
            profile={"email": "healthy@example.com"},
        ),
    ]
    original_from_oauth = RuntimeIntegrationAccountFactory.from_oauth

    def partially_load_oauth(row, *, actor_user_id=None, provider=None):
        if row.id == omitted_default_id:
            raise ValueError("malformed default account metadata")
        return original_from_oauth(
            row,
            actor_user_id=actor_user_id,
            provider=provider,
        )

    monkeypatch.setattr(
        RuntimeIntegrationAccountFactory,
        "from_oauth",
        partially_load_oauth,
    )
    result = await try_load_runtime_integration_registry(
        _FakeDB(oauth_rows, []),
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=["gmail"],
    )

    assert result.status is IntegrationRegistryLoadStatus.PARTIAL
    assert result.registry is not None
    implicit = await can_use_integration(
        _FakeDB(oauth_rows, []),
        user_id=user_id,
        entity_id=entity_id,
        provider="gmail",
    )
    exact = await can_use_integration(
        _FakeDB(oauth_rows, []),
        user_id=user_id,
        entity_id=entity_id,
        provider="gmail",
        integration_account_id=healthy_id,
    )

    assert implicit.allowed is False
    assert "registry is incomplete" in implicit.reason
    assert exact.allowed is True
    assert exact.account_id == healthy_id


@pytest.mark.asyncio
async def test_runtime_registry_drives_exact_account_selection_without_hiding_siblings(
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    default_id = generate_ulid()
    selected_id = generate_ulid()
    oauth_rows = [
        OAuthAccount(
            id=default_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="default@example.com",
            access_token="default-token",
            profile={"email": "default@example.com", "is_default": True},
        ),
        OAuthAccount(
            id=selected_id,
            user_id=user_id,
            provider="gmail",
            provider_user_id="selected@example.com",
            access_token="selected-token",
            profile={"email": "selected@example.com", "is_default": False},
        ),
    ]
    db = _FakeDB(oauth_rows, [])

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=["gmail"],
    )
    default_decision = await can_use_integration(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider="gmail",
        integration_registry=registry,
    )
    selected_decision = await can_use_integration(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider="gmail",
        integration_account_id=selected_id,
        integration_registry=registry,
    )

    assert default_decision.account_id == default_id
    assert selected_decision.account_id == selected_id
    assert [
        option["id"]
        for option in registry.integration("gmail").public_option()["account_options"]
    ] == [default_id, selected_id]


@pytest.mark.asyncio
async def test_runtime_registry_filters_entity_accounts_without_permission(
    monkeypatch,
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    denied_id = generate_ulid()
    denied_account = Integration(
        id=denied_id,
        entity_id=entity_id,
        owner_user_id=user_id,
        provider="stripe",
        status="active",
        config={"name": "Finance", "is_default": True},
        credentials={"secret_key": "stripe-secret"},
        required_permission="mcp.stripe.use",
    )

    async def deny_permission(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(
        "packages.core.services.integration_account_service.user_has_permission",
        deny_permission,
    )
    db = _FakeDB([], [denied_account])

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=["stripe"],
    )

    stripe = registry.integration("stripe")
    assert stripe is not None
    assert stripe.accounts == ()
    assert stripe.denied_permissions == ("mcp.stripe.use",)
    decision = await can_use_integration(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider="stripe",
        integration_account_id=denied_id,
        integration_registry=registry,
    )
    assert decision.allowed is False
    assert decision.account_id is None


@pytest.mark.asyncio
async def test_runtime_registry_blocks_missing_injected_nango_connection() -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    account_id = generate_ulid()
    account = Integration(
        id=account_id,
        entity_id=entity_id,
        owner_user_id=user_id,
        provider="linkedin",
        status="active",
        config={
            "nango": {
                "connection_id": f"{entity_id}--{user_id}--linkedin--missing",
                "provider_config_key": "linkedin",
            },
        },
        credentials={},
    )

    db = _FakeDB([], [account])

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=["linkedin"],
        live_nango_connection_ids=set(),
    )

    linkedin = registry.integration("linkedin")
    assert linkedin is not None
    assert linkedin.accounts == ()
    assert linkedin.reconnect_required_account_ids == (account_id,)
    decision = await can_use_integration(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider="linkedin",
        integration_account_id=account_id,
        integration_registry=registry,
    )
    assert decision.allowed is False
    assert decision.account_id is None
    assert "no longer available in Nango" in decision.reason


@pytest.mark.asyncio
async def test_runtime_registry_blocks_live_cross_entity_nango_pointer(
) -> None:
    user_id = generate_ulid()
    entity_id = generate_ulid()
    foreign_entity_id = generate_ulid()
    account_id = generate_ulid()
    foreign_connection_id = (
        f"{foreign_entity_id}--{generate_ulid()}--linkedin--{generate_ulid()}"
    )
    account = Integration(
        id=account_id,
        entity_id=entity_id,
        owner_user_id=user_id,
        provider="linkedin",
        status="active",
        config={
            "nango": {
                "connection_id": foreign_connection_id,
                "provider_config_key": "linkedin",
            },
        },
        credentials={},
    )

    db = _FakeDB([], [account])

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=["linkedin"],
    )

    linkedin = registry.integration("linkedin")
    assert linkedin is not None
    assert linkedin.accounts == ()
    assert linkedin.reconnect_required_account_ids == (account_id,)


def test_nango_pointer_scope_binds_entity_owner_and_provider() -> None:
    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    connection_id = (
        f"{entity_id}--{owner_user_id}--linkedin--{generate_ulid()}"
    )

    assert nango_connection_matches_runtime_scope(
        connection_id=connection_id,
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        provider="linkedin",
        provider_config_key="linkedin",
    )
    assert not nango_connection_matches_runtime_scope(
        connection_id=connection_id,
        entity_id=entity_id,
        owner_user_id=generate_ulid(),
        provider="linkedin",
        provider_config_key="linkedin",
    )
    assert not nango_connection_matches_runtime_scope(
        connection_id=connection_id,
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        provider="gmail",
        provider_config_key="gmail",
    )


@pytest.mark.asyncio
async def test_runtime_registry_blocks_live_cross_user_nango_pointer(
) -> None:
    owner_user_id = generate_ulid()
    foreign_user_id = generate_ulid()
    entity_id = generate_ulid()
    account_id = generate_ulid()
    foreign_connection_id = (
        f"{entity_id}--{foreign_user_id}--linkedin--{generate_ulid()}"
    )
    account = Integration(
        id=account_id,
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        provider="linkedin",
        status="active",
        config={
            "nango": {
                "connection_id": foreign_connection_id,
                "provider_config_key": "linkedin",
            },
        },
        credentials={},
    )

    db = _FakeDB([], [account])

    registry = await load_runtime_integration_registry(
        db,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider_keys=["linkedin"],
    )

    linkedin = registry.integration("linkedin")
    assert linkedin is not None
    assert linkedin.accounts == ()
    assert linkedin.reconnect_required_account_ids == (account_id,)
