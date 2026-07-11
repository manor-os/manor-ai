from __future__ import annotations

from typing import Any


def runtime_prompt_tool_name(tool: Any) -> str:
    fn = tool.get("function", tool) if isinstance(tool, dict) else {}
    return str(fn.get("name") or (tool.get("name") if isinstance(tool, dict) else "") or "").strip()


def runtime_normalize_tool_name_set(
    names: list[str] | tuple[str, ...] | set[str] | str | None,
) -> set[str]:
    if not names:
        return set()
    values = names.split(",") if isinstance(names, str) else names
    return {str(name).strip() for name in values if str(name or "").strip()}


def runtime_filter_blocked_tools(
    tools: list[dict],
    blocked_tool_names: set[str] | list[str] | tuple[str, ...] | str | None,
) -> list[dict]:
    blocked = runtime_normalize_tool_name_set(blocked_tool_names)
    if not blocked:
        return tools
    return [
        tool
        for tool in tools
        if runtime_prompt_tool_name(tool) not in blocked
    ]


def runtime_set_tools_for_prompt_context(
    ctx: Any,
    *,
    tools: list[dict],
    allowed_tool_names: set[str] | None = None,
) -> None:
    """Attach the resolved runtime tool surface before prompt sections run."""
    ctx.tools = list(tools or [])
    ctx.tool_names = [
        name for name in (runtime_prompt_tool_name(tool) for tool in ctx.tools) if name
    ]
    ctx.allowed_tool_names = (
        set(ctx.tool_names) if allowed_tool_names is None else set(allowed_tool_names)
    )


def runtime_populate_tools_for_prompt_context(
    ctx: Any,
    *,
    agent_id: str | None,
    bound_tool_names: set[str] | None,
    is_master: bool,
    mcp_allowed_names: set[str] | None,
    legacy_tool_profile: str | None,
) -> tuple[list[dict], set[str]]:
    """Resolve turn-scoped tools and attach them to a prompt context."""
    from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_agent
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_surface import runtime_public_agent_tool_surface

    tools, allowed_tool_names = runtime_tool_schemas_for_agent(
        agent_id,
        bound_tool_names=bound_tool_names,
        is_master=is_master,
        mcp_allowed_names=mcp_allowed_names,
        legacy_tool_profile=legacy_tool_profile,
    )
    try:
        surface = ChatSurface(str(getattr(ctx, "runtime_surface", "") or ""))
    except ValueError:
        surface = None
    if surface is not None:
        public_surface = runtime_public_agent_tool_surface(
            surface=surface,
            bound_tool_names=bound_tool_names,
            mcp_allowed_names=mcp_allowed_names,
            allowed_tool_names=allowed_tool_names,
        )
        if public_surface is not None:
            tools, allowed_tool_names = public_surface
    runtime_set_tools_for_prompt_context(
        ctx,
        tools=tools,
        allowed_tool_names=allowed_tool_names,
    )
    return tools, allowed_tool_names


def runtime_populate_named_tools_for_prompt_context(
    ctx: Any,
    *,
    tool_names: list[str] | tuple[str, ...] | set[str] | str | None,
) -> tuple[list[dict], set[str]]:
    """Resolve an explicit configured tool-name list for a prompt context."""
    from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_names

    allowed_tool_names = runtime_normalize_tool_name_set(tool_names)
    tools = runtime_tool_schemas_for_names(sorted(allowed_tool_names))
    runtime_set_tools_for_prompt_context(
        ctx,
        tools=tools,
        allowed_tool_names=allowed_tool_names,
    )
    return tools, allowed_tool_names
