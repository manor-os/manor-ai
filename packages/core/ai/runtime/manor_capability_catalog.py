"""The Manor platform's own capabilities, searched by ``manor(action='search')``.

``manor`` is the platform namespace: Manor actions, Workspace actions and the
Workspace configuration operations, and the Manor-owned MCP servers (Manor
Calendar, platform admin, saving Knowledge to this computer). ``search_tools``
finds everything else: general tools, third-party integrations (including
Manor PMS and Minutes) and Skills.

Each entry says exactly how it is called. Workspace actions and operations
exist only inside a Workspace, so outside one they are not listed; inside one,
the operation replaces the Manor shortcut action that wraps it.
"""
from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any

# Manor-owned MCP servers reached through manor. Third-party integrations and
# separate Manor products (manor_pms, manor_mcp_minutes) stay with search_tools.
MANOR_PLATFORM_MCP_PROVIDERS = ("manor_mcp_calendar", "manor_mcp_admin", "knowledge_local")
# A step inside another capability's flow (Chrome uploads); never searched on its own.
EMBEDDED_MCP_PROVIDERS = frozenset({"chrome_knowledge_local"})
_MCP_MODULES = {"manor_mcp_calendar": "manor_mcp_calendar", "manor_mcp_admin": "manor_admin",
                "knowledge_local": "knowledge_local"}

# Manor actions that wrap one Workspace operation; inside a Workspace the
# operation is listed instead.
WORKSPACE_SHORTCUT_ACTIONS = frozenset({
    "update_operating_model", "add_workspace_service", "remove_workspace_service",
    "map_agent_to_service", "unmap_agent_from_service", "update_workspace_goals",
    "update_workspace_rules", "get_goal_status", "update_goal_value",
})
# Not capabilities of their own: discovery itself, the Workspace gateway and
# the Skill entrypoint (Skills are found by search_tools).
_NOT_LISTED_ACTIONS = frozenset({"search", "workspace", "invoke_skill"})
_DESCRIPTION_CHARS = 260
_PARAMETER_DESCRIPTION_CHARS = 90

# Workspace actions without a schema of their own.
_WORKSPACE_ACTION_DESCRIPTIONS = {
    "search": "Search this Workspace's goals, tasks, agents, knowledge, artifacts, plans and rules.",
    "delegate_service": "Ask one or more of this Workspace's services (agents) to do work and return their answers.",
    "read_delegation_result": "Read the rest of a shortened delegated answer by run_id.",
    "get_goal_status": "Read this Workspace's goals and their progress.",
    "update_goal_value": "Record a verified current value for one Workspace goal.",
    "get_stat_status": "Read the Workspace's tracked stats and their latest assessments.",
    "record_stat_assessment": "Record an assessment for one Workspace stat.",
    "visualize_ledgers": "Chart this Workspace's ledgers (finance, content, recruiting, relationships).",
}


def _compact_parameters(schema: dict | None) -> dict[str, str]:
    function = (schema or {}).get("function") or schema or {}
    properties = ((function.get("parameters") or {}).get("properties")) or {}
    return {
        name: " ".join(str(spec.get("description") or spec.get("type") or "").split())[:_PARAMETER_DESCRIPTION_CHARS]
        for name, spec in properties.items() if isinstance(spec, dict)
    }


@lru_cache(maxsize=1)
def _workspace_action_schemas() -> dict[str, dict]:
    from packages.core.ai.runtime.workspace_delegation_actions import delegate_to_agent_schema
    from packages.core.ai.tools import workspace_agent_tools as tools

    return {
        "create_task": tools.WORKSPACE_CREATE_TASK_SCHEMA,
        "update_task_runtime": tools.WORKSPACE_UPDATE_TASK_RUNTIME_SCHEMA,
        "list_knowledge": tools.WORKSPACE_LIST_KNOWLEDGE_SCHEMA,
        "create_knowledge_folder": tools.WORKSPACE_CREATE_KNOWLEDGE_FOLDER_SCHEMA,
        "add_knowledge_documents": tools.WORKSPACE_ADD_KNOWLEDGE_DOCUMENTS_SCHEMA,
        "remove_knowledge_document": tools.WORKSPACE_REMOVE_KNOWLEDGE_DOCUMENT_SCHEMA,
        "update_knowledge_policy": tools.WORKSPACE_UPDATE_KNOWLEDGE_POLICY_SCHEMA,
        "add_rule": tools.WORKSPACE_ADD_RULE_SCHEMA,
        "request_strategist_review": tools.WORKSPACE_REQUEST_STRATEGIST_REVIEW_SCHEMA,
        "resolve_hitl": tools.WORKSPACE_RESOLVE_HITL_SCHEMA,
        "answer_task_blocker": tools.ANSWER_TASK_BLOCKER_SCHEMA,
        "delegate_service": delegate_to_agent_schema(),
    }


def _description(schema: dict | None, fallback: str = "") -> str:
    function = (schema or {}).get("function") or {}
    text = str(function.get("description") or fallback)
    return " ".join(text.split())[:_DESCRIPTION_CHARS]


@lru_cache(maxsize=1)
def _manor_action_entries() -> tuple[dict[str, Any], ...]:
    from packages.core.ai.tools.manor_tool import manor_action_manifests

    return tuple(
        {**item, "kind": "manor_action", "name": item["invoke_with"]["arguments"]["action"],
         "description": " ".join(str(item["description"]).split())[:_DESCRIPTION_CHARS]}
        for item in manor_action_manifests()
        if item["invoke_with"]["arguments"]["action"] not in _NOT_LISTED_ACTIONS
    )


@lru_cache(maxsize=1)
def _workspace_entries() -> tuple[dict[str, Any], ...]:
    from packages.core.ai.runtime.composite_tools import WorkspaceToolAction
    from packages.core.services.workspace_operation_service import OPERATION_PATCH_OP_CATALOG

    schemas = _workspace_action_schemas()
    entries = []
    for action in WorkspaceToolAction.values():
        if action == "operation":
            continue  # its operations are listed one by one below
        schema = schemas.get(action)
        entries.append({
            "catalog_id": f"workspace_action:{action}", "kind": "workspace_action", "name": action,
            "description": _description(schema, _WORKSPACE_ACTION_DESCRIPTIONS.get(action, "")),
            "parameters": list(_compact_parameters(schema)),
            "parameter_help": _compact_parameters(schema),
            "invoke_with": {"tool": "manor", "arguments": {
                "action": "workspace", "params": {"action": action, "params": {}}}},
        })
    for op in OPERATION_PATCH_OP_CATALOG:
        entries.append({
            "catalog_id": f"workspace_operation:{op.canonical}", "kind": "workspace_operation",
            "name": op.canonical, "display_name": op.canonical.replace(".", " ").replace("_", " "),
            "description": f"Workspace configuration change: {op.summary}.",
            **({"payload_schema": op.payload_schema} if op.payload_schema is not None else {}),
            "invoke_with": {"tool": "manor", "arguments": {"action": "workspace", "params": {
                "action": "operation",
                "params": {"action": "create_draft", "patches": [{"op": op.canonical, "payload": {}}]},
            }}},
            "then": "Review the draft with the user, then apply it with params.action='apply_draft'.",
        })
    return tuple(entries)


@lru_cache(maxsize=1)
def _platform_mcp_entries() -> tuple[dict[str, Any], ...]:
    entries = []
    for provider in MANOR_PLATFORM_MCP_PROVIDERS:
        try:
            module = importlib.import_module(f"packages.core.ai.mcp.{_MCP_MODULES[provider]}")
        except ModuleNotFoundError:
            continue  # not part of this distribution (the open-source build ships without some)
        for tool in module.list_tools():
            name = f"mcp__{provider}__{tool['name']}"
            entries.append({
                "catalog_id": f"manor_mcp:{provider}.{tool['name']}", "kind": "manor_mcp", "name": name,
                "provider": provider,
                "description": " ".join(str(tool.get("description") or "").split())[:_DESCRIPTION_CHARS],
                "parameters": list(((tool.get("parameters") or tool.get("inputSchema") or {}).get("properties") or {})),
                "invoke_with": {"tool": "call_tool", "arguments": {"name": name, "arguments": {}}},
            })
    return tuple(entries)


def manor_capability_catalog(*, in_workspace: bool, usable_providers: frozenset[str] | None = None) -> list[dict]:
    """The entries ``manor(action='search')`` ranks for this context.

    ``usable_providers`` (None = not checked) removes Manor MCP servers the
    actor cannot use, e.g. platform admin for non-admins.
    """
    entries = [
        item for item in _manor_action_entries()
        if not (in_workspace and item["name"] in WORKSPACE_SHORTCUT_ACTIONS)
    ]
    if in_workspace:
        entries += _workspace_entries()
    entries += [
        item for item in _platform_mcp_entries()
        if usable_providers is None or item["provider"] in usable_providers
    ]
    return entries


def all_manor_capability_ids() -> tuple[str, ...]:
    """Every id manor search can list, in any context (registration contract)."""
    return tuple(dict.fromkeys(
        item["catalog_id"] for item in (*_manor_action_entries(), *_workspace_entries(), *_platform_mcp_entries())
    ))


_DETAILED_MATCHES = 2
_SETUP_REASONS: dict[str, str] = {}


def _match(entry: dict, assessment: dict, *, detailed: bool, unavailable: str | None) -> dict:
    match = {
        "id": entry["catalog_id"], "name": entry["name"], "description": entry["description"],
        "call": entry["invoke_with"], "available": unavailable is None,
    }
    if unavailable:
        match["unavailable_reason"] = unavailable
    if "query" in assessment:
        match["for_query"] = assessment["query"]
    if entry.get("payload_schema") is not None:
        match["payload_schema"] = entry["payload_schema"]
    if entry.get("then"):
        match["then"] = entry["then"]
    if detailed and entry.get("parameter_help"):
        match["parameters"] = entry["parameter_help"]
    elif entry.get("parameters"):
        match["parameters"] = entry["parameters"]
    return match


def runtime_manor_mcp_scope(runtime_envelope: Any | None, context_allowed_tool_names: Any = None):
    """Which Manor MCP tools this run may discover: the same scope search_tools applies."""
    from packages.core.ai.runtime.envelope import runtime_effective_tool_scope

    scope = runtime_effective_tool_scope(
        runtime_envelope=runtime_envelope, context_allowed_tool_names=context_allowed_tool_names,
    )
    unrestricted = bool(getattr(runtime_envelope, "mcp_scope_unrestricted", False))
    providers = {
        str(getattr(item, "provider", "") or "") for item in getattr(runtime_envelope, "mcp_provider_scopes", ()) or ()
    }

    def allows(tool_name: str, provider: str) -> bool:
        if tool_name in scope.blocked_tool_names:
            return False
        return scope.allowed_tool_names is None or unrestricted or provider in providers or scope.allows(tool_name)

    return allows


async def runtime_manor_search(
    *, entity_id: str, user_id: str, workspace_id: str | None, query: object = None,
    queries: object = None, max_results: object = None, mcp_tool_in_scope: Any = None,
) -> str:
    """``manor(action='search')``: rank the Manor platform's own capabilities.

    ``mcp_tool_in_scope(name, provider)`` is the run's tool scope; Manor MCP
    tools outside it are not offered (execution checks it again)."""
    import json

    from packages.core.ai.runtime.capability_ranker import query_is_english, rank_runtime_capabilities
    from packages.core.ai.runtime.tool_discovery import runtime_prepare_search_tools_request

    request = runtime_prepare_search_tools_request(query=query, queries=queries, max_results=max_results)
    if not request.ok:
        return json.dumps({"error": request.error})
    if not all(query_is_english(text) for text in request.queries or (request.query,)):
        return json.dumps({
            "error": "query_not_english",
            "message": "Manor action names and descriptions are English. Search again with English keywords, "
                       "whatever language the user writes in.",
        })
    usable: frozenset[str] | None = None
    if entity_id and user_id:
        try:
            from packages.core.database import async_session
            from packages.core.services.agent_permission_service import resolve_usable_mcp_providers

            async with async_session() as db:
                usable = await resolve_usable_mcp_providers(
                    db, user_id=user_id, entity_id=entity_id, provider_keys=MANOR_PLATFORM_MCP_PROVIDERS,
                )
        except Exception:
            usable = None
    # Platform admin is not offered to anyone who cannot use it; other Manor
    # servers stay listed with what they need.
    hidden = {"manor_mcp_admin"} - (usable or frozenset()) if usable is not None else set()
    catalog = [
        item for item in manor_capability_catalog(in_workspace=bool(workspace_id))
        if item.get("provider") not in hidden
        and (not item.get("provider") or mcp_tool_in_scope is None
             or mcp_tool_in_scope(item["name"], item["provider"]))
    ]
    by_id = {item["catalog_id"]: item for item in catalog}
    selection = rank_runtime_capabilities(
        query=request.query, candidates=catalog, limit=request.max_results, queries=request.queries,
    )
    matches = []
    for position, ranked in enumerate(selection.matches):
        entry = by_id[ranked["id"]]
        provider = entry.get("provider")
        unavailable = None
        if provider and usable is not None and provider not in usable:
            unavailable = _SETUP_REASONS.get(provider, "Not available to this user.")
        matches.append(_match(entry, ranked["assessment"], detailed=position < _DETAILED_MATCHES,
                              unavailable=unavailable))
    payload: dict[str, Any] = {
        "mode": "manor", "query": request.query, "matches": matches,
        # Manor MCP tools load like search_tools results and are called through call_tool.
        "loaded_tools": [m["name"] for m in matches if m["name"].startswith("mcp__") and m["available"]],
        "hint": (
            "Call a match exactly as its 'call' shows, filling its parameters. "
            "General tools, third-party integrations and Skills are found with search_tools."
        ),
    }
    if not workspace_id:
        payload["scope"] = "Outside a Workspace: Workspace actions and configuration operations are not listed."
    if not matches:
        payload["hint"] = ("No Manor platform capability matches. Try other English keywords, or use "
                           "search_tools for general tools, integrations and Skills.")
    return json.dumps(payload, ensure_ascii=False)
