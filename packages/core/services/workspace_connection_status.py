"""Read-only connection guidance shared by created and installed Workspaces.

Requirements come from current bindings and durable declarations, never from a
previous successful check. This projection does not change execution admission
or create channel/account bindings.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.setup_preflight import BlueprintSetupPreflightFactory
from packages.core.constants.blueprints import BlueprintInstallRequirementKind, BlueprintInstallTodoKind
from packages.core.models.document import Integration
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.user import OAuthAccount
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition, Workspace
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.integration_account_service import (
    IntegrationAccountKind,
    load_runtime_integration_registry,
    select_runtime_integration_account,
)
from packages.core.services.workspace_readiness import (
    evaluate_workspace_blocking_setup,
    iter_workspace_channel_blocks,
    list_configured_workspace_channels,
)


async def _failed_account_health(db: AsyncSession, *, workspace: Workspace, user_id: str, providers: list[str]) -> set[str]:
    """Project existing health evidence for the exact runtime-selected accounts.

    No provider requests, secret reads, or new execution gate are introduced.
    A saved failed probe remains guidance until the normal health checker
    records a successful check or the user reconnects/selects another account.
    """
    if not providers:
        return set()
    registry = await load_runtime_integration_registry(
        db, entity_id=workspace.entity_id, user_id=user_id, provider_keys=providers,
    )
    selected = []
    for provider in providers:
        account, _ = select_runtime_integration_account(list(registry.accounts_for(provider)))
        if account:
            selected.append(account)
    failed: set[str] = set()
    for kind, model, metadata in (
        (IntegrationAccountKind.OAUTH_ACCOUNT, OAuthAccount, OAuthAccount.profile),
        (IntegrationAccountKind.INTEGRATION, Integration, Integration.config),
    ):
        account_ids = {account.id: account.provider for account in selected if account.kind is kind}
        if not account_ids:
            continue
        rows = (await db.execute(select(model.id, metadata["last_health_check"]).where(model.id.in_(account_ids)))).all()
        for account_id, health in rows:
            if isinstance(health, dict) and health.get("ok") is False:
                failed.add(account_ids[account_id])
    return failed


class WorkspaceConnectionStatusFactory:
    @classmethod
    async def create(cls, db: AsyncSession, *, workspace: Workspace, user_id: str) -> dict:
        settings = workspace.settings or {}
        blueprint = settings.get("_blueprint") or {}
        live_requirements = blueprint.get("live_setup_requirements") or []
        providers: dict[str, dict] = {}
        channels: list[dict] = []
        sessions: list[dict] = []
        service_keys: dict[str, set[str]] = {}

        def declare(provider: object, *, required: bool = True) -> None:
            key = canonical_provider_key(provider)
            if not key:
                return
            if key in providers:
                providers[key]["required"] |= required
            else:
                providers[key] = {"slug": str(provider).strip(), "required": required}

        # Explicit optional requirements take precedence over an inferred
        # dependency. An independently declared required use still wins.
        for todo in live_requirements:
            payload = todo.get("payload") or {}
            kind = todo.get("kind")
            required = bool(todo.get("blocking", True))
            if kind == BlueprintInstallTodoKind.MISSING_INTEGRATION.value:
                declare(payload.get("provider"), required=required)
            elif kind in {BlueprintInstallTodoKind.MCP_SERVER.value, BlueprintInstallTodoKind.MCP_CONFIGURATION.value}:
                declare(payload.get("server_slug"), required=required)
            elif kind == BlueprintInstallTodoKind.CHANNEL.value:
                channels.append({**payload, "required": required})
            elif kind == BlueprintInstallTodoKind.BROWSER_SESSION.value:
                sessions.append({**payload, "required": required})

        for check in (settings.get("blocking_setup") or {}).get("checks") or []:
            if check.get("kind") == "integration_provider":
                declare(check.get("provider"), required=bool(check.get("blocking", True)))

        for flag in settings.get("flagged_integrations") or []:
            if not isinstance(flag, dict):
                continue
            provider = flag.get("provider")
            key = canonical_provider_key(provider)
            if key not in providers and flag.get("source") not in {"channel_setup", "blueprint_channel", "blueprint_session"}:
                declare(provider, required=bool(flag.get("required", True)))
            if key:
                service_keys.setdefault(key, set()).update(flag.get("linked_service_keys") or [])

        # Blueprint channel declarations retain their required/optional policy.
        declared_channels = {
            (channel.get("channel_type"), channel.get("role") or "channel")
            for channel in channels
        }
        for role, channel in iter_workspace_channel_blocks(workspace.operating_model):
            if (channel.get("channel_type"), role) not in declared_channels:
                channels.append({**channel, "role": role})
                declared_channels.add((channel.get("channel_type"), role))

        subs = list((await db.execute(
            select(AgentSubscription).join(Agent, Agent.id == AgentSubscription.agent_id).where(
                AgentSubscription.workspace_id == workspace.id,
                AgentSubscription.entity_id == workspace.entity_id,
                AgentSubscription.status == "active",
                Agent.deleted_at.is_(None),
                Agent.status == "active",
                or_(Agent.entity_id == workspace.entity_id, Agent.entity_id.is_(None)),
            )
        )).scalars().all())
        services_by_agent: dict[str, set[str]] = {}
        for sub in subs:
            services_by_agent.setdefault(sub.agent_id, set()).add(sub.service_key)

        def infer(provider: str, agent_id: str) -> None:
            key = canonical_provider_key(provider)
            if not key:
                return
            if key not in providers:
                declare(provider)
            service_keys.setdefault(key, set()).update(services_by_agent[agent_id])

        def infer_tools(names: list[str], agent_id: str) -> None:
            for name in names:
                parts = str(name).split("__", 2)
                if len(parts) == 3 and parts[0] == "mcp":
                    infer(parts[1], agent_id)

        if services_by_agent:
            for agent_id, server_key in (await db.execute(
                select(AgentMCPBinding.agent_id, MCPServer.server_key)
                .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
                .where(AgentMCPBinding.agent_id.in_(services_by_agent), AgentMCPBinding.status == "active")
            )).all():
                infer(server_key, agent_id)
            for agent_id, tool_name in (await db.execute(
                select(AgentToolBinding.agent_id, ToolDefinition.name)
                .join(ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id)
                .where(AgentToolBinding.agent_id.in_(services_by_agent), ToolDefinition.status == "active")
            )).all():
                infer_tools([tool_name], agent_id)
            for agent_id, tools in (await db.execute(
                select(AgentSkillBinding.agent_id, Skill.tools)
                .join(Skill, Skill.id == AgentSkillBinding.skill_id)
                .where(
                    AgentSkillBinding.agent_id.in_(services_by_agent),
                    AgentSkillBinding.status == "active", Skill.status == "active",
                    or_(Skill.entity_id == workspace.entity_id, Skill.entity_id.is_(None)),
                )
            )).all():
                infer_tools(tools or [], agent_id)

        # Retain exact registry spellings (canonical keys are only identities).
        # Also detect a disabled/removed MCP instead of treating a still-saved
        # OAuth account as proof that the capability itself remains available.
        catalog = list((await db.execute(select(MCPServer.server_key, MCPServer.status))).all()) if providers else []
        active_keys = {canonical_provider_key(key) for key, status in catalog if status == "active"}
        spellings: dict[str, list[str]] = {}
        for raw, _ in catalog:
            spellings.setdefault(canonical_provider_key(raw), []).append(raw)
        provider_specs = [
            {**spec, "slug": registered_key}
            for key, spec in providers.items()
            for registered_key in spellings.get(key, [spec["slug"]])
        ]
        preflight = await BlueprintSetupPreflightFactory.from_contract(
            db, entity_id=workspace.entity_id, user_id=user_id,
            workspace_id=workspace.id,
            contract={
                "requires": {"mcp_servers": provider_specs},
                "channels": channels, "sessions": sessions,
            },
        )
        attached_channel_ids = {
            channel.get("channel_config_id")
            for channel in (await list_configured_workspace_channels(db, workspace) if channels else [])
        }
        failed_health = await _failed_account_health(
            db, workspace=workspace, user_id=user_id,
            providers=[state.provider for state in preflight.requirements if state.kind is BlueprintInstallRequirementKind.INTEGRATION and state.ready],
        )
        requirements = []
        for index, state in enumerate(preflight.requirements):
            item = {
                "kind": state.kind.value, "provider": state.provider,
                "label": state.label, "required": state.required,
                "ready": state.ready, "reason": state.reason,
                "setup_kind": state.setup_kind,
            }
            if state.kind is BlueprintInstallRequirementKind.INTEGRATION and state.provider in failed_health:
                item["ready"] = False
                item["reason"] = "The latest connection health check failed. Open Integrations and run Test now."
            if state.kind is BlueprintInstallRequirementKind.INTEGRATION and canonical_provider_key(state.provider) not in active_keys:
                item["ready"] = False
                item["reason"] = "This integration is disabled or missing from the current MCP catalog."
            if state.kind is BlueprintInstallRequirementKind.CHANNEL and state.resource_id and state.resource_id not in attached_channel_ids:
                item["ready"] = False
                item["reason"] = "Attach the connected channel account to this Workspace."
            item["key"] = state.requirement_key or f"{state.kind.value}:{state.provider}:{index}"
            item["service_keys"] = sorted(filter(None, service_keys.get(canonical_provider_key(state.provider), set())))
            requirements.append(item)

        # Surface exact MCP binding/configuration regressions from the existing
        # runtime gate as well, without inventing a second admission policy.
        gate = await evaluate_workspace_blocking_setup(db, workspace)
        blocking = [todo for todo in live_requirements if todo.get("blocking", True)]
        for check in (gate.details.get("incomplete_checks", []) if gate else []):
            if check.get("todo_kind") not in {
                BlueprintInstallTodoKind.MCP_SERVER.value,
                BlueprintInstallTodoKind.MCP_CONFIGURATION.value,
            }:
                continue
            index = int(check["key"].rsplit(":", 1)[-1])
            # The gate indexes the blocking subset of the durable contract.
            provider = canonical_provider_key((blocking[index].get("payload") or {}).get("server_slug"))
            requirements.append({
                "key": check["key"], "kind": BlueprintInstallRequirementKind.INTEGRATION.value, "provider": provider,
                "label": provider, "required": True, "ready": False,
                "reason": check["reason"], "service_keys": sorted(filter(None, service_keys.get(provider, set()))),
                "setup_kind": "mcp_binding",
            })

        return {
            "workspace_id": workspace.id,
            "requirements": requirements,
            "required_issue_count": sum(item["required"] and not item["ready"] for item in requirements),
        }
