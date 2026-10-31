"""The shared boundary of runtime-tool approvals that belong to a Task.

A Task's run releases its execution lease while a person decides, so the
unified ``HitlRequest`` outlives the turn that asked. These helpers serialize
request creation, resolution and Task terminality on one per-Task lock, show
the Task-thread approval card, and close a Task's unspent requests when it
ends. Resuming a decided request belongs to the Task agent loop
(``task_agent_loop_hitl``).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
    ApprovalOriginKind,
    ApprovalStatus,
)
from packages.core.constants.hitl_envelope import parse_hitl_envelope
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Message, Task
from packages.core.services.conversation_messages import add_message
from packages.core.services.hitl_requests import hitl_requests_from_data
from packages.core.services.task_state_machine import (
    TERMINAL_STATUSES,
    apply_task_status_transition,
)


# The Task gateway's outbox event type (scheduled terminal deliveries).
TASK_RUNTIME_RESUME_EVENT = "packages.core.tasks.ai_tasks.run_agent_task"


class TaskRuntimeApprovalError(RuntimeError):
    """A Task-origin approval is no longer attached to a run that may act on it."""


async def lock_task_runtime_approval_boundary(
    db: AsyncSession,
    *,
    task_id: str,
) -> None:
    """Serialize request creation, resolution, execution and Task terminality.

    Row locks alone cannot prevent the ``no request yet`` phantom: cancellation
    can query zero rows just before the runtime guard inserts one.  A stable
    per-Task PostgreSQL advisory lock is therefore the first lock at every
    Task-origin approval boundary.
    """

    resolved_task_id = str(task_id or "").strip()
    if not resolved_task_id:
        raise TaskRuntimeApprovalError("runtime approval Task id is required")
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is None or getattr(bind.dialect, "name", None) != "postgresql":
        return
    from packages.core.constants.advisory_locks import AdvisoryLockNamespace

    await db.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            ":namespace, hashtext(:lock_key))"
        ),
        {
            "namespace": int(AdvisoryLockNamespace.TASK_RUNTIME_APPROVAL),
            "lock_key": resolved_task_id,
        },
    )


def task_runtime_approval_binding_digest(request: HitlRequest) -> str:
    """Digest a request's immutable approval subject."""

    context = dict(request.context) if isinstance(request.context, dict) else {}
    payload = {
        "version": 1,
        "request_id": str(request.id or ""),
        "entity_id": str(request.entity_id or ""),
        "workspace_id": str(request.workspace_id or ""),
        "task_id": str(getattr(request, "origin_task_id", None) or ""),
        "conversation_id": str(request.origin_conversation_id or ""),
        "action_key": str(request.action_key or ""),
        "capability_id": str(request.capability_id or ""),
        "hitl_type": str(request.hitl_type or ""),
        "risk_level": str(request.risk_level or ""),
        "resource_kind": str(request.resource_kind or ""),
        "resource_id": str(request.resource_id or ""),
        "tool": str(context.get("tool") or ""),
        "args_hash": str(context.get("args_hash") or ""),
        "requested_by": str(context.get("requested_by") or ""),
        "provider": str(context.get("provider") or ""),
        "provider_approval_id": str(context.get("provider_approval_id") or ""),
        "continuation": context.get("continuation"),
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TaskRuntimeApprovalError(
            "runtime approval binding is not JSON serializable"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


async def _expire_locked_runtime_request(
    db: AsyncSession,
    request: HitlRequest,
    *,
    reason: str,
) -> bool:
    """Fail-close one unspent Task approval and its rendered card."""

    if request.status not in {status.value for status in APPROVAL_LIVE_STATUSES}:
        return False
    context = request.context if isinstance(request.context, dict) else {}
    if isinstance(context.get("provider_action_claim"), dict):
        # The external action crossed its durable linearization point.  Its
        # result must be reconciled; cancellation cannot truthfully undo it.
        return False
    request.status = ApprovalStatus.EXPIRED.value
    request.resolved_reason = reason
    request.decided_at = datetime.now(timezone.utc)
    from packages.core.services.task_agent_loop_hitl import wake_run_for_closed_request
    await wake_run_for_closed_request(db, request)
    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et
    from packages.core.services.hitl_requests import mark_hitl_request_resolved

    await ledger_adapters.record_approval_event(
        db,
        request,
        ledger_et.APPROVAL_EXPIRED,
    )
    if request.origin_conversation_id:
        await mark_hitl_request_resolved(
            db,
            conversation_id=request.origin_conversation_id,
            hitl_id=request.id,
            choice="expired",
        )
    return True


def _approval_id(hitl_data: dict[str, Any] | None) -> str | None:
    if not isinstance(hitl_data, dict):
        return None
    hitl = hitl_data.get("hitl")
    value = (
        hitl.get("id")
        if isinstance(hitl, dict)
        else hitl_data.get("approval_token")
    )
    resolved = str(value or hitl_data.get("approval_token") or "").strip()
    return resolved or None


async def project_task_runtime_approval_card(
    db: AsyncSession,
    *,
    task: Task,
    hitl_data: dict[str, Any],
    hitl_id: str,
    requested_by_agent: str | None = None,
    author_subscription_id: str | None = None,
    run_id: str | None = None,
) -> Message:
    """The Task-thread approval card for one runtime approval (idempotent).

    The Task agent loop parks on it. A delegated agent's card says who asked
    (``requested_by_agent``), is authored by that agent's subscription and
    names the run it resumes.
    """

    event_id = f"task-runtime-hitl:{hitl_id}"
    existing = (await db.execute(
        select(Message).where(
            Message.conversation_id == task.conversation_id,
            Message.response_surface_event_id == event_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    requests = hitl_requests_from_data(hitl_data)
    if not requests:
        raise TaskRuntimeApprovalError("runtime approval has no renderable card")
    agent = str(requested_by_agent or "").strip()[:100]
    return await add_message(
        db,
        str(task.conversation_id),
        role="assistant",
        content=(
            f"Asked by {agent}, an agent working on this Task: it needs approval "
            "before it can continue."
            if agent
            else "This scheduled agent needs approval before it can continue."
        ),
        tool_calls=[
            {
                "name": str((requests[0] or {}).get("tool") or "runtime_action"),
                "status": "waiting_human",
            }
        ],
        author_subscription_id=author_subscription_id or task.owner_subscription_id,
        meta={
            "hitl_requests": requests,
            "task_id": task.id,
            "task_runtime_hitl": True,
            **({"requested_by_agent": agent} if agent else {}),
            **({"run_id": run_id, "run_kind": "child"} if run_id else {}),
        },
        response_surface_event_id=event_id,
        message_kind="hitl_request",
        refs=[{"type": "task", "id": task.id, "title": task.title}],
    )


async def expire_task_runtime_approval_request(
    db: AsyncSession,
    *,
    request: HitlRequest,
    reason: str,
) -> bool:
    if not getattr(request, "origin_task_id", None):
        return False
    await lock_task_runtime_approval_boundary(
        db,
        task_id=str(request.origin_task_id),
    )
    return await _expire_locked_runtime_request(db, request, reason=reason)


async def expire_task_runtime_approval_after_checkpoint_failure(
    db: AsyncSession,
    *,
    task: Task,
    blocking_control: dict[str, Any],
    reason: str = "runtime_approval_checkpoint_failed",
) -> bool:
    """Close a guard-committed request when no checkpoint can resume it.

    Used when the run's park (checkpoint or card) cannot be persisted.
    """

    hitl_data = parse_hitl_envelope(blocking_control.get("blocking_result"))
    hitl_id = _approval_id(hitl_data)
    if not hitl_id:
        return False
    await lock_task_runtime_approval_boundary(db, task_id=task.id)
    request = (await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.id == hitl_id,
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.origin_conversation_id == task.conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None:
        return False
    await db.execute(
        select(Task)
        .where(Task.id == task.id, Task.entity_id == task.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await _expire_locked_runtime_request(
        db,
        request,
        reason=reason,
    )


async def expire_task_runtime_approval_for_terminal(
    db: AsyncSession,
    *,
    task: Task,
    reason: str = "task_terminal",
) -> int:
    """Revoke every unspent runtime request owned by a terminal Task.

    Querying by origin Task also closes the narrow guard-commit/park-write
    crash window where no run ever parked on the request. The runs parked on
    these cards stop with the Task (reconcile), so closing them wakes nothing.
    """

    from packages.core.services.task_agent_loop_hitl import closing_for_terminal_task

    with closing_for_terminal_task():
        return await _expire_task_runtime_approval_for_terminal(db, task=task, reason=reason)


async def _expire_task_runtime_approval_for_terminal(
    db: AsyncSession,
    *,
    task: Task,
    reason: str,
) -> int:

    await lock_task_runtime_approval_boundary(db, task_id=task.id)
    requests = list((await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.status.in_(
                tuple(status.value for status in APPROVAL_LIVE_STATUSES)
            ),
        )
        .order_by(HitlRequest.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    from packages.core.services.task_agent_loop_hitl import (
        close_agent_loop_input_requests,
        lock_agent_loop_input_requests,
    )

    # A Task agent loop's open question closes with its Task too.
    input_requests = await lock_agent_loop_input_requests(db, task=task)
    if requests or input_requests:
        # Global human-boundary lock order: HitlRequest(s) → Task → Message.
        await db.execute(
            select(Task)
            .where(Task.id == task.id, Task.entity_id == task.entity_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    expired = 0
    for request in requests:
        if await _expire_locked_runtime_request(db, request, reason=reason):
            expired += 1
    if input_requests:
        await close_agent_loop_input_requests(
            db, task=task, requests=input_requests, reason=reason,
        )
    return expired


async def fail_task_runtime_approval_task(
    db: AsyncSession,
    *,
    task: Task,
    reason: str,
    error_type: str = "TaskRuntimeApprovalError",
) -> Task:
    """Terminalize a Task through the shared approval boundary.

    Callers use this when a Task cannot be admitted. Looking up *all*
    Task-origin requests is crucial: it also closes a request committed just
    before a worker died while parking on it.
    """

    await expire_task_runtime_approval_for_terminal(
        db,
        task=task,
        reason=reason,
    )
    locked = (await db.execute(
        select(Task)
        .where(Task.id == task.id, Task.entity_id == task.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    if locked.status in TERMINAL_STATUSES:
        return locked
    locked.details = {**(locked.details or {}), "failure_reason": reason}
    output = dict(locked.actual_output or {})
    locked.actual_output = {
        **output,
        "task_status": "failed",
        "error_type": error_type,
        "error_message": reason,
        "response": reason,
    }
    await apply_task_status_transition(locked, "failed", db=db)
    return locked


__all__ = [
    "TASK_RUNTIME_RESUME_EVENT",
    "TaskRuntimeApprovalError",
    "expire_task_runtime_approval_after_checkpoint_failure",
    "expire_task_runtime_approval_for_terminal",
    "expire_task_runtime_approval_request",
    "fail_task_runtime_approval_task",
    "lock_task_runtime_approval_boundary",
    "project_task_runtime_approval_card",
    "task_runtime_approval_binding_digest",
]
