from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from packages.core.ai.runtime.legacy_tool_surface import (
    legacy_search_always_loaded_tool_names,
    legacy_search_bound_tool_names_for_profile,
    legacy_shadowed_file_generation_tool,
)
from packages.core.ai.runtime.tool_availability import runtime_annotate_tool_availability
from packages.core.ai.runtime.tool_bindings import runtime_search_tool_binding_scope
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.ai.runtime.tool_discovery import (
    runtime_finalize_search_tools_payload,
    runtime_mcp_provider_from_tool_name,
    runtime_mcp_provider_text_score,
    runtime_prepare_search_tools_request,
    runtime_tool_query_score,
    runtime_tool_search_scope,
)


_TOOL_MANIFEST_DESCRIPTION_CHARS = 260
_TOOL_MANIFEST_PARAMETER_LIMIT = 12


def runtime_search_tools_schema() -> dict:
    """Return the built-in search_tools schema owned by Runtime Harness."""

    return {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Search for available tools by keyword or load specific tools by name. "
                'Use "select:tool_name1,tool_name2" for exact match, or keywords for search.'
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
) -> tuple[list[dict], list[dict]]:
    """Search tool manifests and return MCP providers suppressed by intent."""
    tool_map = {str(name): schema for name, schema in tool_schemas}
    always_loaded = set(always_loaded_tool_names)
    query_lower = query.lower()

    if query_lower.startswith("select:"):
        names = [name.strip() for name in query_lower[7:].split(",") if name.strip()]
        results: list[dict] = []
        selected: set[str] = set()
        suppressed_mcp: list[dict] = []
        scope = runtime_tool_search_scope(
            tool_names=tool_map.keys(),
            query=query_lower,
            active_user_message=active_user_message,
        )
        for requested_name in names:
            for name in runtime_select_tool_candidates(requested_name, tool_map.keys()):
                if name in selected:
                    continue
                if not scope.mcp_tool_allowed(name):
                    continue
                if legacy_shadowed_file_generation_tool(
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
            for name in scope.preferred_chrome_tool_names:
                if name in selected or name not in tool_map:
                    continue
                if (
                    bound_tool_names is not None
                    and name not in always_loaded
                    and name not in bound_tool_names
                ):
                    continue
                if legacy_shadowed_file_generation_tool(
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
                if len(results) >= max_results:
                    break
        return results, suppressed_mcp

    scope = runtime_tool_search_scope(
        tool_names=tool_map.keys(),
        query=query_lower,
        active_user_message=active_user_message,
    )
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
        if legacy_shadowed_file_generation_tool(
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

        provider_score = (
            active_scores.get(provider, 0) * 100
            + provider_query_score * 10
            + max(best_tool_score, 0)
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
            if legacy_shadowed_file_generation_tool(
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


def runtime_search_tool_registry_candidates(
    *,
    tool_schemas: Iterable[tuple[str, dict]],
    query: str,
    max_results: int = 5,
    bound_tool_names: set[str] | None = None,
    active_user_message: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Search registry-backed tools using the Runtime-owned eager baseline."""

    return runtime_search_tool_candidates(
        tool_schemas=tool_schemas,
        query=query,
        max_results=max_results,
        bound_tool_names=bound_tool_names,
        active_user_message=active_user_message,
        always_loaded_tool_names=legacy_search_always_loaded_tool_names(),
    )


async def runtime_execute_search_tools_handler(
    *,
    arguments: Mapping[str, Any],
    entity_id: str = "",
    user_id: str = "",
    tool_schemas: Iterable[tuple[str, dict]],
    available_tool_names: Iterable[str],
    total_tool_count: int | None = None,
) -> str:
    """Execute the built-in search_tools contract against a registry snapshot."""

    available_names = tuple(str(name) for name in available_tool_names)
    search_request = runtime_prepare_search_tools_request(
        query=arguments.get("query", ""),
        max_results=arguments.get("max_results"),
    )
    if not search_request.ok:
        return json.dumps({"error": search_request.error})

    runtime_context = runtime_tool_call_context_from_kwargs(dict(arguments))
    context_allowed = (
        set(runtime_context.allowed_tool_names)
        if runtime_context.allowed_tool_names is not None
        else None
    )
    binding_scope = await runtime_search_tool_binding_scope(
        agent_id=runtime_context.agent_id if isinstance(runtime_context.agent_id, str) else None,
        context_allowed_tool_names=context_allowed,
        available_tool_names=available_names,
    )
    bound_tools = binding_scope.effective_bound_tool_names()
    mcp_allowed_names = (
        set(binding_scope.mcp_allowed_names)
        if binding_scope.mcp_allowed_names is not None
        else None
    )
    bound_tools = legacy_search_bound_tool_names_for_profile(
        available_names,
        legacy_tool_profile=(
            runtime_context.legacy_tool_profile
            if isinstance(runtime_context.legacy_tool_profile, str)
            else None
        ),
        context_allowed_tool_names=context_allowed,
        bound_tool_names=bound_tools,
        mcp_allowed_names=mcp_allowed_names,
    )

    active_user_message = (
        runtime_context.active_user_message
        if isinstance(runtime_context.active_user_message, str)
        else None
    )
    matches, suppressed_mcp = runtime_search_tool_registry_candidates(
        tool_schemas=tool_schemas,
        query=search_request.query,
        max_results=search_request.search_pool_size,
        bound_tool_names=bound_tools,
        active_user_message=active_user_message,
    )
    if matches:
        matches = await runtime_annotate_tool_availability(matches, entity_id, user_id)

    payload = runtime_finalize_search_tools_payload(
        matches=matches,
        request=search_request,
        suppressed_mcp=suppressed_mcp,
        total_tool_count=total_tool_count if total_tool_count is not None else len(available_names),
    )
    return json.dumps(payload, ensure_ascii=False)
