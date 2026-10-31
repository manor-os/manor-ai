"""Approval requests that belong to one user rather than to a Workspace chat.

Work a user sends to one of their own workers outside any Workspace (an
ad-hoc local action, ``submit_local_action``) waits, when the user's rules
ask, on a request only that user may decide. Their notification links to
``/approvals/<id>``, which reads and answers it here. Workspace cards and
conversation approvals keep their own surfaces and are not served here.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
from packages.core.constants.execution import ExecutionStepStatus
from packages.core.database import get_db
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.user import User

router = APIRouter(prefix="/api/v1/approval-requests", tags=["approval-requests"])


class ApprovalDecisionBody(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = None


async def _owned_request(db: AsyncSession, request_id: str, user: User, *, for_update: bool = False) -> HitlRequest:
    query = select(HitlRequest).where(HitlRequest.id == request_id)
    if for_update:
        query = query.with_for_update()
    request = (await db.execute(query)).scalar_one_or_none()
    if request is None or request.entity_id != user.entity_id:
        raise HTTPException(404, "Approval request not found")
    if (
        request.workspace_id is not None
        or request.origin_kind != ApprovalOriginKind.STEP.value
        or (request.context or {}).get("requested_by") != user.id
    ):
        # Workspace and conversation approvals are answered where they live;
        # another user's own request is theirs alone.
        raise HTTPException(403, "This approval is not yours to answer here")
    return request


def _view(request: HitlRequest) -> dict:
    return {
        "id": request.id,
        "status": request.status,
        "action_key": request.action_key,
        "risk_level": request.risk_level,
        "reason": request.reason,
        "hitl_type": request.hitl_type,
        "payload": request.payload or {},
        "step_id": request.origin_step_id,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
    }


@router.get("/{request_id}")
async def get_approval_request(
    request_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    return _view(await _owned_request(db, request_id, user))


@router.post("/{request_id}/decision")
async def decide_approval_request(
    request_id: str,
    body: ApprovalDecisionBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.governance.approvals import ApprovalAuthorityError, deny_approval, grant_approval

    request = await _owned_request(db, request_id, user, for_update=True)
    if request.status != ApprovalStatus.PENDING.value:
        raise HTTPException(409, f"This approval is already {request.status}")
    step = (
        await db.execute(
            select(ExecutionStep).where(ExecutionStep.id == request.origin_step_id).with_for_update()
        )
    ).scalar_one_or_none()
    try:
        if body.decision == "approve":
            await grant_approval(db, request, by_user_id=user.id, via="approval_page")
        else:
            await deny_approval(db, request, by_user_id=user.id, via="approval_page", reason=body.reason)
    except ApprovalAuthorityError as exc:
        raise HTTPException(403, str(exc)) from exc
    worker_id = None
    if step is not None and step.step_status == ExecutionStepStatus.WAITING_HUMAN.value:
        if body.decision == "approve":
            # Back in the queue: the gate finds the grant and spends it.
            step.step_status = ExecutionStepStatus.PENDING.value
            step.human_input_prompt = None
            step.error = None
            plan = await db.get(ExecutionPlan, step.plan_id)
            state = plan.dispatcher_state if plan is not None and isinstance(plan.dispatcher_state, dict) else {}
            worker_id = str(state.get("target_worker_id") or "") or None
        else:
            from datetime import datetime, timezone

            step.step_status = ExecutionStepStatus.FAILED.value
            step.error = {
                "type": "ApprovalRejected",
                "message": body.reason or "The owner rejected this action.",
                "approval_request_id": request.id,
            }
            step.finished_at = datetime.now(timezone.utc)
    await db.commit()
    if worker_id:
        from packages.core.workers.wake import wake_worker

        await wake_worker(worker_id)
    return _view(request)
