from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from packages.core.ai.runtime.capability_search import (
    _runtime_capability_search_terms,
    runtime_merge_capability_matches,
    runtime_companion_tool_names,
    runtime_search_companion_skill_candidates,
    runtime_search_skill_candidates,
    runtime_skill_manifest,
)
from packages.core.ai.runtime.capability_matcher import (
    CapabilitySelection, capability_candidate_page,
)
from packages.core.ai.runtime.capability_ranker import query_is_english, rank_runtime_capabilities
from packages.core.ai.runtime.tool_visibility import (
    runtime_search_always_loaded_tool_names,
    runtime_search_bound_tool_names_for_profile,
    runtime_shadowed_file_generation_tool,
)
from packages.core.ai.runtime.tool_availability import runtime_annotate_tool_availability
from packages.core.ai.runtime.tool_bindings import (
    RuntimeDynamicMCPDiscoveryScopeFactory,
    runtime_search_tool_binding_scope,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.ai.runtime.envelope import runtime_effective_tool_scope
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.workspace_integration_scope import (
    integration_provider_for_tool,
    load_disabled_workspace_providers,
)
from packages.core.ai.runtime.manor_capability_catalog import (
    EMBEDDED_MCP_PROVIDERS,
    MANOR_PLATFORM_MCP_PROVIDERS,
)
from packages.core.ai.runtime.tool_discovery import (
    SEARCH_TOOLS_MAX_QUERIES,
    runtime_mcp_provider_from_tool_name,
    runtime_prepare_search_tools_request,
    runtime_search_tools_payload,
    runtime_tool_query_score,
)

if TYPE_CHECKING:
    from packages.core.ai.runtime.skills import RuntimeSkillExecutability

logger = logging.getLogger(__name__)


_TOOL_MANIFEST_DESCRIPTION_CHARS = 260
_TOOL_MANIFEST_PARAMETER_LIMIT = 12
_CAPABILITY_KINDS = frozenset({"tool", "mcp_tool", "skill"})
# A direct match against a concrete first-party tool is more specific than a
# match against an MCP server's broad catalog description or intent alias.
# Provider identity matches (for example "gmail") still receive a much larger
# provider score and win when the user actually names the integration.
_FIRST_PARTY_DIRECT_MATCH_WEIGHT = 40


# Ranked by the local lexical ranker (or, historically, a model assessment).
_SELECTED_STATUSES = frozenset({"ranked", "assessed"})
# When nothing ranks, the scoped catalog page is shown unranked: this many
# candidates with descriptions, the rest by id only.
FALLBACK_DESCRIBED_CANDIDATES = 12
FALLBACK_DESCRIPTION_CHARS = 240


def runtime_search_tools_schema() -> dict:
    """Return the built-in search_tools schema owned by Runtime Harness."""

    return {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Find tools, MCP actions and reusable Skills; matches do not prove "
                "availability or execution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["semantic", "provider", "exact"],
                        "default": "semantic",
                        "description": (
                            "semantic: query/queries; provider: provider_key; exact: exact_names."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "English keywords; +word must name a capability."
                        ),
                    },
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": SEARCH_TOOLS_MAX_QUERIES,
                        "description": (
                            "English queries per step; each ranked, plus query if set."
                        ),
                    },
                    "provider_key": {
                        "type": "string",
                        "description": (
                            "Provider-mode ID from prior catalog."
                        ),
                    },
                    "exact_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 8,
                        "description": (
                            "Exact Tool/Skill names."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Default 5 or 3/step; exact: name count; cap 8."
                        ),
                    },
                    "kinds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["tool", "mcp_tool", "skill"],
                        },
                        "description": (
                            "All kinds if omitted."
                        ),
                    },
                    "cursor": {
                        "type": "integer", "minimum": 0,
                        "description": "Semantic: prior next_cursor.",
                    },
                },
                "additionalProperties": False,
            },
        },
    }


def runtime_tool_manifest(name: str, schema: dict) -> dict:
    """Small search result shown to the LLM before a full schema is loaded."""
    fn = schema.get("function", {}) if isinstance(schema, dict) else {}
    description = (fn.get("description") or "").strip()
    params = (
        fn.get("parameters", {}).get("properties", {})
        if isinstance(fn.get("parameters"), dict)
        else {}
    )
    manifest = {
        "kind": "mcp_tool" if name.startswith("mcp__") else "tool",
        "name": name,
        "description": description[:_TOOL_MANIFEST_DESCRIPTION_CHARS],
        "parameters": list(params.keys())[:_TOOL_MANIFEST_PARAMETER_LIMIT],
    }
    if len(description) > _TOOL_MANIFEST_DESCRIPTION_CHARS:
        manifest["description_truncated"] = True
    return manifest


def _runtime_provider_operation_query_score(manifest: dict, query: str) -> int:
    """Rank authorized provider operations without singular/plural mismatches."""

    def terms(text: str) -> set[str]:
        result = set()
        for term in _runtime_capability_search_terms(text):
            if len(term) > 4 and term.endswith("ies"):
                term = term[:-3] + "y"
            elif len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
                term = term[:-1]
            result.add(term)
        return result

    identity = terms(str(manifest.get("name") or ""))
    description = terms(str(manifest.get("description") or ""))
    return sum(
        12 if term in identity else 4 if term in description else 0
        for term in terms(query)
    )


def _runtime_requested_capability_kinds(
    raw_kinds: object,
) -> tuple[frozenset[str], str | None]:
    if raw_kinds is None:
        return _CAPABILITY_KINDS, None
    if not isinstance(raw_kinds, (list, tuple, set, frozenset)):
        return frozenset(), "kinds must be an array"
    kinds = frozenset(str(kind or "").strip() for kind in raw_kinds)
    invalid = sorted(kinds - _CAPABILITY_KINDS)
    if invalid:
        return frozenset(), f"unknown capability kinds: {', '.join(invalid)}"
    if not kinds:
        return frozenset(), "kinds must contain at least one capability kind"
    return kinds, None


def _live_mcp_tool_name_and_schema(
    tool: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Read typed discovery records while retaining legacy tuple loaders."""

    name = getattr(tool, "name", None)
    schema = getattr(tool, "schema", None)
    if name is None and isinstance(tool, (tuple, list)) and len(tool) >= 2:
        name, schema = tool[0], tool[1]
    resolved_name = str(name or "").strip()
    if not resolved_name or not isinstance(schema, dict):
        return None
    return resolved_name, schema


def _official_live_discovery_providers(
    *,
    matches: Iterable[Mapping[str, Any]],
    official_providers: set[str],
    provider_keys: Iterable[str] = (),
    exact_names: Iterable[str] = (),
) -> frozenset[str]:
    """Resolve actor-bound official providers worth probing for live schemas.

    Structured provider and exact-name selections narrow discovery. A local
    match is sufficient; Runtime never infers an MCP provider from query text.
    """

    matches = tuple(matches)
    providers = {
        provider
        for match in matches
        if (
            provider := runtime_mcp_provider_from_tool_name(
                str(match.get("name") or "")
            )
        ) in official_providers
        and match.get("available") is True
    }
    for provider in provider_keys:
        provider = str(provider or "").strip().casefold()
        if provider in official_providers:
            providers.add(provider)
    for selector in exact_names:
        provider = runtime_mcp_provider_from_tool_name(str(selector or "").strip())
        if provider in official_providers:
            providers.add(provider)
    return frozenset(providers)


def runtime_select_tool_candidates(selector: str, tool_names: Iterable[str]) -> list[str]:
    """Resolve one structured exact name plus MCP action short names."""
    selector = selector.strip().lower()
    if not selector:
        return []
    names = tuple(str(name) for name in tool_names)
    if selector in names:
        return [selector]
    suffix = f"__{selector}"
    return sorted(
        name
        for name in names
        if name.startswith("mcp__") and name.endswith(suffix)
    )


def _runtime_explicit_provider_keys(
    *,
    provider_keys: Iterable[str],
    exact_names: Iterable[str],
) -> frozenset[str]:
    """Return provider keys named by structured discovery fields.

    Dynamic official MCP tools may have no static fallback schema yet. In that
    case an exact MCP tool name is the only local evidence naming the
    provider, and it still has to participate in the account-readiness batch
    before Runtime performs remote ``tools/list`` discovery.
    """

    return frozenset(
        {
            str(provider or "").strip().casefold()
            for provider in provider_keys
            if str(provider or "").strip()
        }
        | {
            provider
            for selector in exact_names
            if (
                provider := runtime_mcp_provider_from_tool_name(
                    str(selector or "").strip()
                )
            )
        }
    )


def runtime_search_tool_candidates(
    *,
    tool_schemas: Iterable[tuple[str, dict]],
    query: str,
    provider_keys: Iterable[str] = (),
    provider_selection: str = "explicit",
    exact_names: Iterable[str] = (),
    max_results: int = 5,
    bound_tool_names: set[str] | None = None,
    active_user_message: str | None = None,
    always_loaded_tool_names: Iterable[str] = (),
    usable_providers: frozenset[str] | None = None,
    server_index: dict[str, dict] | None = None,
    intent_path_boosts: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Search tool manifests under an explicit structured discovery mode.

    MCP tools are reachable only through exact ``provider_keys`` or
    ``exact_names`` supplied by the structured planner. Natural-language query
    scoring never selects a provider. Once a provider has been explicitly
    resolved, its authorized operations are ranked by the query as well.
    ``server_index`` and ``intent_path_boosts`` remain accepted for compatibility
    with pure callers but cannot influence provider routing.
    """
    tool_map = {str(name): schema for name, schema in tool_schemas}
    always_loaded = set(always_loaded_tool_names)
    requested_providers = tuple(
        dict.fromkeys(
            str(provider or "").strip().casefold()
            for provider in provider_keys
            if str(provider or "").strip()
        )
    )
    if requested_providers:
        suppressed: list[dict] = []
        known_providers = set(server_index or ()) | {
            provider
            for name in tool_map
            if (provider := runtime_mcp_provider_from_tool_name(name))
        }
        for provider in requested_providers:
            if provider not in known_providers:
                suppressed.append({
                    "server_key": provider,
                    "reason": "unknown_server",
                    "similar_servers": [],
                })
            elif usable_providers is not None and provider not in usable_providers:
                suppressed.append({
                    "server_key": provider,
                    "reason": "not_usable",
                    "matched_tools": [],
                })
        if suppressed:
            if provider_selection != "inferred":
                return [], suppressed
            # A semantic provider suggestion is not a required dependency.
            # Preserve the same authorized search scope, excluding only the
            # unavailable providers; never execute or grant an alternate route.
            suppressed_keys = {item["server_key"] for item in suppressed}
            alternatives, other_suppressed = runtime_search_tool_candidates(
                tool_schemas=tool_map.items(),
                query=query,
                provider_keys=(key for key in requested_providers if key not in suppressed_keys),
                provider_selection=provider_selection,
                max_results=max_results,
                bound_tool_names=bound_tool_names,
                active_user_message=active_user_message,
                always_loaded_tool_names=always_loaded,
                usable_providers=usable_providers,
                server_index=server_index,
                intent_path_boosts=intent_path_boosts,
            )
            return alternatives, suppressed + other_suppressed
        tools_by_provider = {
            provider: sorted(
                (
                    (name, schema)
                    for name, schema in tool_map.items()
                    if runtime_mcp_provider_from_tool_name(name) == provider
                    and (
                        bound_tool_names is None
                        or name in always_loaded
                        or name in bound_tool_names
                    )
                ),
                key=lambda item: (
                    -_runtime_provider_operation_query_score(
                        runtime_tool_manifest(item[0], item[1]), query,
                    ),
                    item[0],
                ),
            )
            for provider in requested_providers
        }
        if not any(tools_by_provider.values()):
            return [], [
                {
                    "server_key": provider,
                    "reason": "unknown_server",
                    "similar_servers": [],
                }
                for provider in requested_providers
            ]
        provider_tools: list[tuple[str, dict]] = []
        provider_limit = max(1, max_results // 2) if query.strip() else max_results
        tool_index = 0
        while len(provider_tools) < provider_limit:
            added = False
            for provider in requested_providers:
                tools = tools_by_provider[provider]
                if tool_index >= len(tools):
                    continue
                provider_tools.append(tools[tool_index])
                added = True
                if len(provider_tools) >= provider_limit:
                    break
            if not added:
                break
            tool_index += 1
        provider_matches = [
            runtime_tool_manifest(name, schema)
            for name, schema in provider_tools
        ]
        remaining_results = max_results - len(provider_matches)
        if not query.strip() or remaining_results <= 0:
            return provider_matches, []
        non_mcp_matches, _ = runtime_search_tool_candidates(
            tool_schemas=tool_map.items(),
            query=query,
            max_results=remaining_results,
            bound_tool_names=bound_tool_names,
            active_user_message=active_user_message,
            always_loaded_tool_names=always_loaded,
            usable_providers=usable_providers,
            server_index=server_index,
            intent_path_boosts=intent_path_boosts,
        )
        return provider_matches + non_mcp_matches, []

    query_lower = query.lower()
    names = tuple(
        dict.fromkeys(
            str(name or "").strip()
            for name in exact_names
            if str(name or "").strip()
        )
    )
    if names:
        results: list[dict] = []
        selected: set[str] = set()
        for requested_name in names:
            for name in runtime_select_tool_candidates(requested_name, tool_map.keys()):
                if name in selected:
                    continue
                if runtime_shadowed_file_generation_tool(
                    name,
                    bound_tool_names=bound_tool_names,
                    available_tool_names=tool_map,
                ):
                    continue
                schema = tool_map.get(name)
                if not schema:
                    continue
                if (
                    bound_tool_names is not None
                    and name not in always_loaded
                    and name not in bound_tool_names
                ):
                    continue
                results.append(runtime_tool_manifest(name, schema))
                selected.add(name)
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
        return results, []

    scored_non_mcp: list[tuple[int, str]] = []
    for name, schema in tool_map.items():
        if (
            bound_tool_names is not None
            and name not in always_loaded
            and name not in bound_tool_names
        ):
            continue
        if runtime_shadowed_file_generation_tool(
            name,
            bound_tool_names=bound_tool_names,
            available_tool_names=tool_map,
        ):
            continue

        fn = schema.get("function", {}) if isinstance(schema, dict) else {}
        desc = fn.get("description") or ""
        score = runtime_tool_query_score(name, desc, query_lower)
        provider = runtime_mcp_provider_from_tool_name(name)
        if provider:
            continue
        if score > 0:
            scored_non_mcp.append(
                (score * _FIRST_PARTY_DIRECT_MATCH_WEIGHT, name)
            )

    selected = [
        name
        for _score, name in sorted(
            scored_non_mcp,
            key=lambda item: item[0],
            reverse=True,
        )[:max_results]
    ]
    return [runtime_tool_manifest(name, tool_map[name]) for name in selected], []


def _runtime_selected_tool_names(selectors: Iterable[str], tool_names: Iterable[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    names = tuple(tool_names)
    for selector in selectors:
        for name in runtime_select_tool_candidates(selector, names):
            if name in seen:
                continue
            selected.append(name)
            seen.add(name)
    return selected


def _runtime_chrome_skill_invocation_match(
    *,
    tool_map: dict[str, dict],
    bound_tool_names: set[str] | None,
    always_loaded_tool_names: set[str],
    active_user_message: str | None,
    scope: Any,
) -> dict | None:
    """Prefer the Chrome skill boundary over parent-chat direct MCP discovery."""
    if not scope.chrome_local_browser:
        return None
    name = "invoke_skill"
    if name not in tool_map:
        return None
    if (
        bound_tool_names is not None
        and name not in always_loaded_tool_names
        and name not in bound_tool_names
    ):
        return None
    if not scope.first_party_tool_allowed(name, active_user_message):
        return None
    return runtime_tool_manifest(name, tool_map[name])


def _append_preferred_chrome_tool_matches(
    *,
    results: list[dict],
    selected: set[str],
    tool_map: dict[str, dict],
    bound_tool_names: set[str] | None,
    always_loaded_tool_names: set[str],
    active_user_message: str | None,
    scope: Any,
    max_results: int,
) -> None:
    if not scope.preferred_chrome_tool_names:
        return
    for name in scope.preferred_chrome_tool_names:
        if len(results) >= max_results:
            break
        if name in selected or name not in tool_map:
            continue
        if (
            bound_tool_names is not None
            and name not in always_loaded_tool_names
            and name not in bound_tool_names
        ):
            continue
        if runtime_shadowed_file_generation_tool(
            name,
            bound_tool_names=bound_tool_names,
            available_tool_names=tool_map,
        ):
            continue
        provider = runtime_mcp_provider_from_tool_name(name)
        if provider and not scope.provider_allowed(provider):
            continue
        if not provider and not scope.first_party_tool_allowed(
            name,
            active_user_message,
        ):
            continue
        results.append(runtime_tool_manifest(name, tool_map[name]))
        selected.add(name)


def _append_tool_match_if_allowed(
    *,
    results: list[dict],
    selected: set[str],
    name: str,
    tool_map: dict[str, dict],
    bound_tool_names: set[str] | None,
    always_loaded_tool_names: set[str],
    active_user_message: str | None,
    scope: Any,
) -> bool:
    if name in selected or name not in tool_map:
        return False
    if (
        bound_tool_names is not None
        and name not in always_loaded_tool_names
        and name not in bound_tool_names
    ):
        return False
    if runtime_shadowed_file_generation_tool(
        name,
        bound_tool_names=bound_tool_names,
        available_tool_names=tool_map,
    ):
        return False
    provider = runtime_mcp_provider_from_tool_name(name)
    if provider and not scope.provider_allowed(provider):
        return False
    if not provider and not scope.first_party_tool_allowed(
        name,
        active_user_message,
    ):
        return False
    results.append(runtime_tool_manifest(name, tool_map[name]))
    selected.add(name)
    return True


def _chrome_select_tool_is_default_path_bypass(tool_name: str, scope: Any) -> bool:
    if not scope.chrome_local_browser:
        return False
    return tool_name in {
        "mcp__chrome__claim_tab",
        "mcp__chrome__click_point",
    }


INVOCABLE_UNDER_POLICY_STATE = "invocable_under_policy"
SKILL_SETUP_REQUIRED_STATE = "setup_required"
SKILL_DECLARED_TOOLS_OUT_OF_SCOPE_STATE = "declared_tools_out_of_scope"


def runtime_skill_readiness(
    executability: "RuntimeSkillExecutability",
    *,
    connected_providers: Callable[[str], bool | None],
) -> dict[str, Any] | None:
    """Readiness of one Skill for the caller, or None when it is fully ready.

    ``executability`` comes from the same predicate invoke_skill applies, so
    search and invocation agree on whether the Skill can run. Connection state
    is an extra signal: ``connected_providers`` returns None when unknown,
    which counts as connected (fail open).
    """

    from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link

    if not executability.executable:
        return {
            "state": SKILL_DECLARED_TOOLS_OUT_OF_SCOPE_STATE,
            "missing_tools": list(executability.missing_tools),
        }
    unconnected: list[str] = []
    setup_providers: list[str] = []
    for name in executability.executable_tools:
        provider = runtime_mcp_provider_from_tool_name(name) if name.startswith("mcp__") else None
        if provider and connected_providers(provider) is False:
            unconnected.append(name)
            if provider not in setup_providers:
                setup_providers.append(provider)
    missing = sorted(set(executability.missing_tools) | set(unconnected))
    if not missing:
        return None
    readiness: dict[str, Any] = {"state": INVOCABLE_UNDER_POLICY_STATE, "missing_tools": missing}
    if setup_providers:
        readiness["setup_providers"] = setup_providers
        link = runtime_integration_setup_link(setup_providers[0])
        if link.get("setup_url"):
            readiness["setup_url"] = link["setup_url"]
        if len(unconnected) == len(executability.executable_tools):
            # Every executable tool needs an integration that is not connected.
            readiness["state"] = SKILL_SETUP_REQUIRED_STATE
    return readiness


def runtime_search_tool_registry_candidates(
    *,
    tool_schemas: Iterable[tuple[str, dict]],
    query: str,
    provider_keys: Iterable[str] = (),
    provider_selection: str = "explicit",
    exact_names: Iterable[str] = (),
    max_results: int = 5,
    bound_tool_names: set[str] | None = None,
    active_user_message: str | None = None,
    usable_providers: frozenset[str] | None = None,
    server_index: dict[str, dict] | None = None,
    intent_path_boosts: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Search registry-backed tools using the Runtime-owned eager baseline."""

    return runtime_search_tool_candidates(
        tool_schemas=tool_schemas,
        query=query,
        provider_keys=provider_keys,
        provider_selection=provider_selection,
        exact_names=exact_names,
        max_results=max_results,
        bound_tool_names=bound_tool_names,
        active_user_message=active_user_message,
        always_loaded_tool_names=runtime_search_always_loaded_tool_names(),
        usable_providers=usable_providers,
        server_index=server_index,
        intent_path_boosts=intent_path_boosts,
    )


def _runtime_manor_routes(*, in_scope: bool, in_workspace: bool) -> dict[str, dict]:
    """Native tool names reachable through the in-scope ``manor`` tool, with the exact call.

    Taken from the Manor capability catalog, so inside a Workspace a shortcut
    action is routed through its Workspace action, as ``manor`` executes it.
    """

    if not in_scope:
        return {}
    from packages.core.ai.runtime.manor_capability_catalog import manor_capability_catalog

    try:
        entries = manor_capability_catalog(in_workspace=in_workspace)
    except Exception:
        logger.warning("Manor capability catalog unavailable for discovery", exc_info=True)
        return {}
    from packages.core.ai.runtime.manor_capability_catalog import WORKSPACE_SHORTCUT_ACTIONS

    routes: dict[str, dict] = {}
    for entry in entries:
        kind = entry.get("kind")
        name = str(entry.get("name") or "")
        if kind not in {"manor_action", "workspace_action"} or not isinstance(entry.get("invoke_with"), dict):
            continue
        # Names listed both ways keep the direct manor action unless it is a
        # Workspace shortcut, which the Workspace action replaces.
        prefer_workspace = name in WORKSPACE_SHORTCUT_ACTIONS
        if name in routes and (kind == "workspace_action") is not prefer_workspace:
            continue
        routes[name] = entry["invoke_with"]
    return routes


async def _runtime_approval_policy_deny_matcher(
    *, entity_id: str | None, workspace_id: str | None, user_id: str | None,
    room_id: str | None = None,
) -> tuple[Callable[[str], bool | None] | None, bool]:
    """Return ``(denies, policy_known)`` from the current approval policy.

    Mirrors ``check_step_policy``: the Workspace hard constraints (risk
    ceiling) when there is a Workspace, then the inherited approval chain
    (account, personal, Workspace, room), loaded once per search. A ``deny``
    outcome means policy forbids the tool. ``denies`` returns None when a tool
    cannot be evaluated. When the policy cannot be loaded, ``policy_known`` is
    False: nothing is offered for acquisition and nothing is claimed blocked.
    """

    if not entity_id:
        return None, True
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool,
        runtime_policy_action_for_classification,
    )
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.database import async_session
    from packages.core import governance
    from packages.core.governance import approval_policy_service
    from packages.core.governance.approval_facts import action_facts
    from packages.core.governance.approval_policy import evaluate_policy
    from packages.core.governance.policy import decide

    try:
        async with async_session() as policy_db:
            workspace_policy = (
                await governance.get_policy(policy_db, workspace_id) if workspace_id else None
            )
            chain = await approval_policy_service.load_policy_chain(
                policy_db, entity_id=entity_id, workspace_id=workspace_id or None,
                user_id=user_id or None, room_id=room_id or None,
            )
    except Exception:
        logger.warning("Approval policy unavailable for discovery; offering no acquisition", exc_info=True)
        return None, False

    def denies(name: str) -> bool | None:
        try:
            # Judged by the tool's default action (no arguments). Tools whose
            # action depends on arguments (code_project, generate_file kinds)
            # are approximated by that default; execution re-checks the
            # concrete call through the normal gate.
            classification = classify_runtime_tool(name, {})
            if classification.effect in {RuntimeToolEffect.ORCHESTRATOR, RuntimeToolEffect.CONTROL}:
                return False  # the execution gate does not policy-check these either
            action, _impact = runtime_policy_action_for_classification(classification)
            if workspace_policy is not None and not decide(
                workspace_policy, kind=action.kind, action_key=action.action_key,
                risk_level=action.risk_level, resource_id=action.resource_id,
                capability_id=action.capability_id,
            ).allowed:
                return True
            facts = action_facts(
                action_key=action.action_key, capability_id=action.capability_id,
                resource_kind=action.resource_kind, resource_id=action.resource_id,
                operation=action.operation, risk_level=action.risk_level,
                **action.policy_effect(),
            )
            return evaluate_policy(chain, facts).outcome == "deny"
        except Exception:
            logger.warning("Approval policy evaluation failed for %s", name, exc_info=True)
            return None

    return denies, True


async def _runtime_skill_match_readiness(
    skill_matches: list[dict],
    *,
    descriptors_by_id: Mapping[str, Any],
    allowed_tool_names: set[str] | None,
    runtime_envelope: Any,
    registered_tool_names: Iterable[str],
    provider_readiness_known: bool,
    resolved_provider_keys: frozenset[str],
    usable_providers: frozenset[str],
    load_usable_providers: Callable[..., Awaitable[tuple[frozenset[str], float]]] | None,
) -> dict[int, dict[str, Any]]:
    """Readiness per matched Skill (keyed by ``id(match)``); empty when all are ready."""

    from packages.core.ai.runtime.skills import (
        runtime_skill_executability,
        runtime_skill_from_descriptor,
    )

    registered = tuple(registered_tool_names)
    executability_by_match: dict[int, Any] = {}
    for match in skill_matches:
        descriptor = descriptors_by_id.get(str(match.get("skill_id") or ""))
        if descriptor is None:
            continue
        try:
            executability_by_match[id(match)] = runtime_skill_executability(
                runtime_skill_from_descriptor(descriptor),
                allowed_tool_names=allowed_tool_names,
                runtime_envelope=runtime_envelope,
                registered_tool_names=registered,
            )
        except Exception:
            logger.warning("Skill readiness unavailable for %s", match.get("skill_id"), exc_info=True)
    if not executability_by_match:
        return {}
    known: dict[str, bool] = {}
    if provider_readiness_known:
        known.update({key: key in usable_providers for key in resolved_provider_keys})
    wanted = sorted({
        provider
        for executability in executability_by_match.values()
        for name in executability.executable_tools
        if name.startswith("mcp__")
        and (provider := runtime_mcp_provider_from_tool_name(name))
        and provider not in known
    })
    if wanted and provider_readiness_known and load_usable_providers is not None:
        try:
            extra, _elapsed = await load_usable_providers(wanted)
            known.update({key: key in extra for key in wanted})
        except Exception:
            logger.warning("Skill provider readiness unavailable; continuing without it", exc_info=True)
    readiness: dict[int, dict[str, Any]] = {}
    for match_id, executability in executability_by_match.items():
        state = runtime_skill_readiness(
            executability, connected_providers=lambda provider: known.get(provider),
        )
        if state is not None:
            readiness[match_id] = state
    return readiness


async def runtime_execute_search_tools_handler(
    *,
    arguments: Mapping[str, Any],
    entity_id: str = "",
    user_id: str = "",
    tool_schemas: Iterable[tuple[str, dict]],
    available_tool_names: Iterable[str],
    total_tool_count: int | None = None,
    live_mcp_schema_loader: Callable[
        [frozenset[str]],
        Awaitable[dict[str, list[Any]]],
    ] | None = None,
    skill_descriptor_loader: Callable[
        [],
        Awaitable[Iterable[Any]],
    ] | None = None,
) -> str:
    """Execute the built-in search_tools contract against a registry snapshot."""

    performance_started = time.monotonic()
    provider_resolution_ms = 0.0
    provider_match_ms = 0.0
    ranking_ms = 0.0
    availability_ms = 0.0
    live_schema_ms = 0.0
    skill_search_ms = 0.0
    registry_provider_keys: list[str] = []
    tool_schemas = tuple(tool_schemas)
    available_names = tuple(str(name) for name in available_tool_names)
    search_request = runtime_prepare_search_tools_request(
        mode=arguments.get("mode"),
        query=arguments.get("query"),
        queries=arguments.get("queries"),
        provider_key=arguments.get("provider_key"),
        exact_names=arguments.get("exact_names"),
        max_results=arguments.get("max_results"),
    )
    if not search_request.ok:
        return json.dumps({"error": search_request.error})
    if search_request.mode == "semantic" and not all(
        query_is_english(text) for text in search_request.queries or (search_request.query,)
    ):
        return json.dumps({
            "error": "query_not_english",
            "message": (
                "Tool, Skill and provider names and descriptions are English. Search again "
                "with English keywords for the capability, whatever language the user writes in."
            ),
        })
    requested_kinds, kinds_error = _runtime_requested_capability_kinds(
        arguments.get("kinds")
    )
    if kinds_error:
        return json.dumps({"error": kinds_error})
    cursor = arguments.get("cursor", 0)
    if type(cursor) is not int or cursor < 0 or (cursor and search_request.mode != "semantic"):
        return json.dumps({"error": "cursor must be a nonnegative integer in semantic mode"})

    runtime_context = runtime_tool_call_context_from_kwargs(
        dict(arguments), entity_id=entity_id or None, user_id=user_id or None,
    )
    runtime_envelope = runtime_context.runtime_envelope
    # A prompt Skill has already selected its capability scope. Discovery
    # narrows that scope; it neither reroutes the child nor grants authority.
    discovery_policy = (getattr(runtime_envelope, "metadata", None) or {}).get(
        "prompt_skill_tool_discovery"
    )
    child_discovery = isinstance(discovery_policy, dict)
    child_providers = frozenset(
        canonical_provider_key(key) for key in discovery_policy.get("provider_keys", ())
    ) if child_discovery else frozenset()
    child_prefixes = tuple(discovery_policy.get("tool_prefixes", ())) if child_discovery else ()

    def _child_allows_tool(name: str) -> bool:
        return not child_discovery or (
            name.startswith(child_prefixes)
            and (not child_providers or canonical_provider_key(
                runtime_mcp_provider_from_tool_name(name)
            ) in child_providers)
        )

    def _child_allows_provider(provider: str) -> bool:
        prefix = f"mcp__{provider}__"
        return not child_discovery or (
            (not child_providers or canonical_provider_key(provider) in child_providers)
            and any(prefix.startswith(item) or item.startswith(prefix) for item in child_prefixes)
        )

    if child_discovery:
        requested_kinds = requested_kinds - {"skill"}
        if search_request.mode == "semantic" and len(child_providers) == 1:
            search_request = replace(
                search_request, mode="provider", provider_key=next(iter(child_providers)),
            )
    disabled_providers: frozenset[str] = frozenset()
    if runtime_context.workspace_id:
        from packages.core.database import async_session

        try:
            if not runtime_context.entity_id:
                raise ValueError("Workspace discovery requires an Entity scope.")
            async with async_session() as scope_db:
                disabled_providers = await load_disabled_workspace_providers(
                    scope_db, workspace_id=runtime_context.workspace_id, entity_id=runtime_context.entity_id,
                )
        except Exception:
            logger.warning("Workspace integration discovery policy unavailable", exc_info=True)
            return json.dumps({
                "error": "workspace_integration_scope_unavailable",
                "message": "Workspace integration policy could not be verified. No capabilities were loaded.",
            })
    if disabled_providers:
        tool_schemas = tuple(
            (name, schema) for name, schema in tool_schemas
            if integration_provider_for_tool(name) not in disabled_providers
        )
        available_names = tuple(
            name for name in available_names
            if integration_provider_for_tool(name) not in disabled_providers
        )
    effective_scope = runtime_effective_tool_scope(
        runtime_envelope=runtime_envelope,
        context_allowed_tool_names=runtime_context.allowed_tool_names,
    )
    context_allowed = effective_scope.searchable_tool_names()
    binding_scope = await runtime_search_tool_binding_scope(
        agent_id=runtime_context.agent_id if isinstance(runtime_context.agent_id, str) else None,
        context_allowed_tool_names=context_allowed,
        available_tool_names=available_names,
        context_mcp_provider_scopes=(
            getattr(runtime_envelope, "mcp_provider_scopes", ()) or ()
        ),
        context_mcp_scope_unrestricted=bool(
            getattr(runtime_envelope, "mcp_scope_unrestricted", False)
        ),
        is_master=False,
    )
    dynamic_mcp_scope = RuntimeDynamicMCPDiscoveryScopeFactory.create(
        binding_scope,
        runtime_envelope,
    )
    scoped_mcp_provider_keys = frozenset(
        item.provider
        for item in binding_scope.mcp_provider_scopes
        if item.provider and canonical_provider_key(item.provider) not in disabled_providers
    )
    bound_tools = binding_scope.effective_bound_tool_names()
    mcp_allowed_names = (
        set(binding_scope.mcp_allowed_names)
        if binding_scope.mcp_allowed_names is not None
        else None
    )
    bound_tools = runtime_search_bound_tool_names_for_profile(
        available_names,
        tool_profile=(
            runtime_context.tool_profile
            if isinstance(runtime_context.tool_profile, str)
            else None
        ),
        context_allowed_tool_names=context_allowed,
        bound_tool_names=bound_tools,
        mcp_allowed_names=mcp_allowed_names,
    )
    if bound_tools is None:
        scoped_tool_schemas = tool_schemas
        scoped_available_names = set(available_names)
    else:
        scoped_names = set(bound_tools) | set(
            runtime_search_always_loaded_tool_names()
        )
        scoped_tool_schemas = tuple(
            (name, schema)
            for name, schema in tool_schemas
            if str(name) in scoped_names
        )
        scoped_available_names = {
            name for name in available_names if name in scoped_names
        }

    if child_discovery:
        scoped_tool_schemas = tuple(
            (name, schema) for name, schema in scoped_tool_schemas
            if _child_allows_tool(str(name))
        )
        scoped_available_names = {name for name in scoped_available_names if _child_allows_tool(name)}

    if requested_kinds != _CAPABILITY_KINDS:
        def _requested_tool_kind(name: str) -> bool:
            kind = "mcp_tool" if str(name).startswith("mcp__") else "tool"
            return kind in requested_kinds

        scoped_tool_schemas = tuple(
            (name, schema)
            for name, schema in scoped_tool_schemas
            if _requested_tool_kind(str(name))
        )
        scoped_available_names = {
            name for name in scoped_available_names if _requested_tool_kind(name)
        }

    active_user_message = (
        runtime_context.active_user_message
        if isinstance(runtime_context.active_user_message, str)
        else None
    )

    effective_skill_loader = skill_descriptor_loader
    if effective_skill_loader is None and runtime_envelope is not None:
        async def _load_runtime_skill_descriptors() -> Iterable[Any]:
            from packages.core.ai.runtime.skills import (
                runtime_searchable_skill_descriptors_from_tool_kwargs,
            )
            from packages.core.database import async_session

            async with async_session() as skill_db:
                return await runtime_searchable_skill_descriptors_from_tool_kwargs(
                    skill_db,
                    dict(arguments),
                )

        effective_skill_loader = _load_runtime_skill_descriptors

    searchable_skills: list[Any] = []
    skill_matches: list[dict[str, Any]] = []
    skill_catalog_loaded = False
    skill_discovery_error: str | None = None

    # Build the credential-free provider catalog independently from account
    # availability. Semantic provider selection is delegated to the Runtime's
    # inexpensive worker model and returns only exact catalog IDs.
    usable_providers: frozenset[str] | None = None
    from packages.core.ai.runtime.tool_discovery import runtime_server_index
    from packages.core.ai.runtime.integration_provider_matcher import (
        runtime_integration_provider_candidates,
    )

    server_idx: dict[str, dict] | None = runtime_server_index() or None
    if server_idx and (disabled_providers or child_discovery):
        server_idx = {
            key: value for key, value in server_idx.items()
            if canonical_provider_key(key) not in disabled_providers and _child_allows_provider(key)
        }
    explicit_provider_keys = _runtime_explicit_provider_keys(
        provider_keys=(
            (search_request.provider_key,)
            if search_request.provider_key
            else ()
        ),
        exact_names=search_request.exact_names,
    )
    explicit_provider_keys = frozenset(
        key for key in explicit_provider_keys
        if canonical_provider_key(key) not in disabled_providers and _child_allows_provider(key)
    )
    registry_provider_keys = sorted(
        {key for key in scoped_mcp_provider_keys if _child_allows_provider(key)}
        | {
            provider
            for name, _schema in scoped_tool_schemas
            if (provider := runtime_mcp_provider_from_tool_name(str(name)))
        }
        | explicit_provider_keys
    )
    provider_candidates = runtime_integration_provider_candidates(
        server_index=server_idx,
        tool_schemas=scoped_tool_schemas,
        provider_keys=registry_provider_keys,
    )

    async def _load_usable_providers(
        provider_keys: Iterable[str] | None = None,
    ) -> tuple[frozenset[str], float]:
        started = time.monotonic()
        from packages.core.database import async_session
        from packages.core.services.agent_permission_service import (
            resolve_usable_mcp_providers,
        )

        async with async_session() as discovery_db:
            usable = await resolve_usable_mcp_providers(
                discovery_db,
                user_id=user_id,
                entity_id=entity_id,
                provider_keys=(
                    registry_provider_keys if provider_keys is None else list(provider_keys)
                ),
            )
        return usable, (time.monotonic() - started) * 1000

    async def _load_searchable_skills() -> tuple[list[Any], float]:
        started = time.monotonic()
        assert effective_skill_loader is not None
        skills = list(await effective_skill_loader())
        return skills, (time.monotonic() - started) * 1000

    parallel_names: list[str] = []
    parallel_jobs: list[Awaitable[tuple[Any, float]]] = []
    if entity_id:
        parallel_names.append("providers")
        parallel_jobs.append(_load_usable_providers())
    if "skill" in requested_kinds and effective_skill_loader is not None:
        parallel_names.append("skills")
        parallel_jobs.append(_load_searchable_skills())
    semantic_provider_matches: tuple[Any, ...] = ()
    if parallel_jobs:
        parallel_results = await asyncio.gather(
            *parallel_jobs,
            return_exceptions=True,
        )
        for name, result in zip(parallel_names, parallel_results, strict=True):
            if isinstance(result, BaseException):
                if name == "providers":
                    logger.warning(
                        "Tool discovery provider pre-filter failed; continuing without it",
                        exc_info=(type(result), result, result.__traceback__),
                    )
                    usable_providers = None
                elif name == "skills":
                    logger.warning(
                        "Runtime Skill discovery failed; returning Tool/MCP candidates",
                        exc_info=(type(result), result, result.__traceback__),
                    )
                    skill_discovery_error = (
                        "The Skill catalog could not be searched; no Skill was loaded."
                    )
                else:
                    logger.info(
                        "Integration provider matching failed; continuing without an MCP route",
                        exc_info=(type(result), result, result.__traceback__),
                    )
                continue
            value, elapsed_ms = result
            if name == "providers":
                usable_providers = value
                provider_resolution_ms = elapsed_ms
            elif name == "skills":
                searchable_skills = value
                skill_catalog_loaded = True
                skill_search_ms = elapsed_ms

    selection = CapabilitySelection("unassessed")
    assessments: dict[str, dict] = {}
    # Composite-only operations belong to this catalog too. Never resurrect a
    # registered native tool hidden by scope through its composite alias.
    composite_candidates = []
    scoped_schema_by_name = dict(scoped_tool_schemas)
    registered_names = {name for name, _ in tool_schemas}
    if "manor" in scoped_schema_by_name and not child_discovery:
        from packages.core.ai.tools.manor_tool import manor_action_manifests

        composite_candidates = [
            item for item in manor_action_manifests()
            if item["invoke_with"]["arguments"]["action"] not in registered_names
        ]
    if search_request.mode == "semantic" and not child_discovery:
        candidates = [
            {**runtime_tool_manifest(name, schema),
             "description": str((schema.get("function") or {}).get("description") or "")}
            for name, schema in scoped_tool_schemas if not name.startswith("mcp__")
        ]
        candidates.extend(
            {**runtime_skill_manifest(skill), "description": str(getattr(skill, "description", "") or "")}
            for skill in searchable_skills
        )
        # The Manor platform (its actions and Manor's own MCP servers) is
        # searched by manor(action='search') wherever manor is in scope.
        manor_in_scope = "manor" in scoped_schema_by_name
        if "mcp_tool" in requested_kinds:
            candidates.extend(
                {"kind": "provider", "name": provider.provider_key, "description": provider.description}
                for provider in provider_candidates
                if provider.provider_key not in EMBEDDED_MCP_PROVIDERS
                and not (manor_in_scope and provider.provider_key in MANOR_PLATFORM_MCP_PROVIDERS)
            )
        started = time.monotonic()
        selection = rank_runtime_capabilities(
            query=search_request.query, candidates=candidates, queries=search_request.queries,
        )
        provider_match_ms = (time.monotonic() - started) * 1000
        assessments = {item["id"]: item["assessment"] for item in selection.matches}
        semantic_provider_matches = tuple(
            provider
            for provider in provider_candidates if f"provider:{provider.provider_key}" in assessments
        )

    if search_request.mode == "provider":
        resolved_provider_keys = (
            (search_request.provider_key,)
            if search_request.provider_key and _child_allows_provider(search_request.provider_key)
            else ()
        )
    elif search_request.mode == "exact":
        resolved_provider_keys = tuple(sorted(explicit_provider_keys))
    else:
        resolved_provider_keys = tuple(
            match.provider_key for match in semantic_provider_matches
        )
    search_provider_keys = (
        () if search_request.mode == "exact" else resolved_provider_keys
    )

    ranking_started = time.monotonic()
    matches, suppressed_mcp = runtime_search_tool_registry_candidates(
        tool_schemas=scoped_tool_schemas,
        query=search_request.query,
        provider_keys=search_provider_keys,
        provider_selection="inferred" if search_request.mode == "semantic" else "explicit",
        exact_names=search_request.exact_names,
        max_results=search_request.search_pool_size,
        bound_tool_names=bound_tools,
        active_user_message=active_user_message,
        usable_providers=usable_providers,
        server_index=server_idx,
    )
    ranking_ms += (time.monotonic() - ranking_started) * 1000
    if matches:
        availability_started = time.monotonic()
        matches = await runtime_annotate_tool_availability(matches, entity_id, user_id)
        availability_ms += (time.monotonic() - availability_started) * 1000

    live_schema_by_name: dict[str, dict[str, Any]] = {}
    live_account_ids_by_name: dict[str, tuple[str, ...]] = {}
    live_tool_by_name: dict[str, Any] = {}
    if live_mcp_schema_loader is not None and "mcp_tool" in requested_kinds:
        from packages.core.services.official_remote_mcp import (
            OfficialRemoteMCPProvider,
        )

        official_providers = {
            provider.value for provider in OfficialRemoteMCPProvider
            if canonical_provider_key(provider.value) not in disabled_providers
            and _child_allows_provider(provider.value)
        }
        # v2 has already resolved the actor-callable account set. Intersect it
        # before any remote loader work so a bound-but-disconnected provider
        # can be suggested from static metadata without spending a tools/list
        # request or becoming a dynamic grant.
        discoverable_official_providers = (
            official_providers
            if usable_providers is None
            else official_providers & set(usable_providers)
        )
        discovery_providers = _official_live_discovery_providers(
            matches=matches,
            official_providers=discoverable_official_providers,
            provider_keys=resolved_provider_keys,
            exact_names=search_request.exact_names,
        )
        if discovery_providers:
            live_schema_started = time.monotonic()
            try:
                discovered_by_provider = await live_mcp_schema_loader(
                    discovery_providers
                )
            except Exception:
                logger.warning(
                    "Live official MCP schema discovery failed; using fallback schemas",
                    exc_info=True,
                )
                discovered_by_provider = {}
            live_schema_ms += (time.monotonic() - live_schema_started) * 1000
            if discovered_by_provider:
                authoritative_providers = set(discovered_by_provider)
                merged_schemas = [
                    (name, schema)
                    for name, schema in scoped_tool_schemas
                    if runtime_mcp_provider_from_tool_name(str(name))
                    not in authoritative_providers
                ]
                for tools in discovered_by_provider.values():
                    for tool in tools:
                        resolved = _live_mcp_tool_name_and_schema(tool)
                        if resolved is None:
                            continue
                        name, schema = resolved
                        if integration_provider_for_tool(name) in disabled_providers or not _child_allows_tool(name):
                            continue
                        if not dynamic_mcp_scope.allows(
                            name,
                            provider=getattr(tool, "provider", None),
                            action=getattr(tool, "action", None),
                        ):
                            continue
                        merged_schemas.append((name, schema))
                        live_schema_by_name[name] = schema
                        live_tool_by_name[name] = tool
                        live_account_ids_by_name[name] = tuple(
                            str(account_id)
                            for account_id in getattr(tool, "account_ids", ())
                            if str(account_id or "").strip()
                        )
                live_bound_tools = (
                    None
                    if bound_tools is None
                    else set(bound_tools) | set(live_schema_by_name)
                )
                ranking_started = time.monotonic()
                matches, suppressed_mcp = runtime_search_tool_registry_candidates(
                    tool_schemas=tuple(merged_schemas),
                    query=search_request.query,
                    provider_keys=search_provider_keys,
                    provider_selection="inferred" if search_request.mode == "semantic" else "explicit",
                    exact_names=search_request.exact_names,
                    max_results=search_request.search_pool_size,
                    bound_tool_names=live_bound_tools,
                    active_user_message=active_user_message,
                    usable_providers=usable_providers,
                    server_index=server_idx,
                )
                ranking_ms += (time.monotonic() - ranking_started) * 1000
                if matches:
                    availability_started = time.monotonic()
                    matches = await runtime_annotate_tool_availability(
                        matches,
                        entity_id,
                        user_id,
                    )
                    availability_ms += (
                        time.monotonic() - availability_started
                    ) * 1000
                for match in matches:
                    name = str(match.get("name") or "")
                    schema = live_schema_by_name.get(name)
                    if schema is not None:
                        match["schema"] = schema
                    live_tool = live_tool_by_name.get(name)
                    if live_tool is not None:
                        match["requires_explicit_account"] = bool(
                            match.get("requires_explicit_account")
                            or getattr(
                                live_tool,
                                "requires_explicit_account",
                                False,
                            )
                        )
                    supported_account_ids = live_account_ids_by_name.get(name, ())
                    if supported_account_ids:
                        account_options = match.get("account_options")
                        if isinstance(account_options, list):
                            filtered_options = [
                                option
                                for option in account_options
                                if str(option.get("id") or "")
                                in supported_account_ids
                            ]
                            match["account_options"] = filtered_options
                        else:
                            filtered_options = []
                        match["available_account_ids"] = list(supported_account_ids)
                        match["default_account_id"] = (
                            filtered_options[0].get("id")
                            if filtered_options
                            else supported_account_ids[0]
                        )

    if search_request.mode == "semantic" and selection.status in _SELECTED_STATUSES:
        # A provider assessment is not an assessment of each discovered action.
        # Retain its exact actions as explicitly unassessed candidates for the
        # parent model; select native tools independently, including zero lexical hits.
        matches = [match for match in matches if str(match.get("name", "")).startswith("mcp__")]
        selected_native = [
            {**runtime_tool_manifest(name, schema), "assessment": assessments[f"tool:{name}"]}
            for name, schema in scoped_tool_schemas if f"tool:{name}" in assessments
        ]
        if selected_native:
            matches.extend(await runtime_annotate_tool_availability(selected_native, entity_id, user_id))
    selected_composites = [
        {**item, "schema": scoped_schema_by_name["manor"],
         "assessment": assessments.get(item["catalog_id"], {
             "source": "catalog_lookup", "fit": "unassessed",
             "reason": "Exact operation lookup; execution remains subject to current policy.",
         })}
        for item in composite_candidates
        if item["catalog_id"] in assessments or (
            search_request.mode == "exact"
            and f"manor.{item['invoke_with']['arguments']['action']}" in search_request.exact_names
        )
    ]
    if selected_composites:
        matches.extend(await runtime_annotate_tool_availability(selected_composites, entity_id, user_id))
    visible_matches = list(matches)

    if skill_catalog_loaded and search_request.mode != "provider":
        skill_ranking_started = time.monotonic()
        skill_matches = runtime_search_skill_candidates(
            skills=searchable_skills,
            query=search_request.query,
            exact_selectors=search_request.exact_names,
            max_results=search_request.search_pool_size,
            include_companion_skills=True,
        )
        skill_search_ms += (time.monotonic() - skill_ranking_started) * 1000
        if search_request.mode == "semantic" and selection.status in _SELECTED_STATUSES:
            skill_matches = [
                {**runtime_skill_manifest(skill), "assessment": assessments[f"skill:{skill.id}"]}
                for skill in searchable_skills if f"skill:{skill.id}" in assessments
            ]

    companion_tool_matches: list[dict] = []
    if searchable_skills and (
        not search_request.query.strip()
        or (search_request.mode == "semantic" and skill_matches)
    ):
        companion_names = runtime_companion_tool_names(
            skills=searchable_skills,
            matched_tool_names=(
                str(match.get("name") or "")
                for match in matches
                if match.get("available") is True
            ),
            available_tool_names=(name for name, _schema in scoped_tool_schemas),
        )
        schema_by_name = {name: schema for name, schema in scoped_tool_schemas}
        companion_tool_matches = [
            runtime_tool_manifest(name, schema_by_name[name])
            for name in companion_names
            if name in schema_by_name
        ]
        if companion_tool_matches:
            availability_started = time.monotonic()
            companion_tool_matches = await runtime_annotate_tool_availability(
                companion_tool_matches,
                entity_id,
                user_id,
            )
            availability_ms += (time.monotonic() - availability_started) * 1000
            companion_tool_matches = [
                match for match in companion_tool_matches
                if match.get("available") is True
            ]

    primary_visible_matches = (
        []
        if (
            search_request.mode == "provider"
            and not search_request.query.strip()
            and companion_tool_matches
        )
        else list(visible_matches)
    )
    if skill_matches:
        primary_visible_matches = [
            match
            for match in primary_visible_matches
            if str(match.get("name") or "") != "invoke_skill"
        ]
    if search_request.mode == "semantic" and selection.status in _SELECTED_STATUSES:
        # Ranked order first; actions of a ranked provider follow it.
        visible_matches = sorted(primary_visible_matches + skill_matches, key=lambda match: (
            (match.get("assessment") or {}).get("rank", len(selection.matches)),
            {"direct": 0, "partial": 1, "supporting": 2}.get((match.get("assessment") or {}).get("fit"), 3),
            match.get("available") is False,
        ))[:search_request.max_results]
    else:
        visible_matches = runtime_merge_capability_matches(
            tool_matches=primary_visible_matches, skill_matches=skill_matches,
            query=search_request.query, max_results=search_request.max_results,
        )
    visible_tool_names = {
        str(match.get("name") or "")
        for match in visible_matches
        if match.get("kind") != "skill"
    }
    for match in companion_tool_matches:
        if match["name"] not in visible_tool_names:
            visible_matches.append(match)
            visible_tool_names.add(match["name"])
    companion_matches = runtime_search_companion_skill_candidates(
        skills=searchable_skills,
        tool_matches=visible_matches,
    )
    visible_skill_by_id = {
        str(match.get("skill_id") or ""): match
        for match in visible_matches
        if match.get("kind") == "skill" and str(match.get("skill_id") or "")
    }
    for companion in companion_matches:
        skill_id = str(companion.get("skill_id") or "")
        existing = visible_skill_by_id.get(skill_id)
        if existing is not None:
            existing["capability_role"] = "companion"
            existing["companion_for"] = companion["companion_for"]
            existing["load_before_use"] = True
            continue
        visible_matches.append(companion)
        visible_skill_by_id[skill_id] = companion

    if semantic_provider_matches:
        # The provider matcher understands the requested execution route;
        # lexical topic overlap must not put an unrelated Skill ahead of it.
        # Rank only companions backed by the available, authorized tools above.
        preferred_skill_ids = {
            str(match.get("skill_id") or "")
            for match in companion_matches
            if any(
                runtime_mcp_provider_from_tool_name(name) in resolved_provider_keys
                for name in match.get("companion_for", ())
            )
        }
        visible_matches.sort(key=lambda match: (
            0 if match.get("kind") == "skill"
            and match.get("skill_id") in preferred_skill_ids else 1
        ))

    # Group only actual final callable tool results. Companion tools are
    # intentionally appended outside the search-result slot budget.
    servers_summary: list[dict] | None = None
    if visible_matches:
        server_lookup = server_idx or {}
        grouped: dict[str, dict] = {}
        order: list[str] = []
        for match in visible_matches:
            name = str(match.get("name") or "")
            provider = runtime_mcp_provider_from_tool_name(name)
            if not provider:
                continue
            if provider not in grouped:
                entry = server_lookup.get(provider) or {}
                grouped[provider] = {
                    "key": provider,
                    "name": entry.get("name") or provider,
                    "matched_tools": 0,
                    "top_tools": [],
                    "total_tools": entry.get("tool_count"),
                }
                order.append(provider)
            grouped[provider]["matched_tools"] += 1
            if len(grouped[provider]["top_tools"]) < 3:
                grouped[provider]["top_tools"].append(name)
        servers_summary = [grouped[key] for key in order] or None

    not_in_scope: list[dict] = []
    envelope_profile = getattr(runtime_envelope, "profile", None)
    if (
        search_request.mode == "exact"
        and bound_tools is not None
        and not child_discovery
        and "tool" in requested_kinds
        # External audiences never learn about tools outside their scope.
        and envelope_profile not in {
            RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
        }
    ):
        from packages.core.ai.runtime.tool_discovery import (
            BLOCKED_BY_POLICY_REASON,
            runtime_not_in_scope_entries,
        )
        from packages.core.ai.runtime.tool_visibility import HIDDEN_RUNTIME_TOOLS

        can_acquire = context_allowed is not None and "acquire_capabilities" in context_allowed
        candidate_kwargs = {
            "requested_names": tuple(
                name for name in search_request.exact_names
                if str(name or "").strip().lower() not in HIDDEN_RUNTIME_TOOLS
                and not runtime_shadowed_file_generation_tool(
                    str(name or "").strip().lower(),
                    bound_tool_names=bound_tools,
                    available_tool_names=available_names,
                )
            ),
            "registered_tool_names": tuple(name for name, _schema in tool_schemas),
            "scoped_tool_names": tuple(name for name, _schema in scoped_tool_schemas),
            "blocked_tool_names": effective_scope.blocked_tool_names,
            "platform_routes": _runtime_manor_routes(
                in_scope="manor" in scoped_schema_by_name,
                in_workspace=bool(runtime_context.workspace_id),
            ),
        }
        not_in_scope = runtime_not_in_scope_entries(**candidate_kwargs, can_acquire=can_acquire)
        if any(entry["reason"] != BLOCKED_BY_POLICY_REASON for entry in not_in_scope):
            # Approval deny rules: loaded once, only when something is reported.
            envelope_metadata = getattr(runtime_envelope, "metadata", None)
            policy_blocks, policy_known = await _runtime_approval_policy_deny_matcher(
                entity_id=runtime_context.entity_id or entity_id,
                workspace_id=runtime_context.workspace_id,
                user_id=runtime_context.user_id or user_id,
                room_id=(
                    str(envelope_metadata.get("room_id") or "") or None
                    if isinstance(envelope_metadata, dict)
                    else None
                ),
            )
            if policy_blocks is not None or not policy_known:
                not_in_scope = runtime_not_in_scope_entries(
                    **candidate_kwargs,
                    can_acquire=can_acquire and policy_known,
                    policy_blocks=policy_blocks,
                )
    discovery_user_id = runtime_context.user_id or user_id
    discovery_entity_id = runtime_context.entity_id or entity_id
    if discovery_user_id and discovery_entity_id and visible_matches:
        from packages.core.services.runtime_authorization.collaborator import (
            collaborator_tool_offered,
            is_runtime_collaborator,
            workspace_bound_mcp_tool,
        )

        try:
            # The gate cached this for the search_tools call itself.
            collaborator = await is_runtime_collaborator(discovery_user_id, discovery_entity_id)
            bound_mcp: set[str] = set()
            invocable_skills: set[str] = set()
            mcp_names = {
                name for match in visible_matches
                if (name := str(match.get("name") or "")).startswith("mcp__")
            }
            skill_ids = {
                skill_id for match in visible_matches
                if match.get("kind") == "skill" and (skill_id := str(match.get("skill_id") or ""))
            }
            discovery_workspace_id = runtime_context.workspace_id
            if collaborator and (mcp_names or skill_ids) and isinstance(discovery_workspace_id, str):
                from packages.core.database import async_session
                from packages.core.services.collaborator_scope import skills_invocable_in_workspace

                async with async_session() as collaborator_db:
                    for name in sorted(mcp_names):
                        if await workspace_bound_mcp_tool(
                            collaborator_db, entity_id=discovery_entity_id,
                            workspace_id=discovery_workspace_id, tool_name=name,
                        ):
                            bound_mcp.add(name)
                    # The same rule as the gate's check of an invoked skill_id.
                    invocable_skills = await skills_invocable_in_workspace(
                        collaborator_db, skill_ids,
                        entity_id=discovery_entity_id, workspace_id=discovery_workspace_id,
                    )
        except Exception:
            # Exposure only: the runtime authorization gate still decides each call.
            logger.warning("Collaborator check unavailable for tool discovery", exc_info=True)
            collaborator = False
        if collaborator:
            # Offer an external collaborator only tools their gate can admit;
            # authorization still decides each concrete call.
            visible_matches = [
                match for match in visible_matches
                if (
                    str(match.get("skill_id") or "") in invocable_skills
                    if match.get("kind") == "skill"
                    else str(match.get("name") or "") in bound_mcp
                    or collaborator_tool_offered(str(match.get("name") or ""))
                )
            ]
    payload = runtime_search_tools_payload(
        not_in_scope=not_in_scope,
        matches=visible_matches,
        mode=search_request.mode,
        query=search_request.query,
        provider_keys=resolved_provider_keys,
        suppressed_mcp=suppressed_mcp,
        total_tool_count=(
            total_tool_count
            if bound_tools is None and total_tool_count is not None
            else len(scoped_available_names | set(live_schema_by_name))
        ),
        servers=servers_summary,
        total_skill_count=(
            len(searchable_skills)
            if skill_catalog_loaded
            else None
        ),
    )
    skill_readiness = await _runtime_skill_match_readiness(
        [match for match in payload["matches"] if match.get("kind") == "skill"],
        descriptors_by_id={str(getattr(skill, "id", "") or ""): skill for skill in searchable_skills},
        allowed_tool_names=context_allowed,
        runtime_envelope=runtime_envelope,
        registered_tool_names=available_names,
        provider_readiness_known=usable_providers is not None,
        resolved_provider_keys=frozenset(registry_provider_keys),
        usable_providers=usable_providers or frozenset(),
        load_usable_providers=_load_usable_providers if entity_id else None,
    )
    for match in payload["matches"]:
        match.setdefault("assessment", {
            "source": "catalog_lookup", "fit": "unassessed",
            "reason": "An exact name or catalog candidate is not proof of suitability. Inspect its description and schema.",
        })
        match["availability"] = {
            "source": "runtime_scope", "state": (
                "unavailable" if match.get("available") is False else INVOCABLE_UNDER_POLICY_STATE
            ),
            "execution_verified": False,
            "dependencies_verified": False,
        }
        readiness = skill_readiness.get(id(match))
        if readiness is not None and match["availability"]["state"] == INVOCABLE_UNDER_POLICY_STATE:
            match["availability"].update(readiness)
    if skill_readiness:
        # Not hidden: Skills that cannot run here follow every invocable match.
        payload["matches"].sort(key=lambda match: (
            match.get("kind") == "skill"
            and match["availability"]["state"] != INVOCABLE_UNDER_POLICY_STATE
        ))
    payload["discovery"] = {
        "assessment_status": selection.status if search_request.mode == "semantic" else "not_requested",
        "examined_candidates": selection.examined, "next_cursor": selection.next_cursor,
        "catalog_complete": cursor == 0 and selection.next_cursor is None and not skill_discovery_error and len(searchable_skills) < 200,
        "assessment_failure": selection.failure,
        "capability_availability": "not_established", "action_executed": False,
        "instruction": "Discovery is not a business execution. Try suitable callable tools. No match, a matcher failure, or one disconnected provider does not prove the task impossible. Continue paging when next_cursor is present.",
    }
    if search_request.mode == "semantic" and "manor" in scoped_schema_by_name and not child_discovery:
        payload["discovery"]["manor_platform"] = (
            "Manor platform actions (workspaces, tasks, knowledge base, agents, scheduled jobs, Manor "
            "Calendar, saving Knowledge locally) are not listed here: find them with manor(action='search')."
        )
    if semantic_provider_matches:
        payload["provider_resolution"] = {
            "source": "model_assessment", "selection": "candidate_route",
            "matches": [{"provider_key": match.provider_key,
                         "assessment": assessments[f"provider:{match.provider_key}"]}
                        for match in semantic_provider_matches],
        }
        if "details" in payload:
            payload["details"]["provider_resolution"] = payload["provider_resolution"]
    if (
        search_request.mode == "semantic" and not child_discovery
        and selection.status not in _SELECTED_STATUSES
    ):
        # No keyword hit must not hide zero-keyword native/composite routes.
        # These are scoped descriptors for the parent to inspect, not grants or
        # automatically executed alternatives: the leading candidates with
        # short descriptions, the rest by id (the full page with 600-char
        # descriptions was 25-43k characters of what the model read).
        page, _ = capability_candidate_page(candidates, query=search_request.query, cursor=cursor)
        payload["discovery"]["candidates"] = [
            {**item, "description": item["description"][:FALLBACK_DESCRIPTION_CHARS]}
            for item in page[:FALLBACK_DESCRIBED_CANDIDATES]
        ]
        if len(page) > FALLBACK_DESCRIBED_CANDIDATES:
            payload["discovery"]["other_candidate_ids"] = [
                item["id"] for item in page[FALLBACK_DESCRIBED_CANDIDATES:]
            ]
            payload["discovery"]["other_candidates_hint"] = (
                "Not described here. To read a tool, search again with mode exact and "
                "exact_names set to the part after 'tool:'; otherwise use other English keywords."
            )
    if skill_discovery_error:
        payload["skill_discovery_error"] = skill_discovery_error
    if (
        any(match.get("kind") == "skill" for match in visible_matches)
        and "invoke_skill" in available_names
        and "invoke_skill" not in payload.get("loaded_tools", [])
    ):
        payload.setdefault("loaded_tools", []).append("invoke_skill")
    if runtime_envelope is not None:
        from packages.core.ai.runtime.dynamic_mcp import (
            RuntimeDynamicMCPAccountRegistrySnapshotFactory,
        )
        from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrant

        grants: list[RuntimeDiscoveredToolGrant] = []
        for raw_name in payload.get("loaded_tools", []):
            name = str(raw_name or "").strip()
            tool = live_tool_by_name.get(name)
            if tool is None:
                if name.startswith("mcp__"):
                    grants.append(RuntimeDiscoveredToolGrant(name=name))
                continue
            raw_effect = getattr(tool, "effect", None)
            effect = getattr(raw_effect, "value", raw_effect)
            grants.append(RuntimeDiscoveredToolGrant(
                name=name,
                effect=(str(effect) if effect else None),
                account_ids=live_account_ids_by_name.get(name, ()),
                requires_explicit_account=bool(
                    getattr(tool, "requires_explicit_account", False)
                ),
                supports_all_accounts=bool(
                    getattr(tool, "supports_all_accounts", True)
                ),
                incomplete_account_ids=tuple(
                    str(account_id)
                    for account_id in getattr(tool, "incomplete_account_ids", ())
                    if str(account_id or "").strip()
                ),
                account_registry_snapshot=(
                    RuntimeDynamicMCPAccountRegistrySnapshotFactory.from_discovered(
                        tool
                    )
                ),
            ))
        if grants:
            runtime_envelope.discovered_tool_grants.grant(grants)
    logger.info(
        "tool_discovery.performance %s",
        {
            "version": "v3_structured_provider",
            "total_ms": round((time.monotonic() - performance_started) * 1000, 1),
            "provider_resolution_ms": round(provider_resolution_ms, 1),
            "provider_match_ms": round(provider_match_ms, 1),
            "ranking_ms": round(ranking_ms, 1),
            "availability_ms": round(availability_ms, 1),
            "live_schema_ms": round(live_schema_ms, 1),
            "skill_search_ms": round(skill_search_ms, 1),
            "provider_count": len(registry_provider_keys),
            "matched_provider_count": len(resolved_provider_keys),
            "static_tool_count": len(scoped_tool_schemas),
            "live_tool_count": len(live_schema_by_name),
            "searchable_skill_count": len(searchable_skills),
            "visible_match_count": len(visible_matches),
            "loaded_tool_count": len(payload.get("loaded_tools") or ()),
        },
    )
    return json.dumps(payload, ensure_ascii=False)
