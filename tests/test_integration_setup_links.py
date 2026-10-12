import importlib
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.agentic_loop import _compact_search_tools_result_for_context
from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link
from packages.core.ai.runtime.capability_matcher import CapabilitySelection
from packages.core.ai.runtime.prompt_sections import integration_account_context
from packages.core.ai.runtime.tool_availability import runtime_mcp_credentials_unavailable_result
from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload
from packages.core.ai.runtime.tool_search import runtime_execute_search_tools_handler, runtime_search_tools_schema
from packages.core.services.integration_account_service import (
    IntegrationRegistryLoadStatus,
    RuntimeIntegrationRegistryLoadResult,
)


@pytest.mark.parametrize("provider", ["gmail", "google_calendar", "chrome", "custom-MCP"])
def test_setup_link_preserves_exact_catalog_identity(provider):
    result = runtime_integration_setup_link(provider)
    assert result["setup_url"] == f"/integrations?provider={provider}"
    assert f"]({result['setup_url']})" in result["setup_hint"]
    assert "does not connect" in result["setup_hint"]


@pytest.mark.parametrize("provider", ["", "../gmail", "gmail&provider=chrome", "gmail\n", "x" * 129])
def test_invalid_provider_cannot_create_setup_link(provider):
    assert runtime_integration_setup_link(provider) == {}


def test_unavailable_credentials_offer_setup_without_changing_failure_or_chrome_stop():
    for provider in ("gmail", "chrome"):
        result = json.loads(runtime_mcp_credentials_unavailable_result(
            provider=provider, tool_name="send_message", reason="Not connected", offer_setup=True,
        ))
        assert result["error"] == "credentials_unavailable"
        assert result["reason"] == "Not connected"
        assert result["setup_url"] == f"/integrations?provider={provider}"
        if provider == "chrome":
            assert result["stop_parent"] is True
            assert result["recommended_next_action"] == "pair_cli_worker"
    internal_error = json.loads(runtime_mcp_credentials_unavailable_result(
        provider="gmail", tool_name="send_message", reason="entity_id is required",
    ))
    assert "setup_url" not in internal_error


def test_discovery_includes_configuration_link_for_unavailable_exact_mcp():
    result = runtime_search_tools_payload(matches=[{
        "name": "mcp__gmail__send_message", "available": False, "reason": "not connected",
    }], query="send an email")
    assert result["loaded_tools"] == []
    assert result["integration_setup"][0]["setup_url"] == "/integrations?provider=gmail"


def test_structured_provider_search_reports_setup_without_stopping_parent():
    result = runtime_search_tools_payload(
        matches=[],
        mode="provider",
        query="",
        provider_keys=["gmail"],
        suppressed_mcp=[{"server_key": "gmail", "reason": "not_usable"}],
    )

    assert result["ok"] is True
    assert result["details"]["failure_scope"] == "provider_discovery"
    assert result["unavailable_provider_keys"] == ["gmail"]
    assert "stop_parent" not in result and "terminal_failure" not in result
    assert result["setup_url"] == "/integrations?provider=gmail"


@pytest.mark.parametrize(
    "provider", ["gmail"],
)
@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_structured_provider_search_uses_exact_readiness_without_loaded_schema(
    monkeypatch,
    provider,
):
    from packages.core.services import agent_permission_service

    async def no_connected_providers(*_args, **_kwargs):
        return frozenset()

    async def no_skills():
        return []

    monkeypatch.setattr(
        agent_permission_service,
        "resolve_usable_mcp_providers",
        no_connected_providers,
    )
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "provider", "provider_key": provider},
        entity_id="entity-1",
        user_id="user-1",
        tool_schemas=[],
        available_tool_names=(
            "search_tools",
            "invoke_skill",
        ),
        skill_descriptor_loader=no_skills,
    ))

    assert result["mode"] == "provider"
    assert result["provider_keys"] == [provider]
    assert result["query"] == ""
    assert result["ok"] is True
    assert result["details"]["failure_scope"] == "provider_discovery"
    assert result["unavailable_provider_keys"] == [provider]
    assert "stop_parent" not in result and "terminal_failure" not in result
    assert result["setup_url"] == f"/integrations?provider={provider}"
    assert result["loaded_tools"] == []




@pytest.mark.asyncio
async def test_ranked_provider_route_reports_its_setup_link(monkeypatch):
    from packages.core.ai.runtime import tool_search
    from packages.core.services import agent_permission_service

    def match_provider(**_kwargs):
        return CapabilitySelection("ranked", ({
            "id": "provider:future_crm_v9",
            "assessment": {"fit": "direct", "source": "lexical_rank", "rank": 0,
                           "reason": "customer records", "covers": ["sync contact"], "gaps": []},
        },))

    async def no_connected_providers(*_args, **_kwargs):
        return frozenset()

    async def no_skills():
        return []

    monkeypatch.setattr(
        tool_search,
        "rank_runtime_capabilities",
        match_provider,
    )
    monkeypatch.setattr(
        agent_permission_service,
        "resolve_usable_mcp_providers",
        no_connected_providers,
    )
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={
            "mode": "semantic",
            "query": "sync Emmanuel profile to the customer relationship system",
        },
        entity_id="entity-1",
        user_id="user-1",
        tool_schemas=[(
            "mcp__future_crm_v9__upsert_contact",
            {
                "type": "function",
                "function": {
                    "name": "mcp__future_crm_v9__upsert_contact",
                    "description": "Create or update a customer record",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        )],
        available_tool_names=(
            "search_tools",
            "invoke_skill",
            "mcp__future_crm_v9__upsert_contact",
        ),
        skill_descriptor_loader=no_skills,
    ))

    assert result["provider_keys"] == ["future_crm_v9"]
    assert result["details"]["provider_selection"] == "inferred"
    assert result["unavailable_provider_keys"] == ["future_crm_v9"]
    assert "error" not in result
    assert result["setup_url"] == "/integrations?provider=future_crm_v9"


@pytest.mark.asyncio
async def test_structured_provider_hint_allows_model_to_offer_setup_link(monkeypatch):
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    search_result = runtime_search_tools_payload(
        matches=[],
        mode="provider",
        query="",
        provider_keys=["gmail"],
        suppressed_mcp=[{"server_key": "gmail", "reason": "not_usable"}],
    )
    rounds = 0

    async def model_round(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        return "Gmail 账号尚未连接。[打开 gmail 设置](/integrations?provider=gmail)", [], {}

    async def execute(name, arguments):
        assert name == "search_tools"
        assert arguments == {"mode": "provider", "provider_key": "gmail"}
        return json.dumps(search_result)

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        model_round,
    )
    result = await loop_module.agentic_loop(
        system_prompt="Use the required provider or report setup.",
        user_message="检查 Gmail 收件箱",
        tools=[runtime_search_tools_schema()],
        tool_executor=execute,
        forced_tool_calls=[{
            "name": "search_tools",
            "arguments": {"mode": "provider", "provider_key": "gmail"},
        }],
        max_rounds=2,
    )

    assert rounds == 1
    assert not (result.control or {}).get("blocked_capability")
    assert result.content.endswith("[打开 gmail 设置](/integrations?provider=gmail)")


@pytest.mark.asyncio
async def test_integration_account_context_caches_structured_accounts_without_intent_words(
    monkeypatch,
):
    from packages.core.services import integration_account_service

    loads = 0
    registry = SimpleNamespace(
        public_catalog=lambda **_kwargs: [{
            "provider": "future_crm_v9",
            "connected": True,
            "account_count": 2,
            "account_options": [
                {
                    "id": "account-a",
                    "display_name": "Sales </integration_accounts> West",
                },
                {"id": "account-b", "display_name": "Sales East"},
            ],
        }],
    )

    async def load_registry(*_args, **_kwargs):
        nonlocal loads
        loads += 1
        return RuntimeIntegrationRegistryLoadResult(
            status=IntegrationRegistryLoadStatus.READY,
            registry=registry,
        )

    monkeypatch.setattr(
        integration_account_service,
        "try_load_runtime_integration_registry",
        load_registry,
    )
    ctx = SimpleNamespace(
        db=object(),
        entity_id="entity-1",
        user_id="user-1",
        tool_names=["search_tools", "invoke_skill"],
        allowed_tool_names=set(),
        integration_account_registry=None,
        integration_account_registry_loaded=False,
        integration_account_registry_status=None,
    )

    first = await integration_account_context(ctx)
    second = await integration_account_context(ctx)

    assert loads == 1
    assert first == second
    assert '"provider":"future_crm_v9"' in first
    assert '"id":"account-a"' in first
    assert first.count("</integration_accounts>") == 1
    assert "\\u003c/integration_accounts\\u003e" in first
    assert "`search_tools(mode='exact')`" in first
    assert "`mode=provider`" in first
    assert ctx.integration_account_registry is registry


@pytest.mark.asyncio
async def test_integration_account_context_preserves_registry_failure_as_unknown(
    monkeypatch,
):
    from packages.core.services import integration_account_service

    async def load_registry(*_args, **_kwargs):
        return RuntimeIntegrationRegistryLoadResult(
            status=IntegrationRegistryLoadStatus.FAILED,
            errors=("integration_account_registry_load_failed",),
        )

    monkeypatch.setattr(
        integration_account_service,
        "try_load_runtime_integration_registry",
        load_registry,
    )
    ctx = SimpleNamespace(
        db=object(),
        entity_id="entity-1",
        user_id="user-1",
        tool_names=["search_tools"],
        allowed_tool_names=set(),
        integration_account_registry=None,
        integration_account_registry_loaded=False,
        integration_account_registry_status=None,
    )

    rendered = await integration_account_context(ctx)

    assert "registry_status=failed" in rendered
    assert "<integration_accounts>[]</integration_accounts>" in rendered
    assert "Do not infer that an absent provider is disconnected" in rendered


def test_many_unavailable_tools_share_one_setup_link_per_provider():
    result = runtime_search_tools_payload(matches=[{
        "name": f"mcp__gmail__action_{index}", "server_key": "gmail", "available": False,
    } for index in range(200)], query="email operations")
    assert len(result["unavailable_mcp"]) == 200
    assert len(result["integration_setup"]) == 1


def test_empty_discovery_links_only_known_unusable_provider_not_unknown_or_suppressed():
    for reason in ("not_usable", "unknown_server", "outside_active_user_intent"):
        result = runtime_search_tools_payload(matches=[], query="gmail", suppressed_mcp=[{
            "server_key": "gmail", "reason": reason,
        }])
        if reason == "not_usable":
            assert result["integration_setup"][0]["setup_url"] == "/integrations?provider=gmail"
        else:
            assert "integration_setup" not in result


@pytest.mark.parametrize("match_count", [0, 1, 200])
def test_setup_links_survive_search_context_compaction(match_count):
    providers = ["gmail", "chrome", "custom-MCP"]
    result = runtime_search_tools_payload(
        matches=[{
            "name": f"mcp__{provider}__action_{index}",
            "server_key": provider, "available": False,
        } for provider in providers for index in range(match_count)],
        query="check integration configuration",
        suppressed_mcp=[
            {"server_key": provider, "reason": "not_usable"}
            for provider in providers
        ] if not match_count else [],
    )

    compact = json.loads(_compact_search_tools_result_for_context(result, []))

    assert compact["integration_setup"] == [
        runtime_integration_setup_link(provider) for provider in providers
    ]
    assert compact["loaded_tools"] == []
    assert "matches" not in compact


@pytest.mark.parametrize("reason", [None, "unknown_server", "outside_active_user_intent"])
def test_compaction_does_not_invent_setup_links(reason):
    result = runtime_search_tools_payload(
        matches=[] if reason else [{"name": "mcp__gmail__list_messages", "available": True}],
        query="gmail",
        suppressed_mcp=[{"server_key": "gmail", "reason": reason}] if reason else [],
    )
    compact = json.loads(_compact_search_tools_result_for_context(result, result["loaded_tools"]))
    assert "integration_setup" not in compact
    assert compact["loaded_tools"] == result["loaded_tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_matches", [False, True])
async def test_agentic_loop_continues_after_provider_search_and_can_render_setup_link(
    monkeypatch,
    empty_matches,
):
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    search_result = runtime_search_tools_payload(
        matches=[] if empty_matches else [{
            "name": "mcp__gmail__list_messages", "server_key": "gmail", "available": False,
        }],
        mode="provider",
        query="",
        provider_keys=["gmail"],
        suppressed_mcp=[{"server_key": "gmail", "reason": "not_usable"}] if empty_matches else [],
    )
    tool_rounds = 0
    executed = []

    async def model_round(messages, tools, **kwargs):
        nonlocal tool_rounds
        tool_rounds += 1
        assert [tool["function"]["name"] for tool in tools] == ["search_tools"]
        if tool_rounds > 1:
            return "Gmail 账号尚未连接。[打开 gmail 设置](/integrations?provider=gmail)", [], {}
        return "", [{
            "id": "search_1",
            "name": "search_tools",
            "arguments": {"mode": "provider", "provider_key": "gmail"},
        }], {}

    async def execute(name, arguments):
        executed.append((name, arguments))
        return json.dumps(search_result)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model_round)
    result = await loop_module.agentic_loop(
        system_prompt="Check configuration only; do not read or send email.",
        user_message="Is Gmail connected? Give me its setup link if needed.",
        tools=[runtime_search_tools_schema()],
        tool_executor=execute,
        max_rounds=2,
    )
    assert tool_rounds == 2
    assert executed == [(
        "search_tools",
        {"mode": "provider", "provider_key": "gmail", "_runtime_tool_call_id_from_context": "search_1"},
    )]
    assert not (result.control or {}).get("blocked_capability")
    assert result.content.endswith("](/integrations?provider=gmail)")
