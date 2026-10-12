"""manor(action='search') ranks the Manor platform's own catalog; search_tools the rest."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.tools import manor_tool
from packages.core.ai.runtime import capability_matcher, tool_registry
from packages.core.ai.runtime.tool_search import runtime_execute_search_tools_handler


def test_only_implemented_actions_have_dispatch():
    assert {"create_task", "list_documents", "create_scheduled_job"} <= manor_tool._IMPLEMENTED_ACTIONS
    assert manor_tool._IMPLEMENTED_ACTIONS.isdisjoint({"get_system_health", "list_users", "get_task_health"})


@pytest.mark.asyncio
async def test_manor_search_ranks_the_platform_catalog_without_a_model(monkeypatch):
    async def no_model(*args, **kwargs):
        raise AssertionError("discovery must not call a model")

    monkeypatch.setattr(capability_matcher, "runtime_execute_text_completion", no_model)
    # Not an alias of search_tools any more: it ranks its own catalog.
    monkeypatch.setattr(tool_registry, "runtime_discover_tools",
                        AsyncMock(side_effect=AssertionError("manor search is not search_tools")))
    payload = json.loads(await manor_tool._manor_handler(action="search", query="Create a daily workspace automation"))
    assert payload["mode"] == "manor"
    first = payload["matches"][0]
    assert first["id"] == "manor_action:create_scheduled_job"
    assert first["call"] == {"tool": "manor", "arguments": {"action": "create_scheduled_job"}}
    # Manor actions are called through manor; only Manor MCP tools load.
    assert all(name.startswith("mcp__") for name in payload["loaded_tools"])
    # The catalog is English: a query in another language is sent back once.
    retry = json.loads(await manor_tool._manor_handler(action="search", query="每天搜索 Instagram 并创建工作区自动化"))
    assert retry["error"] == "query_not_english"


@pytest.mark.asyncio
async def test_discovery_adapter_reuses_registered_handler_and_context(monkeypatch):
    handler = AsyncMock(return_value='{"loaded_tools":[]}')
    monkeypatch.setattr(tool_registry, "runtime_ensure_tool_registry_initialized",
                        lambda: SimpleNamespace(get=lambda name: {"handler": handler} if name == "search_tools" else None))
    envelope = object()
    result = await tool_registry.runtime_discover_tools(
        entity_id="e", user_id="u", arguments={"query": "q", "_runtime_envelope_from_context": envelope},
    )
    assert json.loads(result)["loaded_tools"] == []
    handler.assert_awaited_once_with(entity_id="e", user_id="u", query="q", _runtime_envelope_from_context=envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["get_system_health", "get_task_health", "not_a_real_action"])
@pytest.mark.parametrize("entrypoint", ["public", "dispatch"])
async def test_unknown_or_stub_action_is_diagnostic_not_an_alternate_search(monkeypatch, action, entrypoint):
    discover = AsyncMock(side_effect=AssertionError("Do not run a second implicit provider search"))
    monkeypatch.setattr(tool_registry, "runtime_discover_tools", discover)
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    monkeypatch.setattr("packages.core.database.async_session", session)
    result = (await manor_tool._manor_handler(action=action) if entrypoint == "public"
              else await manor_tool._dispatch_action(action, {}, "e"))
    payload = json.loads(result)
    assert payload["error"] == "unsupported_action"
    assert payload["details"]["capability_availability"] == "not_established"
    assert payload["details"]["action_executed"] is False
    discover.assert_not_awaited()
