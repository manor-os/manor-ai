"""Re-decide pending policy approvals after an approval policy write.

A request the policy gate raised ("ask") records a decision under the policy
of its time. When the person then changes the policy so the action is allowed
— switches to "Run automatically", adds an allow rule — the parked run must
not keep waiting for a click the user has already answered in settings.

The write stages one durable outbox row in its own transaction. After commit
the worker re-runs the gate's own evaluation for each pending request in the
changed scope and, when it now allows, approves the request through the same
decision path an Approve click takes, so the parked run wakes exactly as it
would for a person. The grant carries no human consent: its requirements are
narrowed to the (empty) set the new policy asks for, so the execution-time
recheck refuses it if the live action still needs approval.

Only requests a durable host run resumes are released (Task agent loops,
Task runtime checkpoints, Workflow steps). Requests for information, explicit
reviews, provider confirmations and foreground Chat turns stay pending.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus, HitlType
from packages.core.governance.approval_policy import ApprovalScope
from packages.core.models.hitl_request import HitlRequest

logger = logging.getLogger(__name__)

REEVALUATE_PENDING_APPROVALS_TASK = "approval.reevaluate_pending"
POLICY_CHANGE_DECIDED_VIA = "policy_change"
POLICY_CHANGE_RELEASE_CONTEXT_KEY = "policy_change_release"
#: Requests examined per policy write; pending cards per scope are few.
REEVALUATION_BATCH_LIMIT = 100


async def stage_pending_approval_reevaluation(
    db: AsyncSession, *, scope: ApprovalScope, revision: int, actor_id: str | None,
) -> None:
    """Stage the re-decision beside the policy write (caller commits).

    The outbox row commits or rolls back with the write itself, and a repeated
    stage of the same revision is a no-op.
    """
    if not actor_id:
        return  # a system write has no person to attribute a decision to
    from packages.core.models.runtime_run import RuntimeOutboxEvent

    dedupe_key = f"{REEVALUATE_PENDING_APPROVALS_TASK}:{scope.kind}:{scope.id}:{revision}"
    existing = (await db.execute(
        select(RuntimeOutboxEvent.id).where(RuntimeOutboxEvent.dedupe_key == dedupe_key)
    )).scalar_one_or_none()
    if existing is not None:
        return
    db.add(RuntimeOutboxEvent(
        event_type=REEVALUATE_PENDING_APPROVALS_TASK,
        aggregate_id=scope.id,
        dedupe_key=dedupe_key,
        payload={
            "entity_id": scope.entity_id, "scope_kind": scope.kind, "scope_id": scope.id,
            "revision": int(revision), "actor_id": actor_id,
        },
    ))
    await db.flush()


def _host_resumes(request: HitlRequest) -> bool:
    """Whether the decision path wakes a durable run, not a Chat turn."""
    from packages.core.services.task_comment_approval_origin import has_task_comment_origin
    from packages.core.services.workflow_runtime_approvals import workflow_approval_owned

    if workflow_approval_owned(request):
        return True
    return bool(request.origin_task_id) and not has_task_comment_origin(request)


def releasable_by_policy(request: HitlRequest) -> bool:
    """A pending approval the policy gate raised and nothing but policy holds."""
    from packages.core.governance.approvals import is_human_review_requirement

    context = request.context if isinstance(request.context, dict) else {}
    requirements = context.get("approval_requirements")
    return bool(
        request.status == ApprovalStatus.PENDING.value
        and request.hitl_type == HitlType.AUTHORIZE.value
        and request.origin_kind == ApprovalOriginKind.TOOL_CALL.value
        and request.origin_conversation_id
        and isinstance(context.get("policy_evaluation"), dict)
        and isinstance(requirements, list) and requirements
        and not any(is_human_review_requirement(item) for item in requirements)
        and context.get("kind") != "provider"
        and _host_resumes(request)
    )


def _resumed_approved(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    if metadata.get("approval_kind") == "task_runtime":
        return (metadata.get("task_runtime_resume") or {}).get("choice") == "approved"
    return metadata.get("approval_kind") == "workflow_runtime" and metadata.get("approval_outcome") == "approved"


async def _release_one(
    db: AsyncSession, *, request_id: str, scope: ApprovalScope, revision: int, actor_id: str,
) -> str:
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn
    from packages.core.governance.approvals import (
        _consent_requirements, current_policy_for_pending_request,
    )
    from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

    request = await db.get(HitlRequest, request_id)
    if request is None or not releasable_by_policy(request):
        return "skipped"
    # The card decision's lock order: the Task boundary, then the request.
    if request.origin_task_id:
        await lock_task_runtime_approval_boundary(db, task_id=str(request.origin_task_id))
    request = (await db.execute(
        select(HitlRequest).where(HitlRequest.id == request_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None or not releasable_by_policy(request):
        return "skipped"  # decided meanwhile (a click, another write)
    decision = await current_policy_for_pending_request(db, request)
    if not decision.allowed or _consent_requirements(decision):
        return "still_required"
    previous = list(request.context.get("approval_requirements") or [])
    savepoint = await db.begin_nested()
    try:
        resolution = await resolve_runtime_approval_turn(
            db, conversation_id=request.origin_conversation_id, entity_id=request.entity_id,
            user_id=actor_id, hitl_id=request.id, action="approve",
            decided_via=POLICY_CHANGE_DECIDED_VIA,
        )
        if (
            resolution is None or not _resumed_approved(resolution.runtime_metadata)
            or request.status != ApprovalStatus.GRANTED.value
            or request.decided_via != POLICY_CHANGE_DECIDED_VIA
        ):
            # Not an approval that resumes its run (no authority, a stale or
            # foreign continuation, ...): leave the card exactly as it was.
            await savepoint.rollback()
            return "not_resumed"
    except Exception:
        await savepoint.rollback()
        logger.warning("Policy-change approval release failed for %s", request_id, exc_info=True)
        return "not_resumed"
    request.resolved_reason = f"policy_change:{scope.kind}:{scope.id}:r{revision}"[:120]
    request.context = {
        **request.context,
        # The grant holds only what the policy itself allows now; a live
        # recheck that still asks at execution refuses it.
        "approval_requirements": [],
        POLICY_CHANGE_RELEASE_CONTEXT_KEY: {
            "scope": scope.model_dump(), "revision": int(revision), "actor_id": actor_id,
            "released_requirements": previous,
            "policy_evaluation": decision.explanation,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    }
    await savepoint.commit()
    return "released"


async def reevaluate_pending_approvals(
    db: AsyncSession, *, entity_id: str, scope_kind: str, scope_id: str,
    revision: int, actor_id: str | None, limit: int = REEVALUATION_BATCH_LIMIT,
) -> dict[str, int]:
    """Release the pending policy approvals the current policy now allows.

    Every request is re-decided against the live policy in its own
    transaction, so a repeat or late delivery only finds what is still
    pending and a concurrent click wins or loses on the request row lock.
    """
    counts = {"released": 0, "still_required": 0, "not_resumed": 0, "skipped": 0}
    if not actor_id:
        return counts
    scope = ApprovalScope(kind=scope_kind, id=scope_id, entity_id=entity_id)
    query = select(HitlRequest.id).where(
        HitlRequest.entity_id == entity_id,
        HitlRequest.status == ApprovalStatus.PENDING.value,
        HitlRequest.hitl_type == HitlType.AUTHORIZE.value,
        HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL.value,
    )
    # Only requests whose policy chain contains the changed scope; an account
    # default is part of every chain.
    if scope.kind == "workspace":
        query = query.where(HitlRequest.workspace_id == scope.id)
    elif scope.kind == "personal":
        from packages.core.models.task import Conversation

        query = query.where(HitlRequest.workspace_id.is_(None), HitlRequest.origin_conversation_id.in_(
            select(Conversation.id).where(Conversation.user_id == scope.id, Conversation.entity_id == entity_id)
        ))
    elif scope.kind == "room":
        from packages.core.governance.approvals import APPROVAL_POLICY_SUBJECT_CONTEXT_KEY

        query = query.where(
            HitlRequest.context[APPROVAL_POLICY_SUBJECT_CONTEXT_KEY]["subject"]["room_id"].astext == scope.id
        )
    request_ids = list((await db.execute(
        query.order_by(HitlRequest.created_at.asc(), HitlRequest.id.asc()).limit(limit)
    )).scalars().all())
    await db.commit()  # end the read; each request gets its own transaction
    for request_id in request_ids:
        try:
            outcome = await _release_one(
                db, request_id=request_id, scope=scope, revision=revision, actor_id=actor_id,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.warning("Policy-change re-evaluation failed for %s", request_id, exc_info=True)
            outcome = "not_resumed"
        counts[outcome] += 1
    if len(request_ids) >= limit:
        logger.warning("Policy-change re-evaluation for %s %s stopped at %d requests",
                       scope.kind, scope.id, limit)
    if counts["released"]:
        logger.info("Policy change %s %s r%s released %s",
                    scope.kind, scope.id, revision, json.dumps(counts, sort_keys=True))
    return counts
