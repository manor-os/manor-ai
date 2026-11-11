"""Something waits on a person: an open approval/input request, a commitment,
or a Workflow run paused at an approval or input wait."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.workflow_failure import strip_record_ids
from packages.core.attention.contracts import DesiredItem, stage_for_age
from packages.core.constants.approvals import ApprovalStatus, HitlType
from packages.core.constants.attention import AttentionKind, AttentionUrgency
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import HumanCommitment
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun

# Same ages as the reminders (hitl_reminders), so a stage is the age at
# which a reminder is due.
STAGE_HOURS = (24, 96)

# Wait steps that wait on a person. A timer wait resumes on its own.
_PERSON_WAITS = {"approval": "Approval needed: ", "input": "Input needed: ", "human_input": "Input needed: "}


def _one_line(value: object, limit: int = 160) -> str:
    return " ".join(strip_record_ids(str(value or "")).split())[:limit]


def _workflow_label(name: object) -> str:
    """A Workflow's name as a person reads it; generated ones are slugs."""
    text = _one_line(name, 80)
    return text.replace("_", " ").capitalize() if "_" in text and " " not in text else text


def _request_label(request: HitlRequest) -> str:
    from packages.core.services.hitl_reminders import summary

    return summary(request)


class NeedsInputWatcher:
    key = "needs_input"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        items: list[DesiredItem] = []
        requests = (await db.execute(
            select(HitlRequest).where(
                HitlRequest.status == ApprovalStatus.PENDING.value,
                HitlRequest.workspace_id.is_not(None),
                # A Strategist cohort's card is a decision, watched as such.
                or_(HitlRequest.resource_kind.is_(None), HitlRequest.resource_kind != "proposal"),
            )
        )).scalars().all()
        for request in requests:
            asks_approval = request.hitl_type == HitlType.AUTHORIZE.value
            label = _request_label(request)
            items.append(DesiredItem(
                entity_id=request.entity_id,
                workspace_id=request.workspace_id,
                kind=AttentionKind.NEEDS_INPUT.value,
                subject_type="hitl_request",
                subject_id=request.id,
                urgency=AttentionUrgency.ACT_TODAY.value,
                title=("Approval needed: " if asks_approval else "Input needed: ") + label,
                opened_at=request.created_at,
                stage=stage_for_age(request.created_at, now, STAGE_HOURS),
                evidence={
                    "label": label,
                    "hitl_request_id": request.id,
                    "hitl_type": request.hitl_type,
                    "action_key": request.action_key,
                    "task_id": request.origin_task_id,
                },
            ))

        commitments = (await db.execute(
            select(HumanCommitment).where(HumanCommitment.status == "waiting")
        )).scalars().all()
        for commitment in commitments:
            label = _one_line(commitment.expected_input) or commitment.request_kind.replace("_", " ")
            items.append(DesiredItem(
                entity_id=commitment.entity_id,
                workspace_id=commitment.workspace_id,
                kind=AttentionKind.NEEDS_INPUT.value,
                subject_type="human_commitment",
                subject_id=commitment.id,
                urgency=AttentionUrgency.ACT_TODAY.value,
                title="Input needed: " + label,
                opened_at=commitment.requested_at,
                stage=stage_for_age(commitment.requested_at, now, STAGE_HOURS),
                evidence={
                    "label": label,
                    "commitment_id": commitment.id,
                    "request_kind": commitment.request_kind,
                    "source_kind": commitment.source_kind,
                    "source_id": commitment.source_id,
                    "expected_by": commitment.expected_by.isoformat() if commitment.expected_by else None,
                },
            ))
        items.extend(await _workflow_waits(db, now))
        return items


def _wait_started_at(run: WorkflowRun) -> datetime:
    """When the run paused at its current step: its last trace entry for that step."""
    for entry in reversed(run.execution_trace or []):
        if isinstance(entry, dict) and entry.get("node_id") == run.current_step_id:
            try:
                started = datetime.fromisoformat(str(entry.get("completed_at") or entry.get("started_at")))
            except ValueError:
                break
            return started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    fallback = run.updated_at or run.created_at
    return fallback if fallback.tzinfo else fallback.replace(tzinfo=timezone.utc)


def _wait_subject_id(run: WorkflowRun) -> str:
    """One item per wait: a run that moves on to another wait is a new situation."""
    step = str(run.current_step_id or "")
    subject = f"{run.id}:{step}"
    return subject if len(subject) <= 64 else f"{run.id}:{hashlib.sha256(step.encode()).hexdigest()[:24]}"


async def _workflow_waits(db: AsyncSession, now: datetime) -> list[DesiredItem]:
    """Runs paused at a wait step that asks a person to approve or answer.

    A run with a pending HitlRequest parked on it (a tool approval inside a
    step) is not listed: that request is already an item of its own.
    """
    from packages.core.services.workflow_runtime_approvals import CONTEXT_KEY

    parked_run_id = HitlRequest.context[(CONTEXT_KEY, "payload", "workflow_run_id")].as_string()
    parked_on_requests = set((await db.execute(
        select(parked_run_id).where(
            HitlRequest.status == ApprovalStatus.PENDING.value,
            parked_run_id.is_not(None),
        )
    )).scalars().all())
    rows = (await db.execute(
        select(WorkflowRun, WorkflowDefinition.name)
        .outerjoin(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_id)
        .where(
            WorkflowRun.status == "paused",
            WorkflowRun.workspace_id.is_not(None),
            WorkflowRun.current_step_id.is_not(None),
        )
    )).all()
    items: list[DesiredItem] = []
    for run, workflow_name in rows:
        result = (run.step_results or {}).get(run.current_step_id)
        if not isinstance(result, dict) or result.get("status") != "paused" or run.id in parked_on_requests:
            continue
        prefix = _PERSON_WAITS.get(str(result.get("wait_type") or ""))
        if prefix is None:
            continue
        label = _one_line(result.get("review_title") or result.get("output") or run.current_step_id)
        if workflow_name:
            # Staging 2026-11-04: "client_article · Review finished Word 01M6G400…".
            label = f"{_workflow_label(workflow_name)} · {label}" if label else _workflow_label(workflow_name)
        opened_at = _wait_started_at(run)
        items.append(DesiredItem(
            entity_id=run.entity_id,
            workspace_id=run.workspace_id,
            kind=AttentionKind.NEEDS_INPUT.value,
            subject_type="workflow_run",
            subject_id=_wait_subject_id(run),
            urgency=AttentionUrgency.ACT_TODAY.value,
            title=prefix + label,
            opened_at=opened_at,
            stage=stage_for_age(opened_at, now, STAGE_HOURS),
            evidence={
                "label": label,
                "workflow_run_id": run.id,
                "workflow_id": run.workflow_id,
                "step_id": run.current_step_id,
                "wait_type": result.get("wait_type"),
            },
        ))
    return items
