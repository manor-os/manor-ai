"""Read-only projection of the scheduled Agent runtime, never a tool grant.

Configuration catalogs answer what an actor may bind. This projection answers
what one installed service can currently expose to its worker. It shares the
worker's runtime resolver and does not seed catalogs, discover remote tools,
mutate bindings, or replace execution-time authorization and approval.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.planning import (
    RuntimePlannerCapabilityCatalogEntry,
    runtime_planner_action_specs_from_tools_cached,
    runtime_planner_capability_catalog,
)
from packages.core.ai.runtime.tool_bindings import RuntimeMCPProviderToolScope
from packages.core.models.workspace import Agent, AgentSubscription

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffectiveAgentCapabilities:
    service_key: str
    subscription_id: str
    agent_id: str | None
    agent_name: str
    tool_names: tuple[str, ...] = ()
    mcp_tool_names: tuple[str, ...] = ()
    mcp_provider_scopes: tuple[RuntimeMCPProviderToolScope, ...] = ()
    provider_actions: dict[str, list[str]] = field(default_factory=dict)
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    skills: tuple[dict[str, Any], ...] = ()
    capabilities: tuple[RuntimePlannerCapabilityCatalogEntry, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        # This is scope resolution, not a provider connectivity/health claim.
        return not self.errors

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(entry.capability_id for entry in self.capabilities) if self.available else ()

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "service_key": self.service_key,
            "subscription_id": self.subscription_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "scope_status": "resolved" if self.available else "unavailable",
            "capability_ids": list(self.capability_ids),
            **({"errors": list(self.errors)} if self.errors else {}),
        }


class AgentEffectiveCapabilityFactory:
    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        entity_id: str,
        user_id: str | None,
        workspace_id: str | None,
        subscriptions: list[AgentSubscription],
        agents_by_id: dict[str, Agent],
        task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, EffectiveAgentCapabilities]:
        from packages.core.ai.runtime import (
            ChatSurface,
            runtime_prepare_agent_tool_surface_for_turn,
            runtime_request_for_chat_turn,
        )
        from packages.core.models.skill import AgentSkillBinding, Skill
        from packages.core.services.agent_permission_service import parse_mcp_tool_name
        from packages.core.services.workspace_runtime import resolve_workspace_runtime

        keys = [sub.service_key for sub in subscriptions]
        if len(set(keys)) != len(keys):
            raise ValueError("effective capability projection requires unique service keys")
        if not subscriptions:
            return {}

        agents_by_id = {
            key: agent for key, agent in agents_by_id.items()
            if getattr(agent, "entity_id", entity_id) in (None, entity_id)
        }

        actions_by_service, specs_by_service = await _compute_service_provider_actions(
            db, subscriptions=subscriptions, agents_by_id=agents_by_id,
            actor_user_id=user_id, entity_id=entity_id,
        )
        skills_by_agent: dict[str, list[dict[str, Any]]] = {}
        if agents_by_id:
            rows = (await db.execute(
                select(AgentSkillBinding.agent_id, Skill)
                .join(Skill, Skill.id == AgentSkillBinding.skill_id)
                .where(
                    AgentSkillBinding.agent_id.in_(list(agents_by_id)),
                    AgentSkillBinding.status == "active",
                    Skill.status == "active",
                    or_(Skill.entity_id == entity_id, Skill.entity_id.is_(None)),
                )
            )).all()
            for agent_id, skill in rows:
                skills_by_agent.setdefault(agent_id, []).append({
                    "slug": skill.slug or skill.name,
                    "name": skill.name,
                    "description": (skill.description or "")[:150],
                })

        result: dict[str, EffectiveAgentCapabilities] = {}
        for sub in subscriptions:
            agent = agents_by_id.get(sub.agent_id)
            identity = {
                "service_key": sub.service_key,
                "subscription_id": sub.id,
                "agent_id": sub.agent_id,
                "agent_name": str(getattr(agent, "name", "") or ""),
            }
            if agent is None or getattr(agent, "status", "active") != "active":
                result[sub.service_key] = EffectiveAgentCapabilities(
                    **identity, errors=("agent_unavailable",),
                )
                continue

            runtime = await resolve_workspace_runtime(
                db, entity_id=entity_id, user_id=user_id,
                agent_id=sub.agent_id, agent_subscription_id=sub.id,
                workspace_id=workspace_id, conversation_id=conversation_id,
                task_id=task_id, runtime_surface="scheduled_agent_run",
                include_prompt_context=False,
            )
            scope_errors = tuple(getattr(runtime, "tool_scope_errors", ()) or ())
            if scope_errors:
                result[sub.service_key] = EffectiveAgentCapabilities(
                    **identity, errors=scope_errors,
                )
                continue
            surface = runtime_prepare_agent_tool_surface_for_turn(
                runtime_request_for_chat_turn(
                    surface=ChatSurface.SCHEDULED_AGENT_RUN,
                    entity_id=entity_id, user_id=user_id, agent_id=sub.agent_id,
                    workspace_id=runtime.workspace_id, conversation_id=conversation_id,
                    task_id=runtime.task_id, thread_ref_kind=runtime.thread_ref_kind,
                    thread_ref_id=runtime.thread_ref_id, message="",
                    metadata={"agent_subscription_id": sub.id},
                ),
                agent_id=sub.agent_id, bound_tool_names=runtime.bound_tool_names,
                is_master=runtime.is_master, mcp_allowed_names=runtime.mcp_allowed_names,
                mcp_provider_scopes=runtime.mcp_provider_scopes,
                mcp_scope_unrestricted=runtime.mcp_scope_unrestricted,
                tool_profile=runtime.tool_profile,
            )
            effective = surface.allowed_tool_names - surface.blocked_tool_names
            tools = tuple(sorted(name for name in effective if not name.startswith("mcp__")))
            mcp_tools = tuple(sorted(name for name in effective if name.startswith("mcp__")))
            actions, specs = _filter_service_provider_actions_to_runtime_scope(
                actions_by_service.get(sub.service_key, {}),
                specs_by_service.get(sub.service_key, {}),
                exact_tool_names=set(runtime.mcp_allowed_names or ()) | set(mcp_tools),
                provider_scopes=tuple(runtime.mcp_provider_scopes),
                blocked_tool_names=frozenset(surface.blocked_tool_names),
            )
            # Deployment bindings can expose registered actions even without a
            # reusable AgentMCPBinding. Classify these without widening typed
            # action schemas to arbitrary, undiscovered provider operations.
            capability_actions = {provider: list(names) for provider, names in actions.items()}
            for name in mcp_tools:
                if parsed := parse_mcp_tool_name(name):
                    provider, action = parsed
                    capability_actions.setdefault(provider, []).append(action)
            skills = tuple(skills_by_agent.get(sub.agent_id, ()))
            result[sub.service_key] = EffectiveAgentCapabilities(
                **identity, tool_names=tools, mcp_tool_names=mcp_tools,
                mcp_provider_scopes=tuple(runtime.mcp_provider_scopes),
                provider_actions=actions, provider_action_specs=specs, skills=skills,
                capabilities=runtime_planner_capability_catalog(
                    provider_actions=capability_actions, provider_action_specs=specs,
                    platform_tools=tools, skills=skills if "invoke_skill" in tools else (),
                ),
            )
        return result


def _filter_service_provider_actions_to_runtime_scope(
    actions: dict[str, list[str]],
    specs: dict[str, dict[str, dict[str, Any]]],
    *,
    exact_tool_names: set[str],
    provider_scopes: tuple[RuntimeMCPProviderToolScope, ...],
    blocked_tool_names: frozenset[str] = frozenset(),
) -> tuple[dict[str, list[str]], dict[str, dict[str, dict[str, Any]]]]:
    """Keep only cached actions executable by this service's current MCP scope.

    An exact name may be granted by a Workspace deployment. Provider scopes
    retain wildcard/dynamic account discovery even before a schema is loaded
    into the static ToolPool. Neither path may borrow another service's grant.
    """
    filtered_actions: dict[str, list[str]] = {}
    filtered_specs: dict[str, dict[str, dict[str, Any]]] = {}
    for provider, available in actions.items():
        scoped = next(
            (scope for scope in provider_scopes if scope.provider == provider),
            None,
        )
        allowed = [
            action for action in available
            if f"mcp__{provider}__{action}" not in blocked_tool_names
            and (
                f"mcp__{provider}__{action}" in exact_tool_names
                or (scoped is not None and scoped.allows(action))
            )
        ]
        if allowed:
            filtered_actions[provider] = allowed
            filtered_specs[provider] = {
                action: specs.get(provider, {}).get(action, {}) for action in allowed
            }
    return filtered_actions, filtered_specs


async def _compute_service_provider_actions(
    db: AsyncSession,
    *,
    subscriptions: list[AgentSubscription],
    agents_by_id: dict[str, Agent],
    actor_user_id: str | None,
    entity_id: str,
) -> tuple[
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, dict[str, dict[str, Any]]]],
]:
    """Derive {service_key: {provider_key: [allowed_action_keys, ...]}}.

    Reads agent_mcp_bindings + mcp_servers.tools_cached. Empty when no
    bindings exist — Planner gets an empty action menu for that service and
    produces a pure-LLM/subagent plan, which is the right behaviour."""
    if not agents_by_id or not subscriptions:
        return {}, {}

    service_keys_by_agent: dict[str, set[str]] = {}
    for subscription in subscriptions:
        agent_id = getattr(subscription, "agent_id", None)
        service_key = str(getattr(subscription, "service_key", "") or "").strip()
        if agent_id in agents_by_id and service_key:
            service_keys_by_agent.setdefault(agent_id, set()).add(service_key)
    if not service_keys_by_agent:
        return {}, {}

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    bindings = list((await db.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id.in_(list(service_keys_by_agent.keys())),
            AgentMCPBinding.status == "active",
        )
    )).scalars().all())
    if not bindings:
        return {}, {}

    server_ids = {b.mcp_server_id for b in bindings}
    servers = {
        s.id: s for s in (await db.execute(
            select(MCPServer).where(
                MCPServer.id.in_(server_ids),
                MCPServer.status == "active",
            )
        )).scalars().all()
    }
    from packages.core.services.mcp_account_tool_catalog import (
        actor_provider_mcp_tool_caches,
    )
    from packages.core.services.official_remote_mcp import OfficialRemoteMCPProvider

    official_providers = {item.value for item in OfficialRemoteMCPProvider}
    provider_endpoints = {
        server.server_key: server.endpoint
        for server in servers.values()
        if server.server_key in official_providers and server.endpoint
    }
    try:
        discovered_tool_caches = await actor_provider_mcp_tool_caches(
            db,
            provider_endpoints=provider_endpoints,
            user_id=str(actor_user_id or ""),
            entity_id=entity_id,
        )
    except Exception:
        logger.warning(
            "Agent capability projection could not load actor-scoped MCP account catalogs; using server fallback",
            exc_info=True,
        )
        discovered_tool_caches = {}

    out: dict[str, dict[str, list[str]]] = {}
    spec_out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for b in bindings:
        srv = servers.get(b.mcp_server_id)
        if not srv:
            continue
        tool_specs = runtime_planner_action_specs_from_tools_cached(
            discovered_tool_caches.get(srv.server_key) or srv.tools_cached or []
        )
        all_tool_names = list(tool_specs)
        configured_allowed = b.allowed_tools if b.allowed_tools is not None else srv.default_allowed_tools
        if configured_allowed is not None:
            allowed_set = {str(n) for n in configured_allowed if str(n or "").strip()}
            allowed = [n for n in all_tool_names if n in allowed_set or f"mcp__{srv.server_key}__{n}" in allowed_set]
        else:
            allowed = all_tool_names
        if not allowed:
            continue
        for service_key in service_keys_by_agent.get(b.agent_id, set()):
            service_actions = out.setdefault(service_key, {}).setdefault(srv.server_key, [])
            service_specs = spec_out.setdefault(service_key, {}).setdefault(srv.server_key, {})
            for n in allowed:
                if n not in service_actions:
                    service_actions.append(n)
                service_specs.setdefault(n, tool_specs.get(n, {}))
    from packages.core.services.agent_permission_service import resolve_agent_direct_mcp_actions

    direct_by_provider: dict[str, set[str]] = {}
    for agent_id in service_keys_by_agent:
        direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
        for provider, actions in direct_actions.items():
            direct_by_provider.setdefault(provider, set()).update(actions)
    if direct_by_provider:
        direct_servers = {
            s.server_key: s for s in (await db.execute(
                select(MCPServer).where(
                    MCPServer.server_key.in_(list(direct_by_provider)),
                    MCPServer.status == "active",
                )
            )).scalars().all()
        }
        missing_discovery_providers = set(direct_servers) - set(discovered_tool_caches)
        if missing_discovery_providers:
            missing_endpoints = {
                provider: direct_servers[provider].endpoint
                for provider in missing_discovery_providers
                if (
                    provider in official_providers
                    and direct_servers[provider].endpoint
                )
            }
            try:
                discovered_tool_caches.update(
                    await actor_provider_mcp_tool_caches(
                        db,
                        provider_endpoints=missing_endpoints,
                        user_id=str(actor_user_id or ""),
                        entity_id=entity_id,
                    )
                )
            except Exception:
                logger.warning(
                    "Agent capability projection could not load direct actor MCP catalogs; using server fallback",
                    exc_info=True,
                )
        for agent_id, service_keys in service_keys_by_agent.items():
            direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
            for provider, actions in direct_actions.items():
                srv = direct_servers.get(provider)
                if not srv:
                    continue
                tool_specs = runtime_planner_action_specs_from_tools_cached(
                    discovered_tool_caches.get(srv.server_key) or srv.tools_cached or []
                )
                allowed = [action for action in sorted(actions) if action in tool_specs]
                if not allowed:
                    continue
                for service_key in service_keys:
                    service_actions = out.setdefault(service_key, {}).setdefault(provider, [])
                    service_specs = spec_out.setdefault(service_key, {}).setdefault(provider, {})
                    for action in allowed:
                        if action not in service_actions:
                            service_actions.append(action)
                        service_specs.setdefault(action, tool_specs.get(action, {}))
    return out, spec_out
