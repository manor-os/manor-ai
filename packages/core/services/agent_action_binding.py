"""One predicate for "may this agent execute provider.action_key?".

The planner (``_enforce_allowlists`` via ``_compute_service_provider_actions``)
and the dispatcher (``_agent_allows_action_step``) used to answer this
question from two separately written readers of the same tables. Any drift
between them turned into a step that planned fine and then failed at lease
time with ``ActionBindingDenied`` — a class of mid-run failure the user could
not have prevented. Both layers now call this function.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


ACTION_BINDING_DENIED_ERROR_TYPE = "ActionBindingDenied"


async def agent_action_binding_allowed(
    db: AsyncSession,
    *,
    agent_id: str | None,
    provider: str | None,
    action_key: str | None,
) -> tuple[bool, dict[str, Any] | None]:
    """Check the resolved service agent owns the MCP action being leased.

    Returns ``(True, None)`` when allowed, else ``(False, error_dict)`` where
    ``error_dict`` is the exact step.error payload the dispatcher records.
    """

    provider_key = str(provider or "").strip()
    action = str(action_key or "").strip()
    if provider_key == "browser_setup":
        return True, None
    if not agent_id or not provider_key or not action:
        return False, {
            "type": ACTION_BINDING_DENIED_ERROR_TYPE,
            "message": "action step is missing resolved agent, provider, or action_key",
            "provider": provider_key,
            "action_key": action,
        }

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    row = (
        await db.execute(
            select(AgentMCPBinding, MCPServer)
            .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
            .where(
                AgentMCPBinding.agent_id == agent_id,
                AgentMCPBinding.status == "active",
                MCPServer.server_key == provider_key,
                MCPServer.status == "active",
            )
            .limit(1)
        )
    ).first()
    from packages.core.services.agent_permission_service import resolve_agent_direct_mcp_actions

    direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
    if action in direct_actions.get(provider_key, set()):
        return True, None

    if not row:
        return False, {
            "type": ACTION_BINDING_DENIED_ERROR_TYPE,
            "message": (
                f"agent {agent_id} is not bound to MCP provider "
                f"{provider_key!r}"
            ),
            "provider": provider_key,
            "action_key": action,
            "agent_id": agent_id,
        }

    binding, server = row
    allowed_tools = (
        binding.allowed_tools
        if binding.allowed_tools is not None
        else server.default_allowed_tools
    )
    if allowed_tools is None:
        return True, None

    allowed = {str(name) for name in allowed_tools if str(name or "").strip()}
    qualified = f"mcp__{provider_key}__{action}"
    if action in allowed or qualified in allowed:
        return True, None

    return False, {
        "type": ACTION_BINDING_DENIED_ERROR_TYPE,
        "message": (
            f"agent {agent_id} is not allowed to execute "
            f"{provider_key}.{action}"
        ),
        "provider": provider_key,
        "action_key": action,
        "agent_id": agent_id,
        "allowed_tools": sorted(allowed),
    }
