"""manor(action='search') searches the Manor platform; search_tools the rest."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from packages.core.ai.runtime import manor_capability_catalog as catalog_module
from packages.core.ai.runtime.capability_catalog import builtin_capability_catalog
from packages.core.ai.runtime.capability_matcher import candidate_id
from packages.core.ai.runtime.manor_capability_catalog import (
    WORKSPACE_SHORTCUT_ACTIONS,
    manor_capability_catalog,
    runtime_manor_search,
)


def _ids(entries) -> set[str]:
    return {candidate_id(item) for item in entries}


def test_workspace_actions_and_operations_exist_only_inside_a_workspace():
    outside = _ids(manor_capability_catalog(in_workspace=False))
    inside = _ids(manor_capability_catalog(in_workspace=True))
    assert not any(key.startswith(("workspace_action:", "workspace_operation:")) for key in outside)
    assert {"workspace_operation:rule.add", "workspace_operation:app_view.update",
            "workspace_action:create_task"} <= inside
    # Inside a Workspace the operation replaces the Manor shortcut that wraps it.
    assert "manor_action:update_workspace_rules" in outside
    assert not {f"manor_action:{name}" for name in WORKSPACE_SHORTCUT_ACTIONS} & inside
    assert "manor_action:start_workspace_draft" in outside and "manor_action:start_workspace_draft" in inside
    # Discovery itself and the gateway are not capabilities of their own.
    assert not {"manor_action:search", "manor_action:workspace"} & (outside | inside)


def test_search_tools_leaves_the_manor_platform_to_manor():
    manor_view = _ids(builtin_capability_catalog(manor_in_scope=True))
    other_view = _ids(builtin_capability_catalog(manor_in_scope=False))
    assert not any(key.startswith("manor_action:") for key in manor_view)
    assert "provider:manor_mcp_calendar" not in manor_view and "provider:manor_mcp_calendar" in other_view
    # Integrations that are separate Manor products stay with search_tools.
    assert {"provider:manor_pms", "provider:manor_mcp_minutes"} <= manor_view
    # A step inside the Chrome upload flow is never searched on its own.
    assert "provider:chrome_knowledge_local" not in manor_view | other_view


@pytest.fixture
def usable(monkeypatch):
    """The actor's usable Manor MCP servers (no database)."""
    state = {"providers": frozenset({"manor_mcp_calendar"})}

    @asynccontextmanager
    async def no_db():
        yield None

    async def resolve(db, *, user_id, entity_id, provider_keys):
        return state["providers"] & frozenset(provider_keys)

    monkeypatch.setattr("packages.core.database.async_session", no_db)
    monkeypatch.setattr(
        "packages.core.services.agent_permission_service.resolve_usable_mcp_providers", resolve,
    )
    return state


async def _search(**arguments):
    return json.loads(await runtime_manor_search(entity_id="e1", user_id="u1", **arguments))


@pytest.mark.asyncio
async def test_manor_search_finds_platform_capabilities_with_how_to_call_them(usable):
    result = await _search(workspace_id=None, query="create a 30 minute booking link")
    first = result["matches"][0]
    assert first["id"] == "manor_mcp:manor_mcp_calendar.create_booking_link" and first["available"]
    assert first["call"] == {"tool": "call_tool", "arguments": {
        "name": "mcp__manor_mcp_calendar__create_booking_link", "arguments": {}}}
    # Available Manor MCP tools load like search_tools results.
    assert "mcp__manor_mcp_calendar__create_booking_link" in result["loaded_tools"]
    assert "scope" in result  # outside a Workspace

    inside = await _search(workspace_id="w1", query="let agents act without asking under 20 dollars")
    rule = inside["matches"][0]
    assert rule["id"] == "workspace_operation:governance_policy.update"
    assert rule["call"]["arguments"]["params"]["action"] == "operation"
    assert rule["call"]["arguments"]["params"]["params"]["patches"][0]["op"] == "governance_policy.update"
    assert "apply_draft" in rule["then"]


@pytest.mark.asyncio
async def test_manor_search_hides_admin_and_explains_what_a_server_needs(usable):
    admin = await _search(workspace_id=None, query="list platform tenants audit log")
    assert not any(match["id"].startswith("manor_mcp:manor_mcp_admin.") for match in admin["matches"])



@pytest.mark.asyncio
async def test_manor_search_takes_english_queries_one_per_step(usable):
    assert (await _search(workspace_id=None, query="创建预约链接"))["error"] == "query_not_english"
    result = await _search(workspace_id="w1", queries=["add workspace rule refunds need approval",
                                                       "change the app main button text"])
    by_query: dict[str, str] = {}
    for match in result["matches"]:
        by_query.setdefault(match.get("for_query"), match["id"])  # each step's best match
    assert by_query["add workspace rule refunds need approval"] == "workspace_operation:rule.add"
    assert by_query["change the app main button text"] == "workspace_operation:app_view.update"
    nothing = await _search(workspace_id=None, query="xylophone quantum")
    assert nothing["matches"] == [] and "search_tools" in nothing["hint"]


def test_catalog_entries_are_unique_and_callable():
    for in_workspace in (False, True):
        entries = manor_capability_catalog(in_workspace=in_workspace)
        ids = [candidate_id(item) for item in entries]
        assert len(ids) == len(set(ids))
        assert all(item["invoke_with"]["tool"] in {"manor", "call_tool"} for item in entries)
    assert catalog_module.MANOR_PLATFORM_MCP_PROVIDERS == ("manor_mcp_calendar", "manor_mcp_admin", "knowledge_local")
