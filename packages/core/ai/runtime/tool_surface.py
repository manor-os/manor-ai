from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.prompt_tools import (
    runtime_normalize_tool_name_set,
    runtime_prompt_tool_name,
)
from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.resolver import ResolvedRuntimeToolSurface, RuntimeResolver
from packages.core.ai.runtime.surfaces import ChatSurface


def runtime_public_agent_tool_surface(
    *,
    surface: ChatSurface,
    bound_tool_names: set[str] | None,
    mcp_allowed_names: set[str] | None = None,
    allowed_tool_names: set[str] | None = None,
) -> tuple[list[dict], set[str]] | None:
    """Return the strict public-agent tool surface, or None for other surfaces."""

    if surface != ChatSurface.PUBLIC_CUSTOMER_CHAT:
        return None
    from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_names

    bound = set(bound_tool_names or set())
    bound.update(mcp_allowed_names or set())
    allowed = set(allowed_tool_names) if allowed_tool_names is not None else None
    if "workspace_agent" in bound:
        bound.add("workspace_create_task")
        if allowed is not None and "workspace_agent" in allowed:
            allowed.add("workspace_create_task")
    if allowed is not None:
        bound &= allowed
    bound.discard("workspace_agent")
    bound.discard("workspace_operation")
    bound.discard("manor")
    bound.discard("workspace_update_task_runtime")
    return runtime_tool_schemas_for_names(sorted(bound)), bound


def runtime_prepare_tool_surface_for_turn(
    request: AIRuntimeRequest,
    *,
    legacy_runtime_profile: str | None = None,
    tool_schemas: Iterable[dict[str, Any]] | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict[str, Any]] | None = None,
) -> ResolvedRuntimeToolSurface:
    """Resolve the tool surface for one Manor AI runtime turn.

    Entry points should call this adapter instead of constructing
    ``RuntimeResolver`` directly; resolver stage composition is a runtime
    concern, while services/runners only own their product-specific inputs.
    """

    return RuntimeResolver().resolve_tool_surface(
        request,
        legacy_runtime_profile=legacy_runtime_profile,
        tool_schemas=tool_schemas,
        allowed_tool_names=allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
    )


def runtime_prepare_named_tool_surface_for_turn(
    request: AIRuntimeRequest,
    *,
    tool_names: Iterable[str] | str | None,
    legacy_runtime_profile: str | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict[str, Any]] | None = None,
) -> ResolvedRuntimeToolSurface:
    """Resolve an explicit configured tool-name list through RuntimeResolver."""
    from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_names

    allowed_tool_names = runtime_normalize_tool_name_set(tool_names)
    return runtime_prepare_tool_surface_for_turn(
        request,
        legacy_runtime_profile=legacy_runtime_profile,
        tool_schemas=runtime_tool_schemas_for_names(sorted(allowed_tool_names)),
        allowed_tool_names=allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
    )


def runtime_prepare_local_tool_surface_for_turn(
    request: AIRuntimeRequest,
    *,
    tool_schemas: Iterable[dict[str, Any]],
    allowed_tool_names: Iterable[str] | str | None = None,
    legacy_runtime_profile: str | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict[str, Any]] | None = None,
) -> ResolvedRuntimeToolSurface:
    """Resolve a caller-owned local tool schema list through RuntimeResolver."""

    local_tools = list(tool_schemas or ())
    derived_allowed = {
        name for name in (runtime_prompt_tool_name(schema) for schema in local_tools) if name
    }
    explicit_allowed = runtime_normalize_tool_name_set(allowed_tool_names)
    if explicit_allowed:
        derived_allowed &= explicit_allowed
    return runtime_prepare_tool_surface_for_turn(
        request,
        legacy_runtime_profile=legacy_runtime_profile,
        tool_schemas=local_tools,
        allowed_tool_names=derived_allowed,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
    )


def runtime_prepare_agent_tool_surface_for_turn(
    request: AIRuntimeRequest,
    *,
    agent_id: str | None = None,
    bound_tool_names: set[str] | None = None,
    is_master: bool = False,
    mcp_allowed_names: set[str] | None = None,
    legacy_runtime_profile: str | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict[str, Any]] | None = None,
) -> ResolvedRuntimeToolSurface:
    """Resolve an agent/profile tool surface without rendering a prompt."""
    from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_agent

    tools, allowed_tool_names = runtime_tool_schemas_for_agent(
        agent_id,
        bound_tool_names=bound_tool_names,
        is_master=is_master,
        mcp_allowed_names=mcp_allowed_names,
        legacy_tool_profile=legacy_runtime_profile,
    )
    public_surface = runtime_public_agent_tool_surface(
        surface=request.surface,
        bound_tool_names=bound_tool_names,
        mcp_allowed_names=mcp_allowed_names,
        allowed_tool_names=allowed_tool_names,
    )
    if public_surface is not None:
        tools, allowed_tool_names = public_surface
    return runtime_prepare_tool_surface_for_turn(
        request,
        legacy_runtime_profile=legacy_runtime_profile,
        tool_schemas=tools,
        allowed_tool_names=allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
    )


def runtime_prepare_trace_envelope_for_turn(
    request: AIRuntimeRequest,
    *,
    legacy_runtime_profile: str | None = None,
    tool_schemas: Iterable[dict[str, Any]] | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict[str, Any]] | None = None,
) -> RuntimeEnvelope:
    """Resolve a runtime envelope for turns that do not expose tools."""

    return RuntimeResolver().resolve_trace_envelope(
        request,
        legacy_runtime_profile=legacy_runtime_profile,
        tool_schemas=tool_schemas,
        allowed_tool_names=allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
    )
