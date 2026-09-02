"""Runtime boundary for the composite Workspace gateway."""

from __future__ import annotations

from typing import Any


async def runtime_workspace_composite_action(
    *,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    conversation_id: str,
    action: str,
    params: dict[str, Any],
    runtime_tool_kwargs: dict[str, Any] | None = None,
) -> str:
    """Dispatch one normalized Workspace action through the compatibility layer.

    The import remains inside Runtime so public tools never import and call one
    another directly. The historical ``workspace_agent`` handler stays the
    single compatibility implementation while ``manor(action='workspace')``
    is the prompt-visible gateway.
    """

    from packages.core.ai.tools.workspace_agent_tools import (
        _workspace_agent_handler,
    )

    injected_kwargs = dict(runtime_tool_kwargs or {})
    injected_kwargs.pop("workspace_id", None)
    injected_kwargs.pop("conversation_id", None)
    return await _workspace_agent_handler(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        action=action,
        params=params,
        **injected_kwargs,
    )


async def runtime_visualize_workspace_ledgers_action(
    *,
    entity_id: str,
    tool_kwargs: dict[str, Any],
) -> str:
    """Render Workspace Ledger evidence through a Runtime-owned bridge."""

    from packages.core.ai.tools.ledger_query_tools import (
        _visualize_workspace_ledgers,
    )

    return await _visualize_workspace_ledgers(
        entity_id=entity_id,
        **tool_kwargs,
    )


__all__ = [
    "runtime_visualize_workspace_ledgers_action",
    "runtime_workspace_composite_action",
]
