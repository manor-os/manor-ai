"""Agent service — CRUD, subscriptions, tool bindings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote

from sqlalchemy import select, and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import cache
from packages.core.models.base import generate_ulid
from packages.core.models.permission import Visibility
from packages.core.models.workspace import Agent, AgentSubscription, ToolDefinition, AgentToolBinding
from packages.core.services.agent_runtime_config import normalize_agent_runtime_config
from packages.core.services.reusable_resource_locks import (
    RESOURCE_AGENT,
    lock_reusable_resource_reference,
)


class AgentHasActiveWorkspaceMappingsError(ValueError):
    """Raised when deleting an Agent would strand active Workspace mappings."""


@dataclass(frozen=True)
class AgentMCPActionToolRef:
    server_key: str
    action: str


class AgentMCPActionToolIdFactory:
    """Encode actor-scoped MCP actions without creating global ToolDefinitions."""

    PREFIX = "mcp-action:"

    @classmethod
    def create(cls, server_key: object, action: object) -> str:
        server = str(server_key or "").strip()
        action_name = str(action or "").strip()
        if not server or not action_name:
            raise ValueError("MCP server key and action are required")
        return f"{cls.PREFIX}{quote(server, safe='')}/{quote(action_name, safe='')}"

    @classmethod
    def parse(cls, value: object) -> AgentMCPActionToolRef | None:
        text = str(value or "").strip()
        if not text.startswith(cls.PREFIX):
            return None
        encoded = text[len(cls.PREFIX) :]
        server, separator, action = encoded.partition("/")
        if not separator:
            return None
        server_key = unquote(server).strip()
        action_name = unquote(action).strip()
        if not server_key or not action_name:
            return None
        return AgentMCPActionToolRef(server_key=server_key, action=action_name)


# ── Agents ──

async def list_agents(
    db: AsyncSession, entity_id: str, *, include_templates: bool = False,
) -> list[Agent]:
    """List agents for an entity. Optionally include platform templates."""
    conditions = [Agent.entity_id == entity_id, Agent.deleted_at.is_(None)]
    if include_templates:
        conditions = [
            or_(
                Agent.entity_id == entity_id,
                and_(Agent.is_template.is_(True), Agent.is_public.is_(True)),
            ),
            Agent.deleted_at.is_(None),
        ]
    result = await db.execute(
        select(Agent).where(*conditions).order_by(Agent.created_at.desc())
    )
    return list(result.scalars().all())


async def get_agent(db: AsyncSession, agent_id: str) -> Optional[Agent]:
    # Check cache first
    cached = await cache.get(f"agent:{agent_id}")
    if cached is not None and not isinstance(cached, dict):
        return cached

    result = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None)))
    agent = result.scalar_one_or_none()
    if agent:
        await cache.set(f"agent:{agent_id}", {
            "id": agent.id,
            "entity_id": agent.entity_id,
            "name": agent.name,
            "description": agent.description,
            "system_prompt": agent.system_prompt,
            "avatar_url": agent.avatar_url,
            "category": agent.category,
            "tags": agent.tags,
            "is_template": agent.is_template,
            "is_public": agent.is_public,
            "config": agent.config,
        }, ttl=120)
    return agent


def generate_agent_avatar_url(name: str) -> str:
    """Generate a deterministic avatar URL for an agent using DiceBear initials."""
    from urllib.parse import quote
    seed = quote(name, safe="")
    return (
        f"https://api.dicebear.com/9.x/initials/svg"
        f"?seed={seed}"
        f"&backgroundColor=6366f1,8b5cf6,0ea5e9,ec4899,f59e0b,10b981"
        f"&backgroundType=gradientLinear&fontSize=40"
    )


async def create_agent(
    db: AsyncSession, entity_id: str, *,
    name: str, description: str = "", system_prompt: str = "",
    avatar_url: str = "", category: str = "", tags: list[str] | None = None,
    is_template: bool = False, is_public: bool = False,
    config: dict | None = None, source: str = "custom",
    owner_user_id: str | None = None,
    workspace_id: str | None = None,
    visibility: str | None = None,
) -> Agent:
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        visibility=visibility or Visibility.ENTITY,
        name=name,
        description=description or None,
        system_prompt=system_prompt or None,
        avatar_url=avatar_url or None,
        category=category or None,
        tags=tags or [],
        is_template=is_template,
        is_public=is_public,
        config=normalize_agent_runtime_config(config),
        source=source,
    )
    db.add(agent)
    await db.flush()
    return agent


async def update_agent(db: AsyncSession, agent_id: str, entity_id: str, **fields) -> Optional[Agent]:
    agent = await get_agent(db, agent_id)
    if not agent or (agent.entity_id and agent.entity_id != entity_id):
        return None
    # M11: diff BEFORE applying — only real changes to behavior-affecting
    # fields bump the config revision; cosmetic edits (name/description/
    # tags/avatar) and same-value writes leave it alone.
    from packages.core.revisions import (
        AGENT_CONTENT_REVISION_FIELDS,
        bump_revision,
        content_patch_for,
    )
    content_patch = content_patch_for(agent, fields, AGENT_CONTENT_REVISION_FIELDS)
    for k, v in fields.items():
        if hasattr(agent, k) and v is not None:
            if k == "config":
                v = normalize_agent_runtime_config(v)
            setattr(agent, k, v)
    if content_patch:
        await bump_revision(db, agent, patch=content_patch)
    await db.flush()
    # Invalidate cache
    await cache.delete(f"agent:{agent_id}")
    return agent


async def delete_agent(db: AsyncSession, agent_id: str, entity_id: str) -> bool:
    from datetime import datetime, timezone
    agent = await get_agent(db, agent_id)
    if not agent or agent.entity_id != entity_id:
        return False
    active_workspace_mapping = await db.scalar(
        select(AgentSubscription.id).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.agent_id == agent_id,
            AgentSubscription.workspace_id.is_not(None),
            AgentSubscription.status == "active",
        ).limit(1)
    )
    if active_workspace_mapping:
        raise AgentHasActiveWorkspaceMappingsError(
            "This agent is used by an active workspace. Remove its workspace "
            "mappings before deleting or unsubscribing."
        )
    agent.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    # Invalidate cache
    await cache.delete(f"agent:{agent_id}")
    return True


# ── Subscriptions (hire agent) ──

async def subscribe_agent(
    db: AsyncSession, entity_id: str, agent_id: str, *,
    workspace_id: str | None = None, custom_prompt: str = "",
    owner_user_id: str | None = None,
) -> AgentSubscription:
    from packages.core.services.marketplace_agent_service import (
        ensure_marketplace_agent_installed,
    )

    agent = await ensure_marketplace_agent_installed(
        db,
        entity_id=entity_id,
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        allow_local=True,
    )
    await lock_reusable_resource_reference(
        db,
        entity_id=entity_id,
        resource_type=RESOURCE_AGENT,
        resource_id=agent.id,
    )
    sub = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent.id,
        workspace_id=workspace_id,
        custom_prompt=custom_prompt or None,
    )
    db.add(sub)
    await db.flush()
    return sub


async def list_subscriptions(db: AsyncSession, entity_id: str) -> list[AgentSubscription]:
    result = await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.status == "active",
        )
    )
    return list(result.scalars().all())


async def unsubscribe_agent(db: AsyncSession, subscription_id: str, entity_id: str) -> bool:
    result = await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.id == subscription_id,
            AgentSubscription.entity_id == entity_id,
        )
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return False
    sub.status = "cancelled"
    await db.flush()
    return True


async def _sync_agent_mcp_bindings_for_tool_names(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_names: list[str],
) -> None:
    """Mirror Agent settings MCP tool checkboxes into AgentMCPBinding rows.

    The Agent editor stores checkbox selections as ToolDefinition bindings.
    Runtime MCP authorization, however, reads AgentMCPBinding. Keep the two in
    sync for any mcp__provider__tool names touched by the editor.
    """

    from packages.core.services.agent_permission_service import parse_mcp_tool_name

    touched_providers = {
        parsed[0]
        for name in tool_names
        if (parsed := parse_mcp_tool_name(name)) is not None
    }
    if not touched_providers:
        return

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    rows = (
        await db.execute(
            select(ToolDefinition.name)
            .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
            .where(AgentToolBinding.agent_id == agent_id)
        )
    ).scalars().all()
    selected_by_provider: dict[str, set[str]] = {provider: set() for provider in touched_providers}
    for name in rows:
        parsed = parse_mcp_tool_name(name)
        if not parsed:
            continue
        provider, action = parsed
        if provider in selected_by_provider:
            selected_by_provider[provider].add(action)

    servers = (
        await db.execute(
            select(MCPServer).where(
                MCPServer.server_key.in_(list(touched_providers)),
                MCPServer.status == "active",
            )
        )
    ).scalars().all()
    servers_by_key = {server.server_key: server for server in servers}

    for provider, actions in selected_by_provider.items():
        server = servers_by_key.get(provider)
        if not server:
            continue
        binding = (
            await db.execute(
                select(AgentMCPBinding).where(
                    AgentMCPBinding.agent_id == agent_id,
                    AgentMCPBinding.mcp_server_id == server.id,
                )
            )
        ).scalar_one_or_none()
        if actions:
            if binding is None:
                db.add(AgentMCPBinding(
                    id=generate_ulid(),
                    agent_id=agent_id,
                    mcp_server_id=server.id,
                    allowed_tools=sorted(actions),
                    status="active",
                ))
            else:
                binding.allowed_tools = sorted(actions)
                binding.status = "active"
        elif binding is not None:
            binding.allowed_tools = []
            binding.status = "inactive"


async def bind_tools(db: AsyncSession, agent_id: str, tool_ids: list[str]) -> int:
    """Bind tools to an agent. Returns count of new bindings."""
    count = 0
    touched_tool_names: list[str] = []
    for tid in tool_ids:
        tool = (
            await db.execute(
                select(ToolDefinition).where(ToolDefinition.id == tid)
            )
        ).scalar_one_or_none()
        if tool is not None:
            touched_tool_names.append(tool.name)
        existing = await db.execute(
            select(AgentToolBinding).where(
                AgentToolBinding.agent_id == agent_id,
                AgentToolBinding.tool_id == tid,
            )
        )
        if not existing.scalar_one_or_none():
            db.add(AgentToolBinding(agent_id=agent_id, tool_id=tid))
            count += 1
    await db.flush()
    await _sync_agent_mcp_bindings_for_tool_names(
        db,
        agent_id=agent_id,
        tool_names=touched_tool_names,
    )
    await db.flush()
    return count


async def unbind_tools(db: AsyncSession, agent_id: str, tool_ids: list[str]) -> int:
    """Unbind tools from an agent."""
    count = 0
    touched_tool_names: list[str] = []
    for tid in tool_ids:
        tool = (
            await db.execute(
                select(ToolDefinition).where(ToolDefinition.id == tid)
            )
        ).scalar_one_or_none()
        if tool is not None:
            touched_tool_names.append(tool.name)
        result = await db.execute(
            select(AgentToolBinding).where(
                AgentToolBinding.agent_id == agent_id,
                AgentToolBinding.tool_id == tid,
            )
        )
        binding = result.scalar_one_or_none()
        if binding:
            await db.delete(binding)
            count += 1
    await db.flush()
    await _sync_agent_mcp_bindings_for_tool_names(
        db,
        agent_id=agent_id,
        tool_names=touched_tool_names,
    )
    await db.flush()
    return count


async def get_agent_tools(db: AsyncSession, agent_id: str) -> list[ToolDefinition]:
    """Get all tools bound to an agent."""
    result = await db.execute(
        select(ToolDefinition)
        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
        .where(AgentToolBinding.agent_id == agent_id)
    )
    return list(result.scalars().all())


async def get_agent_mcp_action_refs(
    db: AsyncSession,
    *,
    agent_id: str,
    available_actions_by_server: dict[str, set[str]],
) -> list[AgentMCPActionToolRef]:
    """Project active runtime MCP bindings into editor-stable action refs."""

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    rows = list((await db.execute(
        select(AgentMCPBinding, MCPServer)
        .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
        .where(
            AgentMCPBinding.agent_id == agent_id,
            AgentMCPBinding.status == "active",
            MCPServer.status == "active",
        )
    )).all())
    refs: list[AgentMCPActionToolRef] = []
    for binding, server in rows:
        actions = (
            sorted(available_actions_by_server.get(server.server_key, set()))
            if binding.allowed_tools is None
            else sorted({
                str(action).strip()
                for action in binding.allowed_tools
                if str(action).strip()
            })
        )
        refs.extend(
            AgentMCPActionToolRef(server_key=server.server_key, action=action)
            for action in actions
        )
    return refs


async def _sync_agent_tool_bindings_for_mcp_server(
    db: AsyncSession,
    *,
    agent_id: str,
    server_key: str,
    allowed_tools: list[str] | None,
) -> None:
    """Mirror only globally defined MCP actions; actor-private actions stay scoped."""

    from packages.core.services.agent_permission_service import parse_mcp_tool_name
    from packages.core.services.provider_keys import canonical_provider_key

    definitions = list((await db.execute(
        select(ToolDefinition).where(
            ToolDefinition.status == "active",
            ToolDefinition.name.like("mcp__%"),
        )
    )).scalars().all())
    provider = canonical_provider_key(server_key)
    provider_definitions = [
        (definition, parsed[1])
        for definition in definitions
        if (parsed := parse_mcp_tool_name(definition.name)) is not None
        and canonical_provider_key(parsed[0]) == provider
    ]
    provider_tool_ids = {definition.id for definition, _action in provider_definitions}
    if not provider_tool_ids:
        return
    selected_actions = None if allowed_tools is None else set(allowed_tools)
    selected_tool_ids = {
        definition.id
        for definition, action in provider_definitions
        if selected_actions is None or action in selected_actions
    }
    bindings = list((await db.execute(
        select(AgentToolBinding).where(
            AgentToolBinding.agent_id == agent_id,
            AgentToolBinding.tool_id.in_(provider_tool_ids),
        )
    )).scalars().all())
    by_tool_id = {binding.tool_id: binding for binding in bindings}
    for tool_id in provider_tool_ids - selected_tool_ids:
        binding = by_tool_id.get(tool_id)
        if binding is not None:
            await db.delete(binding)
    for tool_id in selected_tool_ids - set(by_tool_id):
        db.add(AgentToolBinding(agent_id=agent_id, tool_id=tool_id))


async def update_agent_mcp_action_refs(
    db: AsyncSession,
    *,
    agent_id: str,
    refs: list[AgentMCPActionToolRef],
    bind: bool,
    available_actions_by_server: dict[str, set[str]],
) -> int:
    """Bind or revoke exact actor-scoped MCP actions and their public mirrors."""

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    requested_by_server: dict[str, set[str]] = {}
    for ref in refs:
        requested_by_server.setdefault(ref.server_key, set()).add(ref.action)
    if not requested_by_server:
        return 0

    servers = list((await db.execute(
        select(MCPServer).where(
            MCPServer.server_key.in_(requested_by_server),
            MCPServer.status == "active",
        )
    )).scalars().all())
    servers_by_key = {server.server_key: server for server in servers}
    missing = sorted(set(requested_by_server) - set(servers_by_key))
    if missing:
        raise ValueError("unknown or inactive MCP servers: " + ", ".join(missing))

    bindings = list((await db.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id == agent_id,
            AgentMCPBinding.mcp_server_id.in_({server.id for server in servers}),
        )
    )).scalars().all())
    binding_by_server_id = {binding.mcp_server_id: binding for binding in bindings}
    changed = 0
    for server_key, requested_actions in requested_by_server.items():
        server = servers_by_key[server_key]
        binding = binding_by_server_id.get(server.id)
        if bind:
            if binding is None:
                binding = AgentMCPBinding(
                    id=generate_ulid(),
                    agent_id=agent_id,
                    mcp_server_id=server.id,
                    allowed_tools=sorted(requested_actions),
                    status="active",
                )
                db.add(binding)
                binding_by_server_id[server.id] = binding
                changed += len(requested_actions)
            elif binding.allowed_tools is None and binding.status == "active":
                pass
            else:
                current = {
                    str(action).strip()
                    for action in (binding.allowed_tools or [])
                    if str(action).strip()
                }
                changed += len(requested_actions - current)
                binding.allowed_tools = sorted(current | requested_actions)
                binding.status = "active"
        elif binding is not None and binding.status == "active":
            current = (
                set(available_actions_by_server.get(server_key, set()))
                if binding.allowed_tools is None
                else {
                    str(action).strip()
                    for action in binding.allowed_tools
                    if str(action).strip()
                }
            )
            changed += len(current & requested_actions)
            remaining = current - requested_actions
            binding.allowed_tools = sorted(remaining)
            binding.status = "active" if remaining else "inactive"

        if binding is not None:
            await _sync_agent_tool_bindings_for_mcp_server(
                db,
                agent_id=agent_id,
                server_key=server_key,
                allowed_tools=(
                    binding.allowed_tools
                    if binding.status == "active"
                    else []
                ),
            )
    await db.flush()
    return changed


# ── Tool definitions ──

def _tool_catalog_display_name(name: str, schema: dict) -> str:
    fn = schema.get("function") if isinstance(schema, dict) else None
    title = fn.get("title") if isinstance(fn, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return name.replace("mcp__", "").replace("_", " ").replace(".", " ").title()


def _tool_catalog_description(schema: dict) -> str:
    fn = schema.get("function") if isinstance(schema, dict) else None
    description = fn.get("description") if isinstance(fn, dict) else None
    return description.strip() if isinstance(description, str) else ""


def _tool_catalog_category(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    if name.startswith("workspace_"):
        return "workspace"
    if name in {"read_file", "patch_file", "inspect_file_engine", "list_files", "glob_files", "grep_files", "bash"}:
        return "files"
    if name.startswith("generate_"):
        return "generation"
    if any(token in name for token in ("email", "gmail", "outlook", "telegram", "slack", "message", "social")):
        return "communication"
    if any(token in name for token in ("task", "schedule", "calendar", "goal")):
        return "operations"
    return "runtime"


async def ensure_runtime_tool_definitions(db: AsyncSession) -> int:
    """Backfill the bindable Agent tool catalog from the live runtime registry.

    Migrations seed a small historical catalog, but OSS/dev databases can be
    created without those rows. The runtime already owns the authoritative tool
    pool, so the Agent editor should not show an empty tool picker just because
    `tool_definitions` has not been seeded yet.
    """
    from packages.core.ai.runtime.tool_registry import (
        runtime_ensure_tool_registry_initialized,
        runtime_registered_tool_schemas,
    )

    runtime_ensure_tool_registry_initialized()

    # Keep saved bindings/audit rows readable, but stop offering retired
    # duplicate file entrypoints in the Agent tool picker.
    await db.execute(
        update(ToolDefinition).where(
            ToolDefinition.name.in_({
                "write_file", "edit_file", "generate_document_file",
                "mcp__manor_mcp_file_engine__inspect",
                "mcp__manor_mcp_file_engine__generate",
                "mcp__manor_mcp_file_engine__patch",
            }),
            ToolDefinition.status != "inactive",
        ).values(status="inactive")
    )

    existing_result = await db.execute(select(ToolDefinition.name))
    existing = {str(row[0]) for row in existing_result.all()}
    created = 0

    for name, schema in sorted(runtime_registered_tool_schemas()):
        if name in existing:
            continue
        db.add(ToolDefinition(
            id=generate_ulid(),
            name=name,
            display_name=_tool_catalog_display_name(name, schema),
            description=_tool_catalog_description(schema),
            category=_tool_catalog_category(name),
            schema=schema,
            status="active",
        ))
        existing.add(name)
        created += 1

    if created:
        await db.flush()
    return created


async def list_tool_definitions(db: AsyncSession, *, include_inactive: bool = False) -> list[ToolDefinition]:
    await ensure_runtime_tool_definitions(db)
    query = select(ToolDefinition)
    if not include_inactive:
        query = query.where(ToolDefinition.status == "active")
    result = await db.execute(
        query.order_by(ToolDefinition.category, ToolDefinition.name)
    )
    return list(result.scalars().all())


async def create_tool_definition(db: AsyncSession, *, name: str, display_name: str = "", description: str = "", category: str = "") -> ToolDefinition:
    tool = ToolDefinition(
        id=generate_ulid(), name=name,
        display_name=display_name or None,
        description=description or None,
        category=category or None,
    )
    db.add(tool)
    await db.flush()
    return tool
