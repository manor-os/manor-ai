"""DB-backed conversational workspace draft service.

Wraps :mod:`workspace_setup_service` -- which holds an in-memory
``WorkspaceSetupSession`` dataclass -- with persistence so the user can
close the tab and resume. A draft is materialized into a real Workspace
on finalize via the same code path the legacy in-place setup wizard
uses (``finalize_setup``), keeping the operating model + agent
subscription + memory seeding logic in one place.

Lifecycle:
  active     -- conversation in progress
  ready      -- all required fields collected, awaiting confirmation
  finalized  -- materialized into a Workspace
  abandoned  -- user gave up

Public API:
  start_draft           -- create empty draft + first assistant turn
  process_draft_message -- one user turn -> updated draft + visible reply
  apply_blueprint       -- pre-fill draft fields from a marketplace blueprint
  finalize_draft        -- create the real workspace and mark draft finalized
  get_draft             -- read access for the API
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.constants.blueprints import BlueprintStatus
from packages.core.ai.runtime import (
    runtime_lint_workspace_draft,
    runtime_reconcile_workspace_draft_fields,
    runtime_run_workspace_architect_turn,
)
from packages.core.blueprints.payload import PayloadError, migrate_payload, validate_payload
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.workspace import Agent
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_setup_service import (
    DEFAULT_FIELDS,
    REQUIRED_FIELDS,
    WorkspaceSetupSession,
    finalize_setup,
)

logger = logging.getLogger(__name__)


_OPENING_USER_MESSAGE = "begin"


# ---------------------------------------------------------------------------
# Loading / saving
# ---------------------------------------------------------------------------

async def get_draft(
    db: AsyncSession,
    draft_id: str,
    entity_id: str,
    *,
    for_update: bool = False,
) -> Optional[WorkspaceDraft]:
    query = select(WorkspaceDraft).where(
        WorkspaceDraft.id == draft_id,
        WorkspaceDraft.entity_id == entity_id,
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalar_one_or_none()


def _session_from_draft(draft: WorkspaceDraft) -> WorkspaceSetupSession:
    return WorkspaceSetupSession(
        entity_id=draft.entity_id,
        fields=copy.deepcopy(draft.fields or DEFAULT_FIELDS),
        messages=list(draft.messages or []),
        ready=bool(draft.ready),
        missing=list(draft.missing or sorted(REQUIRED_FIELDS)),
        user_id=draft.user_id,
    )


def _apply_session_to_draft(
    draft: WorkspaceDraft, session: WorkspaceSetupSession,
) -> None:
    draft.fields = session.fields
    draft.messages = session.messages
    draft.ready = session.ready
    draft.missing = list(session.missing)
    # JSONB columns mutated in place need explicit notification.
    flag_modified(draft, "fields")
    flag_modified(draft, "messages")
    flag_modified(draft, "missing")
    if draft.ready and draft.status == "active":
        draft.status = "ready"
    elif not draft.ready and draft.status == "ready":
        draft.status = "active"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_draft(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: Optional[str] = None,
    initial_brief: Optional[str] = None,
    stream_handler: Optional[Any] = None,
    on_tool_start: Optional[Any] = None,
    on_tool_end: Optional[Any] = None,
) -> Tuple[str, WorkspaceDraft]:
    """Create a fresh draft and seed it with the first assistant turn.

    Routes the opening turn through the typed-tool ``workspace_architect``
    instead of the legacy single-shot JSON wizard, so the same precision
    guarantees apply from the very first message.
    """
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=user_id,
        fields=copy.deepcopy(DEFAULT_FIELDS),
        messages=[],
        missing=sorted(REQUIRED_FIELDS),
        ready=False,
        status="active",
    )
    db.add(draft)
    await db.flush()

    opening = (initial_brief or _OPENING_USER_MESSAGE).strip() or _OPENING_USER_MESSAGE
    visible = await _architect_turn(
        db,
        draft=draft,
        entity_id=entity_id,
        user_id=user_id,
        user_message=opening,
        stream_handler=stream_handler,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
    )
    _record_visible_messages(draft, opening, visible)
    await _refresh_missing_from_lint(db, draft)
    await db.flush()
    await db.refresh(draft)
    return visible, draft


async def process_draft_message(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    user_message: str,
    stream_handler: Optional[Any] = None,
    on_tool_start: Optional[Any] = None,
    on_tool_end: Optional[Any] = None,
) -> Tuple[str, WorkspaceDraft]:
    """Process one user turn against a persisted draft via the architect."""
    draft = await get_draft(db, draft_id, entity_id)
    if draft is None:
        raise ValueError("Draft not found")
    if draft.status == "finalized":
        raise ValueError("Draft already finalized")

    visible = await _architect_turn(
        db,
        draft=draft,
        entity_id=entity_id,
        user_id=draft.user_id,
        user_message=user_message,
        stream_handler=stream_handler,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
    )
    _record_visible_messages(draft, user_message, visible)
    await _refresh_missing_from_lint(db, draft)
    await db.flush()
    await db.refresh(draft)
    return visible, draft


# ---------------------------------------------------------------------------
# Architect glue
# ---------------------------------------------------------------------------

async def _architect_turn(
    db: AsyncSession,
    *,
    draft: WorkspaceDraft,
    entity_id: str,
    user_id: Optional[str],
    user_message: str,
    stream_handler: Optional[Any] = None,
    on_tool_start: Optional[Any] = None,
    on_tool_end: Optional[Any] = None,
) -> str:
    """Invoke the typed-tool architect for one turn. Mutations land on
    ``draft.fields`` via tool calls; this returns the visible reply."""

    history = [
        {"role": m.get("role"), "content": m.get("content")}
        for m in (draft.messages or [])
        if m.get("role") in ("user", "assistant")
    ]
    return await runtime_run_workspace_architect_turn(
        db,
        draft_id=draft.id,
        entity_id=entity_id,
        user_id=user_id,
        user_message=user_message,
        history=history,
        stream_handler=stream_handler,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
    )


def _record_visible_messages(draft: WorkspaceDraft, user_message: str, assistant_reply: str) -> None:
    """Append the user's text + the architect's visible reply to the
    draft's transcript so the next turn carries conversational context.
    Tool round-trips are deliberately omitted -- they're an internal
    implementation detail and would balloon the transcript."""
    msgs = list(draft.messages or [])
    if user_message and user_message.strip().lower() not in ("begin",):
        msgs.append({"role": "user", "content": user_message})
    if assistant_reply:
        msgs.append({"role": "assistant", "content": assistant_reply})
    draft.messages = msgs
    flag_modified(draft, "messages")


def reconcile_draft_fields(draft: WorkspaceDraft) -> bool:
    """Normalize derived draft fields that can drift after agent redesigns."""

    before = copy.deepcopy(dict(draft.fields or {}))
    fields = runtime_reconcile_workspace_draft_fields(before)
    if before == fields:
        return False
    draft.fields = fields
    flag_modified(draft, "fields")
    return True


async def _refresh_missing_from_lint(db: AsyncSession, draft: WorkspaceDraft) -> None:
    """Re-derive ``missing`` + ``ready`` from a fresh lint pass.

    The architect's ``ws_mark_ready`` tool sets these too, but we also
    want them up to date when the architect *didn't* mark ready (e.g.
    mid-conversation or after a remove). Cheaper than running another
    LLM call -- the Runtime draft lint helper is pure Python.
    """
    reconcile_draft_fields(draft)
    lint = await runtime_lint_workspace_draft(
        db,
        entity_id=draft.entity_id,
        draft_id=draft.id,
    )
    if not lint:
        return
    if not lint.get("ok"):
        return
    p0_issues = [i for i in lint.get("issues", []) if i.get("severity") == "P0"]
    missing = sorted({(i.get("where") or "").split(".")[0] for i in p0_issues if i.get("where")})
    draft.missing = missing
    flag_modified(draft, "missing")
    if not p0_issues:
        if not draft.ready:
            draft.ready = True
        if draft.status == "active":
            draft.status = "ready"
    else:
        if draft.ready:
            draft.ready = False
        if draft.status == "ready":
            draft.status = "active"


def _blueprint_goal_to_draft(goal: dict[str, Any]) -> dict[str, Any]:
    """Translate canonical Blueprint goal fields into setup-draft fields."""
    out = dict(goal)
    metric_key = str(goal.get("metric_key") or goal.get("goal_key") or "").strip()
    if metric_key:
        out["goal_key"] = metric_key
    if goal.get("target_value") is not None:
        out["target"] = goal["target_value"]
    if goal.get("measurement_cadence"):
        out["cadence"] = goal["measurement_cadence"]
    return out


def _blueprint_scheduled_job_to_draft(job: dict[str, Any]) -> dict[str, Any]:
    """Keep a scheduled job structured while adapting it to Draft automation."""
    target = dict(job.get("execution_target") or {})
    return {
        "automation_key": job.get("job_id"),
        "name": job.get("name"),
        "description": job.get("payload_message") or "",
        "service_key": target.get("service_key") or "",
        "job_type": job.get("job_type"),
        "schedule_kind": job.get("schedule_kind"),
        "cron_expr": job.get("cron_expr"),
        "every_seconds": job.get("every_seconds"),
        "run_at": job.get("run_at"),
        "timezone": job.get("timezone"),
        "payload_message": job.get("payload_message"),
        "execution_type": job.get("execution_type"),
        "execution_target": target,
        "execution_script": job.get("execution_script"),
        "default_delivery_mode": job.get("default_delivery_mode"),
        "source": "blueprint",
    }


def _blueprint_skill_to_missing_spec(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(skill[key])
        for key in (
            "slug",
            "name",
            "description",
            "system_prompt",
            "tools",
            "input_schema",
            "output_format",
            "category",
            "tags",
            "config",
            "version",
        )
        if key in skill
    }


async def _blueprint_agent_mappings(
    db: AsyncSession,
    *,
    entity_id: str,
    recipe: dict[str, Any],
    embedded: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve portable Blueprint agent refs into target Draft mappings."""
    subscriptions = [
        dict(item) for item in recipe.get("subscriptions") or []
        if isinstance(item, dict)
    ]
    embedded_agents = {
        str(item.get("slug") or "").strip(): dict(item)
        for item in embedded.get("agents") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }
    embedded_skills = {
        str(item.get("slug") or "").strip(): dict(item)
        for item in embedded.get("skills") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }

    external_slugs = {
        str(item.get("agent_slug") or "").strip()
        for item in subscriptions
        if str(item.get("agent_slug") or "").strip() not in embedded_agents
    }
    available_by_slug: dict[str, Agent] = {}
    if external_slugs:
        rows = list((await db.execute(
            select(Agent).where(
                Agent.slug.in_(external_slugs),
                Agent.deleted_at.is_(None),
                Agent.status == "active",
                or_(Agent.entity_id == entity_id, Agent.entity_id.is_(None)),
            )
        )).scalars().all())
        # Prefer an entity-owned row over the platform template when both use
        # the same portable slug.
        for row in sorted(rows, key=lambda agent: agent.entity_id != entity_id):
            if row.slug:
                available_by_slug.setdefault(row.slug, row)

    mappings: list[dict[str, Any]] = []
    missing_agents: list[str] = []
    for index, subscription in enumerate(subscriptions):
        agent_slug = str(subscription.get("agent_slug") or "").strip()
        service_key = str(subscription.get("service_key") or "").strip()
        if not service_key:
            service_key = f"blueprint_service_{index + 1}"

        embedded_agent = embedded_agents.get(agent_slug)
        if embedded_agent is not None:
            skill_refs = [
                str(ref).strip()
                for ref in embedded_agent.get("skill_bindings") or []
                if str(ref or "").strip()
            ]
            missing_skill_specs = [
                _blueprint_skill_to_missing_spec(embedded_skills[ref])
                for ref in skill_refs
                if ref in embedded_skills
            ]
            mcp_refs = [
                str(binding.get("server_slug") or "").strip()
                for binding in embedded_agent.get("mcp_bindings") or []
                if isinstance(binding, dict)
                and str(binding.get("server_slug") or "").strip()
            ]
            mappings.append({
                "service_key": service_key,
                "strategy": "create_custom",
                "rationale": "Copied from the Blueprint's embedded Agent.",
                "blueprint_agent_slug": agent_slug,
                "create_agent_draft": {
                    "agent_name": embedded_agent.get("name") or agent_slug,
                    "agent_slug": agent_slug,
                    "agent_description": embedded_agent.get("description") or "",
                    "system_prompt": embedded_agent.get("system_prompt") or "",
                    "tool_bindings": list(embedded_agent.get("tool_bindings") or []),
                    "business_capabilities": list(
                        embedded_agent.get("business_capabilities") or []
                    ),
                    "skill_bindings": skill_refs,
                    "mcp_bindings": mcp_refs,
                    "missing_skill_specs": missing_skill_specs,
                    "missing_integrations": [],
                },
            })
            continue

        available = available_by_slug.get(agent_slug)
        if available is not None:
            mappings.append({
                "service_key": service_key,
                "agent_id": available.id,
                "recommended_agent_id": available.id,
                "recommended_agent_name": available.name,
                "strategy": "match",
                "rationale": "Resolved from the Blueprint's portable Agent slug.",
                "blueprint_agent_slug": agent_slug,
            })
            continue

        if agent_slug:
            missing_agents.append(agent_slug)
        mappings.append({
            "service_key": service_key,
            "agent_id": None,
            "recommended_agent_name": agent_slug or "Missing Blueprint Agent",
            "strategy": "blueprint_external",
            "rationale": "Install the Blueprint's required Agent before finalizing.",
            "blueprint_agent_slug": agent_slug,
        })

    return mappings, sorted(set(missing_agents))


async def apply_blueprint(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    blueprint_id: str,
) -> WorkspaceDraft:
    """Merge a canonical Blueprint recipe and its capabilities into a Draft."""
    draft = await get_draft(db, draft_id, entity_id)
    if draft is None:
        raise ValueError("Draft not found")
    if draft.status == "finalized":
        raise ValueError("Draft already finalized")

    bp = await db.get(WorkspaceBlueprint, blueprint_id)
    if bp is None:
        raise ValueError("Blueprint not available")

    # Inlined purchase lookup — core services must not import from
    # apps.api routers.
    purchased = False
    if bp.entity_id != entity_id:
        from packages.core.models.blueprint_purchase import BlueprintPurchase
        purchased = (await db.execute(
            select(BlueprintPurchase.id).where(
                BlueprintPurchase.blueprint_id == bp.id,
                BlueprintPurchase.buyer_entity_id == entity_id,
                BlueprintPurchase.status == "completed",
            )
        )).scalar_one_or_none() is not None

    # A completed purchase keeps the blueprint usable even after the
    # seller archives/unpublishes it (spec §4.3/§5.3).
    if bp.status != BlueprintStatus.PUBLISHED and not purchased:
        raise ValueError("Blueprint not available")

    # Paid gate: the payload IS the paid product. Merging it into a draft
    # would leak it to non-purchasers (the router maps PermissionError
    # to 402).
    if (bp.price_cents or 0) > 0 and bp.entity_id != entity_id and not purchased:
        raise PermissionError("purchase required to apply this blueprint")

    raw_payload = dict(bp.payload or {})
    if not raw_payload.get("blueprint_version") and not (
        isinstance(raw_payload.get("manifest"), dict)
        and raw_payload["manifest"].get("blueprint_version")
    ):
        # Early marketplace rows predate payload versioning. Lift them through
        # the official v1.0 migrator instead of keeping a second apply path.
        raw_payload = {
            **raw_payload,
            "blueprint_version": "1.0",
            "title": raw_payload.get("title") or bp.title,
        }
    try:
        validate_payload(raw_payload)
        payload = migrate_payload(raw_payload)
    except PayloadError as exc:
        raise ValueError(f"Blueprint payload is invalid: {exc}") from exc

    manifest = dict(payload.get("manifest") or {})
    contract = dict(payload.get("contract") or {})
    embedded = dict(payload.get("embedded") or {})
    recipe = dict(payload.get("recipe") or {})
    policy = dict(payload.get("policy") or {})
    operating_model = dict(recipe.get("operating_model") or {})

    fields = copy.deepcopy(dict(draft.fields or DEFAULT_FIELDS))
    shell = {
        "name": manifest.get("title"),
        "kind": manifest.get("kind") or operating_model.get("kind"),
        "operating_context": operating_model.get("context"),
        "primary_work": operating_model.get("primary_work"),
    }
    for key, value in shell.items():
        if not fields.get(key) and value:
            fields[key] = value

    for list_key in ("services", "rules", "automations"):
        value = operating_model.get(list_key)
        if isinstance(value, list) and value:
            fields[list_key] = copy.deepcopy(value)

    blueprint_goals = [
        _blueprint_goal_to_draft(dict(goal))
        for goal in recipe.get("goals") or []
        if isinstance(goal, dict)
    ]
    if not blueprint_goals:
        blueprint_goals = [
            _blueprint_goal_to_draft(dict(goal))
            for goal in operating_model.get("goals") or []
            if isinstance(goal, dict)
        ]
    if blueprint_goals:
        fields["goals"] = blueprint_goals

    scheduled_automations = [
        _blueprint_scheduled_job_to_draft(dict(job))
        for job in recipe.get("scheduled_jobs") or []
        if isinstance(job, dict)
    ]
    if scheduled_automations:
        existing_keys = {
            str(item.get("automation_key") or item.get("name") or "").strip()
            for item in fields.get("automations") or []
            if isinstance(item, dict)
        }
        fields.setdefault("automations", []).extend(
            item for item in scheduled_automations
            if str(item.get("automation_key") or item.get("name") or "").strip()
            not in existing_keys
        )

    for object_key in ("evaluation", "budget_policy", "channel_config"):
        value = operating_model.get(object_key)
        if isinstance(value, dict) and value:
            fields[object_key] = copy.deepcopy(value)

    channels = [
        dict(channel) for channel in contract.get("channels") or []
        if isinstance(channel, dict)
    ]
    if channels and not (fields.get("channel_config") or {}).get("channels"):
        channel_config = dict(fields.get("channel_config") or {})
        channel_config["channels"] = [
            {
                "role": "channel",
                "channel_type": channel.get("channel_type"),
                "provider": channel.get("provider") or channel.get("channel_type"),
                "name": channel.get("purpose") or channel.get("channel_type"),
                "purpose": channel.get("purpose") or "Blueprint channel requirement",
                "login_required": True,
            }
            for channel in channels
            if channel.get("channel_type")
        ]
        fields["channel_config"] = channel_config

    mappings, missing_agents = await _blueprint_agent_mappings(
        db,
        entity_id=entity_id,
        recipe=recipe,
        embedded=embedded,
    )
    if mappings:
        fields["agent_mappings"] = mappings
    if missing_agents:
        fields["_blueprint_missing_agents"] = missing_agents
    else:
        fields.pop("_blueprint_missing_agents", None)

    services = [
        dict(service) for service in fields.get("services") or []
        if isinstance(service, dict)
    ]
    known_service_keys = {
        str(service.get("service_key") or "").strip() for service in services
    }
    for mapping in mappings:
        service_key = str(mapping.get("service_key") or "").strip()
        if service_key and service_key not in known_service_keys:
            services.append({
                "service_key": service_key,
                "name": service_key,
                "description": "Service copied from a Blueprint subscription.",
                "autonomy_level": "supervised",
                "owner_role": "workspace_owner",
            })
            known_service_keys.add(service_key)
    if services:
        fields["services"] = services

    knowledge_packs = [
        dict(pack) for pack in embedded.get("knowledge_packs") or []
        if isinstance(pack, dict)
    ]
    if knowledge_packs:
        fields["knowledge_attachments"] = [
            {
                "name": pack.get("title") or pack.get("slug"),
                "purpose": pack.get("purpose") or "Blueprint knowledge pack",
                "mode": "create_new",
                "generate_starter_doc": False,
                "blueprint_slug": pack.get("slug"),
                "folder_structure": list(pack.get("folder_structure") or []),
                "starter_documents": copy.deepcopy(
                    list(pack.get("starter_documents") or [])
                ),
            }
            for pack in knowledge_packs
            if pack.get("title") or pack.get("slug")
        ]

    governance = policy.get("governance")
    if isinstance(governance, dict) and governance:
        fields["governance_policy"] = copy.deepcopy(governance)

    # Preserve recipe-level operating semantics that the Draft editor does not
    # expose as first-class fields yet. finalize_setup merges these before its
    # normalized fields, so source IDs can never override new materialized IDs.
    portable_operating_model = copy.deepcopy(operating_model)
    for transient_key in (
        "kind",
        "context",
        "primary_work",
        "settings",
        "services",
        "goals",
        "rules",
        "automations",
        "evaluation",
        "budget_policy",
        "channel_config",
        "agent_mappings",
    ):
        portable_operating_model.pop(transient_key, None)
    strategist = recipe.get("strategist")
    if isinstance(strategist, dict) and strategist:
        strategist_copy = copy.deepcopy(strategist)
        cadence = strategist_copy.get("cadence")
        if isinstance(cadence, dict):
            if cadence.get("schedule"):
                strategist_copy["cadence"] = cadence["schedule"]
            else:
                strategist_copy.pop("cadence", None)
            if cadence.get("trigger_conditions") is not None:
                strategist_copy["trigger_conditions"] = copy.deepcopy(
                    cadence["trigger_conditions"]
                )
        portable_operating_model["strategist"] = strategist_copy
    if portable_operating_model:
        fields["_blueprint_operating_model"] = portable_operating_model

    required_mcp = [
        str(item.get("slug") or "").strip()
        for item in (contract.get("requires") or {}).get("mcp_servers") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    ]
    integration_flags: list[dict[str, Any]] = [
        {
            "provider": provider,
            "purpose": "Required by the applied Blueprint",
            "required": True,
            "source": "blueprint",
        }
        for provider in required_mcp
    ]
    integration_flags.extend({
        "provider": channel.get("provider") or channel.get("channel_type"),
        "purpose": channel.get("purpose") or "Blueprint channel requirement",
        "required": bool(channel.get("required", True)),
        "source": "blueprint_channel",
    } for channel in channels)
    integration_flags.extend({
        "provider": session.get("provider"),
        "purpose": session.get("purpose") or "Blueprint browser session requirement",
        "required": bool(session.get("required", True)),
        "source": "blueprint_session",
    } for session in contract.get("sessions") or [] if isinstance(session, dict))
    if integration_flags:
        from packages.core.services.integration_resolution import (
            resolve_missing_integration_flags,
        )

        fields["flagged_integrations"] = await resolve_missing_integration_flags(
            db,
            entity_id=entity_id,
            user_id=draft.user_id,
            flagged=integration_flags,
        )

    draft.fields = fields
    draft.applied_blueprint_id = blueprint_id
    flag_modified(draft, "fields")

    # Append a system note to the conversation so the LLM sees it on the
    # next turn and stops asking questions the blueprint already answered.
    note = {
        "role": "user",
        "content": (
            f"<workspace_setup_note>The user applied blueprint "
            f"\"{bp.title}\" (id={bp.id}). The fields above are now "
            f"pre-populated; use them as the basis and only ask about "
            f"anything still missing.</workspace_setup_note>"
        ),
    }
    msgs = list(draft.messages or [])
    msgs.append(note)
    draft.messages = msgs
    flag_modified(draft, "messages")

    await db.flush()
    await _refresh_missing_from_lint(db, draft)
    await db.flush()
    await db.refresh(draft)
    return draft


async def finalize_draft(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    progress: Optional[Any] = None,
) -> Tuple[str, WorkspaceDraft]:
    """Materialize the draft into a real Workspace.

    Pass ``progress=callable(step, payload)`` to receive incremental durable
    materialization checkpoints. The API commits this function's transaction,
    then continues the same progress stream through post-commit startup
    dispatch.
    """
    # Serialize finalize requests for the same draft. A second request waits
    # for the first transaction and then returns its materialized workspace.
    draft = await get_draft(db, draft_id, entity_id, for_update=True)
    if draft is None:
        raise ValueError("Draft not found")
    if draft.status == "finalized":
        if draft.finalized_workspace_id:
            return draft.finalized_workspace_id, draft
        raise ValueError("Draft already finalized but missing workspace id")
    if draft.status == "abandoned":
        raise ValueError("Draft was abandoned and cannot be finalized")
    if not draft.ready:
        missing = ", ".join(draft.missing or [])
        raise ValueError(
            f"Draft not ready -- still missing: {missing or 'unknown fields'}"
        )

    reconcile_draft_fields(draft)
    session = _session_from_draft(draft)
    workspace_id = await finalize_setup(session, db, progress=progress)

    if draft.applied_blueprint_id:
        from packages.core.models.workspace import Workspace

        workspace = await db.get(Workspace, workspace_id)
        blueprint = await db.get(WorkspaceBlueprint, draft.applied_blueprint_id)
        if workspace is not None:
            settings = dict(workspace.settings or {})
            settings["_blueprint"] = {
                "blueprint_id": draft.applied_blueprint_id,
                "blueprint_slug": getattr(blueprint, "slug", None),
                "title": getattr(blueprint, "title", None),
                "applied_via": "workspace_draft",
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
            workspace.settings = settings

    draft.status = "finalized"
    draft.finalized_workspace_id = workspace_id
    draft.finalized_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(draft)
    return workspace_id, draft


async def abandon_draft(
    db: AsyncSession, *, draft_id: str, entity_id: str,
) -> bool:
    draft = await get_draft(db, draft_id, entity_id)
    if draft is None:
        return False
    if draft.status == "finalized":
        return False
    draft.status = "abandoned"
    await db.flush()
    return True
