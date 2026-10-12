from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.mcp import apify
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect


@pytest.fixture(autouse=True)
def _context():
    apify.set_call_context({
        "entity_id": "entity", "user_id": "user", "workspace_id": "workspace",
        "credential_scope": "user", "integration_account_id": "account",
        "agent_id": "agent", "conversation_id": "conversation",
    })
    yield
    apify.clear_call_context()


def _payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_official_actor_tools_cannot_bypass_the_collection_gateway():
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.services.mcp_seed import _MCP_CATALOG
    from packages.core.services.official_remote_mcp import OfficialRemoteMCPProvider

    pool = ToolPool()
    pool.initialize()
    assert set(pool.registered_tool_names(prefix="mcp__apify__", include_undiscoverable=True)) == {
        "mcp__apify__quote_instagram_collection", "mcp__apify__start_instagram_collection",
        "mcp__apify__get_instagram_collection", "mcp__apify__cancel_instagram_collection",
    }
    assert next(row for row in _MCP_CATALOG if row[0] == "apify")[3] == "builtin"
    assert "apify" not in {provider.value for provider in OfficialRemoteMCPProvider}
    result = await apify.call_tool("call-actor", {"actor": "apify/instagram-scraper"}, "secret")
    assert result["isError"]
    assert _payload(result)["code"] == "unknown_tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"mode": "profile", "entity_id": "someone-else"},
    {"mode": "search", "credential_scope": "platform"},
    {"mode": "search", "actor_id": "arbitrary/actor"},
    {"mode": "search", "results_limit": True},
    {"mode": "search", "max_cost_usd": 1.0},
    {"mode": "profile", "lookback_days": 365},
])
async def test_model_arguments_cannot_supply_scope_or_unbounded_operation(arguments):
    result = await apify.call_tool("quote_instagram_collection", arguments, "secret")
    assert result["isError"]
    assert _payload(result)["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_requires_dispatcher_context():
    apify.clear_call_context()
    result = await apify.call_tool("start_instagram_collection", {"quote_id": "quote"}, "secret")
    assert _payload(result)["code"] == "actor_context_required"


@pytest.mark.asyncio
async def test_invalid_profile_returns_actionable_validation_before_any_database_or_provider_call():
    result = await apify.call_tool("quote_instagram_collection", {
        "mode": "profile", "profile_url": "https://example.com/creator",
    }, "secret")
    assert result["isError"]
    assert _payload(result)["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_quote_separates_budget_from_input_and_propagates_trusted_scope(monkeypatch):
    from packages.core.services import apify_collections

    quote = AsyncMock(return_value={"collection_id": "quote", "max_credits": 650})
    monkeypatch.setattr(apify_collections, "quote_collection", quote)
    result = await apify.call_tool("quote_instagram_collection", {
        "mode": "search", "query": "creator", "max_cost_usd": "0.50",
    }, "secret")
    assert not result["isError"]
    quote.assert_awaited_once_with(
        entity_id="entity", user_id="user", workspace_id="workspace",
        credential_scope="user", account_id="account",
        arguments={"mode": "search", "query": "creator"}, max_cost_usd="0.50",
        agent_id="agent", conversation_id="conversation",
    )
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, service, arguments", [
    ("start_instagram_collection", "start_collection", {"quote_id": "saved"}),
    ("get_instagram_collection", "get_collection", {"collection_id": "saved"}),
    ("cancel_instagram_collection", "cancel_collection", {"collection_id": "saved"}),
])
async def test_existing_operation_dispatch_and_safe_errors(monkeypatch, tool, service, arguments):
    from packages.core.services import apify_collections

    operation = AsyncMock(side_effect=RuntimeError("provider token secret"))
    monkeypatch.setattr(apify_collections, service, operation)
    result = await apify.call_tool(tool, arguments, "secret")
    assert _payload(result)["code"] == "collection_unavailable"
    assert "secret" not in json.dumps(result)
    assert operation.await_args.kwargs["token"] == "secret"
    assert operation.await_args.kwargs["account_id"] == "account"


@pytest.mark.asyncio
async def test_concurrent_calls_keep_actor_context_separate(monkeypatch):
    from packages.core.services import apify_collections

    async def get(**kwargs):
        await asyncio.sleep(0)
        return {"owner": kwargs["user_id"]}

    monkeypatch.setattr(apify_collections, "get_collection", get)

    async def run(user):
        apify.set_call_context({"entity_id": "entity", "user_id": user, "credential_scope": "user"})
        try:
            return _payload(await apify.call_tool("get_instagram_collection", {"collection_id": "saved"}, "secret"))
        finally:
            apify.clear_call_context()

    assert await asyncio.gather(run("one"), run("two")) == [{"owner": "one"}, {"owner": "two"}]


@pytest.mark.parametrize("action, effect", [
    ("quote_instagram_collection", RuntimeToolEffect.READ_ONLY),
    ("get_instagram_collection", RuntimeToolEffect.READ_ONLY),
    ("start_instagram_collection", RuntimeToolEffect.ACTION),
    ("cancel_instagram_collection", RuntimeToolEffect.ACTION),
    ("run_arbitrary_actor", RuntimeToolEffect.ACTION),
])
def test_apify_uses_existing_tool_approval_boundary(action, effect):
    classification = classify_runtime_tool(f"mcp__apify__{action}", {})
    assert classification.effect == effect
