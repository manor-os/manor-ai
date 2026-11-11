"""A Strategist proposal cohort waits for someone's decision."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.attention.contracts import DesiredItem, stage_for_age
from packages.core.attention.watchers.needs_input import STAGE_HOURS as REMINDER_STAGE_HOURS
from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.attention import AttentionKind, AttentionUrgency
from packages.core.constants.proposal import ProposalItemKind, ProposalItemStatus, ProposalStatus
from packages.core.constants.task import TaskStatus
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.task import Task

# The same stages as the cohort card's reminders; the card lapses, and the
# cohort with it, at hitl_reminders.UNANSWERED_AUTHORIZATION_HOURS.
STAGE_HOURS = REMINDER_STAGE_HOURS


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class DecisionPendingWatcher:
    key = "decision_pending"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        # (entity, workspace, review) → what is still undecided in it.
        cohorts: dict[tuple[str, str, str], dict] = {}

        def cohort(entity_id: str, workspace_id: str, review_id: str) -> dict:
            return cohorts.setdefault((entity_id, workspace_id, review_id), {
                "task_ids": [], "item_ids": [], "titles": [], "opened_at": None,
            })

        def seen(entry: dict, created_at: datetime) -> None:
            created_at = _aware(created_at)
            if entry["opened_at"] is None or created_at < entry["opened_at"]:
                entry["opened_at"] = created_at

        tasks = (await db.execute(
            select(Task).where(
                Task.status == TaskStatus.PROPOSED,
                Task.workspace_id.is_not(None),
                Task.details["strategist_review_id"].astext.is_not(None),
            )
        )).scalars().all()
        for task in tasks:
            entry = cohort(task.entity_id, task.workspace_id, str(task.details["strategist_review_id"]))
            entry["task_ids"].append(task.id)
            entry["titles"].append(task.title)
            seen(entry, task.created_at)

        item_rows = (await db.execute(
            select(ProposalItemRecord, ProposalRecord.review_id)
            .join(ProposalRecord, ProposalRecord.id == ProposalItemRecord.proposal_id)
            .where(
                ProposalItemRecord.status == ProposalItemStatus.PROPOSED,
                ProposalItemRecord.kind != ProposalItemKind.TASK,
                ProposalRecord.status == ProposalStatus.OPEN,
            )
        )).all()
        for item, review_id in item_rows:
            entry = cohort(item.entity_id, item.workspace_id, str(review_id))
            entry["item_ids"].append(item.id)
            seen(entry, item.created_at)

        summaries = {
            (record.workspace_id, record.review_id): (record.id, record.summary)
            for record in (await db.execute(
                select(ProposalRecord).where(
                    ProposalRecord.review_id.in_(sorted({key[2] for key in cohorts}) or [""]),
                )
            )).scalars().all()
        }

        # The cohort's card, whose reminders these stages replace.
        cards = {
            str(resource_id): request_id
            for request_id, resource_id in (await db.execute(
                select(HitlRequest.id, HitlRequest.resource_id).where(
                    HitlRequest.resource_kind == "proposal",
                    HitlRequest.status == ApprovalStatus.PENDING.value,
                    HitlRequest.resource_id.in_([value[0] for value in summaries.values()] or [""]),
                )
            )).all()
        }

        items: list[DesiredItem] = []
        for (entity_id, workspace_id, review_id), entry in cohorts.items():
            proposal_id, summary = summaries.get((workspace_id, review_id), (None, None))
            count = len(entry["task_ids"]) + len(entry["item_ids"])
            headline = summary or (entry["titles"][0] if entry["titles"] else f"{count} proposed change(s)")
            items.append(DesiredItem(
                entity_id=entity_id,
                workspace_id=workspace_id,
                kind=AttentionKind.DECISION_PENDING.value,
                subject_type="proposal_review",
                subject_id=review_id,
                urgency=AttentionUrgency.DECIDE.value,
                title=f"Proposal waiting for a decision: {headline}",
                opened_at=entry["opened_at"],
                stage=stage_for_age(entry["opened_at"], now, STAGE_HOURS),
                evidence={
                    "label": headline,
                    "review_id": review_id,
                    "proposal_id": proposal_id,
                    "hitl_request_id": cards.get(str(proposal_id)) if proposal_id else None,
                    "task_ids": sorted(entry["task_ids"]),
                    "item_ids": sorted(entry["item_ids"]),
                },
            ))
        return items
