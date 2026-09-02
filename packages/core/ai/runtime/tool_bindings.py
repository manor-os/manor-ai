from __future__ import annotations

from collections.abc import Iterable, Mapping
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
class RuntimeMCPProviderToolScope:
    """One provider-level MCP binding, preserving wildcard semantics."""

    provider: str
    allowed_actions: frozenset[str] | None = None

    def allows(self, action: str) -> bool:
        return self.allowed_actions is None or action in self.allowed_actions


class RuntimeMCPProviderToolScopeFactory:
    """Convert the permission service mapping into an immutable runtime scope."""

    @staticmethod
    def create(
        scope: Mapping[str, Iterable[str] | None],
    ) -> tuple[RuntimeMCPProviderToolScope, ...]:
        bindings: list[RuntimeMCPProviderToolScope] = []
        for raw_provider, raw_actions in sorted(scope.items()):
            provider = str(raw_provider or "").strip()
            if not provider:
                continue
            bindings.append(RuntimeMCPProviderToolScope(
                provider=provider,
                allowed_actions=(
                    None
                    if raw_actions is None
                    else frozenset(
                        str(action).strip()
                        for action in raw_actions
                        if str(action or "").strip()
                    )
                ),
            ))
        return tuple(bindings)

    @staticmethod
    def merge(
        *scope_groups: Iterable[RuntimeMCPProviderToolScope],
    ) -> tuple[RuntimeMCPProviderToolScope, ...]:
        """Union provider scopes while preserving ``None == all actions``."""

        merged: dict[str, set[str] | None] = {}
        for group in scope_groups:
            for item in group:
                provider = str(item.provider or "").strip()
                if not provider:
                    continue
                if item.allowed_actions is None:
                    merged[provider] = None
                    continue
                if provider in merged and merged[provider] is None:
                    continue
                merged.setdefault(provider, set())
                actions = merged[provider]
                if actions is not None:
                    actions.update(item.allowed_actions)
        return RuntimeMCPProviderToolScopeFactory.create(merged)


@dataclass(frozen=True)
class RuntimeSearchToolBindingScope:
    bound_tool_names: frozenset[str] | None = None
    mcp_allowed_names: frozenset[str] | None = None
    mcp_provider_scopes: tuple[RuntimeMCPProviderToolScope, ...] = ()
    is_master: bool = False
    source: str = "none"

    def effective_bound_tool_names(self) -> set[str] | None:
        if self.bound_tool_names is None and self.mcp_allowed_names is None:
            return None
        names = set(self.bound_tool_names or ())
        names.update(self.mcp_allowed_names or ())
        return names

    def allows_dynamic_mcp_tool(
        self,
        tool_name: str,
        *,
        provider: str | None = None,
        action: str | None = None,
    ) -> bool:
        """Authorize a newly discovered name without widening an allowlist."""

        name = str(tool_name or "").strip()
        if self.is_master or (
            self.bound_tool_names is None
            and self.mcp_allowed_names is None
            and not self.mcp_provider_scopes
        ):
            return True
        if name in (self.bound_tool_names or ()) or name in (self.mcp_allowed_names or ()):
            return True
        parts = name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return False
        provider_key = str(provider or parts[1]).strip()
        action_key = str(action or parts[2]).strip()
        binding = next(
            (
                item
                for item in self.mcp_provider_scopes
                if item.provider == provider_key
            ),
            None,
        )
        return binding.allows(action_key) if binding is not None else False


@dataclass(frozen=True)
class RuntimeDynamicMCPDiscoveryScope:
    """Turn-scoped policy for vendor-added MCP tool names."""

    binding_scope: RuntimeSearchToolBindingScope
    owner_interactive_scope: bool = False
    blocked_tool_names: frozenset[str] = frozenset()

    def allows(
        self,
        tool_name: str,
        *,
        provider: str | None = None,
        action: str | None = None,
    ) -> bool:
        name = str(tool_name or "").strip()
        if not name or name in self.blocked_tool_names:
            return False
        return self.binding_scope.allows_dynamic_mcp_tool(
            name,
            provider=provider,
            action=action,
        ) or self.owner_interactive_scope


class RuntimeDynamicMCPDiscoveryScopeFactory:
    """Combine exact Agent bindings with explicit owner-chat policy."""

    @staticmethod
    def create(
        binding_scope: RuntimeSearchToolBindingScope,
        runtime_envelope: object | None,
    ) -> RuntimeDynamicMCPDiscoveryScope:
        blocked = frozenset(
            str(name)
            for name in (
                getattr(runtime_envelope, "blocked_tool_names", ()) or ()
            )
            if str(name or "").strip()
        )
        if runtime_envelope is None:
            return RuntimeDynamicMCPDiscoveryScope(
                binding_scope=binding_scope,
                blocked_tool_names=blocked,
            )

        from packages.core.ai.runtime.surfaces import ChatSurface
        from packages.core.constants.runtime_principal import RuntimePrincipalKind

        metadata = getattr(runtime_envelope, "metadata", None)
        chat_mode = (
            str(metadata.get("chat_mode") or "").strip().lower()
            if isinstance(metadata, Mapping)
            else ""
        )
        principal_kind = RuntimePrincipalKind.parse(
            getattr(getattr(runtime_envelope, "principal", None), "kind", None)
        )
        has_explicit_mcp_scope = bool(
            binding_scope.mcp_provider_scopes
        ) or any(
            name.startswith("mcp__")
            for name in (
                set(binding_scope.bound_tool_names or ())
                | set(binding_scope.mcp_allowed_names or ())
            )
        )
        owner_interactive_scope = (
            chat_mode != "flows"
            and not getattr(runtime_envelope, "agent_id", None)
            and not has_explicit_mcp_scope
            and getattr(runtime_envelope, "surface", None) in {
                ChatSurface.GLOBAL_OWNER_CHAT,
                ChatSurface.WORKSPACE_CHAT,
            }
            and principal_kind in {
                RuntimePrincipalKind.OWNER,
                RuntimePrincipalKind.WORKSPACE_MEMBER,
            }
        )
        return RuntimeDynamicMCPDiscoveryScope(
            binding_scope=binding_scope,
            owner_interactive_scope=owner_interactive_scope,
            blocked_tool_names=blocked,
        )


@dataclass(frozen=True)
class RuntimeAgentToolScope:
    bound_tool_names: frozenset[str] | None = None
    mcp_allowed_names: frozenset[str] | None = None
    mcp_provider_scopes: tuple[RuntimeMCPProviderToolScope, ...] = ()
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
    mcp_provider_scopes: tuple[RuntimeMCPProviderToolScope, ...] = ()

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
        mcp_provider_scopes = RuntimeMCPProviderToolScopeFactory.create(scope)
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
        mcp_provider_scopes=mcp_provider_scopes,
        source="agent_bindings",
        errors=tuple(errors),
    )


async def runtime_search_tool_binding_scope(
    *,
    agent_id: str | None,
    context_allowed_tool_names: Iterable[str] | None,
    available_tool_names: Iterable[str],
    context_mcp_provider_scopes: Iterable[RuntimeMCPProviderToolScope] = (),
    context_mcp_scope_unrestricted: bool = False,
    is_master: bool = False,
) -> RuntimeSearchToolBindingScope:
    """Resolve the tool allowlist that search_tools should search within."""
    context_allowed = (
        frozenset(str(name) for name in context_allowed_tool_names)
        if context_allowed_tool_names is not None
        else None
    )

    # The active Runtime envelope already resolved Agent, Workspace, Task,
    # surface, and blocked-tool overlays into this exact set. Search consumes
    # that snapshot directly; it must not perform another binding lookup and
    # accidentally search a broader or narrower catalog. Execution performs
    # its own fresh durable-binding check immediately before the handler runs.
    if context_allowed is not None:
        return RuntimeSearchToolBindingScope(
            bound_tool_names=context_allowed,
            mcp_allowed_names=frozenset(
                name for name in context_allowed if name.startswith("mcp__")
            ),
            mcp_provider_scopes=tuple(context_mcp_provider_scopes),
            # ``is_master`` is identity, not authority after Task/operation
            # overlays. Only the resolved run-local MCP scope may be open.
            is_master=context_mcp_scope_unrestricted,
            source="runtime_context",
        )

    if is_master:
        return RuntimeSearchToolBindingScope(
            bound_tool_names=None,
            mcp_allowed_names=None,
            is_master=True,
            source="master_agent",
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
        return RuntimeSearchToolBindingScope(
            bound_tool_names=None,
            mcp_allowed_names=None,
            is_master=True,
            source="master_agent",
        )

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
                mcp_provider_scopes=agent_scope.mcp_provider_scopes,
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
                source="unclassified_tool",
            )
        )
    return tuple(sorted(bindings, key=lambda item: (item.name, item.capability_id or "")))
