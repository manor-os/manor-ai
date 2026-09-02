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
  workflows              workspace bindings + portable runtime graph
  custom_fields          definitions
  governance_policy      current revision's policy dict
  channel_requirements   account type / provider / purpose / service key,
                         including shared accounts bound to the Workspace
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

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.payload import (
    BLUEPRINT_VERSION,
    LOCAL_RUNTIME_REFERENCE_KEYS,
    _is_secret_shape,
    validate_payload,
)
from packages.core.blueprints.simulation import generate_simulation_experience
from packages.core.blueprints.workflow_dependencies import (
    WorkflowDependencyError,
    WorkflowDependencyFactory,
)
from packages.core.constants.goals import GoalStatus
from packages.core.constants.blueprints import (
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES,
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS,
    BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES,
    BLUEPRINT_KNOWLEDGE_LIST_PAGE_SIZE,
    BLUEPRINT_KNOWLEDGE_LIST_MAX_PAGE_SIZE,
    is_runtime_derived_scheduled_job,
)
from packages.core.governance.policy import policy_to_dict
from packages.core.governance.service import get_policy
from packages.core.goals.numbers import goal_number_to_json
from packages.core.models.channel import ChannelConfig
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.document import Channel, Document, DocumentGroup, DocumentGroupMember
from packages.core.models.goal import Goal
from packages.core.models.workspace_stat import WorkspaceStat
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowTemplateInstallation,
)
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)
from packages.core.services.sensitive_data import sanitize_sensitive_payload
from packages.core.services.workflow_run_trace import visible_workflow_tags

logger = logging.getLogger(__name__)

async def _installed_marketplace_links_by_local_id(
    db: AsyncSession,
    *,
    entity_id: str,
    local_resource_type: str,
    local_resource_ids: set[str] | list[str],
) -> dict[str, MarketplaceResourceLink]:
    """Load authoritative install provenance for local runtime resources."""
    ids = {str(resource_id) for resource_id in local_resource_ids if resource_id}
    if not ids:
        return {}
    rows = list((await db.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.relationship == "installed_from",
            MarketplaceResourceLink.local_resource_type == local_resource_type,
            MarketplaceResourceLink.local_resource_id.in_(ids),
        )
    )).scalars().all())
    return {row.local_resource_id: row for row in rows}


def _portable_slug(value: Any, *, fallback: str, row_id: str) -> str:
    """Build a stable payload-only slug for legacy rows that lack one."""
    base = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    base = (base or fallback)[:72].rstrip("-")
    return f"{base}-{str(row_id)[-8:].lower()}"


def _portable_agent_slug(agent: Agent) -> str:
    return str(agent.slug or "").strip() or _portable_slug(
        agent.name,
        fallback="workspace-agent",
        row_id=agent.id,
    )


def _portable_skill_slug(skill: Skill) -> str:
    return str(skill.slug or "").strip() or _portable_slug(
        skill.name,
        fallback="workspace-skill",
        row_id=skill.id,
    )


@dataclass(frozen=True)
class _PortableSkillIdentity:
    slug: str
    embedded: bool
    marketplace_id: str | None
    marketplace_source: str | None


def _portable_skill_identity(
    skill: Skill,
    *,
    entity_id: str,
    source_link: MarketplaceResourceLink | None,
    include_skills: bool,
) -> _PortableSkillIdentity:
    """Classify one Skill once for bindings and scheduled targets."""

    config = dict(skill.config or {})
    source_skill_id = str(
        source_link.marketplace_resource_id
        if source_link is not None
        else config.get("marketplace_id")
        or config.get("source_skill_id")
        or (skill.id if skill.entity_id is None else "")
    ).strip()
    marketplace_source = (
        source_link.marketplace_source
        if source_link is not None
        else "manor"
        if config.get("marketplace_id")
        else "platform"
        if source_skill_id
        else None
    )
    embedded = bool(
        skill.entity_id == entity_id
        and not skill.is_public
        and not source_skill_id
        and include_skills
    )
    return _PortableSkillIdentity(
        slug=(
            _portable_skill_slug(skill)
            if skill.slug or embedded or source_skill_id else ""
        ),
        embedded=embedded,
        marketplace_id=source_skill_id or None,
        marketplace_source=marketplace_source,
    )


class ExportError(Exception):
    """Raised when a workspace can't be exported as a blueprint."""


# Settings keys that are runtime-only (not config) — dropped on export.
_RUNTIME_SETTINGS_KEYS = frozenset({
    "sandbox",          # gets re-set by installer based on mode
    "_blueprint",       # blueprint provenance metadata
    "simulation_experience",  # regenerated into recipe on export
    "last_briefing_at", # runtime cursor
    "created_by_user_id",
    "provisioning",
    "ledger_matching",  # creation-time matching provenance, not portable config
})

# Workspace.settings is an open runtime JSON object and may contain credentials,
# billing state, local row IDs, or feature caches. Only registered product
# configuration and keys declared by the Blueprint that installed this
# Workspace may cross the portability boundary.
_PORTABLE_WORKSPACE_SETTING_KEYS = frozenset({
    "access_mode",
    "audio_defaults",
    "blocking_setup",
    "blueprint_frozen",
    "external_publishing",
    "frozen_at",
    "install_expectation",
    "ledger_contracts",
    "publication_setup",
    "stickman_studio_profile",
    "stickman_topic_policy",
    "stickman_visual_continuity_profile",
    "timezone",
    "topic_ledger",
    "workspace_shared_assets",
})

_NONPORTABLE_REFERENCE_KEYS = LOCAL_RUNTIME_REFERENCE_KEYS | frozenset({
    "minio_dir",
    "source_blueprint_id",
    "source_blueprint_component_key",
})
_BLUEPRINT_SECRET_SCAN_MAX_DEPTH = 64


def _without_nonportable_references(value: Any) -> Any:
    """Drop source-row references while preserving logical external config."""
    if isinstance(value, dict):
        return {
            key: _without_nonportable_references(item)
            for key, item in value.items()
            if (
                key not in _NONPORTABLE_REFERENCE_KEYS
                and not _is_secret_shape(str(key))
            )
        }
    if isinstance(value, list):
        return [_without_nonportable_references(item) for item in value]
    return value


def _portable_workspace_settings(settings: Any) -> dict[str, Any]:
    """Project Workspace settings through the same recursive safety boundary."""
    if not isinstance(settings, dict):
        return {}
    blueprint_record = settings.get("_blueprint")
    declared_keys = (
        blueprint_record.get("portable_setting_keys")
        if isinstance(blueprint_record, dict)
        else []
    )
    allowed_keys = _PORTABLE_WORKSPACE_SETTING_KEYS | {
        str(key)
        for key in declared_keys or []
        if isinstance(key, str) and key not in _RUNTIME_SETTINGS_KEYS
    }
    selected = {
        key: value
        for key, value in settings.items()
        if key in allowed_keys
    }
    if sanitize_sensitive_payload(selected) != selected:
        raise ExportError(
            "Workspace settings selected for Blueprint export contain credentials; "
            "remove them or move them to the integration setup flow"
        )
    return _without_nonportable_references(selected)


def _assert_no_blueprint_credentials(payload: dict[str, Any]) -> None:
    """Fail closed when any portable field still contains credential material."""
    if (
        sanitize_sensitive_payload(
            payload,
            max_depth=_BLUEPRINT_SECRET_SCAN_MAX_DEPTH,
        )
        != payload
    ):
        raise ExportError(
            "Blueprint export fields selected for publication contain credentials; "
            "remove them or move them to the integration setup flow"
        )


# ── Public API ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _ExportedWorkflows:
    """Portable Flow payloads plus the local-to-portable identity registry."""

    payloads: list[dict[str, Any]]
    key_by_workflow_id: dict[str, str]
    key_by_binding_id: dict[str, str]


@dataclass
class ExportContext:
    """Knobs the operator can pass to control what's included."""

    include_subscriptions: bool = True
    include_goals: bool = True
    include_stats: bool = True
    include_scheduled_jobs: bool = True
    include_workflows: bool = True
    include_custom_fields: bool = True
    include_governance: bool = True
    include_channel_requirements: bool = True
    include_session_requirements: bool = True
    # Backward-compatible alias used by the original API/UI. When true and no
    # explicit knowledge_pack_mode is supplied, safe inline Markdown is used.
    include_memory_files: bool = False
    # v1.1 embedded sections. Default-on so a fresh export carries the
    # workspace-private agents/skills the operator built; turn off for a
    # "config-only" export (e.g. for review before publishing).
    include_embedded_agents: bool = True
    include_embedded_skills: bool = True
    include_knowledge_packs: bool = True
    # Knowledge_pack body inclusion is opt-in even when the section is
    # exported — default ``skeleton`` mode emits folder structure only.
    knowledge_pack_mode: Optional[str] = None  # 'skeleton' | 'inline_text'
    # Request-only source Document identities used to select exact starter
    # bodies. These local ids are validated against the Workspace and never
    # enter the portable payload. ``None`` preserves the legacy all-eligible
    # inline export; an empty set deliberately exports only the skeleton.
    knowledge_document_ids: Optional[frozenset[str]] = None
    # Agent-level starter_memory inclusion. Off by default because
    # accumulated agent memories often encode author-private style hints
    # (voice, reaction patterns) that don't translate to other entities.
    include_starter_memory: bool = False
    # Creator-authored install-time personalization declarations. Installed
    # Workspace content is already materialized, so declarations are emitted
    # only when the caller deliberately supplies schema and matching tokens.
    install_variables: Optional[tuple[dict[str, Any], ...]] = None


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
        await _export_subscriptions(
            db,
            workspace.entity_id,
            workspace_id,
            allow_generated_private_agent_slugs=ctx.include_embedded_agents,
        )
        if ctx.include_subscriptions else []
    )
    goals = (
        await _export_goals(db, workspace.entity_id, workspace_id)
        if ctx.include_goals else []
    )
    stats = (
        await _export_stats(db, workspace.entity_id, workspace_id)
        if ctx.include_stats else []
    )
    exported_workflows = (
        await _export_workflows(db, workspace.entity_id, workspace_id)
        if ctx.include_workflows or ctx.include_scheduled_jobs
        else _ExportedWorkflows([], {}, {})
    )
    workflows = exported_workflows.payloads if ctx.include_workflows else []
    scheduled_jobs = (
        await _export_scheduled_jobs(
            db,
            workspace.entity_id,
            workspace_id,
            workflow_key_by_id=(
                exported_workflows.key_by_workflow_id
                if ctx.include_workflows else {}
            ),
            workflow_key_by_binding_id=(
                exported_workflows.key_by_binding_id
                if ctx.include_workflows else {}
            ),
        )
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
        await _export_session_requirements(
            db,
            workspace.entity_id,
            references=[
                workspace.operating_model,
                workspace.settings,
                subscriptions,
                goals,
                scheduled_jobs,
                workflows,
                channels,
            ],
        )
        if ctx.include_session_requirements else []
    )

    # ── v1.1 embedded + contract.requires assembly ────────────────────
    embedded_agents: list[dict[str, Any]] = []
    embedded_skills: list[dict[str, Any]] = []
    required_tools: set[str] = set()
    required_mcp_servers: list[dict[str, Any]] = []
    required_skills: list[dict[str, Any]] = []
    required_agents: list[dict[str, Any]] = []

    if (
        ctx.include_embedded_agents
        or ctx.include_embedded_skills
        or any(job.get("execution_type") == "skill" for job in scheduled_jobs)
    ):
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

    knowledge_pack_mode = (
        ctx.knowledge_pack_mode
        or ("inline_text" if ctx.include_memory_files else "skeleton")
    )
    if knowledge_pack_mode not in {"skeleton", "inline_text"}:
        raise ExportError(
            "knowledge_pack_mode must be 'skeleton' or 'inline_text'"
        )
    if ctx.knowledge_document_ids:
        if not ctx.include_knowledge_packs:
            raise ExportError(
                "knowledge_document_ids requires include_knowledge_packs"
            )
        if knowledge_pack_mode != "inline_text":
            raise ExportError(
                "knowledge_document_ids requires knowledge_pack_mode='inline_text'"
            )
    elif ctx.knowledge_document_ids is not None:
        knowledge_pack_mode = "skeleton"
    knowledge_packs = (
        await _export_knowledge_packs(
            db,
            workspace.entity_id,
            workspace_id,
            mode=knowledge_pack_mode,
            selected_document_ids=ctx.knowledge_document_ids,
        )
        if ctx.include_knowledge_packs else []
    )

    # Workspace shell — kind / context / primary_work / settings absorb
    # into operating_model so the v1.1 recipe has one place to read from.
    om: dict[str, Any] = _without_nonportable_references(
        dict(workspace.operating_model or {})
    )
    # recipe.subscriptions is the canonical portable replacement for source
    # agent mappings. recipe.goals is the canonical portable Goal declaration.
    # Knowledge packs replace source DocumentGroup ids.
    om.pop("agent_mappings", None)
    om.pop("goals", None)
    knowledge = om.get("knowledge")
    if isinstance(knowledge, dict):
        knowledge = dict(knowledge)
        knowledge.pop("default_group_ids", None)
        knowledge.pop("group_purposes", None)
        om["knowledge"] = knowledge
    # The dedicated Workspace columns are the live runtime source of truth.
    # ``operating_model`` can retain an older copy after a direct Workspace
    # edit; allowing that stale copy to win makes export/install roll back the
    # user's current framing.
    if workspace.operating_context:
        om["context"] = workspace.operating_context
    else:
        om.pop("context", None)
    if workspace.primary_work:
        om["primary_work"] = workspace.primary_work
    else:
        om.pop("primary_work", None)
    if workspace.kind:
        om["kind"] = workspace.kind
    else:
        om.pop("kind", None)
    om["heartbeat_enabled"] = bool(workspace.heartbeat_enabled)
    if workspace.heartbeat_cadence:
        om["heartbeat_cadence"] = workspace.heartbeat_cadence
    ws_settings = _portable_workspace_settings(workspace.settings)
    if ws_settings:
        om["settings"] = ws_settings
    else:
        om.pop("settings", None)
    blueprint_prompts = list(om.pop("blueprint_prompts", []) or [])
    blueprint_governance_rules = list(om.pop("blueprint_governance_rules", []) or [])
    strategist = None
    stored_strategist = om.pop("strategist", None)
    if isinstance(stored_strategist, dict) and stored_strategist:
        strategist = {
            key: _without_nonportable_references(stored_strategist[key])
            for key in (
                "business_model",
                "proposal_shape",
                "priors",
                "evaluation_rubric",
                "do_not_propose",
                "voice",
                "system_prompt_override",
                "use_goals",
            )
            if key in stored_strategist
        }
        cadence = stored_strategist.get("cadence")
        trigger_conditions = stored_strategist.get("trigger_conditions")
        if cadence is not None or trigger_conditions is not None:
            strategist["cadence"] = {
                "schedule": cadence,
                "trigger_conditions": _without_nonportable_references(
                    trigger_conditions
                ),
            }

    install_variables = (
        [dict(item) for item in ctx.install_variables]
        if ctx.install_variables is not None
        else []
    )

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
            "variables": install_variables,
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
            "strategist": strategist,
            "prompts": blueprint_prompts,
            "subscriptions": subscriptions,
            "scheduled_jobs": scheduled_jobs,
            "workflows": workflows,
            "stats": stats,
            "goals": goals,
            "task_categories": [],
            "custom_fields": custom_fields,
            "sla_policies": [],
            "escalation_rules": blueprint_governance_rules,
            # Filled below from the complete portable payload so every new
            # Blueprint is immediately demonstrable after Workspace simulation install.
            "simulation_experience": None,
        },
        "policy": {
            "governance": governance,
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }

    payload["recipe"]["simulation_experience"] = generate_simulation_experience(payload)

    # The payload contains multiple free-form portable fields. Scan the final
    # assembled document so newly added exporters cannot bypass the boundary.
    _assert_no_blueprint_credentials(payload)

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
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *,
    allow_generated_private_agent_slugs: bool,
) -> list[dict[str, Any]]:
    """Resolve agent_id to a portable Agent slug for import."""
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
    source_links = await _installed_marketplace_links_by_local_id(
        db,
        entity_id=entity_id,
        local_resource_type="agent",
        local_resource_ids=agent_ids,
    )

    out: list[dict[str, Any]] = []
    for r in rows:
        a = agent_by_id.get(r.agent_id)
        source_link = source_links.get(r.agent_id)
        can_embed = bool(
            a is not None
            and a.entity_id == entity_id
            and not a.is_public
            and source_link is None
            and not str((a.config or {}).get("source_agent_id") or "").strip()
            and allow_generated_private_agent_slugs
        )
        source_agent_id = str(
            (
                source_link.marketplace_resource_id
                if source_link is not None
                else (a.config or {}).get("source_agent_id")
                if a is not None and a.entity_id is not None
                else a.id if a is not None and a.entity_id is None else ""
            )
            or ""
        ).strip()
        agent_slug = (
            _portable_agent_slug(a)
            if a is not None and (a.slug or can_embed or source_agent_id)
            else ""
        )
        if not agent_slug:
            logger.warning(
                "blueprint export: dropping subscription %s — external agent %s has no exact id or portable slug",
                r.id, r.agent_id,
            )
            continue
        out.append({
            "service_key": r.service_key,
            "agent_slug": agent_slug,
            **(
                {"marketplace_agent_id": source_agent_id}
                if source_agent_id else {}
            ),
            "custom_prompt": r.custom_prompt,
            "config": _without_nonportable_references(dict(r.config or {})),
        })
    return out


async def _export_goals(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict[str, Any]]:
    rows = list((await db.execute(
        select(Goal).where(
            Goal.entity_id == entity_id,
            Goal.workspace_id == workspace_id,
            Goal.status == GoalStatus.ACTIVE.value,
        )
    )).scalars().all())
    stat_ids = {goal.stat_id for goal in rows if goal.stat_id}
    stats_by_id: dict[str, WorkspaceStat] = {}
    if stat_ids:
        stats = list((await db.execute(select(WorkspaceStat).where(
            WorkspaceStat.entity_id == entity_id,
            WorkspaceStat.workspace_id == workspace_id,
            WorkspaceStat.id.in_(stat_ids),
        ))).scalars().all())
        stats_by_id = {stat.id: stat for stat in stats}
    return [
        {
            "goal_key": g.goal_key,
            "title": g.title,
            "description": g.description,
            "metric_key": g.metric_key,
            "stat_key": stats_by_id[g.stat_id].key if g.stat_id in stats_by_id else None,
            "target_value": goal_number_to_json(g.target_value),
            # baseline is config-ish (operator-set starting point) so
            # we keep it; current_value / pace are runtime — dropped.
            "baseline_value": goal_number_to_json(g.baseline_value),
            "deadline": g.deadline.isoformat() if g.deadline else None,
            "measurement_source": _without_nonportable_references(
                g.measurement_source
            ),
            "measurement_cadence": g.measurement_cadence,
            "priority": g.priority,
        }
        for g in rows
    ]

async def _export_stats(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict[str, Any]]:
    rows = list((await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == entity_id,
        WorkspaceStat.workspace_id == workspace_id,
        WorkspaceStat.status == "active",
    ))).scalars().all())
    return [
        {
            "library_key": stat.library_key,
            "key": stat.key,
            "name": stat.name,
            "description": stat.description,
            "value_type": stat.value_type,
            "unit": stat.unit,
            "window": stat.window,
            "collector_type": stat.collector_type,
            "collector_config": _without_nonportable_references(stat.collector_config),
            "collection_cadence": stat.collection_cadence,
            "freshness_limit_seconds": stat.freshness_limit_seconds,
            "goal_eligible": stat.goal_eligible,
        }
        for stat in rows
    ]


async def _export_scheduled_jobs(
    db: AsyncSession, entity_id: str, workspace_id: str,
    *,
    workflow_key_by_id: dict[str, str],
    workflow_key_by_binding_id: dict[str, str],
) -> list[dict[str, Any]]:
    rows = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
        )
    )).scalars().all())
    scheduled_skill_ids = {
        str((row.execution_target or {}).get("skill_id") or "").strip()
        for row in rows
        if row.execution_type == "skill"
        and str((row.execution_target or {}).get("skill_id") or "").strip()
    }
    scheduled_skills = list((await db.execute(
        select(Skill).where(
            Skill.id.in_(scheduled_skill_ids),
            Skill.status == "active",
            or_(
                Skill.entity_id == entity_id,
                and_(Skill.entity_id.is_(None), Skill.is_public.is_(True)),
            ),
        )
    )).scalars().all()) if scheduled_skill_ids else []
    scheduled_skill_by_id = {skill.id: skill for skill in scheduled_skills}
    scheduled_skill_links = await _installed_marketplace_links_by_local_id(
        db,
        entity_id=entity_id,
        local_resource_type="skill",
        local_resource_ids=scheduled_skill_ids,
    ) if rows and scheduled_skill_ids else {}
    subscriptions = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    service_keys_by_agent_id: dict[str, set[str]] = {}
    for subscription in subscriptions:
        if subscription.agent_id and subscription.service_key:
            service_keys_by_agent_id.setdefault(
                subscription.agent_id,
                set(),
            ).add(subscription.service_key)
    portable_workflow_keys = {
        *workflow_key_by_id.values(),
        *workflow_key_by_binding_id.values(),
    }
    out: list[dict[str, Any]] = []
    for j in rows:
        if is_runtime_derived_scheduled_job(
            job_id=j.job_id,
            execution_type=j.execution_type,
        ):
            continue
        raw_target = dict(j.execution_target or {})
        source_agent_id = raw_target.get("agent_id") or j.agent_id
        execution_target = _without_nonportable_references(raw_target)
        source_skill_id = str(raw_target.get("skill_id") or "").strip()
        if j.execution_type == "skill":
            if not source_skill_id:
                raise ExportError(
                    f"scheduled Skill job {j.job_id!r} has no portable target"
                )
            source_skill = scheduled_skill_by_id.get(source_skill_id)
            if source_skill is None:
                raise ExportError(
                    f"scheduled Skill job {j.job_id!r} has no portable target: "
                    "the referenced Skill is inactive, missing, or inaccessible"
                )
            identity = _portable_skill_identity(
                source_skill,
                entity_id=entity_id,
                source_link=scheduled_skill_links.get(source_skill_id),
                include_skills=True,
            )
            if not identity.slug or (
                not identity.embedded and not identity.marketplace_id
            ):
                raise ExportError(
                    f"scheduled Skill job {j.job_id!r} has no portable target"
                )
            execution_target["skill_component_key"] = identity.slug
            if identity.marketplace_id:
                execution_target["skill_marketplace_id"] = identity.marketplace_id
                execution_target["skill_marketplace_source"] = (
                    identity.marketplace_source or "platform"
                )
        if j.execution_type == "workflow":
            source_workflow_id = str(
                raw_target.get("workflow_id") or ""
            ).strip()
            source_binding_id = str(
                raw_target.get("binding_id") or ""
            ).strip()
            source_workflow_key = str(
                workflow_key_by_binding_id.get(source_binding_id)
                or workflow_key_by_id.get(source_workflow_id)
                or raw_target.get("workflow_slug")
                or ""
            ).strip()
            if (
                not source_workflow_key
                or source_workflow_key not in portable_workflow_keys
            ):
                raise ExportError(
                    f"scheduled Workflow job {j.job_id!r} has no portable target; "
                    "include its Flow in the Blueprint export"
                )
            execution_target["workflow_slug"] = source_workflow_key
        if not execution_target.get("service_key") and source_agent_id:
            service_keys = service_keys_by_agent_id.get(source_agent_id, set())
            if len(service_keys) == 1:
                execution_target["service_key"] = next(iter(service_keys))
            elif len(service_keys) > 1 and j.execution_type in {
                "agent",
                "agent_message",
            }:
                raise ExportError(
                    f"scheduled Agent job {j.job_id!r} has an ambiguous portable "
                    "target; set execution_target.service_key explicitly"
                )
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
            "execution_target": execution_target,
            "execution_script": j.execution_script,
            "default_delivery_mode": j.default_delivery_mode,
            "enabled": bool(j.enabled),
            "delete_after_run": bool(j.delete_after_run),
        })
    return out


def _portable_workflow_slug(
    workflow: WorkflowDefinition,
    binding: Optional[WorkflowBinding] = None,
) -> str:
    binding_config = dict(binding.config or {}) if binding is not None else {}
    configured = str(
        binding_config.get("workspace_blueprint_workflow_slug") or ""
    ).strip()
    if configured:
        return configured
    raw = str(workflow.name or "").strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,118}[a-z0-9]", raw):
        return raw
    return _portable_slug(
        raw,
        fallback="workspace-workflow",
        row_id=workflow.id,
    )


def _export_workflow_row(
    workflow: WorkflowDefinition,
    *,
    binding: Optional[WorkflowBinding] = None,
    slug: Optional[str] = None,
    workflow_key_by_id: dict[str, str],
    internal: bool = False,
) -> dict[str, Any]:
    """Serialize the canonical runtime graph into the workflow shape the
    v1.1 installer already accepts.

    Runtime graphs use ``type/config/next`` while hand-authored Blueprint
    graphs may use ``kind/depends_on``. The installer accepts both, so keeping
    the runtime representation avoids a lossy graph inversion.
    """
    definition_variables = dict(workflow.variables or {})
    binding_variables = dict(binding.variables or {}) if binding is not None else {}
    variables = {**definition_variables, **binding_variables}

    trigger_type = (
        binding.trigger_type if binding is not None else workflow.trigger_type
    ) or "manual"
    trigger_config = {
        **dict(workflow.trigger_config or {}),
        **(dict(binding.trigger_config or {}) if binding is not None else {}),
    }
    binding_config = dict(binding.config or {}) if binding is not None else {}
    proposal_authorization = binding_config.pop("proposal_authorization", None)
    for key in (
        "source",
        "source_template_id",
        "workspace_blueprint_workflow_slug",
    ):
        binding_config.pop(key, None)

    try:
        portable_steps = WorkflowDependencyFactory.to_portable(
            list(workflow.steps or []),
            workflow_key_by_id=workflow_key_by_id,
        )
    except WorkflowDependencyError as exc:
        raise ExportError(str(exc)) from exc

    payload: dict[str, Any] = {
        "slug": slug or _portable_workflow_slug(workflow, binding),
        "name": (
            binding.name
            if binding is not None and binding.name
            else workflow.name
        ),
        "description": workflow.description,
        "trigger_type": trigger_type,
        "variables": [
            {"key": key, "default": _without_nonportable_references(value)}
            for key, value in sorted(variables.items())
        ],
        "steps": _without_nonportable_references(portable_steps),
        "category": workflow.category,
        "tags": visible_workflow_tags(workflow.tags),
        "version": int(workflow.version or 1),
        "internal": internal,
        "binding_config": _without_nonportable_references(binding_config),
        "enabled": bool(binding.enabled if binding is not None else workflow.is_active),
        "status": str(binding.status if binding is not None else workflow.status),
        "definition_enabled": bool(workflow.is_active),
        "definition_status": str(workflow.status),
    }
    portable_trigger_config = _without_nonportable_references(trigger_config)
    if portable_trigger_config:
        payload["trigger_config"] = portable_trigger_config
    if isinstance(proposal_authorization, dict):
        payload["proposal_authorization"] = _without_nonportable_references(
            proposal_authorization
        )
    trigger_ref = trigger_config.get("trigger_ref")
    if trigger_ref:
        payload["trigger_ref"] = _without_nonportable_references(trigger_ref)

    # Preserve the portable input declaration kept on installed trigger nodes.
    for step in workflow.steps or []:
        if not isinstance(step, dict) or step.get("type") != "trigger":
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        run_inputs = config.get("run_inputs")
        if isinstance(run_inputs, list):
            payload["run_inputs"] = _without_nonportable_references(run_inputs)
        break
    return payload


async def _export_workflows(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
) -> _ExportedWorkflows:
    """Export workflow deployments belonging to this workspace.

    A definition can be entity-scoped and deployed through a Workspace
    binding, or directly scoped to the Workspace. Both forms are included.
    Multiple bindings of the same definition are kept as distinct
    portable components so their trigger/config overrides survive.
    """
    bound_rows = list((await db.execute(
        select(WorkflowBinding, WorkflowDefinition)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowDefinition.entity_id == entity_id,
        )
        .order_by(WorkflowDefinition.name, WorkflowBinding.id)
    )).all())

    scoped = list((await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.workspace_id == workspace_id,
        ).order_by(WorkflowDefinition.name, WorkflowDefinition.id)
    )).scalars().all())
    workspace_workflow_ids = {
        workflow.id for _binding, workflow in bound_rows
    } | {workflow.id for workflow in scoped}
    installations = list((await db.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.workflow_id.in_(workspace_workflow_ids),
            WorkflowTemplateInstallation.source_type == "workspace_blueprint",
        ).order_by(WorkflowTemplateInstallation.created_at,
                   WorkflowTemplateInstallation.id)
    )).scalars().all()) if workspace_workflow_ids else []
    component_keys_by_workflow_id: dict[str, set[str]] = {}
    for installation in installations:
        component_key = str(installation.component_key or "").strip()
        if component_key:
            component_keys_by_workflow_id.setdefault(
                installation.workflow_id, set(),
            ).add(component_key)
    # A Blueprint installation key is the portable identity. Mutable
    # definition/binding names are only a fallback for manually-authored or
    # ambiguous legacy rows.
    stable_component_key_by_workflow_id = {
        workflow_id: next(iter(component_keys))
        for workflow_id, component_keys in component_keys_by_workflow_id.items()
        if len(component_keys) == 1
    }
    internal_workflow_ids = {
        installation.workflow_id
        for installation in installations
        if bool((installation.installation_metadata or {}).get("internal"))
    }

    export_rows: list[
        tuple[WorkflowDefinition, Optional[WorkflowBinding], str, bool]
    ] = []
    bound_definition_ids: set[str] = set()
    used_slugs: set[str] = set()
    for binding, workflow in bound_rows:
        bound_definition_ids.add(workflow.id)
        base_slug = stable_component_key_by_workflow_id.get(
            workflow.id,
            _portable_workflow_slug(workflow, binding),
        )
        slug = base_slug
        if slug in used_slugs:
            slug = f"{base_slug[:109].rstrip('-_')}-{binding.id[-8:].lower()}"
        used_slugs.add(slug)
        export_rows.append((workflow, binding, slug, False))

    for workflow in scoped:
        if workflow.id in bound_definition_ids:
            continue
        slug = stable_component_key_by_workflow_id.get(
            workflow.id,
            _portable_workflow_slug(workflow),
        )
        if slug in used_slugs:
            slug = f"{slug[:109].rstrip('-_')}-{workflow.id[-8:].lower()}"
        used_slugs.add(slug)
        export_rows.append((
            workflow,
            None,
            slug,
            workflow.id in internal_workflow_ids,
        ))

    workflow_key_by_id: dict[str, str] = {}
    workflow_key_by_binding_id: dict[str, str] = {}
    for workflow, binding, slug, _internal in export_rows:
        workflow_key_by_id.setdefault(workflow.id, slug)
        if binding is not None:
            workflow_key_by_binding_id[binding.id] = slug
    return _ExportedWorkflows(
        payloads=[
            _export_workflow_row(
                workflow,
                binding=binding,
                slug=slug,
                workflow_key_by_id=workflow_key_by_id,
                internal=internal,
            )
            for workflow, binding, slug, internal in export_rows
        ],
        key_by_workflow_id=workflow_key_by_id,
        key_by_binding_id=workflow_key_by_binding_id,
    )


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
    """Export local and explicitly bound accounts without account identities.

    The deployment's service key, not its local Agent/Subscription id, carries
    inbound routing across install and re-export.
    """
    subscriptions = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    by_id = {row.id: row for row in subscriptions}
    rows = (await db.execute(
        select(
            ChannelConfig.channel_type, ChannelConfig.provider, ChannelConfig.name,
            ChannelConfig.config["purpose"].astext,
            ChannelConfig.config["role"].astext,
            ChannelConfig.config["linked_service_key"].astext,
            Channel.config["purpose"].astext,
            Channel.config["role"].astext,
            Channel.config["linked_service_key"].astext,
            Channel.agent_subscription_id, Channel.agent_id,
        ).outerjoin(Channel, and_(
            Channel.entity_id == entity_id,
            Channel.workspace_id == workspace_id,
            Channel.type == ChannelConfig.channel_type,
            Channel.status == "active",
            Channel.config["channel_config_id"].astext == ChannelConfig.id,
        )).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.status == "active",
            or_(ChannelConfig.workspace_id == workspace_id, Channel.id.is_not(None)),
        ).order_by(ChannelConfig.created_at, ChannelConfig.id, Channel.id)
    )).all()
    requirements = []
    for channel_type, provider, name, purpose, role, service, bound_purpose, bound_role, bound_service, sub_id, agent_id in rows:
        subscription = by_id.get(sub_id)
        if sub_id and subscription is None:
            raise ExportError("Channel references an unavailable Workspace service")
        if not sub_id and agent_id:
            matches = [row for row in subscriptions if row.agent_id == agent_id]
            if len(matches) != 1:
                raise ExportError("Channel needs an explicit Workspace service before export")
            subscription = matches[0]
        service_key = subscription.service_key if subscription else bound_service or service
        requirements.append({
            "channel_type": channel_type,
            "provider": provider,
            "purpose": bound_purpose or purpose or name,
            "required": True,
            **({"role": bound_role or role} if bound_role or role else {}),
            **({"linked_service_key": service_key} if service_key else {}),
        })
    return requirements


_SESSION_REFERENCE_ID_KEYS = frozenset({
    "session_id",
    "browser_session_id",
    "integration_session_id",
})


def _collect_session_references(
    value: Any,
    *,
    ids: set[str],
    providers: set[str],
    labels_by_provider: dict[str, set[str]],
) -> None:
    if isinstance(value, dict):
        for key in _SESSION_REFERENCE_ID_KEYS:
            raw_id = value.get(key)
            if isinstance(raw_id, str) and raw_id.strip():
                ids.add(raw_id.strip())

        provider = str(
            value.get("provider")
            or value.get("session_provider")
            or ""
        ).strip().lower()
        label = str(
            value.get("session_label")
            or value.get("label")
            or ""
        ).strip().lower()
        if provider:
            providers.add(provider)
            if label:
                labels_by_provider.setdefault(provider, set()).add(label)

        for item in value.values():
            _collect_session_references(
                item,
                ids=ids,
                providers=providers,
                labels_by_provider=labels_by_provider,
            )
    elif isinstance(value, list):
        for item in value:
            _collect_session_references(
                item,
                ids=ids,
                providers=providers,
                labels_by_provider=labels_by_provider,
            )


async def _export_session_requirements(
    db: AsyncSession,
    entity_id: str,
    *,
    references: list[Any],
) -> list[dict[str, Any]]:
    """Export only entity sessions referenced by this workspace's config.

    IntegrationSession has no workspace_id. Matching every active entity row
    leaked unrelated Workspace requirements into the Blueprint, so references
    are resolved by explicit session id or provider/label declarations found
    in the Workspace's portable configuration.
    """
    referenced_ids: set[str] = set()
    referenced_providers: set[str] = set()
    labels_by_provider: dict[str, set[str]] = {}
    _collect_session_references(
        references,
        ids=referenced_ids,
        providers=referenced_providers,
        labels_by_provider=labels_by_provider,
    )
    if not referenced_ids and not referenced_providers:
        return []

    rows = list((await db.execute(
        select(IntegrationSession).where(
            IntegrationSession.entity_id == entity_id,
            IntegrationSession.status == "active",
        ).order_by(IntegrationSession.provider, IntegrationSession.label, IntegrationSession.id)
    )).scalars().all())
    out: list[dict[str, Any]] = []
    for s in rows:
        provider = str(s.provider or "").strip().lower()
        label = str(s.label or "").strip().lower()
        provider_labels = labels_by_provider.get(provider)
        referenced = s.id in referenced_ids or (
            provider in referenced_providers
            and (not provider_labels or label in provider_labels)
        )
        if not referenced:
            continue
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
    agent_source_links = await _installed_marketplace_links_by_local_id(
        db,
        entity_id=entity_id,
        local_resource_type="agent",
        local_resource_ids=agent_ids,
    )

    embedded_agents_out: list[dict[str, Any]] = []
    required_agents_out: list[dict[str, Any]] = []

    # Collect IDs across both buckets so binding queries can be batched.
    embedded_agent_ids: list[str] = []

    for a in agents:
        source_link = agent_source_links.get(a.id)
        source_agent_id = str(
            (
                source_link.marketplace_resource_id
                if source_link is not None
                else (a.config or {}).get("source_agent_id")
                if a.entity_id is not None
                else a.id
            )
            or ""
        ).strip()
        is_embedded = (
            a.entity_id == entity_id
            and not a.is_public
            and not source_agent_id
        )
        if is_embedded and include_agents:
            embedded_agent_ids.append(a.id)
        else:
            portable_slug = _portable_agent_slug(a) if source_agent_id or a.slug else ""
            if portable_slug:
                required_agents_out.append({
                    "slug": portable_slug,
                    "min_version": getattr(a, "version", None) or "1.0",
                    **(
                        {"marketplace_id": source_agent_id}
                        if source_agent_id else {}
                    ),
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
    skill_binding_refs: dict[str, list[dict[str, str]]] = {}
    starter_memory: dict[str, list[dict[str, Any]]] = {}

    required_tools: set[str] = set()
    required_mcp_out: list[dict[str, Any]] = []
    required_skills_out: list[dict[str, Any]] = []
    embedded_skills_out: list[dict[str, Any]] = []
    embedded_skill_slugs_seen: set[str] = set()
    required_skill_keys: dict[str, dict[str, Any]] = {}

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
            allowed_tools = (
                None if b.allowed_tools is None else list(b.allowed_tools)
            )
            # config_override may contain secrets — only export the KEY
            # NAMES (operators can inspect what would be set), and drop
            # any that look secret-shaped (api_token, *_secret, ...).
            safe_keys = sorted(
                k for k in (b.config_override or {}).keys()
                if isinstance(k, str) and not _is_secret_shape(k)
            )
            mcp_bindings.setdefault(b.agent_id, []).append({
                "server_slug": srv.server_key,
                "allowed_tools": allowed_tools,
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
        skill_source_links = await _installed_marketplace_links_by_local_id(
            db,
            entity_id=entity_id,
            local_resource_type="skill",
            local_resource_ids=skill_ids,
        )
        for b in sb_rows:
            sk = skill_by_id.get(b.skill_id)
            if not sk:
                logger.warning(
                    "blueprint export: skill_id %s missing — skipping binding",
                    b.skill_id,
                )
                continue
            source_link = skill_source_links.get(sk.id)
            source_skill_id = str(
                source_link.marketplace_resource_id
                if source_link is not None
                else (sk.config or {}).get("marketplace_id")
                or (sk.config or {}).get("source_skill_id")
                or (sk.id if sk.entity_id is None else "")
            ).strip()
            marketplace_source = (
                source_link.marketplace_source
                if source_link is not None
                else "manor"
                if (sk.config or {}).get("marketplace_id")
                else "platform"
                if source_skill_id
                else None
            )
            sk_is_embedded = (
                sk.entity_id == entity_id
                and not sk.is_public
                and not source_skill_id
                and include_skills
            )
            sk_slug = (
                _portable_skill_slug(sk)
                if sk.slug or sk_is_embedded or source_skill_id else ""
            )
            if not sk_slug:
                logger.warning(
                    "blueprint export: external skill_id %s has no portable slug — skipping binding",
                    b.skill_id,
                )
                continue
            skill_bindings.setdefault(b.agent_id, []).append(sk_slug)
            if sk_is_embedded:
                if sk_slug in embedded_skill_slugs_seen:
                    continue
                embedded_skill_slugs_seen.add(sk_slug)
                embedded_skills_out.append(_export_skill_row(sk, slug=sk_slug))
                # Skill tools also flow into requires.tools
                for t in (sk.tools or []):
                    if isinstance(t, str):
                        required_tools.add(t)
            else:
                requirement_key = (
                    f"{marketplace_source}:{source_skill_id}"
                    if source_skill_id else f"legacy-slug:{sk_slug}"
                )
                if requirement_key not in required_skill_keys:
                    required_skill_keys[requirement_key] = {
                        "slug": sk_slug,
                        "min_version": sk.version or "1.0.0",
                        **(
                            {
                                "marketplace_id": source_skill_id,
                                "marketplace_source": marketplace_source,
                            }
                            if source_skill_id else {}
                        ),
                    }
                if source_skill_id:
                    skill_binding_refs.setdefault(b.agent_id, []).append({
                        "slug": sk_slug,
                        "marketplace_id": source_skill_id,
                        "marketplace_source": marketplace_source or "platform",
                    })
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

    # A scheduled Skill is executable Blueprint content even when no embedded
    # Agent happens to bind it. Include/require it by the same portability
    # rules so recipe.scheduled_jobs can resolve its component key on install.
    scheduled_jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
            ScheduledJob.execution_type == "skill",
        )
    )).scalars().all())
    scheduled_skill_ids = sorted({
        str((job.execution_target or {}).get("skill_id") or "").strip()
        for job in scheduled_jobs
        if str((job.execution_target or {}).get("skill_id") or "").strip()
    })
    scheduled_skills = list((await db.execute(
        select(Skill).where(
            Skill.id.in_(scheduled_skill_ids),
            Skill.status == "active",
            or_(
                Skill.entity_id == entity_id,
                and_(Skill.entity_id.is_(None), Skill.is_public.is_(True)),
            ),
        )
    )).scalars().all()) if scheduled_skill_ids else []
    if {skill.id for skill in scheduled_skills} != set(scheduled_skill_ids):
        raise ExportError(
            "scheduled Skill changed or became inaccessible during export"
        )
    scheduled_links = await _installed_marketplace_links_by_local_id(
        db,
        entity_id=entity_id,
        local_resource_type="skill",
        local_resource_ids=scheduled_skill_ids,
    )
    for skill in scheduled_skills:
        identity = _portable_skill_identity(
            skill,
            entity_id=entity_id,
            source_link=scheduled_links.get(skill.id),
            # An executable scheduled target is a required dependency, not an
            # optional Agent catalog decoration.
            include_skills=True,
        )
        if not identity.slug or (
            not identity.embedded and not identity.marketplace_id
        ):
            raise ExportError(
                f"scheduled Skill {skill.id!r} has no portable target"
            )
        if identity.embedded:
            if identity.slug not in embedded_skill_slugs_seen:
                embedded_skill_slugs_seen.add(identity.slug)
                embedded_skills_out.append(
                    _export_skill_row(skill, slug=identity.slug)
                )
            required_tools.update(
                tool for tool in (skill.tools or []) if isinstance(tool, str)
            )
            continue
        requirement_key = (
            f"{identity.marketplace_source}:{identity.marketplace_id}"
            if identity.marketplace_id
            else f"legacy-slug:{identity.slug}"
        )
        required_skill_keys.setdefault(requirement_key, {
            "slug": identity.slug,
            "min_version": skill.version or "1.0.0",
            **(
                {
                    "marketplace_id": identity.marketplace_id,
                    "marketplace_source": identity.marketplace_source,
                }
                if identity.marketplace_id else {}
            ),
        })

    required_skills_out = [
        required_skill_keys[key] for key in sorted(required_skill_keys)
    ]

    # 3) Assemble embedded.agents[] in the order their slugs sort —
    #    deterministic output is important for diff-based review.
    by_id = {a.id: a for a in agents}
    for aid in sorted(embedded_agent_ids, key=lambda i: (_portable_agent_slug(by_id[i]), i)):
        a = by_id[aid]
        embedded_agents_out.append({
            "slug": _portable_agent_slug(a),
            "version": getattr(a, "version", None) or "1.0",
            "name": a.name,
            "description": a.description,
            "system_prompt": a.system_prompt,
            "config": _without_nonportable_references(dict(a.config or {})),
            "category": a.category,
            "tags": list(a.tags or []),
            "business_capabilities": sorted({
                str(capability).strip()
                for capability in (a.config or {}).get("business_capabilities", [])
                if str(capability or "").strip()
            }),
            "tool_bindings": sorted(tool_bindings.get(a.id, [])),
            "mcp_bindings": mcp_bindings.get(a.id, []),
            "skill_bindings": sorted(skill_bindings.get(a.id, [])),
            **(
                {
                    "skill_binding_refs": sorted(
                        skill_binding_refs.get(a.id, []),
                        key=lambda ref: (
                            ref.get("marketplace_source", ""),
                            ref.get("marketplace_id", ""),
                        ),
                    ),
                }
                if skill_binding_refs.get(a.id) else {}
            ),
            "starter_memory": starter_memory.get(a.id, []),
        })

    # Sort the requires.agents list for determinism too.
    required_agents_out.sort(
        key=lambda d: (d.get("marketplace_id") or "", d.get("slug") or "")
    )

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


def _export_skill_row(sk: Skill, *, slug: str | None = None) -> dict[str, Any]:
    """Serialise a Skill row to the embedded.skills shape. Tools listed
    here also need to land in contract.requires.tools — caller does that."""
    return {
        "slug": slug or sk.slug,
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
        "config": _without_nonportable_references(dict(sk.config or {})),
    }


# ── Knowledge packs ───────────────────────────────────────────────────

async def _export_knowledge_packs(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *,
    mode: str = "skeleton",
    selected_document_ids: Optional[frozenset[str]] = None,
) -> list[dict[str, Any]]:
    """Export DocumentGroups for this workspace as knowledge_packs.

    Hard rules — applied even when the operator passes ``mode='inline_text'``:
      * Document.classification not in {'public'}  → drop
      * Document.visibility = 'private'             → drop
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
        if selected_document_ids:
            raise ExportError(
                "Selected Knowledge documents are unavailable or ineligible "
                f"for Blueprint export: {', '.join(sorted(selected_document_ids))}"
            )
        return []

    requested_document_ids = (
        set(selected_document_ids)
        if selected_document_ids is not None
        else None
    )
    if mode == "inline_text":
        content_text = Document.metadata_["content_text"].astext
        preflight = (
            select(
                Document.id,
                Document.file_size,
                func.octet_length(content_text).label("content_bytes"),
            )
            .join(
                DocumentGroupMember,
                DocumentGroupMember.document_id == Document.id,
            )
            .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
            .where(
                Document.entity_id == entity_id,
                DocumentGroup.entity_id == entity_id,
                DocumentGroup.workspace_id == workspace_id,
                Document.is_trashed.is_(False),
                Document.visibility != "private",
                Document.pii_detected.is_(False),
                Document.quarantine_status == "clean",
                Document.classification == "public",
                or_(
                    Document.mime_type.like("text/%"),
                    func.lower(Document.name).like("%.md"),
                ),
            )
            .distinct()
        )
        if requested_document_ids is not None:
            preflight = preflight.where(Document.id.in_(requested_document_ids))
        preflight_rows = list((await db.execute(preflight)).all())
        if len(preflight_rows) > BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS:
            raise ExportError(
                "Blueprint Knowledge exceeds the maximum of "
                f"{BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS} starter documents"
            )
        known_total_bytes = 0
        for document_id, file_size, content_bytes in preflight_rows:
            known_size = content_bytes if content_bytes is not None else file_size
            if known_size is None:
                continue
            size = int(known_size)
            if size > BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES:
                raise ExportError(
                    f"Knowledge document {document_id!r} exceeds the "
                    f"{BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES}-byte Blueprint limit"
                )
            known_total_bytes += size
        if known_total_bytes > BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES:
            raise ExportError(
                "Blueprint Knowledge starter content exceeds "
                f"{BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES} total bytes"
            )

    matched_document_ids: set[str] = set()
    starter_document_count = 0
    starter_document_bytes = 0
    out: list[dict[str, Any]] = []
    for g in groups:
        # Group membership is canonical. Older exports looked for a legacy
        # settings.document_ids list, which made normal Knowledge groups appear
        # empty even though DocumentGroupMember rows existed.
        document_query = (
            select(Document)
            .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
            .where(
                Document.entity_id == entity_id,
                DocumentGroupMember.group_id == g.id,
            )
            .order_by(Document.name, Document.id)
        )
        if mode == "inline_text" and requested_document_ids is not None:
            document_query = document_query.where(
                Document.id.in_(requested_document_ids)
            )
        docs = list((await db.execute(document_query)).scalars().all())

        group_settings = g.settings if isinstance(g.settings, dict) else {}
        folder_structure = [
            _without_nonportable_references(dict(item))
            for item in group_settings.get("folder_structure") or []
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        ]
        folder_paths = {
            str(item.get("path") or "").strip()
            for item in folder_structure
        }
        starter_documents: list[dict[str, Any]] = []
        for d in docs:
            if not _document_safe_to_export(d):
                continue
            metadata = d.metadata_ if isinstance(d.metadata_, dict) else {}
            path = _document_blueprint_path(d)
            if path not in folder_paths:
                folder_structure.append({
                    "path": path,
                    "description": metadata.get("description"),
                })
                folder_paths.add(path)
            exported_document = _document_inline_starter(d)
            if (
                mode == "inline_text"
                and exported_document is not None
                and (
                    requested_document_ids is None
                    or d.id in requested_document_ids
                )
            ):
                document_bytes = len(exported_document["body_md"].encode("utf-8"))
                if document_bytes > BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES:
                    raise ExportError(
                        f"Knowledge document {d.id!r} exceeds the "
                        f"{BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES}-byte Blueprint limit"
                    )
                starter_document_count += 1
                starter_document_bytes += document_bytes
                if starter_document_count > BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS:
                    raise ExportError(
                        "Blueprint Knowledge exceeds the maximum of "
                        f"{BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS} starter documents"
                    )
                if starter_document_bytes > BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES:
                    raise ExportError(
                        "Blueprint Knowledge starter content exceeds "
                        f"{BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES} total bytes"
                    )
                starter_documents.append(exported_document)
                matched_document_ids.add(d.id)

        out.append({
            "slug": str(
                group_settings.get("installed_from_blueprint_slug")
                or _slugify(g.name)
            ),
            "title": g.name,
            "purpose": group_settings.get("purpose"),
            "mode": mode,
            "folder_structure": folder_structure,
            "starter_documents": starter_documents,
            "external_source": (
                _without_nonportable_references(
                    group_settings.get("external_source")
                )
                if group_settings.get("installed_from_blueprint_slug")
                else None
            ),
        })
    if requested_document_ids is not None:
        missing = sorted(requested_document_ids - matched_document_ids)
        if missing:
            raise ExportError(
                "Selected Knowledge documents are unavailable or ineligible "
                f"for Blueprint export: {', '.join(missing)}"
            )
    return out


async def list_exportable_knowledge_documents(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    limit: int = BLUEPRINT_KNOWLEDGE_LIST_PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return one bounded page of safe candidates, never document bodies."""
    if not 1 <= limit <= BLUEPRINT_KNOWLEDGE_LIST_MAX_PAGE_SIZE or offset < 0:
        raise ValueError("Invalid Blueprint Knowledge page")
    content_text = Document.metadata_["content_text"].astext
    blueprint_path = Document.metadata_["blueprint_starter_path"].astext
    path = func.coalesce(blueprint_path, Document.name)
    scoped_documents = (
        select(DocumentGroupMember.document_id)
        .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
        .where(DocumentGroup.entity_id == entity_id, DocumentGroup.workspace_id == workspace_id)
    )
    page = (
        select(
            Document.id,
            Document.name,
            path.label("blueprint_path"),
            func.octet_length(content_text).label("content_bytes"),
        ).where(
            Document.entity_id == entity_id,
            Document.id.in_(scoped_documents),
            Document.is_trashed.is_(False),
            Document.visibility != "private",
            Document.pii_detected.is_(False),
            Document.quarantine_status == "clean",
            Document.classification == "public",
            func.jsonb_typeof(Document.metadata_["content_text"]) == "string",
            content_text.op("~")(r"\S"),
            func.octet_length(content_text).between(1, BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES),
            or_(
                Document.mime_type.like("text/%"),
                func.lower(Document.name).like("%.md"),
            ),
        )
        .order_by(func.lower(path), Document.name, Document.id)
        .offset(offset).limit(limit).cte("blueprint_knowledge_page")
    )
    # Membership fanout is bounded too. Extra groups remain in the actual
    # Knowledge net; this picker shows a labelled preview, not a second ACL.
    max_groups = 8
    groups = (
        select(DocumentGroup.id.label("group_id"), DocumentGroup.name.label("group_name"))
        .join(DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id)
        .where(
            DocumentGroupMember.document_id == page.c.id,
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        ).order_by(DocumentGroup.name, DocumentGroup.id)
        .limit(max_groups + 1).lateral()
    )
    rows = (await db.execute(
        select(page, groups).join(groups, true())
        .order_by(func.lower(page.c.blueprint_path), page.c.name, page.c.id, groups.c.group_name, groups.c.group_id)
    )).all()

    candidates_by_id: dict[str, dict[str, Any]] = {}
    for document_id, name, path, content_bytes, group_id, group_name in rows:
        starter_size = int(content_bytes)
        normalized_path = str(path or name or "document.md").strip()
        if not normalized_path.lower().endswith(".md"):
            normalized_path = f"{normalized_path}.md"
        candidate = candidates_by_id.setdefault(document_id, {
            "id": document_id,
            "name": name,
            "path": normalized_path,
            "file_size": starter_size,
            "groups": [],
            "groups_truncated": False,
        })
        if len(candidate["groups"]) < max_groups:
            candidate["groups"].append({"id": group_id, "name": group_name})
        else:
            candidate["groups_truncated"] = True

    return list(candidates_by_id.values())


def _document_blueprint_path(d: Document) -> str:
    metadata = d.metadata_ if isinstance(d.metadata_, dict) else {}
    return str(
        metadata.get("blueprint_starter_path")
        or d.name
        or "document.md"
    ).strip()


def _document_inline_starter(d: Document) -> Optional[dict[str, Any]]:
    if not _document_safe_to_export(d) or not _document_is_markdown(d):
        return None
    metadata = d.metadata_ if isinstance(d.metadata_, dict) else {}
    body = metadata.get("content_text")
    if not isinstance(body, str) or not body.strip():
        return None
    path = _document_blueprint_path(d)
    exported_document: dict[str, Any] = {
        "key": _document_portable_key(d),
        "path": path if path.lower().endswith(".md") else f"{path}.md",
        "body_md": body,
    }
    template = metadata.get("blueprint_template")
    if isinstance(template, dict):
        exported_document["template"] = dict(template)
    return exported_document


def _document_portable_key(d: Document) -> str:
    """Return a stable opaque identity without exporting the source row id."""

    metadata = d.metadata_ if isinstance(d.metadata_, dict) else {}
    existing = str(metadata.get("blueprint_document_key") or "").strip()
    if existing:
        return existing
    stem = _slugify(_document_blueprint_path(d))[:80].rstrip("-")
    digest = hashlib.sha256(
        f"manor-blueprint-document:{d.id}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{stem or 'knowledge-document'}-{digest}"


def _document_safe_to_export(d: Document) -> bool:
    if d.is_trashed:
        return False
    if d.visibility == "private":
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
