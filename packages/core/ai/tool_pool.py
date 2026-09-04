"""
Tool Pool — manages available tools for agent execution.

Ported from manor-multi-agent's pool.py. Key concepts:
- Runtime eager surface: tools available to every agent
- Deferred tools: schema withheld until search_tools loads them
- Runtime registry adapters resolve agent/workspace tool visibility
- Composite tools (manor, code) route to action sub-handlers
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Iterable
from typing import Any, Optional

from packages.core.ai.runtime.dynamic_mcp import (
    RuntimeDynamicMCPRehydrationResult,
    RuntimeDynamicMCPRehydrationStatus,
    RuntimeDynamicMCPToolBinding,
    RuntimeDynamicMCPToolBindingFactory,
    RuntimeDynamicMCPToolGrantFactory,
    runtime_discover_official_remote_mcp_tools,
    runtime_dynamic_mcp_binding_is_current,
    runtime_dynamic_mcp_failure_result,
    runtime_dynamic_mcp_result_is_stale,
    runtime_dynamic_mcp_tool_handler,
    runtime_rehydrate_dynamic_mcp_tool_binding,
)
from packages.core.ai.runtime.tool_search import (
    runtime_execute_search_tools_handler,
    runtime_search_tools_schema,
    runtime_search_tool_registry_candidates,
)
from packages.core.ai.runtime.tool_execution import (
    runtime_execute_registered_tool,
    runtime_preflight_tool_resolution,
)
from packages.core.ai.runtime.tool_visibility import (
    runtime_tool_is_deferred,
)

logger = logging.getLogger(__name__)

_DYNAMIC_MCP_HANDLER_TTL_SECONDS = 15 * 60

# Compatibility aliases for existing tests and process-local diagnostics. The
# implementation and factory live under Runtime so ToolPool remains a registry.
_DynamicMCPToolBinding = RuntimeDynamicMCPToolBinding
_DynamicMCPToolBindingFactory = RuntimeDynamicMCPToolBindingFactory


def is_deferred(name: str, auto_pass: set | None = None) -> bool:
    """Check if a tool should be deferred (schema withheld)."""
    return runtime_tool_is_deferred(name, auto_pass_tool_names=auto_pass)


class ToolPool:
    """Registry of available tools for AI agent execution."""

    def __init__(self):
        self._tools: dict[str, dict] = {}  # name -> {schema, handler, deferred}
        self._dynamic_mcp_tools: dict[
            tuple[str, str, str],
            _DynamicMCPToolBinding,
        ] = {}
        self._initialized = False

    def initialize(self) -> None:
        """Load all built-in tools into the pool."""
        from packages.core.ai.tools import register_all_tools

        register_all_tools(self)
        self._register_search_tools()
        self._initialized = True
        logger.info(
            "Tool pool initialized with %d tools (%d always-loaded, %d deferred)",
            len(self._tools),
            sum(1 for n in self._tools if not is_deferred(n)),
            sum(1 for n in self._tools if is_deferred(n)),
        )

    def register(self, name: str, schema: dict, handler, deferred: bool = False):
        self._tools[name] = {"schema": schema, "handler": handler, "deferred": deferred}

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def registered_tool_names(self, *, prefix: str | None = None) -> tuple[str, ...]:
        names = tuple(self._tools.keys())
        if prefix is None:
            return names
        return tuple(name for name in names if name.startswith(prefix))

    def registered_tool_schemas(self) -> tuple[tuple[str, dict], ...]:
        return tuple((name, copy.deepcopy(entry.get("schema") or {})) for name, entry in self._tools.items())

    def _prune_dynamic_mcp_tools(self, *, now: float | None = None) -> None:
        """Remove expired actor-scoped handlers from the process-local cache."""
        cutoff = time.monotonic() if now is None else now
        for actor_key, binding in tuple(self._dynamic_mcp_tools.items()):
            if binding.expires_at <= cutoff:
                self._dynamic_mcp_tools.pop(actor_key, None)

    def get(self, name: str) -> Optional[dict]:
        return self._tools.get(name)

    def get_schema(self, name: str) -> Optional[dict]:
        """Return a copy of a registered tool schema for internal lazy loading."""
        tool = self._tools.get(name)
        if not tool:
            return None
        return copy.deepcopy(tool["schema"])

    def get_schemas_for_names(self, names: list[str] | set[str] | tuple[str, ...]) -> list[dict]:
        """Return registered schemas for the provided names, preserving input order."""
        schemas: list[dict] = []
        for name in names:
            schema = self.get_schema(str(name))
            if schema is not None:
                schemas.append(schema)
        return schemas

    async def execute(
        self,
        name: str,
        arguments: dict,
        entity_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        runtime_artifact_urls: Iterable[str] | None = None,
        dependency_artifact_urls: Iterable[str] | None = None,
        workspace_id: str | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        active_user_message: str | None = None,
        manual_skill_selected: bool = False,
        manual_skill_ids: list[str] | None = None,
        manual_skill_slugs: list[str] | None = None,
        tool_profile: str | None = None,
        allowed_tool_names: set[str] | None = None,
        llm_metadata: dict[str, Any] | None = None,
        llm_model: str | None = None,
        runtime_envelope: Any | None = None,
    ) -> Any:
        """Execute a registered tool through the Runtime Harness."""
        resolution_preflight = runtime_preflight_tool_resolution(
            tool_name=name,
            arguments=arguments,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            runtime_envelope=runtime_envelope,
        )
        if resolution_preflight.blocked_result is not None:
            return resolution_preflight.blocked_result
        dynamic_resolution = await self._ensure_dynamic_mcp_tool_binding(
            name,
            entity_id=entity_id,
            user_id=user_id,
            allowed_tool_names=allowed_tool_names,
            runtime_envelope=runtime_envelope,
        )
        def blocked_dynamic_result(
            resolution: RuntimeDynamicMCPRehydrationResult | None,
        ) -> str | None:
            if resolution is None or resolution.status not in {
                RuntimeDynamicMCPRehydrationStatus.OFFICIAL_UNAVAILABLE,
                RuntimeDynamicMCPRehydrationStatus.DISCOVERY_FAILED,
            }:
                return None
            blocked_result = runtime_dynamic_mcp_failure_result(
                resolution
            )
            if resolution_preflight.harness is not None:
                resolution_preflight.harness.record_tool_block_result(
                    name,
                    blocked_result,
                )
            return blocked_result

        blocked_result = blocked_dynamic_result(dynamic_resolution)
        if blocked_result is not None:
            return blocked_result
        dynamic_binding = (
            dynamic_resolution.binding
            if dynamic_resolution is not None
            and dynamic_resolution.status is RuntimeDynamicMCPRehydrationStatus.BOUND
            else None
        )

        async def execute_registered(
            binding: RuntimeDynamicMCPToolBinding | None,
        ) -> Any:
            return await runtime_execute_registered_tool(
                tool_name=name,
                arguments=arguments,
                handler_resolver=lambda tool_name: self._handler_for_actor(
                    tool_name,
                    entity_id=entity_id,
                    user_id=user_id,
                ),
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                runtime_artifact_urls=runtime_artifact_urls,
                dependency_artifact_urls=dependency_artifact_urls,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                active_user_message=active_user_message,
                manual_skill_selected=manual_skill_selected,
                manual_skill_ids=manual_skill_ids,
                manual_skill_slugs=manual_skill_slugs,
                tool_profile=tool_profile,
                allowed_tool_names=allowed_tool_names,
                llm_metadata=llm_metadata,
                llm_model=llm_model,
                runtime_envelope=runtime_envelope,
                discovered_tool_grant=(
                    RuntimeDynamicMCPToolGrantFactory.from_binding(name, binding)
                    if binding is not None
                    else None
                ),
                logger=logger,
            )

        result = await execute_registered(dynamic_binding)
        if dynamic_binding is None or not runtime_dynamic_mcp_result_is_stale(result):
            return result

        self._dynamic_mcp_tools.pop(
            (str(entity_id or ""), str(user_id or ""), name),
            None,
        )
        refreshed_resolution = await self._ensure_dynamic_mcp_tool_binding(
            name,
            entity_id=entity_id,
            user_id=user_id,
            allowed_tool_names=allowed_tool_names,
            runtime_envelope=runtime_envelope,
        )
        blocked_result = blocked_dynamic_result(refreshed_resolution)
        if blocked_result is not None:
            return blocked_result
        if (
            refreshed_resolution is None
            or refreshed_resolution.status is not RuntimeDynamicMCPRehydrationStatus.BOUND
            or refreshed_resolution.binding is None
        ):
            return result
        return await execute_registered(refreshed_resolution.binding)

    def search(
        self,
        query: str,
        max_results: int = 5,
        bound_tools: set | None = None,
        active_user_message: str | None = None,
    ) -> list[dict]:
        """Search tools by keyword. Used by search_tools tool."""
        matches, _ = self.search_with_details(
            query,
            max_results=max_results,
            bound_tools=bound_tools,
            active_user_message=active_user_message,
        )
        return matches

    def search_with_details(
        self,
        query: str,
        max_results: int = 5,
        bound_tools: set | None = None,
        active_user_message: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """Search tools by delegating candidate planning to Runtime Harness."""
        return runtime_search_tool_registry_candidates(
            tool_schemas=self.registered_tool_schemas(),
            query=query,
            max_results=max_results,
            bound_tool_names=bound_tools,
            active_user_message=active_user_message,
        )

    def _register_search_tools(self):
        """Register the built-in search_tools discovery tool."""

        async def _search_handler(
            entity_id: str = "",
            user_id: str = "",
            **kwargs,
        ) -> str:
            discovered_bindings: dict[str, _DynamicMCPToolBinding] = {}

            async def _load_live_schemas(
                provider_keys: frozenset[str],
            ) -> dict[str, list[Any]]:
                discovered = await runtime_discover_official_remote_mcp_tools(
                    provider_keys=provider_keys,
                    entity_id=entity_id,
                    user_id=user_id,
                )
                now = time.monotonic()
                self._prune_dynamic_mcp_tools(now=now)
                expires_at = now + _DYNAMIC_MCP_HANDLER_TTL_SECONDS
                for tools in discovered.values():
                    for tool in tools:
                        discovered_bindings[tool.name] = (
                            _DynamicMCPToolBindingFactory.from_discovered(
                                tool,
                                expires_at=expires_at,
                            )
                        )
                return discovered

            result = await runtime_execute_search_tools_handler(
                arguments=kwargs,
                entity_id=entity_id,
                user_id=user_id,
                tool_schemas=self.registered_tool_schemas(),
                available_tool_names=self._tools.keys(),
                total_tool_count=len(self._tools),
                live_mcp_schema_loader=_load_live_schemas,
            )
            try:
                payload = json.loads(result)
            except (TypeError, ValueError):
                return result
            if not isinstance(payload, dict):
                return result
            granted_names = {
                str(name)
                for name in payload.get("loaded_tools", [])
                if str(name) in discovered_bindings
            }
            for name in granted_names:
                self._dynamic_mcp_tools[(entity_id, user_id, name)] = (
                    discovered_bindings[name]
                )
            return result

        self.register("search_tools", runtime_search_tools_schema(), _search_handler)

    @staticmethod
    def _grant_dynamic_mcp_tool_binding(
        tool_name: str,
        binding: RuntimeDynamicMCPToolBinding,
        *,
        runtime_envelope: Any | None,
    ) -> None:
        if runtime_envelope is None:
            return
        runtime_envelope.discovered_tool_grants.grant([
            RuntimeDynamicMCPToolGrantFactory.from_binding(tool_name, binding)
        ])

    async def _ensure_dynamic_mcp_tool_binding(
        self,
        tool_name: str,
        *,
        entity_id: str | None,
        user_id: str | None,
        allowed_tool_names: set[str] | None,
        runtime_envelope: Any | None,
    ) -> RuntimeDynamicMCPRehydrationResult | None:
        """Rehydrate a persisted Workflow/Task name after process restart."""

        if not entity_id or not user_id:
            return
        if runtime_envelope is not None:
            if tool_name in set(
                getattr(runtime_envelope, "blocked_tool_names", ()) or ()
            ):
                return
            effective_allowed = getattr(
                runtime_envelope,
                "effective_allowed_tool_names",
                None,
            )
            allowed = (
                effective_allowed()
                if callable(effective_allowed)
                else set(getattr(runtime_envelope, "allowed_tool_names", ()) or ())
            )
            if allowed and tool_name not in allowed:
                return
        elif allowed_tool_names is not None and tool_name not in allowed_tool_names:
            return

        cached_binding = self._dynamic_mcp_binding_for_actor(
            tool_name,
            entity_id=entity_id,
            user_id=user_id,
        )
        if cached_binding is not None:
            if await runtime_dynamic_mcp_binding_is_current(
                cached_binding,
                entity_id=str(entity_id),
                user_id=str(user_id),
            ):
                self._grant_dynamic_mcp_tool_binding(
                    tool_name,
                    cached_binding,
                    runtime_envelope=runtime_envelope,
                )
                return RuntimeDynamicMCPRehydrationResult(
                    status=RuntimeDynamicMCPRehydrationStatus.BOUND,
                    tool_name=tool_name,
                    provider=cached_binding.provider,
                    binding=cached_binding,
                )
            self._dynamic_mcp_tools.pop(
                (str(entity_id), str(user_id), tool_name),
                None,
            )

        resolution = await runtime_rehydrate_dynamic_mcp_tool_binding(
            tool_name,
            entity_id=entity_id,
            user_id=user_id,
            expires_at=time.monotonic() + _DYNAMIC_MCP_HANDLER_TTL_SECONDS,
        )
        if resolution.status is not RuntimeDynamicMCPRehydrationStatus.BOUND:
            return resolution
        binding = resolution.binding
        if binding is None:
            return RuntimeDynamicMCPRehydrationResult(
                status=RuntimeDynamicMCPRehydrationStatus.OFFICIAL_UNAVAILABLE,
                tool_name=tool_name,
                provider=resolution.provider,
            )
        self._dynamic_mcp_tools[
            (str(entity_id), str(user_id), tool_name)
        ] = binding
        self._grant_dynamic_mcp_tool_binding(
            tool_name,
            binding,
            runtime_envelope=runtime_envelope,
        )
        return resolution

    def _dynamic_mcp_binding_for_actor(
        self,
        tool_name: str,
        *,
        entity_id: str | None,
        user_id: str | None,
    ) -> _DynamicMCPToolBinding | None:
        actor_key = (str(entity_id or ""), str(user_id or ""), tool_name)
        binding = self._dynamic_mcp_tools.get(actor_key)
        if binding is not None and binding.expires_at <= time.monotonic():
            self._dynamic_mcp_tools.pop(actor_key, None)
            binding = None
        return binding

    def _handler_for_actor(
        self,
        tool_name: str,
        *,
        entity_id: str | None,
        user_id: str | None,
    ):
        binding = self._dynamic_mcp_binding_for_actor(
            tool_name,
            entity_id=entity_id,
            user_id=user_id,
        )
        if binding is not None:
            return runtime_dynamic_mcp_tool_handler(binding)
        return self._tools.get(tool_name, {}).get("handler")


# Global singleton
tool_pool = ToolPool()
