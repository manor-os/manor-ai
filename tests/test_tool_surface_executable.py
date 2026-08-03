"""Platform principle: every agent-visible tool must be directly executable.

An agent-visible tool is anything the model can discover and try to call:

  1. Deferred MCP tool schemas advertised via
     ``mcp_builtin._SERVER_TOOL_SCHEMAS`` — each server key must resolve
     to a registered in-process module and every advertised tool name
     must exist in that module's ``list_tools()``.
  2. ``manor`` composite-tool actions surfaced by ``action=search`` —
     every surfaced action must have a real dispatch branch.
  3. ``code`` composite-tool actions — the catalog and the dispatch
     table must be identical.
  4. Built-in skill packs — every ``mcp__<server>__*`` tool a seeded
     skill declares must belong to a server that can actually dispatch
     (in-process module, or a vendor-hosted remote MCP).

This file iterates over whatever the runtime registries contain, so it
ships in both Cloud and OSS trees without referencing edition-specific
server keys.
"""
from __future__ import annotations

import json
from pathlib import Path

from packages.core.ai.mcp import get_module
from packages.core.ai.tools.mcp_builtin import _SERVER_TOOL_SCHEMAS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_ROOT = _REPO_ROOT / "packages" / "core" / "ai" / "skills"

# Servers that intentionally advertise an EMPTY deferred-tool list while
# a richer in-process module exists (hidden capability, surfaced through
# other channels). Anything listed here must keep its advertised list
# empty — otherwise the allowlist rots into a bypass of the guard.
_INTENTIONALLY_EMPTY = {"local_browser"}


# ── 1. Deferred MCP schemas ─────────────────────────────────────────────────

def test_every_advertised_mcp_schema_is_dispatchable() -> None:
    problems: list[str] = []
    for server_key in sorted(_SERVER_TOOL_SCHEMAS):
        tools = _SERVER_TOOL_SCHEMAS[server_key]
        module = get_module(server_key)
        if module is None:
            problems.append(
                f"{server_key}: advertises {len(tools)} tool schema(s) but has no "
                "registered in-process module (BUILTIN_MCP_MODULES) — calls dead-end "
                "with 'No in-process MCP module'"
            )
            continue
        advertised = {t["name"] for t in tools}
        real = {t["name"] for t in module.list_tools()}
        missing = sorted(advertised - real)
        if missing:
            problems.append(
                f"{server_key}: advertises tools the module's list_tools() does not "
                f"implement: {missing}"
            )
    assert not problems, "agent-visible MCP schemas that cannot execute:\n" + "\n".join(problems)


def test_intentionally_empty_allowlist_cannot_rot() -> None:
    """Allowlisted keys must actually be empty (and real) — otherwise the
    allowlist silently exempts a server from the subset guard above."""
    for server_key in sorted(_INTENTIONALLY_EMPTY):
        if server_key not in _SERVER_TOOL_SCHEMAS:
            # Not present in this edition's registry — nothing to allow.
            continue
        assert _SERVER_TOOL_SCHEMAS[server_key] == [], (
            f"{server_key} is in _INTENTIONALLY_EMPTY but advertises tools; "
            "remove it from the allowlist so the subset guard applies"
        )
        assert get_module(server_key) is not None, (
            f"{server_key} is in _INTENTIONALLY_EMPTY but has no module either — "
            "it is a pure placeholder and should be removed"
        )


# ── 2. manor composite tool ────────────────────────────────────────────────

# The catalog stubs from the staging dogfooding incident: advertised by
# action=search at the time, but with no dispatch branch behind them.
_KNOWN_MANOR_STUBS = {
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


def test_manor_search_only_surfaces_implemented_actions() -> None:
    from packages.core.ai.tools.manor_tool import (
        _ALL_ACTIONS,
        _IMPLEMENTED_ACTIONS,
        _search_actions,
    )

    # Sanity: the implemented set is derived from the dispatcher source and
    # must stay within the catalog (no ghost branches).
    assert set(_IMPLEMENTED_ACTIONS) <= set(_ALL_ACTIONS)

    # Exhaustive: probing search with every catalog action's own name and
    # description must never surface an unimplemented action.
    for name, desc in _ALL_ACTIONS.items():
        for query in (name, name.replace("_", " "), desc):
            for hit in _search_actions(query, max_results=100):
                assert hit["action"] in _IMPLEMENTED_ACTIONS, (
                    f"manor action=search surfaced {hit['action']!r} (query {query!r}) "
                    "but _dispatch_action has no branch for it"
                )


def test_known_manor_stubs_stay_out_of_search_results() -> None:
    from packages.core.ai.tools.manor_tool import (
        _IMPLEMENTED_ACTIONS,
        _search_actions,
    )

    for stub in sorted(_KNOWN_MANOR_STUBS):
        if stub in _IMPLEMENTED_ACTIONS:
            # Stub gained a real dispatch branch — surfacing it is now correct.
            continue
        surfaced = {hit["action"] for hit in _search_actions(stub, max_results=100)}
        assert stub not in surfaced, (
            f"known stub {stub!r} is back in manor search results without a "
            "dispatch branch"
        )


# ── 3. code composite tool ─────────────────────────────────────────────────

def test_code_tool_catalog_matches_dispatch_table() -> None:
    from packages.core.ai.tools.code_tool import _ALL_ACTIONS, _DISPATCH

    catalog = set(_ALL_ACTIONS)
    dispatch = set(_DISPATCH)
    assert catalog == dispatch, (
        f"code tool catalog/dispatch drift — catalog-only: {sorted(catalog - dispatch)}; "
        f"dispatch-only: {sorted(dispatch - catalog)}"
    )


# ── 4. Built-in skill packs ────────────────────────────────────────────────

def _remote_transport_server_keys() -> set[str]:
    """Server keys served by a vendor-hosted remote MCP (transport != builtin).

    Their tool surface comes from the vendor's ``tools/list`` at agent
    runtime, so no in-process module is expected."""
    from packages.core.services.mcp_seed import _MCP_CATALOG

    return {row[0] for row in _MCP_CATALOG if row[3] != "builtin"}


def _declared_mcp_servers_by_skill() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for cfg_path in sorted(_SKILLS_ROOT.glob("*/config.json")):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(cfg, dict):
            continue
        keys: set[str] = set()
        for tool in cfg.get("tools") or []:
            if not isinstance(tool, str) or not tool.startswith("mcp__"):
                continue
            body = tool[len("mcp__"):]
            assert "__" in body, f"{cfg_path}: malformed MCP tool name {tool!r}"
            keys.add(body.rsplit("__", 1)[0])
        if keys:
            out[cfg_path.parent.name] = keys
    return out


def test_builtin_skills_only_declare_dispatchable_mcp_servers() -> None:
    by_skill = _declared_mcp_servers_by_skill()
    assert by_skill, "expected at least one built-in skill declaring mcp__* tools"

    remote = _remote_transport_server_keys()
    problems: list[str] = []
    for slug in sorted(by_skill):
        for server_key in sorted(by_skill[slug]):
            if server_key in remote:
                continue
            if get_module(server_key) is None:
                problems.append(
                    f"skill {slug!r}: declares mcp__{server_key}__* tools but "
                    f"'{server_key}' has no in-process module and is not a "
                    "remote-transport server — the skill instructs the agent to "
                    "call tools that cannot execute"
                )
    assert not problems, "\n".join(problems)
