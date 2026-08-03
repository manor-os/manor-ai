"""Proposal / ProposalItem persistence + decision bookkeeping (M7).

The v1 shape: on the strategist_review_v2 path each persisted proposed
``Task`` also gets a ``proposal_items`` row (kind="task"). The item layer
never drives execution in v1 — the existing Task status flow does — it
records the governed decision (who approved/rejected what, why, and
which execution root it became) so M9/M10 can build on it.

All functions are AsyncSession-first and flush (never commit); callers
own the transaction, matching the strategist service conventions.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.proposals.constants import (
    EXPERIMENT_ACTION_KEY,
    EXPERIMENT_MEDIUM_RISK_MAX_COST,
    HUMAN_REQUEST_ACTION_KEY,
    REASON_CODES,
    TASK_ACTION_KEY,
    change_action_key,
    change_risk_level,
)

logger = logging.getLogger(__name__)

_ITEM_KEY_MAX = 40


def _normalize_item_key(value: str | None, fallback: str = "item") -> str:
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or fallback).strip().lower())
    base = re.sub(r"_+", "_", base).strip("_") or fallback
    return base[:_ITEM_KEY_MAX]


async def create_proposal_with_items(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    review_id: str,
    summary: str,
    notes: Optional[str] = None,
    persisted_tasks: list[tuple],  # list[(ProposedTask, Task)]
) -> ProposalRecord:
    """Persist one ProposalRecord + one kind="task" item per persisted Task.

    ``persisted_tasks`` pairs each validated ``ProposedTask`` with the Task
    row ``_persist_tasks`` wrote for it. Item keys mirror the tasks'
    ``strategist_task_key`` (clipped to the item_key column width, deduped
    within the proposal) so ``depends_on_item_keys`` line up with
    ``details.depends_on_task_keys``.
    """
    record = ProposalRecord(
        entity_id=entity_id,
        workspace_id=workspace_id,
        review_id=review_id,
        summary=summary,
        notes=notes,
        status="open",
    )
    db.add(record)
    await db.flush()

    # First pass: task_key → item_key map so dependency refs translate 1:1.
    used: dict[str, int] = {}
    item_key_by_task_key: dict[str, str] = {}
    resolved_keys: list[str] = []
    for proposed, task in persisted_tasks:
        details = getattr(task, "details", None) or {}
        raw_key = details.get("strategist_task_key") or proposed.task_key or proposed.title
        key = _normalize_item_key(raw_key, fallback="task")
        count = used.get(key, 0)
        used[key] = count + 1
        if count:
            suffix = f"_{count + 1}"
            key = key[: _ITEM_KEY_MAX - len(suffix)] + suffix
        # First occurrence wins (mirrors _persist_tasks, where dependency
        # refs to a duplicated base key resolve to the first task).
        item_key_by_task_key.setdefault(_normalize_item_key(raw_key, fallback="task"), key)
        if raw_key:
            item_key_by_task_key.setdefault(str(raw_key), key)
        resolved_keys.append(key)

    for (proposed, task), item_key in zip(persisted_tasks, resolved_keys):
        payload = proposed.model_dump(mode="json")
        payload["task_id"] = task.id
        basis = (
            proposed.basis.model_dump(mode="json")
            if getattr(proposed, "basis", None) is not None
            else None
        )
        deps = [
            item_key_by_task_key.get(dep) or _normalize_item_key(dep)
            for dep in (proposed.depends_on_task_keys or [])
        ]
        db.add(ProposalItemRecord(
            proposal_id=record.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            item_key=item_key,
            kind="task",
            payload=payload,
            basis=basis,
            correlation_key=getattr(proposed, "correlation_key", None),
            risk_level="low",
            action_key=TASK_ACTION_KEY,
            depends_on_item_keys=deps or None,
            status="proposed",
        ))
    await db.flush()
    return record


async def create_human_request_items(
    db: AsyncSession,
    *,
    record: ProposalRecord,
    proposed_requests: list,  # list[ProposedHumanRequest]
) -> list[ProposalItemRecord]:
    """Persist kind="human_request" items on an existing proposal (M10).

    Per the M8 catalog human requests never mint a HitlRequest —
    each item is born ``approved`` with an auto decision; the caller
    (strategist service) then opens the HumanCommitment and flips the
    item to ``executing``. Item keys are prefixed ``hr_`` so they can
    never collide with task item keys.
    """
    existing_keys = set((await db.execute(
        select(ProposalItemRecord.item_key).where(
            ProposalItemRecord.proposal_id == record.id,
        )
    )).scalars().all())

    now = datetime.now(timezone.utc)
    items: list[ProposalItemRecord] = []
    for proposed in proposed_requests:
        key = _normalize_item_key(f"hr_{proposed.request_key}", fallback="hr")
        suffix_n = 1
        base_key = key
        while key in existing_keys:
            suffix_n += 1
            suffix = f"_{suffix_n}"
            key = base_key[: _ITEM_KEY_MAX - len(suffix)] + suffix
        existing_keys.add(key)

        item = ProposalItemRecord(
            proposal_id=record.id,
            entity_id=record.entity_id,
            workspace_id=record.workspace_id,
            item_key=key,
            kind="human_request",
            payload=proposed.model_dump(mode="json"),
            risk_level="low",
            action_key=HUMAN_REQUEST_ACTION_KEY,
            status="approved",
            decided_at=now,
            decision={
                "decided_by": None,
                "decision": "auto",
                "reason_code": None,
                "decided_at": now.isoformat(),
            },
        )
        db.add(item)
        items.append(item)
    await db.flush()
    return items


def experiment_risk_level(guardrails: Optional[dict]) -> str:
    """M8 catalog: risk by guardrails.max_cost (≤ $20 → medium, else high)."""
    guardrails = guardrails if isinstance(guardrails, dict) else {}
    try:
        max_cost = float(guardrails.get("max_cost", EXPERIMENT_MEDIUM_RISK_MAX_COST))
    except (TypeError, ValueError):
        max_cost = EXPERIMENT_MEDIUM_RISK_MAX_COST
    return "medium" if max_cost <= EXPERIMENT_MEDIUM_RISK_MAX_COST else "high"


async def create_experiment_items(
    db: AsyncSession,
    *,
    record: ProposalRecord,
    proposed_experiments: list,  # list[ProposedExperiment]
) -> list[ProposalItemRecord]:
    """Persist kind="experiment" items on an existing proposal (M13).

    Unlike human requests, experiments DO mint a per-item HitlRequest
    (caller's job, via ``resolve_approval``): items are born ``proposed``;
    the risk level is validator-computed from ``guardrails.max_cost`` per
    the M8 catalog, never model-self-reported. Item keys are prefixed
    ``xp_`` so they can never collide with task / human-request keys.
    """
    existing_keys = set((await db.execute(
        select(ProposalItemRecord.item_key).where(
            ProposalItemRecord.proposal_id == record.id,
        )
    )).scalars().all())

    items: list[ProposalItemRecord] = []
    for proposed in proposed_experiments:
        key = _normalize_item_key(f"xp_{proposed.experiment_key}", fallback="xp")
        suffix_n = 1
        base_key = key
        while key in existing_keys:
            suffix_n += 1
            suffix = f"_{suffix_n}"
            key = base_key[: _ITEM_KEY_MAX - len(suffix)] + suffix
        existing_keys.add(key)

        payload = proposed.model_dump(mode="json")
        item = ProposalItemRecord(
            proposal_id=record.id,
            entity_id=record.entity_id,
            workspace_id=record.workspace_id,
            item_key=key,
            kind="experiment",
            payload=payload,
            basis=None,
            risk_level=experiment_risk_level(payload.get("guardrails")),
            action_key=EXPERIMENT_ACTION_KEY,
            status="proposed",
        )
        db.add(item)
        items.append(item)
    await db.flush()
    return items


# item_key prefix per change kind — keeps change keys from ever colliding
# with task ("<task_key>") / human-request ("hr_") / experiment ("xp_") keys.
CHANGE_ITEM_KEY_PREFIX: dict[str, str] = {
    "automation_change": "ac",
    "workflow_change": "wc",
    "goal_change": "gc",
}


def _next_item_key(base: str, existing_keys: set[str]) -> str:
    key = _normalize_item_key(base, fallback="ch")
    base_key = key
    suffix_n = 1
    while key in existing_keys:
        suffix_n += 1
        suffix = f"_{suffix_n}"
        key = base_key[: _ITEM_KEY_MAX - len(suffix)] + suffix
    existing_keys.add(key)
    return key


async def create_change_items(
    db: AsyncSession,
    *,
    record: ProposalRecord,
    proposed_changes: list[tuple],  # list[(kind, ProposedChange)]
) -> list[ProposalItemRecord]:
    """Persist the configuration-change items on an existing proposal (M7).

    ``action_key`` comes from the M8 catalog
    (``ACTION_KEY_BY_KIND["<kind>.<operation>"]``) and ``risk_level`` from
    the M8 table (create/delete/archive = high, the reversible tweaks =
    medium) — never model-self-reported. Items are born ``proposed``; the
    caller runs the validator and then ``resolve_approval`` per item.
    ``expected_revision`` is mirrored onto its own column so the CAS token
    is queryable without digging into the payload JSONB.
    """
    existing_keys = set((await db.execute(
        select(ProposalItemRecord.item_key).where(
            ProposalItemRecord.proposal_id == record.id,
        )
    )).scalars().all())

    items: list[ProposalItemRecord] = []
    for kind, proposed in proposed_changes:
        prefix = CHANGE_ITEM_KEY_PREFIX.get(kind, "ch")
        key = _next_item_key(f"{prefix}_{proposed.change_key}", existing_keys)
        payload = proposed.model_dump(mode="json")
        basis = payload.pop("basis", None)
        item = ProposalItemRecord(
            proposal_id=record.id,
            entity_id=record.entity_id,
            workspace_id=record.workspace_id,
            item_key=key,
            kind=kind,
            payload=payload,
            basis=basis,
            correlation_key=(
                f"{proposed.target_kind}:{proposed.target_id}"[:96]
                if proposed.target_id else None
            ),
            risk_level=change_risk_level(proposed.operation),
            action_key=change_action_key(kind, proposed.operation),
            expected_revision=proposed.expected_revision,
            status="proposed",
        )
        db.add(item)
        items.append(item)
    await db.flush()
    return items


async def get_proposal_for_review(
    db: AsyncSession, review_id: str,
) -> Optional[ProposalRecord]:
    return (
        await db.execute(
            select(ProposalRecord)
            .where(ProposalRecord.review_id == review_id)
            .order_by(ProposalRecord.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_items_for_review(
    db: AsyncSession, review_id: str,
) -> list[ProposalItemRecord]:
    return list((
        await db.execute(
            select(ProposalItemRecord)
            .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
            .where(ProposalRecord.review_id == review_id)
            .order_by(ProposalItemRecord.created_at.asc(), ProposalItemRecord.id.asc())
        )
    ).scalars().all())


async def decide_items(
    db: AsyncSession,
    *,
    review_id: str,
    task_ids: Optional[list[str]] = None,
    decision: str,
    actor_id: Optional[str] = None,
    reason_code: Optional[str] = None,
    comment: Optional[str] = None,
    execution_root_id: Optional[str] = None,
) -> list[ProposalItemRecord]:
    """Record an approve/reject decision on kind="task" items.

    Finds still-``proposed`` items whose ``payload.task_id`` is in
    ``task_ids`` (``None`` = all items of the review), stamps status +
    decision JSONB + ``decided_at`` (+ ``execution_root_id`` when the
    approval already has an execution root, i.e. the work batch), and
    resolves the parent proposal when no items remain ``proposed``.
    Idempotent: already-decided items are left untouched.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be approved|rejected, got {decision!r}")
    if reason_code is not None and reason_code not in REASON_CODES:
        raise ValueError(
            f"unknown reason_code {reason_code!r}; must be one of {sorted(REASON_CODES)}"
        )

    items = await get_items_for_review(db, review_id)
    if not items:
        return []
    now = datetime.now(timezone.utc)
    selected: list[ProposalItemRecord] = []
    for item in items:
        if item.status != "proposed":
            continue
        if task_ids is not None:
            task_id = (item.payload or {}).get("task_id")
            if task_id not in task_ids:
                continue
        item.status = decision
        item.decided_at = now
        item.decision = {
            "decided_by": actor_id or "system",
            "decision": decision,
            "reason_code": reason_code,
            "comment": comment,
            "decided_at": now.isoformat(),
        }
        if execution_root_id and decision == "approved":
            item.execution_root_id = execution_root_id
        selected.append(item)

    # Resolve parent proposals with no open items left.
    proposal_ids = {item.proposal_id for item in items}
    for proposal_id in proposal_ids:
        remaining = [
            i for i in items
            if i.proposal_id == proposal_id and i.status == "proposed"
        ]
        if remaining:
            continue
        record = await db.get(ProposalRecord, proposal_id)
        if record is not None and record.status == "open":
            record.status = "resolved"
            record.resolved_at = now
    await db.flush()
    return selected
