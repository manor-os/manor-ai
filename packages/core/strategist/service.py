"""Strategist orchestration entry point.

``run_review(workspace_id)`` does one full cycle:

  1. Load workspace + check it exists.
  2. Dedupe: if a review just ran in the last `min_gap` seconds AND
     produced open proposals, skip — the operator hasn't acted yet.
  3. Gather context (goals, tasks, memory).
  4. Single Claude call → validated Proposal.
  5. Cross-check service_keys against the workspace allowlist.
  6. Write Task rows with status='proposed', tagged with review_id.
  7. Post a single proposal card in workspace_chat with a
     ``pending_action`` so the operator can [Approve all] / [Reject].
  8. Optionally write a ``learning`` memory note about the review.

``approve_proposal`` / ``reject_proposal`` mutate the cohort all-at-once
when the operator clicks the chat card.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import Workspace
from packages.core.ai.runtime import runtime_strategist_review_billing_context
from packages.core.ai.runtime.task_requirements import merge_task_runtime_capabilities
from packages.core.services.task_dependencies import dependency_ids_from_details, details_with_dependency_state
from packages.core.services.task_service import update_task
from packages.core.services.workspace_work_reconciliation import (
    reconcile_active_work_batches,
    stale_reconciliation_results,
)
from packages.core.strategist.context import gather_context
from packages.core.strategist.prompt import generate_proposal
from packages.core.strategist.proposal import Proposal, ProposedTask
from packages.core.workspace_chat import service as chat_service

logger = logging.getLogger(__name__)

_SOCIAL_PROVIDER_LABELS = {
    "twitter_x": "X/Twitter",
    "linkedin": "LinkedIn",
    "linkedin_browser": "LinkedIn",
    "facebook": "Facebook",
}
_STRATEGIST_SETTINGS_KEY = "strategist"
_AUTO_APPROVE_PROPOSALS_KEY = "auto_approve_proposals"


class StrategistError(Exception):
    pass


# ── Main entry ────────────────────────────────────────────────────────

async def run_review(
    db: AsyncSession,
    workspace_id: str,
    *,
    trigger: str = "scheduled",
    skip_if_open_proposals: bool = True,
) -> dict:
    """Run one Strategist review cycle. Caller commits."""
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        raise StrategistError(
            f"workspace {workspace_id} not found (or in soft-delete trash)",
        )

    # Paused workspaces don't get reviews
    if workspace.status != "active":
        logger.info("Strategist: workspace %s is %s — skipping", workspace_id, workspace.status)
        return {"workspace_id": workspace_id, "skipped": True, "reason": f"workspace_{workspace.status}"}

    measurement_refresh = await _refresh_internal_goal_measurements_for_review(db, workspace)
    if measurement_refresh["measured"] or measurement_refresh["errors"]:
        # Strategist must reason over the latest execution evidence, including
        # cases where the review later skips because an active work batch or
        # open proposal still needs operator attention.
        await db.commit()

    work_batch_reconciliation = await reconcile_active_work_batches(db, workspace)
    stale_work_batches = stale_reconciliation_results(work_batch_reconciliation)
    if work_batch_reconciliation:
        # Persist completion/stall state before any early return below.
        await db.commit()

    if trigger == "scheduled":
        active_batch = await _active_work_batch_with_open_tasks(db, workspace)
        if active_batch:
            logger.info(
                "Strategist: active work batch %s still has %d open task(s); skipping scheduled review",
                active_batch["batch_id"],
                len(active_batch["open_task_ids"]),
            )
            return {
                "workspace_id": workspace_id,
                "skipped": True,
                "reason": "active_work_batch",
                **active_batch,
            }

    ctx = await gather_context(db, workspace, trigger=trigger)
    ctx.work_batch_reconciliation = stale_work_batches
    starter_cleanup = await _resolve_fulfilled_starter_document_proposals(
        db,
        workspace,
        ctx,
    )
    if starter_cleanup.get("task_ids"):
        await db.commit()
        removed_ids = set(starter_cleanup["task_ids"])
        ctx.open_proposed_tasks = [
            task for task in ctx.open_proposed_tasks if task.id not in removed_ids
        ]

    # Dedupe: if the last review still has unaccepted proposals, don't
    # pile on more — the operator already has decisions to make.
    if skip_if_open_proposals and ctx.open_proposed_tasks:
        logger.info(
            "Strategist: %d proposals from prior review still open; skipping",
            len(ctx.open_proposed_tasks),
        )
        return {
            "workspace_id": workspace_id,
            "skipped": True,
            "reason": "open_proposals",
            "open_count": len(ctx.open_proposed_tasks),
        }

    # Operator-declared skip conditions (recipe.strategist.cadence.
    # trigger_conditions.skip_if_any). Evaluated BEFORE the LLM call so
    # a 'skip on weekend' rule doesn't burn credits.
    matched = _evaluate_skip_conditions(ctx)
    if matched is not None:
        logger.info(
            "Strategist: trigger_condition %r matched; skipping",
            matched,
        )
        return {
            "workspace_id": workspace_id,
            "skipped": True,
            "reason": "trigger_condition",
            "expression": matched,
        }

    review_id = "rv_" + generate_ulid()

    async with runtime_strategist_review_billing_context(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
    ):
        proposal = await generate_proposal(ctx, review_id=review_id, db=db)

    _sanitize_governance_language(proposal, ctx)
    _suppress_starter_document_proposals(proposal, ctx)
    _enforce_allowlists(proposal, ctx.allowed_service_keys)
    _enforce_integration_scope(proposal, ctx)
    _enforce_proposal_shape(proposal, ctx)

    auto_approve_proposals = proposal_auto_approval_enabled(workspace)

    # Write Task rows + collect their ids.
    new_task_ids = await _persist_tasks(db, workspace, proposal)
    await _record_strategist_review_evidence(
        db,
        workspace=workspace,
        proposal=proposal,
        trigger=trigger,
        task_ids=new_task_ids,
        ctx=ctx,
    )

    approved_task_ids: list[str] = []
    if auto_approve_proposals and new_task_ids:
        approved_task_ids = await approve_proposal(
            db,
            entity_id=workspace.entity_id,
            review_id=review_id,
            only_task_ids=new_task_ids,
        )

    # Always commit before posting to chat — the chat post opens its
    # own session and shouldn't see uncommitted tasks.
    await db.commit()

    # Post the proposal card. Best-effort.
    if new_task_ids or proposal.notes:
        await _post_proposal_chat(
            workspace,
            proposal,
            new_task_ids,
            auto_approved=bool(approved_task_ids),
        )

    return {
        "workspace_id": workspace_id,
        "review_id": review_id,
        "task_count": len(new_task_ids),
        "task_ids": new_task_ids,
        "auto_approved": bool(approved_task_ids),
        "approved_task_ids": approved_task_ids,
        "summary": proposal.summary,
        "notes": proposal.notes,
    }


# ── Approval ──────────────────────────────────────────────────────────

async def approve_proposal(
    db: AsyncSession,
    *,
    entity_id: str,
    review_id: str,
    only_task_ids: Optional[list[str]] = None,
) -> list[str]:
    """Approve proposed tasks and start only dependency-ready work.

    Tasks with no predecessors flip to ``in_progress`` immediately, which
    fires the Planner hook. Dependent tasks stay ``pending`` until their
    predecessors complete and ``workspace_operation_service`` releases them.
    ``only_task_ids`` lets the operator approve a subset.
    """
    all_rows = await _find_proposed(db, entity_id, review_id, None)
    rows = _selected_rows_with_required_dependencies(all_rows, only_task_ids)
    moved: list[str] = []
    batch_id: str | None = None
    if rows:
        batch_id = await _create_proposal_work_batch(db, rows, review_id=review_id)
    for t in rows:
        details = dict(t.details or {})
        if batch_id:
            details["workspace_work_batch_id"] = batch_id
        dep_ids = dependency_ids_from_details(details)
        if dep_ids:
            details = await details_with_dependency_state(db, t, details)
        gate_status = str(details.get("dependency_status") or "completed")
        if dep_ids and gate_status != "completed":
            # ``pending`` is approved-but-not-started. It does not trigger the
            # Planner; the dependency gate releases it to ``in_progress`` later.
            next_status = "pending"
        else:
            # Setting in_progress triggers plan_and_run_task via the hook
            # in update_task() (fires when status == "in_progress" and
            # task.owner_subscription_id is set).
            next_status = "in_progress"
        await update_task(db, t.id, entity_id, status=next_status, details=details)
        moved.append(t.id)
    if moved:
        await _record_proposal_approval_activity(
            db,
            tasks=rows,
            review_id=review_id,
            task_ids=moved,
            batch_id=batch_id,
        )
    return moved


async def reject_proposal(
    db: AsyncSession,
    *,
    entity_id: str,
    review_id: str,
    only_task_ids: Optional[list[str]] = None,
    reason: Optional[str] = None,
) -> list[str]:
    rows = await _find_proposed(db, entity_id, review_id, only_task_ids)
    cancelled: list[str] = []
    for t in rows:
        details = dict(t.details or {})
        if reason:
            details["rejection_reason"] = reason
        await update_task(
            db, t.id, entity_id,
            status="cancelled", details=details,
        )
        cancelled.append(t.id)
    return cancelled


def proposal_auto_approval_enabled(workspace: Workspace) -> bool:
    settings = workspace.settings if isinstance(workspace.settings, dict) else {}
    strategist = settings.get(_STRATEGIST_SETTINGS_KEY)
    if not isinstance(strategist, dict):
        return False
    return strategist.get(_AUTO_APPROVE_PROPOSALS_KEY) is True


async def set_proposal_auto_approval(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    enabled: bool,
    changed_by: str | None = None,
) -> Workspace:
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        ).with_for_update()
    )).scalar_one_or_none()
    if workspace is None:
        raise StrategistError(f"workspace {workspace_id} not found")

    settings = dict(workspace.settings or {})
    raw_strategist_settings = settings.get(_STRATEGIST_SETTINGS_KEY)
    strategist_settings = (
        dict(raw_strategist_settings)
        if isinstance(raw_strategist_settings, dict)
        else {}
    )
    strategist_settings[_AUTO_APPROVE_PROPOSALS_KEY] = bool(enabled)
    now_iso = datetime.now(timezone.utc).isoformat()
    if enabled:
        strategist_settings["auto_approve_proposals_set_at"] = now_iso
        if changed_by:
            strategist_settings["auto_approve_proposals_set_by"] = changed_by
    else:
        strategist_settings["auto_approve_proposals_disabled_at"] = now_iso
        if changed_by:
            strategist_settings["auto_approve_proposals_disabled_by"] = changed_by
    settings[_STRATEGIST_SETTINGS_KEY] = strategist_settings
    workspace.settings = settings

    try:
        from packages.core.services.workspace_service import record_activity

        await record_activity(
            db,
            workspace_id,
            entity_id,
            event_type="strategist_proposal.auto_approval_enabled" if enabled else "strategist_proposal.auto_approval_disabled",
            summary=(
                "Strategist proposals will be approved automatically."
                if enabled
                else "Strategist proposal auto-approval was disabled."
            ),
            details={
                "auto_approve_proposals": bool(enabled),
                "changed_by": changed_by,
            },
            user_id=changed_by,
        )
    except Exception:
        logger.debug("Strategist: failed to record proposal auto-approval setting activity", exc_info=True)

    await db.flush()
    return workspace


async def _find_proposed(
    db: AsyncSession, entity_id: str, review_id: str,
    only_task_ids: Optional[list[str]],
) -> list[Task]:
    stmt = select(Task).where(
        Task.entity_id == entity_id,
        Task.status == "proposed",
        Task.details["strategist_review_id"].astext == review_id,
    )
    if only_task_ids:
        stmt = stmt.where(Task.id.in_(only_task_ids))
    stmt = stmt.order_by(Task.created_at.asc(), Task.id.asc())
    return list((await db.execute(stmt)).scalars().all())


def _selected_rows_with_required_dependencies(
    rows: list[Task],
    only_task_ids: Optional[list[str]],
) -> list[Task]:
    """Return selected proposal tasks plus same-review prerequisite tasks.

    Operator selection should not create an impossible work wave. If the user
    approves a downstream task, the strategist-owned prerequisites from the
    same proposal cohort are approved with it.
    """
    if not only_task_ids:
        return rows

    by_id = {task.id: task for task in rows}
    selected: set[str] = {task_id for task_id in only_task_ids if task_id in by_id}
    stack = list(selected)
    while stack:
        task_id = stack.pop()
        task = by_id.get(task_id)
        if task is None:
            continue
        for dep_id in dependency_ids_from_details(task.details):
            if dep_id not in by_id or dep_id in selected:
                continue
            selected.add(dep_id)
            stack.append(dep_id)
    return [task for task in rows if task.id in selected]


async def _resolve_fulfilled_starter_document_proposals(
    db: AsyncSession,
    workspace: Workspace,
    ctx,
) -> dict:
    """Mark setup-owned starter knowledge proposals obsolete once docs exist."""
    fulfilled_keys = {
        str(net.get("starter_task_key"))
        for net in getattr(ctx, "knowledge_nets", []) or []
        if net.get("starter_task_key")
        and (
            str(net.get("starter_document_status") or "").lower() == "ready"
            or int(net.get("document_count") or 0) > 0
        )
    }
    if not fulfilled_keys:
        return {"task_ids": [], "review_ids": []}

    candidates = list((await db.execute(
        select(Task).where(
            Task.workspace_id == workspace.id,
            Task.entity_id == workspace.entity_id,
            Task.status == "proposed",
        )
    )).scalars().all())
    rows = [
        task for task in candidates
        if str((task.details or {}).get("strategist_task_key") or "") in fulfilled_keys
    ]
    if not rows:
        return {"task_ids": [], "review_ids": []}

    task_ids: list[str] = []
    review_ids = {
        str((task.details or {}).get("strategist_review_id"))
        for task in rows
        if (task.details or {}).get("strategist_review_id")
    }
    reason = "Obsolete: workspace setup already generated the starter knowledge document."
    for task in rows:
        details = dict(task.details or {})
        details["obsolete_reason"] = "fulfilled_by_workspace_starter_document"
        details["rejection_reason"] = reason
        await update_task(
            db,
            task.id,
            workspace.entity_id,
            status="cancelled",
            details=details,
        )
        task_ids.append(task.id)

    if review_ids:
        await _sync_proposal_cards_after_starter_cleanup(
            db,
            workspace,
            review_ids=review_ids,
            obsolete_task_ids=set(task_ids),
        )
    return {"task_ids": task_ids, "review_ids": sorted(review_ids)}


async def _sync_proposal_cards_after_starter_cleanup(
    db: AsyncSession,
    workspace: Workspace,
    *,
    review_ids: set[str],
    obsolete_task_ids: set[str],
) -> None:
    candidate_messages = list((await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.workspace_id == workspace.id,
            Conversation.entity_id == workspace.entity_id,
            Message.resolved_at.is_(None),
            Message.pending_action.isnot(None),
        )
    )).scalars().all())
    messages = [
        msg for msg in candidate_messages
        if isinstance(msg.pending_action, dict)
        and msg.pending_action.get("kind") == "approve_proposals"
        and str(msg.pending_action.get("review_id") or "") in review_ids
    ]
    if not messages:
        return

    referenced_ids: set[str] = set()
    for msg in messages:
        action = msg.pending_action if isinstance(msg.pending_action, dict) else {}
        referenced_ids.update(str(task_id) for task_id in action.get("task_ids") or [])

    if referenced_ids:
        remaining_rows = list((await db.execute(
            select(Task).where(
                Task.entity_id == workspace.entity_id,
                Task.workspace_id == workspace.id,
                Task.id.in_(referenced_ids - obsolete_task_ids),
                Task.status == "proposed",
            )
        )).scalars().all())
    else:
        remaining_rows = []
    by_id = {task.id: task for task in remaining_rows}

    now = datetime.now(timezone.utc)
    for msg in messages:
        action = dict(msg.pending_action or {})
        original_ids = [str(task_id) for task_id in action.get("task_ids") or []]
        remaining = [by_id[task_id] for task_id in original_ids if task_id in by_id]
        removed_count = len([task_id for task_id in original_ids if task_id in obsolete_task_ids])
        if not removed_count:
            continue

        if remaining:
            action["task_ids"] = [task.id for task in remaining]
            action["task_titles"] = [task.title for task in remaining]
            msg.pending_action = action
            msg.refs = [{"type": "task", "id": task.id} for task in remaining]
            msg.content = _proposal_card_content_after_starter_cleanup(
                remaining,
                removed_count=removed_count,
            )
        else:
            msg.resolved_at = now
            msg.resolution = {
                "choice": "runtime_obsolete",
                "note": "Workspace setup generated the starter knowledge documents.",
            }
            msg.refs = [
                ref for ref in (msg.refs or [])
                if not (isinstance(ref, dict) and ref.get("id") in obsolete_task_ids)
            ]
        db.add(Message(
            id=generate_ulid(),
            conversation_id=msg.conversation_id,
            role="system",
            content=(
                "Workspace setup generated starter knowledge documents; "
                f"{removed_count} duplicate proposal task(s) were marked obsolete."
            ),
            author_kind="system",
            message_kind="system",
            refs=[{"type": "message", "id": msg.id}],
        ))


def _proposal_card_content_after_starter_cleanup(
    remaining_tasks: list[Task],
    *,
    removed_count: int,
) -> str:
    lines = [
        "Strategist proposal updated: workspace setup already generated "
        f"{removed_count} starter knowledge document task(s).",
    ]
    if remaining_tasks:
        lines.append("")
        lines.append("Remaining task(s) for operator review:")
        for task in remaining_tasks:
            lines.append(f"  - {task.title}")
    return "\n".join(lines)


# ── Internals ─────────────────────────────────────────────────────────

def _enforce_allowlists(
    proposal: Proposal, allowed_service_keys: list[str],
) -> None:
    allowed = set(allowed_service_keys)
    for t in proposal.tasks:
        if t.owner_service_key not in allowed:
            raise StrategistError(
                f"Strategist proposed task {t.title!r} with owner_service_key="
                f"{t.owner_service_key!r}, not in workspace allowlist {sorted(allowed)}"
            )
        bad = [k for k in t.delegate_service_keys if k not in allowed]
        if bad:
            raise StrategistError(
                f"Strategist proposed task {t.title!r} with unknown "
                f"delegate_service_keys: {bad}"
            )


def _enforce_integration_scope(proposal: Proposal, ctx) -> None:
    """Drop social-platform tasks outside this workspace's provider scope."""
    scoped_providers = set(getattr(ctx, "configured_integrations", []) or []) & set(_SOCIAL_PROVIDER_LABELS)
    if "linkedin" in scoped_providers or "linkedin_browser" in scoped_providers:
        scoped_providers.update({"linkedin", "linkedin_browser"})
    if not scoped_providers:
        return

    kept = []
    dropped_labels: set[str] = set()
    for task in proposal.tasks:
        text = " ".join(
            str(part or "")
            for part in (task.title, task.description, task.rationale, task.expected_output)
        )
        mentioned = _mentioned_social_providers(text)
        out_of_scope = mentioned - scoped_providers
        if out_of_scope:
            dropped_labels.update(_SOCIAL_PROVIDER_LABELS.get(key, key) for key in out_of_scope)
            continue
        kept.append(task)

    if len(kept) == len(proposal.tasks):
        return

    kept_keys = {task.task_key for task in kept if task.task_key}
    for task in kept:
        task.depends_on_task_keys = [
            key for key in task.depends_on_task_keys
            if key in kept_keys
        ]
    proposal.tasks = kept

    scoped_labels = sorted(_SOCIAL_PROVIDER_LABELS.get(key, key) for key in scoped_providers)
    note = (
        "Dropped out-of-scope strategist task(s) mentioning "
        f"{', '.join(sorted(dropped_labels))}. "
        f"Workspace-scoped integrations: {', '.join(scoped_labels)}."
    )
    proposal.notes = _append_note(proposal.notes, note)


def _suppress_starter_document_proposals(proposal: Proposal, ctx) -> None:
    """Drop proposed tasks already covered by setup starter-doc generation."""
    reserved_keys = {
        str(net.get("starter_task_key"))
        for net in getattr(ctx, "knowledge_nets", []) or []
        if net.get("starter_task_key")
        and str(net.get("starter_document_status") or "").lower()
        in {"scheduled", "generating", "ready"}
    }
    if not reserved_keys:
        return

    kept = []
    dropped = []
    for task in proposal.tasks:
        key = _proposal_task_key(task.task_key or task.title)
        if key in reserved_keys:
            dropped.append(task)
            continue
        kept.append(task)
    if not dropped:
        return

    kept_keys = {task.task_key for task in kept if task.task_key}
    for task in kept:
        task.depends_on_task_keys = [
            key for key in task.depends_on_task_keys
            if key in kept_keys
        ]
    proposal.tasks = kept
    names = ", ".join(task.title for task in dropped[:3])
    suffix = "" if len(dropped) <= 3 else f" and {len(dropped) - 3} more"
    proposal.notes = _append_note(
        proposal.notes,
        "Skipped setup-owned starter knowledge proposal(s) already "
        f"scheduled/generated by workspace setup: {names}{suffix}.",
    )


def _append_note(existing: str | None, note: str, *, max_chars: int = 1500) -> str:
    text = f"{existing}\n\n{note}" if existing else note
    return text[:max_chars]


# ── Strategist template enforcement ───────────────────────────────────

def _enforce_proposal_shape(proposal: Proposal, ctx) -> None:
    """Apply ``recipe.strategist.proposal_shape`` constraints to a fresh
    proposal cohort.

    Three things are enforced post-LLM:
      * ``max_tasks_per_cycle`` — hard cap. Excess tasks are dropped from
        the END of the list (the LLM is asked to put highest-impact first,
        so trimming the tail is the least bad option). A note is appended.
      * ``preferred_categories`` — soft signal. Tasks whose category fields
        are outside the preferred set are kept but flagged in the note.
      * ``must_include_categories_per_week`` — soft signal. If the cohort
        contains zero tasks in a "must include" category, a note prompts
        the operator (we don't fabricate tasks).

    Hard rejection (drop) is reserved for the max cap because it's the
    only constraint with a clear, non-arbitrary truncation rule.
    """
    shape = (getattr(ctx, "strategist_template", None) or {}).get("proposal_shape")
    if not isinstance(shape, dict) or not shape:
        return

    max_cap = shape.get("max_tasks_per_cycle")
    if isinstance(max_cap, int) and max_cap >= 0 and len(proposal.tasks) > max_cap:
        dropped = len(proposal.tasks) - max_cap
        kept = list(proposal.tasks[:max_cap])
        # Rewrite depends_on_task_keys against the surviving set.
        kept_keys = {t.task_key for t in kept if t.task_key}
        for t in kept:
            t.depends_on_task_keys = [
                k for k in t.depends_on_task_keys if k in kept_keys
            ]
        proposal.tasks = kept
        proposal.notes = _append_note(
            proposal.notes,
            f"Dropped {dropped} proposal(s) above proposal_shape.max_tasks_per_cycle={max_cap}.",
        )

    preferred = [str(c) for c in (shape.get("preferred_categories") or []) if c]
    if preferred and proposal.tasks:
        prefset = set(preferred)
        outside = [
            (t.title, _task_category(t))
            for t in proposal.tasks
            if _task_category(t) and _task_category(t) not in prefset
        ]
        if outside:
            sample = "; ".join(f"{title!r} ({cat})" for title, cat in outside[:3])
            proposal.notes = _append_note(
                proposal.notes,
                f"Some proposals fall outside preferred_categories "
                f"{sorted(prefset)}: {sample}. "
                "Consider whether they're worth approving.",
            )

    must_weekly = [
        str(c) for c in (shape.get("must_include_categories_per_week") or []) if c
    ]
    if must_weekly and proposal.tasks:
        present = {_task_category(t) for t in proposal.tasks}
        missing = [c for c in must_weekly if c not in present]
        if missing:
            proposal.notes = _append_note(
                proposal.notes,
                f"This cohort doesn't include any task in must-include "
                f"category/categories: {missing}. The Strategist may schedule "
                f"one in a future cycle.",
            )


def _task_category(task) -> str | None:
    """Best-effort task category lookup — Proposal schema doesn't have a
    formal category field today, so we look in details first, then fall
    back to a 'kind' field if the LLM produced one."""
    details = getattr(task, "details", None) or {}
    if isinstance(details, dict):
        for k in ("category", "task_category", "kind"):
            v = details.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    kind = getattr(task, "kind", None)
    if isinstance(kind, str) and kind.strip():
        return kind.strip()
    return None


# ── Skip-condition evaluator ──────────────────────────────────────────

_SKIP_COND_RE = re.compile(
    r"^\s*(?P<name>[a-z_]+)\s*"
    r"(?P<op>==|!=|<=|>=|<|>)\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def _evaluate_skip_conditions(ctx) -> str | None:
    """Evaluate ``recipe.strategist.cadence.trigger_conditions.skip_if_any``.

    Returns the matched expression string if any condition fires (caller
    skips the review); ``None`` if no expression matched OR no conditions
    are configured.

    Supported grammar — intentionally tiny so the template can be authored
    by hand without a Python sandbox risk:

      ``<name> <op> <value>``   where op in <, <=, >, >=, ==, !=
      ``<name>``                bare name → truthy check on the resolved value

    Recognised names:
      ``open_proposed_tasks_count``   len of open_proposed_tasks
      ``recent_proposal_count``       sum of recent_proposal_outcomes
      ``missing_setup_count``         len of missing_setup
      ``calibration_sample_size``     ctx.calibration.get("sample_size", 0)
      ``budget_remaining_pct``        workspace.monthly_budget_usd → spent
      ``goals_count``                 len of goals

    Unknown names raise nothing (logged as warning + treated as 0) so an
    author mistyping a name doesn't accidentally skip every review.
    """
    tpl = getattr(ctx, "strategist_template", None) or {}
    cadence = tpl.get("cadence")
    if not isinstance(cadence, dict):
        return None
    triggers = cadence.get("trigger_conditions")
    if not isinstance(triggers, dict):
        return None
    skip_if_any = triggers.get("skip_if_any") or []
    if not isinstance(skip_if_any, list):
        return None

    for expr in skip_if_any:
        if not isinstance(expr, str):
            continue
        if _eval_one_condition(expr.strip(), ctx):
            return expr

    return None


def _eval_one_condition(expr: str, ctx) -> bool:
    if not expr:
        return False
    m = _SKIP_COND_RE.match(expr)
    if m is None:
        # Bare-name form: truthy if the value is non-zero / non-empty.
        try:
            v = _resolve_skip_name(expr.strip(), ctx)
            return bool(v)
        except KeyError:
            logger.warning("Strategist: unknown skip_if name %r", expr)
            return False
    name = m.group("name")
    op = m.group("op")
    try:
        rhs = float(m.group("value"))
    except ValueError:
        return False
    try:
        lhs_raw = _resolve_skip_name(name, ctx)
    except KeyError:
        logger.warning("Strategist: unknown skip_if name %r in expr %r", name, expr)
        return False
    try:
        lhs = float(lhs_raw if lhs_raw is not None else 0)
    except (TypeError, ValueError):
        return False
    return {
        "==": lhs == rhs,
        "!=": lhs != rhs,
        "<": lhs < rhs,
        "<=": lhs <= rhs,
        ">": lhs > rhs,
        ">=": lhs >= rhs,
    }.get(op, False)


def _resolve_skip_name(name: str, ctx) -> float | int:
    """Return the numeric value for a known skip-condition variable.
    Raises KeyError for unknown names so callers can log + treat as 0."""
    name = name.lower()
    if name == "open_proposed_tasks_count":
        return len(getattr(ctx, "open_proposed_tasks", []) or [])
    if name == "recent_proposal_count":
        outcomes = getattr(ctx, "recent_proposal_outcomes", {}) or {}
        return sum(len(v or []) for v in outcomes.values())
    if name == "missing_setup_count":
        return len(getattr(ctx, "missing_setup", []) or [])
    if name == "calibration_sample_size":
        cal = getattr(ctx, "calibration", {}) or {}
        return int(cal.get("sample_size", 0) or 0)
    if name == "goals_count":
        return len(getattr(ctx, "goals", []) or [])
    if name == "budget_remaining_pct":
        ws = getattr(ctx, "workspace", None)
        if ws is None:
            return 100
        cap = getattr(ws, "monthly_budget_usd", None)
        spent = getattr(ws, "monthly_spent_usd", None)
        if cap in (None, 0):
            return 100
        try:
            return max(0.0, min(100.0, (1.0 - float(spent or 0) / float(cap)) * 100.0))
        except (TypeError, ValueError, ZeroDivisionError):
            return 100
    raise KeyError(name)


def _mentioned_social_providers(text: str) -> set[str]:
    lowered = f" {(text or '').lower()} "
    providers: set[str] = set()
    social_context = bool(re.search(
        r"\b(post|posts|posting|publish|tweet|tweets|follower|followers|"
        r"engagement|mentions|replies|dm|audience|social|campaign|content|"
        r"growth|analytics)\b",
        lowered,
    ))
    if re.search(r"\b(linkedin|linked-in|linked in)\b", lowered):
        providers.add("linkedin")
    if re.search(r"\b(facebook|fb)\b", lowered):
        providers.add("facebook")
    if (
        re.search(r"\b(twitter|tweet|tweets|tweeting|retweet|retweets)\b", lowered)
        or re.search(r"\bx\.com\b", lowered)
        or re.search(r"\bx\s*/\s*twitter\b", lowered)
        or re.search(r"\bx\s*\(\s*twitter\s*\)", lowered)
        or re.search(
            r"\bx\s+(post|posts|thread|threads|account|followers|mentions|dm|dms|publish|campaign)\b",
            lowered,
        )
        or (social_context and re.search(r"\bx\b", lowered))
    ):
        providers.add("twitter_x")
    return providers


async def _active_work_batch_with_open_tasks(
    db: AsyncSession,
    workspace: Workspace,
) -> dict | None:
    from packages.core.models.workspace import WorkspaceWorkBatch
    from packages.core.services.task_state_machine import TERMINAL_STATUSES

    batches = list((await db.execute(
        select(WorkspaceWorkBatch)
        .where(
            WorkspaceWorkBatch.workspace_id == workspace.id,
            WorkspaceWorkBatch.entity_id == workspace.entity_id,
            WorkspaceWorkBatch.status == "active",
        )
        .order_by(WorkspaceWorkBatch.created_at.asc())
    )).scalars().all())
    for batch in batches:
        task_ids = [str(task_id) for task_id in (batch.task_ids or []) if str(task_id).strip()]
        if not task_ids:
            continue
        rows = list((await db.execute(
            select(Task.id, Task.status).where(
                Task.workspace_id == workspace.id,
                Task.entity_id == workspace.entity_id,
                Task.id.in_(task_ids),
            )
        )).all())
        statuses = {task_id: status for task_id, status in rows}
        open_task_ids = [
            task_id
            for task_id in task_ids
            if statuses.get(task_id) not in TERMINAL_STATUSES
        ]
        if open_task_ids:
            return {
                "batch_id": batch.id,
                "open_task_ids": open_task_ids,
                "source_kind": batch.source_kind,
            }
    return None


async def _refresh_internal_goal_measurements_for_review(
    db: AsyncSession,
    workspace: Workspace,
) -> dict[str, object]:
    """Measure workspace-internal goals before the Strategist reads context.

    External KPIs still come from their configured integrations/cadence. The
    internal provider is different: it is derived from Manor runtime evidence
    (linked tasks, task status, actual/estimated impact), so a review should
    refresh it synchronously before deciding whether to propose more work.
    """
    from packages.core.goals.measurement import MeasurementError, measure_goal
    from packages.core.goals.scheduling import is_workspace_internal_measurement_source

    goals = list((await db.execute(
        select(Goal).where(
            Goal.workspace_id == workspace.id,
            Goal.entity_id == workspace.entity_id,
            Goal.status == "active",
        )
    )).scalars().all())

    measured = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for goal in goals:
        if not is_workspace_internal_measurement_source(goal.measurement_source):
            continue
        try:
            result = await measure_goal(goal.id, db=db)
            if result.get("skipped"):
                skipped += 1
            else:
                measured += 1
        except MeasurementError as exc:
            errors.append({"goal_id": goal.id, "error": str(exc)})
            logger.info("Strategist: internal goal measurement skipped for %s: %s", goal.id, exc)
        except Exception as exc:
            errors.append({"goal_id": goal.id, "error": str(exc)})
            logger.exception("Strategist: failed to refresh internal goal %s", goal.id)

    return {"measured": measured, "skipped": skipped, "errors": errors}


async def _create_proposal_work_batch(
    db: AsyncSession,
    tasks: list[Task],
    *,
    review_id: str,
) -> str | None:
    first = tasks[0] if tasks else None
    if first is None or not first.workspace_id:
        return None
    from packages.core.services.workspace_operation_service import create_work_batch

    batch = await create_work_batch(
        db,
        workspace_id=first.workspace_id,
        entity_id=first.entity_id,
        task_ids=[task.id for task in tasks],
        source_kind="strategist_proposal",
        summary=f"Strategist proposal task wave ({len(tasks)} tasks)",
        details={"strategist_review_id": review_id},
    )
    try:
        from packages.core.services.workspace_service import record_activity

        await record_activity(
            db,
            first.workspace_id,
            first.entity_id,
            event_type="workspace_work_batch.started",
            summary=f"Strategist task wave started: {len(tasks)} task(s)",
            details={
                "batch_id": batch.id,
                "review_id": review_id,
                "task_ids": [task.id for task in tasks],
                "source_kind": "strategist_proposal",
            },
        )
    except Exception:
        logger.debug("Strategist: failed to record work batch start activity", exc_info=True)
    return batch.id


async def _record_proposal_approval_activity(
    db: AsyncSession,
    *,
    tasks: list[Task],
    review_id: str,
    task_ids: list[str],
    batch_id: str | None,
) -> None:
    first = next((task for task in tasks if task.workspace_id), None)
    if first is None or not first.workspace_id:
        return
    try:
        from packages.core.services.workspace_service import record_activity

        await record_activity(
            db,
            first.workspace_id,
            first.entity_id,
            event_type="strategist_proposal.approved",
            summary=f"Strategist proposal approved: {len(task_ids)} task(s)",
            details={
                "review_id": review_id,
                "batch_id": batch_id,
                "task_ids": list(task_ids),
            },
        )
    except Exception:
        logger.debug("Strategist: failed to record proposal approval activity", exc_info=True)


_UNSUPPORTED_AUTO_APPROVAL_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\balign(?:s)?\s+with\s+the\s+auto[- ]approved\s+action\s+types\s+per\s+governance\s+policy\b",
            re.IGNORECASE,
        ),
        "stays within low-risk internal work that does not require HITL",
    ),
    (
        re.compile(
            r"\bauto[- ]approved(?:\s+per\s+governance\s+policy)?\b",
            re.IGNORECASE,
        ),
        "low-risk internal work",
    ),
    (
        re.compile(
            r"\bautomatically\s+approved(?:\s+per\s+governance\s+policy)?\b",
            re.IGNORECASE,
        ),
        "low-risk internal work",
    ),
    (
        re.compile(r"\bauto[- ]approve(?:d)?\s+actions?\b", re.IGNORECASE),
        "low-risk internal actions",
    ),
)


def _sanitize_governance_language(proposal: Proposal, ctx) -> None:
    """Avoid surfacing unsupported auto-approval claims to the operator.

    The LLM sometimes treats "internal/read-only/draft-only" as equivalent to
    platform auto-approval. Governance distinguishes those: low-risk work may
    be safe to propose, but it is not auto-approved unless a policy pattern
    explicitly says so.
    """
    policy = getattr(ctx, "governance_policy", None) or {}
    if policy.get("auto_approve_actions"):
        return

    proposal.summary = _sanitize_unsupported_auto_approval_text(proposal.summary)
    if proposal.notes:
        proposal.notes = _sanitize_unsupported_auto_approval_text(proposal.notes)
    for task in proposal.tasks:
        if task.description:
            task.description = _sanitize_unsupported_auto_approval_text(task.description)
        if task.rationale:
            task.rationale = _sanitize_unsupported_auto_approval_text(task.rationale)
        if task.estimated_impact and task.estimated_impact.rationale:
            task.estimated_impact.rationale = _sanitize_unsupported_auto_approval_text(
                task.estimated_impact.rationale,
            )


def _sanitize_unsupported_auto_approval_text(text: str) -> str:
    cleaned = text
    for pattern, replacement in _UNSUPPORTED_AUTO_APPROVAL_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _task_expected_output_from_proposed(pt: ProposedTask) -> dict:
    """Build the Task.expected_output payload from a ProposedTask's deliverables."""

    payload = dict(pt.expected_output or {})
    payload["deliverables"] = [d.model_dump() for d in pt.deliverables]
    return payload


async def _persist_tasks(
    db: AsyncSession,
    workspace: Workspace,
    proposal: Proposal,
) -> list[str]:
    ids: list[str] = []
    rows_by_key: dict[str, Task] = {}
    pending_key_deps: dict[str, list[str]] = {}
    pending_goal_links: list[tuple[Task, str, float | None]] = []

    used_keys: dict[str, int] = {}
    requested_goal_ids = {
        str(pt.estimated_impact.goal_id)
        for pt in proposal.tasks
        if pt.estimated_impact and pt.estimated_impact.goal_id
    }
    valid_goal_ids: set[str] = set()
    if requested_goal_ids:
        valid_goal_ids = set((await db.execute(
            select(Goal.id).where(
                Goal.entity_id == workspace.entity_id,
                Goal.workspace_id == workspace.id,
                Goal.id.in_(requested_goal_ids),
            )
        )).scalars().all())

    for pt in proposal.tasks:
        base_key = _proposal_task_key(pt.task_key or pt.title)
        count = used_keys.get(base_key, 0)
        used_keys[base_key] = count + 1
        task_key = base_key if count == 0 else f"{base_key}_{count + 1}"
        owner_sub_id = await _resolve_subscription_id(
            db, workspace.id, workspace.entity_id, pt.owner_service_key,
        )
        depends_on_keys = [_proposal_task_key(k) for k in (pt.depends_on_task_keys or [])]
        details = {
            "strategist_review_id": proposal.review_id,
            "strategist_task_key": task_key,
            "depends_on_task_keys": depends_on_keys,
            "estimated_impact": pt.estimated_impact.model_dump() if pt.estimated_impact else None,
            "rationale": pt.rationale,
        }
        runtime_context = merge_task_runtime_capabilities(
            {},
            pt.required_capabilities,
            replace=True,
        )
        if runtime_context:
            details["runtime_context"] = runtime_context
        row = Task(
            id=generate_ulid(),
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            title=pt.title,
            description=pt.description,
            status="proposed",
            priority=pt.priority,
            task_type="ai_generated",
            details=details,
            owner_service_key=pt.owner_service_key,
            owner_subscription_id=owner_sub_id,
            delegate_service_keys=list(pt.delegate_service_keys),
            expected_output=_task_expected_output_from_proposed(pt),
            creator_id=None,
        )
        db.add(row)
        ids.append(row.id)
        rows_by_key[task_key] = row
        pending_key_deps[task_key] = depends_on_keys
        if (
            pt.estimated_impact
            and pt.estimated_impact.goal_id
            and pt.estimated_impact.goal_id in valid_goal_ids
        ):
            pending_goal_links.append((
                row,
                pt.estimated_impact.goal_id,
                pt.estimated_impact.metric_delta,
            ))
    await db.flush()

    if pending_goal_links:
        from packages.core.goals.service import link_task_to_goal

        for row, goal_id, metric_delta in pending_goal_links:
            await link_task_to_goal(
                db,
                goal_id=goal_id,
                task_id=row.id,
                contribution="direct",
                estimated_impact=metric_delta,
            )

    for task_key, row in rows_by_key.items():
        dep_ids = [
            rows_by_key[dep_key].id
            for dep_key in pending_key_deps.get(task_key, [])
            if dep_key in rows_by_key and rows_by_key[dep_key].id != row.id
        ]
        if dep_ids:
            details = dict(row.details or {})
            details["depends_on_task_ids"] = dep_ids
            row.details = details
    await db.flush()
    return ids


def _proposal_task_key(value: str | None) -> str:
    """Normalize LLM-provided task keys into compact stable ids."""
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "task").strip().lower())
    base = re.sub(r"_+", "_", base).strip("_") or "task"
    return base[:80]


async def _record_strategist_review_evidence(
    db: AsyncSession,
    *,
    workspace: Workspace,
    proposal: Proposal,
    trigger: str,
    task_ids: list[str],
    ctx,
) -> None:
    """Record the Strategist's own decision so later loops can inspect it."""
    try:
        from packages.core.services.runtime_learning import record_runtime_evidence
        from packages.core.services.workspace_evaluation import record_workspace_evaluation_snapshot

        evaluation_evidence_id = None
        workspace_evaluation = getattr(ctx, "workspace_evaluation", None)
        if isinstance(workspace_evaluation, dict):
            evaluation_evidence = await record_workspace_evaluation_snapshot(
                db,
                workspace_evaluation,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                source="strategist",
                trace_id=proposal.review_id,
            )
            evaluation_evidence_id = evaluation_evidence.id

        task_summaries = []
        for task in proposal.tasks:
            impact = task.estimated_impact.model_dump() if task.estimated_impact else None
            task_summaries.append({
                "task_key": task.task_key,
                "title": task.title,
                "owner_service_key": task.owner_service_key,
                "delegate_service_keys": list(task.delegate_service_keys or []),
                "depends_on_task_keys": list(task.depends_on_task_keys or []),
                "required_capabilities": list(task.required_capabilities or []),
                "priority": task.priority,
                "estimated_impact": impact,
                "rationale": task.rationale,
            })

        await record_runtime_evidence(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            trace_id=proposal.review_id,
            evidence_type="strategist_review",
            source="strategist",
            status="succeeded",
            summary=f"Strategist proposed {len(task_ids)} task(s): {proposal.summary[:500]}",
            details={
                "review_id": proposal.review_id,
                "trigger": trigger,
                "proposal_summary": proposal.summary,
                "notes": proposal.notes,
                "task_ids": list(task_ids),
                "tasks": task_summaries,
                "input_snapshot": {
                    "goal_count": len(getattr(ctx, "goals", []) or []),
                    "recent_task_count": len(getattr(ctx, "recent_tasks", []) or []),
                    "recent_plan_count": len(getattr(ctx, "recent_plans", []) or []),
                    "runtime_evidence_count": len(getattr(ctx, "recent_runtime_evidence", []) or []),
                    "learning_candidate_count": len(getattr(ctx, "learning_candidates", []) or []),
                    "work_batch_reconciliation": list(getattr(ctx, "work_batch_reconciliation", []) or [])[:10],
                    "open_proposed_count": len(getattr(ctx, "open_proposed_tasks", []) or []),
                    "missing_setup": list(getattr(ctx, "missing_setup", []) or []),
                    "configured_integrations": list(getattr(ctx, "configured_integrations", []) or [])[:30],
                    "configured_channels": list(getattr(ctx, "configured_channels", []) or [])[:20],
                    "knowledge_net_count": len(getattr(ctx, "knowledge_nets", []) or []),
                    "governance_hitl_actions": (
                        (getattr(ctx, "governance_policy", None) or {}).get("hitl_required_actions") or []
                    )[:20],
                    "governance_auto_approve_actions": (
                        (getattr(ctx, "governance_policy", None) or {}).get("auto_approve_actions") or []
                    )[:20],
                    "workspace_evaluation_score": (
                        (workspace_evaluation.get("overall") or {}).get("score")
                        if isinstance(workspace_evaluation, dict) else None
                    ),
                    "workspace_evaluation_evidence_id": evaluation_evidence_id,
                },
            },
            metrics={
                "task_count": len(proposal.tasks),
                "persisted_task_count": len(task_ids),
                "notes_present": bool(proposal.notes),
            },
        )
    except Exception:
        logger.warning("Failed to record strategist runtime evidence", exc_info=True)


async def _resolve_subscription_id(
    db: AsyncSession, workspace_id: str, entity_id: str, service_key: str,
) -> Optional[str]:
    """Find the active AgentSubscription for a service_key in this workspace."""
    from packages.core.models.workspace import AgentSubscription
    result = await db.execute(
        select(AgentSubscription.id).where(
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.service_key == service_key,
            AgentSubscription.status == "active",
        ).limit(1)
    )
    row = result.scalar_one_or_none()
    return row


# ── Chat surfacing ────────────────────────────────────────────────────

async def _post_proposal_chat(
    workspace: Workspace,
    proposal: Proposal,
    task_ids: list[str],
    *,
    auto_approved: bool = False,
) -> None:
    """Single proposal card in the workspace_main conversation.

    Uses ``message_kind='proposal'`` + ``pending_action`` so the chat
    UI renders [Approve all] [Reject all] buttons. Fine-grained pick
    still works through the API (``only_task_ids`` parameter).
    """
    if auto_approved:
        body_lines = [f"📋 Strategist proposal auto-approved — {proposal.summary}"]
    else:
        body_lines = [f"📋 Strategist proposal — {proposal.summary}"]
    if proposal.tasks:
        body_lines.append("")
        for t, tid in zip(proposal.tasks, task_ids):
            impact = ""
            if t.estimated_impact and t.estimated_impact.metric_delta is not None:
                impact = f" (~{t.estimated_impact.metric_delta:+g})"
            body_lines.append(f"  • [{t.priority}] {t.title}{impact}")
            if t.rationale:
                body_lines.append(f"      _{t.rationale}_")
    else:
        body_lines.append("\n_(no actionable tasks this cycle)_")
    if proposal.notes:
        body_lines.append("")
        body_lines.append(f"📝 Notes: {proposal.notes}")
    if auto_approved and task_ids:
        body_lines.append("")
        body_lines.append("✓ Workspace proposal auto-approval is enabled; these tasks were started automatically.")

    pending_action = (
        {
            "kind": "approve_proposals",
            "review_id": proposal.review_id,
            "task_ids": task_ids,
            "task_titles": [t.title for t in proposal.tasks],
            "options": ["approve_all", "reject_all"],
        }
        if task_ids and not auto_approved
        else None
    )

    refs = [{"type": "task", "id": t} for t in task_ids]

    try:
        from packages.core.database import async_session
        # Post the chat message first and commit — notifications must not
        # poison the session and cause the proposal card to be lost.
        async with async_session() as db:
            await chat_service.post_message(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                body="\n".join(body_lines),
                message_kind="proposal",
                author_kind="agent",
                refs=refs,
                pending_action=pending_action,
            )
            await db.commit()

        # Notify all entity users (separate session so failures are isolated).
        try:
            from packages.core.services.notification_service import create_notification
            from packages.core.models.user import User
            from sqlalchemy import select

            task_count = len(task_ids)
            raw_title = (
                f"Proposal auto-approved: {proposal.summary or f'{task_count} tasks'}"
                if auto_approved
                else f"New proposal: {proposal.summary or f'{task_count} tasks'}"
            )
            notif_title = raw_title[:490] + "…" if len(raw_title) > 490 else raw_title
            notif_body = (
                f"{workspace.name} — Strategist auto-approved and started {task_count} task"
                f"{'s' if task_count != 1 else ''}."
                if auto_approved
                else (
                    f"{workspace.name} — Strategist proposed {task_count} task"
                    f"{'s' if task_count != 1 else ''}. Review and approve to start execution."
                )
            )
            async with async_session() as db2:
                users = (await db2.execute(
                    select(User.id).where(User.entity_id == workspace.entity_id, User.status == "active")
                )).scalars().all()
                for uid in users:
                    await create_notification(
                        db2, workspace.entity_id, uid,
                        type="proposal",
                        title=notif_title,
                        body=notif_body,
                        link=f"/workspaces/{workspace.id}?tab=chat",
                        meta={
                            "workspace_id": workspace.id,
                            "workspace_name": workspace.name,
                            "review_id": proposal.review_id,
                            "task_ids": task_ids,
                            "task_count": task_count,
                        },
                    )
                await db2.commit()
        except Exception:
            logger.debug("Strategist: notification creation failed (non-blocking)", exc_info=True)
    except Exception:
        logger.warning("Strategist: failed to post proposal card", exc_info=True)
