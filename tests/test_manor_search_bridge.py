"""Manor composite tool — search bridge to the deferred MCP tool pool.

Covers the dogfooding failure where the ``manor`` tool's ``action=search``
advertised catalog stubs whose handlers only return "not yet implemented",
and never pointed the model at the deferred MCP tools (loadable via
``search_tools``) that actually do the work.

Three behaviors under test:
  A. Search results (and the no-query department summary) never advertise
     unimplemented stub actions.
  B. ``action=search`` also searches the deferred MCP tool pool and appends
     ``mcp_tool_matches`` (name + description only, capped at 5), gated by
     the acting user's per-provider availability.
  C. Calling an unimplemented catalog action returns an error that includes
     the same bridge suggestions instead of a dead end.

Admin-MCP-specific bridge coverage lives in tests/test_manor_mcp_admin.py
(OSS-excluded); this file only uses generic fake providers so it can ship
in the OSS tree.
"""
from __future__ import annotations

import json

import pytest

from packages.core.ai.tools import manor_tool
from packages.core.ai.tools.manor_tool import (
    _ALL_ACTIONS,
    _IMPLEMENTED_ACTIONS,
    _manor_handler,
    _search_actions,
)

# Catalog entries whose dispatch branch does not exist today — the
# "active trap" set from the staging incident (plus friends found by
# diffing the catalog against the dispatcher).
_KNOWN_STUB_ACTIONS = {
    "get_system_health",
    "list_token_usage",
    "list_users",
    "get_task_health",
    "delete_task",
    "get_agent",
    "list_agent_tools",
    "bind_agent_tool",
    "unbind_agent_tool",
    "mark_notification_read",
}


def _schema(name: str, description: str) -> tuple[str, dict]:
    return (
        name,
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {"arg": {"type": "string"}},
                },
            },
        },
    )


_FAKE_MCP_SCHEMAS = (
    _schema(
        "mcp__google_calendar__list_events",
        "List upcoming events on the user's Google Calendar.",
    ),
    _schema(
        "mcp__google_calendar__create_event",
        "Create a new event on the user's Google Calendar.",
    ),
    _schema(
        "mcp__twitter_x__post_tweet",
        "Post a tweet on X (Twitter).",
    ),
)


def _patch_bridge(
    monkeypatch,
    *,
    schemas=_FAKE_MCP_SCHEMAS,
    usable: frozenset[str] | None = frozenset({"google_calendar"}),
    calls: list | None = None,
    flag_enabled: bool = True,
):
    async def _fake_flag(*, entity_id, user_id):
        return flag_enabled

    monkeypatch.setattr(manor_tool, "_bridge_search_enabled", _fake_flag)
    monkeypatch.setattr(manor_tool, "_registered_tool_schemas", lambda: tuple(schemas))

    async def _fake_usable(*, entity_id, user_id, provider_keys):
        if calls is not None:
            calls.append(
                {
                    "entity_id": entity_id,
                    "user_id": user_id,
                    "provider_keys": tuple(provider_keys),
                }
            )
        return usable if usable is not None else frozenset()

    monkeypatch.setattr(manor_tool, "_usable_mcp_providers", _fake_usable)


# ── Part A — stop advertising stubs ─────────────────────────────────────────


def test_implemented_actions_derived_from_dispatcher():
    assert {
        "list_tasks",
        "create_task",
        "get_dashboard_summary",
        "list_staff",
        "send_email",
        "list_documents",
        "list_ready_integrations",
    } <= _IMPLEMENTED_ACTIONS
    assert _IMPLEMENTED_ACTIONS.isdisjoint(_KNOWN_STUB_ACTIONS)


def test_search_never_returns_unimplemented_stub_actions():
    # Sweep every catalog action name as a query — the strongest bait for
    # each stub is its own name — and assert only implemented actions come
    # back.
    for action_name in _ALL_ACTIONS:
        for match in _search_actions(action_name.replace("_", " "), max_results=8):
            assert match["action"] in _IMPLEMENTED_ACTIONS, (
                f"search advertised unimplemented stub '{match['action']}' "
                f"for query '{action_name}'"
            )


@pytest.mark.asyncio
async def test_department_summary_excludes_stub_actions(monkeypatch):
    _patch_bridge(monkeypatch, schemas=())
    result = json.loads(
        await _manor_handler(entity_id="ent_1", user_id="user_1", action="search")
    )
    listed = {name for names in result["departments"].values() for name in names}
    assert listed.isdisjoint(_KNOWN_STUB_ACTIONS)
    assert result["total_actions"] == len(listed)


# ── Part B — bridge search into the deferred MCP pool ───────────────────────


@pytest.mark.asyncio
async def test_search_appends_gated_mcp_tool_matches(monkeypatch):
    calls: list = []
    _patch_bridge(monkeypatch, calls=calls)

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="search",
            query="calendar events",
        )
    )

    matches = result["mcp_tool_matches"]
    assert matches
    for match in matches:
        assert set(match) == {"name", "description"}
        assert match["name"].startswith("mcp__google_calendar__")
    assert "search_tools" in result["mcp_hint"]
    assert "select:" in result["mcp_hint"]

    # Availability was resolved for the acting user over the pool's providers.
    assert calls and calls[0]["entity_id"] == "ent_1"
    assert calls[0]["user_id"] == "user_1"
    assert set(calls[0]["provider_keys"]) == {"google_calendar", "twitter_x"}


@pytest.mark.asyncio
async def test_search_bridge_excludes_unusable_providers(monkeypatch):
    _patch_bridge(monkeypatch, usable=frozenset())

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="search",
            query="calendar events",
        )
    )

    assert "mcp_tool_matches" not in result


@pytest.mark.asyncio
async def test_search_bridge_requires_user_context(monkeypatch):
    calls: list = []
    _patch_bridge(monkeypatch, calls=calls)

    result = json.loads(
        await _manor_handler(entity_id="ent_1", action="search", query="calendar events")
    )

    assert "mcp_tool_matches" not in result
    assert calls == []  # availability check never consulted without a user


@pytest.mark.asyncio
async def test_search_bridge_caps_matches_at_five(monkeypatch):
    schemas = tuple(
        _schema(
            f"mcp__provider_{i}__sync_widget_records",
            "Sync widget records from the upstream system.",
        )
        for i in range(8)
    )
    _patch_bridge(
        monkeypatch,
        schemas=schemas,
        usable=frozenset({f"provider_{i}" for i in range(8)}),
    )

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="search",
            query="sync widget records",
        )
    )

    assert 1 <= len(result["mcp_tool_matches"]) <= 5


@pytest.mark.asyncio
async def test_search_bridge_skipped_when_flag_off(monkeypatch):
    calls: list = []
    _patch_bridge(monkeypatch, calls=calls, flag_enabled=False)

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="search",
            query="calendar events",
        )
    )

    assert "mcp_tool_matches" not in result
    # The per-provider availability sweep (the expensive part) never runs.
    assert calls == []
    # Part A stays unconditional: stub hiding doesn't depend on the flag.
    listed = {m["action"] for m in result["matches"]}
    assert listed <= _IMPLEMENTED_ACTIONS


@pytest.mark.asyncio
async def test_unimplemented_action_flag_off_keeps_search_fallback_hint(monkeypatch):
    _patch_bridge(monkeypatch, flag_enabled=False)

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="get_task_health",
        )
    )

    # Part C wording stays improved flag-off — just without MCP matches.
    assert "not implemented" in result["error"]
    assert "mcp_tool_matches" not in result
    assert "search" in result["hint"]


@pytest.mark.asyncio
async def test_search_bridge_failure_degrades_to_action_matches(monkeypatch):
    async def _flag_on(*, entity_id, user_id):
        return True

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(manor_tool, "_bridge_search_enabled", _flag_on)
    monkeypatch.setattr(manor_tool, "_registered_tool_schemas", _boom)

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="search",
            query="tasks",
        )
    )

    assert "mcp_tool_matches" not in result
    assert any(m["action"] == "list_tasks" for m in result["matches"])


# ── Part C — the not-implemented error is no longer a dead end ──────────────


@pytest.mark.asyncio
async def test_unimplemented_action_error_includes_bridge_suggestions(monkeypatch):
    schemas = (
        _schema(
            "mcp__ops_reporting__get_task_health_report",
            "Get the task health / SLA report for the workspace.",
        ),
    )
    _patch_bridge(monkeypatch, schemas=schemas, usable=frozenset({"ops_reporting"}))

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="get_task_health",
        )
    )

    assert "not implemented" in result["error"]
    names = [m["name"] for m in result["mcp_tool_matches"]]
    assert "mcp__ops_reporting__get_task_health_report" in names
    assert "search_tools" in result["hint"]
    assert "select:" in result["hint"]


@pytest.mark.asyncio
async def test_unimplemented_action_error_without_matches_stays_helpful(monkeypatch):
    _patch_bridge(monkeypatch, schemas=())

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="get_task_health",
        )
    )

    assert "not implemented" in result["error"]
    assert "mcp_tool_matches" not in result
    assert "search" in result["hint"]


@pytest.mark.asyncio
async def test_unknown_action_suggestions_only_include_implemented(monkeypatch):
    _patch_bridge(monkeypatch, schemas=())

    result = json.loads(
        await _manor_handler(
            entity_id="ent_1",
            user_id="user_1",
            action="totally_bogus_action",
        )
    )

    assert "Unknown action" in result["error"]
    assert set(result["suggestions"]) <= _IMPLEMENTED_ACTIONS
