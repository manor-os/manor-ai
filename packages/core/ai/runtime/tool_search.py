from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

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
from packages.core.ai.runtime.tool_discovery import (
    runtime_mcp_provider_from_tool_name,
    runtime_mcp_provider_text_score,
    runtime_prepare_search_tools_request,
    runtime_search_tools_payload,
    runtime_server_query_score,
    runtime_tool_query_score,
    runtime_tool_search_scope,
)

logger = logging.getLogger(__name__)


_TOOL_MANIFEST_DESCRIPTION_CHARS = 260
_TOOL_MANIFEST_PARAMETER_LIMIT = 12


def runtime_search_tools_schema() -> dict:
    """Return the built-in search_tools schema owned by Runtime Harness."""

    return {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Search tools by keyword. Use \"select:tool_name1,tool_name2\" for "
                'exact match, or "browse_server:key" for one server\'s tools.'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": 'Search query. Use "select:name1,name2" for exact match.',
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max tools to return (default 5, hard cap 8).",
                    },
                },
                "required": ["query"],
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
        "name": name,
        "description": description[:_TOOL_MANIFEST_DESCRIPTION_CHARS],
        "parameters": list(params.keys())[:_TOOL_MANIFEST_PARAMETER_LIMIT],
    }
    if len(description) > _TOOL_MANIFEST_DESCRIPTION_CHARS:
        manifest["description_truncated"] = True
    return manifest


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
    query: str,
    matches: Iterable[Mapping[str, Any]],
    official_providers: set[str],
    scoped_providers: Iterable[str] = (),
    server_index: Mapping[str, Mapping[str, Any]] | None = None,
) -> frozenset[str]:
    """Resolve actor-bound official providers worth probing for live schemas.

    Exact selectors and provider names narrow discovery. Otherwise, a local
    match is considered sufficient and avoids remote I/O; only a local miss
    fans out across the semantic provider scope. The scope already comes from
    the active Runtime envelope, so this is complete for the Agent without
    searching unbound providers.
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
    normalized = str(query or "").strip().lower()
    if normalized.startswith("select:"):
        for selector in normalized[7:].split(","):
            provider = runtime_mcp_provider_from_tool_name(selector.strip())
            if provider in official_providers:
                providers.add(provider)
    elif normalized.startswith("browse_server:"):
        provider = normalized.split(":", 1)[1].strip().split(None, 1)[0]
        if provider in official_providers:
            providers.add(provider)
    else:
        scoped = {
            str(provider).strip()
            for provider in scoped_providers
            if str(provider).strip() in official_providers
        }
        scored = []
        for provider in scoped:
            entry = (server_index or {}).get(provider)
            score = (
                runtime_server_query_score(dict(entry), normalized)
                if entry is not None
                else runtime_mcp_provider_text_score(provider, normalized)
            )
            if score > 0:
                scored.append((score, provider))
        if scored:
            providers.update(provider for _score, provider in scored)
        elif not matches:
            providers.update(scoped)
    return frozenset(providers)


def runtime_select_tool_candidates(selector: str, tool_names: Iterable[str]) -> list[str]:
    """Resolve exact ``select:`` names plus MCP short names."""
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


def runtime_search_tool_candidates(
    *,
    tool_schemas: Iterable[tuple[str, dict]],
    query: str,
    max_results: int = 5,
    bound_tool_names: set[str] | None = None,
    active_user_message: str | None = None,
    always_loaded_tool_names: Iterable[str] = (),
    usable_providers: frozenset[str] | None = None,
    server_index: dict[str, dict] | None = None,
    intent_path_boosts: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Search tool manifests and return MCP providers suppressed by intent.

    ``usable_providers`` (tool_discovery_v2 A1) and ``server_index``
    (tool_discovery_v2 A2) both default to ``None``, which preserves exact
    v1 behavior: no provider pre-filtering and no server-level score floor.
    ``intent_path_boosts`` (tool_discovery_v2 A3 intent-path memory) is an
    optional provider->boost map, each value capped at +9 so memory can
    only nudge among near-ties — it never beats a real keyword+alias match
    (provider_query_score contributes at least 100 once nonzero, since
    runtime_mcp_provider_text_score's minimum nonzero value is 10, and it's
    weighted x10 here).
    """
    tool_map = {str(name): schema for name, schema in tool_schemas}
    always_loaded = set(always_loaded_tool_names)
    query_lower = query.lower()

    scope = runtime_tool_search_scope(
        tool_names=tool_map.keys(),
        query=query_lower,
        active_user_message=active_user_message,
    )
    chrome_skill_match = _runtime_chrome_skill_invocation_match(
        tool_map=tool_map,
        bound_tool_names=bound_tool_names,
        always_loaded_tool_names=always_loaded,
        active_user_message=active_user_message,
        scope=scope,
    )
    if chrome_skill_match is not None:
        return [chrome_skill_match], []

    # B1: browse_server:<key> lists one server's tools, bypassing the
    # per-provider dedup applied below (the whole point of browsing one
    # server is to see many of its tools). Gated on server_index is not
    # None (only passed by the handler when tool_discovery_v2 is on) so
    # the convention doesn't exist at all when the flag is off — flag-off
    # callers get byte-identical v1 behavior even if a query happens to
    # start with this literal prefix.
    if server_index is not None and query_lower.startswith("browse_server:"):
        remainder = query[len("browse_server:"):].strip()
        parts = remainder.split(None, 1)
        server_key = (parts[0] if parts else "").strip().lower()
        extra_terms = parts[1] if len(parts) > 1 else ""
        if usable_providers is not None and server_key not in usable_providers:
            return [], [{
                "server_key": server_key,
                "reason": "not_usable",
                "matched_tools": [],
            }]
        server_tools = [
            (name, schema) for name, schema in tool_map.items()
            if runtime_mcp_provider_from_tool_name(name) == server_key
        ]
        if not server_tools:
            return [], [{
                "server_key": server_key,
                "reason": "unknown_server",
                "similar_servers": sorted(
                    {runtime_mcp_provider_from_tool_name(n) or ""
                     for n in tool_map
                     if server_key[:4] and server_key[:4] in n}
                )[:3],
            }]
        scored = sorted(
            server_tools,
            key=lambda item: (
                runtime_tool_query_score(
                    item[0],
                    (item[1].get("function", {}) or {}).get("description", ""),
                    extra_terms,
                ) if extra_terms else 0,
                item[0],
            ),
            reverse=True,
        )
        return (
            [runtime_tool_manifest(name, schema)
             for name, schema in scored[:max_results]],
            [],
        )

    if query_lower.startswith("select:"):
        names = [name.strip() for name in query_lower[7:].split(",") if name.strip()]
        results: list[dict] = []
        selected: set[str] = set()
        suppressed_mcp: list[dict] = []
        requested_tool_names = _runtime_selected_tool_names(names, tool_map.keys())
        _append_preferred_chrome_tool_matches(
            results=results,
            selected=selected,
            tool_map=tool_map,
            bound_tool_names=bound_tool_names,
            always_loaded_tool_names=always_loaded,
            active_user_message=active_user_message,
            scope=scope,
            max_results=min(max_results, 5) if scope.preferred_chrome_tool_names else max_results,
        )
        _append_selected_chrome_support_tool_matches(
            results=results,
            selected=selected,
            requested_tool_names=requested_tool_names,
            tool_map=tool_map,
            bound_tool_names=bound_tool_names,
            always_loaded_tool_names=always_loaded,
            active_user_message=active_user_message,
            scope=scope,
            max_results=max_results,
        )
        _append_preferred_chrome_tool_matches(
            results=results,
            selected=selected,
            tool_map=tool_map,
            bound_tool_names=bound_tool_names,
            always_loaded_tool_names=always_loaded,
            active_user_message=active_user_message,
            scope=scope,
            max_results=max_results,
        )
        for requested_name in names:
            for name in runtime_select_tool_candidates(requested_name, tool_map.keys()):
                if name in selected:
                    continue
                if _chrome_select_tool_is_default_path_bypass(name, scope):
                    continue
                if not scope.mcp_tool_allowed(name):
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
                provider = runtime_mcp_provider_from_tool_name(name)
                # Review #4: v1-parity visibility — select: is an explicit
                # request for one exact tool by name. Do NOT hard-gate on
                # usable_providers here (that regressed an unconnected
                # provider's select: from "shown, marked unavailable" to a
                # bare no-match). Downstream availability annotation
                # (handler-level, after this function returns) still marks
                # it unavailable and surfaces it via unavailable_mcp with
                # the connect hint, exactly like v1. browse_server:'s hard
                # gate is intentionally different and untouched — browsing
                # implies "show me this whole server," where a not_usable
                # answer is itself the meaningful response.
                if (
                    not provider
                    and not scope.first_party_tool_allowed(name, active_user_message)
                ):
                    continue
                if provider and not scope.provider_allowed(provider):
                    suppressed_mcp.append({
                        "server_key": provider,
                        "reason": "outside_active_user_intent",
                        "matched_tools": [name],
                    })
                    continue
                results.append(runtime_tool_manifest(name, schema))
                selected.add(name)
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
        if not results and scope.preferred_chrome_tool_names:
            _append_preferred_chrome_tool_matches(
                results=results,
                selected=selected,
                tool_map=tool_map,
                bound_tool_names=bound_tool_names,
                always_loaded_tool_names=always_loaded,
                active_user_message=active_user_message,
                scope=scope,
                max_results=max_results,
            )
        return results, suppressed_mcp

    active_scores = scope.active_provider_scores
    scored_non_mcp: list[tuple[int, str]] = []
    mcp_groups: dict[str, list[tuple[int, str]]] = {}
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
            if usable_providers is not None and provider not in usable_providers:
                continue  # A1: unusable providers never enter the scoring pool
            if not scope.mcp_tool_allowed(name):
                continue
            mcp_groups.setdefault(provider, []).append((score, name))
            continue
        if not scope.first_party_tool_allowed(name, active_user_message):
            continue

        if score > 0:
            scored_non_mcp.append((score, name))

    suppressed_mcp: list[dict] = []
    mcp_candidates: list[tuple[int, str, list[tuple[int, str]]]] = []

    for provider, tool_scores in mcp_groups.items():
        provider_query_score = runtime_mcp_provider_text_score(provider, query_lower)
        if server_index is not None:
            entry = server_index.get(provider)
            if entry is not None:
                provider_query_score = max(
                    provider_query_score,
                    runtime_server_query_score(entry, query_lower),
                )
        best_tool_score = max((score for score, _ in tool_scores), default=0)

        if not scope.provider_allowed(provider):
            if provider_query_score > 0 or best_tool_score > 0:
                suppressed_mcp.append({
                    "server_key": provider,
                    "reason": "outside_active_user_intent",
                    "matched_tools": [
                        name for score, name in sorted(
                            tool_scores,
                            key=lambda item: (item[0], item[1]),
                            reverse=True,
                        )[:3]
                        if score > 0
                    ],
                })
            continue

        if provider_query_score <= 0 and best_tool_score <= 0:
            continue

        boost = 0.0
        if intent_path_boosts:
            boost = min(float(intent_path_boosts.get(provider, 0.0)), 9.0)
        provider_score = (
            active_scores.get(provider, 0) * 100
            + provider_query_score * 10
            + max(best_tool_score, 0)
            + boost
        )
        mcp_candidates.append((provider_score, provider, tool_scores))

    ranked: list[tuple[int, str]] = []
    ranked.extend(scored_non_mcp)
    for provider_score, _provider, tool_scores in sorted(
        mcp_candidates,
        key=lambda item: (item[0], item[1]),
        reverse=True,
    ):
        positive_tools = [
            (score, name)
            for score, name in sorted(
                tool_scores,
                key=lambda item: (item[0], item[1]),
                reverse=True,
            )
            if score > 0
        ]
        if not positive_tools:
            positive_tools = [max(tool_scores, key=lambda item: item[1])]
        for tool_score, name in positive_tools:
            ranked.append((provider_score + max(tool_score, 0), name))

    ranked.sort(key=lambda item: item[0], reverse=True)
    selected: list[str] = []
    selected_set: set[str] = set()
    seen_mcp_providers: set[str] = set()

    for _, name in ranked:
        provider = runtime_mcp_provider_from_tool_name(name)
        if provider and provider in seen_mcp_providers:
            if not (
                scope.restrict_social_tools
                and name in scope.allowed_social_tool_names
            ):
                continue
        selected.append(name)
        selected_set.add(name)
        if provider:
            seen_mcp_providers.add(provider)
        if len(selected) >= max_results:
            break

    if scope.preferred_chrome_tool_names:
        for name in scope.preferred_chrome_tool_names:
            if len(selected) >= max_results:
                break
            if name in selected_set or name not in tool_map:
                continue
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
            provider = runtime_mcp_provider_from_tool_name(name)
            if provider and not scope.provider_allowed(provider):
                continue
            if not provider and not scope.first_party_tool_allowed(
                name,
                active_user_message,
            ):
                continue
            selected.append(name)
            selected_set.add(name)

    for _, name in ranked:
        if len(selected) >= max_results:
            break
        if name in selected_set:
            continue
        selected.append(name)
        selected_set.add(name)

    return [runtime_tool_manifest(name, tool_map[name]) for name in selected], suppressed_mcp


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


def _append_selected_chrome_support_tool_matches(
    *,
    results: list[dict],
    selected: set[str],
    requested_tool_names: list[str],
    tool_map: dict[str, dict],
    bound_tool_names: set[str] | None,
    always_loaded_tool_names: set[str],
    active_user_message: str | None,
    scope: Any,
    max_results: int,
) -> None:
    if not scope.chrome_local_browser:
        return
    for name in requested_tool_names:
        if len(results) >= max_results:
            break
        if runtime_mcp_provider_from_tool_name(name) != "chrome_knowledge_local":
            continue
        _append_tool_match_if_allowed(
            results=results,
            selected=selected,
            name=name,
            tool_map=tool_map,
            bound_tool_names=bound_tool_names,
            always_loaded_tool_names=always_loaded_tool_names,
            active_user_message=active_user_message,
            scope=scope,
        )


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
        "mcp__chrome__activate_tab",
        "mcp__chrome__switch_tab",
    }


def runtime_search_tool_registry_candidates(
    *,
    tool_schemas: Iterable[tuple[str, dict]],
    query: str,
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
        max_results=max_results,
        bound_tool_names=bound_tool_names,
        active_user_message=active_user_message,
        always_loaded_tool_names=runtime_search_always_loaded_tool_names(),
        usable_providers=usable_providers,
        server_index=server_index,
        intent_path_boosts=intent_path_boosts,
    )


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
) -> str:
    """Execute the built-in search_tools contract against a registry snapshot."""

    tool_schemas = tuple(tool_schemas)
    available_names = tuple(str(name) for name in available_tool_names)
    search_request = runtime_prepare_search_tools_request(
        query=arguments.get("query", ""),
        max_results=arguments.get("max_results"),
    )
    if not search_request.ok:
        return json.dumps({"error": search_request.error})

    runtime_context = runtime_tool_call_context_from_kwargs(dict(arguments))
    runtime_envelope = runtime_context.runtime_envelope
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
        if item.provider
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

    active_user_message = (
        runtime_context.active_user_message
        if isinstance(runtime_context.active_user_message, str)
        else None
    )

    # tool_discovery_v2 (A1 pre-filter + A2 server scoring): resolve the flag
    # and the usable-provider set once per search call. No `db` is threaded
    # into this handler (unlike the plan's pseudocode assumption) — the
    # availability annotation below opens its own session the same way, so
    # we mirror that pattern here rather than adding a new db parameter.
    # Any failure degrades both params to None = exact v1 behavior.
    v2_enabled = False
    usable_providers: frozenset[str] | None = None
    server_idx: dict[str, dict] | None = None
    intent_path_boosts: dict[str, float] | None = None
    try:
        if entity_id:
            from packages.core.database import async_session
            from packages.core.services.feature_flags import is_enabled

            async with async_session() as flag_db:
                v2_enabled = await is_enabled(
                    flag_db, "tool_discovery_v2",
                    entity_id=entity_id, user_id=user_id, fallback=False,
                )
                if v2_enabled:
                    from packages.core.services.agent_permission_service import (
                        resolve_usable_mcp_providers,
                    )
                    # Derive provider keys from THIS handler's own schema
                    # view rather than importing the mcp_builtin catalog
                    # from the (non-runtime) permission service — that
                    # service is scanned by
                    # test_production_tool_to_tool_imports_stay_runtime_owned
                    # and must stay free of packages.core.ai.tools imports.
                    # This handler lives under ai/runtime and is exempt.
                    provider_keys = sorted(scoped_mcp_provider_keys | {
                        provider
                        for name, _schema in scoped_tool_schemas
                        if (provider := runtime_mcp_provider_from_tool_name(str(name)))
                    })
                    _t0 = time.monotonic()
                    usable_providers = await resolve_usable_mcp_providers(
                        flag_db, user_id=user_id, entity_id=entity_id,
                        provider_keys=provider_keys,
                    )
                    logger.info(
                        "resolve_usable_mcp_providers took %.1fms for %d providers",
                        (time.monotonic() - _t0) * 1000,
                        len(provider_keys),
                    )
        if v2_enabled:
            from packages.core.ai.runtime.tool_discovery import runtime_server_index
            server_idx = runtime_server_index() or None
        if v2_enabled and entity_id and user_id and active_user_message:
            # A3 rank boost: re-run the SAME cache-first lookup
            # resolve_runtime_chat_context already did earlier this turn
            # (tool_path_memory.lookup_paths is Redis-blob-backed, so this
            # is one extra cheap cached read, not a second Postgres scan,
            # per the plan's explicit allowance — there's no channel yet
            # to pass the turn's already-computed path list down into this
            # tool-call handler without new context plumbing).
            from packages.core.services import tool_path_memory as tpm
            paths = await tpm.lookup_paths(
                entity_id=entity_id, user_id=user_id,
                user_message=active_user_message,
            )
            if paths:
                intent_path_boosts = tpm.fold_path_boosts(paths)
    except Exception:
        v2_enabled = False
        usable_providers = None
        server_idx = None  # degrade to v1
        intent_path_boosts = None

    # A1 suggestion channel (spec §A3... §A1): a query that strongly names an
    # UNCONNECTED provider must not regress below v1 ("no tools matched").
    # Score the full server index (usable + unusable) against the query; the
    # top <=2 unusable servers with a positive server score are exempted
    # from the pre-filter exclusion below, so their tools flow through the
    # ordinary v1-style scoring/ranking path unchanged. The EXISTING
    # availability annotation (below) independently re-checks real
    # connection state from the DB regardless of this exemption, so an
    # exempted-but-still-unconnected provider's matched tool still ends up
    # correctly marked unavailable -> surfaced via the existing
    # unavailable_mcp mechanism, never fabricating a false "connected"
    # status. Skipped for the select:/browse_server: explicit conventions,
    # where "usable" must stay a hard gate (browse_server's own not_usable
    # check would otherwise be silently bypassed for a suggested key).
    effective_usable_providers = usable_providers
    suggestion_providers: frozenset[str] = frozenset()
    if (
        v2_enabled
        and usable_providers is not None
        and server_idx
        and not search_request.query.lower().startswith(("select:", "browse_server:"))
    ):
        try:
            scored_unusable = sorted(
                (
                    (runtime_server_query_score(entry, search_request.query), key)
                    for key, entry in server_idx.items()
                    if key not in usable_providers
                ),
                key=lambda item: (item[0], item[1]),
                reverse=True,
            )
            suggestion_providers = frozenset(
                key for score, key in scored_unusable[:2] if score > 0
            )
            if suggestion_providers:
                effective_usable_providers = usable_providers | suggestion_providers
        except Exception:
            suggestion_providers = frozenset()
            effective_usable_providers = usable_providers  # degrade: no suggestions

    matches, suppressed_mcp = runtime_search_tool_registry_candidates(
        tool_schemas=scoped_tool_schemas,
        query=search_request.query,
        max_results=search_request.search_pool_size,
        bound_tool_names=bound_tools,
        active_user_message=active_user_message,
        usable_providers=effective_usable_providers,
        server_index=server_idx,
        intent_path_boosts=intent_path_boosts,
    )
    if matches:
        matches = await runtime_annotate_tool_availability(matches, entity_id, user_id)

    live_schema_by_name: dict[str, dict[str, Any]] = {}
    live_account_ids_by_name: dict[str, tuple[str, ...]] = {}
    live_tool_by_name: dict[str, Any] = {}
    if live_mcp_schema_loader is not None:
        from packages.core.services.official_remote_mcp import (
            OfficialRemoteMCPProvider,
        )

        official_providers = {provider.value for provider in OfficialRemoteMCPProvider}
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
            query=search_request.query,
            matches=matches,
            official_providers=discoverable_official_providers,
            scoped_providers=scoped_mcp_provider_keys,
            server_index=server_idx,
        )
        if discovery_providers:
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
                matches, suppressed_mcp = runtime_search_tool_registry_candidates(
                    tool_schemas=tuple(merged_schemas),
                    query=search_request.query,
                    max_results=search_request.search_pool_size,
                    bound_tool_names=live_bound_tools,
                    active_user_message=active_user_message,
                    usable_providers=effective_usable_providers,
                    server_index=server_idx,
                    intent_path_boosts=intent_path_boosts,
                )
                if matches:
                    matches = await runtime_annotate_tool_availability(
                        matches,
                        entity_id,
                        user_id,
                    )
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

    # Slot rule for the A1 suggestion channel: a suggested-but-unusable
    # provider's tool must never displace a usable result. Availability
    # annotation (above) already sorts available matches first, which
    # would otherwise push a suggested tool past the normal max_results cap
    # whenever enough available/non-MCP candidates exist to fill it. Split
    # suggestion-provider matches out and always append them BEYOND the cap
    # (never competing for a slot), so they still reach the payload's
    # existing unavailable_mcp mechanism via runtime_search_tools_payload.
    if suggestion_providers and matches:
        primary_matches = [
            m for m in matches
            if runtime_mcp_provider_from_tool_name(str(m.get("name") or "")) not in suggestion_providers
        ]
        suggested_matches = [
            m for m in matches
            if runtime_mcp_provider_from_tool_name(str(m.get("name") or "")) in suggestion_providers
        ]
        visible_matches = primary_matches[:search_request.max_results] + suggested_matches
    else:
        visible_matches = list(matches)[:search_request.max_results]

    # B1: servers[] summary — only computed when tool_discovery_v2 is on,
    # grouped over the FINAL (post-slice, post-slot-rule) matches in match
    # order, one entry per provider the first time it's seen. Additive;
    # matches[] itself is untouched.
    servers_summary: list[dict] | None = None
    if v2_enabled and visible_matches:
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
                    # Minor #8: shown-vs-total, so browse_server: (and any
                    # search result) makes clear this may only be a slice
                    # of the server's real tool count, not its whole menu.
                    "total_tools": entry.get("tool_count"),
                }
                order.append(provider)
            grouped[provider]["matched_tools"] += 1
            if len(grouped[provider]["top_tools"]) < 3:
                grouped[provider]["top_tools"].append(name)
        servers_summary = [grouped[key] for key in order] or None

    payload = runtime_search_tools_payload(
        matches=visible_matches,
        query=search_request.query,
        suppressed_mcp=suppressed_mcp,
        total_tool_count=(
            total_tool_count
            if bound_tools is None and total_tool_count is not None
            else len(scoped_available_names | set(live_schema_by_name))
        ),
        servers=servers_summary,
    )
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
    return json.dumps(payload, ensure_ascii=False)
