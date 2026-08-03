"""Strategist orchestration entry point.

``run_review(workspace_id)`` does one full cycle:

  1. Load workspace + check it exists.
  2. Dedupe: if a review just ran in the last `min_gap` seconds AND
     produced open proposals, skip — the operator hasn't acted yet.
  3. Gather context (goals, tasks, memory).
  4. Single Claude call → validated Proposal.
  5. Cross-check service_keys against the workspace allowlist.
  6. Write Task rows with status='proposed', tagged with review_id.
  7. Post a single proposal card in workspace_chat with a ``pending_action``
     so the operator can [Approve all] / [Always approve] / [Reject all].
  8. Optionally write a ``learning`` memory note about the review.

``approve_proposal`` / ``reject_proposal`` mutate the cohort all-at-once
when the operator clicks the chat card.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
    ApprovalOriginKind,
    ApprovalStatus,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.task import TaskStatus
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import Workspace
from packages.core.ai.runtime import runtime_strategist_review_billing_context
from packages.core.ai.runtime.task_requirements import merge_task_runtime_capabilities
from packages.core.proposals.constants import (
    TASK_ACTION_KEY,
    strategist_action_label,
)
from packages.core.services.hitl_options import DEFAULT_APPROVAL_OPTIONS
from packages.core.services.task_dependencies import dependency_ids_from_details, details_with_dependency_state
from packages.core.services.task_service import update_task
from packages.core.services.workspace_work_reconciliation import (
    reconcile_active_work_batches,
    stale_reconciliation_results,
)
from packages.core.strategist.context import gather_context
from packages.core.strategist.prompt import generate_proposal
from packages.core.strategist.proposal import Proposal, ProposedTask
from packages.core.strategist.triggers import ReviewTrigger, ReviewTriggerKind
from packages.core.workspace_chat import service as chat_service

logger = logging.getLogger(__name__)

_STRATEGIST_SETTINGS_KEY = "strategist"
_AUTO_APPROVE_PROPOSALS_KEY = "auto_approve_proposals"


class StrategistError(Exception):
    pass


# ── Main entry ────────────────────────────────────────────────────────

async def run_review(
    db: AsyncSession,
    workspace_id: str,
    *,
    trigger: "ReviewTrigger | ReviewTriggerKind | str" = ReviewTriggerKind.SCHEDULED,
    briefing_markdown: Optional[str] = None,
    review_run=None,
) -> dict:
    """Run one Strategist review cycle. Caller commits.

    ``trigger`` carries the typed :class:`ReviewTriggerKind` plus opaque
    detail prose. Suppression is decided from the kind and nothing else:

    * a suppressible trigger (SCHEDULED / EVENT) that is blocked returns a
      ``skipped`` result — nobody is waiting, and piling proposals on top
      of undecided ones is worse than waiting a cycle;
    * a HUMAN_REQUESTED trigger that is blocked returns a structured
      ``needs_decision`` result naming what blocks it. It is never dropped
      on the floor: somebody asked and is waiting for an answer.

    Either way the block is announced once in workspace chat (see
    ``_post_review_skip_notice``), so a suppressed review stops being
    invisible.

    ``briefing_markdown`` / ``review_run`` are only supplied on the
    strategist_review_v2 path (M5/M6): the deterministic ReviewBriefing
    markdown replaces the legacy context sections in the LLM user prompt,
    and the proposal cohort is tagged with the ReviewRun's id so decisions
    trace back to the frozen snapshot. Both default to ``None``, keeping
    the legacy path (prompts included) byte-identical.
    """
    trigger = ReviewTrigger.coerce(trigger)
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

    # Cadence ticks yield to work that is already in flight. Deliberately
    # narrower than the open-proposal gate below: event triggers fire
    # *because* something finished, and a human trigger never waits on a
    # batch — it waits on the operator.
    if trigger.kind is ReviewTriggerKind.SCHEDULED:
        active_batch = await _active_work_batch_with_open_tasks(db, workspace)
        if active_batch:
            logger.info(
                "Strategist: active work batch %s still has %d open task(s); skipping scheduled review",
                active_batch["batch_id"],
                len(active_batch["open_task_ids"]),
            )
            await _post_review_skip_notice(
                db,
                workspace,
                trigger=trigger,
                fingerprint=_active_batch_fingerprint(active_batch),
                body=_active_batch_skip_body(trigger, active_batch),
            )
            return {
                "workspace_id": workspace_id,
                "skipped": True,
                "reason": "active_work_batch",
                **active_batch,
            }

    ctx = await gather_context(db, workspace, trigger=trigger.label)
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

    # The last review still has unaccepted proposals. What happens next is
    # decided by the trigger KIND, never by the trigger text.
    if ctx.open_proposed_tasks:
        conflict = open_proposal_conflict(ctx.open_proposed_tasks)
        await _post_review_skip_notice(
            db,
            workspace,
            trigger=trigger,
            fingerprint=conflict["fingerprint"],
            body=_open_proposals_skip_body(trigger, conflict),
        )
        if trigger.kind.suppressible:
            logger.info(
                "Strategist: %d proposals from prior review still open; skipping",
                conflict["open_count"],
            )
            return {
                "workspace_id": workspace_id,
                "skipped": True,
                "reason": "open_proposals",
                "open_count": conflict["open_count"],
            }
        # A person asked for this review and is waiting. Do not run it on
        # top of an undecided cohort, and do not swallow it either — hand
        # the conflict back so the caller can ask them what to do.
        logger.info(
            "Strategist: human-requested review blocked by %d open proposal(s); "
            "returning needs_decision",
            conflict["open_count"],
        )
        return {
            "workspace_id": workspace_id,
            "skipped": True,
            "needs_decision": True,
            "reason": "open_proposals",
            "open_count": conflict["open_count"],
            "conflict": conflict,
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

    review_id = review_run.id if review_run is not None else "rv_" + generate_ulid()

    # Only pass briefing_markdown on the v2 path so the legacy call shape
    # (and anything monkeypatching generate_proposal) stays untouched.
    proposal_kwargs: dict = {}
    if briefing_markdown is not None:
        proposal_kwargs["briefing_markdown"] = briefing_markdown

    async with runtime_strategist_review_billing_context(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
    ):
        proposal = await generate_proposal(
            ctx,
            review_id=review_id,
            db=db,
            **proposal_kwargs,
        )

    _sanitize_governance_language(proposal, ctx)
    _suppress_starter_document_proposals(proposal, ctx)
    _suppress_meta_learning_proposals(proposal)
    _enforce_allowlists(proposal, ctx.allowed_service_keys)
    _enforce_proposal_shape(proposal, ctx)

    auto_approve_proposals = proposal_auto_approval_enabled(workspace)

    # Write Task rows + collect their ids.
    new_task_ids = await _persist_tasks(db, workspace, proposal)
    # Ledger (M1): one proposal_created fact per review.
    from packages.core.ledger.adapters import record_proposal_created
    await record_proposal_created(db, workspace, review_id=review_id, task_ids=new_task_ids)
    await _record_strategist_review_evidence(
        db,
        workspace=workspace,
        proposal=proposal,
        trigger=trigger,
        task_ids=new_task_ids,
        ctx=ctx,
    )

    # ── M7/M8 (v2 path only): proposal_items bookkeeping + one
    # HitlRequest resolution for the whole cohort. Legacy path
    # (briefing_markdown is None) writes no proposal rows at all.
    governance: dict = {}
    if (
        briefing_markdown is not None
        and review_run is not None
        and (
            new_task_ids
            or proposal.human_requests
            or proposal.experiments
            or proposal.automation_changes
            or proposal.workflow_changes
            or proposal.goal_changes
        )
    ):
        governance = await _wire_proposal_governance(
            db,
            workspace=workspace,
            review_run=review_run,
            proposal=proposal,
            new_task_ids=new_task_ids,
        )

    policy_denied = governance.get("outcome") == "deny"
    standing_allow = governance.get("outcome") == "allow"

    approved_task_ids: list[str] = []
    if policy_denied:
        # Governance hard block: cancel the cohort (items already marked
        # rejected/POLICY_BLOCKED by the mirror inside reject_proposal).
        await reject_proposal(
            db,
            entity_id=workspace.entity_id,
            review_id=review_id,
            only_task_ids=new_task_ids or None,
            reason=governance.get("reason") or "Blocked by governance policy.",
            reason_code="POLICY_BLOCKED",
            actor_kind="system",
        )
    elif (auto_approve_proposals or standing_allow) and new_task_ids:
        approved_task_ids = await approve_proposal(
            db,
            entity_id=workspace.entity_id,
            review_id=review_id,
            only_task_ids=new_task_ids,
            actor_kind="system",
        )

    # Non-task items (change kinds / experiments) that resolved to
    # needs_human ride the SAME cohort card as the tasks — a proposal made
    # only of them still has to reach an operator.
    pending_items: list[dict] = []
    if governance.get("proposal_id"):
        pending_items = await _pending_item_digests(db, review_id=review_id)

    # Always commit before posting to chat — the chat post opens its
    # own session and shouldn't see uncommitted tasks.
    await db.commit()

    # Post the proposal card. Best-effort.
    if new_task_ids or pending_items or proposal.notes:
        await _post_proposal_chat(
            workspace,
            proposal,
            new_task_ids,
            auto_approved=bool(approved_task_ids),
            policy_denied=policy_denied,
            pending_items=pending_items,
            # Name what authorised the auto-execution. The cohort approval
            # is resolved against TASK_ACTION_KEY, so a standing-grant
            # "allow" is exactly that key's grant; otherwise it was the
            # legacy workspace-wide boolean.
            auto_approved_action_key=TASK_ACTION_KEY if standing_allow else None,
        )

    # Surface human requests in workspace chat (M10). Best-effort —
    # the commitments are already committed above.
    if governance.get("human_requests"):
        await _post_human_request_chat(workspace, governance["human_requests"])

    result = {
        "workspace_id": workspace_id,
        "review_id": review_id,
        "task_count": len(new_task_ids),
        "task_ids": new_task_ids,
        "auto_approved": bool(approved_task_ids),
        "approved_task_ids": approved_task_ids,
        "summary": proposal.summary,
        "notes": proposal.notes,
    }
    if governance:
        result["proposal_id"] = governance.get("proposal_id")
        result["approval_outcome"] = governance.get("outcome")
        result["approval_request_id"] = governance.get("approval_request_id")
        if governance.get("validation_notes"):
            result["validation_notes"] = governance["validation_notes"]
        if governance.get("human_requests"):
            result["human_requests"] = governance["human_requests"]
        if governance.get("experiments"):
            result["experiments"] = governance["experiments"]
        if governance.get("changes"):
            result["changes"] = governance["changes"]
        if pending_items:
            result["pending_items"] = pending_items
    return result


async def _wire_proposal_governance(
    db: AsyncSession,
    *,
    workspace: Workspace,
    review_run,
    proposal: Proposal,
    new_task_ids: list[str],
) -> dict:
    """M7/M8 v1: persist the proposal-item cohort and resolve its approval.

    Runs only on the v2 (briefing) path. Creates one ProposalRecord + one
    kind="task" item per persisted Task, runs the v1 validator (strips
    invalid basis report_refs), then asks the unified approval core ONCE
    for the whole cohort (subject = workspace.proposal.task, resource =
    the ProposalRecord). Returns what run_review needs to branch on:
    ``{proposal_id, outcome, reason, approval_request_id, validation_notes}``.
    """
    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        resolve_approval,
    )
    from packages.core.models.consolidation_report import ConsolidationReport
    from packages.core.proposals import (
        TASK_ACTION_KEY,
        create_proposal_with_items,
        get_items_for_review,
        validate_items,
    )

    review_id = review_run.id
    rows = (await db.execute(
        select(Task).where(Task.id.in_(new_task_ids))
    )).scalars().all()
    by_id = {t.id: t for t in rows}
    # _persist_tasks appends exactly one id per ProposedTask, in order.
    pairs = [
        (pt, by_id[tid])
        for pt, tid in zip(proposal.tasks, new_task_ids)
        if tid in by_id
    ]
    record = await create_proposal_with_items(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=review_id,
        summary=proposal.summary,
        notes=proposal.notes,
        persisted_tasks=pairs,
    )
    items = await get_items_for_review(db, review_id)
    report_rows = (await db.execute(
        select(ConsolidationReport).where(ConsolidationReport.review_id == review_id)
    )).scalars().all()
    validated = await validate_items(db, review_run, report_rows, items)
    validation_notes = [note for _, note in validated if note]

    # The cohort HitlRequest only concerns kind="task" items; a
    # human_requests-only proposal never mints one (M8 catalog).
    decision = None
    if pairs:
        decision = await resolve_approval(
            db,
            subject=ApprovalSubject(
                entity_id=workspace.entity_id,
                action_key=TASK_ACTION_KEY,
                capability_id=None,
                resource_kind="proposal",
                resource_id=record.id,
                risk_level="low",
                kind="action",
                # Proposals intrinsically need a human unless a standing grant
                # (policy auto_approve_actions) or the legacy workspace boolean
                # says otherwise — without this flag a policy with no opinion
                # would silently auto-approve every cohort.
                requires_approval=True,
                workspace_id=workspace.id,
            ),
            origin=ApprovalOrigin(
                kind=ApprovalOriginKind.OPERATION.value,
                context={
                    "plane": "strategist_proposal",
                    "review_id": review_id,
                    "proposal_id": record.id,
                    "task_ids": list(new_task_ids),
                    "task_titles": [pt.title for pt, _ in pairs],
                },
            ),
            intrinsic_rule="proposal.review",
            intrinsic_reason="Strategist proposals require operator approval before execution.",
        )

    approval_request_id = (
        decision.request.id
        if decision is not None and decision.request is not None
        else None
    )
    if decision is not None and decision.outcome == "needs_human" and approval_request_id:
        # Stamp the open request on every item so surfaces can join back.
        for item in items:
            item.approval_request_id = approval_request_id
        await db.flush()

    human_requests = await _execute_human_request_items(
        db,
        workspace=workspace,
        record=record,
        review_id=review_id,
        proposal=proposal,
    )

    experiments = await _execute_experiment_items(
        db,
        workspace=workspace,
        record=record,
        review_id=review_id,
        proposal=proposal,
    )

    changes = await _execute_change_items(
        db,
        workspace=workspace,
        record=record,
        review_run=review_run,
        review_id=review_id,
        proposal=proposal,
        report_rows=report_rows,
    )
    if changes:
        validation_notes.extend(
            digest["note"] for digest in changes if digest.get("note")
        )

    return {
        "proposal_id": record.id,
        "outcome": decision.outcome if decision is not None else None,
        "reason": decision.reason if decision is not None else None,
        "approval_request_id": approval_request_id,
        "validation_notes": validation_notes,
        "human_requests": human_requests,
        "experiments": experiments,
        "changes": changes,
    }


async def _execute_human_request_items(
    db: AsyncSession,
    *,
    workspace: Workspace,
    record,
    review_id: str,
    proposal: Proposal,
) -> list[dict]:
    """M10 human_request execution: persist auto-approved items, open one
    HumanCommitment per request (participant resolution deferred — queue
    by role), stamp the commitment as the item's execution root, and emit
    the ledger facts. Returns chat-surface digests."""
    proposed = list(proposal.human_requests or [])
    if not proposed:
        return []

    from datetime import timedelta

    from packages.core.humans import open_commitment
    from packages.core.ledger.adapters import record_human_request_item_auto_approved
    from packages.core.proposals import create_human_request_items

    items = await create_human_request_items(
        db, record=record, proposed_requests=proposed,
    )

    now = datetime.now(timezone.utc)
    digests: list[dict] = []
    for proposed_request, item in zip(proposed, items):
        expected_by = (
            now + timedelta(hours=proposed_request.expected_by_hours)
            if proposed_request.expected_by_hours
            else None
        )
        commitment = await open_commitment(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            request_kind=proposed_request.request_kind,
            source_kind="proposal_item",
            source_id=item.id,
            expected_input=proposed_request.question,
            participant_id=None,  # v1: queue by role, never by named person
            role_required=proposed_request.role_required,
            expected_by=expected_by,
            causation_id=item.id,
        )
        item.execution_root_id = commitment.id
        item.status = "executing"

        await record_human_request_item_auto_approved(
            db, item, review_id=review_id, commitment_id=commitment.id,
        )
        digests.append({
            "item_id": item.id,
            "commitment_id": commitment.id,
            "request_kind": proposed_request.request_kind,
            "question": proposed_request.question,
            "role_required": proposed_request.role_required,
            "expected_by": expected_by.isoformat() if expected_by else None,
        })
    await db.flush()
    return digests


async def _execute_experiment_items(
    db: AsyncSession,
    *,
    workspace: Workspace,
    record,
    review_id: str,
    proposal: Proposal,
) -> list[dict]:
    """M13 experiment items: persist + per-item governance.

    Unlike the task cohort (ONE HitlRequest for the whole cohort) each
    experiment mints its own request (subject action_key
    ``workspace.proposal.experiment``, resource = the proposal item), so a
    standing grant / policy decision applies per experiment:

      * allow (standing grant)   → Experiment row created + started, item
        ``executing`` with ``execution_root_id`` = experiment id;
      * needs_human              → item stays ``proposed`` carrying the
        pending ``approval_request_id`` (resolved by the cohort card via
        ``_mirror_item_decisions``);
      * deny                     → item ``rejected`` / POLICY_BLOCKED.

    Returns chat/result digests.
    """
    proposed = list(getattr(proposal, "experiments", None) or [])
    if not proposed:
        return []

    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        resolve_approval,
    )
    from packages.core.proposals import EXPERIMENT_ACTION_KEY, create_experiment_items

    items = await create_experiment_items(
        db, record=record, proposed_experiments=proposed,
    )

    now = datetime.now(timezone.utc)
    digests: list[dict] = []
    for proposed_experiment, item in zip(proposed, items):
        decision = await resolve_approval(
            db,
            subject=ApprovalSubject(
                entity_id=workspace.entity_id,
                action_key=EXPERIMENT_ACTION_KEY,
                capability_id=None,
                resource_kind="proposal_item",
                resource_id=item.id,
                risk_level=item.risk_level,
                kind="action",
                # Experiments intrinsically need a human unless a standing
                # grant (policy auto_approve_actions) says otherwise.
                requires_approval=True,
                workspace_id=workspace.id,
            ),
            origin=ApprovalOrigin(
                kind=ApprovalOriginKind.OPERATION.value,
                context={
                    "plane": "strategist_proposal",
                    "review_id": review_id,
                    "proposal_id": record.id,
                    "proposal_item_id": item.id,
                    "experiment_key": proposed_experiment.experiment_key,
                    "hypothesis": proposed_experiment.hypothesis,
                    "target_kind": proposed_experiment.target_kind,
                    "target_id": proposed_experiment.target_id,
                    "max_runs": proposed_experiment.max_runs,
                    "duration_days": proposed_experiment.duration_days,
                    "max_cost": proposed_experiment.guardrails.max_cost,
                },
            ),
            intrinsic_rule="proposal.experiment",
            intrinsic_reason=(
                "Experiments require operator approval before a config "
                "overlay is applied."
            ),
        )

        experiment_id = None
        if decision.outcome == "allow":
            item.status = "approved"
            item.decided_at = now
            item.decision = {
                "decided_by": None,
                "decision": "approved",
                "reason_code": None,
                "comment": decision.reason or "standing grant",
                "decided_at": now.isoformat(),
            }
            experiment = await _create_and_start_experiment_for_item(db, item)
            experiment_id = experiment.id if experiment is not None else None
        elif decision.outcome == "deny":
            item.status = "rejected"
            item.decided_at = now
            item.decision = {
                "decided_by": None,
                "decision": "rejected",
                "reason_code": "POLICY_BLOCKED",
                "comment": decision.reason,
                "decided_at": now.isoformat(),
            }
        else:  # needs_human — the item waits on its own approval card
            if decision.request is not None:
                item.approval_request_id = decision.request.id

        digests.append({
            "item_id": item.id,
            "experiment_key": proposed_experiment.experiment_key,
            "outcome": decision.outcome,
            "experiment_id": experiment_id,
            "approval_request_id": (
                decision.request.id if decision.request is not None else None
            ),
            "risk_level": item.risk_level,
        })
    await db.flush()
    return digests


async def _execute_change_items(
    db: AsyncSession,
    *,
    workspace: Workspace,
    record,
    review_run,
    review_id: str,
    proposal: Proposal,
    report_rows,
) -> list[dict]:
    """M7/M8/M10 configuration-change items: persist → validate → govern.

    Each change item is governed INDIVIDUALLY (like experiments, unlike the
    task cohort): its action_key comes from the M8 catalog per
    kind+operation, so a standing grant on
    ``workspace.proposal.automation_change.update`` can auto-apply schedule
    tweaks while a delete still stops for a human.

      * validator reject → item already ``rejected`` (STALE_REVISION /
        DUPLICATE / INSUFFICIENT_DATA); never reaches approval;
      * allow (standing/policy) → ``apply_change_item`` runs immediately and
        the request (if any) is consumed;
      * needs_human          → item stays ``proposed`` with the pending
        ``approval_request_id`` (the cohort card applies it on approve, via
        ``_mirror_change_items_on_cohort_decision``);
      * deny (policy)        → item ``rejected`` / POLICY_BLOCKED.
    """
    proposed: list[tuple] = [
        *[("automation_change", c) for c in (proposal.automation_changes or [])],
        *[("workflow_change", c) for c in (proposal.workflow_changes or [])],
        *[("goal_change", c) for c in (proposal.goal_changes or [])],
    ]
    if not proposed:
        return []

    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        consume_approval,
        resolve_approval,
    )
    from packages.core.proposals import (
        apply_change_item,
        create_change_items,
        validate_items,
    )

    items = await create_change_items(
        db, record=record, proposed_changes=proposed,
    )
    validated = await validate_items(db, review_run, report_rows, items)
    notes_by_item = {item.id: note for item, note in validated}

    now = datetime.now(timezone.utc)
    digests: list[dict] = []
    for (kind, proposed_change), item in zip(proposed, items):
        digest: dict = {
            "item_id": item.id,
            "kind": kind,
            "change_key": proposed_change.change_key,
            "operation": proposed_change.operation,
            "target_kind": proposed_change.target_kind,
            "target_id": proposed_change.target_id,
            "risk_level": item.risk_level,
            "action_key": item.action_key,
            "note": notes_by_item.get(item.id),
            "outcome": None,
            "applied": False,
            "approval_request_id": None,
        }
        if item.status != "proposed":
            # Validator already rejected it (and recorded the reason).
            digest["outcome"] = "rejected"
            digest["reason_code"] = (item.decision or {}).get("reason_code")
            digests.append(digest)
            continue

        decision = await resolve_approval(
            db,
            subject=ApprovalSubject(
                entity_id=workspace.entity_id,
                action_key=item.action_key,
                capability_id=None,
                resource_kind="proposal_item",
                resource_id=item.id,
                risk_level=item.risk_level,
                kind="action",
                # Config changes intrinsically need a human unless a standing
                # grant (policy auto_approve_actions) says otherwise.
                requires_approval=True,
                workspace_id=workspace.id,
            ),
            origin=ApprovalOrigin(
                kind=ApprovalOriginKind.OPERATION.value,
                context={
                    "plane": "strategist_proposal",
                    "review_id": review_id,
                    "proposal_id": record.id,
                    "proposal_item_id": item.id,
                    "change_kind": kind,
                    "operation": proposed_change.operation,
                    "target_kind": proposed_change.target_kind,
                    "target_id": proposed_change.target_id,
                    "expected_revision": proposed_change.expected_revision,
                    "rationale": proposed_change.rationale,
                },
            ),
            intrinsic_rule=f"proposal.{kind}",
            intrinsic_reason=(
                "Configuration changes require operator approval before the "
                "canonical row is edited."
            ),
        )
        digest["outcome"] = decision.outcome
        if decision.request is not None:
            digest["approval_request_id"] = decision.request.id

        if decision.outcome == "allow":
            item.status = "approved"
            item.decided_at = now
            item.decision = {
                "decided_by": None,
                "decision": "approved",
                "reason_code": None,
                "comment": decision.reason or "standing grant",
                "decided_at": now.isoformat(),
            }
            result = await apply_change_item(db, item)
            digest["applied"] = result["ok"]
            digest["revision"] = result.get("revision")
            digest["error"] = result.get("error")
            digest["error_code"] = result.get("error_code")
            if decision.request is not None:
                await consume_approval(db, decision.request)
        elif decision.outcome == "deny":
            item.status = "rejected"
            item.decided_at = now
            item.decision = {
                "decided_by": None,
                "decision": "rejected",
                "reason_code": "POLICY_BLOCKED",
                "comment": decision.reason,
                "decided_at": now.isoformat(),
            }
            digest["reason_code"] = "POLICY_BLOCKED"
        else:  # needs_human — waits on its own approval card
            if decision.request is not None:
                item.approval_request_id = decision.request.id
        digests.append(digest)
    await db.flush()
    return digests


async def _create_and_start_experiment_for_item(db: AsyncSession, item):
    """Create the Experiment row from an approved kind="experiment" item's
    payload and start it (baseline freeze + overlay). On success the item
    flips to ``executing`` with the experiment as its execution root; a
    start failure (e.g. target deleted while awaiting approval) marks the
    item ``failed`` instead of raising — the review must not die on it."""
    from packages.core.experiments import ExperimentError, start_experiment
    from packages.core.models.experiment import Experiment

    payload = dict(item.payload or {})
    experiment = Experiment(
        entity_id=item.entity_id,
        workspace_id=item.workspace_id,
        proposal_item_id=item.id,
        hypothesis=str(payload.get("hypothesis") or ""),
        scope={
            "target_kind": payload.get("target_kind"),
            "target_id": payload.get("target_id"),
            "max_runs": payload.get("max_runs"),
            "duration_days": payload.get("duration_days") or 7,
        },
        success_metrics=payload.get("success_metrics") or {},
        guardrails=payload.get("guardrails") or {},
        overlay_patch=payload.get("overlay_patch") or {},
        status="pending",
    )
    db.add(experiment)
    await db.flush()
    try:
        await start_experiment(db, experiment)
    except ExperimentError as exc:
        logger.warning(
            "Strategist: experiment item %s failed to start: %s", item.id, exc,
        )
        item.status = "failed"
        item.finished_at = datetime.now(timezone.utc)
        decision = dict(item.decision or {})
        decision["start_error"] = str(exc)
        item.decision = decision
        experiment.status = "rolled_back"
        await db.flush()
        return None
    item.status = "executing"
    item.execution_root_id = experiment.id
    await db.flush()
    return experiment


async def _mirror_item_decisions(
    db: AsyncSession,
    *,
    review_id: str,
    task_ids: list[str],
    approved: bool,
    actor_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    reason: Optional[str] = None,
    reason_code: Optional[str] = None,
    only_item_ids: Optional[list[str]] = None,
) -> None:
    """Mirror an approve/reject decision onto the review's proposal items.

    Runs off the review's ProposalRecord, NOT off the presence of tasks: a
    cohort made only of change / experiment items (no task rows at all) is
    a normal outcome and must still be resolved end-to-end. No-op when the
    review has no ProposalRecord (legacy / flag-off path), so pre-v2
    behavior is untouched.

    On approve the cohort's open HitlRequest (if any) is granted +
    consumed — the operator's card click IS the approval; on reject it is
    denied. The cohort request only ever covers kind="task" items, so it is
    left alone when this decision touched no tasks.

    ``only_item_ids`` narrows the non-task half of the cohort (the
    ``approve_selected`` path); ``None`` means every still-pending item.
    """
    from packages.core.proposals.service import decide_items, get_proposal_for_review

    record = await get_proposal_for_review(db, review_id)
    if record is None:
        return
    # Never pass ``None`` through: decide_items reads that as "every item of
    # the review", which would swallow the change/experiment items before
    # their own mirrors (and their per-item requests) ever run.
    selected_task_ids = list(task_ids or [])
    if approved:
        await decide_items(
            db,
            review_id=review_id,
            task_ids=selected_task_ids,
            decision="approved",
            actor_id=actor_id,
            execution_root_id=batch_id,
        )
    else:
        await decide_items(
            db,
            review_id=review_id,
            task_ids=selected_task_ids,
            decision="rejected",
            actor_id=actor_id,
            reason_code=reason_code or "OTHER",
            comment=reason,
        )

    from packages.core.governance.approvals import (
        consume_approval,
        deny_approval,
        find_requests_by_dedup,
        grant_approval,
    )

    if selected_task_ids:
        open_reqs = [
            r for r in await find_requests_by_dedup(
                db, entity_id=record.entity_id, dedup_key=f"proposal:{record.id}",
            )
            if r.status in APPROVAL_LIVE_STATUSES
        ]
        for req in open_reqs:
            if approved:
                if req.status == ApprovalStatus.PENDING:
                    await grant_approval(db, req, by_user_id=actor_id, via="chat_card")
                if req.status == ApprovalStatus.GRANTED:
                    await consume_approval(db, req)
            elif req.status == ApprovalStatus.PENDING:
                await deny_approval(db, req, by_user_id=actor_id, via="chat_card", reason=reason)

    # M13: experiment items ride the same cohort card. Approving the review
    # also approves its still-proposed experiment items (creating + starting
    # the Experiment and consuming their per-item requests); rejecting
    # closes them out with the same reason_code.
    await _mirror_experiment_items_on_cohort_decision(
        db,
        record=record,
        approved=approved,
        actor_id=actor_id,
        reason=reason,
        reason_code=reason_code,
        only_item_ids=only_item_ids,
    )

    # M10: configuration-change items ride the same cohort card.
    await _mirror_change_items_on_cohort_decision(
        db,
        record=record,
        approved=approved,
        actor_id=actor_id,
        reason=reason,
        reason_code=reason_code,
        only_item_ids=only_item_ids,
    )


async def _mirror_experiment_items_on_cohort_decision(
    db: AsyncSession,
    *,
    record,
    approved: bool,
    actor_id: Optional[str] = None,
    reason: Optional[str] = None,
    reason_code: Optional[str] = None,
    only_item_ids: Optional[list[str]] = None,
) -> None:
    from sqlalchemy import select as sa_select

    from packages.core.governance.approvals import (
        consume_approval,
        deny_approval,
        find_requests_by_dedup,
        grant_approval,
    )
    from packages.core.models.proposal import ProposalItemRecord

    if only_item_ids is not None and not only_item_ids:
        return
    query = sa_select(ProposalItemRecord).where(
        ProposalItemRecord.proposal_id == record.id,
        ProposalItemRecord.kind == "experiment",
        ProposalItemRecord.status == "proposed",
    )
    if only_item_ids is not None:
        query = query.where(ProposalItemRecord.id.in_(list(only_item_ids)))
    items = list((await db.execute(
        query.order_by(ProposalItemRecord.created_at.asc(), ProposalItemRecord.id.asc())
    )).scalars().all())
    if not items:
        return

    now = datetime.now(timezone.utc)
    for item in items:
        open_reqs = [
            r for r in await find_requests_by_dedup(
                db,
                entity_id=record.entity_id,
                dedup_key=f"proposal_item:{item.id}",
            )
            if r.status in APPROVAL_LIVE_STATUSES
        ]
        if approved:
            item.status = "approved"
            item.decided_at = now
            item.decision = {
                "decided_by": actor_id or "user",
                "decision": "approved",
                "reason_code": None,
                "decided_at": now.isoformat(),
            }
            # The operator's card click IS the approval — grant + consume.
            for req in open_reqs:
                if req.status == ApprovalStatus.PENDING:
                    await grant_approval(db, req, by_user_id=actor_id, via="chat_card")
                if req.status == ApprovalStatus.GRANTED:
                    await consume_approval(db, req)
            await _create_and_start_experiment_for_item(db, item)
        else:
            item.status = "rejected"
            item.decided_at = now
            item.decision = {
                "decided_by": actor_id or "user",
                "decision": "rejected",
                "reason_code": reason_code or "OTHER",
                "comment": reason,
                "decided_at": now.isoformat(),
            }
            for req in open_reqs:
                if req.status == ApprovalStatus.PENDING:
                    await deny_approval(
                        db, req, by_user_id=actor_id, via="chat_card", reason=reason,
                    )

    # Resolve the parent proposal if these were its last open items
    # (decide_items ran before experiment items were decided, so its own
    # resolution pass may have seen them still ``proposed``).
    remaining = (await db.execute(
        sa_select(ProposalItemRecord.id).where(
            ProposalItemRecord.proposal_id == record.id,
            ProposalItemRecord.status == "proposed",
        ).limit(1)
    )).scalar_one_or_none()
    if remaining is None and record.status == "open":
        record.status = "resolved"
        record.resolved_at = now
    await db.flush()


async def _mirror_change_items_on_cohort_decision(
    db: AsyncSession,
    *,
    record,
    approved: bool,
    actor_id: Optional[str] = None,
    reason: Optional[str] = None,
    reason_code: Optional[str] = None,
    only_item_ids: Optional[list[str]] = None,
) -> None:
    """Approving the cohort card also applies its pending change items
    (granting + consuming their per-item requests); rejecting denies them.

    Mirrors ``_mirror_experiment_items_on_cohort_decision`` exactly — the
    operator's click IS the approval, so the request is granted and then
    consumed at dispatch (consume-at-lease semantics).
    """
    from sqlalchemy import select as sa_select

    from packages.core.governance.approvals import (
        consume_approval,
        deny_approval,
        find_requests_by_dedup,
        grant_approval,
    )
    from packages.core.models.proposal import ProposalItemRecord
    from packages.core.proposals import CHANGE_KINDS, apply_change_item

    if only_item_ids is not None and not only_item_ids:
        return
    query = sa_select(ProposalItemRecord).where(
        ProposalItemRecord.proposal_id == record.id,
        ProposalItemRecord.kind.in_(CHANGE_KINDS),
        ProposalItemRecord.status == "proposed",
    )
    if only_item_ids is not None:
        query = query.where(ProposalItemRecord.id.in_(list(only_item_ids)))
    items = list((await db.execute(
        query.order_by(ProposalItemRecord.created_at.asc(), ProposalItemRecord.id.asc())
    )).scalars().all())
    if not items:
        return

    now = datetime.now(timezone.utc)
    for item in items:
        open_reqs = [
            r for r in await find_requests_by_dedup(
                db,
                entity_id=record.entity_id,
                dedup_key=f"proposal_item:{item.id}",
            )
            if r.status in APPROVAL_LIVE_STATUSES
        ]
        if approved:
            item.status = "approved"
            item.decided_at = now
            item.decision = {
                "decided_by": actor_id or "user",
                "decision": "approved",
                "reason_code": None,
                "decided_at": now.isoformat(),
            }
            for req in open_reqs:
                if req.status == ApprovalStatus.PENDING:
                    await grant_approval(db, req, by_user_id=actor_id, via="chat_card")
                if req.status == ApprovalStatus.GRANTED:
                    await consume_approval(db, req)
            await apply_change_item(db, item)
        else:
            item.status = "rejected"
            item.decided_at = now
            item.decision = {
                "decided_by": actor_id or "user",
                "decision": "rejected",
                "reason_code": reason_code or "OTHER",
                "comment": reason,
                "decided_at": now.isoformat(),
            }
            for req in open_reqs:
                if req.status == ApprovalStatus.PENDING:
                    await deny_approval(
                        db, req, by_user_id=actor_id, via="chat_card", reason=reason,
                    )

    # Resolve the parent proposal if these were its last open items.
    remaining = (await db.execute(
        sa_select(ProposalItemRecord.id).where(
            ProposalItemRecord.proposal_id == record.id,
            ProposalItemRecord.status == "proposed",
        ).limit(1)
    )).scalar_one_or_none()
    if remaining is None and record.status == "open":
        record.status = "resolved"
        record.resolved_at = now
    await db.flush()


# ── Approval ──────────────────────────────────────────────────────────

async def approve_proposal(
    db: AsyncSession,
    *,
    entity_id: str,
    review_id: str,
    only_task_ids: Optional[list[str]] = None,
    only_item_ids: Optional[list[str]] = None,
    actor_kind: str = "user",
    actor_id: Optional[str] = None,
) -> list[str]:
    """Approve a proposal cohort: its tasks AND its non-task items.

    Tasks with no predecessors flip to ``in_progress`` immediately, which
    fires the Planner hook. Dependent tasks stay ``pending`` until their
    predecessors complete and ``workspace_operation_service`` releases them.
    ``only_task_ids`` lets the operator approve a subset (``[]`` = no tasks
    at all, ``None`` = every proposed task of the review); ``only_item_ids``
    does the same for the change / experiment items.

    Returns the task ids that moved — ``[]`` for an item-only cohort, which
    is a normal outcome, not a drift signal.
    """
    all_rows = await _find_proposed(db, entity_id, review_id, None)
    rows = (
        []
        if only_task_ids is not None and not only_task_ids
        else _selected_rows_with_required_dependencies(all_rows, only_task_ids)
    )
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
        # Apply details first so the status-transition ledger event sees the
        # work-batch root (update_task re-applies the same dict afterwards).
        t.details = details
        await update_task(db, t.id, entity_id, status=next_status, details=details)
        moved.append(t.id)
        from packages.core.ledger.adapters import record_proposal_item_decision
        await record_proposal_item_decision(
            db, t, review_id=review_id, approved=True, batch_id=batch_id,
            actor_kind=actor_kind, actor_id=actor_id,
        )
    if moved:
        await _record_proposal_approval_activity(
            db,
            tasks=rows,
            review_id=review_id,
            task_ids=moved,
            batch_id=batch_id,
        )
    # M7: mirror the decision onto proposal_items (v2 reviews only). Runs
    # unconditionally — a cohort can be items-only, and then there is no
    # task row to hang the decision off.
    await _mirror_item_decisions(
        db,
        review_id=review_id,
        task_ids=moved,
        approved=True,
        actor_id=actor_id,
        batch_id=batch_id,
        only_item_ids=only_item_ids,
    )
    return moved


async def reject_proposal(
    db: AsyncSession,
    *,
    entity_id: str,
    review_id: str,
    only_task_ids: Optional[list[str]] = None,
    only_item_ids: Optional[list[str]] = None,
    reason: Optional[str] = None,
    reason_code: Optional[str] = None,
    actor_kind: str = "user",
    actor_id: Optional[str] = None,
) -> list[str]:
    """Reject a proposal cohort: its tasks AND its non-task items.

    ``only_task_ids`` / ``only_item_ids`` follow ``approve_proposal``'s
    convention (``[]`` = none of that half, ``None`` = all of it).
    """
    rows = (
        []
        if only_task_ids is not None and not only_task_ids
        else await _find_proposed(db, entity_id, review_id, only_task_ids)
    )
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
        from packages.core.ledger.adapters import record_proposal_item_decision
        await record_proposal_item_decision(
            db, t, review_id=review_id, approved=False,
            actor_kind=actor_kind, actor_id=actor_id, reason=reason,
            reason_code=reason_code,
        )
    # M7: mirror the decision onto proposal_items (v2 reviews only). Runs
    # unconditionally so an items-only cohort is closed out too.
    await _mirror_item_decisions(
        db,
        review_id=review_id,
        task_ids=cancelled,
        approved=False,
        actor_id=actor_id,
        reason=reason,
        reason_code=reason_code,
        only_item_ids=only_item_ids,
    )
    return cancelled


SUPERSEDE_REASON_CODE = "SUPERSEDED"
SUPERSEDE_REASON = (
    "Superseded: a newer Strategist review was requested before this "
    "proposal was decided."
)


async def list_open_proposals(db: AsyncSession, workspace_id: str) -> list[Task]:
    """Proposal tasks still awaiting an operator decision."""
    from packages.core.strategist.context import _open_proposed_tasks

    return await _open_proposed_tasks(db, workspace_id)


async def supersede_open_proposals(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    actor_id: Optional[str] = None,
) -> dict:
    """Reject every undecided proposal so a fresh review can replace them.

    Used by the explicit two-step chat flow: the operator is told what is
    open, says "replace them", and the second tool call lands here. The
    rejection rides the normal path (task cancelled, ledger fact, proposal
    items closed) with ``reason_code='SUPERSEDED'`` so the decision is
    audited — but that code is excluded from the learning loop, because
    "something newer arrived" is not a judgement about the proposal.
    """
    open_tasks = await list_open_proposals(db, workspace_id)
    by_review: dict[str, list[str]] = {}
    for task in open_tasks:
        details = task.details if isinstance(task.details, dict) else {}
        review_id = details.get("strategist_review_id")
        if not review_id:
            continue
        by_review.setdefault(str(review_id), []).append(task.id)

    rejected: list[str] = []
    for review_id, task_ids in by_review.items():
        rejected.extend(await reject_proposal(
            db,
            entity_id=entity_id,
            review_id=review_id,
            only_task_ids=task_ids,
            reason=SUPERSEDE_REASON,
            reason_code=SUPERSEDE_REASON_CODE,
            actor_kind="user",
            actor_id=actor_id,
        ))
    return {
        "rejected_task_ids": rejected,
        "review_ids": sorted(by_review),
        "reason_code": SUPERSEDE_REASON_CODE,
    }


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
        Task.status == TaskStatus.PROPOSED,
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
            Task.status == TaskStatus.PROPOSED,
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
        and msg.pending_action.get("kind") == PendingActionKind.APPROVE_PROPOSALS
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
                Task.status == TaskStatus.PROPOSED,
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
            kept_ids = {task.id for task in remaining}
            action["task_ids"] = [task.id for task in remaining]
            action["task_titles"] = [task.title for task in remaining]
            if isinstance(action.get("tasks"), list):
                action["tasks"] = _prune_proposal_task_entries(action["tasks"], kept_ids)
            msg.pending_action = action
            msg.meta = _prune_proposal_meta(msg.meta, kept_ids)
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


def _prune_proposal_task_entries(entries: list, kept_ids: set[str]) -> list:
    """Drop structured card entries whose task was retired."""
    return [
        entry for entry in entries
        if isinstance(entry, dict) and str(entry.get("task_id")) in kept_ids
    ]


def _prune_proposal_meta(meta: Optional[dict], kept_ids: set[str]) -> Optional[dict]:
    """Keep ``meta["proposal"]["tasks"]`` in step with the surviving tasks."""
    if not isinstance(meta, dict):
        return meta
    payload = meta.get("proposal")
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        return meta
    updated = dict(meta)
    updated["proposal"] = {
        **payload,
        "tasks": _prune_proposal_task_entries(payload["tasks"], kept_ids),
    }
    return updated


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


_META_LEARNING_PATTERN = re.compile(
    r"(?i)\b(write|record|document|capture|consolidate|update|summari[sz]e)\b"
    r".{0,40}\b(learnings?|lessons learned|workspace memory|"
    r"LEARNINGS\.md|MEMORY\.md|STATE\.md)\b"
)
_MEMORY_DOC_PATTERN = re.compile(r"(?i)\b(?:LEARNINGS|MEMORY)\.md\b")


def _is_meta_learning_task(task: ProposedTask) -> bool:
    """True when a proposed task is meta/bookkeeping work about learnings.

    Conservative on purpose: a workspace legitimately producing
    educational/learning *content* must not be caught. Suppress only when
    BOTH hold:
      (a) the title/description matches a meta-writing pattern (verb +
          learnings/memory-doc object) or the title names LEARNINGS.md /
          MEMORY.md directly; AND
      (b) the task has no goal linkage — a real learning-content business
          task would carry `estimated_impact.goal_id`.
    Learnings already flow through the automatic learning pipeline
    (runtime evidence → learning candidates → operator resolve → memory
    apply), so they must never become proposed tasks.
    """
    impact = task.estimated_impact
    if impact is not None and impact.goal_id:
        return False
    text = f"{task.title}\n{task.description or ''}"
    if _META_LEARNING_PATTERN.search(text):
        return True
    return bool(_MEMORY_DOC_PATTERN.search(task.title))


def _suppress_meta_learning_proposals(proposal: Proposal) -> None:
    """Drop meta/bookkeeping learning tasks the learning pipeline owns."""
    kept = []
    dropped = []
    for task in proposal.tasks:
        if _is_meta_learning_task(task):
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
    titles = ", ".join(task.title for task in dropped[:3])
    suffix = "" if len(dropped) <= 3 else f" and {len(dropped) - 3} more"
    logger.info(
        "Strategist: suppressed %d meta/bookkeeping task proposal(s): %s%s",
        len(dropped),
        titles,
        suffix,
    )
    proposal.notes = _append_note(
        proposal.notes,
        f"Suppressed {len(dropped)} meta/bookkeeping task(s) — learnings are "
        "recorded automatically by the learning pipeline, not via proposed "
        f"tasks: {titles}{suffix}.",
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


# ── suppression bookkeeping ───────────────────────────────────────────

_SKIP_NOTICE_META_KEY = "strategist_review_skip"
_CONFLICT_SAMPLE_LIMIT = 10

_TRIGGER_NOUNS: dict[ReviewTriggerKind, str] = {
    ReviewTriggerKind.SCHEDULED: "scheduled review",
    ReviewTriggerKind.EVENT: "automatic review",
    ReviewTriggerKind.HUMAN_REQUESTED: "review you asked for",
}


def _trigger_noun(trigger: ReviewTrigger) -> str:
    return _TRIGGER_NOUNS[trigger.kind]


def open_proposal_conflict(open_tasks: list[Task]) -> dict:
    """Describe the undecided cohort that blocks a review.

    The shape is the contract the chat tool hands to the model when a
    human-requested review cannot run: how many proposals, what they are
    called, which review produced them, and when.
    """
    ordered = sorted(
        open_tasks,
        key=lambda task: (task.created_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    proposals: list[dict] = []
    review_ids: list[str] = []
    for task in ordered[:_CONFLICT_SAMPLE_LIMIT]:
        details = task.details if isinstance(task.details, dict) else {}
        review_id = details.get("strategist_review_id")
        if review_id and review_id not in review_ids:
            review_ids.append(str(review_id))
        proposals.append({
            "task_id": task.id,
            "title": task.title,
            "review_id": review_id,
            "proposed_at": task.created_at.isoformat() if task.created_at else None,
        })
    newest = ordered[0].created_at if ordered else None
    oldest = ordered[-1].created_at if ordered else None
    task_ids = sorted(task.id for task in open_tasks)
    return {
        "kind": "open_proposals",
        "open_count": len(open_tasks),
        "review_ids": review_ids,
        "review_id": review_ids[0] if review_ids else None,
        "proposals": proposals,
        "newest_proposed_at": newest.isoformat() if newest else None,
        "oldest_proposed_at": oldest.isoformat() if oldest else None,
        "task_ids": task_ids,
        "fingerprint": _fingerprint("open_proposals", task_ids),
    }


def _fingerprint(kind: str, parts: list[str]) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def _active_batch_fingerprint(active_batch: dict) -> str:
    return _fingerprint(
        "active_work_batch",
        [str(active_batch.get("batch_id") or "")]
        + sorted(str(x) for x in (active_batch.get("open_task_ids") or [])),
    )


def _open_proposals_skip_body(trigger: ReviewTrigger, conflict: dict) -> str:
    count = conflict["open_count"]
    plural = "s" if count != 1 else ""
    titles = [str(item.get("title") or "").strip() for item in conflict["proposals"]]
    listed = "; ".join(t for t in titles[:3] if t)
    lines = [
        f"Skipped the {_trigger_noun(trigger)}: {count} proposal{plural} from the "
        "last review " + ("are" if count != 1 else "is") + " still awaiting your decision."
    ]
    if listed:
        lines.append(f"Waiting on: {listed}.")
    lines.append(
        "Approve or reject them and the next review runs on its own — or ask "
        "me to start a fresh review and replace them."
    )
    return " ".join(lines)


def _active_batch_skip_body(trigger: ReviewTrigger, active_batch: dict) -> str:
    open_count = len(active_batch.get("open_task_ids") or [])
    plural = "s" if open_count != 1 else ""
    return (
        f"Skipped the {_trigger_noun(trigger)}: the current work batch still has "
        f"{open_count} task{plural} in flight. The next review runs once "
        "they finish or are closed out."
    )


async def _post_review_skip_notice(
    db: AsyncSession,
    workspace: Workspace,
    *,
    trigger: ReviewTrigger,
    fingerprint: str,
    body: str,
) -> bool:
    """Announce a suppressed review in workspace chat, at most once per state.

    Anti-spam: the notice carries the blocking state's fingerprint in the
    message metadata, and we re-post only when that fingerprint differs
    from the most recent notice. A cadence that keeps tripping over the
    same three undecided proposals therefore says so once; approving one
    of them (or a new cohort appearing) changes the fingerprint and earns
    a fresh line.
    """
    try:
        conv = await chat_service.ensure_main_conversation(
            db, entity_id=workspace.entity_id, workspace_id=workspace.id,
        )
        last_meta = (await db.execute(
            select(Message.meta)
            .where(
                Message.conversation_id == conv.id,
                Message.meta[_SKIP_NOTICE_META_KEY].isnot(None),
            )
            .order_by(Message.id.desc())
            .limit(1)
        )).scalars().first()
        previous = (
            last_meta.get(_SKIP_NOTICE_META_KEY)
            if isinstance(last_meta, dict) else None
        )
        if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
            return False

        await chat_service.post_message(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            body=body,
            # Same informational variant the policy-blocked proposal card
            # uses: a proposal-plane message with no buttons attached.
            message_kind="proposal",
            author_kind="agent",
            refs=[{"type": "workspace", "id": workspace.id}],
            pending_action=None,
            meta={
                _SKIP_NOTICE_META_KEY: {
                    "fingerprint": fingerprint,
                    "trigger_kind": trigger.kind.value,
                },
            },
        )
        await db.commit()
        return True
    except Exception:  # noqa: BLE001 — a missing notice must not fail a review
        logger.warning(
            "Strategist: failed to post skip notice for workspace %s",
            workspace.id, exc_info=True,
        )
        return False


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
    trigger: ReviewTrigger,
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
                "trigger_kind": trigger.kind.value,
                "trigger_detail": trigger.detail,
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


# ── Non-task item digests for the cohort card ─────────────────────────

# target_kind → the noun an operator recognises.
_TARGET_NOUN: dict[str, str] = {
    "scheduled_job": "automation",
    "workflow_binding": "workflow binding",
    "workflow_definition": "workflow",
    "goal": "goal",
}

# operation → the verb that opens the one-line summary.
_OPERATION_VERB: dict[str, str] = {
    "create": "Create",
    "update": "Update",
    "pause": "Pause",
    "resume": "Resume",
    "delete": "Delete",
    "archive": "Archive",
    "update_deadline": "Change deadline of",
}

# Non-task kinds that wait on the cohort card. human_request items never
# do — they open a HumanCommitment answered from the Human queue.
_CARD_ITEM_KINDS: tuple[str, ...] = (
    "automation_change", "workflow_change", "goal_change", "experiment",
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _pending_item_digests(db: AsyncSession, *, review_id: str) -> list[dict]:
    """Compact renderings of the non-task items still awaiting a human
    decision, for the proposal card's ``pending_action``.

    Shape per item: ``{item_id, kind, action_key, risk_level, summary}``.
    ``summary`` is deterministic (operation + target name), never a second
    LLM call — the card must render identically on every replay.
    """
    from packages.core.models.proposal import ProposalItemRecord, ProposalRecord

    items = list((await db.execute(
        select(ProposalItemRecord)
        .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
        .where(
            ProposalRecord.review_id == review_id,
            ProposalItemRecord.status == "proposed",
            ProposalItemRecord.kind.in_(_CARD_ITEM_KINDS),
        )
        .order_by(ProposalItemRecord.created_at.asc(), ProposalItemRecord.id.asc())
    )).scalars().all())
    return [
        {
            "item_id": item.id,
            "kind": item.kind,
            "action_key": item.action_key,
            "risk_level": item.risk_level,
            "summary": await _proposal_item_summary(db, item),
        }
        for item in items
    ]


async def _proposal_item_summary(db: AsyncSession, item) -> str:
    """One human-readable line for a non-task proposal item."""
    payload = item.payload if isinstance(item.payload, dict) else {}
    if item.kind == "experiment":
        text = str(payload.get("hypothesis") or "").strip()
        return _clip(text or str(payload.get("experiment_key") or "experiment"), 140)

    operation = str(payload.get("operation") or "").strip()
    target_kind = str(payload.get("target_kind") or "").strip()
    noun = _TARGET_NOUN.get(target_kind) or (target_kind.replace("_", " ") or "target")
    name = await _change_target_name(db, target_kind, payload)
    quoted = f' "{_clip(name, 60)}"' if name else ""

    if operation == "update_target":
        direction = await _goal_target_direction(db, payload)
        return f"{direction} target of{quoted or ' the goal'}"
    if operation == "update":
        patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
        fields = ", ".join(sorted(str(key) for key in patch)[:3])
        suffix = f" ({fields})" if fields else ""
        return f"Update {noun}{quoted}{suffix}"
    verb = _OPERATION_VERB.get(operation) or (
        operation.replace("_", " ").capitalize() if operation else "Change"
    )
    return f"{verb} {noun}{quoted}".strip()


async def _change_target_name(
    db: AsyncSession, target_kind: str, payload: dict,
) -> Optional[str]:
    """Display name of a change item's target — one PK lookup, no joins.

    ``create`` items have no target yet, so the proposed name/title in the
    patch stands in for it.
    """
    from packages.core.proposals.change_executor import MODEL_BY_TARGET_KIND

    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
    target_id = payload.get("target_id")
    if not target_id:
        return str(patch.get("name") or patch.get("title") or "").strip() or None
    model = MODEL_BY_TARGET_KIND.get(target_kind)
    if model is None:
        return None
    row = await db.get(model, str(target_id))
    if row is None:
        return None
    label = getattr(row, "name", None) or getattr(row, "title", None)
    return str(label).strip() if label else None


async def _goal_target_direction(db: AsyncSession, payload: dict) -> str:
    """"Raise" / "Lower" / "Change" — read off the patch vs the live goal."""
    from decimal import Decimal, InvalidOperation

    from packages.core.models.goal import Goal

    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
    proposed = patch.get("target_value")
    target_id = payload.get("target_id")
    if proposed is None or not target_id:
        return "Change"
    goal = await db.get(Goal, str(target_id))
    current = getattr(goal, "target_value", None) if goal is not None else None
    if current is None:
        return "Change"
    try:
        if Decimal(str(proposed)) > Decimal(str(current)):
            return "Raise"
        if Decimal(str(proposed)) < Decimal(str(current)):
            return "Lower"
    except (InvalidOperation, ValueError):
        return "Change"
    return "Change"


# ── Chat surfacing ────────────────────────────────────────────────────

async def _post_human_request_chat(
    workspace: Workspace,
    human_requests: list[dict],
) -> None:
    """One plain informational chat message per human request (M10).

    Deliberately NO ``pending_action``: the frontend's ChatActionCard
    vocabulary has no human_request kind yet, so the actioning surface
    is the Human queue (M9 `/human-queue` + the respond endpoint); this
    message just makes the ask visible where the operator already looks.
    Best-effort, own session — mirrors ``_post_proposal_chat``.
    """
    try:
        from packages.core.database import async_session
        async with async_session() as db:
            for entry in human_requests:
                kind_label = str(entry.get("request_kind") or "input").replace("_", " ")
                lines = [
                    f"🙋 Strategist needs a human {kind_label}: {entry.get('question')}",
                ]
                if entry.get("role_required"):
                    lines.append(f"Role: {entry['role_required']}")
                if entry.get("expected_by"):
                    lines.append(f"Needed by: {entry['expected_by']}")
                lines.append(
                    "Respond (or decline) from the workspace Human queue."
                )
                await chat_service.post_message(
                    db,
                    entity_id=workspace.entity_id,
                    workspace_id=workspace.id,
                    body="\n".join(lines),
                    message_kind="hitl_request",
                    author_kind="agent",
                    refs=[{
                        "type": "human_commitment",
                        "id": entry.get("commitment_id"),
                    }],
                )
            await db.commit()
    except Exception:
        logger.warning("Strategist: failed to post human request chat", exc_info=True)


# ── Proposal card payload ─────────────────────────────────────────────
#
# The card is built from typed data, not from prose. ``_post_proposal_chat``
# emits one structured entry per task (``pending_action["tasks"]`` and
# ``meta["proposal"]["tasks"]``) and renders the SAME values into the message
# body for surfaces that only ever see text (notifications, email, Slack).
# The frontend reads the structured entries; it never parses the body back.

#: Strategist priority scale (see RUNTIME_STRATEGIST_DEFAULT_PREAMBLE).
PROPOSAL_PRIORITY_WORDS = {
    5: "critical",
    4: "high",
    3: "medium",
    2: "low",
    1: "minimal",
}

#: Priorities worth calling out. 3 (medium) is the model's default, so
#: labelling it — or anything below it — adds noise without adding signal.
PROPOSAL_PROMINENT_PRIORITIES = frozenset({5, 4})


async def _proposal_impact_goals(
    entity_id: str, goal_ids: set[str],
) -> dict[str, dict]:
    """Resolve linked goals so the card can name what a metric_delta moves.

    Best-effort: an unreadable goal simply means the task's impact is
    omitted, never a half-filled payload.
    """
    if not goal_ids:
        return {}
    try:
        from packages.core.database import async_session

        async with async_session() as db:
            rows = (await db.execute(
                select(Goal.id, Goal.title, Goal.metric_key).where(
                    Goal.entity_id == entity_id,
                    Goal.id.in_(list(goal_ids)),
                )
            )).all()
        return {
            str(row.id): {"title": row.title, "metric_key": row.metric_key}
            for row in rows
        }
    except Exception:
        logger.debug("Strategist: goal lookup for proposal card failed", exc_info=True)
        return {}


def _proposal_task_entries(
    proposal: Proposal,
    task_ids: list[str],
    goals: dict[str, dict],
) -> list[dict]:
    """One typed entry per proposed task, for the card to render directly.

    Impact keys (``goal_id`` / ``goal_title`` / ``metric_key`` /
    ``metric_delta``) are present together or not at all: a predicted number
    with nothing to attach it to is not showable, so it is dropped.
    """
    entries: list[dict] = []
    for pt, task_id in zip(proposal.tasks, task_ids):
        entry: dict = {
            "task_id": task_id,
            "title": pt.title,
            "priority": int(pt.priority),
        }
        if pt.rationale:
            entry["rationale"] = pt.rationale
        impact = pt.estimated_impact
        goal = goals.get(str(impact.goal_id)) if (impact and impact.goal_id) else None
        if goal and impact.metric_delta is not None:
            entry["goal_id"] = str(impact.goal_id)
            entry["goal_title"] = goal.get("title")
            entry["metric_key"] = goal.get("metric_key")
            entry["metric_delta"] = float(impact.metric_delta)
        entries.append(entry)
    return entries


def _proposal_impact_label(entry: dict) -> str:
    """Human name for what a metric_delta moves: goal title, else metric key."""
    title = (entry.get("goal_title") or "").strip()
    if title:
        return title
    metric_key = (entry.get("metric_key") or "").strip()
    return metric_key.replace("_", " ") if metric_key else ""


def _proposal_task_body_line(entry: dict) -> str:
    """The task's line in the message body — same values as the card, in words.

    Reads naturally in a notification or an email digest, where no card and
    no ``pending_action`` ever arrive.
    """
    parts: list[str] = []
    priority = entry.get("priority")
    if priority in PROPOSAL_PROMINENT_PRIORITIES:
        parts.append(f"{PROPOSAL_PRIORITY_WORDS[priority]} priority")
    if entry.get("metric_delta") is not None:
        label = _proposal_impact_label(entry)
        delta = f"{entry['metric_delta']:+g}"
        parts.append(
            f"expected {delta} toward “{label}”" if label else f"expected {delta}"
        )
    suffix = f" — {', '.join(parts)}" if parts else ""
    return f"  • {entry['title']}{suffix}"


async def _post_proposal_chat(
    workspace: Workspace,
    proposal: Proposal,
    task_ids: list[str],
    *,
    auto_approved: bool = False,
    policy_denied: bool = False,
    pending_items: Optional[list[dict]] = None,
    auto_approved_action_key: Optional[str] = None,
) -> None:
    """Single proposal card in the workspace_main conversation.

    Uses ``message_kind='proposal'`` + ``pending_action`` so the chat
    UI renders [Approve all] [Always approve] [Reject all] buttons while the
    wire contract stays on the canonical approve/always_approve/reject values.
    Fine-grained pick still works through the API (``only_task_ids`` parameter).

    ``pending_items`` carries the non-task items of the cohort that are
    waiting on a human (change kinds / experiments). They ride the same
    card, so a proposal made only of them still gets one — the existing
    ``task_ids`` / ``task_titles`` keys are untouched.

    Per-task display data travels typed, in ``pending_action["tasks"]`` and
    ``meta["proposal"]["tasks"]``: priority as a number, and the predicted
    ``metric_delta`` together with the goal it moves. The frontend renders
    those; it does not parse the message body.

    ``policy_denied=True`` (M8 hard block) posts the informational variant:
    the cohort was auto-rejected by governance policy, so no approval
    buttons are rendered.

    ``auto_approved_action_key`` names the standing grant that authorised an
    auto-execution, so the card says what let it run and where to change it.
    ``None`` with ``auto_approved=True`` means the legacy workspace-wide
    boolean did it — worded differently on purpose.
    """
    items = [item for item in (pending_items or []) if isinstance(item, dict)]
    goal_ids = {
        str(t.estimated_impact.goal_id)
        for t in proposal.tasks
        if t.estimated_impact
        and t.estimated_impact.goal_id
        and t.estimated_impact.metric_delta is not None
    }
    task_entries = _proposal_task_entries(
        proposal,
        task_ids,
        await _proposal_impact_goals(workspace.entity_id, goal_ids),
    )
    auto_approve_label = (
        strategist_action_label(auto_approved_action_key)
        if auto_approved_action_key else None
    )
    auto_approve_reason = (
        f"Auto-approved by your standing approval for “{auto_approve_label}”. "
        "Manage this in Settings → Approval automation."
        if auto_approve_label
        else (
            "Started automatically by workspace-wide auto-approval. "
            "Manage this in Settings → Approval automation."
        )
    )
    if policy_denied:
        body_lines = [f"📋 Strategist proposal blocked by governance policy — {proposal.summary}"]
    elif auto_approved:
        body_lines = [f"📋 Strategist proposal auto-approved — {proposal.summary}"]
    else:
        body_lines = [f"📋 Strategist proposal — {proposal.summary}"]
    if proposal.tasks:
        body_lines.append("")
        for entry in task_entries:
            body_lines.append(_proposal_task_body_line(entry))
            if entry.get("rationale"):
                body_lines.append(f"      _{entry['rationale']}_")
    elif not items:
        body_lines.append("\n_(no actionable tasks this cycle)_")
    if items:
        body_lines.append("")
        for item in items:
            kind_label = str(item.get("kind") or "item").replace("_", " ")
            body_lines.append(f"  • [{kind_label}] {item.get('summary') or ''}".rstrip())
    if proposal.notes:
        body_lines.append("")
        body_lines.append(f"📝 Notes: {proposal.notes}")
    # Informational footers, kept as data so the card renders them without
    # fishing them back out of the body.
    footnotes: list[str] = []
    if auto_approved and task_ids:
        footnotes.append(auto_approve_reason)
        body_lines.append("")
        body_lines.append(f"✓ {auto_approve_reason}")
    if policy_denied and task_ids:
        footnotes.append(
            "These tasks were rejected automatically: the workspace governance "
            "policy hard-blocks this action. Review the policy to change this."
        )
        body_lines.append("")
        body_lines.append(f"⛔ {footnotes[-1]}")

    pending_action = (
        {
            "kind": PendingActionKind.APPROVE_PROPOSALS.value,
            "review_id": proposal.review_id,
            "task_ids": task_ids,
            # Kept for older clients and for surfaces that only need labels;
            # ``tasks`` is the typed payload the card actually renders from.
            "task_titles": [t.title for t in proposal.tasks],
            "tasks": task_entries,
            "items": items,
            "options": list(DEFAULT_APPROVAL_OPTIONS),
        }
        if (task_ids or items) and not auto_approved and not policy_denied
        else None
    )

    # Auto-approved and policy-denied cards carry no ``pending_action`` at
    # all, yet they render the same task list. ``meta["proposal"]`` is the
    # one place the structured payload always lives.
    meta = {
        "proposal": {
            "review_id": proposal.review_id,
            "summary": proposal.summary,
            "notes": proposal.notes,
            "tasks": task_entries,
            "items": items,
            "footnotes": footnotes,
        }
    }

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
                meta=meta,
            )
            await db.commit()

        # Notify all entity users (separate session so failures are isolated).
        try:
            from packages.core.services.notification_service import create_notification
            from packages.core.models.user import User
            from sqlalchemy import select

            task_count = len(task_ids)
            # An items-only cohort has no tasks to count — say "change"
            # instead of lying about "0 tasks".
            count_label = (
                f"{task_count} task{'s' if task_count != 1 else ''}"
                if task_count or not items
                else f"{len(items)} change{'s' if len(items) != 1 else ''}"
            )
            raw_title = (
                f"Proposal auto-approved: {proposal.summary or count_label}"
                if auto_approved
                else f"New proposal: {proposal.summary or count_label}"
            )
            notif_title = raw_title[:490] + "…" if len(raw_title) > 490 else raw_title
            notif_body = (
                f"{workspace.name} — started {count_label}. {auto_approve_reason}"
                if auto_approved
                else (
                    f"{workspace.name} — Strategist proposed {count_label}. "
                    "Review and approve to start execution."
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
