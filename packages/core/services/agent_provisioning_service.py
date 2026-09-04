"""Agent provisioning — turn a "design spec" into a real Agent row + all
the bindings it needs to actually do work.

The spec mirrors what the workspace architect's
``ws_request_custom_agent`` tool produces, but the function is intentionally
generic: any caller (workspace setup finalize, retroactive auto-map, an
operator tool, a CLI script, a future "spawn agent" wizard) can build a
``CustomAgentSpec`` and call ``provision_custom_agent`` to get a fully
wired agent back.

Every binding step is best-effort and isolated -- a failed skill bind
won't take down the agent itself. We log warnings, return what got
built, and let the caller decide whether to surface the gaps.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.capability_bindings import (
    normalize_workspace_custom_agent_tool_bindings,
)
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import (
    Agent, AgentToolBinding, ToolDefinition,
)
from packages.core.models.skill import Skill, AgentSkillBinding
from packages.core.models.mcp import MCPServer, AgentMCPBinding
from packages.core.services.reusable_resource_locks import (
    lock_agent_skill_binding_references,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec + result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CustomAgentSpec:
    """Inputs for ``provision_custom_agent``.

    Mirrors ``draft.fields.agent_mappings[i].create_agent_draft`` but the
    function is generic; nothing here is workspace-specific.
    """

    agent_name: str
    system_prompt: str
    agent_slug: Optional[str] = None
    description: str = ""
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    tool_bindings: List[str] = field(default_factory=list)
    business_capabilities: List[str] = field(default_factory=list)
    skill_bindings: List[str] = field(default_factory=list)  # ids OR slugs
    skill_binding_refs: List[Dict[str, Any]] = field(default_factory=list)
    mcp_bindings: List[str] = field(default_factory=list)    # ids OR server_keys
    mcp_allowed_tools: Dict[str, Optional[List[str]]] = field(default_factory=dict)
    missing_skill_specs: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "auto_provisioned"
    source_blueprint_id: Optional[str] = None
    source_blueprint_component_key: Optional[str] = None
    workspace_id: Optional[str] = None
    workspace_name: str = ""
    service_key: str = ""
    automation_id: Optional[str] = None
    automation_name: str = ""


@dataclass
class ProvisionResult:
    """What ``provision_custom_agent`` materialised."""
    agent_id: str
    agent_name: str
    bound_tools: List[str] = field(default_factory=list)
    bound_skills: List[str] = field(default_factory=list)
    created_skills: List[str] = field(default_factory=list)
    bound_mcp_servers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class AgentMCPBindingUpdateMode(StrEnum):
    """How one provisioning batch updates an existing MCP allowlist."""

    MERGE = "merge"
    REPLACE = "replace"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def provision_custom_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    spec: CustomAgentSpec,
    requester_user_id: str | None = None,
) -> ProvisionResult:
    """Materialise a CustomAgentSpec into a real Agent + bindings.

    The Agent + its bindings are flushed to the session, but **not
    committed** -- the caller (finalize_setup, an HTTP handler, etc.) is
    expected to own the transaction so this can compose with other DB
    work in the same request. Runtime/user entry points must provide
    ``requester_user_id`` so local Skill bindings pass the shared resource
    authorization gateway; omission is reserved for trusted setup/install
    orchestration.
    """
    if not spec.agent_name.strip():
        raise ValueError("agent_name is required")
    if not spec.system_prompt.strip():
        raise ValueError("system_prompt is required")

    warnings: List[str] = []
    capabilities = sorted({
        capability.strip()
        for capability in spec.business_capabilities
        if isinstance(capability, str) and capability.strip()
    })
    portable_slug = str(spec.agent_slug or "").strip() or None
    agent = None
    if (
        spec.source_blueprint_id
        and spec.source_blueprint_component_key
        and spec.workspace_id
    ):
        from packages.core.services.marketplace_resource_links import (
            RELATIONSHIP_INSTALLED_COMPONENT,
            RESOURCE_AGENT,
            RESOURCE_WORKSPACE_BLUEPRINT,
            SCOPE_WORKSPACE,
            get_marketplace_resource_link,
        )

        component_link = await get_marketplace_resource_link(
            db,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=spec.source_blueprint_id,
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=spec.workspace_id,
            local_resource_type=RESOURCE_AGENT,
            component_key=spec.source_blueprint_component_key,
            for_update=True,
        )
        if component_link is not None:
            agent = (await db.execute(
                select(Agent).where(
                    Agent.id == component_link.local_resource_id,
                    Agent.entity_id == entity_id,
                    Agent.workspace_id == spec.workspace_id,
                    Agent.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if agent is None:
                await db.delete(component_link)
                await db.flush()
    elif portable_slug:
        # Generic local Agent drafts retain historical slug idempotency.
        # Blueprint components use the exact scoped provenance branch above.
        agent = (await db.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.slug == portable_slug,
                Agent.deleted_at.is_(None),
            ).order_by(Agent.created_at.asc()).limit(1)
        )).scalar_one_or_none()

    if agent is None:
        agent_id = generate_ulid()
        from packages.core.services.agent_service import generate_agent_avatar_url
        agent = Agent(
            id=agent_id,
            entity_id=entity_id,
            workspace_id=(spec.workspace_id if spec.source_blueprint_id else None),
            name=spec.agent_name.strip(),
            slug=portable_slug,
            description=(spec.description or f"Custom agent: {spec.agent_name}").strip(),
            avatar_url=generate_agent_avatar_url(spec.agent_name.strip()),
            system_prompt=spec.system_prompt.strip(),
            category=spec.category,
            tags=list(spec.tags),
            is_template=False,
            is_public=False,
            source=spec.source,
            status="active",
            config={
                "auto_generated": True,
                "business_capabilities": capabilities,
            },
        )
        db.add(agent)
        await db.flush()
    else:
        from packages.core.revisions import (
            AGENT_CONTENT_REVISION_FIELDS,
            bump_revision,
            content_patch_for,
        )
        agent_id = agent.id
        config = dict(agent.config or {})
        config["auto_generated"] = bool(config.get("auto_generated", True))
        config["business_capabilities"] = sorted(set(
            config.get("business_capabilities") or []
        ) | set(capabilities))
        # M11: re-provisioning an existing agent with the same capability set
        # is a no-op — only a widened config / reactivation bumps.
        content_patch = content_patch_for(
            agent,
            {"config": config, "status": "active"},
            AGENT_CONTENT_REVISION_FIELDS,
        )
        agent.config = config
        agent.status = "active"
        if content_patch:
            await bump_revision(db, agent, patch=content_patch)

    if spec.source_blueprint_id and spec.source_blueprint_component_key and spec.workspace_id:
        from packages.core.services.marketplace_resource_links import (
            RELATIONSHIP_INSTALLED_COMPONENT,
            RESOURCE_AGENT,
            RESOURCE_WORKSPACE_BLUEPRINT,
            SCOPE_WORKSPACE,
            record_marketplace_resource_link,
        )

        await record_marketplace_resource_link(
            db,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=spec.source_blueprint_id,
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=spec.workspace_id,
            local_resource_type=RESOURCE_AGENT,
            local_resource_id=agent.id,
            component_key=spec.source_blueprint_component_key,
            metadata={"source_slug": portable_slug},
        )

    bound_tools = await _bind_tools(db, agent_id=agent_id, tool_names=spec.tool_bindings, warnings=warnings)
    base_skill_binding_config = _base_skill_binding_config(spec=spec, agent_id=agent_id)
    exact_bound_skills, missing_exact_skill_refs = (
        await _bind_exact_marketplace_skills(
            db,
            agent_id=agent_id,
            entity_id=entity_id,
            refs=spec.skill_binding_refs,
            binding_config=base_skill_binding_config,
        )
    )
    bound_skills, missing_skill_refs = await _bind_existing_skills(
        db,
        agent_id=agent_id,
        entity_id=entity_id,
        workspace_id=spec.workspace_id,
        refs=spec.skill_bindings,
        binding_config=base_skill_binding_config,
        requester_user_id=requester_user_id,
    )
    bound_skills = list(dict.fromkeys([*exact_bound_skills, *bound_skills]))
    created_skills, reused_skills, resolved_requested_skill_refs = (
        await _create_and_bind_missing_skills(
            db, agent_id=agent_id, entity_id=entity_id, category=spec.category,
            specs=spec.missing_skill_specs, warnings=warnings,
            workspace_id=spec.workspace_id,
            source_blueprint_id=spec.source_blueprint_id,
            binding_config=base_skill_binding_config,
        )
    )
    bound_skills.extend(ref for ref in reused_skills if ref not in bound_skills)
    materialized_skill_refs = (
        set(bound_skills)
        | set(created_skills)
        | set(reused_skills)
        | set(resolved_requested_skill_refs)
    )
    for ref in [*missing_exact_skill_refs, *missing_skill_refs]:
        if ref not in materialized_skill_refs:
            warnings.append(f"skill not found: {ref}")
    bound_mcp_servers = await _bind_mcp_servers(
        db,
        agent_id=agent_id,
        refs=spec.mcp_bindings,
        warnings=warnings,
        allowed_tools_by_server=spec.mcp_allowed_tools,
    )

    await db.flush()
    return ProvisionResult(
        agent_id=agent_id,
        agent_name=agent.name,
        bound_tools=bound_tools,
        bound_skills=bound_skills,
        created_skills=created_skills,
        bound_mcp_servers=bound_mcp_servers,
        warnings=warnings,
    )


def spec_from_create_agent_draft(
    draft: Dict[str, Any],
    *,
    workspace_id: str = "",
    workspace_name: str = "",
    operating_context: str = "",
    primary_work: str = "",
    service_key: str = "",
) -> CustomAgentSpec:
    """Adapter from ``mapping.create_agent_draft`` to a generic spec.

    IMPORTANT: an Agent is a *general worker* — owned by the entity,
    reusable across workspaces. The Agent's identity (name +
    description + system_prompt) describes the **capability**, not a
    specific workspace. Workspace-specific framing (operating_context,
    primary_work, "you serve workspace X") belongs in
    ``AgentSubscription.custom_prompt`` so the same Agent can be used
    with different framings in different workspaces.

    The architect is instructed to write the explicit ``system_prompt``
    accordingly. We pass the workspace metadata in only as fallback
    fodder for synthesizing a generic capability prompt when the
    architect didn't provide one — and even then we frame the output
    around the service / capability, not the workspace.
    """
    agent_name = (draft.get("agent_name") or "").strip() or service_key.replace("_", " ").title()
    explicit_prompt = (draft.get("system_prompt") or "").strip()
    seed = (draft.get("system_prompt_seed") or "").strip()

    if explicit_prompt:
        system_prompt = explicit_prompt
    else:
        capability = service_key.replace("_", " ") if service_key else "your assigned service"
        parts = [
            f"You are {agent_name}.",
            (
                seed
                or f"Your role is to deliver the '{capability}' capability for whatever workspace you're subscribed to."
            ),
            (
                "Operate as a general specialist in this capability. The workspace you're "
                "running in supplies its own framing (goals, channels, audience) via the "
                "subscription's custom_prompt — read those when present and follow them."
            ),
            f"Stay in the lane of '{capability}'; defer cross-capability requests to the operator." if service_key else "",
        ]
        system_prompt = "\n\n".join(p for p in parts if p)

    tags = []
    if service_key:
        tags.append(service_key)
    tags.append("auto_created")

    has_skills = bool(
        draft.get("skill_bindings")
        or draft.get("skill_binding_refs")
        or draft.get("missing_skill_specs")
    )
    tool_bindings = normalize_workspace_custom_agent_tool_bindings(
        draft.get("tool_bindings") or [],
        business_capability_ids=draft.get("business_capabilities") or [],
        has_skills=has_skills,
    )

    return CustomAgentSpec(
        agent_name=agent_name,
        system_prompt=system_prompt,
        agent_slug=(draft.get("agent_slug") or draft.get("blueprint_agent_slug")),
        description=draft.get("agent_description") or (
            f"General worker for '{service_key}' capability" if service_key else "Custom agent"
        ),
        category=service_key or None,
        tags=tags,
        tool_bindings=list(tool_bindings),
        business_capabilities=list(draft.get("business_capabilities") or []),
        skill_bindings=list(draft.get("skill_bindings") or []),
        skill_binding_refs=[
            dict(ref)
            for ref in draft.get("skill_binding_refs") or []
            if isinstance(ref, dict)
        ],
        mcp_bindings=list(draft.get("mcp_bindings") or []),
        mcp_allowed_tools={
            str(key): (list(value) if isinstance(value, list) else None)
            for key, value in (draft.get("mcp_allowed_tools") or {}).items()
            if str(key or "").strip()
        },
        missing_skill_specs=list(draft.get("missing_skill_specs") or []),
        source="auto_workspace_setup",
        source_blueprint_id=(
            str(draft.get("source_blueprint_id") or "").strip() or None
        ),
        source_blueprint_component_key=(
            str(draft.get("source_blueprint_component_key") or "").strip() or None
        ),
        workspace_id=workspace_id or None,
        workspace_name=workspace_name,
        service_key=service_key,
    )


# ---------------------------------------------------------------------------
# Internal binders
# ---------------------------------------------------------------------------

async def _bind_tools(
    db: AsyncSession, *, agent_id: str, tool_names: List[str], warnings: List[str],
) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for t in tool_names or []:
        if not isinstance(t, str):
            continue
        name = t.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return []
    existing = (await db.execute(
        select(ToolDefinition).where(
            ToolDefinition.name.in_(names),
            ToolDefinition.status == "active",
        )
    )).scalars().all()
    by_name = {t.name: t for t in existing}
    existing_tool_ids = set((await db.execute(
        select(AgentToolBinding.tool_id).where(AgentToolBinding.agent_id == agent_id)
    )).scalars().all())
    try:
        from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names

        registered_names = set(runtime_registered_tool_names())
    except Exception:
        registered_names = set()
    bound: List[str] = []
    for name in names:
        td = by_name.get(name)
        if td is None:
            if name not in registered_names:
                warnings.append(f"tool not found: {name}")
                continue
            # Lazy-register runtime-known tools missing from the DB catalog.
            td = ToolDefinition(
                id=generate_ulid(),
                name=name,
                display_name=name.replace("_", " ").title(),
                status="active",
            )
            db.add(td)
            await db.flush()
            by_name[name] = td
        if td.id not in existing_tool_ids:
            db.add(AgentToolBinding(agent_id=agent_id, tool_id=td.id))
            existing_tool_ids.add(td.id)
        bound.append(name)
    return bound


async def _add_agent_skill_binding_once(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str,
    skill_id: str,
    config: dict[str, Any] | None = None,
) -> None:
    existing = (await db.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.skill_id == skill_id,
        )
    )).scalar_one_or_none()
    if existing:
        existing.status = "active"
        existing.config = _merge_agent_skill_binding_config(existing.config, config)
        return
    await lock_agent_skill_binding_references(
        db,
        entity_id=entity_id,
        agent_id=agent_id,
        skill_id=skill_id,
    )
    db.add(AgentSkillBinding(
        id=generate_ulid(),
        agent_id=agent_id,
        skill_id=skill_id,
        config=_merge_agent_skill_binding_config({}, config),
        status="active",
    ))


async def _bind_existing_skills(
    db: AsyncSession,
    *,
    agent_id: str,
    entity_id: str,
    workspace_id: str | None,
    refs: List[str],
    binding_config: dict[str, Any] | None = None,
    requester_user_id: str | None = None,
) -> Tuple[List[str], List[str]]:
    refs = list(dict.fromkeys(
        s.strip() for s in (refs or [])
        if isinstance(s, str) and s.strip()
    ))
    if not refs:
        return [], []

    # Exact ids are authoritative. Slug lookup exists only for historical
    # drafts and is accepted solely when one accessible Skill has that slug.
    # Keep the same hyphen/underscore normalization as runtime invocation.
    from packages.core.services.skill_service import skill_slug_lookup_key

    local_skill_scope = or_(
        Skill.workspace_id.is_(None),
        Skill.workspace_id == workspace_id,
    )
    exact_rows = list((await db.execute(
        select(Skill).where(
            Skill.status == "active",
            or_(
                # Exact local IDs are authoritative across an entity. A
                # Skill's workspace_id is its home/ownership context, not an
                # invocation boundary. User entry points filter these rows
                # through the shared resource gateway below.
                Skill.entity_id == entity_id,
                and_(
                    Skill.entity_id.is_(None),
                    Skill.workspace_id.is_(None),
                    Skill.is_public.is_(True),
                ),
            ),
            Skill.id.in_(refs),
        )
    )).scalars().all())
    exact_match_refs = {skill.id for skill in exact_rows}
    if requester_user_id:
        from packages.core.models.permission import Capability, ResourceType
        from packages.core.services.resource_access import (
            ResourceDescriptor,
            user_can_access_resource,
        )

        visible_exact_rows: list[Skill] = []
        for skill in exact_rows:
            if skill.entity_id is None or await user_can_access_resource(
                db,
                descriptor=ResourceDescriptor.from_row(skill, ResourceType.SKILL),
                entity_id=entity_id,
                user_id=requester_user_id,
                capability=Capability.VIEW,
            ):
                visible_exact_rows.append(skill)
        exact_rows = visible_exact_rows
    exact_by_id = {skill.id: skill for skill in exact_rows}
    # A denied exact ID remains denied; never reinterpret it as a legacy slug
    # and accidentally bind a different accessible Skill with that value.
    legacy_refs = [ref for ref in refs if ref not in exact_match_refs]
    candidates_by_slug: dict[str, list[Skill]] = {}
    if legacy_refs:
        lookup_keys = list(dict.fromkeys(skill_slug_lookup_key(ref) for ref in legacy_refs))
        slug_rows = list((await db.execute(
            select(Skill).where(
                Skill.status == "active",
                or_(
                    and_(Skill.entity_id == entity_id, local_skill_scope),
                    and_(
                        Skill.entity_id.is_(None),
                        Skill.workspace_id.is_(None),
                        Skill.is_public.is_(True),
                    ),
                ),
                func.replace(func.lower(Skill.slug), "-", "_").in_(lookup_keys),
            )
        )).scalars().all())
        if requester_user_id:
            visible_slug_rows: list[Skill] = []
            for skill in slug_rows:
                if skill.entity_id is None or await user_can_access_resource(
                    db,
                    descriptor=ResourceDescriptor.from_row(
                        skill,
                        ResourceType.SKILL,
                    ),
                    entity_id=entity_id,
                    user_id=requester_user_id,
                    capability=Capability.VIEW,
                ):
                    visible_slug_rows.append(skill)
            slug_rows = visible_slug_rows
        for skill in slug_rows:
            if skill.slug:
                candidates_by_slug.setdefault(skill_slug_lookup_key(skill.slug), []).append(skill)

    bound: List[str] = []
    seen: set[str] = set()
    missing_refs: list[str] = []
    for ref in refs:
        skill = exact_by_id.get(ref)
        if skill is None:
            candidates = candidates_by_slug.get(skill_slug_lookup_key(ref)) or []
            if len(candidates) == 1:
                skill = candidates[0]
        if skill is None:
            missing_refs.append(ref)
            continue

        if skill.entity_id is None:
            from packages.core.services.marketplace_resource_links import (
                MarketplaceIdentityConflictError,
            )
            from packages.core.services.marketplace_skill_service import (
                ensure_marketplace_skill_installed,
            )

            try:
                skill = await ensure_marketplace_skill_installed(
                    db,
                    entity_id=entity_id,
                    skill_id=skill.id,
                )
            except MarketplaceIdentityConflictError:
                raise
            except ValueError:
                missing_refs.append(ref)
                continue
        if skill.id in seen:
            continue
        await _add_agent_skill_binding_once(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
            skill_id=skill.id,
            config={
                **(binding_config or {}),
                "match": {
                    "type": "explicit_skill_binding",
                    "requested_ref": ref,
                },
            },
        )
        bound.append(skill.slug or skill.id)
        seen.add(skill.id)
    return bound, missing_refs


async def _bind_exact_marketplace_skills(
    db: AsyncSession,
    *,
    agent_id: str,
    entity_id: str,
    refs: List[Dict[str, Any]],
    binding_config: dict[str, Any] | None = None,
) -> Tuple[List[str], List[str]]:
    bound: list[str] = []
    missing: list[str] = []
    seen_sources: set[tuple[str, str]] = set()
    seen_skills: set[str] = set()
    for raw_ref in refs or []:
        if not isinstance(raw_ref, dict):
            continue
        marketplace_source = str(
            raw_ref.get("marketplace_source") or "platform"
        ).strip()
        marketplace_id = str(raw_ref.get("marketplace_id") or "").strip()
        source_key = (marketplace_source, marketplace_id)
        if not marketplace_id or source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        requested_ref = f"{marketplace_source}:{marketplace_id}"
        skill = None
        if marketplace_source == "platform":
            from packages.core.services.marketplace_resource_links import (
                MarketplaceIdentityConflictError,
            )
            from packages.core.services.marketplace_skill_service import (
                ensure_marketplace_skill_installed,
            )

            try:
                skill = await ensure_marketplace_skill_installed(
                    db,
                    entity_id=entity_id,
                    skill_id=marketplace_id,
                )
            except MarketplaceIdentityConflictError:
                raise
            except ValueError:
                skill = None
        elif marketplace_source == "manor":
            # The curated Manor catalog is Cloud-only. OSS export retains a
            # valid branch that reports the exact ref as unavailable.
            skill = None
        if skill is None:
            missing.append(requested_ref)
            continue
        if skill.id in seen_skills:
            continue
        await _add_agent_skill_binding_once(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
            skill_id=skill.id,
            config={
                **(binding_config or {}),
                "match": {
                    "type": "exact_marketplace_skill_binding",
                    "marketplace_source": marketplace_source,
                    "marketplace_id": marketplace_id,
                    "requested_slug": str(raw_ref.get("slug") or "").strip(),
                },
            },
        )
        bound.append(skill.slug or skill.id)
        seen_skills.add(skill.id)
    return bound, missing


def _skill_ref(skill: Skill) -> str:
    return skill.slug or skill.id


async def _execute_skill_reuse_selector_completion(
    *,
    entity_id: str,
    requested_skill: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> str:
    """Ask the LLM whether an existing skill should satisfy a missing spec."""
    from packages.core.ai.runtime.completions import runtime_execute_text_completion
    from packages.core.ai.runtime.sources import RUNTIME_SKILL_MATCHER_SOURCE

    system = (
        "You select reusable skills for an AI workspace platform. "
        "Do not perform keyword matching. Judge semantic capability coverage: "
        "what the requested skill must do, what inputs/tools it needs, and "
        "whether an existing skill can perform the work as-is. Choose an "
        "existing skill only when it substantially covers the requested "
        "capability without needing a rewrite. Return JSON only."
    )
    user = {
        "requested_skill": requested_skill,
        "candidate_skills": candidates,
        "output_contract": {
            "reuse": "boolean",
            "skill_id": "candidate id when reuse=true, otherwise null",
            "confidence": "number from 0 to 1",
            "reason": "short explanation",
        },
    }
    completion = await runtime_execute_text_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, default=str)},
        ],
        entity_id=entity_id,
        source=RUNTIME_SKILL_MATCHER_SOURCE,
        temperature=0.1,
        max_tokens=500,
    )
    return completion.content or ""


async def _select_existing_skill_for_missing_spec(
    db: AsyncSession,
    *,
    entity_id: str,
    spec: Dict[str, Any],
    category: Optional[str],
    warnings: List[str],
) -> tuple[Skill, dict[str, Any]] | None:
    """Use an LLM to pick an existing skill before creating a new one."""
    from packages.core.services.skill_bundle import extract_json_object
    from packages.core.services.skill_service import is_placeholder_skill_identifier

    skill_priority = case((Skill.entity_id == entity_id, 0), else_=1)
    rows = (await db.execute(
        select(Skill)
        .where(
            Skill.status == "active",
            or_(
                Skill.entity_id == entity_id,
                and_(Skill.entity_id.is_(None), Skill.is_public.is_(True)),
            ),
        )
        .order_by(skill_priority.asc(), Skill.created_at.desc())
    )).scalars().all()
    candidates: List[Skill] = [
        row for row in rows
        if not is_placeholder_skill_identifier(row.name)
        and not is_placeholder_skill_identifier(row.slug)
    ]
    if not candidates:
        return None

    candidate_payload = [
        {
            "id": skill.id,
            "slug": skill.slug,
            "name": skill.name,
            "description": skill.description or "",
            "category": skill.category or "",
            "tools": list(skill.tools or []),
            "scope": "entity" if skill.entity_id == entity_id else "public",
        }
        for skill in candidates
    ]
    requested = {
        "name": spec.get("name") or "",
        "slug": spec.get("slug") or "",
        "description": spec.get("description") or "",
        "system_prompt": spec.get("system_prompt") or "",
        "tools": list(spec.get("tools") or []),
        "category": category or spec.get("category") or "",
    }

    try:
        raw = await _execute_skill_reuse_selector_completion(
            entity_id=entity_id,
            requested_skill=requested,
            candidates=candidate_payload,
        )
        decision = extract_json_object(raw)
    except Exception as exc:
        warnings.append(f"skill reuse selector failed: {exc}")
        return None

    if not bool(decision.get("reuse")):
        return None
    try:
        confidence = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    if confidence < 0.7:
        return None

    selected = str(decision.get("skill_id") or decision.get("id") or decision.get("slug") or "").strip()
    if not selected:
        return None
    for skill in candidates:
        if selected in {skill.id, skill.slug, skill.name}:
            logger.info(
                "Reusing existing skill %s for requested missing skill %r (confidence=%.2f)",
                skill.id, spec.get("name"), confidence,
            )
            return skill, {
                "type": "llm_reuse",
                "confidence": confidence,
                "reason": str(decision.get("reason") or "").strip(),
                "requested_skill_name": requested.get("name") or "",
                "requested_skill_slug": requested.get("slug") or "",
            }
    warnings.append(f"skill reuse selector returned unknown skill: {selected}")
    return None


async def _create_and_bind_missing_skills(
    db: AsyncSession,
    *,
    agent_id: str,
    entity_id: str,
    category: Optional[str],
    specs: List[Dict[str, Any]],
    warnings: List[str],
    workspace_id: Optional[str] = None,
    source_blueprint_id: Optional[str] = None,
    binding_config: dict[str, Any] | None = None,
) -> Tuple[List[str], List[str], List[str]]:
    created: List[str] = []
    reused: List[str] = []
    resolved_requested_refs: List[str] = []
    for ms in specs or []:
        if not isinstance(ms, dict):
            continue
        name = (ms.get("name") or "").strip()
        prompt = (ms.get("system_prompt") or "").strip()
        if not name or not prompt:
            warnings.append("skipped missing_skill_spec without name/system_prompt")
            continue

        component_key = str(
            ms.get("source_blueprint_component_key") or ""
        ).strip()
        linked_skill = None
        if source_blueprint_id and workspace_id and component_key:
            from packages.core.services.marketplace_resource_links import (
                RELATIONSHIP_INSTALLED_COMPONENT,
                RESOURCE_SKILL,
                RESOURCE_WORKSPACE_BLUEPRINT,
                SCOPE_WORKSPACE,
                get_marketplace_resource_link,
            )

            component_link = await get_marketplace_resource_link(
                db,
                entity_id=entity_id,
                marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
                marketplace_resource_id=source_blueprint_id,
                relationship=RELATIONSHIP_INSTALLED_COMPONENT,
                scope_type=SCOPE_WORKSPACE,
                scope_id=workspace_id,
                local_resource_type=RESOURCE_SKILL,
                component_key=component_key,
                for_update=True,
            )
            if component_link is not None:
                linked_skill = (await db.execute(
                    select(Skill).where(
                        Skill.id == component_link.local_resource_id,
                        Skill.entity_id == entity_id,
                        Skill.workspace_id == workspace_id,
                        Skill.status == "active",
                    )
                )).scalar_one_or_none()
                if linked_skill is None:
                    await db.delete(component_link)
                    await db.flush()

        existing = (
            (
                linked_skill,
                {
                    "type": "exact_blueprint_component",
                    "source_blueprint_id": source_blueprint_id,
                    "component_key": component_key,
                },
            )
            if linked_skill is not None else None
        )
        if existing is None and not (
            source_blueprint_id and workspace_id and component_key
        ):
            existing = await _select_existing_skill_for_missing_spec(
                db,
                entity_id=entity_id,
                spec=ms,
                category=category,
                warnings=warnings,
            )
        if existing is not None:
            existing_skill, match = existing
            await _add_agent_skill_binding_once(
                db,
                entity_id=entity_id,
                agent_id=agent_id,
                skill_id=existing_skill.id,
                config={
                    **(binding_config or {}),
                    "requested_skill": _requested_skill_binding_context(ms),
                    "match": match,
                },
            )
            reused.append(_skill_ref(existing_skill))
            resolved_requested_refs.extend(
                ref for ref in (ms.get("slug"), ms.get("name"))
                if isinstance(ref, str) and ref.strip()
            )
            continue

        from packages.core.services.skill_service import create_skill

        skill_row = await create_skill(
            db,
            entity_id=entity_id,
            name=name,
            system_prompt=prompt,
            slug=(ms.get("slug") or "").strip() or None,
            display_name=name,
            description=(ms.get("description") or "Auto-created skill")[:1000],
            tools=list(ms.get("tools") or []),
            input_schema={},
            output_format="text",
            category=category,
            tags=[category, "auto_created"] if category else ["auto_created"],
            is_public=False,
            version=str(ms.get("version") or "1.0.0"),
            workspace_id=(workspace_id if component_key else None),
            config={
                **dict(ms.get("config") or {}),
                "auto_generated": True,
                "source": (
                    "blueprint" if component_key else "auto_workspace_setup"
                ),
                **(
                    {
                        "source_blueprint_id": source_blueprint_id,
                        "source_blueprint_component_key": component_key,
                    }
                    if source_blueprint_id and component_key else {}
                ),
            },
        )
        if source_blueprint_id and workspace_id and component_key:
            from packages.core.services.marketplace_resource_links import (
                RELATIONSHIP_INSTALLED_COMPONENT,
                RESOURCE_SKILL,
                RESOURCE_WORKSPACE_BLUEPRINT,
                SCOPE_WORKSPACE,
                record_marketplace_resource_link,
            )

            await record_marketplace_resource_link(
                db,
                entity_id=entity_id,
                marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
                marketplace_resource_id=source_blueprint_id,
                relationship=RELATIONSHIP_INSTALLED_COMPONENT,
                scope_type=SCOPE_WORKSPACE,
                scope_id=workspace_id,
                local_resource_type=RESOURCE_SKILL,
                local_resource_id=skill_row.id,
                component_key=component_key,
                marketplace_version=str(ms.get("version") or "1.0.0"),
                metadata={"source_slug": ms.get("slug")},
            )
        await _add_agent_skill_binding_once(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
            skill_id=skill_row.id,
            config={
                **(binding_config or {}),
                "requested_skill": _requested_skill_binding_context(ms),
                "match": {"type": "generated_from_missing_skill_spec"},
            },
        )
        created.append(_skill_ref(skill_row))
        resolved_requested_refs.extend(
            ref for ref in (ms.get("slug"), ms.get("name"))
            if isinstance(ref, str) and ref.strip()
        )
    return created, reused, resolved_requested_refs


def _merge_agent_skill_binding_config(
    current: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(current or {})
    payload = dict(incoming or {})
    if not payload:
        return merged
    incoming_contexts = payload.pop("contexts", None)
    if incoming_contexts is None:
        incoming_contexts = [payload]
    elif isinstance(incoming_contexts, dict):
        incoming_contexts = [incoming_contexts]
    elif not isinstance(incoming_contexts, list):
        incoming_contexts = []

    contexts: list[dict[str, Any]] = [
        dict(item)
        for item in (merged.get("contexts") or [])
        if isinstance(item, dict)
    ]
    seen = {json.dumps(item, sort_keys=True, default=str) for item in contexts}
    for raw_context in incoming_contexts:
        if not isinstance(raw_context, dict):
            continue
        context = {k: v for k, v in raw_context.items() if v not in (None, "", [], {})}
        if not context:
            continue
        key = json.dumps(context, sort_keys=True, default=str)
        if key in seen:
            continue
        contexts.append(context)
        seen.add(key)
    if contexts:
        merged["contexts"] = contexts[-20:]

    for key, value in payload.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _base_skill_binding_config(*, spec: CustomAgentSpec, agent_id: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "binding_type": "agent_skill_binding",
        "source": spec.source or "agent_provisioning",
        "agent_id": agent_id,
        "agent_name": spec.agent_name,
        "workspace_id": spec.workspace_id,
        "workspace_name": spec.workspace_name,
        "service_key": spec.service_key,
        "automation_id": spec.automation_id,
        "automation_name": spec.automation_name,
    }
    return {k: v for k, v in config.items() if v not in (None, "", [], {})}


def _requested_skill_binding_context(spec: Dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "name": spec.get("name"),
            "slug": spec.get("slug"),
            "description": spec.get("description"),
            "tools": list(spec.get("tools") or []),
        }.items()
        if value not in (None, "", [], {})
    }


async def _bind_mcp_servers(
    db: AsyncSession,
    *,
    agent_id: str,
    refs: List[str],
    warnings: List[str],
    allowed_tools_by_server: Dict[str, Optional[List[str] | Tuple[str, ...]]] | None = None,
    update_mode: AgentMCPBindingUpdateMode = AgentMCPBindingUpdateMode.REPLACE,
) -> List[str]:
    refs = [m for m in (refs or []) if isinstance(m, str) and m]
    if not refs:
        return []
    servers = (await db.execute(
        select(MCPServer).where(
            MCPServer.status == "active",
            (MCPServer.id.in_(refs)) | (MCPServer.server_key.in_(refs)),
        )
    )).scalars().all()
    existing_bindings = {
        binding.mcp_server_id: binding
        for binding in (await db.execute(
            select(AgentMCPBinding).where(AgentMCPBinding.agent_id == agent_id)
        )).scalars().all()
    }
    # Agent settings renders MCP actions from ToolDefinition rows, while the
    # runtime authorizes them from AgentMCPBinding.  Keep both projections in
    # sync whenever provisioning supplies an exact action allowlist so an
    # AI-created Agent can be reviewed and safely edited in the same UI as a
    # manually configured Agent.
    mcp_tool_definitions_by_provider: dict[str, list[tuple[ToolDefinition, str]]] = {}
    existing_mcp_tool_bindings: dict[str, AgentToolBinding] = {}
    if allowed_tools_by_server is not None:
        from packages.core.services.agent_permission_service import parse_mcp_tool_name
        from packages.core.services.provider_keys import canonical_provider_key

        mcp_tool_definitions = list((await db.execute(
            select(ToolDefinition).where(
                ToolDefinition.status == "active",
                ToolDefinition.name.like("mcp__%"),
            )
        )).scalars().all())
        for tool_definition in mcp_tool_definitions:
            parsed = parse_mcp_tool_name(tool_definition.name)
            if parsed is None:
                continue
            provider, action = parsed
            mcp_tool_definitions_by_provider.setdefault(
                canonical_provider_key(provider), []
            ).append((tool_definition, action))
        existing_mcp_tool_bindings = {
            binding.tool_id: binding
            for binding in (await db.execute(
                select(AgentToolBinding).where(AgentToolBinding.agent_id == agent_id)
            )).scalars().all()
        }
    bound: List[str] = []
    matched_refs: set[str] = set()
    for srv in servers:
        has_explicit_allowlist = bool(
            allowed_tools_by_server is not None
            and srv.server_key in allowed_tools_by_server
        )
        selected_actions = (
            allowed_tools_by_server.get(srv.server_key)
            if has_explicit_allowlist and allowed_tools_by_server is not None
            else None
        )
        normalized_allowed_tools = (
            sorted({str(action).strip() for action in selected_actions if str(action).strip()})
            if selected_actions is not None
            else None
        )
        existing = existing_bindings.get(srv.id)
        if existing is not None:
            existing_wildcard = (
                existing.status == "active" and existing.allowed_tools is None
            )
            existing.status = "active"
            if has_explicit_allowlist:
                if update_mode is AgentMCPBindingUpdateMode.MERGE:
                    if existing_wildcard or normalized_allowed_tools is None:
                        normalized_allowed_tools = None
                    else:
                        normalized_allowed_tools = sorted(
                            {
                                str(action).strip()
                                for action in (existing.allowed_tools or [])
                                if str(action).strip()
                            }
                            | set(normalized_allowed_tools)
                        )
                existing.allowed_tools = normalized_allowed_tools
        else:
            binding = AgentMCPBinding(
                id=generate_ulid(),
                agent_id=agent_id,
                mcp_server_id=srv.id,
                allowed_tools=normalized_allowed_tools if has_explicit_allowlist else None,
                status="active",
            )
            db.add(binding)
            existing_bindings[srv.id] = binding
        if has_explicit_allowlist:
            from packages.core.services.provider_keys import canonical_provider_key

            provider_tools = mcp_tool_definitions_by_provider.get(
                canonical_provider_key(srv.server_key), []
            )
            provider_tool_ids = {
                tool_definition.id for tool_definition, _action in provider_tools
            }
            if normalized_allowed_tools is None:
                selected_tool_ids = provider_tool_ids
            else:
                selected_action_set = set(normalized_allowed_tools)
                selected_tool_ids = {
                    tool_definition.id
                    for tool_definition, action in provider_tools
                    if action in selected_action_set
                }
            for tool_id in provider_tool_ids - selected_tool_ids:
                stale_binding = existing_mcp_tool_bindings.pop(tool_id, None)
                if stale_binding is not None:
                    await db.delete(stale_binding)
            for tool_id in selected_tool_ids - set(existing_mcp_tool_bindings):
                tool_binding = AgentToolBinding(agent_id=agent_id, tool_id=tool_id)
                db.add(tool_binding)
                existing_mcp_tool_bindings[tool_id] = tool_binding
        bound.append(srv.server_key)
        matched_refs.add(srv.server_key)
        matched_refs.add(srv.id)
    for ref in refs:
        if ref not in matched_refs:
            warnings.append(f"mcp server not found: {ref}")
    return bound


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "skill"
