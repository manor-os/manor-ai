from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from packages.core.services.integration_operation_catalog import (
    MCPServerKind,
    MCPServerKindFactory,
    NANGO_PROVIDER_CAPABILITIES,
    integration_operation_catalog,
    normalize_mcp_operations,
    nango_provider_capability,
)
from packages.core.services.mcp_account_tool_catalog import (
    MCPAccountToolCatalogFactory,
    actor_mcp_tool_cache,
    actor_provider_mcp_tool_caches,
)


def test_mcp_server_kind_factory_classifies_origin_not_transport_alone():
    assert MCPServerKindFactory.from_server("gmail", "builtin") is MCPServerKind.MANAGED
    assert MCPServerKindFactory.from_server("stripe", "http") is MCPServerKind.MANAGED
    assert MCPServerKindFactory.from_server("paypal", "http") is MCPServerKind.MANAGED
    assert MCPServerKindFactory.from_server("customer-mcp", "http") is MCPServerKind.CUSTOM
    assert MCPServerKindFactory.from_server("customer-cli", "stdio") is MCPServerKind.CUSTOM


def test_normalize_mcp_operations_preserves_schema_and_classifies_effect():
    operations = normalize_mcp_operations(
        "gmail",
        [
            {
                "name": "send_message",
                "description": "Send an email.",
                "inputSchema": {
                    "type": "object",
                    "required": ["to", "body"],
                    "properties": {
                        "to": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
            },
            {
                "name": "delete_draft",
                "parameters": {
                    "type": "object",
                    "required": ["draft_id"],
                    "properties": {"draft_id": {"type": "string"}},
                },
            },
        ],
    )

    by_name = {item["name"]: item for item in operations}
    assert by_name["send_message"] == {
        "name": "send_message",
        "tool_name": "mcp__gmail__send_message",
        "label": "Send message",
        "resource": "Message",
        "description": "Send an email.",
        "effect": "write",
        "input_schema": {
            "type": "object",
            "required": ["to", "body"],
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    }
    assert by_name["delete_draft"]["resource"] == "Draft"
    assert by_name["delete_draft"]["effect"] == "destructive"


def test_normalize_mcp_operations_accepts_openai_function_and_full_tool_name():
    operations = normalize_mcp_operations(
        "github",
        [
            {
                "type": "function",
                "function": {
                    "name": "mcp__github__list_issues",
                    "description": "List repository issues.",
                    "parameters": {
                        "type": "object",
                        "properties": {"repo": {"type": "string"}},
                    },
                },
            },
            {"name": "mcp__github__list_issues"},
        ],
    )

    assert len(operations) == 1
    assert operations[0]["name"] == "list_issues"
    assert operations[0]["resource"] == "Issue"
    assert operations[0]["effect"] == "read"
    assert operations[0]["input_schema"]["required"] == []


def test_normalize_mcp_operations_reuses_known_effects_and_fails_closed_for_unknowns():
    operations = normalize_mcp_operations(
        "paypal",
        [
            {"name": "show_product_details"},
            {"name": "cancel_subscription"},
            {
                "name": "future_paypal_lookup",
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "future_paypal_dangerous_action",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": True,
                },
            },
        ],
    )

    by_name = {item["name"]: item for item in operations}
    assert by_name["show_product_details"]["effect"] == "read"
    assert by_name["cancel_subscription"]["effect"] == "destructive"
    assert by_name["future_paypal_lookup"]["effect"] == "write"
    assert by_name["future_paypal_dangerous_action"]["effect"] == "destructive"


def test_integration_operation_catalog_uses_executable_builtin_surface():
    operations, source = integration_operation_catalog(
        server_key="gmail",
        transport="builtin",
        tools_cached={},
    )

    send_message = next(item for item in operations if item["name"] == "send_message")
    assert source == "builtin"
    assert send_message["input_schema"]["required"] == ["to", "subject", "body"]
    assert "body" in send_message["input_schema"]["properties"]


def test_quickbooks_void_invoice_is_marked_destructive():
    operations, source = integration_operation_catalog(
        server_key="quickbooks",
        transport="builtin",
        tools_cached={},
    )

    void_invoice = next(item for item in operations if item["name"] == "void_invoice")
    assert source == "builtin"
    assert void_invoice["effect"] == "destructive"


def test_integration_operation_catalog_normalizes_remote_cache():
    operations, source = integration_operation_catalog(
        server_key="linear",
        transport="http",
        tools_cached={
            "tools": [
                {
                    "name": "create_issue",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                },
            ],
        },
    )

    assert source == "cache"
    assert operations[0]["name"] == "create_issue"
    assert operations[0]["resource"] == "Issue"


def test_official_unknown_operation_ignores_cached_read_hint() -> None:
    operations, _source = integration_operation_catalog(
        server_key="stripe",
        transport="http",
        tools_cached={
            "tools": [{
                "name": "future_stripe_tool",
                "effect": "read",
                "annotations": {"readOnlyHint": True},
            }],
        },
    )

    assert operations[0]["effect"] == "write"


def test_account_tool_catalog_factory_unions_tools_with_account_provenance():
    default_row = SimpleNamespace(
        id="row-default",
        oauth_account_id="account-default",
        integration_id=None,
        tools_cached={
            "tools": [
                {"name": "shared", "description": "default schema"},
                {"name": "default_only"},
            ]
        },
    )
    secondary_row = SimpleNamespace(
        id="row-secondary",
        oauth_account_id="account-secondary",
        integration_id=None,
        tools_cached={
            "tools": [
                {"name": "shared", "description": "secondary schema"},
                {"name": "secondary_only"},
            ]
        },
    )

    cache = MCPAccountToolCatalogFactory.merge(
        [secondary_row, default_row],
        account_order=["account-default", "account-secondary"],
    )
    by_name = {tool["name"]: tool for tool in cache["tools"]}

    assert cache["source"] == "account_discovery"
    assert by_name["shared"]["description"] == "default schema"
    assert by_name["shared"]["account_ids"] == [
        "account-default",
        "account-secondary",
    ]
    assert by_name["secondary_only"]["account_ids"] == ["account-secondary"]

    partial_cache = MCPAccountToolCatalogFactory.merge(
        [default_row],
        account_order=["account-default"],
        requires_explicit_account=True,
    )
    assert partial_cache["tools"][0]["requires_explicit_account"] is True
    partial_operations = normalize_mcp_operations(
        "stripe",
        partial_cache["tools"],
    )
    assert partial_operations[0]["requires_explicit_account"] is True


def test_account_tool_catalog_factory_preserves_incompatible_account_contracts():
    default_row = SimpleNamespace(
        id="row-default",
        oauth_account_id="account-default",
        integration_id=None,
        tools_cached={
            "tools": [{
                "name": "create_record",
                "inputSchema": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {"title": {"type": "string"}},
                },
                "outputSchema": {
                    "type": "object",
                    "required": ["record_id"],
                    "properties": {"record_id": {"type": "string"}},
                },
            }]
        },
    )
    incompatible_row = SimpleNamespace(
        id="row-secondary",
        oauth_account_id="account-secondary",
        integration_id=None,
        tools_cached={
            "tools": [{
                "name": "create_record",
                "inputSchema": {
                    "type": "object",
                    "required": ["record_id"],
                    "properties": {"record_id": {"type": "string"}},
                },
                "outputSchema": {
                    "type": "object",
                    "required": ["updated"],
                    "properties": {"updated": {"type": "boolean"}},
                },
            }]
        },
    )

    cache = MCPAccountToolCatalogFactory.merge(
        [incompatible_row, default_row],
        account_order=["account-default", "account-secondary"],
    )

    assert cache["tools"] == [{
        "name": "create_record",
        "inputSchema": {
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
        "outputSchema": {
            "type": "object",
            "required": ["record_id"],
            "properties": {"record_id": {"type": "string"}},
        },
        "account_ids": ["account-default", "account-secondary"],
        "account_input_schemas": {
            "account-default": {
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
            },
            "account-secondary": {
                "type": "object",
                "required": ["record_id"],
                "properties": {"record_id": {"type": "string"}},
            },
        },
        "account_output_schemas": {
            "account-default": {
                "type": "object",
                "required": ["record_id"],
                "properties": {"record_id": {"type": "string"}},
            },
            "account-secondary": {
                "type": "object",
                "required": ["updated"],
                "properties": {"updated": {"type": "boolean"}},
            },
        },
        "requires_explicit_account": True,
        "supports_all_accounts": False,
    }]

    operation = normalize_mcp_operations("stripe", cache["tools"])[0]
    assert operation["account_ids"] == ["account-default", "account-secondary"]
    assert operation["account_input_schemas"]["account-secondary"]["required"] == [
        "record_id"
    ]
    assert operation["account_output_schemas"]["account-secondary"]["required"] == [
        "updated"
    ]
    assert operation["supports_all_accounts"] is False
    runtime_schema = MCPAccountToolCatalogFactory.runtime_input_schema(
        cache["tools"][0]["account_input_schemas"],
        account_argument="integration_account_id",
    )
    assert runtime_schema["required"] == ["integration_account_id"]
    assert {
        branch["properties"]["integration_account_id"]["const"]
        for branch in runtime_schema["oneOf"]
    } == {"account-default", "account-secondary"}
    same_input_schema = MCPAccountToolCatalogFactory.runtime_input_schema(
        {
            "account-default": default_row.tools_cached["tools"][0]["inputSchema"],
            "account-secondary": default_row.tools_cached["tools"][0]["inputSchema"],
        },
        account_argument="integration_account_id",
    )
    assert same_input_schema["required"] == ["integration_account_id"]
    assert len(same_input_schema["oneOf"]) == 2
    assert MCPAccountToolCatalogFactory.conservative_effect([
        {"effect": "read"},
        {"effect": "destructive"},
    ]).value == "destructive"


def test_mcp_tool_operation_response_preserves_account_specific_contracts():
    from apps.api.routers.integrations import MCPToolOperation

    payload = MCPToolOperation(
        name="create_record",
        tool_name="mcp__stripe__create_record",
        label="Create record",
        resource="Record",
        effect="write",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        account_input_schemas={"account-2": {"required": ["record_id"]}},
        account_output_schemas={"account-2": {"required": ["updated"]}},
        supports_all_accounts=False,
    ).model_dump()

    assert payload["account_input_schemas"]["account-2"]["required"] == [
        "record_id"
    ]
    assert payload["account_output_schemas"]["account-2"]["required"] == [
        "updated"
    ]
    assert payload["supports_all_accounts"] is False


def test_account_tool_catalog_detects_output_only_contract_conflicts():
    common_input = {
        "type": "object",
        "required": ["record_id"],
        "properties": {"record_id": {"type": "string"}},
    }
    rows = [
        SimpleNamespace(
            id="row-default",
            oauth_account_id="account-default",
            integration_id=None,
            tools_cached={
                "tools": [{
                    "name": "get_record",
                    "inputSchema": common_input,
                    "outputSchema": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                    },
                }]
            },
        ),
        SimpleNamespace(
            id="row-secondary",
            oauth_account_id="account-secondary",
            integration_id=None,
            tools_cached={
                "tools": [{
                    "name": "get_record",
                    "inputSchema": common_input,
                    "outputSchema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                    },
                }]
            },
        ),
    ]

    tool = MCPAccountToolCatalogFactory.merge(
        rows,
        account_order=["account-default", "account-secondary"],
    )["tools"][0]

    assert tool["requires_explicit_account"] is True
    assert tool["supports_all_accounts"] is False
    assert tool["account_output_schemas"]["account-secondary"]["properties"][
        "status"
    ]["type"] == "string"


def test_account_tool_catalog_rejects_stale_or_wrong_endpoint_snapshots():
    now = datetime(2026, 8, 26, tzinfo=UTC)
    fresh = SimpleNamespace(
        endpoint="https://mcp.stripe.com",
        tools_cached_at=now - timedelta(minutes=5),
    )
    stale = SimpleNamespace(
        endpoint="https://mcp.stripe.com",
        tools_cached_at=now - timedelta(days=2),
    )
    wrong_endpoint = SimpleNamespace(
        endpoint="https://old-mcp.stripe.test",
        tools_cached_at=now - timedelta(minutes=5),
    )

    assert MCPAccountToolCatalogFactory.is_usable(
        fresh,
        endpoint="https://mcp.stripe.com",
        now=now,
    )
    assert not MCPAccountToolCatalogFactory.is_usable(
        stale,
        endpoint="https://mcp.stripe.com",
        now=now,
    )
    assert not MCPAccountToolCatalogFactory.is_usable(
        wrong_endpoint,
        endpoint="https://mcp.stripe.com",
        now=now,
    )


@pytest.mark.asyncio
async def test_provider_catalog_merge_is_scoped_through_actor_registry(monkeypatch):
    from packages.core.services import (
        integration_account_service,
        mcp_account_tool_catalog,
    )

    registry = object()
    calls: list[tuple[str, str, object]] = []

    async def load_registry(db, *, user_id, entity_id, provider_keys):
        assert user_id == "user-1"
        assert entity_id == "entity-1"
        assert set(provider_keys) == {"stripe", "paypal"}
        return registry

    async def actor_cache(db, *, provider, registry, endpoint, now=None):
        calls.append((provider, endpoint, registry))
        return {"tools": [{"name": f"{provider}_action"}]}

    monkeypatch.setattr(
        integration_account_service,
        "load_runtime_integration_registry",
        load_registry,
    )
    monkeypatch.setattr(
        mcp_account_tool_catalog,
        "actor_mcp_tool_cache",
        actor_cache,
    )
    caches = await actor_provider_mcp_tool_caches(
        object(),
        provider_endpoints={
            "stripe": "https://mcp.stripe.com",
            "paypal": "https://mcp.paypal.com/http",
        },
        user_id="user-1",
        entity_id="entity-1",
    )

    assert set(caches) == {"stripe", "paypal"}
    assert calls == [
        ("stripe", "https://mcp.stripe.com", registry),
        ("paypal", "https://mcp.paypal.com/http", registry),
    ]


@pytest.mark.asyncio
async def test_actor_catalog_query_never_includes_another_tenant_account():
    from sqlalchemy.dialects import postgresql

    from packages.core.services.integration_account_service import (
        IntegrationAccountKind,
        IntegrationAccountOwnership,
        IntegrationAccountScope,
        RuntimeIntegrationAccount,
        RuntimeIntegrationBinding,
        RuntimeIntegrationRegistry,
    )

    actor_account_id = "01ACTORACCOUNT0000000000000"
    other_tenant_account_id = "01OTHERACCOUNT0000000000000"
    account = RuntimeIntegrationAccount(
        id=actor_account_id,
        provider="stripe",
        kind=IntegrationAccountKind.OAUTH_ACCOUNT,
        scope=IntegrationAccountScope.USER,
        ownership=IntegrationAccountOwnership.MINE,
        owner_user_id="user-a",
        display_name="Actor Stripe",
        is_default=True,
    )
    registry = RuntimeIntegrationRegistry(
        user_id="user-a",
        entity_id="entity-a",
        integrations=(RuntimeIntegrationBinding(provider="stripe", accounts=(account,)),),
        covered_providers=frozenset({"stripe"}),
    )
    row = SimpleNamespace(
        id="catalog-a",
        provider="stripe",
        oauth_account_id=actor_account_id,
        integration_id=None,
        endpoint="https://mcp.stripe.com",
        tools_cached={"tools": [{"name": "list_invoices"}]},
        tools_cached_at=datetime(2026, 8, 26, tzinfo=UTC),
    )

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [row]

    class _DB:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Rows()

    db = _DB()
    cache = await actor_mcp_tool_cache(
        db,
        provider="stripe",
        registry=registry,
        endpoint="https://mcp.stripe.com",
        now=datetime(2026, 8, 26, 1, tzinfo=UTC),
    )

    compiled = str(db.statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert actor_account_id in compiled
    assert other_tenant_account_id not in compiled
    assert [tool["name"] for tool in cache["tools"]] == ["list_invoices"]


def test_builtin_catalog_does_not_fallback_to_stale_cached_tools():
    operations, source = integration_operation_catalog(
        server_key="missing_builtin_provider",
        transport="builtin",
        tools_cached={"tools": [{"name": "stale_tool"}]},
    )

    assert operations == []
    assert source == "unavailable"


def test_nango_provider_capability_declares_mode_and_operations():
    assert set(NANGO_PROVIDER_CAPABILITIES) >= {
        "gmail",
        "google_calendar",
        "google_drive",
        "outlook",
        "twitter",
        "whatsapp",
    }

    gmail = nango_provider_capability("gmail")
    assert gmail == {
        "provider_config_key": "gmail",
        "provider": "gmail",
        "mode": "oauth_saas",
        "operations": {"read": True, "write": True, "inbound": False},
    }
    assert nango_provider_capability("unknown-provider") is None

    twitter = nango_provider_capability("twitter")
    assert twitter == {
        "provider_config_key": "twitter",
        "provider": "twitter",
        "mode": "oauth_saas",
        "operations": {"read": True, "write": True, "inbound": False},
    }

    whatsapp = nango_provider_capability("whatsapp")
    assert whatsapp == {
        "provider_config_key": "whatsapp",
        "provider": "whatsapp",
        "mode": "bidirectional_chat",
        "operations": {"read": True, "write": True, "inbound": True},
    }
