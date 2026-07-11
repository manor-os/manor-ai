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
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.ai.runtime import (
    runtime_lint_workspace_draft,
    runtime_reconcile_workspace_draft_fields,
    runtime_run_workspace_architect_turn,
)
from packages.core.models.blueprint import WorkspaceBlueprint
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
    db: AsyncSession, draft_id: str, entity_id: str,
) -> Optional[WorkspaceDraft]:
    result = await db.execute(
        select(WorkspaceDraft).where(
            WorkspaceDraft.id == draft_id,
            WorkspaceDraft.entity_id == entity_id,
        )
    )
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
    await _maybe_suggest_blueprint(db, draft)
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
    await _maybe_suggest_blueprint(db, draft)
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


async def apply_blueprint(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    blueprint_id: str,
) -> WorkspaceDraft:
    """Merge a blueprint's payload into the draft's fields.

    Payload schema is loose by design (see
    ``packages/core/blueprints/payload.py``); we copy whichever
    top-level keys overlap with our DEFAULT_FIELDS plus the operating
    model lists.
    """
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
    if bp.status != "published" and not purchased:
        raise ValueError("Blueprint not available")

    # Paid gate: the payload IS the paid product. Merging it into a draft
    # would leak it to non-purchasers (the router maps PermissionError
    # to 402).
    if (bp.price_cents or 0) > 0 and bp.entity_id != entity_id and not purchased:
        raise PermissionError("purchase required to apply this blueprint")

    payload = dict(bp.payload or {})
    ws_block = payload.get("workspace") or {}

    fields = dict(draft.fields or DEFAULT_FIELDS)
    for key in ("name", "kind", "operating_context", "primary_work"):
        if not fields.get(key) and ws_block.get(key):
            fields[key] = ws_block[key]
    for list_key in ("services", "goals", "rules", "automations"):
        if payload.get(list_key):
            fields[list_key] = payload[list_key]
    if payload.get("evaluation"):
        fields["evaluation"] = payload["evaluation"]
    if payload.get("channel_config"):
        fields["channel_config"] = payload["channel_config"]
    if payload.get("agent_mappings"):
        fields["agent_mappings"] = payload["agent_mappings"]
    if payload.get("knowledge_attachments"):
        fields["knowledge_attachments"] = payload["knowledge_attachments"]
    if payload.get("governance_policy"):
        fields["governance_policy"] = payload["governance_policy"]
    if payload.get("knowledge"):
        fields["knowledge"] = payload["knowledge"]

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

    Pass ``progress=callable(step, payload)`` to receive incremental
    finalize checkpoints (workspace_created, agent_provisioned,
    runtime_scheduled, strategist_dispatched, complete) -- the SSE
    streaming endpoint uses this to drive a per-step UI.
    """
    draft = await get_draft(db, draft_id, entity_id)
    if draft is None:
        raise ValueError("Draft not found")
    if draft.status == "finalized":
        if draft.finalized_workspace_id:
            return draft.finalized_workspace_id, draft
        raise ValueError("Draft already finalized but missing workspace id")
    if not draft.ready:
        missing = ", ".join(draft.missing or [])
        raise ValueError(
            f"Draft not ready -- still missing: {missing or 'unknown fields'}"
        )

    reconcile_draft_fields(draft)
    session = _session_from_draft(draft)
    workspace_id = await finalize_setup(session, db, progress=progress)

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


# ---------------------------------------------------------------------------
# Blueprint suggestion (lightweight keyword scoring)
# ---------------------------------------------------------------------------

# Token threshold below which we don't bother proposing a blueprint --
# the user hasn't told us enough yet for the match to be meaningful.
_MIN_INTENT_TOKENS = 3
_TOP_BLUEPRINTS_LIMIT = 24


async def _maybe_suggest_blueprint(
    db: AsyncSession, draft: WorkspaceDraft,
) -> None:
    """Best-effort blueprint suggestion. Sets ``suggested_blueprint_id``
    on the draft if a published blueprint scores above a small threshold.

    Skipped when the user already accepted a blueprint or when the draft
    has too little content to match against.
    """
    if draft.applied_blueprint_id:
        return
    if draft.suggested_blueprint_id:
        return  # Don't churn the suggestion mid-conversation.

    intent_tokens = _intent_tokens_from_fields(draft.fields)
    if len(intent_tokens) < _MIN_INTENT_TOKENS:
        return

    result = await db.execute(
        select(WorkspaceBlueprint)
        .where(WorkspaceBlueprint.status == "published")
        .order_by(WorkspaceBlueprint.install_count.desc())
        .limit(_TOP_BLUEPRINTS_LIMIT)
    )
    candidates = result.scalars().all()
    if not candidates:
        return

    best: Optional[WorkspaceBlueprint] = None
    best_score = 0
    for bp in candidates:
        score = _score_blueprint_match(bp, intent_tokens)
        if score > best_score:
            best = bp
            best_score = score

    if best is not None and best_score >= 2:
        draft.suggested_blueprint_id = best.id


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: Any) -> List[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(str(text).lower())


def _intent_tokens_from_fields(fields: Optional[Dict[str, Any]]) -> set[str]:
    if not fields:
        return set()
    parts: List[str] = []
    for key in ("name", "kind", "operating_context", "primary_work"):
        v = fields.get(key)
        if v:
            parts.append(str(v))
    services = fields.get("services") or []
    for svc in services:
        if isinstance(svc, dict):
            parts.append(str(svc.get("service_key", "")))
            parts.append(str(svc.get("description", "")))
    return set(_tokenize(" ".join(parts)))


def _score_blueprint_match(bp: WorkspaceBlueprint, intent: set[str]) -> int:
    if not intent:
        return 0
    haystack: List[str] = [bp.title or "", bp.summary or ""]
    tags = bp.tags or []
    if isinstance(tags, list):
        haystack.extend(str(t) for t in tags)
    bp_tokens = set(_tokenize(" ".join(haystack)))
    return len(intent & bp_tokens)
