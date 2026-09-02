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
  create_draft_shell     -- persist the resumable draft before any LLM work
  start_draft           -- create empty draft + first assistant turn
  process_draft_message -- one user turn -> updated draft + visible reply
  apply_blueprint       -- pre-fill draft fields from a marketplace blueprint
  finalize_draft        -- create the real workspace and mark draft finalized
  get_draft             -- read access for the API
"""
from __future__ import annotations

import copy
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.constants.blueprints import BlueprintStatus
from packages.core.constants.workspace_drafts import (
    CREATION_PREFERENCES_FIELD,
    CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION,
    WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD,
    uses_ui_runtime_mode,
)
from packages.core.ai.runtime import (
    runtime_lint_workspace_draft,
    runtime_reconcile_workspace_draft_fields,
    runtime_run_workspace_architect_turn,
)
from packages.core.blueprints.freshness import (
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY,
    UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    blueprint_content_fingerprint,
    blueprint_section_fingerprints,
    blueprint_upgrade_unsupported_fingerprint,
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
from packages.core.services.marketplace_billing import (
    blueprint_delivery_requires_paid_plan,
    blueprint_delivery_source,
)
from packages.core.services.resource_access import (
    ResourceDescriptor,
    readable_resource_ids,
)

logger = logging.getLogger(__name__)


class WorkspaceDraftNotReadyError(ValueError):
    """A verified lint result invalidated a previously ready Draft."""


def opening_user_message(initial_brief: Optional[str]) -> str:
    return (initial_brief or "").strip() or "begin"


# ---------------------------------------------------------------------------
# Loading / saving
# ---------------------------------------------------------------------------

async def get_draft(
    db: AsyncSession,
    draft_id: str,
    entity_id: str,
    user_id: str,
    *,
    for_update: bool = False,
) -> Optional[WorkspaceDraft]:
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    query = select(WorkspaceDraft).where(
        WorkspaceDraft.id == draft_id,
        WorkspaceDraft.entity_id == entity_id,
        WorkspaceDraft.user_id == user_id,
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
    user_id: str,
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
    draft = await create_draft_shell(
        db,
        entity_id=entity_id,
        user_id=user_id,
        initial_brief=initial_brief,
    )

    opening = opening_user_message(initial_brief)
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


async def create_draft_shell(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    initial_brief: Optional[str] = None,
    draft_id: Optional[str] = None,
) -> WorkspaceDraft:
    """Create the durable, resumable part of a Workspace draft.

    Streaming callers commit this shell before invoking the Architect so a
    disconnected client can resume with the exact initial brief and draft id.
    Non-streaming callers keep their existing single-transaction behavior by
    using :func:`start_draft`.
    """
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("Workspace draft user context is required")
    fields = copy.deepcopy(DEFAULT_FIELDS)
    fields[WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD] = (
        CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION
    )
    fields[CREATION_PREFERENCES_FIELD] = {
        "goal_confirmed": False,
        "autonomy_confirmed": False,
    }
    normalized_brief = (initial_brief or "").strip()
    if normalized_brief:
        fields["initial_brief"] = normalized_brief

    draft = WorkspaceDraft(
        **({"id": draft_id} if draft_id else {}),
        entity_id=entity_id,
        user_id=user_id,
        fields=fields,
        messages=[],
        missing=sorted(REQUIRED_FIELDS),
        ready=False,
        status="active",
    )
    db.add(draft)
    await db.flush()
    return draft


async def process_draft_message(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    user_message: str,
    user_id: str,
    stream_handler: Optional[Any] = None,
    on_tool_start: Optional[Any] = None,
    on_tool_end: Optional[Any] = None,
    dedupe_opening: bool = False,
) -> Tuple[str, WorkspaceDraft]:
    """Process one user turn against a persisted draft via the architect."""
    draft = await get_draft(
        db, draft_id, entity_id, user_id, for_update=True,
    )
    if draft is None:
        raise ValueError("Draft not found")
    if draft.status not in {"active", "ready"}:
        raise ValueError(f"Draft is {draft.status} and cannot be changed")

    # Opening streams are resumable.  If the first connection committed its
    # turn but the browser missed the terminal SSE frame, replay the committed
    # assistant text instead of spending another architect turn or appending a
    # duplicate opening exchange.  The row lock above serializes this check
    # with an opening stream that is still finishing.
    if dedupe_opening:
        previous_reply = next(
            (
                str(message.get("content") or "")
                for message in reversed(draft.messages or [])
                if message.get("role") == "assistant"
                and str(message.get("content") or "").strip()
            ),
            "",
        )
        if previous_reply:
            if stream_handler is not None:
                await stream_handler("text_delta", {"content": previous_reply})
            return previous_reply, draft

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
    user_id: str,
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


def apply_public_field_updates(
    draft: WorkspaceDraft,
    updates: dict[str, Any],
) -> None:
    """Merge user edits; a runtime-mode switch is itself the user's choice."""

    previous_fields = dict(draft.fields or {})
    fields = dict(previous_fields)
    fields.update(updates)
    if (
        WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD in fields
        or CREATION_PREFERENCES_FIELD in fields
    ):
        current = fields.get(CREATION_PREFERENCES_FIELD)
        preferences = dict(current) if isinstance(current, dict) else {}
        if (
            any(key in updates and updates[key] != previous_fields.get(key)
                for key in ("goals", "stats"))
        ):
            preferences["goal_confirmed"] = False
        autonomy_changed = any(
            key in updates and updates[key] != previous_fields.get(key)
            for key in ("heartbeat_enabled", "heartbeat_cadence")
        )
        if uses_ui_runtime_mode(fields) and "heartbeat_enabled" in updates:
            preferences["autonomy_confirmed"] = True
        elif autonomy_changed:
            preferences["autonomy_confirmed"] = False
        fields[CREATION_PREFERENCES_FIELD] = preferences
    draft.fields = fields
    flag_modified(draft, "fields")


async def _refresh_missing_from_lint(
    db: AsyncSession,
    draft: WorkspaceDraft,
) -> bool:
    """Re-derive ``missing`` + ``ready`` from a fresh lint pass.

    The architect's ``ws_mark_ready`` tool sets these too, but we also
    want them up to date when the architect *didn't* mark ready (e.g.
    mid-conversation or after a remove). Cheaper than running another
    LLM call -- the Runtime draft lint helper is pure Python.
    """
    reconcile_draft_fields(draft)
    fields = dict(draft.fields or {})
    account_contract = fields.get("_blueprint_account_contract")
    personalization_invalid = False
    if isinstance(account_contract, dict):
        from packages.core.blueprints.installer import (
            InstallError,
            resolve_install_variables,
        )
        from packages.core.blueprints.setup_preflight import (
            BlueprintSetupPreflightFactory,
        )

        preflight_contract = account_contract
        try:
            resolved_contract_payload, _ = resolve_install_variables(
                {"contract": account_contract},
                fields.get("blueprint_personalization")
                if isinstance(fields.get("blueprint_personalization"), dict)
                else {},
            )
            preflight_contract = resolved_contract_payload["contract"]
        except InstallError:
            personalization_invalid = True
            preflight_contract = {
                **account_contract,
                "channels": [],
            }
        preflight = await BlueprintSetupPreflightFactory.from_contract(
            db,
            contract=preflight_contract,
            entity_id=draft.entity_id,
            user_id=draft.user_id,
            selected_channel_config_ids=fields.get("blueprint_channel_config_ids")
            or fields.get("_blueprint_channel_config_ids"),
        )
        from packages.core.services.integration_resolution import (
            resolve_missing_integration_flags,
        )

        preserved_flags = await resolve_missing_integration_flags(
            db,
            entity_id=draft.entity_id,
            user_id=draft.user_id,
            flagged=[
                item
                for item in fields.get("flagged_integrations") or []
                if isinstance(item, dict)
                and str(item.get("source") or "") not in {
                    "blueprint",
                    "blueprint_channel",
                    "blueprint_session",
                    "blueprint_account_preflight",
                }
            ],
        )
        account_flags = [{
            "provider": requirement.provider,
            "name": requirement.label,
            "purpose": requirement.purpose or requirement.reason,
            "required": requirement.required,
            "source": "blueprint_account_preflight",
            "setup_kind": requirement.setup_kind,
            "requirement_kind": requirement.kind.value,
        } for requirement in preflight.requirements if not requirement.ready]
        fields["flagged_integrations"] = [*preserved_flags, *account_flags]
        fields["_blueprint_channel_requirements"] = [
            asdict(requirement)
            for requirement in preflight.requirements
            if requirement.requirement_key
        ]
        fields["_blueprint_channel_config_ids"] = {
            requirement.requirement_key: requirement.resource_id
            for requirement in preflight.requirements
            if requirement.requirement_key and requirement.resource_id
        }
        draft.fields = fields
        flag_modified(draft, "fields")
    lint = await runtime_lint_workspace_draft(
        db,
        entity_id=draft.entity_id,
        draft_id=draft.id,
        user_id=draft.user_id,
    )
    if not lint or not lint.get("ok"):
        draft.ready = False
        if draft.status == "ready":
            draft.status = "active"
        return False
    p0_issues = [i for i in lint.get("issues", []) if i.get("severity") == "P0"]
    missing = {
        (i.get("where") or "").split(".")[0]
        for i in p0_issues
        if i.get("where")
    }
    declarations = fields.get("_blueprint_variable_declarations")
    personalization = fields.get("blueprint_personalization")
    values = personalization if isinstance(personalization, dict) else {}
    if personalization_invalid:
        missing.add("blueprint_personalization")
    if declarations:
        from packages.core.blueprints.installer import InstallError, resolve_install_variables

        try:
            resolve_install_variables({"contract": {"variables": declarations}}, values)
        except InstallError:
            missing.add("blueprint_personalization")
    if any(
        bool(item.get("required", True))
        and bool(item.get("blocks_creation", True))
        for item in fields.get("flagged_integrations") or []
        if isinstance(item, dict)
    ):
        missing.add("flagged_integrations")
    missing = sorted(missing)
    draft.missing = missing
    flag_modified(draft, "missing")
    if not missing:
        if not draft.ready:
            draft.ready = True
        if draft.status == "active":
            draft.status = "ready"
    else:
        if draft.ready:
            draft.ready = False
        if draft.status == "ready":
            draft.status = "active"
    return True


def _blueprint_goal_to_draft(goal: dict[str, Any]) -> dict[str, Any]:
    """Translate canonical Blueprint goal fields into setup-draft fields."""
    out = dict(goal)
    goal_key = str(goal.get("goal_key") or "").strip()
    if goal_key:
        out["goal_key"] = goal_key
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
    user_id: str | None = None,
    recipe: dict[str, Any],
    embedded: dict[str, Any],
    source_blueprint_id: str = "",
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

    external_ids = {
        str(item.get("marketplace_agent_id") or "").strip()
        for item in subscriptions
        if str(item.get("marketplace_agent_id") or "").strip()
    }
    available_by_id: dict[str, Agent] = {}
    if external_ids:
        exact_rows = list((await db.execute(
            select(Agent).where(
                Agent.id.in_(external_ids),
                Agent.deleted_at.is_(None),
                Agent.status == "active",
                Agent.entity_id.is_(None),
                Agent.is_template.is_(True),
                Agent.is_public.is_(True),
            )
        )).scalars().all())
        available_by_id = {row.id: row for row in exact_rows}

    external_slugs = {
        str(item.get("agent_slug") or "").strip()
        for item in subscriptions
        if not str(item.get("marketplace_agent_id") or "").strip()
        if str(item.get("agent_slug") or "").strip() not in embedded_agents
    }
    available_by_slug: dict[str, Agent] = {}
    if external_slugs:
        rows = list((await db.execute(
            select(Agent).where(
                Agent.slug.in_(external_slugs),
                Agent.deleted_at.is_(None),
                Agent.status == "active",
                or_(
                    Agent.entity_id == entity_id,
                    and_(
                        Agent.entity_id.is_(None),
                        Agent.is_template.is_(True),
                        Agent.is_public.is_(True),
                    ),
                ),
            )
        )).scalars().all())
        entity_rows = [row for row in rows if row.entity_id == entity_id]
        readable_ids = await readable_resource_ids(
            db,
            descriptors=[
                ResourceDescriptor.from_row(row, "agent")
                for row in entity_rows
            ],
            entity_id=entity_id,
            user_id=user_id,
        )
        rows = [
            row for row in rows
            if row.entity_id is None or row.id in readable_ids
        ]
        candidates_by_slug: dict[str, list[Agent]] = {}
        for row in rows:
            if row.slug:
                candidates_by_slug.setdefault(row.slug, []).append(row)
        # Slug-only Blueprint payloads predate exact Marketplace ids. Resolve
        # them only when unambiguous; never prefer one same-slug resource.
        for slug, candidates in candidates_by_slug.items():
            if len(candidates) == 1:
                available_by_slug[slug] = candidates[0]
                continue
            installed_candidates = [
                candidate
                for candidate in candidates
                if candidate.entity_id == entity_id
                and str(
                    (candidate.config or {}).get("source_agent_id") or ""
                ).strip()
            ]
            if len(installed_candidates) != 1:
                continue
            installed_source_id = str(
                (installed_candidates[0].config or {}).get("source_agent_id")
                or ""
            ).strip()
            marketplace_source_ids = {
                candidate.id
                for candidate in candidates
                if candidate.entity_id is None
            }
            if (
                not marketplace_source_ids
                or marketplace_source_ids == {installed_source_id}
            ):
                available_by_slug[slug] = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.entity_id is None
                        and candidate.id == installed_source_id
                    ),
                    installed_candidates[0],
                )

    mappings: list[dict[str, Any]] = []
    missing_agents: list[str] = []
    for index, subscription in enumerate(subscriptions):
        agent_slug = str(subscription.get("agent_slug") or "").strip()
        marketplace_agent_id = str(
            subscription.get("marketplace_agent_id") or ""
        ).strip()
        service_key = str(subscription.get("service_key") or "").strip()
        if not service_key:
            service_key = f"blueprint_service_{index + 1}"

        embedded_agent = (
            None if marketplace_agent_id else embedded_agents.get(agent_slug)
        )
        if embedded_agent is not None:
            exact_skill_refs = [
                ref for ref in embedded_agent.get("skill_binding_refs") or []
                if isinstance(ref, dict)
                and str(ref.get("marketplace_id") or "").strip()
            ]
            exact_skill_slugs = {
                str(ref.get("slug") or "").strip()
                for ref in exact_skill_refs
                if str(ref.get("slug") or "").strip()
            }
            legacy_skill_refs = [
                str(ref).strip()
                for ref in embedded_agent.get("skill_bindings") or []
                if str(ref or "").strip()
                and str(ref).strip() not in exact_skill_slugs
            ]
            missing_skill_specs = [
                {
                    **_blueprint_skill_to_missing_spec(embedded_skills[ref]),
                    **(
                        {
                            "source_blueprint_component_key": str(
                                embedded_skills[ref].get("component_id")
                                or embedded_skills[ref].get("id")
                                or ref
                            ).strip()
                        }
                        if source_blueprint_id else {}
                    ),
                }
                for ref in legacy_skill_refs
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
                    "source_blueprint_id": source_blueprint_id or None,
                    "source_blueprint_component_key": str(
                        embedded_agent.get("component_id")
                        or embedded_agent.get("id")
                        or agent_slug
                    ).strip(),
                    "agent_description": embedded_agent.get("description") or "",
                    "system_prompt": embedded_agent.get("system_prompt") or "",
                    "tool_bindings": list(embedded_agent.get("tool_bindings") or []),
                    "business_capabilities": list(
                        embedded_agent.get("business_capabilities") or []
                    ),
                    "skill_bindings": legacy_skill_refs,
                    "skill_binding_refs": [dict(ref) for ref in exact_skill_refs],
                    "mcp_bindings": mcp_refs,
                    "missing_skill_specs": missing_skill_specs,
                    "missing_integrations": [],
                },
            })
            continue

        available = (
            available_by_id.get(marketplace_agent_id)
            if marketplace_agent_id
            else available_by_slug.get(agent_slug)
        )
        if available is not None:
            resolved_marketplace_agent_id = marketplace_agent_id
            if not resolved_marketplace_agent_id and available.entity_id is None:
                resolved_marketplace_agent_id = available.id
            mappings.append({
                "service_key": service_key,
                "agent_id": available.id,
                "recommended_agent_id": available.id,
                "recommended_agent_name": available.name,
                "strategy": "match",
                "rationale": (
                    "Resolved from the Blueprint's exact Marketplace Agent id."
                    if marketplace_agent_id
                    else "Resolved from an unambiguous legacy Agent slug."
                ),
                "blueprint_agent_slug": agent_slug,
                "marketplace_agent_id": resolved_marketplace_agent_id or None,
            })
            continue

        missing_ref = marketplace_agent_id or agent_slug
        if missing_ref:
            missing_agents.append(missing_ref)
        mappings.append({
            "service_key": service_key,
            "agent_id": None,
            "recommended_agent_name": agent_slug or "Missing Blueprint Agent",
            "strategy": "blueprint_external",
            "rationale": "Install the Blueprint's required Agent before finalizing.",
            "blueprint_agent_slug": agent_slug,
            "marketplace_agent_id": marketplace_agent_id or None,
        })

    return mappings, sorted(set(missing_agents))


async def apply_blueprint(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    blueprint_id: str,
    user_id: str,
) -> WorkspaceDraft:
    """Merge a canonical Blueprint recipe and its capabilities into a Draft."""
    draft = await get_draft(
        db, draft_id, entity_id, user_id, for_update=True,
    )
    if draft is None:
        raise ValueError("Draft not found")
    if draft.status not in {"active", "ready"}:
        raise ValueError(f"Draft is {draft.status} and cannot be changed")

    bp = await db.get(WorkspaceBlueprint, blueprint_id)
    if bp is None:
        raise ValueError("Blueprint not available")

    # Inlined purchase lookup — core services must not import from
    # apps.api routers.
    purchase = None
    if bp.entity_id != entity_id:
        from packages.core.constants.blueprints import BlueprintPurchaseStatus
        from packages.core.models.blueprint_purchase import BlueprintPurchase
        purchase = (await db.execute(
            select(BlueprintPurchase).where(
                BlueprintPurchase.blueprint_id == bp.id,
                BlueprintPurchase.buyer_entity_id == entity_id,
                BlueprintPurchase.status == BlueprintPurchaseStatus.COMPLETED.value,
            )
        )).scalar_one_or_none()
    purchased = purchase is not None

    requires_paid_plan = blueprint_delivery_requires_paid_plan(bp, purchase)
    if requires_paid_plan and bp.entity_id != entity_id:
        from packages.core.services.marketplace_billing import (
            require_paid_marketplace_plan,
        )

        await require_paid_marketplace_plan(
            db,
            entity_id=entity_id,
            blueprint_id=bp.id,
        )

    # A completed purchase keeps the blueprint usable even after the
    # seller archives/unpublishes it (spec §4.3/§5.3).
    if bp.status != BlueprintStatus.PUBLISHED and not purchased:
        raise ValueError("Blueprint not available")

    # Paid gate: the payload IS the paid product. Merging it into a draft
    # would leak it to non-purchasers (the router maps PermissionError
    # to 402).
    if requires_paid_plan and bp.entity_id != entity_id and not purchased:
        raise PermissionError("purchase required to apply this blueprint")

    source_payload, source_version = blueprint_delivery_source(bp, purchase)
    raw_payload = dict(source_payload)
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
    previous_preferences = fields.get(CREATION_PREFERENCES_FIELD)
    preserve_runtime_mode = (
        uses_ui_runtime_mode(fields)
        and isinstance(previous_preferences, dict)
        and previous_preferences.get("autonomy_confirmed") is True
    )
    if (
        WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD in fields
        or CREATION_PREFERENCES_FIELD in fields
    ):
        fields[CREATION_PREFERENCES_FIELD] = {
            "goal_confirmed": False,
            "autonomy_confirmed": preserve_runtime_mode,
        }
    fields["_blueprint_install_metadata"] = {
        BLUEPRINT_VERSION_KEY: str(source_version or "") or None,
        # Freshness compares against the published/delivered source payload,
        # not the normalized in-memory shape used by the installer. Payload
        # migrations may add defaults without changing the Blueprint itself.
        CONTENT_FINGERPRINT_KEY: blueprint_content_fingerprint(raw_payload),
        SECTION_FINGERPRINTS_KEY: blueprint_section_fingerprints(raw_payload),
    }
    # Keep the delivered source until finalize: the Marketplace row may change
    # while this Draft is being configured, including for paid receipt installs.
    fields["_blueprint_source_payload"] = copy.deepcopy(raw_payload)
    fields.pop("blueprint_channel_config_ids", None)
    fields.pop("_blueprint_channel_config_ids", None)
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
    fields["stats"] = copy.deepcopy(recipe.get("stats") or [])

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

    if "heartbeat_enabled" in operating_model and not preserve_runtime_mode:
        fields["heartbeat_enabled"] = bool(operating_model["heartbeat_enabled"])
    if operating_model.get("heartbeat_cadence"):
        fields["heartbeat_cadence"] = operating_model["heartbeat_cadence"]

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
        user_id=user_id,
        recipe=recipe,
        embedded=embedded,
        source_blueprint_id=bp.id,
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

    # Workspace settings are portable Blueprint semantics too. Keep them in a
    # private Draft field so a user field patch cannot spoof the install
    # contract, then materialize them during finalize_setup.
    blueprint_settings = operating_model.get("settings")
    if isinstance(blueprint_settings, dict):
        fields["_blueprint_settings"] = copy.deepcopy(blueprint_settings)
    else:
        fields.pop("_blueprint_settings", None)
    variable_declarations = [
        copy.deepcopy(item)
        for item in contract.get("variables") or []
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    ]
    if variable_declarations:
        fields["_blueprint_variable_declarations"] = variable_declarations
        fields["blueprint_personalization"] = {
            str(item["key"]): copy.deepcopy(item.get("default"))
            for item in variable_declarations
            if "default" in item and item.get("default") is not None
        }
    else:
        fields.pop("_blueprint_variable_declarations", None)
        fields.pop("blueprint_personalization", None)
    # Blueprint-backed drafts must not gain capabilities from descriptive
    # copy that the Blueprint did not explicitly declare.
    fields["_blueprint_disable_business_ledger_matching"] = True

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
        "stats",
        "rules",
        "automations",
        "evaluation",
        "budget_policy",
        "channel_config",
        "agent_mappings",
        "heartbeat_enabled",
        "heartbeat_cadence",
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
    fields["_blueprint_account_contract"] = {
        "variables": copy.deepcopy(variable_declarations),
        "requires": {
            "mcp_servers": copy.deepcopy(
                list((contract.get("requires") or {}).get("mcp_servers") or [])
            ),
        },
        "channels": copy.deepcopy(channels),
        "sessions": copy.deepcopy(list(contract.get("sessions") or [])),
    }
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
    user_id: str,
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
    draft = await get_draft(
        db, draft_id, entity_id, user_id, for_update=True,
    )
    if draft is None:
        raise ValueError("Draft not found")
    if draft.status == "finalized":
        if draft.finalized_workspace_id:
            return draft.finalized_workspace_id, draft
        raise ValueError("Draft already finalized but missing workspace id")
    if draft.status == "abandoned":
        raise ValueError("Draft was abandoned and cannot be finalized")
    lint_verified = await _refresh_missing_from_lint(db, draft)
    if not lint_verified:
        raise ValueError("Draft readiness could not be verified")
    if not draft.ready:
        missing = ", ".join(draft.missing or [])
        raise WorkspaceDraftNotReadyError(
            f"Draft not ready -- still missing: {missing or 'unknown fields'}"
        )

    draft_fields = dict(draft.fields or {})
    supplied_personalization: dict[str, Any] = {}
    variable_declarations = [
        dict(item)
        for item in draft_fields.get("_blueprint_variable_declarations") or []
        if isinstance(item, dict)
    ]
    if variable_declarations:
        supplied_personalization = dict(
            draft_fields.get("blueprint_personalization") or {}
        )
        declared_keys = {
            str(item.get("key") or "").strip()
            for item in variable_declarations
            if str(item.get("key") or "").strip()
        }
        supplied_personalization = {
            key: value
            for key, value in supplied_personalization.items()
            if key in declared_keys
        }
        from packages.core.blueprints.installer import (
            InstallError,
            resolve_install_variables,
        )

        materialization_fields = {
            key: copy.deepcopy(value)
            for key, value in draft_fields.items()
            if key not in {
                "_blueprint_variable_declarations",
                "blueprint_personalization",
                "_blueprint_account_contract",
                "_blueprint_channel_config_ids",
                "_blueprint_channel_requirements",
                "blueprint_channel_config_ids",
                "_blueprint_source_payload",
            }
        }
        try:
            resolved_wrapper, materialized_personalization = resolve_install_variables(
                {
                    "contract": {"variables": variable_declarations},
                    "draft_fields": materialization_fields,
                },
                supplied_personalization,
            )
        except InstallError as exc:
            raise WorkspaceDraftNotReadyError(str(exc)) from exc
        draft_fields.update(resolved_wrapper["draft_fields"])
        blueprint_settings = dict(draft_fields.get("_blueprint_settings") or {})
        if materialized_personalization:
            blueprint_settings["blueprint_personalization"] = copy.deepcopy(
                materialized_personalization
            )
        else:
            blueprint_settings.pop("blueprint_personalization", None)
        draft_fields["_blueprint_settings"] = blueprint_settings

    blueprint_payload = draft_fields.get("_blueprint_source_payload")
    resolved_blueprint_payload = None
    if draft.applied_blueprint_id:
        from packages.core.blueprints.installer import (
            InstallError,
            resolve_install_variables,
        )

        if not isinstance(blueprint_payload, dict):
            # Legacy drafts did not retain their source. Recover it only when
            # its fingerprint proves that it is still the applied content.
            blueprint = await db.get(WorkspaceBlueprint, draft.applied_blueprint_id)
            blueprint_payload = getattr(blueprint, "payload", None)
            metadata = draft_fields.get("_blueprint_install_metadata") or {}
            if not metadata.get(CONTENT_FINGERPRINT_KEY) or (
                blueprint_content_fingerprint(blueprint_payload)
                != metadata[CONTENT_FINGERPRINT_KEY]
            ):
                raise WorkspaceDraftNotReadyError(
                    "Blueprint source changed or is unavailable; reapply it before finalizing."
                )
        try:
            resolved_blueprint_payload, _ = resolve_install_variables(
                migrate_payload(blueprint_payload), supplied_personalization,
            )
        except (InstallError, PayloadError) as exc:
            raise WorkspaceDraftNotReadyError(str(exc)) from exc

    selected_channel_config_ids = {
        str(key): str(value)
        for key, value in dict(
            draft_fields.get("_blueprint_channel_config_ids") or {}
        ).items()
        if str(key).strip() and str(value).strip()
    }
    account_contract = draft_fields.get("_blueprint_account_contract")
    resolved_channels: list[dict[str, Any]] = []
    if isinstance(account_contract, dict):
        from packages.core.blueprints.installer import (
            InstallError,
            resolve_install_variables,
        )
        from packages.core.blueprints.setup_preflight import (
            BlueprintSetupPreflightError,
            BlueprintSetupPreflightFactory,
        )

        try:
            resolved_account_payload, _ = resolve_install_variables(
                {"contract": account_contract}, supplied_personalization,
            )
            resolved_channels = list(resolved_account_payload["contract"].get("channels") or [])
            selected_channel_config_ids = await BlueprintSetupPreflightFactory.resolve_channel_selections(
                db, channels=resolved_channels, entity_id=entity_id,
                user_id=user_id, selected=selected_channel_config_ids,
            )
        except (InstallError, BlueprintSetupPreflightError) as exc:
            raise WorkspaceDraftNotReadyError(str(exc)) from exc

    session = _session_from_draft(draft)
    session.fields = copy.deepcopy(draft_fields)
    session.fields.pop("_blueprint_source_payload", None)
    workspace_id = await finalize_setup(session, db, progress=progress)

    if selected_channel_config_ids:
        from packages.core.blueprints.installer import _bind_blueprint_channel_configs
        from packages.core.models.workspace import Workspace

        installed_workspace = await db.get(Workspace, workspace_id)
        if installed_workspace is not None:
            await _bind_blueprint_channel_configs(
                db,
                workspace=installed_workspace,
                channel_requirements=resolved_channels,
                selected_channel_config_ids=selected_channel_config_ids,
                user_id=user_id,
            )

    draft.fields = draft_fields
    flag_modified(draft, "fields")
    if draft.applied_blueprint_id:
        from packages.core.models.workspace import Workspace
        from packages.core.services.marketplace_resource_links import (
            RELATIONSHIP_INSTALLED_FROM,
            RESOURCE_WORKSPACE,
            RESOURCE_WORKSPACE_BLUEPRINT,
            SCOPE_WORKSPACE,
            record_marketplace_resource_link,
        )

        workspace = await db.get(Workspace, workspace_id)
        blueprint = await db.get(WorkspaceBlueprint, draft.applied_blueprint_id)
        if workspace is not None:
            install_metadata = dict(
                (draft.fields or {}).get("_blueprint_install_metadata") or {}
            )
            blueprint_version = install_metadata.get(BLUEPRINT_VERSION_KEY)
            if BLUEPRINT_VERSION_KEY not in install_metadata:
                blueprint_version = (
                    str(getattr(blueprint, "content_version", "") or "") or None
                )
            content_fingerprint = install_metadata.get(CONTENT_FINGERPRINT_KEY)
            if content_fingerprint is None:
                content_fingerprint = blueprint_content_fingerprint(
                    blueprint_payload,
                )
            section_fingerprints = install_metadata.get(SECTION_FINGERPRINTS_KEY)
            if not isinstance(section_fingerprints, dict):
                section_fingerprints = blueprint_section_fingerprints(
                    blueprint_payload,
                )
            settings = dict(workspace.settings or {})
            settings[BLUEPRINT_SETTINGS_KEY] = {
                "blueprint_id": draft.applied_blueprint_id,
                "blueprint_slug": getattr(blueprint, "slug", None),
                "title": getattr(blueprint, "title", None),
                "applied_via": "workspace_draft",
                "installed_at": datetime.now(timezone.utc).isoformat(),
                BLUEPRINT_VERSION_KEY: blueprint_version,
                CONTENT_FINGERPRINT_KEY: content_fingerprint,
                SECTION_FINGERPRINTS_KEY: section_fingerprints,
                UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: (
                    blueprint_upgrade_unsupported_fingerprint(blueprint_payload)
                ),
                MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: (
                    blueprint_upgrade_unsupported_fingerprint(
                        resolved_blueprint_payload
                    )
                ),
                "variable_declarations": variable_declarations,
                "portable_setting_keys": sorted(
                    key
                    for key in dict(
                        (draft.fields or {}).get("_blueprint_settings") or {}
                    )
                    if key != "blueprint_personalization"
                ),
            }
            workspace.settings = settings
            await record_marketplace_resource_link(
                db,
                entity_id=entity_id,
                marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
                marketplace_resource_id=draft.applied_blueprint_id,
                relationship=RELATIONSHIP_INSTALLED_FROM,
                scope_type=SCOPE_WORKSPACE,
                scope_id=workspace_id,
                local_resource_type=RESOURCE_WORKSPACE,
                local_resource_id=workspace_id,
                marketplace_version=blueprint_version,
                linked_by=user_id,
                metadata={"source_slug": getattr(blueprint, "slug", None)},
            )

    draft.status = "finalized"
    draft.finalized_workspace_id = workspace_id
    draft.finalized_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(draft)
    return workspace_id, draft


async def abandon_draft(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    user_id: str,
) -> bool:
    draft = await get_draft(
        db, draft_id, entity_id, user_id, for_update=True,
    )
    if draft is None:
        return False
    if draft.status not in {"active", "ready"}:
        return False
    draft.status = "abandoned"
    await db.flush()
    return True
