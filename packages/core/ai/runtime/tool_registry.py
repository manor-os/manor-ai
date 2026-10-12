from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS
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


def runtime_tool_guidance_skill(name: str) -> str | None:
    """Read a repository-registered guide, never model/MCP schema metadata.

    Real tool surfaces initialize the registry before starting a loop. Do not
    initialize new capabilities just to load documentation for a custom loop.
    """

    entry = _tool_pool().get(name)
    return entry.get("guidance_skill") if entry else None


def runtime_tool_schema(name: str) -> dict | None:
    return runtime_ensure_tool_registry_initialized().get_schema(name)


async def runtime_tool_schema_for_actor(
    name: str,
    *,
    entity_id: str,
    user_id: str,
) -> dict | None:
    """Return the current static or actor-scoped dynamic schema for execution."""

    return await runtime_ensure_tool_registry_initialized().get_schema_for_actor(
        name,
        entity_id=entity_id,
        user_id=user_id,
    )


def runtime_tool_schemas_for_names(names: Iterable[str]) -> list[dict]:
    return runtime_ensure_tool_registry_initialized().get_schemas_for_names(names)


def runtime_registered_tool_names(
    *,
    prefix: str | None = None,
    include_undiscoverable: bool = False,
) -> tuple[str, ...]:
    return runtime_ensure_tool_registry_initialized().registered_tool_names(
        prefix=prefix,
        include_undiscoverable=include_undiscoverable,
    )


def runtime_registered_tool_schemas() -> tuple[tuple[str, dict], ...]:
    return runtime_ensure_tool_registry_initialized().registered_tool_schemas()


async def runtime_discover_tools(
    *, arguments: dict[str, Any], entity_id: str = "", user_id: str = "",
) -> str:
    """Share search_tools' scoped discovery and dynamic schema registration.

    This is only a discovery alias, never a generic handler-execution bypass.
    The search handler revalidates the runtime scope and actor's accounts;
    loading a schema still does not authorize a business operation.
    """
    entry = runtime_ensure_tool_registry_initialized().get("search_tools")
    return await entry["handler"](entity_id=entity_id, user_id=user_id, **arguments)


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


async def runtime_execute_tool_dependency(
    name: str,
    args: dict,
    **kwargs: Any,
) -> Any:
    """Execute a server-selected dependency with its own Runtime authorization.

    Composite handlers use this for their implementation dependencies, never
    for arbitrary model-selected tools. The child keeps the parent's principal,
    surface, hard denials and event stream. Its exact tool allowance is local
    to this call; it does not grant new tools to the parent's Agent loop.
    """
    envelope = kwargs.get("runtime_envelope")
    if envelope is not None:
        kwargs["runtime_envelope"] = replace(
            envelope,
            tool_names=(name,),
            allowed_tool_names=(name,),
        )
    kwargs["allowed_tool_names"] = {name}
    return await runtime_execute_tool(name, args, **kwargs)


async def runtime_execute_tool(
    name: str,
    args: dict,
    **kwargs: Any,
) -> Any:
    registry = runtime_ensure_tool_registry_initialized()
    result = await registry.execute(name, args, **kwargs)
    from packages.core.ai.runtime.control import is_runtime_tool_suspension

    if is_runtime_tool_suspension(result):
        return result

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
        continuation_kwargs = dict(kwargs)
        continuation_args = dict(retry_args)
        continuation_args.update({
            key: value
            for key, value in args.items()
            if key in RUNTIME_TOOL_CONTEXT_KEYS and key.startswith("_")
        })
        allowed_tool_names = set(kwargs.get("allowed_tool_names") or ())
        allowed_tool_names.add(retry_name)
        continuation_kwargs["allowed_tool_names"] = allowed_tool_names
        runtime_envelope = kwargs.get("runtime_envelope")
        if runtime_envelope is not None:
            continuation_kwargs["runtime_envelope"] = replace(
                runtime_envelope,
                tool_names=tuple(dict.fromkeys((
                    *runtime_envelope.tool_names,
                    retry_name,
                ))),
                allowed_tool_names=tuple(dict.fromkeys((
                    *runtime_envelope.allowed_tool_names,
                    retry_name,
                ))),
            )
        return await registry.execute(
            retry_name,
            continuation_args,
            **continuation_kwargs,
        )

    workflow_context = {
        "workflow_run_id": args.get("_workflow_run_id_from_context"),
        "workflow_lineage_root_run_id": args.get(
            "_workflow_lineage_root_run_id_from_context"
        ),
        "workflow_action_grant_id": args.get(
            "_workflow_action_grant_id_from_context"
        ),
        "workflow_step_id": args.get("_workflow_step_id_from_context"),
    }

    return await runtime_auto_confirm_provider_approval(
        tool_name=name,
        arguments=args,
        result=result,
        execute=_execute_without_auto_confirm,
        entity_id=str(kwargs.get("entity_id") or ""),
        user_id=kwargs.get("user_id"),
        workspace_id=kwargs.get("workspace_id"),
        task_id=kwargs.get("task_id"),
        runtime_metadata=(
            getattr(kwargs.get("runtime_envelope"), "metadata", None)
            if kwargs.get("runtime_envelope") is not None
            else None
        ),
        runtime_surface=(
            getattr(kwargs.get("runtime_envelope"), "surface", None)
            if kwargs.get("runtime_envelope") is not None
            else None
        ),
        **workflow_context,
    )
