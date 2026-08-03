from __future__ import annotations

from typing import Any, Iterable

from packages.core.ai.runtime.tool_visibility import (
    RuntimeToolRegistrySurface,
    runtime_tool_registry_surface_from_schemas,
)
from packages.core.ai.runtime.tool_discovery import runtime_mcp_tool_names_for_active_intent


def _tool_pool():
    from packages.core.ai.tool_pool import tool_pool

    return tool_pool


def runtime_ensure_tool_registry_initialized():
    """Return the ToolPool after ensuring its registry is loaded."""

    pool = _tool_pool()
    if not pool.tool_count:
        pool.initialize()
    return pool


def runtime_tool_count() -> int:
    return int(_tool_pool().tool_count)


def runtime_tool_schema(name: str) -> dict | None:
    return runtime_ensure_tool_registry_initialized().get_schema(name)


def runtime_tool_schemas_for_names(names: Iterable[str]) -> list[dict]:
    return runtime_ensure_tool_registry_initialized().get_schemas_for_names(names)


def runtime_registered_tool_names(*, prefix: str | None = None) -> tuple[str, ...]:
    return runtime_ensure_tool_registry_initialized().registered_tool_names(prefix=prefix)


def runtime_registered_tool_schemas() -> tuple[tuple[str, dict], ...]:
    return runtime_ensure_tool_registry_initialized().registered_tool_schemas()


def runtime_registered_tool_surface_from_schemas(
    tool_schemas: Iterable[tuple[str, dict]],
    *,
    bound_tool_names: set[str] | None = None,
    is_master: bool = False,
    mcp_allowed_names: set[str] | None = None,
    tool_profile: str | None = None,
) -> RuntimeToolRegistrySurface:
    """Resolve prompt-visible schemas and executable names from registry data.

    This keeps agent/profile surface resolution in the runtime layer; ToolPool
    remains a registry/executor and does not own per-entrypoint visibility.
    """

    return runtime_tool_registry_surface_from_schemas(
        tool_schemas,
        bound_tool_names=bound_tool_names,
        is_master=is_master,
        mcp_allowed_names=mcp_allowed_names,
        tool_profile=tool_profile,
    )


def runtime_registered_mcp_tool_names_for_active_intent(
    active_user_message: str | None,
) -> set[str]:
    """Return registered MCP tools whose provider is named in the current turn."""

    return runtime_mcp_tool_names_for_active_intent(
        tool_names=runtime_registered_tool_names(prefix="mcp__"),
        active_user_message=active_user_message,
    )


def runtime_tool_schemas_for_agent(
    agent_id: str | None,
    *,
    bound_tool_names: set[str] | None = None,
    is_master: bool = False,
    mcp_allowed_names: set[str] | None = None,
    tool_profile: str | None = None,
) -> tuple[list[dict], set[str]]:
    """Resolve prompt-visible schemas and executable names for an agent turn."""

    pool = runtime_ensure_tool_registry_initialized()
    surface = runtime_registered_tool_surface_from_schemas(
        pool.registered_tool_schemas(),
        bound_tool_names=bound_tool_names,
        is_master=is_master,
        mcp_allowed_names=mcp_allowed_names,
        tool_profile=tool_profile,
    )
    return list(surface.prompt_schemas), set(surface.visible_tool_names)


async def runtime_execute_tool(
    name: str,
    args: dict,
    **kwargs: Any,
) -> str:
    registry = runtime_ensure_tool_registry_initialized()
    result = await registry.execute(name, args, **kwargs)

    # A provider can demand its own per-action confirmation. When the operator
    # has already said "Always approve" for that provider action, answer the
    # gate here instead of asking again: confirm and retry inside this tool
    # call, so the model never sees an approval it cannot resolve and cannot
    # improvise a second, unapproved attempt (the loop this fixes).
    #
    # The confirm/retry calls go straight to the registry, NOT back through
    # this function: routing them through here would feed each retry's result
    # into auto-confirm again, and a retry that is itself gated would recurse
    # without end. One auto-answer per tool call, then the normal card flow.
    from packages.core.ai.runtime.approval_service import (
        runtime_auto_confirm_provider_approval,
    )

    async def _execute_without_auto_confirm(retry_name: str, retry_args: dict) -> str:
        return await registry.execute(retry_name, retry_args, **kwargs)

    return await runtime_auto_confirm_provider_approval(
        tool_name=name,
        arguments=args,
        result=result,
        execute=_execute_without_auto_confirm,
        entity_id=str(kwargs.get("entity_id") or ""),
        user_id=kwargs.get("user_id"),
        workspace_id=kwargs.get("workspace_id"),
    )
