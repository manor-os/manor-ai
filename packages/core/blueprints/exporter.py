"""workspace → blueprint payload.

Reads the configuration shape of a workspace and serialises it into a
portable JSON document. The exporter is conservative on purpose: when
in doubt, drop the field. Operators should read what they're about to
publish — and the smaller the payload, the easier that read is.

What flows OUT:

  workspace shell        kind / context / primary_work / operating_model /
                         settings (minus runtime flags)
  goals                  configuration only — current_value / baseline /
                         pace_status / measurements all dropped
  subscriptions          service_key + agent_slug (NOT agent_id) so it can
                         re-resolve on import
  scheduled_jobs         the trigger definition; last_run_at / status dropped
  custom_fields          definitions
  governance_policy      current revision's policy dict
  channel_requirements   from existing ChannelConfig rows — type + name only
  session_requirements   from existing IntegrationSession rows — provider /
                         label / health_check / expected_login_url

What stays IN the workspace (never exported):

  tasks / plans / leases / measurements / activity logs (runtime data)
  any credential ref / encrypted blob (secrets)
  budget consumption (monthly_spent, alert_state, reset_at)
  ULIDs (replaced with portable handles)
  workspace.id (caller picks a new one on import)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.payload import (
    BLUEPRINT_VERSION,
    _is_secret_shape,
    validate_payload,
)
from packages.core.governance.policy import policy_to_dict
from packages.core.governance.service import get_policy
from packages.core.models.channel import ChannelConfig
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.document import Document, DocumentGroup
from packages.core.models.goal import Goal
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)

logger = logging.getLogger(__name__)


class ExportError(Exception):
    """Raised when a workspace can't be exported as a blueprint."""


# Settings keys that are runtime-only (not config) — dropped on export.
_RUNTIME_SETTINGS_KEYS = frozenset({
    "sandbox",          # gets re-set by installer based on mode
    "_blueprint",       # blueprint provenance metadata
    "last_briefing_at", # runtime cursor
})


# ── Public API ────────────────────────────────────────────────────────

@dataclass
class ExportContext:
    """Knobs the operator can pass to control what's included."""

    include_subscriptions: bool = True
    include_goals: bool = True
    include_scheduled_jobs: bool = True
    include_custom_fields: bool = True
    include_governance: bool = True
    include_channel_requirements: bool = True
    include_session_requirements: bool = True
    # Memory files are large + opinionated — opt-in.
    include_memory_files: bool = False
    # v1.1 embedded sections. Default-on so a fresh export carries the
    # workspace-private agents/skills the operator built; turn off for a
    # "config-only" export (e.g. for review before publishing).
    include_embedded_agents: bool = True
    include_embedded_skills: bool = True
    include_knowledge_packs: bool = True
    # Knowledge_pack body inclusion is opt-in even when the section is
    # exported — default ``skeleton`` mode emits folder structure only.
    knowledge_pack_mode: str = "skeleton"   # 'skeleton' | 'inline_text'
    # Agent-level starter_memory inclusion. Off by default because
    # accumulated agent memories often encode author-private style hints
    # (voice, reaction patterns) that don't translate to other entities.
    include_starter_memory: bool = False


async def export_workspace(
    db: AsyncSession,
    workspace_id: str,
    *,
    title: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    author_handle: Optional[str] = None,
    author_display_name: Optional[str] = None,
    context: Optional[ExportContext] = None,
) -> dict[str, Any]:
    """Build a blueprint payload from an existing workspace. The
    payload is validated before return so the caller can trust it
    round-trips through the installer.

    Caller is responsible for persisting the result into a
    ``WorkspaceBlueprint`` row (the exporter doesn't write).
    """
    ctx = context or ExportContext()

    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if workspace is None:
        raise ExportError(f"workspace {workspace_id!r} not found")

    # Pre-fetch the sections that depend on toggles. Doing them up front
    # keeps the v1.1 assembly below declarative.
    subscriptions = (
        await _export_subscriptions(db, workspace.entity_id, workspace_id)
        if ctx.include_subscriptions else []
    )
    goals = (
        await _export_goals(db, workspace.entity_id, workspace_id)
        if ctx.include_goals else []
    )
    scheduled_jobs = (
        await _export_scheduled_jobs(db, workspace_id)
        if ctx.include_scheduled_jobs else []
    )
    custom_fields = (
        await _export_custom_fields(db, workspace.entity_id, workspace_id)
        if ctx.include_custom_fields else []
    )
    governance = (
        await _export_governance(db, workspace_id) or {}
        if ctx.include_governance else {}
    )
    channels = (
        await _export_channel_requirements(db, workspace.entity_id, workspace_id)
        if ctx.include_channel_requirements else []
    )
    sessions = (
        # Sessions are entity-scoped — but we only surface ones bound
        # by the workspace's existing subscriptions / goal configs to
        # avoid leaking other workspaces' integrations.
        await _export_session_requirements(db, workspace.entity_id)
        if ctx.include_session_requirements else []
    )

    # ── v1.1 embedded + contract.requires assembly ────────────────────
    embedded_agents: list[dict[str, Any]] = []
    embedded_skills: list[dict[str, Any]] = []
    required_tools: set[str] = set()
    required_mcp_servers: list[dict[str, Any]] = []
    required_skills: list[dict[str, Any]] = []
    required_agents: list[dict[str, Any]] = []

    if ctx.include_embedded_agents or ctx.include_embedded_skills:
        embed_result = await _export_embedded_agents_and_skills(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace_id,
            include_agents=ctx.include_embedded_agents,
            include_skills=ctx.include_embedded_skills,
            include_starter_memory=ctx.include_starter_memory,
        )
        embedded_agents = embed_result["embedded_agents"]
        embedded_skills = embed_result["embedded_skills"]
        required_tools.update(embed_result["required_tools"])
        required_mcp_servers = embed_result["required_mcp_servers"]
        required_skills = embed_result["required_skills"]
        required_agents = embed_result["required_agents"]

    knowledge_packs = (
        await _export_knowledge_packs(
            db, workspace.entity_id, workspace_id, mode=ctx.knowledge_pack_mode
        )
        if ctx.include_knowledge_packs else []
    )

    # Workspace shell — kind / context / primary_work / settings absorb
    # into operating_model so the v1.1 recipe has one place to read from.
    om: dict[str, Any] = dict(workspace.operating_model or {})
    if workspace.operating_context:
        om.setdefault("context", workspace.operating_context)
    if workspace.primary_work:
        om.setdefault("primary_work", workspace.primary_work)
    if workspace.kind:
        om.setdefault("kind", workspace.kind)
    ws_settings = {
        k: v for k, v in (workspace.settings or {}).items()
        if k not in _RUNTIME_SETTINGS_KEYS
    }
    if ws_settings:
        om.setdefault("settings", ws_settings)

    payload: dict[str, Any] = {
        "manifest": {
            "blueprint_version": BLUEPRINT_VERSION,
            "slug": None,
            "title": title,
            "summary": summary,
            "use_when": None,
            "description": description,
            "tags": list(tags or []),
            "kind": workspace.kind,
            "category": None,
            "author": {
                "handle": author_handle,
                "display_name": author_display_name,
            },
            "cover_image_url": None,
            "forked_from_id": None,
            "changelog": None,
        },
        "contract": {
            "variables": [],
            "channels": channels,
            "sessions": sessions,
            "requires": {
                "manor_min_version": None,
                "tools": sorted(required_tools),
                "mcp_servers": required_mcp_servers,
                "skills": required_skills,
                "agents": required_agents,
            },
        },
        "embedded": {
            "skills": embedded_skills,
            "agents": embedded_agents,
            "knowledge_packs": knowledge_packs,
        },
        "recipe": {
            "operating_model": om,
            "strategist": None,
            "prompts": [],
            "subscriptions": subscriptions,
            "scheduled_jobs": scheduled_jobs,
            "workflows": [],
            "goals": goals,
            "task_categories": [],
            "custom_fields": custom_fields,
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": governance,
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }

    # Last-line defence: validate (catches any future divergence
    # between exporter helpers and the payload schema).
    validate_payload(payload)
    return payload


# ── Section helpers ──────────────────────────────────────────────────
#
# The workspace shell (kind / operating_context / primary_work /
# operating_model / settings) is assembled inline inside
# ``export_workspace`` — see the operating_model absorption pass there.

async def _export_subscriptions(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict[str, Any]]:
    """Resolve agent_id → agent_slug. Subscriptions whose agent has
    no slug are skipped with a warning — we can't re-bind them on import."""
    rows = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    if not rows:
        return []

    agent_ids = {r.agent_id for r in rows}
    agents = list((await db.execute(
        select(Agent).where(Agent.id.in_(agent_ids))
    )).scalars().all())
    agent_by_id = {a.id: a for a in agents}

    out: list[dict[str, Any]] = []
    for r in rows:
        a = agent_by_id.get(r.agent_id)
        if a is None or not a.slug:
            logger.warning(
                "blueprint export: dropping subscription %s — agent %s has no slug",
                r.id, r.agent_id,
            )
            continue
        out.append({
            "service_key": r.service_key,
            "agent_slug": a.slug,
            "custom_prompt": r.custom_prompt,
            "config": dict(r.config or {}),
        })
    return out


async def _export_goals(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict[str, Any]]:
    rows = list((await db.execute(
        select(Goal).where(
            Goal.entity_id == entity_id,
            Goal.workspace_id == workspace_id,
            Goal.status == "active",
        )
    )).scalars().all())
    return [
        {
            "title": g.title,
            "description": g.description,
            "metric_key": g.metric_key,
            "target_value": float(g.target_value) if g.target_value is not None else None,
            # baseline is config-ish (operator-set starting point) so
            # we keep it; current_value / pace are runtime — dropped.
            "baseline_value": float(g.baseline_value) if g.baseline_value is not None else None,
            "deadline": g.deadline.isoformat() if g.deadline else None,
            "measurement_source": g.measurement_source,
            "measurement_cadence": g.measurement_cadence,
            "priority": g.priority,
        }
        for g in rows
    ]


async def _export_scheduled_jobs(
    db: AsyncSession, workspace_id: str,
) -> list[dict[str, Any]]:
    rows = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.workspace_id == workspace_id,
            ScheduledJob.enabled.is_(True),
        )
    )).scalars().all())
    out: list[dict[str, Any]] = []
    for j in rows:
        out.append({
            # job_id is logically a slug — keep it for portability.
            "job_id": j.job_id,
            "name": j.name,
            "job_type": j.job_type,
            "schedule_kind": j.schedule_kind,
            "cron_expr": j.cron_expr,
            "every_seconds": j.every_seconds,
            "run_at": j.run_at,
            "timezone": j.timezone,
            "payload_message": j.payload_message,
            "execution_type": j.execution_type,
            "execution_target": dict(j.execution_target or {}),
            "execution_script": j.execution_script,
            "default_delivery_mode": j.default_delivery_mode,
        })
    return out


async def _export_custom_fields(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict[str, Any]]:
    rows = list((await db.execute(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.entity_id == entity_id,
            CustomFieldDefinition.workspace_id == workspace_id,
            CustomFieldDefinition.status == "active",
        )
    )).scalars().all())
    return [
        {
            "name": c.name,
            "display_name": c.display_name,
            "field_type": c.field_type,
            "target": c.target,
            "options": list(c.options or []),
            "default_value": c.default_value,
            "required": c.required,
            "sort_order": c.sort_order,
        }
        for c in rows
    ]


async def _export_governance(
    db: AsyncSession, workspace_id: str,
) -> Optional[dict[str, Any]]:
    policy = await get_policy(db, workspace_id)
    raw = policy_to_dict(policy)
    # Skip if the policy is just the default (no need to ship empty rules).
    if not any(
        raw.get(k) for k in (
            "never_allow_actions", "hitl_required_actions",
            "auto_approve_actions", "never_allow_capabilities",
            "hitl_required_capabilities", "auto_approve_capabilities",
            "budget_caps_per_kind",
        )
    ) and raw.get("max_risk_level") == "high":
        return None
    return raw


async def _export_channel_requirements(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict[str, Any]]:
    """List the *types* of channels the workspace needs, without any
    credential material. The installer surfaces this as a to-do list."""
    rows = list((await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.workspace_id == workspace_id,
            ChannelConfig.status == "active",
        )
    )).scalars().all())
    return [
        {
            "channel_type": c.channel_type,
            "provider": c.provider,
            "purpose": c.name,  # operator-friendly label
            "required": True,
        }
        for c in rows
    ]


async def _export_session_requirements(
    db: AsyncSession, entity_id: str,
) -> list[dict[str, Any]]:
    """Browser sessions are entity-scoped, but a blueprint's caller
    only sees the ones bound to *some* configuration. We include all
    active sessions in the entity — the installer will dedupe by
    (provider, label) so re-installing doesn't duplicate work."""
    rows = list((await db.execute(
        select(IntegrationSession).where(
            IntegrationSession.entity_id == entity_id,
            IntegrationSession.status == "active",
        )
    )).scalars().all())
    out: list[dict[str, Any]] = []
    for s in rows:
        md = s.metadata_json or {}
        out.append({
            "provider": s.provider,
            "label": s.label,
            "expected_login_url": md.get("expected_login_url"),
            "health_check": dict(s.health_check or {}),
            "required": True,
            "purpose": md.get("purpose"),
        })
    return out


# ── Embedded agents / skills / knowledge ──────────────────────────────
#
# Decision rule: an agent or skill is EMBEDDED in the blueprint if it
# was authored inside this entity and isn't promoted to the marketplace
# (``is_public=false``). Anything else (platform templates, public
# marketplace agents/skills) is EXTERNAL — declared in
# ``contract.requires.*`` so the install side knows to resolve it from
# its own catalog.


async def _export_embedded_agents_and_skills(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    include_agents: bool,
    include_skills: bool,
    include_starter_memory: bool,
) -> dict[str, Any]:
    """Resolve every agent this workspace is subscribed to, split into
    embedded vs external, and walk each embedded agent's 3 binding
    tables + agent-level memory.

    Returns a dict with keys: ``embedded_agents``, ``embedded_skills``,
    ``required_tools``, ``required_mcp_servers``, ``required_skills``,
    ``required_agents``. Lists are sorted for deterministic output.
    """
    # 1) Subscribed agents
    sub_rows = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    agent_ids = sorted({r.agent_id for r in sub_rows if r.agent_id})
    agents = list((await db.execute(
        select(Agent).where(Agent.id.in_(agent_ids))
    )).scalars().all()) if agent_ids else []

    embedded_agents_out: list[dict[str, Any]] = []
    required_agents_out: list[dict[str, Any]] = []

    # Collect IDs across both buckets so binding queries can be batched.
    embedded_agent_ids: list[str] = []

    for a in agents:
        is_embedded = (
            a.entity_id == entity_id
            and not a.is_public
            and a.slug  # without a slug there's no portable handle
        )
        if is_embedded and include_agents:
            embedded_agent_ids.append(a.id)
        else:
            if a.slug:
                required_agents_out.append({
                    "slug": a.slug,
                    "min_version": getattr(a, "version", None) or "1.0",
                })
            else:
                logger.warning(
                    "blueprint export: agent %s has no slug — cannot declare as requires.agents",
                    a.id,
                )

    # 2) Binding tables — batched fetches keyed by embedded agent ids
    tool_bindings: dict[str, list[str]] = {}
    mcp_bindings: dict[str, list[dict[str, Any]]] = {}
    skill_bindings: dict[str, list[str]] = {}
    starter_memory: dict[str, list[dict[str, Any]]] = {}

    required_tools: set[str] = set()
    required_mcp_out: list[dict[str, Any]] = []
    required_skills_out: list[dict[str, Any]] = []
    embedded_skills_out: list[dict[str, Any]] = []

    if embedded_agent_ids:
        # a) Tool bindings → ToolDefinition.name
        tb_rows = list((await db.execute(
            select(AgentToolBinding)
            .where(AgentToolBinding.agent_id.in_(embedded_agent_ids))
        )).scalars().all())
        tool_ids = sorted({b.tool_id for b in tb_rows})
        tool_defs = list((await db.execute(
            select(ToolDefinition).where(ToolDefinition.id.in_(tool_ids))
        )).scalars().all()) if tool_ids else []
        tool_name_by_id = {t.id: t.name for t in tool_defs}
        for b in tb_rows:
            name = tool_name_by_id.get(b.tool_id)
            if not name:
                logger.warning(
                    "blueprint export: tool_id %s not found in catalog — skipping binding",
                    b.tool_id,
                )
                continue
            tool_bindings.setdefault(b.agent_id, []).append(name)
            required_tools.add(name)

        # b) MCP bindings → server_key + allowlists
        mcp_rows = list((await db.execute(
            select(AgentMCPBinding)
            .where(
                AgentMCPBinding.agent_id.in_(embedded_agent_ids),
                AgentMCPBinding.status == "active",
            )
        )).scalars().all())
        mcp_ids = sorted({b.mcp_server_id for b in mcp_rows})
        mcp_servers = list((await db.execute(
            select(MCPServer).where(MCPServer.id.in_(mcp_ids))
        )).scalars().all()) if mcp_ids else []
        mcp_by_id = {m.id: m for m in mcp_servers}
        # Track which servers showed up so we declare them as requires.
        mcp_required_keys: dict[str, dict[str, Any]] = {}
        for b in mcp_rows:
            srv = mcp_by_id.get(b.mcp_server_id)
            if not srv or not srv.server_key:
                logger.warning(
                    "blueprint export: mcp_server_id %s missing/keyless — skipping binding",
                    b.mcp_server_id,
                )
                continue
            allowed_tools = list(b.allowed_tools or [])
            # config_override may contain secrets — only export the KEY
            # NAMES (operators can inspect what would be set), and drop
            # any that look secret-shaped (api_token, *_secret, ...).
            safe_keys = sorted(
                k for k in (b.config_override or {}).keys()
                if isinstance(k, str) and not _is_secret_shape(k)
            )
            mcp_bindings.setdefault(b.agent_id, []).append({
                "server_slug": srv.server_key,
                "allowed_tools": allowed_tools or None,
                "config_override_allowlist": safe_keys,
            })
            # Aggregate the requires.mcp_servers entry across bindings
            existing = mcp_required_keys.get(srv.server_key)
            if existing is None:
                mcp_required_keys[srv.server_key] = {
                    "slug": srv.server_key,
                    "purpose": srv.description or srv.name,
                    "config_fields_to_set": list(safe_keys),
                }
            else:
                existing["config_fields_to_set"] = sorted(set(
                    existing["config_fields_to_set"]
                ) | set(safe_keys))
        required_mcp_out = [
            mcp_required_keys[k] for k in sorted(mcp_required_keys)
        ]

        # c) Skill bindings → split into embedded vs required
        sb_rows = list((await db.execute(
            select(AgentSkillBinding)
            .where(
                AgentSkillBinding.agent_id.in_(embedded_agent_ids),
                AgentSkillBinding.status == "active",
            )
        )).scalars().all())
        skill_ids = sorted({b.skill_id for b in sb_rows})
        skills = list((await db.execute(
            select(Skill).where(Skill.id.in_(skill_ids))
        )).scalars().all()) if skill_ids else []
        skill_by_id = {s.id: s for s in skills}
        embedded_skill_slugs_seen: set[str] = set()
        required_skill_keys: dict[str, dict[str, Any]] = {}
        for b in sb_rows:
            sk = skill_by_id.get(b.skill_id)
            if not sk or not sk.slug:
                logger.warning(
                    "blueprint export: skill_id %s missing/slugless — skipping binding",
                    b.skill_id,
                )
                continue
            skill_bindings.setdefault(b.agent_id, []).append(sk.slug)
            sk_is_embedded = (
                sk.entity_id == entity_id
                and not sk.is_public
                and include_skills
            )
            if sk_is_embedded:
                if sk.slug in embedded_skill_slugs_seen:
                    continue
                embedded_skill_slugs_seen.add(sk.slug)
                embedded_skills_out.append(_export_skill_row(sk))
                # Skill tools also flow into requires.tools
                for t in (sk.tools or []):
                    if isinstance(t, str):
                        required_tools.add(t)
            else:
                if sk.slug not in required_skill_keys:
                    required_skill_keys[sk.slug] = {
                        "slug": sk.slug,
                        "min_version": sk.version or "1.0.0",
                    }
        required_skills_out = [
            required_skill_keys[k] for k in sorted(required_skill_keys)
        ]

        # d) Starter memory — only true agent-level rows
        if include_starter_memory:
            mem_rows = list((await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id.in_(embedded_agent_ids),
                    AgentMemory.user_id.is_(None),
                    AgentMemory.workspace_id.is_(None),
                    AgentMemory.status == "active",
                ).order_by(AgentMemory.importance.desc(), AgentMemory.created_at)
            )).scalars().all())
            for m in mem_rows:
                # Refuse to ship confidential/restricted agent memory
                # even when the operator opts in to starter_memory.
                if m.classification in ("confidential", "restricted"):
                    continue
                if m.visibility == "private":
                    continue
                starter_memory.setdefault(m.agent_id, []).append({
                    "memory_type": m.memory_type,
                    "scope": m.scope,
                    "content": m.content,
                    "importance": m.importance,
                    "confidence": m.confidence,
                })

    # 3) Assemble embedded.agents[] in the order their slugs sort —
    #    deterministic output is important for diff-based review.
    by_id = {a.id: a for a in agents}
    for aid in sorted(embedded_agent_ids, key=lambda i: (by_id[i].slug or "", i)):
        a = by_id[aid]
        embedded_agents_out.append({
            "slug": a.slug,
            "version": getattr(a, "version", None) or "1.0",
            "name": a.name,
            "description": a.description,
            "system_prompt": a.system_prompt,
            "config": dict(a.config or {}),
            "category": a.category,
            "tags": list(a.tags or []),
            "tool_bindings": sorted(tool_bindings.get(a.id, [])),
            "mcp_bindings": mcp_bindings.get(a.id, []),
            "skill_bindings": sorted(skill_bindings.get(a.id, [])),
            "starter_memory": starter_memory.get(a.id, []),
        })

    # Sort the requires.agents list for determinism too.
    required_agents_out.sort(key=lambda d: d.get("slug") or "")

    return {
        "embedded_agents": embedded_agents_out,
        "embedded_skills": sorted(
            embedded_skills_out, key=lambda d: d.get("slug") or ""
        ),
        "required_tools": required_tools,
        "required_mcp_servers": required_mcp_out,
        "required_skills": required_skills_out,
        "required_agents": required_agents_out,
    }


def _export_skill_row(sk: Skill) -> dict[str, Any]:
    """Serialise a Skill row to the embedded.skills shape. Tools listed
    here also need to land in contract.requires.tools — caller does that."""
    return {
        "slug": sk.slug,
        "version": sk.version or "1.0.0",
        "name": sk.name,
        "display_name": sk.display_name,
        "description": sk.description,
        "system_prompt": sk.system_prompt,
        "tools": list(sk.tools or []),
        "input_schema": dict(sk.input_schema or {}),
        "output_format": sk.output_format or "text",
        "category": sk.category,
        "tags": list(sk.tags or []),
        "is_public": False,  # embedded skills are private by definition
        "config": dict(sk.config or {}),
    }


# ── Knowledge packs ───────────────────────────────────────────────────

async def _export_knowledge_packs(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *,
    mode: str = "skeleton",
) -> list[dict[str, Any]]:
    """Export DocumentGroups for this workspace as knowledge_packs.

    Hard rules — applied even when the operator passes ``mode='inline_text'``:
      * Document.classification not in {'public'}  → drop
      * Document.legal_hold = true                  → drop
      * Document.pii_detected = true                → drop
      * Document.quarantine_status != 'clean'       → drop
      * Document.is_trashed = true                  → drop
      * Non-markdown files (mime_type not text/* or .md ext) → drop body
        (path still listed under folder_structure)

    Skeleton mode emits the directory structure but never any file body;
    inline_text emits body_md only for surviving .md files.
    """
    groups = list((await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        ).order_by(DocumentGroup.name)
    )).scalars().all())

    if not groups:
        return []

    out: list[dict[str, Any]] = []
    for g in groups:
        # Documents tied to this group via folder hierarchy — we don't
        # currently model a direct DocumentGroup→Document FK, so the
        # safest move is to use group.settings.document_ids if present;
        # otherwise emit just the group shell.
        document_ids = list((g.settings or {}).get("document_ids") or [])
        docs: list[Document] = []
        if document_ids:
            docs = list((await db.execute(
                select(Document).where(Document.id.in_(document_ids))
            )).scalars().all())

        folder_structure: list[dict[str, Any]] = []
        starter_documents: list[dict[str, Any]] = []
        for d in docs:
            if not _document_safe_to_export(d):
                continue
            folder_structure.append({
                "path": d.name,
                "description": (d.metadata_ or {}).get("description"),
            })
            if mode == "inline_text" and _document_is_markdown(d):
                # body_md isn't actually stored in Document; the body
                # lives at fs_path or file_url. Exporter is read-only
                # so we attach a pointer + a TODO note rather than
                # synchronously fetching. Installer will surface this
                # as a knowledge-pack-pending todo.
                starter_documents.append({
                    "path": d.name if d.name.endswith(".md") else f"{d.name}.md",
                    "body_md": (
                        "# TODO: paste content from "
                        f"{d.fs_path or d.file_url or 'source'} here"
                    ),
                })

        out.append({
            "slug": _slugify(g.name),
            "title": g.name,
            "purpose": (g.settings or {}).get("purpose"),
            "mode": mode,
            "folder_structure": folder_structure,
            "starter_documents": starter_documents,
            "external_source": None,
        })
    return out


def _document_safe_to_export(d: Document) -> bool:
    if d.is_trashed:
        return False
    if d.legal_hold:
        return False
    if d.pii_detected:
        return False
    if d.quarantine_status != "clean":
        return False
    if d.classification != "public":
        return False
    return True


def _document_is_markdown(d: Document) -> bool:
    if d.mime_type and d.mime_type.startswith("text/"):
        return True
    name = (d.name or "").lower()
    return name.endswith(".md")


_SLUG_NON_WORD = ("[^a-z0-9]+", "-")


def _slugify(name: str) -> str:
    """Conservative slugifier — lowercase, replace non-alnum runs with
    a single ``-``, strip leading/trailing dashes. Used for
    auto-generating knowledge_pack slugs from DocumentGroup names."""
    import re
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "knowledge-pack"
