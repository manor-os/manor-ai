"""Agent service — CRUD, subscriptions, tool bindings."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import cache
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Agent, AgentSubscription, ToolDefinition, AgentToolBinding


# ── Agents ──

async def list_agents(
    db: AsyncSession, entity_id: str, *, include_templates: bool = False,
) -> list[Agent]:
    """List agents for an entity. Optionally include platform templates."""
    conditions = [Agent.entity_id == entity_id, Agent.deleted_at.is_(None)]
    if include_templates:
        conditions = [
            or_(Agent.entity_id == entity_id, and_(Agent.is_template == True, Agent.is_public == True)),
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
) -> Agent:
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        description=description or None,
        system_prompt=system_prompt or None,
        avatar_url=avatar_url or None,
        category=category or None,
        tags=tags or [],
        is_template=is_template,
        is_public=is_public,
        config=config or {},
        source=source,
    )
    db.add(agent)
    await db.flush()
    return agent


async def update_agent(db: AsyncSession, agent_id: str, entity_id: str, **fields) -> Optional[Agent]:
    agent = await get_agent(db, agent_id)
    if not agent or (agent.entity_id and agent.entity_id != entity_id):
        return None
    for k, v in fields.items():
        if hasattr(agent, k) and v is not None:
            setattr(agent, k, v)
    await db.flush()
    # Invalidate cache
    await cache.delete(f"agent:{agent_id}")
    return agent


async def delete_agent(db: AsyncSession, agent_id: str, entity_id: str) -> bool:
    from datetime import datetime, timezone
    agent = await get_agent(db, agent_id)
    if not agent or agent.entity_id != entity_id:
        return False
    agent.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    # Invalidate cache
    await cache.delete(f"agent:{agent_id}")
    return True


# ── Subscriptions (hire agent) ──

async def subscribe_agent(
    db: AsyncSession, entity_id: str, agent_id: str, *,
    workspace_id: str | None = None, custom_prompt: str = "",
) -> AgentSubscription:
    sub = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent_id,
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
    if name in {"read_file", "write_file", "edit_file", "list_files", "glob_files", "grep_files", "bash"}:
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
