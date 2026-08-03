"""Deterministic pre-approval validation for proposal items (M7).

Two responsibilities:

1. **basis hygiene (all kinds)** — ``basis`` is *validate-if-present*. The
   design doc's strict "reject items citing unknown reports" rule stays
   deferred: the Strategist prompt only SHOULDs the citations, so an
   invalid ref is a data-quality signal, not grounds for rejection.
   Invalid ``report_refs`` are stripped (so downstream consumers never
   chase dangling refs) and reported in the returned notes.

2. **change-kind gatekeeping (M7 step 3/4 + M10)** — for
   ``automation_change`` / ``workflow_change`` / ``goal_change`` items the
   validator is the fail-fast half of the revision CAS:

   * **cross-workspace guard** — the target row must belong to this
     item's workspace/entity, else ``INSUFFICIENT_DATA``.
   * **missing target** → ``INSUFFICIENT_DATA``. Deliberately *not*
     ``STALE_REVISION``: a revision can only be stale relative to a row
     that exists; a target the Strategist named but that no longer (or
     never did) exists means the briefing it reasoned from does not
     describe reality, which is exactly what INSUFFICIENT_DATA means.
   * **``expected_revision`` CAS precheck** — ``row.revision !=
     expected_revision`` → ``STALE_REVISION``. This is only fail-fast;
     the authoritative CAS runs under a row lock in
     ``change_executor.apply_change_item`` (the approval wait is exactly
     when a concurrent edit lands).
   * **correlation/duplicate check** — another OPEN change item (status
     proposed/approved/executing) already targets the same
     ``(target_kind, target_id)`` → the newer one is ``DUPLICATE``.

Rejected items are still persisted with their decision recorded — they
are facts and learning signal (M7: 被 validator 拒绝的 item 也持久化).

Deferred hooks (documented so later waves wire them here, not elsewhere):

* critical coverage gap → reject the affected kinds ``INSUFFICIENT_DATA``.
* risk computation for task items (v1 task items are always risk "low" /
  action "workspace.proposal.task").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, tuple_

from packages.core.models.proposal import ProposalItemRecord
from packages.core.proposals.constants import CHANGE_KINDS
from packages.core.revisions import StaleRevisionError, assert_revision

logger = logging.getLogger(__name__)

# A change item still "holds" its target while it is waiting for approval
# or being applied. Terminal statuses (rejected/failed/succeeded/…) free it.
OPEN_ITEM_STATUSES: tuple[str, ...] = ("proposed", "approved", "executing")


def _model_for(target_kind: str):
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    return {
        "scheduled_job": ScheduledJob,
        "workflow_binding": WorkflowBinding,
        "workflow_definition": WorkflowDefinition,
        "goal": Goal,
    }.get(target_kind)


def target_in_scope(row: Any, *, entity_id: str, workspace_id: str) -> bool:
    """Does ``row`` belong to this workspace (or its entity, for rows that
    are entity-scoped by design)?

    ``WorkflowDefinition`` has no workspace column at all (a template is
    entity-level); ``ScheduledJob`` may carry the workspace only inside
    ``execution_target`` (mirrors the automation_portfolio consolidator);
    ``WorkflowBinding`` / ``Goal`` may legitimately be entity-level
    (workspace_id NULL) — those are in scope for their own entity.
    """
    if getattr(row, "entity_id", None) not in (None, entity_id):
        return False
    if not hasattr(row, "workspace_id"):
        return True
    row_workspace = getattr(row, "workspace_id", None)
    if row_workspace == workspace_id:
        return True
    if row_workspace is not None:
        return False
    execution_target = getattr(row, "execution_target", None)
    if isinstance(execution_target, dict):
        return execution_target.get("workspace_id") in (None, workspace_id)
    return True


def _reject(item: ProposalItemRecord, reason_code: str, comment: str) -> None:
    now = datetime.now(timezone.utc)
    item.status = "rejected"
    item.decided_at = now
    item.decision = {
        "decided_by": None,
        "decision": "rejected",
        "reason_code": reason_code,
        "comment": comment,
        "decided_at": now.isoformat(),
    }


async def _validate_change_item(
    db, item: ProposalItemRecord,
) -> Optional[str]:
    """Run the change-kind gates. Returns a note when the item was rejected
    (the item itself carries the decision)."""
    payload = item.payload if isinstance(item.payload, dict) else {}
    target_kind = str(payload.get("target_kind") or "")
    operation = str(payload.get("operation") or "")
    target_id = payload.get("target_id")

    model = _model_for(target_kind)
    if model is None:
        note = f"item {item.item_key}: unknown target_kind {target_kind!r}"
        _reject(item, "INSUFFICIENT_DATA", note)
        return note

    if operation == "create":
        # Nothing to CAS or dedupe against — the row does not exist yet.
        return None

    if not target_id:
        note = f"item {item.item_key}: operation {operation!r} without target_id"
        _reject(item, "INSUFFICIENT_DATA", note)
        return note

    row = await db.get(model, str(target_id))
    if row is None:
        note = (
            f"item {item.item_key}: {target_kind} {target_id} does not exist "
            f"(the briefing it was proposed from no longer describes reality)"
        )
        _reject(item, "INSUFFICIENT_DATA", note)
        return note

    if not target_in_scope(
        row, entity_id=item.entity_id, workspace_id=item.workspace_id,
    ):
        note = (
            f"item {item.item_key}: {target_kind} {target_id} does not belong "
            f"to workspace {item.workspace_id}"
        )
        _reject(item, "INSUFFICIENT_DATA", note)
        return note

    # ── expected_revision CAS precheck (fail fast; executor re-checks) ──
    expected = payload.get("expected_revision")
    if expected is None:
        expected = item.expected_revision
    try:
        await assert_revision(row, expected)
    except StaleRevisionError as exc:
        note = f"item {item.item_key}: {exc}"
        _reject(item, "STALE_REVISION", note)
        return note

    # ── correlation/duplicate: one open change per target row ──────────
    await db.flush()
    conflicting = (await db.execute(
        select(ProposalItemRecord.item_key).where(
            ProposalItemRecord.workspace_id == item.workspace_id,
            ProposalItemRecord.kind.in_(CHANGE_KINDS),
            ProposalItemRecord.status.in_(OPEN_ITEM_STATUSES),
            ProposalItemRecord.id != item.id,
            ProposalItemRecord.payload["target_kind"].astext == target_kind,
            ProposalItemRecord.payload["target_id"].astext == str(target_id),
            # Only items that came FIRST hold the target — the newer
            # proposal is the duplicate.
            tuple_(
                ProposalItemRecord.created_at, ProposalItemRecord.id,
            ) < tuple_(item.created_at, item.id),
        ).limit(1)
    )).scalar_one_or_none()
    if conflicting is not None:
        note = (
            f"item {item.item_key}: another open change item ({conflicting}) "
            f"already targets {target_kind} {target_id}"
        )
        _reject(item, "DUPLICATE", note)
        return note
    return None


async def validate_items(
    db,
    review,
    report_rows,
    items: list[ProposalItemRecord],
) -> list[tuple[ProposalItemRecord, Optional[str]]]:
    """Validate items against this review's consolidation reports + live rows.

    Returns ``[(item, note_or_None), ...]`` — the note (when present)
    describes what was stripped/rejected for that item. Task /
    human_request / experiment items are never rejected here; change-kind
    items may be (they carry the decision, and the caller must skip any
    item whose ``status`` is no longer ``proposed``).
    """
    valid_refs: set[str] = set()
    for row in report_rows or []:
        row_id = getattr(row, "id", None)
        domain = getattr(row, "domain", None)
        if row_id:
            valid_refs.add(str(row_id))
        if domain:
            valid_refs.add(str(domain))

    results: list[tuple[ProposalItemRecord, Optional[str]]] = []
    for item in items:
        note: Optional[str] = None
        basis = item.basis if isinstance(item.basis, dict) else None
        if basis:
            report_refs = [str(r) for r in (basis.get("report_refs") or [])]
            kept = [r for r in report_refs if r in valid_refs]
            invalid = [r for r in report_refs if r not in valid_refs]
            if invalid:
                note = (
                    f"item {item.item_key}: stripped unknown report_refs "
                    f"{invalid} (valid = report ids/domains of review "
                    f"{getattr(review, 'id', None)!r})"
                )
                logger.info("Proposal validator: %s", note)
                item.basis = {**basis, "report_refs": kept}
        if item.kind in CHANGE_KINDS and item.status == "proposed":
            change_note = await _validate_change_item(db, item)
            if change_note:
                logger.info("Proposal validator rejected: %s", change_note)
                note = f"{note}; {change_note}" if note else change_note
        results.append((item, note))
    await db.flush()
    return results
