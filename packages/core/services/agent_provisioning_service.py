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
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, or_, select
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
    description: str = ""
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    tool_bindings: List[str] = field(default_factory=list)
    business_capabilities: List[str] = field(default_factory=list)
    skill_bindings: List[str] = field(default_factory=list)  # ids OR slugs
    mcp_bindings: List[str] = field(default_factory=list)    # ids OR server_keys
    missing_skill_specs: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "auto_provisioned"
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def provision_custom_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    spec: CustomAgentSpec,
) -> ProvisionResult:
    """Materialise a CustomAgentSpec into a real Agent + bindings.

    The Agent + its bindings are flushed to the session, but **not
    committed** -- the caller (finalize_setup, an HTTP handler, etc.) is
    expected to own the transaction so this can compose with other DB
    work in the same request.
    """
    if not spec.agent_name.strip():
        raise ValueError("agent_name is required")
    if not spec.system_prompt.strip():
        raise ValueError("system_prompt is required")

    warnings: List[str] = []

    # ── Create the Agent row ──
    agent_id = generate_ulid()
    from packages.core.services.agent_service import generate_agent_avatar_url
    agent = Agent(
        id=agent_id,
        entity_id=entity_id,
        name=spec.agent_name.strip(),
        description=(spec.description or f"Custom agent: {spec.agent_name}").strip(),
        avatar_url=generate_agent_avatar_url(spec.agent_name.strip()),
        system_prompt=spec.system_prompt.strip(),
        category=spec.category,
        tags=list(spec.tags),
        is_template=False,
        is_public=False,
        source=spec.source,
        status="active",
        config={"auto_generated": True},
    )
    db.add(agent)
    await db.flush()

    bound_tools = await _bind_tools(db, agent_id=agent_id, tool_names=spec.tool_bindings, warnings=warnings)
    base_skill_binding_config = _base_skill_binding_config(spec=spec, agent_id=agent_id)
    bound_skills = await _bind_existing_skills(
        db,
        agent_id=agent_id,
        entity_id=entity_id,
        refs=spec.skill_bindings,
        warnings=warnings,
        binding_config=base_skill_binding_config,
    )
    created_skills, reused_skills = await _create_and_bind_missing_skills(
        db, agent_id=agent_id, entity_id=entity_id, category=spec.category,
        specs=spec.missing_skill_specs, warnings=warnings,
        binding_config=base_skill_binding_config,
    )
    bound_skills.extend(ref for ref in reused_skills if ref not in bound_skills)
    bound_mcp_servers = await _bind_mcp_servers(
        db, agent_id=agent_id, refs=spec.mcp_bindings, warnings=warnings,
    )

    await db.flush()
    return ProvisionResult(
        agent_id=agent_id,
        agent_name=spec.agent_name,
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
        description=draft.get("agent_description") or (
            f"General worker for '{service_key}' capability" if service_key else "Custom agent"
        ),
        category=service_key or None,
        tags=tags,
        tool_bindings=list(tool_bindings),
        business_capabilities=list(draft.get("business_capabilities") or []),
        skill_bindings=list(draft.get("skill_bindings") or []),
        mcp_bindings=list(draft.get("mcp_bindings") or []),
        missing_skill_specs=list(draft.get("missing_skill_specs") or []),
        source="auto_workspace_setup",
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
        db.add(AgentToolBinding(agent_id=agent_id, tool_id=td.id))
        bound.append(name)
    return bound


async def _add_agent_skill_binding_once(
    db: AsyncSession,
    *,
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
    refs: List[str],
    warnings: List[str],
    binding_config: dict[str, Any] | None = None,
) -> List[str]:
    refs = [s for s in (refs or []) if isinstance(s, str) and s]
    if not refs:
        return []
    rows = (await db.execute(
        select(Skill).where(
            Skill.status == "active",
            or_(Skill.entity_id == entity_id, Skill.is_public.is_(True)),
            (Skill.id.in_(refs)) | (Skill.slug.in_(refs)),
        )
    )).scalars().all()
    bound: List[str] = []
    seen: set[str] = set()
    matched_refs: set[str] = set()
    for s in rows:
        if s.id in seen:
            continue
        await _add_agent_skill_binding_once(
            db,
            agent_id=agent_id,
            skill_id=s.id,
            config={
                **(binding_config or {}),
                "match": {"type": "explicit_skill_binding"},
            },
        )
        bound.append(s.slug or s.id)
        seen.add(s.id)
        matched_refs.add(s.id)
        if s.slug:
            matched_refs.add(s.slug)
    for ref in refs:
        if ref not in matched_refs:
            warnings.append(f"skill not found: {ref}")
    return bound


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
            or_(Skill.entity_id == entity_id, Skill.is_public.is_(True)),
        )
        .order_by(skill_priority.asc(), Skill.created_at.desc())
        .limit(80)
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
    binding_config: dict[str, Any] | None = None,
) -> Tuple[List[str], List[str]]:
    created: List[str] = []
    reused: List[str] = []
    for ms in specs or []:
        if not isinstance(ms, dict):
            continue
        name = (ms.get("name") or "").strip()
        prompt = (ms.get("system_prompt") or "").strip()
        if not name or not prompt:
            warnings.append("skipped missing_skill_spec without name/system_prompt")
            continue

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
                agent_id=agent_id,
                skill_id=existing_skill.id,
                config={
                    **(binding_config or {}),
                    "requested_skill": _requested_skill_binding_context(ms),
                    "match": match,
                },
            )
            reused.append(_skill_ref(existing_skill))
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
            version="1.0.0",
            config={"auto_generated": True, "source": "auto_workspace_setup"},
        )
        await _add_agent_skill_binding_once(
            db,
            agent_id=agent_id,
            skill_id=skill_row.id,
            config={
                **(binding_config or {}),
                "requested_skill": _requested_skill_binding_context(ms),
                "match": {"type": "generated_from_missing_skill_spec"},
            },
        )
        created.append(_skill_ref(skill_row))
    return created, reused


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
    db: AsyncSession, *, agent_id: str, refs: List[str], warnings: List[str],
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
    bound: List[str] = []
    matched_refs: set[str] = set()
    for srv in servers:
        db.add(AgentMCPBinding(
            id=generate_ulid(),
            agent_id=agent_id,
            mcp_server_id=srv.id,
            status="active",
        ))
        bound.append(srv.server_key)
        matched_refs.add(srv.server_key)
        matched_refs.add(srv.id)
    for ref in refs:
        if ref not in matched_refs:
            warnings.append(f"mcp server not found: {ref}")
    return bound


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "skill"
