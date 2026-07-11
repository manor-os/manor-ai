from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging

from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES, RiskLevel
from packages.core.ai.runtime.profiles import RuntimeProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeToolBinding:
    name: str
    capability_id: str | None
    risk_level: RiskLevel = "safe"
    required_approval: bool = False
    source: str = "capability_catalog"

    def to_trace_dict(self) -> dict:
        return {
            "name": self.name,
            "capability_id": self.capability_id,
            "risk_level": self.risk_level,
            "required_approval": self.required_approval,
            "source": self.source,
        }


@dataclass(frozen=True)
class RuntimeSearchToolBindingScope:
    bound_tool_names: frozenset[str] | None = None
    mcp_allowed_names: frozenset[str] | None = None
    is_master: bool = False
    source: str = "none"

    def effective_bound_tool_names(self) -> set[str] | None:
        if self.bound_tool_names is None and self.mcp_allowed_names is None:
            return None
        names = set(self.bound_tool_names or ())
        names.update(self.mcp_allowed_names or ())
        return names


@dataclass(frozen=True)
class RuntimeAgentToolScope:
    bound_tool_names: frozenset[str] | None = None
    mcp_allowed_names: frozenset[str] | None = None
    is_master: bool = False
    source: str = "unscoped"
    errors: tuple[str, ...] = ()

    def mutable_pair(self) -> tuple[set[str] | None, set[str] | None]:
        return (
            None if self.bound_tool_names is None else set(self.bound_tool_names),
            None if self.mcp_allowed_names is None else set(self.mcp_allowed_names),
        )


async def runtime_agent_tool_scope(
    db,
    *,
    agent_id: str | None,
    is_master: bool,
    available_tool_names: Iterable[str] | None = None,
) -> RuntimeAgentToolScope:
    """Resolve one agent turn's first-party and MCP binding scope."""
    if not db or is_master or not agent_id:
        return RuntimeAgentToolScope(is_master=is_master, source="master_or_unscoped")

    errors: list[str] = []
    bound_tool_names: frozenset[str] = frozenset()
    mcp_allowed_names: frozenset[str] = frozenset()

    try:
        from packages.core.services.agent_service import get_agent_tools

        bound_tool_names = frozenset(
            str(tool.name)
            for tool in await get_agent_tools(db, agent_id)
            if str(getattr(tool, "name", "") or "").strip()
        )
    except Exception:
        errors.append("agent_tool_bindings")
        logger.warning(
            "Runtime agent tool binding resolution failed for agent %s; using empty scope",
            agent_id,
            exc_info=True,
        )

    try:
        from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
        from packages.core.services.agent_permission_service import (
            filter_mcp_tools_by_scope,
            resolve_agent_mcp_scope,
        )

        scope = await resolve_agent_mcp_scope(db, agent_id)
        all_tool_names = (
            tuple(str(name) for name in available_tool_names)
            if available_tool_names is not None
            else runtime_registered_tool_names(prefix="mcp__")
        )
        all_mcp_names = [name for name in all_tool_names if name.startswith("mcp__")]
        mcp_allowed_names = frozenset(filter_mcp_tools_by_scope(all_mcp_names, scope))
    except Exception:
        errors.append("agent_mcp_scope")
        logger.warning(
            "Runtime agent MCP scope resolution failed for agent %s; default-deny",
            agent_id,
            exc_info=True,
        )

    return RuntimeAgentToolScope(
        bound_tool_names=bound_tool_names,
        mcp_allowed_names=mcp_allowed_names,
        source="agent_bindings",
        errors=tuple(errors),
    )


async def runtime_search_tool_binding_scope(
    *,
    agent_id: str | None,
    context_allowed_tool_names: Iterable[str] | None,
    available_tool_names: Iterable[str],
) -> RuntimeSearchToolBindingScope:
    """Resolve the tool allowlist that search_tools should search within."""
    if context_allowed_tool_names is not None:
        allowed = frozenset(str(name) for name in context_allowed_tool_names)
        return RuntimeSearchToolBindingScope(
            bound_tool_names=allowed,
            mcp_allowed_names=frozenset(name for name in allowed if name.startswith("mcp__")),
            source="runtime_context",
        )

    if not agent_id:
        return RuntimeSearchToolBindingScope()

    try:
        from packages.core.constants.agents import is_master_agent
    except Exception:
        return RuntimeSearchToolBindingScope(
            bound_tool_names=frozenset(),
            mcp_allowed_names=frozenset(),
            source="agent_identity_unavailable",
        )

    if is_master_agent(agent_id):
        return RuntimeSearchToolBindingScope(is_master=True, source="master_agent")

    try:
        from packages.core.database import async_session

        async with async_session() as db:
            agent_scope = await runtime_agent_tool_scope(
                db,
                agent_id=agent_id,
                is_master=False,
                available_tool_names=available_tool_names,
            )
            return RuntimeSearchToolBindingScope(
                bound_tool_names=agent_scope.bound_tool_names,
                mcp_allowed_names=agent_scope.mcp_allowed_names,
                source=agent_scope.source,
            )
    except Exception:
        return RuntimeSearchToolBindingScope(
            bound_tool_names=frozenset(),
            mcp_allowed_names=frozenset(),
            source="agent_bindings_unavailable",
        )


def tool_bindings_for_profile_tools(
    profile: RuntimeProfile,
    tool_names: set[str],
) -> tuple[RuntimeToolBinding, ...]:
    bindings: list[RuntimeToolBinding] = []
    bound_tool_names: set[str] = set()
    for capability in CORE_CAPABILITIES.values():
        if capability.profiles and profile not in capability.profiles:
            continue
        for tool_name in capability.tool_names:
            if tool_name not in tool_names:
                continue
            bindings.append(
                RuntimeToolBinding(
                    name=tool_name,
                    capability_id=capability.id,
                    risk_level=capability.risk_level,
                    required_approval=capability.required_approval,
                )
            )
            bound_tool_names.add(tool_name)
    for tool_name in sorted(tool_names - bound_tool_names):
        bindings.append(
            RuntimeToolBinding(
                name=tool_name,
                capability_id=None,
                source="unclassified_legacy_tool",
            )
        )
    return tuple(sorted(bindings, key=lambda item: (item.name, item.capability_id or "")))
