from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPAccountRegistrySnapshot,
    )

from packages.core.ai.runtime.principals import RuntimePrincipal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_bindings import RuntimeMCPProviderToolScope
from packages.core.services.sensitive_data import sanitize_sensitive_payload


@dataclass(frozen=True, slots=True)
class RuntimeEffectiveToolScope:
    """Exact run-local tool names shared by discovery and execution gates."""

    allowed_tool_names: frozenset[str] | None
    blocked_tool_names: frozenset[str] = frozenset()
    source: str = "unscoped"

    def searchable_tool_names(self) -> set[str] | None:
        if self.allowed_tool_names is None:
            return None
        return set(self.allowed_tool_names) - set(self.blocked_tool_names)

    def allows(self, tool_name: str) -> bool:
        names = self.searchable_tool_names()
        return names is None or str(tool_name or "").strip() in names


def runtime_effective_tool_scope(
    *,
    runtime_envelope: Any | None,
    context_allowed_tool_names: Iterable[str] | None = None,
) -> RuntimeEffectiveToolScope:
    """Resolve one authoritative scope without reconstructing Agent bindings.

    A Runtime envelope is the trusted snapshot for the active run. The hidden
    context allowlist is retained only for legacy callers that do not yet carry
    an envelope; it must never narrow or widen an existing envelope silently.
    Fresh execution authorization still revalidates durable bindings.
    """

    if runtime_envelope is not None:
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
        return RuntimeEffectiveToolScope(
            allowed_tool_names=frozenset(
                str(name).strip()
                for name in allowed
                if str(name or "").strip()
            ),
            blocked_tool_names=frozenset(
                str(name).strip()
                for name in (
                    getattr(runtime_envelope, "blocked_tool_names", ()) or ()
                )
                if str(name or "").strip()
            ),
            source="runtime_envelope",
        )

    if context_allowed_tool_names is None:
        return RuntimeEffectiveToolScope(
            allowed_tool_names=None,
            source="unscoped",
        )
    return RuntimeEffectiveToolScope(
        allowed_tool_names=frozenset(
            str(name).strip()
            for name in context_allowed_tool_names
            if str(name or "").strip()
        ),
        source="legacy_context",
    )


@dataclass(slots=True)
class RuntimeDiscoveredToolGrant:
    """Run-local execution metadata proven by live MCP discovery."""

    name: str
    effect: str | None = None
    account_ids: tuple[str, ...] = ()
    requires_explicit_account: bool = False
    supports_all_accounts: bool | None = None
    incomplete_account_ids: tuple[str, ...] = ()
    account_registry_snapshot: RuntimeDynamicMCPAccountRegistrySnapshot | None = None


@dataclass(slots=True)
class RuntimeDiscoveredToolGrants:
    """Ephemeral tool names granted by this run's trusted discovery path."""

    tool_names: set[str] = field(default_factory=set)
    metadata_by_name: dict[str, RuntimeDiscoveredToolGrant] = field(
        default_factory=dict
    )

    def grant(self, tools: Iterable[str | RuntimeDiscoveredToolGrant]) -> None:
        for item in tools:
            if isinstance(item, RuntimeDiscoveredToolGrant):
                name = str(item.name or "").strip()
                if name:
                    self.tool_names.add(name)
                    self.metadata_by_name[name] = item
                continue
            name = str(item or "").strip()
            if name:
                self.tool_names.add(name)

    def allows(self, tool_name: str) -> bool:
        return str(tool_name or "").strip() in self.tool_names

    def metadata(self, tool_name: str) -> RuntimeDiscoveredToolGrant | None:
        return self.metadata_by_name.get(str(tool_name or "").strip())


@dataclass(frozen=True)
class RuntimeEnvelope:
    """Trace-level description of one Manor AI run."""

    surface: ChatSurface
    principal: RuntimePrincipal
    profile: RuntimeProfile
    tool_profile: str | None = None
    entity_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    thread_ref_kind: str | None = None
    thread_ref_id: str | None = None
    tool_names: tuple[str, ...] = ()
    allowed_tool_names: tuple[str, ...] = ()
    blocked_tool_names: tuple[str, ...] = ()
    # Semantic MCP authority for this run. Exact tool names alone cannot
    # represent a provider wildcard when vendors add actions after startup.
    mcp_provider_scopes: tuple[RuntimeMCPProviderToolScope, ...] = ()
    mcp_scope_unrestricted: bool = False
    capability_ids: tuple[str, ...] = ()
    tool_bindings: tuple[dict[str, Any], ...] = ()
    unclassified_tool_names: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    skill_descriptors: tuple[dict[str, Any], ...] = ()
    memory_mounts: tuple[str, ...] = ()
    file_context_mounts: tuple[str, ...] = ()
    subagent_names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    discovered_tool_grants: RuntimeDiscoveredToolGrants = field(
        default_factory=RuntimeDiscoveredToolGrants,
        compare=False,
        repr=False,
    )

    def effective_allowed_tool_names(self) -> set[str]:
        return (
            set(self.allowed_tool_names)
            | set(self.discovered_tool_grants.tool_names)
        ) - set(self.blocked_tool_names)

    def to_trace_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("discovered_tool_grants", None)
        data["mcp_provider_scopes"] = tuple(
            {
                "provider": item.provider,
                "allowed_actions": (
                    None
                    if item.allowed_actions is None
                    else tuple(sorted(item.allowed_actions))
                ),
            }
            for item in self.mcp_provider_scopes
        )
        if self.discovered_tool_grants.tool_names:
            data["discovered_tool_names"] = sorted(
                self.discovered_tool_grants.tool_names
            )
        data["surface"] = self.surface.value
        data["profile"] = self.profile.value
        data["principal"]["kind"] = self.principal.kind.value
        return data

    def to_message_meta(self) -> dict[str, Any]:
        data = self.to_trace_dict()
        # Discovery grants are capability state for the active run, not a
        # persisted permission that a later turn may replay.
        data.pop("discovered_tool_names", None)
        # Provider wildcards are also run-local authorization state. A later
        # message must re-resolve durable Agent bindings instead of replaying
        # this snapshot as permission.
        data.pop("mcp_provider_scopes", None)
        data.pop("mcp_scope_unrestricted", None)
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        data["metadata"] = sanitize_sensitive_payload({
            key: value
            for key, value in metadata.items()
            if key in {
                "legacy_path",
                "disable_tools",
                "turn_execution_plan",
                "tool_surface_policy",
                "runtime_events",
                "runtime_attachment_context",
                "runtime_file_context_mounts",
                "runtime_memory_mounts",
                "runtime_middleware",
                "runtime_resolver_middleware",
                "runtime_skill_descriptors",
                "runtime_subagents",
            }
        })
        return data
