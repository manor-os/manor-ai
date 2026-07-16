"""Runtime-owned facade for agent provisioning actions."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def runtime_query_entity_agents_action(
    *,
    entity_id: str,
    query: str = "",
    statuses: list[str] | None = None,
    include_templates: bool = False,
    limit: int = 50,
) -> str:
    """Return non-sensitive Agent metadata through the Runtime boundary."""

    from sqlalchemy import and_, func, or_, select

    from packages.core.database import async_session
    from packages.core.models.skill import AgentSkillBinding
    from packages.core.models.workspace import (
        Agent,
        AgentSubscription,
        AgentToolBinding,
    )

    resolved_limit = max(1, min(int(limit or 50), 100))
    normalized_query = str(query or "").strip().casefold()
    normalized_statuses = {
        str(status).strip().casefold()
        for status in (statuses or [])[:10]
        if str(status).strip()
    }
    ownership = Agent.entity_id == entity_id
    if include_templates:
        ownership = or_(
            ownership,
            and_(Agent.is_template.is_(True), Agent.is_public.is_(True)),
        )

    async with async_session() as db:
        rows = (
            await db.execute(
                select(Agent)
                .where(Agent.deleted_at.is_(None), ownership)
                .order_by(Agent.created_at.desc())
                .limit(200)
            )
        ).scalars().all()
        agents = [
            agent
            for agent in rows
            if (
                not normalized_statuses
                or str(agent.status or "").casefold() in normalized_statuses
            )
            and (
                not normalized_query
                or normalized_query
                in (
                    f"{agent.name or ''} {agent.description or ''} "
                    f"{agent.category or ''}"
                ).casefold()
            )
        ][:resolved_limit]
        agent_ids = [agent.id for agent in agents]

        async def grouped_counts(model, *conditions):
            if not agent_ids:
                return {}
            result = await db.execute(
                select(model.agent_id, func.count().label("count"))
                .where(model.agent_id.in_(agent_ids), *conditions)
                .group_by(model.agent_id)
            )
            return {str(row.agent_id): int(row.count) for row in result}

        tool_counts = await grouped_counts(AgentToolBinding)
        skill_counts = await grouped_counts(
            AgentSkillBinding,
            AgentSkillBinding.status == "active",
        )
        deployment_counts = await grouped_counts(
            AgentSubscription,
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.status == "active",
        )

    items = [
        {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "category": agent.category,
            "status": agent.status,
            "source": "template" if agent.is_template else agent.source,
            "tool_count": tool_counts.get(agent.id, 0),
            "skill_count": skill_counts.get(agent.id, 0),
            "deployment_count": deployment_counts.get(agent.id, 0),
        }
        for agent in agents
    ]
    return json.dumps({"agents": items, "total": len(items)})


async def runtime_provision_agent_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Create a custom agent through the Runtime action boundary."""

    if not entity_id:
        return json.dumps({"ok": False, "error": "entity_id missing from tool context"})

    raw_params = dict(params or {})
    agent_name = (raw_params.get("agent_name") or "").strip()
    system_prompt = (raw_params.get("system_prompt") or "").strip()
    if not agent_name or not system_prompt:
        return json.dumps({"ok": False, "error": "agent_name and system_prompt are required"})

    try:
        from packages.core.database import async_session
        from packages.core.services.agent_provisioning_service import (
            CustomAgentSpec,
            provision_custom_agent,
        )

        spec = CustomAgentSpec(
            agent_name=agent_name,
            system_prompt=system_prompt,
            description=(raw_params.get("description") or "").strip(),
            category=raw_params.get("category"),
            tags=list(raw_params.get("tags") or []),
            tool_bindings=list(raw_params.get("tool_bindings") or []),
            skill_bindings=list(raw_params.get("skill_bindings") or []),
            mcp_bindings=list(raw_params.get("mcp_bindings") or []),
            missing_skill_specs=list(raw_params.get("missing_skill_specs") or []),
            source="chat_tool",
        )

        async with async_session() as db:
            try:
                result = await provision_custom_agent(db, entity_id=entity_id, spec=spec)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                raise exc

        return json.dumps({
            "ok": True,
            "agent_id": result.agent_id,
            "agent_name": result.agent_name,
            "bound_tools": result.bound_tools,
            "bound_skills": result.bound_skills,
            "created_skills": result.created_skills,
            "bound_mcp_servers": result.bound_mcp_servers,
            "warnings": result.warnings,
        }, ensure_ascii=False)
    except Exception as exc:
        logger.exception("provision_agent failed")
        return json.dumps({"ok": False, "error": f"failed to provision agent: {exc}"})
