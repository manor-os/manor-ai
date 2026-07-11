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
import logging
from collections.abc import Iterable
from importlib import import_module
from typing import Any, Optional

from packages.core.ai.runtime.tool_search import (
    runtime_execute_search_tools_handler,
    runtime_search_tools_schema,
    runtime_search_tool_registry_candidates,
)
from packages.core.ai.runtime.tool_execution import (
    runtime_execute_registered_tool,
)
from packages.core.ai.runtime.legacy_tool_surface import (
    legacy_tool_is_deferred,
)

logger = logging.getLogger(__name__)

_LEGACY_RUNTIME_EXPORTS = {
    "ALWAYS_LOADED": ("packages.core.ai.runtime.legacy_tool_surface", "ALWAYS_LOADED"),
    "MASTER_ALWAYS_LOADED": ("packages.core.ai.runtime.legacy_tool_surface", "MASTER_ALWAYS_LOADED"),
    "TOOL_PROFILE_WORKSPACE_AGENT": (
        "packages.core.ai.runtime.legacy_tool_surface",
        "TOOL_PROFILE_WORKSPACE_AGENT",
    ),
    "WORKSPACE_AGENT_ALWAYS_LOADED": (
        "packages.core.ai.runtime.legacy_tool_surface",
        "WORKSPACE_AGENT_ALWAYS_LOADED",
    ),
    "WORKSPACE_AGENT_CONTEXTUAL_TOOLS": (
        "packages.core.ai.runtime.legacy_tool_surface",
        "WORKSPACE_AGENT_CONTEXTUAL_TOOLS",
    ),
}


def __getattr__(name: str) -> Any:
    exported = _LEGACY_RUNTIME_EXPORTS.get(name)
    if not exported:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = exported
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def is_deferred(name: str, auto_pass: set | None = None) -> bool:
    """Check if a tool should be deferred (schema withheld)."""
    return legacy_tool_is_deferred(name, auto_pass_tool_names=auto_pass)


class ToolPool:
    """Registry of available tools for AI agent execution."""

    def __init__(self):
        self._tools: dict[str, dict] = {}  # name -> {schema, handler, deferred}
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
        active_user_message: str | None = None,
        manual_skill_selected: bool = False,
        manual_skill_slugs: list[str] | None = None,
        legacy_tool_profile: str | None = None,
        allowed_tool_names: set[str] | None = None,
        llm_metadata: dict[str, Any] | None = None,
        llm_model: str | None = None,
        runtime_envelope: Any | None = None,
    ) -> str:
        """Execute a registered tool through the Runtime Harness."""
        return await runtime_execute_registered_tool(
            tool_name=name,
            arguments=arguments,
            handler_resolver=lambda tool_name: self._tools.get(tool_name, {}).get("handler"),
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
            manual_skill_slugs=manual_skill_slugs,
            legacy_tool_profile=legacy_tool_profile,
            allowed_tool_names=allowed_tool_names,
            llm_metadata=llm_metadata,
            llm_model=llm_model,
            runtime_envelope=runtime_envelope,
            logger=logger,
        )

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
            return await runtime_execute_search_tools_handler(
                arguments=kwargs,
                entity_id=entity_id,
                user_id=user_id,
                tool_schemas=self.registered_tool_schemas(),
                available_tool_names=self._tools.keys(),
                total_tool_count=len(self._tools),
            )

        self.register("search_tools", runtime_search_tools_schema(), _search_handler)


# Global singleton
tool_pool = ToolPool()
