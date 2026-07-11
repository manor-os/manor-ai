"""blueprint payload → new workspace.

Two install modes:

  ``simulate``  Sets ``settings.sandbox=true`` so the M3 sandbox
                machinery takes over — plans default to ``dry_run``,
                measurements are simulated, no real channel sends.
                The blueprint payload is preserved verbatim under
                ``settings._blueprint`` so ``promote_workspace`` later
                knows what to flip back to.

  ``live``      Real install. Operator is on the hook for cost +
                consequences immediately.

Both modes:

  * Create the workspace
  * Resolve agent_slug → Agent (skip subscriptions whose agent isn't
    available unless ``create_missing_agents=true``)
  * Apply governance policy (writes a revision row)
  * Create custom field definitions
  * Create goals (with their measurement schedules — same as
    ``goals.create_goal``'s install_schedule path)
  * Create scheduled jobs
  * **Don't** create channels or browser sessions — those need
    operator-side capture. Returns them as ``InstallTodo`` items.

Returns ``InstallResult`` so the caller can render a summary card
("workspace created, 3 goals, 2 jobs, 4 things you need to wire up
before this works for real").
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.payload import (
    PayloadError, migrate_payload, validate_payload,
)
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.models.base import generate_ulid
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.document import DocumentGroup
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
)
from packages.core.services.entity_service import create_workspace
from packages.core.services.workspace_access import (
    ensure_workspace_owner_membership,
    settings_with_default_workspace_access,
)

logger = logging.getLogger(__name__)


class InstallError(Exception):
    """Raised when a blueprint can't be installed (malformed payload,
    missing required agents, etc.)."""


class InstallMode(str, Enum):
    SIMULATE = "simulate"
    LIVE = "live"


@dataclass
class InstallTodo:
    """One unmet requirement the operator needs to address before the
    workspace is fully functional."""

    kind: str
    """'channel' | 'browser_session' | 'missing_agent' | 'note'"""
    detail: str
    """Operator-readable description of what to do."""
    payload: dict[str, Any] = field(default_factory=dict)
    """Machine-readable details — drives the UI's deep-link buttons."""
    blocking: bool = True
    """If False, the workspace can run without this — surfaces as a
    soft warning instead of a red flag."""


@dataclass
class InstallResult:
    workspace_id: str
    mode: InstallMode
    blueprint_id: Optional[str]
    blueprint_slug: Optional[str]
    goal_ids: list[str] = field(default_factory=list)
    subscription_ids: list[str] = field(default_factory=list)
    scheduled_job_ids: list[str] = field(default_factory=list)
    custom_field_ids: list[str] = field(default_factory=list)
    governance_applied: bool = False
    todos: list[InstallTodo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────────

async def install_blueprint(
    db: AsyncSession,
    *,
    entity_id: str,
    payload: dict[str, Any],
    mode: InstallMode = InstallMode.SIMULATE,
    workspace_name: Optional[str] = None,
    user_id: Optional[str] = None,
    blueprint_id: Optional[str] = None,
    blueprint_slug: Optional[str] = None,
    create_missing_agents: bool = False,
    governance_preset: str = "standard",
) -> InstallResult:
    """Materialise a blueprint payload as a new workspace. Caller commits.

    ``workspace_name`` overrides the blueprint's title for the workspace
    name — handy when the operator wants their own label.
    """
    try:
        validate_payload(payload)
        # Normalise to v1.1 shape — v1.0 payloads get auto-migrated here
        # so the rest of this function reads a single canonical layout.
        payload = migrate_payload(payload)
    except PayloadError as exc:
        raise InstallError(f"invalid blueprint payload: {exc}") from exc

    manifest = payload["manifest"]
    contract = payload["contract"]
    recipe = payload["recipe"]
    policy = payload["policy"]

    # operating_model absorbs the workspace shell fields (kind / context /
    # primary_work / settings) during migration. Extract them back out
    # before writing the JSONB column so the column stays clean.
    om_full = dict(recipe.get("operating_model") or {})
    ws_kind = manifest.get("kind") or om_full.pop("kind", None) or ""
    operating_context = om_full.pop("context", None) or ""
    primary_work = om_full.pop("primary_work", None) or ""
    ws_settings_seed = dict(om_full.pop("settings", None) or {})

    # Strategist template (recipe.strategist) goes into
    # operating_model.strategist. Split the nested ``cadence`` so legacy
    # readers (which expect a string at operating_model.strategist.cadence)
    # keep working — the structured trigger_conditions live as a peer.
    strategist_cfg = recipe.get("strategist")
    if isinstance(strategist_cfg, dict) and strategist_cfg:
        merged = dict(om_full.get("strategist") or {})
        cadence_obj = strategist_cfg.get("cadence")
        if isinstance(cadence_obj, dict):
            if cadence_obj.get("schedule"):
                merged["cadence"] = cadence_obj["schedule"]
            tc = cadence_obj.get("trigger_conditions")
            if tc is not None:
                merged["trigger_conditions"] = tc
        elif isinstance(cadence_obj, str):
            merged["cadence"] = cadence_obj
        for key in (
            "business_model", "proposal_shape", "priors",
            "evaluation_rubric", "do_not_propose", "voice",
            "system_prompt_override",
        ):
            if key in strategist_cfg:
                merged[key] = strategist_cfg[key]
        om_full["strategist"] = merged

    workspace_operating_model = om_full  # remaining keys (services, rules, strategist, ...)

    name = workspace_name or manifest.get("title") or "Untitled workspace"
    if mode == InstallMode.SIMULATE:
        # Distinguish in the workspace list — but we keep the underlying
        # `kind` so promote() can restore.
        name = f"[SIM] {name}"

    workspace = await create_workspace(
        db, entity_id,
        name=name,
        description=manifest.get("description") or "",
        kind=ws_kind,
        operating_context=operating_context,
        primary_work=primary_work,
    )

    # Carry operating_model + settings forward (create_workspace
    # leaves both empty by design).
    workspace.operating_model = workspace_operating_model
    settings = settings_with_default_workspace_access(ws_settings_seed)
    if user_id:
        settings.setdefault("created_by_user_id", user_id)
    if mode == InstallMode.SIMULATE:
        settings["sandbox"] = True
    else:
        settings.pop("sandbox", None)
    settings["_blueprint"] = {
        "blueprint_id": blueprint_id,
        "blueprint_slug": blueprint_slug,
        "title": manifest.get("title"),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "install_mode": mode.value,
        "original_kind": ws_kind,
        # Persist requirement lists so promote() can re-check them.
        # NOTE: promote.py still reads these top-level keys for back-compat.
        "channel_requirements": list(contract.get("channels") or []),
        "session_requirements": list(contract.get("sessions") or []),
    }
    workspace.settings = settings
    await db.flush()
    await ensure_workspace_owner_membership(
        db,
        entity_id=entity_id,
        workspace_id=workspace.id,
        user_id=user_id,
        added_by=user_id,
    )

    result = InstallResult(
        workspace_id=workspace.id,
        mode=mode,
        blueprint_id=blueprint_id,
        blueprint_slug=blueprint_slug,
    )

    # ── Final governance policy preview ──
    # Compute the post-preset policy BEFORE installing embedded agents so
    # we can reject blueprints that try to bind agents to actions the
    # operator's chosen preset would never allow. The actual policy row
    # is written later in this function (one source of truth, same
    # values).
    from packages.core.governance.presets import PRESETS, apply_preset
    if governance_preset not in PRESETS:
        raise InstallError(
            f"unknown governance_preset {governance_preset!r}; "
            f"valid: {sorted(PRESETS)}"
        )
    governance_section = policy.get("governance") or {}
    try:
        base_policy_preview = WorkspacePolicy(**{
            k: v for k, v in governance_section.items()
            if k in {
                "never_allow_actions", "hitl_required_actions",
                "auto_approve_actions", "max_risk_level",
                "budget_caps_per_kind",
            }
        })
    except TypeError as exc:
        raise InstallError(f"governance_policy malformed: {exc}")
    final_policy_preview = apply_preset(base_policy_preview, governance_preset)

    # ── Embedded skills ──
    # Skills first because embedded agents may bind to them.
    embedded = payload["embedded"]
    skill_id_by_slug: dict[str, str] = {}
    for sk in embedded.get("skills") or []:
        sk_id = await _install_embedded_skill(
            db, entity_id=entity_id, sk=sk,
        )
        if sk_id and sk.get("slug"):
            skill_id_by_slug[sk["slug"]] = sk_id

    # ── Embedded agents (with tool / MCP / skill bindings + starter_memory) ──
    for a in embedded.get("agents") or []:
        await _install_embedded_agent(
            db, entity_id=entity_id, a=a,
            skill_id_by_slug=skill_id_by_slug,
            final_policy=final_policy_preview,
            todos=result.todos,
        )

    # ── Knowledge packs ──
    for kp in embedded.get("knowledge_packs") or []:
        await _install_knowledge_pack(
            db, entity_id=entity_id, workspace_id=workspace.id, kp=kp,
            todos=result.todos,
        )

    # ── Subscriptions ──
    for sub in recipe.get("subscriptions") or []:
        sub_id, todo = await _install_subscription(
            db, entity_id=entity_id, workspace_id=workspace.id, sub=sub,
            create_missing=create_missing_agents,
        )
        if sub_id:
            result.subscription_ids.append(sub_id)
        if todo:
            result.todos.append(todo)

    # ── Custom fields ──
    for cf in recipe.get("custom_fields") or []:
        cfd_id = await _install_custom_field(
            db, entity_id=entity_id, workspace_id=workspace.id, cf=cf,
        )
        result.custom_field_ids.append(cfd_id)

    # ── Goals (with measurement schedule) ──
    for g in recipe.get("goals") or []:
        gid = await _install_goal(
            db, entity_id=entity_id, workspace_id=workspace.id, g=g, mode=mode,
        )
        result.goal_ids.append(gid)

    # ── Scheduled jobs ──
    for sj in recipe.get("scheduled_jobs") or []:
        sj_id = await _install_scheduled_job(
            db, entity_id=entity_id, workspace_id=workspace.id, sj=sj,
            user_id=user_id, mode=mode,
        )
        result.scheduled_job_ids.append(sj_id)

    # ── Workflows ──
    # WorkflowDefinition is entity-scoped (no workspace_id column), so
    # multiple workspaces in the same entity share workflow definitions
    # by slug. The installer is idempotent — (entity_id, name) reuse.
    for w in recipe.get("workflows") or []:
        await _install_workflow(db, entity_id=entity_id, w=w)

    # NOTE: v1.1-new sections that still aren't materialised:
    # recipe.task_categories, recipe.sla_policies, recipe.escalation_rules,
    # recipe.prompts, policy.expected_baseline. Accepted by validate_payload
    # but skipped here. Tracked in roadmap.

    # ── Governance policy ──
    # The post-preset policy was already computed at the top of this
    # function (so embedded agents could be validated against it). Reuse
    # those values here — there's only one source of truth.
    governance = governance_section
    try:
        base_policy = WorkspacePolicy(**{
            k: v for k, v in governance.items()
            if k in {
                "never_allow_actions", "hitl_required_actions",
                "auto_approve_actions", "max_risk_level",
                "never_allow_capabilities", "hitl_required_capabilities",
                "auto_approve_capabilities",
                "budget_caps_per_kind",
            }
        })
    except TypeError as exc:
        raise InstallError(f"governance_policy malformed: {exc}")
    final_policy = apply_preset(base_policy, governance_preset)

    # Always write a policy row so the audit chain shows the active
    # ruleset — even when the blueprint had no governance section but
    # the operator picked 'safe'.
    if (
        governance
        or governance_preset != "standard"
        or final_policy != base_policy
    ):
        await update_policy(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            policy=final_policy,
            changed_by=user_id,
            change_summary=(
                f"installed from blueprint "
                f"{blueprint_slug or blueprint_id or '<inline>'} "
                f"(preset={governance_preset})"
            ),
        )
        result.governance_applied = True

    # Stash the preset choice in the blueprint metadata so the
    # simulation report can later say "you ran on Safe".
    bp_meta = dict(workspace.settings.get("_blueprint") or {})
    bp_meta["governance_preset"] = governance_preset
    settings = dict(workspace.settings or {})
    settings["_blueprint"] = bp_meta
    workspace.settings = settings

    # ── Channel + session requirements → todo list ──
    for req in contract.get("channels") or []:
        result.todos.append(InstallTodo(
            kind="channel",
            detail=(
                f"Pair a {req.get('channel_type')} channel"
                + (f" for {req.get('purpose')}" if req.get("purpose") else "")
            ),
            payload=dict(req),
            blocking=bool(req.get("required", True)),
        ))

    for req in contract.get("sessions") or []:
        result.todos.append(InstallTodo(
            kind="browser_session",
            detail=(
                f"Capture a {req.get('provider')} browser session"
                + (f" labelled '{req.get('label')}'" if req.get("label") else "")
            ),
            payload=dict(req),
            blocking=bool(req.get("required", True)),
        ))

    # ── Post-install checks ──
    # Run each check inline; failures surface as blocking todos. The
    # blueprint says "you should be able to do X after install" — if X
    # doesn't work, the operator finds out NOW, not at first cron tick.
    for chk in policy.get("post_install_checks") or []:
        await _run_post_install_check(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            check=chk,
            todos=result.todos,
        )

    if mode == InstallMode.SIMULATE:
        result.notes.append(
            "Installed in SIMULATE mode — plans default to dry_run, "
            "measurements are simulated. Promote when ready."
        )
    else:
        result.notes.append(
            "Installed in LIVE mode — actions hit real systems. "
            "Pair the required channels / sessions before the first run."
        )

    return result


# ── Section installers ───────────────────────────────────────────────

async def _install_subscription(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
    sub: dict[str, Any], create_missing: bool,
) -> tuple[Optional[str], Optional[InstallTodo]]:
    slug = sub.get("agent_slug")
    if not slug:
        return None, InstallTodo(
            kind="missing_agent",
            detail=f"subscription {sub.get('service_key')!r} has no agent_slug",
            payload=dict(sub), blocking=True,
        )
    agent = (await db.execute(
        select(Agent).where(
            Agent.entity_id == entity_id,
            Agent.slug == slug,
            Agent.status == "active",
        )
    )).scalar_one_or_none()
    if agent is None:
        agent = (await db.execute(
            select(Agent).where(
                Agent.slug == slug,
                Agent.is_template.is_(True),
                Agent.status == "active",
            ).limit(1)
        )).scalar_one_or_none()
    if agent is None:
        if not create_missing:
            return None, InstallTodo(
                kind="missing_agent",
                detail=(
                    f"agent slug {slug!r} not installed locally — "
                    f"either install it or re-run with create_missing_agents=true "
                    f"to skip this subscription."
                ),
                payload={"agent_slug": slug, "service_key": sub.get("service_key")},
                blocking=True,
            )
        # Create a placeholder agent so the subscription has something
        # to bind to. The operator must edit the system prompt later.
        agent = Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            name=slug.replace("-", " ").title(),
            slug=slug,
            system_prompt="(placeholder — installed from blueprint, please edit)",
            is_template=False,
            status="draft",
        )
        db.add(agent)
        await db.flush()

    row = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent.id,
        workspace_id=workspace_id,
        service_key=sub.get("service_key"),
        custom_prompt=sub.get("custom_prompt"),
        config=dict(sub.get("config") or {}),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row.id, None


async def _install_custom_field(
    db: AsyncSession, *, entity_id: str, workspace_id: str, cf: dict[str, Any],
) -> str:
    row = CustomFieldDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=cf["name"],
        display_name=cf.get("display_name") or cf["name"],
        field_type=cf.get("field_type", "text"),
        target=cf.get("target", "task"),
        options=list(cf.get("options") or []),
        default_value=cf.get("default_value"),
        required=bool(cf.get("required", False)),
        sort_order=int(cf.get("sort_order", 0)),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row.id


async def _install_goal(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
    g: dict[str, Any], mode: InstallMode,
) -> str:
    """Delegate to ``goals.create_goal`` so the measurement schedule
    is installed via the same path as a manual create."""
    from packages.core.goals import create_goal

    deadline = None
    if g.get("deadline"):
        try:
            deadline = date.fromisoformat(str(g["deadline"]))
        except ValueError:
            logger.warning(
                "blueprint install: skipping unparseable deadline %r on goal %r",
                g.get("deadline"), g.get("title"),
            )

    measurement_source = g.get("measurement_source")
    if mode == InstallMode.SIMULATE and measurement_source:
        # Tag the source so the measurement service knows to simulate
        # (the existing sandbox path already honours this flag).
        measurement_source = {**measurement_source, "_simulate": True}

    goal = await create_goal(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title=g["title"],
        description=g.get("description"),
        metric_key=g["metric_key"],
        target_value=Decimal(str(g["target_value"])),
        baseline_value=(
            Decimal(str(g["baseline_value"]))
            if g.get("baseline_value") is not None else None
        ),
        deadline=deadline,
        measurement_source=measurement_source,
        measurement_cadence=g.get("measurement_cadence"),
        priority=int(g.get("priority", 3)),
    )
    return goal.id


async def _install_scheduled_job(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
    sj: dict[str, Any], user_id: Optional[str], mode: InstallMode,
) -> str:
    """Direct insert — the scheduler service has many bespoke create
    paths; we replicate the field set the blueprint exporter emitted."""
    # job_id is unique globally — scope it to this install so two
    # installs of the same blueprint don't collide.
    base_job_id = sj.get("job_id") or f"bp-{generate_ulid()[:8]}"
    job_id = f"{base_job_id}-{workspace_id[-8:]}"
    execution_target = dict(sj.get("execution_target") or {})
    execution_type = sj.get("execution_type") or "agent"
    if execution_type == "agent_message":
        execution_type = "agent"

    agent_id = sj.get("agent_id")
    service_key = execution_target.get("service_key")
    if not agent_id and service_key:
        sub = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.service_key == service_key,
                AgentSubscription.status == "active",
            ).limit(1)
        )).scalar_one_or_none()
        if sub is not None:
            agent_id = sub.agent_id

    row = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=sj.get("name"),
        job_type=sj.get("job_type", "cron"),
        schedule_kind=sj.get("schedule_kind"),
        cron_expr=sj.get("cron_expr"),
        every_seconds=sj.get("every_seconds"),
        run_at=sj.get("run_at"),
        timezone=sj.get("timezone", "UTC"),
        payload_message=sj.get("payload_message"),
        agent_id=agent_id,
        execution_type=execution_type,
        execution_target=execution_target,
        execution_script=sj.get("execution_script"),
        default_delivery_mode=sj.get("default_delivery_mode"),
        user_id=user_id,
        # In simulate mode, jobs still tick — but the underlying actions
        # respect settings.sandbox so they don't reach external systems.
        enabled=True,
    )
    db.add(row)
    await db.flush()
    return row.id


# ── Embedded skills / agents / knowledge packs ────────────────────────


async def _install_embedded_skill(
    db: AsyncSession, *, entity_id: str, sk: dict[str, Any],
) -> Optional[str]:
    """Create (or reuse) a Skill row from embedded.skills[].

    Idempotency: if (entity_id, slug) already exists for this entity,
    reuse the existing row. Tools listed by the skill are NOT validated
    here — they're declared in contract.requires.tools and validated by
    the exporter's invariant + the post-install check.
    """
    slug = sk.get("slug")
    if not slug:
        logger.warning("blueprint install: embedded skill missing slug, skipping")
        return None

    existing = (await db.execute(
        select(Skill).where(Skill.entity_id == entity_id, Skill.slug == slug)
    )).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "blueprint install: skill %r already exists in entity, reusing",
            slug,
        )
        return existing.id

    row = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name=sk.get("name") or slug,
        slug=slug,
        display_name=sk.get("display_name") or sk.get("name") or slug,
        description=sk.get("description"),
        system_prompt=sk.get("system_prompt") or "",
        tools=list(sk.get("tools") or []),
        input_schema=dict(sk.get("input_schema") or {}),
        output_format=sk.get("output_format") or "text",
        category=sk.get("category"),
        tags=list(sk.get("tags") or []),
        is_public=False,  # embedded skills are entity-private by definition
        version=sk.get("version") or "1.0.0",
        config=dict(sk.get("config") or {}),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row.id


async def _install_embedded_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    a: dict[str, Any],
    skill_id_by_slug: dict[str, str],
    final_policy: WorkspacePolicy,
    todos: list[InstallTodo],
) -> Optional[str]:
    """Create (or reuse) an Agent row plus tool / MCP / skill bindings
    and starter memory.

    Governance check: every tool in ``tool_bindings`` is matched against
    ``final_policy.never_allow_actions`` (after stripping the ``tool.``
    prefix). A hit raises ``InstallError`` — the blueprint asks for an
    action that the operator's preset would always block, so installing
    it would deliver a dead agent.

    Missing MCP servers don't fail the install; they surface as
    InstallTodo so the operator can pair them post-install.
    """
    slug = a.get("slug")
    if not slug:
        logger.warning("blueprint install: embedded agent missing slug, skipping")
        return None

    # Governance preview check
    _enforce_governance_against_agent(a, final_policy)

    existing = (await db.execute(
        select(Agent).where(Agent.entity_id == entity_id, Agent.slug == slug)
    )).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "blueprint install: agent %r already exists in entity, reusing",
            slug,
        )
        return existing.id

    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name=a.get("name") or slug,
        slug=slug,
        description=a.get("description"),
        system_prompt=a.get("system_prompt"),
        config=dict(a.get("config") or {}),
        is_template=False,
        is_public=False,  # embedded agents stay entity-private
        category=a.get("category"),
        tags=list(a.get("tags") or []),
        source="blueprint",
        status="active",
        version=a.get("version") or "1.0",
    )
    db.add(agent)
    await db.flush()

    # Tool bindings — fail-fast on missing ToolDefinition (the exporter's
    # invariant says requires.tools should cover everything embedded
    # agents bind, so a missing tool means the target entity hasn't
    # caught up to the same catalog version).
    for tool_name in a.get("tool_bindings") or []:
        td = (await db.execute(
            select(ToolDefinition).where(ToolDefinition.name == tool_name)
        )).scalar_one_or_none()
        if td is None:
            raise InstallError(
                f"embedded agent {slug!r}: tool {tool_name!r} not in this "
                f"deployment's ToolDefinition catalog. Add the tool first or "
                f"drop it from the blueprint."
            )
        db.add(AgentToolBinding(agent_id=agent.id, tool_id=td.id))

    # MCP bindings — missing server becomes a todo, not a failure.
    for binding in a.get("mcp_bindings") or []:
        if not isinstance(binding, dict):
            continue
        server_slug = binding.get("server_slug")
        if not server_slug:
            continue
        srv = (await db.execute(
            select(MCPServer).where(MCPServer.server_key == server_slug)
        )).scalar_one_or_none()
        if srv is None:
            todos.append(InstallTodo(
                kind="mcp_server",
                detail=(
                    f"Install the {server_slug!r} MCP server, then bind it to "
                    f"agent {slug!r}. The blueprint expects these fields to be "
                    f"set on the binding: "
                    f"{list(binding.get('config_override_allowlist') or []) or '(none)'}"
                ),
                payload={
                    "server_slug": server_slug,
                    "agent_slug": slug,
                    "allowed_tools": binding.get("allowed_tools"),
                    "config_override_allowlist": binding.get("config_override_allowlist"),
                },
                blocking=True,
            ))
            continue
        db.add(AgentMCPBinding(
            id=generate_ulid(),
            agent_id=agent.id,
            mcp_server_id=srv.id,
            allowed_tools=list(binding.get("allowed_tools") or []) or None,
            # config_override starts empty — the operator fills in the
            # allowlisted fields via UI (or the MCP setup flow).
            config_override={},
            status="active",
        ))

    # Skill bindings — embedded skills resolve via skill_id_by_slug
    # (created earlier in install_blueprint); external skills look up by
    # slug. If neither path resolves, surface a todo.
    for sk_slug in a.get("skill_bindings") or []:
        skill_id = skill_id_by_slug.get(sk_slug)
        if skill_id is None:
            # External (public) skill — look it up by slug.
            sk_row = (await db.execute(
                select(Skill).where(Skill.slug == sk_slug, Skill.is_public.is_(True))
            )).scalar_one_or_none()
            if sk_row is None:
                todos.append(InstallTodo(
                    kind="missing_skill",
                    detail=(
                        f"Skill {sk_slug!r} required by agent {slug!r} is not "
                        f"installed in this deployment. Install it, then bind "
                        f"to the agent manually."
                    ),
                    payload={"skill_slug": sk_slug, "agent_slug": slug},
                    blocking=True,
                ))
                continue
            skill_id = sk_row.id
        db.add(AgentSkillBinding(
            id=generate_ulid(),
            agent_id=agent.id,
            skill_id=skill_id,
            status="active",
        ))

    # Starter memory — agent-level (workspace_id NULL, user_id NULL).
    for m in a.get("starter_memory") or []:
        if not isinstance(m, dict) or "user_id" in m and m["user_id"] is not None:
            # validate_payload already rejected user_id, but be defensive.
            continue
        db.add(AgentMemory(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent.id,
            user_id=None,
            workspace_id=None,
            memory_type=m.get("memory_type") or "instruction",
            scope=m.get("scope"),
            content=m.get("content") or "",
            importance=int(m.get("importance") or 5),
            confidence=float(m.get("confidence") or 1.0),
            source="blueprint",
            metadata_={"installed_with_agent_slug": slug},
            status="active",
        ))

    await db.flush()
    return agent.id


def _enforce_governance_against_agent(
    a: dict[str, Any], final_policy: WorkspacePolicy,
) -> None:
    """Raise InstallError if any tool the agent binds would be hard-blocked
    by the post-preset governance policy. Tool names are matched by glob
    against ``never_allow_actions`` after stripping the ``tool.`` prefix
    (so ``tool.x.delete_account`` matches ``x.delete_*``)."""
    import fnmatch
    never = final_policy.never_allow_actions or []
    if not never:
        return
    for tool in a.get("tool_bindings") or []:
        if not isinstance(tool, str):
            continue
        action_form = tool[len("tool."):] if tool.startswith("tool.") else tool
        for pattern in never:
            if fnmatch.fnmatchcase(action_form, pattern) or fnmatch.fnmatchcase(tool, pattern):
                raise InstallError(
                    f"embedded agent {a.get('slug')!r} binds tool {tool!r} "
                    f"which is permanently blocked by governance policy "
                    f"pattern {pattern!r} under the chosen preset. Either "
                    f"drop the binding from the blueprint or pick a less "
                    f"restrictive preset."
                )


async def _install_knowledge_pack(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    kp: dict[str, Any],
    todos: list[InstallTodo],
) -> Optional[str]:
    """Create a DocumentGroup for a knowledge_pack. Document bodies (when
    the pack opts into ``inline_text`` mode) are NOT auto-materialised —
    the exporter is read-only and we don't have a file-storage write
    path inside the installer. Surface them as todos so the operator (or
    UI) can paste the bodies into actual document rows.
    """
    slug = kp.get("slug")
    title = kp.get("title") or slug
    if not title:
        return None

    # Idempotent: reuse existing group with the same (entity_id,
    # workspace_id, name) tuple.
    existing = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
            DocumentGroup.name == title,
        )
    )).scalar_one_or_none()
    if existing is not None:
        group_id = existing.id
    else:
        group = DocumentGroup(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            name=title,
            settings={
                "purpose": kp.get("purpose"),
                "folder_structure": list(kp.get("folder_structure") or []),
                "mode": kp.get("mode") or "skeleton",
                "external_source": kp.get("external_source"),
                "installed_from_blueprint_slug": slug,
            },
        )
        db.add(group)
        await db.flush()
        group_id = group.id

    # Inline_text mode → surface starter_documents as actionable todos
    # for the operator to paste contents into real Document rows.
    if kp.get("mode") == "inline_text":
        for d in kp.get("starter_documents") or []:
            if not isinstance(d, dict):
                continue
            todos.append(InstallTodo(
                kind="knowledge_pack_document",
                detail=(
                    f"Paste the body of {d.get('path')!r} into the "
                    f"{title!r} knowledge pack (workspace document)."
                ),
                payload={
                    "knowledge_pack_slug": slug,
                    "document_group_id": group_id,
                    "path": d.get("path"),
                    "body_md": d.get("body_md"),
                },
                blocking=False,
            ))

    return group_id


# ── Workflows ─────────────────────────────────────────────────────────


async def _install_workflow(
    db: AsyncSession, *, entity_id: str, w: dict[str, Any],
) -> Optional[str]:
    """Translate a blueprint workflow into a WorkflowDefinition row.

    Blueprint format uses ``kind``/``depends_on`` (backward dependency
    edges); the WorkflowDefinition model uses ``type``/``next`` (forward
    next-step edges). This function inverts the dependency graph so the
    runtime engine sees the format it expects.

    Variables in blueprint are an array of ``{key, default}`` objects;
    the model stores them as a single dict ``{key: default}``.

    Idempotent: (entity_id, name) reuse. ``name`` is set to the blueprint's
    ``slug`` so the portable handle survives.
    """
    slug = w.get("slug")
    if not slug:
        logger.warning("blueprint install: workflow missing slug, skipping")
        return None

    existing = (await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.name == slug,
        )
    )).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "blueprint install: workflow %r already exists in entity, reusing",
            slug,
        )
        return existing.id

    # Translate steps: kind → type, depends_on → next.
    bp_steps = list(w.get("steps") or [])
    next_map: dict[str, list[str]] = {}
    for s in bp_steps:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not sid:
            continue
        for dep in s.get("depends_on") or []:
            next_map.setdefault(dep, []).append(sid)

    runtime_steps: list[dict[str, Any]] = []
    for s in bp_steps:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sid = s["id"]
        # Pack everything besides id/kind/depends_on into the model's
        # ``config`` so the runtime engine has full access without us
        # imposing a fixed schema.
        config = {
            k: v for k, v in s.items()
            if k not in ("id", "kind", "depends_on", "name")
        }
        runtime_steps.append({
            "id": sid,
            "type": s.get("kind") or "agent",
            "name": s.get("name") or sid,
            "config": config,
            "next": next_map.get(sid, []),
        })

    # Convert variables list → dict {key: default_value}.
    variables_dict: dict[str, Any] = {}
    for v in w.get("variables") or []:
        if isinstance(v, dict) and v.get("key"):
            variables_dict[v["key"]] = v.get("default")

    trigger_config: dict[str, Any] = {}
    if w.get("trigger_ref"):
        trigger_config["trigger_ref"] = w["trigger_ref"]

    row = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        name=slug,
        description=w.get("description"),
        trigger_type=w.get("trigger_type") or "manual",
        trigger_config=trigger_config,
        steps=runtime_steps,
        variables=variables_dict,
        category=w.get("category"),
        tags=list(w.get("tags") or []),
        is_active=True,
        version=int(w.get("version") or 1),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row.id


# ── Post-install checks ───────────────────────────────────────────────


async def _run_post_install_check(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    check: dict[str, Any],
    todos: list[InstallTodo],
) -> None:
    """Execute one post_install_check inline. Failures become blocking
    todos so the operator knows the workspace isn't fully wired.

    Supported kinds (extend as new ones land in the schema):

      session_alive    — verify an IntegrationSession with provider /
                         (optional) label exists and status='active'
      agent_callable   — verify an AgentSubscription with service_key
                         exists and status='active'
      cron_scheduled   — verify a ScheduledJob whose job_id starts with
                         the blueprint's job_id (installer suffixes
                         with workspace_id[-8:] for uniqueness)
      workflow_present — verify a WorkflowDefinition with name=slug
                         exists (a real ``workflow_dryrun`` invocation
                         is a runtime concern, deferred)

    Unknown check kinds are recorded as a non-blocking note so the
    operator at least sees them.
    """
    if not isinstance(check, dict):
        return
    kind = check.get("kind")

    if kind == "session_alive":
        label = check.get("session_label")
        provider = check.get("provider")
        stmt = select(IntegrationSession).where(
            IntegrationSession.entity_id == entity_id,
            IntegrationSession.status == "active",
        )
        if provider:
            stmt = stmt.where(IntegrationSession.provider == provider)
        if label:
            stmt = stmt.where(IntegrationSession.label == label)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            todos.append(InstallTodo(
                kind="post_install_check",
                detail=(
                    f"Post-install check failed: no active session "
                    f"(provider={provider!r}, label={label!r})."
                ),
                payload={"check": check, "result": "missing_session"},
                blocking=True,
            ))
        return

    if kind == "agent_callable":
        service_key = check.get("service_key")
        if not service_key:
            return
        row = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.service_key == service_key,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()
        if row is None:
            todos.append(InstallTodo(
                kind="post_install_check",
                detail=(
                    f"Post-install check failed: no active subscription "
                    f"with service_key={service_key!r}."
                ),
                payload={"check": check, "result": "missing_subscription"},
                blocking=True,
            ))
        return

    if kind == "cron_scheduled":
        job_id = check.get("job_id")
        if not job_id:
            return
        # Installer suffixes job_id with workspace_id[-8:] so we match by
        # prefix; restricting to this workspace keeps it tenant-safe.
        rows = list((await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.workspace_id == workspace_id,
                ScheduledJob.job_id.startswith(job_id),
            )
        )).scalars().all())
        if not rows:
            todos.append(InstallTodo(
                kind="post_install_check",
                detail=(
                    f"Post-install check failed: no scheduled job whose id "
                    f"starts with {job_id!r}."
                ),
                payload={"check": check, "result": "missing_scheduled_job"},
                blocking=True,
            ))
        return

    if kind in ("workflow_present", "workflow_dryrun"):
        slug = check.get("workflow_slug")
        if not slug:
            return
        row = (await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.name == slug,
            )
        )).scalar_one_or_none()
        if row is None:
            todos.append(InstallTodo(
                kind="post_install_check",
                detail=(
                    f"Post-install check failed: workflow {slug!r} not "
                    f"installed in this entity."
                ),
                payload={"check": check, "result": "missing_workflow"},
                blocking=True,
            ))
        return

    # Unknown kind — surface as a note (non-blocking) so the operator
    # at least sees it and a future Manor can plug it in.
    todos.append(InstallTodo(
        kind="post_install_check",
        detail=f"Unknown post_install_check kind={kind!r}; skipping.",
        payload={"check": check, "result": "unknown_kind"},
        blocking=False,
    ))
