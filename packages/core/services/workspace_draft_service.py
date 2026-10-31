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

import asyncio
import copy
import json
import logging
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.services.workspace_draft_sources import (
    register_draft_sources,
    sources_transcript_note,
)

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
from packages.core.ai.runtime.control import RuntimeTurnAborted
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
    blueprint_delivery_is_paid,
    blueprint_delivery_source,
)
from packages.core.services.resource_access import (
    ResourceDescriptor,
    readable_resource_ids,
)

logger = logging.getLogger(__name__)


class WorkspaceDraftNotReadyError(ValueError):
    """A verified lint result invalidated a previously ready Draft."""


class WorkspaceDraftMaterializationError(ValueError):
    """A resource failure after provisioning whose savepoint has rolled back."""


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
    populate_existing: bool = False,
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
    if populate_existing:
        query = query.execution_options(populate_existing=True)
    result = await db.execute(query)
    return result.scalar_one_or_none()


# An Architect turn spans many model rounds (minutes). It owns its draft
# through this claim, renewed whenever it re-locks the row, instead of an open
# transaction: a transaction would keep the row lock, a pooled connection and
# the process's database admission permit through every model call.
TURN_CLAIM_FIELD = "_architect_turn"
TURN_CLAIM_TTL_SECONDS = 300.0
TURN_CLAIM_WAIT_SECONDS = 600.0
_TURN_CLAIM_POLL_SECONDS = 0.5


def _turn_claim_active(claim: Any, now: float) -> bool:
    if not isinstance(claim, dict):
        return False
    try:
        return float(claim.get("expires_at") or 0) > now
    except (TypeError, ValueError):
        return False


class DraftTurn:
    """One Architect turn's ownership of a draft between short transactions.

    ``claim`` waits (without holding a transaction) for any other live turn,
    then marks the draft as this turn's. Each draft write re-locks the row with
    ``lock`` and commits with ``release``; ``lock`` aborts the turn if the
    draft left an editable status or another turn took it over after this
    turn's claim expired. Other draft writers (field edits, Blueprint apply)
    are merged by re-reading the row at every lock.
    """

    def __init__(self, db: AsyncSession, *, draft_id: str, entity_id: str, user_id: str):
        self.db = db
        self.draft_id = draft_id
        self.entity_id = entity_id
        self.user_id = user_id
        self.claim_id = uuid.uuid4().hex
        self.claimed = False

    async def _load(self) -> Optional[WorkspaceDraft]:
        return await get_draft(
            self.db, self.draft_id, self.entity_id, self.user_id,
            for_update=True, populate_existing=True,
        )

    def _stamp(self, draft: WorkspaceDraft) -> None:
        fields = dict(draft.fields or {})
        fields[TURN_CLAIM_FIELD] = {
            "id": self.claim_id,
            "expires_at": time.time() + TURN_CLAIM_TTL_SECONDS,
        }
        draft.fields = fields
        flag_modified(draft, "fields")

    async def claim(self) -> WorkspaceDraft:
        deadline = time.monotonic() + TURN_CLAIM_WAIT_SECONDS
        while True:
            draft = await self._load()
            if draft is None:
                raise ValueError("Draft not found")
            if draft.status not in {"active", "ready"}:
                raise ValueError(f"Draft is {draft.status} and cannot be changed")
            if not _turn_claim_active((draft.fields or {}).get(TURN_CLAIM_FIELD), time.time()):
                self._stamp(draft)
                self.claimed = True
                return draft
            # Another turn is running (e.g. the opening stream this resumed
            # stream must replay). Wait for it without a transaction.
            await self.db.commit()
            if time.monotonic() >= deadline:
                raise ValueError("The Architect is still working on this draft. Try again shortly.")
            await asyncio.sleep(_TURN_CLAIM_POLL_SECONDS)

    async def lock(self) -> WorkspaceDraft:
        draft = await self._load()
        if draft is None:
            raise RuntimeTurnAborted("Draft not found")
        if draft.status not in {"active", "ready"}:
            raise RuntimeTurnAborted(f"Draft is {draft.status} and cannot be changed")
        claim = (draft.fields or {}).get(TURN_CLAIM_FIELD)
        if not isinstance(claim, dict) or claim.get("id") != self.claim_id:
            raise RuntimeTurnAborted(
                "Another Architect turn took over this draft; send the message again",
            )
        self._stamp(draft)
        return draft

    async def release(self) -> None:
        """Commit this turn's draft writes; no transaction stays open."""
        await self.db.commit()

    def finish(self, draft: WorkspaceDraft) -> None:
        """Drop the claim in the turn's final write (the caller commits it)."""
        fields = dict(draft.fields or {})
        if fields.pop(TURN_CLAIM_FIELD, None) is not None:
            draft.fields = fields
            flag_modified(draft, "fields")
        self.claimed = False

    async def abandon(self) -> None:
        """Best effort after a failed or cancelled turn; expiry covers crashes."""
        if not self.claimed:
            return
        try:
            await self.db.rollback()
            draft = await self._load()
            claim = (draft.fields or {}).get(TURN_CLAIM_FIELD) if draft is not None else None
            if isinstance(claim, dict) and claim.get("id") == self.claim_id:
                self.finish(draft)
            await self.db.commit()
        except Exception:  # noqa: BLE001 - never mask the turn's own error
            logger.warning("Could not release Architect turn claim for draft %s", self.draft_id, exc_info=True)
        finally:
            self.claimed = False


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
    document_ids: Optional[list[str]] = None,
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
        document_ids=document_ids,
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
    note = sources_transcript_note(
        s for s in (draft.fields or {}).get("sources", []) if s.get("turn_index") == 0
    )
    _record_visible_messages(
        draft,
        f"{opening}\n{note}" if note and opening != "begin" else opening,
        visible,
    )
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
    document_ids: Optional[list[str]] = None,
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
    if document_ids or normalized_brief:
        await register_draft_sources(
            db,
            draft=draft,
            entity_id=entity_id,
            user_id=user_id,
            message=normalized_brief,
            document_ids=list(document_ids or []),
        )
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
    document_ids: Optional[list[str]] = None,
) -> Tuple[str, WorkspaceDraft]:
    """Process one user turn against a persisted draft via the architect.

    The draft is never locked across model calls. The turn claims the draft
    (waiting for another live turn), commits, runs the Architect with every
    tool write in its own short locked transaction, then re-locks to record
    the transcript and readiness. The caller commits that final write.
    Tool writes already committed stay applied if the turn later fails.
    """
    turn = DraftTurn(db, draft_id=draft_id, entity_id=entity_id, user_id=user_id)
    draft = await turn.claim()
    try:
        # Opening streams are resumable.  If the first connection committed
        # its turn but the browser missed the terminal SSE frame, replay the
        # committed assistant text instead of spending another architect turn
        # or appending a duplicate opening exchange.  The claim serializes
        # this check with an opening stream that is still finishing.
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
                turn.finish(draft)
                if stream_handler is not None:
                    await stream_handler("text_delta", {"content": previous_reply})
                return previous_reply, draft

        added_sources = await register_draft_sources(
            db,
            draft=draft,
            entity_id=entity_id,
            user_id=user_id,
            message=user_message,
            document_ids=list(document_ids or []),
        )
        await turn.release()
        visible = await _architect_turn(
            db,
            draft=draft,
            entity_id=entity_id,
            user_id=draft.user_id,
            user_message=user_message,
            stream_handler=stream_handler,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            turn=turn,
        )
        draft = await turn.lock()
        note = sources_transcript_note(added_sources)
        _record_visible_messages(
            draft,
            f"{user_message}\n{note}" if note else user_message,
            visible,
        )
        turn.finish(draft)
        await _refresh_missing_from_lint(db, draft)
        await db.flush()
        await db.refresh(draft)
        return visible, draft
    except BaseException:
        await turn.abandon()
        raise


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
    repair_service_keys: tuple[str, ...] | None = None,
    turn: DraftTurn | None = None,
) -> str:
    """Invoke the typed-tool architect for one turn. Mutations land on
    ``draft.fields`` via tool calls; this returns the visible reply.

    With ``turn``, no transaction is held between tool calls; without it the
    caller's transaction (and draft lock) spans the turn, as before."""

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
        **({"repair_service_keys": repair_service_keys} if repair_service_keys is not None else {}),
        **({"turn": turn} if turn is not None else {}),
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
    *,
    issues_out: list[dict[str, Any]] | None = None,
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
    if issues_out is not None:
        issues_out.extend(p0_issues)
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


async def _repair_agent_bindings_before_finalize(
    db: AsyncSession,
    draft: WorkspaceDraft,
    issues: list[dict[str, Any]],
    *,
    progress: Optional[Any] = None,
) -> bool:
    """Give generated binding failures one scoped repair before creating rows.

    User choices, setup requirements and frozen Blueprint content are never
    repaired by this path. The normal Architect Runtime owns model billing,
    tool authorization and concurrent-edit revalidation.
    """
    if draft.applied_blueprint_id or draft.missing != ["agent_mappings"] or not issues:
        return True
    mappings = {
        str(mapping.get("service_key")): mapping
        for mapping in (draft.fields or {}).get("agent_mappings") or []
        if isinstance(mapping, dict) and mapping.get("strategy") == "create_custom"
        and mapping.get("service_key")
    }
    binding_fields = ("tool_bindings", "business_capabilities", "skill_bindings", "mcp_bindings")
    affected: set[str] = set()
    for issue in issues:
        matched = next((
            key for key in mappings
            if issue.get("where") in {
                f"agent_mappings.{key}.{field}" for field in binding_fields
            }
        ), None)
        if matched is None:
            return True
        affected.add(matched)

    from packages.core.services.plan_gate import enforce_workspace_capacity

    # Reject a full plan before spending on repair; materialization rechecks it.
    await enforce_workspace_capacity(db, draft.entity_id)
    service_keys = tuple(sorted(affected))
    if progress is not None:
        progress("agent_bindings_repair_started", {"service_keys": list(service_keys)})
    reply = await _architect_turn(
        db,
        draft=draft,
        entity_id=draft.entity_id,
        user_id=draft.user_id,
        user_message=json.dumps({
            "operation": "repair_agent_bindings_before_creation",
            "validation_issues": issues,
            "agent_mappings": [mappings[key] for key in service_keys],
        }, ensure_ascii=False),
        repair_service_keys=service_keys,
    )
    verified = await _refresh_missing_from_lint(db, draft)
    if verified and draft.ready:
        _record_visible_messages(draft, "", reply)
    if progress is not None:
        progress("agent_bindings_repair_done", {"ready": verified and draft.ready})
    return verified


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
    purchased = purchase is not None

    paid_delivery = blueprint_delivery_is_paid(bp, purchase)

    # A completed purchase keeps the blueprint usable even after the
    # seller archives/unpublishes it (spec §4.3/§5.3).
    if bp.status != BlueprintStatus.PUBLISHED and not purchased:
        raise ValueError("Blueprint not available")

    # Applying a paid Blueprint to a draft is an install path and requires a
    # completed purchase (the router maps PermissionError to 402).
    if paid_delivery and bp.entity_id != entity_id and not purchased:
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

    from packages.core.governance.defaults import normalize_default_workspace_governance
    from packages.core.governance.policy import policy_to_dict

    from packages.core.governance.approval_projection import import_blueprint_governance
    imported_policy, imported_rules = import_blueprint_governance(policy.get("governance") or {}, operating_model.get("rules") or [])
    operating_rules, default_governance, _ = normalize_default_workspace_governance(imported_rules, imported_policy)
    if "rules" in operating_model:
        operating_model["rules"] = operating_rules

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
            if list_key == "rules":
                # A template must not inherit a previous rule's user-confirmed
                # hard-block receipt, even if its key and contents match.
                fields.pop("_explicit_hard_block_rules", None)

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
    # These are portable native resource declarations, not a second
    # implementation hidden in operating_model.  finalize_draft materializes
    # them through the same installer primitives as direct Blueprint install.
    fields["workflows"] = copy.deepcopy(recipe.get("workflows") or [])
    fields["stats"] = copy.deepcopy(recipe.get("stats") or [])
    fields["scheduled_jobs"] = copy.deepcopy(recipe.get("scheduled_jobs") or [])

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

    if isinstance(operating_model.get("app_view"), dict):
        fields["app_view"] = copy.deepcopy(operating_model["app_view"])

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

    if policy.get("governance") or operating_rules:
        fields["governance_policy"] = policy_to_dict(default_governance)

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
        "app_view",
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
    # A live Architect turn no longer holds the row lock between its writes;
    # do not materialize a half-applied turn.
    if _turn_claim_active((draft.fields or {}).get(TURN_CLAIM_FIELD), time.time()):
        raise ValueError("The Architect is still working on this draft. Try again when it finishes.")
    issues: list[dict[str, Any]] = []
    lint_verified = await _refresh_missing_from_lint(db, draft, issues_out=issues)
    if not lint_verified:
        raise ValueError("Draft readiness could not be verified")
    if not draft.ready:
        try:
            lint_verified = await _repair_agent_bindings_before_finalize(
                db, draft, issues, progress=progress,
            )
        except RuntimeTurnAborted:
            # Capability matching releases the draft lock during provider work.
            # A concurrent finalize may have succeeded before we reacquire it.
            current = await get_draft(db, draft_id, entity_id, user_id, for_update=True)
            if current is not None and current.status == "finalized" and current.finalized_workspace_id:
                return current.finalized_workspace_id, current
            raise
        if not lint_verified:
            raise ValueError("Draft readiness could not be verified after Agent binding repair")
    if not draft.ready:
        missing = ", ".join(draft.missing or [])
        raise WorkspaceDraftNotReadyError(
            f"Draft not ready -- still missing: {missing or 'unknown fields'}"
        )

    draft_fields = dict(draft.fields or {})
    # Compatibility for Drafts saved before portable resources became
    # first-class fields.  Only fill absent fields from the immutable source;
    # never overwrite a user's subsequent Draft edits.
    source_recipe = (
        ((draft_fields.get("_blueprint_source_payload") or {}).get("recipe") or {})
        if isinstance(draft_fields.get("_blueprint_source_payload"), dict) else {}
    )
    for field, recipe_key in (
        ("workflows", "workflows"),
        ("scheduled_jobs", "scheduled_jobs"),
    ):
        if field not in draft_fields and isinstance(source_recipe.get(recipe_key), list):
            draft_fields[field] = copy.deepcopy(source_recipe[recipe_key])
    legacy_model = draft_fields.get("_blueprint_operating_model")
    if "app_view" not in draft_fields and isinstance(legacy_model, dict) and isinstance(legacy_model.get("app_view"), dict):
        draft_fields["app_view"] = copy.deepcopy(legacy_model["app_view"])
    try:
        from packages.core.blueprints.installer import validate_portable_recipe_resources
        validate_portable_recipe_resources({
            "workflows": draft_fields.get("workflows") or [],
            "scheduled_jobs": draft_fields.get("scheduled_jobs") or [],
        })
    except ValueError as exc:
        raise WorkspaceDraftNotReadyError(f"Draft resources are invalid: {exc}") from exc
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
    # Older Draft UI represents imported jobs as automation cards.  Their
    # canonical scheduled_jobs declaration is materialized below, so do not
    # let the legacy setup adapter create a second scheduler row first.
    scheduled_job_keys = {
        str(item.get("job_id") or "").strip()
        for item in draft_fields.get("scheduled_jobs") or []
        if isinstance(item, dict)
    }
    if scheduled_job_keys:
        session.fields["automations"] = [
            item for item in session.fields.get("automations") or []
            if not isinstance(item, dict)
            or str(item.get("automation_key") or "").strip() not in scheduled_job_keys
        ]
    # Keep shell provisioning and portable resources atomic even for a caller
    # that catches this error and commits its outer transaction for another
    # reason.  Lint corrections happened before this savepoint and remain
    # retryable; a Workspace shell never survives a failed resource install.
    materialization_savepoint = await db.begin_nested()
    try:
        workspace_id = await finalize_setup(session, db, progress=progress)
    except BaseException:
        if materialization_savepoint.is_active:
            await materialization_savepoint.rollback()
        raise

    # Drafts may originate from the Architect or from an applied Blueprint.
    # In both cases portable resources are materialized through the native
    # Blueprint installer primitives, after the Workspace shell exists and in
    # the caller's transaction.  This is intentionally separate from the
    # legacy Draft `automations` prose field, which is not an executable job.
    resource_recipe = {
        "workflows": draft_fields.get("workflows") or [],
        "scheduled_jobs": draft_fields.get("scheduled_jobs") or [],
    }
    if resource_recipe["workflows"] or resource_recipe["scheduled_jobs"] or isinstance(draft_fields.get("app_view"), dict):
        from packages.core.blueprints.installer import (
            InstallError,
            InstallMode,
            blueprint_workflow_installation_source_id,
            materialize_workspace_recipe_resources,
        )
        from packages.core.models.workspace import Agent, AgentSubscription, Workspace
        from packages.core.models.workspace_stat import WorkspaceStat

        try:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None:
                raise WorkspaceDraftMaterializationError("Workspace creation did not return a Workspace row")
            source_key = str(draft.applied_blueprint_id or draft.id)
            materialized = await materialize_workspace_recipe_resources(
                db, entity_id=entity_id, workspace=workspace, recipe=resource_recipe,
                user_id=user_id, mode=InstallMode.LIVE,
                source_template_id=blueprint_workflow_installation_source_id(
                    f"workspace-draft:{source_key}", workspace_id,
                ),
            )
            if isinstance(draft_fields.get("app_view"), dict):
                from packages.core.blueprints.app_view import remap_app_view
                from packages.core.blueprints.exporter import _portable_agent_slug
                from packages.core.blueprints.installer import installed_blueprint_job_id
                app_agents = (await db.execute(
                    select(Agent).join(AgentSubscription, AgentSubscription.agent_id == Agent.id).where(
                        AgentSubscription.workspace_id == workspace_id,
                        AgentSubscription.entity_id == entity_id,
                        AgentSubscription.status == "active",
                    )
                )).scalars().all()
                materialized_stat_keys = set((await db.execute(
                    select(WorkspaceStat.key).where(
                        WorkspaceStat.workspace_id == workspace_id,
                        WorkspaceStat.entity_id == entity_id,
                        WorkspaceStat.status == "active",
                    )
                )).scalars())
                operating_model = dict(workspace.operating_model or {})
                operating_model["app_view"] = remap_app_view(
                    draft_fields["app_view"],
                    workflows=dict(materialized["workflow_binding_ids"]),
                    workflow_definitions=dict(materialized["workflow_ids"]),
                    stat_keys=materialized_stat_keys,
                    agents={_portable_agent_slug(agent): agent.id for agent in app_agents},
                    automations={
                        str(job.get("job_id")): installed_blueprint_job_id(
                            str(job.get("job_id")), workspace_id,
                        )
                        for job in draft_fields.get("scheduled_jobs") or [] if isinstance(job, dict) and job.get("job_id")
                    },
                )
                workspace.operating_model = operating_model
        except BaseException as exc:
            if materialization_savepoint.is_active:
                await materialization_savepoint.rollback()
            # Preserve cancellation/system exceptions while giving ordinary
            # resource failures one retriable, caller-visible contract.
            if isinstance(exc, Exception) and not isinstance(exc, WorkspaceDraftMaterializationError):
                raise WorkspaceDraftMaterializationError(str(exc)) from exc
            raise

    await materialization_savepoint.commit()

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
                "author_hard_blocks_installed": False,
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
            from packages.core.blueprints.registry import sync_installed_package

            await sync_installed_package(db, settings[BLUEPRINT_SETTINGS_KEY])
            workspace.settings = settings
            from packages.core.blueprints.reconfigure import capture_install_baseline

            await capture_install_baseline(
                db, workspace=workspace, payload=resolved_blueprint_payload
            )
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
