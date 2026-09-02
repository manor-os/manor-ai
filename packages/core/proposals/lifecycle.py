"""Shared cohort-decision lifecycle for non-task Proposal items.

Item kinds own their execution handler; this module owns the mechanics that
must stay identical across every kind: selecting still-proposed items,
stamping the decision, settling per-item approval requests and resolving the
parent cohort once no item remains open.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
    ApprovalStatus,
)
from packages.core.constants.proposal import (
    ProposalDecisionKind,
    ProposalItemKind,
    ProposalItemStatus,
    ProposalStatus,
)
from packages.core.governance.approvals import (
    consume_approval,
    deny_approval,
    find_requests_by_dedup,
    grant_approval,
)
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord


ProposalItemApprovalHandler = Callable[[ProposalItemRecord], Awaitable[None]]


async def _live_approval_requests(
    db: AsyncSession,
    *,
    record: ProposalRecord,
    item: ProposalItemRecord,
):
    return [
        request
        for request in await find_requests_by_dedup(
            db,
            entity_id=record.entity_id,
            dedup_key=f"proposal_item:{item.id}",
        )
        if request.status in APPROVAL_LIVE_STATUSES
    ]


async def _settle_approval_requests(
    db: AsyncSession,
    *,
    requests,
    approved: bool,
    actor_id: str | None,
    actor_kind: str,
    reason: str | None,
) -> None:
    for request in requests:
        if approved:
            if request.status == ApprovalStatus.PENDING:
                await grant_approval(
                    db,
                    request,
                    by_user_id=actor_id,
                    via="chat_card",
                    authority_prechecked=actor_kind == "system",
                )
            if request.status == ApprovalStatus.GRANTED:
                await consume_approval(db, request)
        elif request.status == ApprovalStatus.PENDING:
            await deny_approval(
                db,
                request,
                by_user_id=actor_id,
                via="chat_card",
                reason=reason,
                authority_prechecked=actor_kind == "system",
            )


async def resolve_proposal_if_settled(
    db: AsyncSession,
    record: ProposalRecord,
    *,
    resolved_at: datetime | None = None,
) -> bool:
    """Resolve the parent iff no proposed item remains; return whether set."""
    remaining = (
        await db.execute(
            select(ProposalItemRecord.id)
            .where(
                ProposalItemRecord.proposal_id == record.id,
                ProposalItemRecord.status == ProposalItemStatus.PROPOSED,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if remaining is not None or record.status != ProposalStatus.OPEN:
        return False
    record.status = ProposalStatus.RESOLVED
    record.resolved_at = resolved_at or datetime.now(timezone.utc)
    return True


async def apply_non_task_cohort_decision(
    db: AsyncSession,
    *,
    record: ProposalRecord,
    kinds: Collection[ProposalItemKind | str],
    approved: bool,
    on_approved: ProposalItemApprovalHandler,
    actor_id: str | None = None,
    actor_kind: str = "user",
    reason: str | None = None,
    reason_code: str | None = None,
    only_item_ids: list[str] | None = None,
) -> list[ProposalItemRecord]:
    """Apply one cohort decision to a kind family and run its handler.

    The caller supplies only the kind-specific effect (start an experiment,
    apply a change, or dispatch a Flow).  Exceptions from that effect remain
    kind-owned and may either propagate or be translated by the handler.
    """
    if only_item_ids is not None and not only_item_ids:
        return []
    kind_values = [str(kind) for kind in kinds]
    query = select(ProposalItemRecord).where(
        ProposalItemRecord.proposal_id == record.id,
        ProposalItemRecord.kind.in_(kind_values),
        ProposalItemRecord.status == ProposalItemStatus.PROPOSED,
    )
    if only_item_ids is not None:
        query = query.where(ProposalItemRecord.id.in_(list(only_item_ids)))
    items = list(
        (
            await db.execute(
                query.order_by(
                    ProposalItemRecord.created_at.asc(),
                    ProposalItemRecord.id.asc(),
                )
            )
        ).scalars().all()
    )

    now = datetime.now(timezone.utc)
    decision_kind = (
        ProposalDecisionKind.APPROVED
        if approved
        else ProposalDecisionKind.REJECTED
    )
    for item in items:
        requests = await _live_approval_requests(db, record=record, item=item)
        item.status = (
            ProposalItemStatus.APPROVED
            if approved
            else ProposalItemStatus.REJECTED
        )
        item.decided_at = now
        item.decision = {
            "decided_by": actor_id or "user",
            "decision": decision_kind,
            "reason_code": None if approved else reason_code or "OTHER",
            "decided_at": now.isoformat(),
            **({"comment": reason} if not approved else {}),
        }
        await _settle_approval_requests(
            db,
            requests=requests,
            approved=approved,
            actor_id=actor_id,
            actor_kind=actor_kind,
            reason=reason,
        )
        if approved:
            await on_approved(item)

    await resolve_proposal_if_settled(db, record, resolved_at=now)
    await db.flush()
    return items
