"""Runtime-owned facade for agent provisioning actions."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def runtime_query_agent_capabilities_action(
    *,
    entity_id: str,
    user_id: str,
) -> str:
    """Return the exact actor-scoped ids accepted by ``provision_agent``."""

    if not entity_id:
        return json.dumps({"ok": False, "error": "entity_id missing from tool context"})
    if not user_id:
        return json.dumps({"ok": False, "error": "user_id missing from tool context"})

    from packages.core.database import async_session
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCatalogFactory,
    )

    async with async_session() as db:
        catalog = await AgentCapabilityCatalogFactory.create(
            db,
            entity_id=entity_id,
            user_id=user_id,
        )
    return json.dumps({
        "ok": True,
        "selection_contract": (
            "Choose exact ids from capabilities and pass them unchanged as "
            "capability_ids to provision_agent. Do not invent or rewrite ids."
        ),
        "capabilities": catalog.prompt_payload(),
    }, ensure_ascii=False)


async def runtime_query_entity_agents_action(
    *,
    entity_id: str,
    query: str = "",
    statuses: list[str] | None = None,
    categories: list[str] | None = None,
    sources: list[str] | None = None,
    workspace_ids: list[str] | None = None,
    owner_user_ids: list[str] | None = None,
    include_templates: bool = False,
    created_after: str = "",
    created_before: str = "",
    updated_after: str = "",
    updated_before: str = "",
    deployment_count_min: int | None = None,
    deployment_count_max: int | None = None,
    require_schedulable_scope: bool = False,
    schedulable_workspace_id: str | None = None,
    schedulable_user_id: str | None = None,
    viewer_user_id: str | None = None,
    readable_workspace_ids: set[str] | None = None,
    runtime_workspace_id: str | None = None,
    enforce_viewer_scope: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Any = None,
) -> str:
    """Return one fully filtered page of non-sensitive Agent metadata."""

    from sqlalchemy import and_, false, func, or_, select

    from packages.core.database import async_session
    from packages.core.models.skill import AgentSkillBinding
    from packages.core.models.workspace import (
        Agent,
        AgentSubscription,
        AgentToolBinding,
    )

    try:
        resolved_limit = max(1, min(int(limit or 50), 100))
        resolved_offset = max(0, min(int(offset or 0), 10_000))
        normalized_query = str(query or "").strip()
        normalized_statuses = _agent_filter_values(statuses, lowercase=True)
        normalized_categories = _agent_filter_values(categories, lowercase=True)
        normalized_sources = _agent_filter_values(sources, lowercase=True)
        normalized_workspace_ids = _agent_filter_values(workspace_ids)
        normalized_owner_user_ids = _agent_filter_values(owner_user_ids)
        created_range = _agent_datetime_range(
            created_after,
            created_before,
            "created",
        )
        updated_range = _agent_datetime_range(
            updated_after,
            updated_before,
            "updated",
        )
        deployment_min = (
            int(deployment_count_min)
            if deployment_count_min is not None
            else None
        )
        deployment_max = (
            int(deployment_count_max)
            if deployment_count_max is not None
            else None
        )
        if (deployment_min is not None and deployment_min < 0) or (
            deployment_max is not None and deployment_max < 0
        ):
            raise ValueError("deployment count bounds must be non-negative")
        if (
            deployment_min is not None
            and deployment_max is not None
            and deployment_min > deployment_max
        ):
            raise ValueError(
                "deployment_count_min must be less than or equal to "
                "deployment_count_max"
            )
    except (TypeError, ValueError) as exc:
        return json.dumps({
            "ok": False,
            "error": "invalid_agent_filters",
            "message": str(exc),
        })
    ownership = Agent.entity_id == entity_id
    if include_templates:
        ownership = or_(
            ownership,
            and_(Agent.is_template.is_(True), Agent.is_public.is_(True)),
        )

    if enforce_viewer_scope and not viewer_user_id and not runtime_workspace_id:
        return json.dumps({
            "ok": False,
            "error": "user or Workspace context is required",
        })

    session_context = nullcontext(db) if db is not None else async_session()
    async with session_context as session:
        if enforce_viewer_scope:
            if viewer_user_id:
                from packages.core.services.workspace_access import (
                    readable_workspace_ids_for_user,
                )

                readable_workspace_ids = await readable_workspace_ids_for_user(
                    session,
                    entity_id=entity_id,
                    user_id=viewer_user_id,
                    role=None,
                )
            else:
                readable_workspace_ids = {str(runtime_workspace_id)}
        filters = [Agent.deleted_at.is_(None), ownership]
        if readable_workspace_ids is not None:
            visible_agent_clauses = [
                and_(
                    Agent.workspace_id.is_(None),
                    Agent.owner_user_id.is_(None),
                ),
                Agent.workspace_id.in_(readable_workspace_ids),
            ]
            if viewer_user_id:
                visible_agent_clauses.append(
                    and_(
                        Agent.workspace_id.is_(None),
                        Agent.owner_user_id == viewer_user_id,
                    )
                )
            if include_templates:
                visible_agent_clauses.append(
                    and_(Agent.is_template.is_(True), Agent.is_public.is_(True))
                )
            filters.append(or_(*visible_agent_clauses))
        if normalized_query:
            filters.append(or_(
                Agent.name.icontains(normalized_query, autoescape=True),
                Agent.description.icontains(normalized_query, autoescape=True),
                Agent.category.icontains(normalized_query, autoescape=True),
                Agent.slug.icontains(normalized_query, autoescape=True),
            ))
        if normalized_statuses:
            filters.append(func.lower(Agent.status).in_(normalized_statuses))
        if normalized_categories:
            filters.append(func.lower(Agent.category).in_(normalized_categories))
        if normalized_workspace_ids:
            filters.append(Agent.workspace_id.in_(normalized_workspace_ids))
        if normalized_owner_user_ids:
            filters.append(Agent.owner_user_id.in_(normalized_owner_user_ids))
        if normalized_sources:
            source_clauses = []
            if "template" in normalized_sources:
                source_clauses.append(Agent.is_template.is_(True))
            stored_sources = tuple(
                value for value in normalized_sources if value != "template"
            )
            if stored_sources:
                source_clauses.append(func.lower(Agent.source).in_(stored_sources))
            filters.append(or_(*source_clauses))
        for column, (after_dt, before_dt) in (
            (Agent.created_at, created_range),
            (Agent.updated_at, updated_range),
        ):
            if after_dt is not None:
                filters.append(column >= after_dt)
            if before_dt is not None:
                filters.append(column <= before_dt)

        deployment_filters = [
            AgentSubscription.agent_id == Agent.id,
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.status == "active",
        ]
        deployment_count = (
            select(func.count())
            .select_from(AgentSubscription)
            .where(*deployment_filters)
            .correlate(Agent)
            .scalar_subquery()
        )
        visible_deployment_count = deployment_count
        if readable_workspace_ids is not None and not require_schedulable_scope:
            visible_subscription_clauses = [
                and_(
                    AgentSubscription.workspace_id.is_(None),
                    AgentSubscription.owner_user_id.is_(None),
                ),
                AgentSubscription.workspace_id.in_(readable_workspace_ids),
            ]
            if viewer_user_id:
                visible_subscription_clauses.append(and_(
                    AgentSubscription.workspace_id.is_(None),
                    AgentSubscription.owner_user_id == viewer_user_id,
                ))
            visible_deployment_count = (
                select(func.count())
                .select_from(AgentSubscription)
                .where(*deployment_filters, or_(*visible_subscription_clauses))
                .correlate(Agent)
                .scalar_subquery()
            )
        if require_schedulable_scope:
            scoped_deployment_filters = list(deployment_filters)
            if schedulable_workspace_id:
                scoped_deployment_filters.append(
                    AgentSubscription.workspace_id == schedulable_workspace_id
                )
            elif schedulable_user_id:
                scoped_deployment_filters.extend((
                    AgentSubscription.workspace_id.is_(None),
                    AgentSubscription.owner_user_id == schedulable_user_id,
                ))
            else:
                scoped_deployment_filters.append(false())
            visible_deployment_count = (
                select(func.count())
                .select_from(AgentSubscription)
                .where(*scoped_deployment_filters)
                .correlate(Agent)
                .scalar_subquery()
            )
            filters.extend((
                Agent.status == "active",
                visible_deployment_count == 1,
            ))
        tool_count = (
            select(func.count())
            .select_from(AgentToolBinding)
            .where(AgentToolBinding.agent_id == Agent.id)
            .correlate(Agent)
            .scalar_subquery()
        )
        skill_count = (
            select(func.count())
            .select_from(AgentSkillBinding)
            .where(
                AgentSkillBinding.agent_id == Agent.id,
                AgentSkillBinding.status == "active",
            )
            .correlate(Agent)
            .scalar_subquery()
        )
        if deployment_min is not None:
            filters.append(visible_deployment_count >= deployment_min)
        if deployment_max is not None:
            filters.append(visible_deployment_count <= deployment_max)

        agent_rows = list((await session.execute(
            select(
                Agent,
                tool_count.label("tool_count"),
                skill_count.label("skill_count"),
                visible_deployment_count.label("deployment_count"),
            )
            .where(*filters)
            .order_by(Agent.created_at.desc(), Agent.id.desc())
            .limit(resolved_limit)
            .offset(resolved_offset)
        )).all())
        total = int((await session.execute(
            select(func.count()).select_from(Agent).where(*filters)
        )).scalar_one())

    items = [
        {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "category": agent.category,
            "status": agent.status,
            "source": "template" if agent.is_template else agent.source,
            "tool_count": int(tool_count_value or 0),
            "skill_count": int(skill_count_value or 0),
            "deployment_count": int(deployment_count_value or 0),
            "workspace_id": agent.workspace_id,
            "owner_user_id": agent.owner_user_id,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
        }
        for agent, tool_count_value, skill_count_value, deployment_count_value
        in agent_rows
    ]
    next_offset = resolved_offset + len(items)
    has_more = next_offset < total
    return json.dumps({
        "ok": True,
        "agents": items,
        "total": total,
        "count": len(items),
        "limit": resolved_limit,
        "offset": resolved_offset,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    })


def _agent_filter_values(
    values: Sequence[Any] | None,
    *,
    lowercase: bool = False,
) -> tuple[str, ...]:
    if values is not None and (
        isinstance(values, (str, bytes)) or not isinstance(values, Sequence)
    ):
        raise TypeError("plural agent filters must be arrays")
    normalized: list[str] = []
    for value in (values or [])[:50]:
        item = str(value or "").strip()
        if lowercase:
            item = item.lower()
        if item and item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _agent_datetime_bound(value: str, field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field_name} range endpoints must be ISO-8601 datetimes"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _agent_datetime_range(
    after: str,
    before: str,
    field_name: str,
) -> tuple[datetime | None, datetime | None]:
    after_dt = _agent_datetime_bound(after, field_name)
    before_dt = _agent_datetime_bound(before, field_name)
    if after_dt is not None and before_dt is not None and after_dt > before_dt:
        raise ValueError(
            f"{field_name}_after must be less than or equal to {field_name}_before"
        )
    return after_dt, before_dt


async def runtime_provision_agent_action(
    *,
    entity_id: str,
    user_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Create a custom agent through the Runtime action boundary."""

    if not entity_id:
        return json.dumps({"ok": False, "error": "entity_id missing from tool context"})
    if not user_id:
        return json.dumps({"ok": False, "error": "user_id missing from tool context"})

    raw_params = dict(params or {})
    agent_name = (raw_params.get("agent_name") or "").strip()
    system_prompt = (raw_params.get("system_prompt") or "").strip()
    if not agent_name or not system_prompt:
        return json.dumps({"ok": False, "error": "agent_name and system_prompt are required"})

    try:
        from packages.core.database import async_session
        from packages.core.services.agent_capability_catalog import (
            AgentCapabilityCatalogFactory,
            AgentCapabilitySelectionError,
        )
        from packages.core.services.agent_provisioning_service import (
            CustomAgentSpec,
            provision_custom_agent,
        )

        async with async_session() as db:
            try:
                catalog = await AgentCapabilityCatalogFactory.create(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                )
                capability_ids = list(raw_params.get("capability_ids") or [])
                if capability_ids:
                    plan = catalog.resolve(capability_ids)
                else:
                    plan = catalog.resolve_exact_refs(
                        tool_names=list(raw_params.get("tool_bindings") or []),
                        business_capability_ids=list(
                            raw_params.get("business_capabilities") or []
                        ),
                        skill_ids=list(raw_params.get("skill_bindings") or []),
                        mcp_server_keys=list(raw_params.get("mcp_bindings") or []),
                    )

                spec = CustomAgentSpec(
                    agent_name=agent_name,
                    system_prompt=system_prompt,
                    description=(raw_params.get("description") or "").strip(),
                    category=raw_params.get("category"),
                    tags=list(raw_params.get("tags") or []),
                    tool_bindings=list(plan.tool_names),
                    business_capabilities=list(plan.business_capability_ids),
                    skill_bindings=list(plan.skill_ids),
                    skill_binding_refs=[
                        dict(ref)
                        for ref in raw_params.get("skill_binding_refs") or []
                        if isinstance(ref, dict)
                    ],
                    mcp_bindings=list(plan.mcp_server_keys),
                    mcp_allowed_tools={
                        key: (list(value) if value is not None else None)
                        for key, value in plan.mcp_allowed_tools.items()
                    },
                    missing_skill_specs=list(raw_params.get("missing_skill_specs") or []),
                    source="chat_tool",
                )
                result = await provision_custom_agent(
                    db,
                    entity_id=entity_id,
                    spec=spec,
                    requester_user_id=user_id,
                )
                await db.commit()
            except AgentCapabilitySelectionError:
                await db.rollback()
                raise
            except Exception as exc:
                await db.rollback()
                raise exc

        logger.info(
            "provision_agent capability plan entity=%s user=%s status=%s selected=%s",
            entity_id,
            user_id,
            plan.status.value,
            list(plan.selected_catalog_ids),
        )
        return json.dumps({
            "ok": True,
            "agent_id": result.agent_id,
            "agent_name": result.agent_name,
            "bound_tools": result.bound_tools,
            "bound_skills": result.bound_skills,
            "created_skills": result.created_skills,
            "bound_mcp_servers": result.bound_mcp_servers,
            "warnings": result.warnings,
            "capability_plan_status": plan.status.value,
            "setup_required": list(plan.setup_required),
        }, ensure_ascii=False)
    except Exception as exc:
        logger.exception("provision_agent failed")
        return json.dumps({"ok": False, "error": f"failed to provision agent: {exc}"})
