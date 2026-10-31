"""In-process, server-owned capability replacement for an existing Agent run.

Public tool text is only an observation. It cannot recreate this typed carrier
after JSON serialization; a resumed run resolves durable configuration afresh.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from packages.core.ai.runtime.control import RuntimeTurnAborted
from packages.core.ai.runtime.envelope import RuntimeEnvelope


@dataclass(frozen=True)
class _CapabilityRefresh:
    envelope: RuntimeEnvelope
    allowed_tool_names: frozenset[str]
    tools: tuple[dict[str, Any], ...]


class _RuntimeCapabilityRefreshResult(str):
    _refresh: _CapabilityRefresh


class _AppliedCapabilityRefreshResult(_RuntimeCapabilityRefreshResult):
    """Only the harness may mint this after validating the active identity."""


def runtime_capability_refresh_result(
    public_result: str,
    *,
    runtime_envelope: RuntimeEnvelope,
    allowed_tool_names: Iterable[str],
    tools: list[dict[str, Any]],
) -> str:
    """Carry a freshly rebuilt scope from a trusted host acquisition handler."""
    if not isinstance(runtime_envelope, RuntimeEnvelope):
        raise TypeError("Capability refresh requires a RuntimeEnvelope")
    result = _RuntimeCapabilityRefreshResult(public_result)
    result._refresh = _CapabilityRefresh(
        envelope=deepcopy(runtime_envelope),
        allowed_tool_names=frozenset(allowed_tool_names),
        tools=tuple(deepcopy(tools)),
    )
    return result


def is_runtime_capability_refresh(result: Any) -> bool:
    return isinstance(result, _RuntimeCapabilityRefreshResult)


def runtime_apply_capability_refresh(
    result: str,
    *,
    current_envelope: RuntimeEnvelope | None,
    control_tools: list[dict[str, Any]],
) -> tuple[RuntimeEnvelope, set[str], str]:
    """Validate an exact run, replace its scope, and preserve host controls.

    No old business schema, wildcard, discovery grant, or allowlist is merged
    back into the rebuilt scope. Controls are supplied by the host, not the
    acquisition result. Execution still performs its normal live auth checks.
    """
    if not is_runtime_capability_refresh(result) or current_envelope is None:
        raise RuntimeTurnAborted("Capability refresh requires an active scoped run")
    refresh = result._refresh
    envelope = refresh.envelope
    identity_fields = (
        "surface", "principal", "profile", "tool_profile", "entity_id", "user_id",
        "agent_id", "workspace_id", "conversation_id", "task_id",
        "thread_ref_kind", "thread_ref_id", "memory_mounts", "file_context_mounts",
        "subagent_names",
    )
    if any(getattr(envelope, key) != getattr(current_envelope, key) for key in identity_fields):
        raise RuntimeTurnAborted("Capability refresh changed the active runtime identity")
    for key in (
        "agent_subscription_id", "subscription_id", "step_id", "plan_id",
        "lease_id", "run_id", "runtime_run_id",
    ):
        if envelope.metadata.get(key) != current_envelope.metadata.get(key):
            raise RuntimeTurnAborted("Capability refresh changed the active runtime lineage")

    allowed = set(refresh.allowed_tool_names)
    if allowed != envelope.effective_allowed_tool_names():
        raise RuntimeTurnAborted("Capability refresh has conflicting tool scopes")
    schemas: dict[str, dict[str, Any]] = {}
    for schema in refresh.tools:
        function = schema.get("function") if isinstance(schema, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name or name not in allowed:
            raise RuntimeTurnAborted("Capability refresh contains an out-of-scope schema")
        if name in schemas:
            raise RuntimeTurnAborted("Capability refresh contains duplicate tool schemas")
        schemas[name] = deepcopy(schema)
    for schema in control_tools:
        name = str((schema.get("function") or {}).get("name") or "")
        if name and name not in envelope.blocked_tool_names:
            # The host's control schema wins over any rebuilt business catalog.
            schemas[name] = deepcopy(schema)
            allowed.add(name)
    # Acquisition can add business capabilities, but cannot remove a host's
    # per-run restrictions (including controls this host cannot resume).
    blocked = set(current_envelope.blocked_tool_names) | set(envelope.blocked_tool_names)
    allowed.difference_update(blocked)
    schemas = {name: schema for name, schema in schemas.items() if name not in blocked}
    envelope = replace(
        envelope,
        tool_names=tuple(schemas),
        allowed_tool_names=tuple(sorted(allowed)),
        blocked_tool_names=tuple(sorted(blocked)),
        # Approval state, trace events and runtime identities are not config.
        metadata=current_envelope.metadata,
    )
    applied = _AppliedCapabilityRefreshResult(str(result))
    applied._refresh = _CapabilityRefresh(envelope, frozenset(allowed), tuple(schemas.values()))
    return envelope, allowed, applied


class RuntimeToolBindingReach(StrEnum):
    """Where binding one first-party tool to an Agent would leave it."""

    IN_SCOPE = "in_scope"  # already callable in this run: nothing to bind
    BINDABLE = "bindable"  # a binding puts it into the Agent's runtime scope
    POLICY_DENIED = "policy_denied"  # governance or this run's surface/profile forbids it
    NOT_EXPOSED = "not_exposed"  # the runtime never exposes it to a bound Agent
    NOT_REGISTERED = "not_registered"  # this deployment does not run it


_REACH_MESSAGES = {
    RuntimeToolBindingReach.POLICY_DENIED: (
        "{name} is blocked for this Agent by the Workspace's governance or by this run's surface; "
        "binding it cannot make it callable. Only the user can change that policy."
    ),
    RuntimeToolBindingReach.NOT_EXPOSED: (
        "{name} is never exposed to a Workspace Agent's runtime scope, even when bound; "
        "binding it cannot make it callable. Use the tool that covers it or another route."
    ),
    RuntimeToolBindingReach.NOT_REGISTERED: (
        "{name} is not available on this deployment; binding it cannot make it callable."
    ),
}


def runtime_tool_binding_reach(
    name: str,
    *,
    envelope: RuntimeEnvelope,
    registered_tool_schemas: Iterable[tuple[str, dict]] | None = None,
) -> RuntimeToolBindingReach:
    """Predict, with the runtime's own resolution, what binding ``name`` achieves.

    Capability acquisition must agree with the scope the run will actually
    get: a tool already in ``envelope`` needs no binding, and a tool the
    surface/profile/governance resolution would still withhold after binding
    is a precise refusal, never a "ready" request that applies as a no-op.
    """
    from packages.core.ai.runtime.policies import filter_runtime_tools
    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_surface_from_schemas

    name = str(name or "").strip()
    if name in envelope.effective_allowed_tool_names():
        return RuntimeToolBindingReach.IN_SCOPE
    if registered_tool_schemas is None:
        from packages.core.ai.runtime.tool_registry import runtime_registered_tool_schemas

        registered_tool_schemas = runtime_registered_tool_schemas()
    schemas = tuple(registered_tool_schemas)
    if name not in {schema_name for schema_name, _ in schemas}:
        return RuntimeToolBindingReach.NOT_REGISTERED
    if name in set(envelope.blocked_tool_names):
        return RuntimeToolBindingReach.POLICY_DENIED
    # The same surface resolution a turn runs, for this Agent's current scope
    # plus the binding: co-bound tools can change what a binding exposes.
    surface = runtime_registered_tool_surface_from_schemas(
        schemas, bound_tool_names={*envelope.allowed_tool_names, name}, is_master=False,
        mcp_allowed_names=set(), tool_profile=envelope.tool_profile,
    )
    if name not in surface.visible_tool_names:
        return RuntimeToolBindingReach.NOT_EXPOSED
    _, allowed, blocked = filter_runtime_tools(
        surface=envelope.surface, profile=envelope.profile, tools=(), allowed_tool_names={name},
    )
    if name not in allowed or name in blocked:
        return RuntimeToolBindingReach.POLICY_DENIED
    return RuntimeToolBindingReach.BINDABLE


def runtime_tool_binding_reach_message(name: str, reach: RuntimeToolBindingReach) -> str | None:
    template = _REACH_MESSAGES.get(reach)
    return template.format(name=name) if template else None


def runtime_applied_capability_schemas(result: Any) -> list[dict[str, Any]] | None:
    """Return schemas only from a harness-validated in-memory result."""
    if not isinstance(result, _AppliedCapabilityRefreshResult):
        return None
    return deepcopy(list(result._refresh.tools))


def runtime_applied_capability_envelope(result: Any) -> RuntimeEnvelope | None:
    """Share the active envelope's live discovery grants with the loop."""
    if not isinstance(result, _AppliedCapabilityRefreshResult):
        return None
    return result._refresh.envelope
