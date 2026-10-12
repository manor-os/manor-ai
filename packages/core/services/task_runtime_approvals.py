"""Durable runtime-tool approval bridge for autonomous TaskRunner jobs.

Interactive Chat can keep an approval continuation in the active request.  A
scheduled Agent cannot: its worker must release its execution lease while a
human decides.  The unified ``HitlRequest`` therefore owns the private model
checkpoint and exact resolver-produced continuation, while ``Task.details``
contains only a non-sensitive pointer/state projection.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.engine import ChatMessage
from packages.core.ai.runtime.task_checkpoint import (
    RuntimeTaskHitlCheckpoint,
    runtime_deserialize_task_messages,
    runtime_serialize_task_messages,
)
from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
    ApprovalOriginKind,
    ApprovalStatus,
)
from packages.core.constants.hitl_envelope import parse_hitl_envelope
from packages.core.constants.task import TaskStatus
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Conversation, Message, Task
from packages.core.services.conversation_messages import add_message
from packages.core.services.hitl_requests import hitl_requests_from_data
from packages.core.services.task_state_machine import (
    TERMINAL_STATUSES,
    apply_task_status_transition,
)


TASK_RUNTIME_HITL_DETAIL_KEY = "_runtime_tool_hitl_v1"
TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY = "task_runtime_checkpoint_v1"
TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY = (
    "task_runtime_checkpoint_signature_v1"
)
TASK_RUNTIME_RESOLUTION_CONTEXT_KEY = "task_runtime_resolution_v1"
TASK_RUNTIME_EXECUTION_CONTEXT_KEY = "task_runtime_execution_v1"
TASK_RUNTIME_RESUME_EVENT = "packages.core.tasks.ai_tasks.run_agent_task"
_TASK_RUNTIME_RESOLUTION_MAX_BYTES = 128_000
_TASK_RUNTIME_RESOLUTION_SIGNATURE_DOMAIN = (
    b"manor.task-runtime-hitl-resolution.v1\0"
)
_TASK_RUNTIME_EXECUTION_SIGNATURE_DOMAIN = (
    b"manor.task-runtime-hitl-execution.v1\0"
)


class TaskRuntimeApprovalError(RuntimeError):
    """A stored Task approval continuation failed an authority check."""


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
    """Digest the immutable approval subject embedded in a signed checkpoint."""

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


def _approval_binding_digest(request: HitlRequest) -> str:
    """Backward-compatible private spelling for the public testable contract."""

    return task_runtime_approval_binding_digest(request)


def _runtime_resolution_signature(
    resolution: dict[str, Any],
) -> str:
    """Authenticate the decision and exact resolver-authored continuation."""

    from packages.core.config import get_settings

    signed = {
        "choice": resolution.get("choice"),
        "generation": resolution.get("generation"),
        "checkpoint_digest": resolution.get("checkpoint_digest"),
        "checkpoint_signature": resolution.get("checkpoint_signature"),
        "runtime_metadata": resolution.get("runtime_metadata"),
    }
    try:
        encoded = json.dumps(
            signed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TaskRuntimeApprovalError(
            "runtime approval resolution is not JSON serializable"
        ) from exc
    secret = get_settings().JWT_SECRET_KEY.encode("utf-8")
    return hmac.new(
        secret,
        _TASK_RUNTIME_RESOLUTION_SIGNATURE_DOMAIN + encoded,
        hashlib.sha256,
    ).hexdigest()


def _validate_runtime_resolution_signature(
    resolution: dict[str, Any],
) -> str:
    signature = resolution.get("resolution_signature")
    if (
        not isinstance(signature, str)
        or len(signature) != 64
        or not hmac.compare_digest(signature, _runtime_resolution_signature(resolution))
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval resolution signature is invalid"
        )
    return signature


def _runtime_execution_signature(execution: dict[str, Any]) -> str:
    """Authenticate the durable action-attempt/receipt state.

    A standard tool action cannot generally be replayed after a worker crash:
    the process may have died immediately before *or* immediately after the
    external side effect.  This record makes that boundary explicit.  A
    claimed attempt without a committed receipt is reconciled as ambiguous,
    never silently replayed or reported as success.
    """

    from packages.core.config import get_settings

    signed = {
        "version": execution.get("version"),
        "attempt_id": execution.get("attempt_id"),
        "phase": execution.get("phase"),
        "claimed_at": execution.get("claimed_at"),
        "completed_at": execution.get("completed_at"),
        "checkpoint_digest": execution.get("checkpoint_digest"),
        "resolution_signature": execution.get("resolution_signature"),
        "result_status": execution.get("result_status"),
        "result_digest": execution.get("result_digest"),
    }
    encoded = json.dumps(
        signed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        get_settings().JWT_SECRET_KEY.encode("utf-8"),
        _TASK_RUNTIME_EXECUTION_SIGNATURE_DOMAIN + encoded,
        hashlib.sha256,
    ).hexdigest()


def _validate_runtime_execution_signature(
    execution: dict[str, Any],
) -> str:
    signature = execution.get("execution_signature")
    if (
        execution.get("version") != 1
        or not isinstance(execution.get("attempt_id"), str)
        or not execution["attempt_id"]
        or execution.get("phase") not in {"executing", "completed", "ambiguous"}
        or not isinstance(signature, str)
        or len(signature) != 64
        or not hmac.compare_digest(signature, _runtime_execution_signature(execution))
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval execution receipt is invalid"
        )
    return signature


def _checkpoint_signature(request: HitlRequest) -> str | None:
    context = request.context if isinstance(request.context, dict) else {}
    value = context.get(TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY)
    return value if isinstance(value, str) and value else None


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


@dataclass(frozen=True)
class RuntimeTaskApprovalResume:
    choice: str
    checkpoint: RuntimeTaskHitlCheckpoint
    runtime_metadata: dict[str, Any]

    @property
    def messages(self) -> list[ChatMessage]:
        return runtime_deserialize_task_messages(self.checkpoint.messages)


def existing_task_runtime_approval_resolution(
    request: HitlRequest,
) -> dict[str, Any] | None:
    """Project an already-staged Task resolution for idempotent card clicks."""

    origin_task_id = getattr(request, "origin_task_id", None)
    if not origin_task_id:
        return None
    raw_context = getattr(request, "context", None)
    context = raw_context if isinstance(raw_context, dict) else {}
    checkpoint_raw = context.get(TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY)
    resolution = context.get(TASK_RUNTIME_RESOLUTION_CONTEXT_KEY)
    if not isinstance(checkpoint_raw, dict) or not isinstance(resolution, dict):
        return None
    try:
        checkpoint = RuntimeTaskHitlCheckpoint.from_dict(checkpoint_raw)
    except (TypeError, ValueError):
        return None
    signature = _checkpoint_signature(request)
    choice = str(resolution.get("choice") or "")
    expected_status = (
        ApprovalStatus.GRANTED.value
        if choice == "approved"
        else ApprovalStatus.DENIED.value
    )
    try:
        _validate_runtime_resolution_signature(resolution)
    except TaskRuntimeApprovalError:
        return None
    if (
        choice not in {"approved", "rejected"}
        or request.status != expected_status
        or not signature
        or not checkpoint.verify_signature(signature)
        or checkpoint.approval_binding_digest != _approval_binding_digest(request)
        or resolution.get("checkpoint_signature") != signature
        or checkpoint.entity_id != getattr(request, "entity_id", None)
        or str(checkpoint.workspace_id or "")
        != str(getattr(request, "workspace_id", None) or "")
        or checkpoint.task_id != origin_task_id
        or checkpoint.hitl_id != getattr(request, "id", None)
        or checkpoint.conversation_id
        != getattr(request, "origin_conversation_id", None)
        or resolution.get("generation") != checkpoint.generation
        or resolution.get("checkpoint_digest") != checkpoint.digest()
    ):
        return None
    return {
        "approval_kind": "task_runtime",
        "task_runtime_resume": {
            "task_id": checkpoint.task_id,
            "hitl_id": getattr(request, "id", None),
            "generation": checkpoint.generation,
            "choice": choice,
        },
    }


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


def _task_hitl_generation(task: Task) -> int:
    details = task.details if isinstance(task.details, dict) else {}
    current = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    generation = current.get("generation") if isinstance(current, dict) else 0
    if isinstance(generation, bool) or not isinstance(generation, int):
        generation = 0
    return max(0, generation) + 1


def _freeze_approved_runtime_metadata(
    runtime_metadata: dict[str, Any] | None,
    *,
    request: HitlRequest,
) -> dict[str, Any]:
    """Validate the resolver-owned exact call before making it executable."""

    if not isinstance(runtime_metadata, dict):
        raise TaskRuntimeApprovalError("approved runtime action has no continuation")
    try:
        encoded = json.dumps(
            runtime_metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TaskRuntimeApprovalError(
            "approved runtime continuation is not JSON serializable"
        ) from exc
    if len(encoded.encode("utf-8")) > _TASK_RUNTIME_RESOLUTION_MAX_BYTES:
        raise TaskRuntimeApprovalError("approved runtime continuation is too large")
    frozen = json.loads(encoded)
    forced_calls = frozen.get("forced_tool_calls")
    approved_call = frozen.get("approved_tool_call")
    if approved_call is not None:
        from packages.core.ai.runtime.approval_messages import is_chrome_publication_call

        if (
            forced_calls or not isinstance(approved_call, dict)
            or not isinstance(approved_call.get("arguments"), dict)
            or not is_chrome_publication_call(str(approved_call.get("name") or ""), approved_call["arguments"])
        ):
            raise TaskRuntimeApprovalError("approved publication continuation is invalid")
        call = approved_call
    elif not isinstance(forced_calls, list) or len(forced_calls) != 1:
        raise TaskRuntimeApprovalError("approved runtime continuation is invalid")
    else:
        call = forced_calls[0]
    if (
        not isinstance(call, dict)
        or not str(call.get("name") or "").strip()
        or not isinstance(call.get("arguments"), dict)
    ):
        raise TaskRuntimeApprovalError("approved runtime forced call is invalid")
    from packages.core.ai.runtime.approval_messages import is_chrome_basic_approval_item

    request_context = request.context if isinstance(request.context, dict) else {}
    continuing_browser = (
        approved_call is None and call.get("disable_followup_tools") is False
        and is_chrome_basic_approval_item(request_context)
    )
    if approved_call is None and call.get("disable_followup_tools") is not True and not continuing_browser:
        raise TaskRuntimeApprovalError("approved runtime forced call is invalid")
    extra_names = frozen.get("extra_tool_names")
    if not isinstance(extra_names, list) or not all(
        isinstance(name, str) and name.strip() for name in extra_names
    ):
        raise TaskRuntimeApprovalError("approved runtime tool surface is invalid")
    if call["name"] not in extra_names:
        raise TaskRuntimeApprovalError("approved runtime tool surface is incomplete")

    call_arguments = dict(call["arguments"])
    if request_context.get("kind") == "provider":
        from packages.core.ai.runtime.provider_approvals import (
            freeze_provider_approval_request,
            pop_tool_continuation,
        )

        provider_request = freeze_provider_approval_request(
            request_context.get("continuation")
        )
        retry_continuation = pop_tool_continuation(call_arguments)
        if (
            provider_request is None
            or retry_continuation is None
            or call["name"] != provider_request.get("confirmation_tool")
            or call_arguments != provider_request.get("confirmation_arguments")
            or retry_continuation.get("tool")
            != provider_request.get("retry_tool")
            or retry_continuation.get("arguments")
            != provider_request.get("retry_arguments")
            or provider_request.get("retry_tool") not in extra_names
        ):
            raise TaskRuntimeApprovalError(
                "approved provider continuation does not match the request"
            )
    else:
        request_continuation = request_context.get("continuation")
        approval_token = str(call_arguments.pop("approval_token", "") or "")
        if (
            not isinstance(request_continuation, dict)
            or request_continuation.get("version") != 1
            or call["name"] != request_context.get("tool")
            or call["name"] != request_continuation.get("tool")
            or approval_token != request.id
            or call_arguments != request_continuation.get("arguments")
        ):
            raise TaskRuntimeApprovalError(
                "approved runtime continuation does not match the request"
            )
        if continuing_browser or (approved_call is None and call["name"].startswith("mcp__chrome__")):
            from packages.core.ai.runtime.approval_messages import runtime_approval_runtime_metadata

            expected = runtime_approval_runtime_metadata(request_context, request.id)
            if (
                expected is None or extra_names != expected["extra_tool_names"]
                or frozen.get("chrome_basic_approval_policy_v1")
                != expected.get("chrome_basic_approval_policy_v1")
            ):
                raise TaskRuntimeApprovalError("approved browser tool surface does not match the request")
    return frozen


async def _provider_hitl_data(
    db: AsyncSession,
    *,
    task: Task,
    execution_user_id: str,
    tool_name: str,
    tool_arguments: dict[str, Any],
    blocking_result: Any,
) -> dict[str, Any] | None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )
    from packages.core.ai.runtime.provider_approvals import (
        normalize_provider_approval,
    )

    request = normalize_provider_approval(
        tool_name,
        tool_arguments,
        blocking_result,
    )
    if request is None:
        return None
    return await register_provider_runtime_approval(
        db,
        conversation_id=str(task.conversation_id or ""),
        entity_id=task.entity_id,
        user_id=execution_user_id,
        request=request,
        task_id=task.id,
    )


async def _project_approval_card(
    db: AsyncSession,
    *,
    task: Task,
    hitl_data: dict[str, Any],
    hitl_id: str,
) -> Message:
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
    return await add_message(
        db,
        str(task.conversation_id),
        role="assistant",
        content="This scheduled agent needs approval before it can continue.",
        tool_calls=[
            {
                "name": str((requests[0] or {}).get("tool") or "runtime_action"),
                "status": "waiting_human",
            }
        ],
        author_subscription_id=task.owner_subscription_id,
        meta={
            "hitl_requests": requests,
            "task_id": task.id,
            "task_runtime_hitl": True,
        },
        response_surface_event_id=event_id,
        message_kind="hitl_request",
        refs=[{"type": "task", "id": task.id, "title": task.title}],
    )


async def project_task_runtime_approval_card(
    db: AsyncSession,
    *,
    task: Task,
    hitl_data: dict[str, Any],
    hitl_id: str,
) -> Message:
    """The Task-thread approval card for one runtime approval (idempotent).

    The same card TaskRunner shows; the Task agent loop parks on it too.
    """

    return await _project_approval_card(db, task=task, hitl_data=hitl_data, hitl_id=hitl_id)


async def _validate_checkpoint_scope(
    db: AsyncSession,
    *,
    checkpoint: RuntimeTaskHitlCheckpoint,
    task: Task,
    request: HitlRequest,
    conversation: Conversation,
) -> None:
    """Re-prove the exact Task, actor, Agent instance and destination tuple."""

    details = task.details if isinstance(task.details, dict) else {}
    request_context = request.context if isinstance(request.context, dict) else {}
    from packages.core.services.task_requester_identity import (
        resolve_task_execution_user_id,
    )

    trusted_execution_user_id = await resolve_task_execution_user_id(db, task)
    from packages.core.constants.agents import is_master_agent

    if task.agent_id and not is_master_agent(task.agent_id) and not (
        checkpoint.agent_subscription_id
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval requires an exact Agent subscription"
        )
    identity_matches = (
        checkpoint.task_id == task.id == getattr(request, "origin_task_id", None)
        and checkpoint.entity_id == task.entity_id == request.entity_id
        and checkpoint.conversation_id
        == task.conversation_id
        == request.origin_conversation_id
        == conversation.id
        and checkpoint.hitl_id == request.id
        and str(checkpoint.workspace_id or "")
        == str(task.workspace_id or "")
        == str(request.workspace_id or "")
        == str(conversation.workspace_id or "")
        and checkpoint.execution_user_id == str(trusted_execution_user_id or "")
        and checkpoint.execution_user_id
        == str(request_context.get("requested_by") or "")
        and str(checkpoint.agent_id or "") == str(task.agent_id or "")
        and str(checkpoint.agent_subscription_id or "")
        == str(task.owner_subscription_id or "")
        and str(checkpoint.scheduled_run_id or "")
        == str(details.get("scheduled_run_id") or "")
        and str(checkpoint.scheduled_job_id or "")
        == str(details.get("scheduled_job_id") or "")
        and checkpoint.approval_binding_digest
        == _approval_binding_digest(request)
    )
    if not identity_matches:
        raise TaskRuntimeApprovalError("runtime approval authority scope mismatch")

    if task.workspace_id:
        if conversation.scope not in {"workspace_main", "workspace_thread"}:
            raise TaskRuntimeApprovalError(
                "runtime approval Workspace conversation is invalid"
            )
    else:
        if (
            conversation.workspace_id is not None
            or str(conversation.user_id or "") != checkpoint.execution_user_id
        ):
            raise TaskRuntimeApprovalError(
                "runtime approval direct conversation owner mismatch"
            )
        if task.agent_id:
            agent_matches = str(conversation.agent_id or "") == str(task.agent_id)
            if is_master_agent(task.agent_id):
                agent_matches = agent_matches or conversation.agent_id is None
            if not agent_matches:
                raise TaskRuntimeApprovalError(
                    "runtime approval direct conversation Agent mismatch"
                )

    if checkpoint.agent_subscription_id:
        from packages.core.models.workspace import AgentSubscription

        subscription = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.id == checkpoint.agent_subscription_id,
                AgentSubscription.entity_id == checkpoint.entity_id,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()
        if (
            subscription is None
            or str(subscription.workspace_id or "")
            != str(checkpoint.workspace_id or "")
            or str(subscription.agent_id or "") != str(checkpoint.agent_id or "")
            or (
                checkpoint.workspace_id is None
                and str(subscription.owner_user_id or "")
                != checkpoint.execution_user_id
            )
            or (
                checkpoint.workspace_id is None
                and str(conversation.agent_subscription_id or "")
                != checkpoint.agent_subscription_id
            )
        ):
            raise TaskRuntimeApprovalError(
                "runtime approval Agent subscription mismatch"
            )


def _validate_checkpoint_signature(
    *,
    checkpoint: RuntimeTaskHitlCheckpoint,
    request: HitlRequest,
    marker: dict[str, Any] | None = None,
    resolution: dict[str, Any] | None = None,
) -> str:
    signature = _checkpoint_signature(request)
    if not signature or not checkpoint.verify_signature(signature):
        raise TaskRuntimeApprovalError("runtime approval checkpoint signature is invalid")
    if marker is not None and marker.get("checkpoint_signature") != signature:
        raise TaskRuntimeApprovalError("runtime approval checkpoint marker is untrusted")
    if resolution is not None and resolution.get("checkpoint_signature") != signature:
        raise TaskRuntimeApprovalError("runtime approval resolution is untrusted")
    return signature


async def _load_scope_conversation(
    db: AsyncSession,
    *,
    task: Task,
) -> Conversation:
    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == task.conversation_id,
            Conversation.entity_id == task.entity_id,
            Conversation.status == "active",
        )
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if conversation is None:
        raise TaskRuntimeApprovalError(
            "scheduled runtime approval conversation is invalid"
        )
    return conversation


async def persist_task_runtime_approval_gate(
    db: AsyncSession,
    *,
    task: Task,
    execution_user_id: str | None,
    blocking_control: dict[str, Any],
    tool_arguments: dict[str, Any] | None,
    messages: Iterable[ChatMessage],
    usage: dict[str, Any],
    turns_used: int,
    tool_calls_made: list[str],
    supervisor_evidence: list[dict[str, str]],
    remaining_tool_calls: list[dict[str, Any]] | None = None,
    disable_followup_tools: bool = False,
) -> dict[str, Any]:
    """Persist a private checkpoint and one idempotent approval card."""

    resolved_execution_user_id = str(execution_user_id or "").strip()
    if not task.conversation_id or not resolved_execution_user_id:
        raise TaskRuntimeApprovalError(
            "scheduled runtime approval requires a conversation and owner"
        )
    await lock_task_runtime_approval_boundary(db, task_id=task.id)
    tool_name = str(blocking_control.get("tool_name") or "").strip()
    blocking_result = blocking_control.get("blocking_result")
    hitl_data = parse_hitl_envelope(blocking_result)
    if hitl_data is None:
        hitl_data = await _provider_hitl_data(
            db,
            task=task,
            execution_user_id=resolved_execution_user_id,
            tool_name=tool_name,
            tool_arguments=dict(tool_arguments or {}),
            blocking_result=blocking_result,
        )
    hitl_id = _approval_id(hitl_data)
    if not hitl_data or not hitl_id:
        raise TaskRuntimeApprovalError("blocking runtime result has no durable approval")

    request = (await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.id == hitl_id,
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_conversation_id == task.conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None or request.status != ApprovalStatus.PENDING.value:
        raise TaskRuntimeApprovalError("runtime approval is not pending")
    if request.origin_task_id != task.id:
        raise TaskRuntimeApprovalError("runtime approval task lineage mismatch")

    # Every runtime-approval path uses HitlRequest → Task lock order.  The
    # resolver already owns the request lock; matching it here removes the
    # inverse Task → HitlRequest deadlock window at the human boundary.
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task.id,
            Task.entity_id == request.entity_id,
            Task.conversation_id == request.origin_conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise TaskRuntimeApprovalError("runtime approval Task is missing")
    conversation = await _load_scope_conversation(db, task=task)
    if task.status in TERMINAL_STATUSES:
        await _expire_locked_runtime_request(
            db,
            request,
            reason="runtime_approval_origin_terminal",
        )
        return {
            "kind": "origin_terminal",
            "tool_name": tool_name,
            "hitl_id": hitl_id,
            "task_status": task.status,
            "state": "expired",
        }

    generation = _task_hitl_generation(task)
    details = task.details if isinstance(task.details, dict) else {}
    checkpoint = RuntimeTaskHitlCheckpoint(
        task_id=task.id,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        execution_user_id=resolved_execution_user_id,
        agent_id=task.agent_id,
        agent_subscription_id=task.owner_subscription_id,
        scheduled_run_id=details.get("scheduled_run_id"),
        scheduled_job_id=details.get("scheduled_job_id"),
        conversation_id=str(task.conversation_id),
        hitl_id=hitl_id,
        approval_binding_digest=_approval_binding_digest(request),
        generation=generation,
        messages=runtime_serialize_task_messages(messages),
        usage=dict(usage or {}),
        turns_used=max(0, int(turns_used)),
        tool_calls_made=list(tool_calls_made),
        supervisor_evidence=list(supervisor_evidence),
        remaining_tool_calls=list(remaining_tool_calls or []),
        disable_followup_tools=disable_followup_tools,
    )
    await _validate_checkpoint_scope(
        db,
        checkpoint=checkpoint,
        task=task,
        request=request,
        conversation=conversation,
    )
    signature = checkpoint.signature()
    context = dict(request.context or {})
    context[TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY] = checkpoint.to_dict()
    context[TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY] = signature
    context.pop(TASK_RUNTIME_RESOLUTION_CONTEXT_KEY, None)
    request.context = context
    message = await _project_approval_card(
        db,
        task=task,
        hitl_data=hitl_data,
        hitl_id=hitl_id,
    )
    return {
        "kind": "approval_required",
        "tool_name": tool_name,
        "hitl_id": hitl_id,
        "message_id": message.id,
        "generation": generation,
        "checkpoint_digest": checkpoint.digest(),
        "checkpoint_signature": signature,
        "state": "waiting",
    }


def _task_resume_payload(
    task: Task,
    *,
    checkpoint: RuntimeTaskHitlCheckpoint,
    checkpoint_signature: str,
    resolution_signature: str,
) -> dict[str, Any]:
    details = task.details if isinstance(task.details, dict) else {}
    return {
        "task_id": task.id,
        "agent_id": task.agent_id,
        "scheduled_run_id": details.get("scheduled_run_id"),
        "scheduled_job_id": details.get("scheduled_job_id"),
        "runtime_hitl_id": checkpoint.hitl_id,
        "runtime_hitl_generation": checkpoint.generation,
        "runtime_hitl_checkpoint_digest": checkpoint.digest(),
        "runtime_hitl_checkpoint_signature": checkpoint_signature,
        "runtime_hitl_resolution_signature": resolution_signature,
    }


async def ensure_task_runtime_approval_resolvable(
    db: AsyncSession,
    *,
    request: HitlRequest,
) -> bool:
    """Validate a pending Task gate before any grant/deny mutation."""

    task_id = str(getattr(request, "origin_task_id", None) or "").strip()
    if not task_id:
        return False
    await lock_task_runtime_approval_boundary(db, task_id=task_id)
    context = request.context if isinstance(request.context, dict) else {}
    checkpoint_raw = context.get(TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY)
    if not isinstance(checkpoint_raw, dict):
        raise TaskRuntimeApprovalError(
            "Task-origin runtime approval has no durable checkpoint"
        )
    try:
        checkpoint = RuntimeTaskHitlCheckpoint.from_dict(checkpoint_raw)
    except (TypeError, ValueError) as exc:
        raise TaskRuntimeApprovalError(
            "Task-origin runtime approval checkpoint is invalid"
        ) from exc
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.entity_id == request.entity_id,
            Task.conversation_id == request.origin_conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise TaskRuntimeApprovalError("runtime approval Task is missing")
    marker = (
        task.details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
        if isinstance(task.details, dict)
        else None
    )
    _validate_checkpoint_signature(
        checkpoint=checkpoint,
        request=request,
        marker=marker if isinstance(marker, dict) else None,
    )
    conversation = await _load_scope_conversation(db, task=task)
    await _validate_checkpoint_scope(
        db,
        checkpoint=checkpoint,
        task=task,
        request=request,
        conversation=conversation,
    )
    if (
        task.status in TERMINAL_STATUSES
        or task.status != TaskStatus.WAITING_ON_CUSTOMER
        or not isinstance(marker, dict)
        or marker.get("state") != "waiting"
        or marker.get("hitl_id") != request.id
        or marker.get("generation") != checkpoint.generation
        or marker.get("checkpoint_digest") != checkpoint.digest()
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval Task is no longer waiting on this checkpoint"
        )
    return True


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

    Used when checkpoint/card persistence fails, and by hosts that cannot yet
    resume a Task-origin approval at all (the Task agent loop until Phase B).
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

    Querying by origin Task, rather than only by its public marker, also
    closes the narrow guard-commit/checkpoint-write crash window where no
    marker was ever installed.
    """

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

    # A Task agent loop's open question closes with its Task too ([] for
    # every other Task: TaskRunner input cards keep their behaviour).
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


async def fail_task_runtime_approval_origin(
    db: AsyncSession,
    *,
    request: HitlRequest,
    reason: str,
) -> bool:
    """Fail closed when a Task-origin request cannot be safely resumed."""

    task_id = str(getattr(request, "origin_task_id", None) or "").strip()
    if not task_id:
        return False
    await lock_task_runtime_approval_boundary(db, task_id=task_id)
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.entity_id == request.entity_id,
            Task.conversation_id == request.origin_conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    changed = await _expire_locked_runtime_request(db, request, reason=reason)
    if task is None or task.status in TERMINAL_STATUSES:
        return changed

    details = dict(task.details or {})
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    if isinstance(marker, dict) and marker.get("hitl_id") == request.id:
        details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
            **marker,
            "state": "finished",
            "result_status": "failed",
            "failure_reason": reason,
        }
        task.details = details
    await apply_task_status_transition(task, "failed", db=db)

    # The terminal gateway delivery for a scheduled Task is staged by
    # apply_task_status_transition (scheduled_task_terminal_delivery) in this
    # same transaction; do not stage a second one here.
    return True


async def fail_task_runtime_approval_task(
    db: AsyncSession,
    *,
    task: Task,
    reason: str,
    error_type: str = "TaskRuntimeApprovalError",
) -> Task:
    """Terminalize a Task through the shared approval boundary.

    Callers use this when checkpoint/delivery validation fails before a single
    request can be trusted.  Looking up *all* Task-origin requests is crucial:
    it also closes a request committed just before a worker died while writing
    its checkpoint.
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
    details = dict(locked.details or {})
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    if isinstance(marker, dict):
        details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
            **marker,
            "state": "finished",
            "result_status": "failed",
            "failure_reason": reason,
        }
    details["failure_reason"] = reason
    locked.details = details
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


async def claim_task_runtime_approval_execution(
    db: AsyncSession,
    *,
    request: HitlRequest,
    task_id: str | None,
) -> bool:
    """Linearize an approved Task action immediately before tool/provider I/O."""

    origin_task_id = str(getattr(request, "origin_task_id", None) or "").strip()
    if not origin_task_id:
        return False
    if str(task_id or "") != origin_task_id:
        raise TaskRuntimeApprovalError(
            "Task runtime approval cannot execute outside its origin Task"
        )
    await lock_task_runtime_approval_boundary(db, task_id=origin_task_id)
    context = dict(request.context) if isinstance(request.context, dict) else {}
    checkpoint_raw = context.get(TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY)
    resolution = context.get(TASK_RUNTIME_RESOLUTION_CONTEXT_KEY)
    if not isinstance(checkpoint_raw, dict) or not isinstance(resolution, dict):
        raise TaskRuntimeApprovalError("runtime approval continuation is missing")
    try:
        checkpoint = RuntimeTaskHitlCheckpoint.from_dict(checkpoint_raw)
    except (TypeError, ValueError) as exc:
        raise TaskRuntimeApprovalError(
            "runtime approval checkpoint is invalid"
        ) from exc
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == origin_task_id,
            Task.entity_id == request.entity_id,
            Task.conversation_id == request.origin_conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise TaskRuntimeApprovalError("runtime approval Task is missing")
    details = dict(task.details or {})
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    signature = _validate_checkpoint_signature(
        checkpoint=checkpoint,
        request=request,
        marker=marker if isinstance(marker, dict) else None,
        resolution=resolution,
    )
    resolution_signature = _validate_runtime_resolution_signature(resolution)
    conversation = await _load_scope_conversation(db, task=task)
    await _validate_checkpoint_scope(
        db,
        checkpoint=checkpoint,
        task=task,
        request=request,
        conversation=conversation,
    )
    if (
        task.status in TERMINAL_STATUSES
        or not isinstance(marker, dict)
        or marker.get("state") not in {"resume_queued", "resuming"}
        or marker.get("choice") != "approved"
        or marker.get("hitl_id") != request.id
        or marker.get("generation") != checkpoint.generation
        or marker.get("checkpoint_digest") != checkpoint.digest()
        or marker.get("checkpoint_signature") != signature
        or marker.get("resolution_signature") != resolution_signature
        or resolution.get("choice") != "approved"
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval origin Task is no longer executable"
        )
    execution = context.get(TASK_RUNTIME_EXECUTION_CONTEXT_KEY)
    if marker.get("state") == "resume_queued":
        if execution is not None:
            raise TaskRuntimeApprovalError(
                "runtime approval execution attempt already exists"
            )
        execution = {
            "version": 1,
            "attempt_id": generate_ulid(),
            "phase": "executing",
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "checkpoint_digest": checkpoint.digest(),
            "resolution_signature": resolution_signature,
            "result_status": None,
            "result_digest": None,
        }
        execution["execution_signature"] = _runtime_execution_signature(execution)
        context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY] = execution
        request.context = context
        details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
            **marker,
            "state": "resuming",
            "execution_attempt_id": execution["attempt_id"],
            "execution_signature": execution["execution_signature"],
        }
        task.details = details
        return True
    if not isinstance(execution, dict):
        raise TaskRuntimeApprovalError(
            "runtime approval execution attempt is missing"
        )
    execution_signature = _validate_runtime_execution_signature(execution)
    if (
        execution.get("phase") != "executing"
        or marker.get("execution_attempt_id") != execution.get("attempt_id")
        or marker.get("execution_signature") != execution_signature
        or execution.get("checkpoint_digest") != checkpoint.digest()
        or execution.get("resolution_signature") != resolution_signature
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval execution attempt is stale"
        )
    return False


async def complete_task_runtime_approval_execution(
    db: AsyncSession,
    *,
    task_id: str,
    checkpoint: RuntimeTaskHitlCheckpoint,
    result_status: str,
    result_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit the receipt for a claimed action beside Task finalization.

    The receipt distinguishes a completed handler from the unavoidable crash
    ambiguity of arbitrary external side effects.  It is written in the same
    transaction as the terminal Task update by ``TaskRunner``.
    """

    await lock_task_runtime_approval_boundary(db, task_id=task_id)
    request = (await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.id == checkpoint.hitl_id,
            HitlRequest.origin_task_id == task_id,
            HitlRequest.origin_conversation_id == checkpoint.conversation_id,
            HitlRequest.entity_id == checkpoint.entity_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None:
        raise TaskRuntimeApprovalError("runtime approval execution request is missing")
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.entity_id == checkpoint.entity_id,
            Task.conversation_id == checkpoint.conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise TaskRuntimeApprovalError(
            "runtime approval execution Task is no longer receivable"
        )
    context = dict(request.context or {})
    resolution = context.get(TASK_RUNTIME_RESOLUTION_CONTEXT_KEY)
    execution = context.get(TASK_RUNTIME_EXECUTION_CONTEXT_KEY)
    if not isinstance(resolution, dict) or not isinstance(execution, dict):
        raise TaskRuntimeApprovalError(
            "runtime approval execution state is missing"
        )
    resolution_signature = _validate_runtime_resolution_signature(resolution)
    execution_signature = _validate_runtime_execution_signature(execution)
    details = dict(task.details or {})
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    if (
        request.status != ApprovalStatus.CONSUMED.value
        or not isinstance(marker, dict)
        or marker.get("state") not in {"resuming", "finished"}
        or marker.get("hitl_id") != checkpoint.hitl_id
        or marker.get("generation") != checkpoint.generation
        or marker.get("execution_attempt_id") != execution.get("attempt_id")
        or marker.get("execution_signature") != execution_signature
        or execution.get("phase") != "executing"
        or execution.get("checkpoint_digest") != checkpoint.digest()
        or execution.get("resolution_signature") != resolution_signature
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval execution receipt no longer matches its attempt"
        )
    canonical_result = dict(result_payload or {})
    encoded_result = json.dumps(
        canonical_result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    completed = {
        **execution,
        "phase": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "result_status": str(result_status or ""),
        "result_digest": hashlib.sha256(encoded_result).hexdigest(),
    }
    completed["execution_signature"] = _runtime_execution_signature(completed)
    context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY] = completed
    request.context = context
    details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
        **marker,
        "execution_phase": "completed",
        "execution_signature": completed["execution_signature"],
    }
    task.details = details
    return completed


async def mark_task_runtime_approval_execution_ambiguous(
    db: AsyncSession,
    *,
    task: Task,
    reason: str,
) -> bool:
    """Durably close a claimed attempt that has no trustworthy receipt."""

    await lock_task_runtime_approval_boundary(db, task_id=task.id)
    details = dict(task.details or {})
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    if not isinstance(marker, dict) or marker.get("state") != "resuming":
        return False
    request = (await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.id == str(marker.get("hitl_id") or ""),
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.origin_conversation_id == task.conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None:
        raise TaskRuntimeApprovalError(
            "ambiguous runtime approval execution request is missing"
        )
    context = dict(request.context or {})
    execution = context.get(TASK_RUNTIME_EXECUTION_CONTEXT_KEY)
    if not isinstance(execution, dict):
        raise TaskRuntimeApprovalError(
            "ambiguous runtime approval execution attempt is missing"
        )
    execution_signature = _validate_runtime_execution_signature(execution)
    provider_execution_started = context.get("kind") == "provider"
    if (
        request.status not in (
            {
                ApprovalStatus.CONSUMED.value,
                ApprovalStatus.GRANTED.value,
                ApprovalStatus.EXPIRED.value,
            }
            if provider_execution_started
            else {ApprovalStatus.CONSUMED.value}
        )
        or execution.get("phase") != "executing"
        or marker.get("execution_attempt_id") != execution.get("attempt_id")
        or marker.get("execution_signature") != execution_signature
    ):
        raise TaskRuntimeApprovalError(
            "ambiguous runtime approval execution attempt is stale"
        )
    ambiguous = {
        **execution,
        "phase": "ambiguous",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "result_status": "failed",
        "result_digest": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
    }
    ambiguous["execution_signature"] = _runtime_execution_signature(ambiguous)
    context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY] = ambiguous
    request.context = context
    details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
        **marker,
        "state": "finished",
        "execution_phase": "ambiguous",
        "execution_signature": ambiguous["execution_signature"],
        "result_status": "failed",
        "failure_reason": "runtime_approval_execution_ambiguous",
    }
    task.details = details
    return True


async def stage_task_runtime_approval_resolution(
    db: AsyncSession,
    *,
    request: HitlRequest,
    choice: str,
    runtime_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """CAS a Task gate to resume-queued and stage its transactional outbox."""

    task_id = str(getattr(request, "origin_task_id", None) or "").strip()
    if not task_id:
        return None
    await lock_task_runtime_approval_boundary(db, task_id=task_id)
    request_context = getattr(request, "context", None)
    checkpoint_raw = (
        request_context if isinstance(request_context, dict) else {}
    ).get(
        TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY
    )
    if not isinstance(checkpoint_raw, dict):
        raise TaskRuntimeApprovalError(
            "Task-origin runtime approval has no durable checkpoint"
        )
    try:
        checkpoint = RuntimeTaskHitlCheckpoint.from_dict(checkpoint_raw)
    except (TypeError, ValueError) as exc:
        raise TaskRuntimeApprovalError(
            "Task-origin runtime approval checkpoint is invalid"
        ) from exc
    if (
        checkpoint.task_id != task_id
        or checkpoint.hitl_id != request.id
        or checkpoint.conversation_id != request.origin_conversation_id
    ):
        raise TaskRuntimeApprovalError("runtime approval checkpoint lineage mismatch")
    if choice not in {"approve", "always_approve", "reject"}:
        raise TaskRuntimeApprovalError("runtime approval choice is invalid")
    normalized_choice = (
        "approved" if choice in {"approve", "always_approve"} else "rejected"
    )
    expected_request_status = (
        ApprovalStatus.GRANTED.value
        if normalized_choice == "approved"
        else ApprovalStatus.DENIED.value
    )
    if request.status != expected_request_status:
        raise TaskRuntimeApprovalError("runtime approval request state is inconsistent")
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.entity_id == request.entity_id,
            Task.conversation_id == request.origin_conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise TaskRuntimeApprovalError("runtime approval Task is no longer resumable")
    conversation = await _load_scope_conversation(db, task=task)
    details = dict(task.details or {})
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    signature = _validate_checkpoint_signature(
        checkpoint=checkpoint,
        request=request,
        marker=marker if isinstance(marker, dict) else None,
    )
    await _validate_checkpoint_scope(
        db,
        checkpoint=checkpoint,
        task=task,
        request=request,
        conversation=conversation,
    )
    marker_identity_valid = (
        isinstance(marker, dict)
        and str(marker.get("hitl_id") or "") == request.id
        and marker.get("generation") == checkpoint.generation
        and str(marker.get("checkpoint_digest") or "") == checkpoint.digest()
        and marker.get("checkpoint_signature") == signature
    )
    if not marker_identity_valid:
        raise TaskRuntimeApprovalError("runtime approval checkpoint is stale")

    existing_resolution = (request.context or {}).get(
        TASK_RUNTIME_RESOLUTION_CONTEXT_KEY
    )
    if marker.get("state") == "resume_queued":
        if isinstance(existing_resolution, dict):
            _validate_runtime_resolution_signature(existing_resolution)
        if (
            not isinstance(existing_resolution, dict)
            or marker.get("choice") != normalized_choice
            or existing_resolution.get("choice") != normalized_choice
            or existing_resolution.get("generation") != checkpoint.generation
            or existing_resolution.get("checkpoint_digest") != checkpoint.digest()
            or existing_resolution.get("checkpoint_signature") != signature
        ):
            raise TaskRuntimeApprovalError("runtime approval resolution conflicts")
        return {
            "approval_kind": "task_runtime",
            "task_runtime_resume": {
                "task_id": task.id,
                "hitl_id": request.id,
                "generation": checkpoint.generation,
                "choice": normalized_choice,
            },
        }

    if task.status in TERMINAL_STATUSES:
        raise TaskRuntimeApprovalError("runtime approval Task is no longer resumable")
    if task.status != TaskStatus.WAITING_ON_CUSTOMER:
        raise TaskRuntimeApprovalError("runtime approval Task is not waiting")
    if marker.get("state") != "waiting":
        raise TaskRuntimeApprovalError("runtime approval checkpoint is stale")

    frozen_runtime_metadata = (
        _freeze_approved_runtime_metadata(runtime_metadata, request=request)
        if normalized_choice == "approved"
        else {}
    )
    context = dict(request.context or {})
    resolution = {
        "choice": normalized_choice,
        "generation": checkpoint.generation,
        "checkpoint_digest": checkpoint.digest(),
        "checkpoint_signature": signature,
        "runtime_metadata": frozen_runtime_metadata,
    }
    resolution["resolution_signature"] = _runtime_resolution_signature(resolution)
    context[TASK_RUNTIME_RESOLUTION_CONTEXT_KEY] = resolution
    request.context = context
    details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
        **marker,
        "state": "resume_queued",
        "choice": normalized_choice,
        "resolution_signature": resolution["resolution_signature"],
    }
    task.details = details
    if request.decided_by_user_id:
        from packages.core.services.task_execution_origin import record_manual_task_execution

        record_manual_task_execution(
            task, user_id=request.decided_by_user_id, action="runtime_approval_resume",
        )
    if task.status != TaskStatus.PENDING:
        await apply_task_status_transition(task, "pending", db=db)

    expected_payload = _task_resume_payload(
        task,
        checkpoint=checkpoint,
        checkpoint_signature=signature,
        resolution_signature=resolution["resolution_signature"],
    )
    dedupe_key = f"{TASK_RUNTIME_RESUME_EVENT}:approval:{request.id}"
    event = (await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key
            == dedupe_key
        ).with_for_update()
    )).scalar_one_or_none()
    if event is None:
        db.add(
            RuntimeOutboxEvent(
                event_type=TASK_RUNTIME_RESUME_EVENT,
                aggregate_id=task.id,
                dedupe_key=dedupe_key,
                payload=expected_payload,
            )
        )
    elif (
        event.event_type != TASK_RUNTIME_RESUME_EVENT
        or event.aggregate_id != task.id
        or dict(event.payload or {}) != expected_payload
    ):
        raise TaskRuntimeApprovalError(
            "runtime approval outbox identity conflicts with its checkpoint"
        )
    return {
        "approval_kind": "task_runtime",
        "task_runtime_resume": {
            "task_id": task.id,
            "hitl_id": request.id,
            "generation": checkpoint.generation,
            "choice": normalized_choice,
        },
    }


async def load_task_runtime_approval_resume(
    db: AsyncSession,
    task: Task,
    *,
    delivery_identity: dict[str, Any] | None = None,
) -> RuntimeTaskApprovalResume | None:
    """Load and revalidate the exact continuation staged by the resolver."""

    initial_details = task.details if isinstance(task.details, dict) else {}
    initial_marker = initial_details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    if (
        not isinstance(initial_marker, dict)
        or initial_marker.get("state") != "resume_queued"
    ):
        return None
    if not isinstance(delivery_identity, dict):
        raise TaskRuntimeApprovalError(
            "runtime approval resume delivery identity is required"
        )
    await lock_task_runtime_approval_boundary(db, task_id=task.id)
    hitl_id = str(initial_marker.get("hitl_id") or "").strip()
    request = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id == hitl_id,
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.origin_conversation_id == task.conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None:
        raise TaskRuntimeApprovalError("runtime approval request is missing")
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task.id,
            Task.entity_id == request.entity_id,
            Task.conversation_id == request.origin_conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status in TERMINAL_STATUSES:
        raise TaskRuntimeApprovalError("runtime approval Task is no longer resumable")
    details = task.details if isinstance(task.details, dict) else {}
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    if not isinstance(marker, dict) or marker.get("state") != "resume_queued":
        raise TaskRuntimeApprovalError("runtime approval resume marker is stale")
    checkpoint_raw = (request.context or {}).get(
        TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY
    )
    resolution = (request.context or {}).get(TASK_RUNTIME_RESOLUTION_CONTEXT_KEY)
    if not isinstance(checkpoint_raw, dict) or not isinstance(resolution, dict):
        raise TaskRuntimeApprovalError("runtime approval continuation is missing")
    try:
        checkpoint = RuntimeTaskHitlCheckpoint.from_dict(checkpoint_raw)
    except (TypeError, ValueError) as exc:
        raise TaskRuntimeApprovalError(
            "runtime approval checkpoint is invalid"
        ) from exc
    _validate_checkpoint_signature(
        checkpoint=checkpoint,
        request=request,
        marker=marker,
        resolution=resolution,
    )
    resolution_signature = _validate_runtime_resolution_signature(resolution)
    conversation = await _load_scope_conversation(db, task=task)
    await _validate_checkpoint_scope(
        db,
        checkpoint=checkpoint,
        task=task,
        request=request,
        conversation=conversation,
    )
    if (
        checkpoint.task_id != task.id
        or checkpoint.conversation_id != task.conversation_id
        or checkpoint.hitl_id != request.id
        or checkpoint.generation != marker.get("generation")
        or checkpoint.digest() != marker.get("checkpoint_digest")
        or checkpoint.digest() != resolution.get("checkpoint_digest")
        or checkpoint.generation != resolution.get("generation")
        or marker.get("choice") != resolution.get("choice")
        or marker.get("resolution_signature") != resolution_signature
        or delivery_identity.get("runtime_hitl_id") != request.id
        or delivery_identity.get("runtime_hitl_generation")
        != checkpoint.generation
        or delivery_identity.get("runtime_hitl_checkpoint_digest")
        != checkpoint.digest()
        or delivery_identity.get("runtime_hitl_checkpoint_signature")
        != _checkpoint_signature(request)
        or delivery_identity.get("runtime_hitl_resolution_signature")
        != resolution_signature
        or str(delivery_identity.get("agent_id") or "")
        != str(checkpoint.agent_id or "")
        or str(delivery_identity.get("scheduled_run_id") or "")
        != str(checkpoint.scheduled_run_id or "")
        or str(delivery_identity.get("scheduled_job_id") or "")
        != str(checkpoint.scheduled_job_id or "")
    ):
        raise TaskRuntimeApprovalError("runtime approval continuation lineage mismatch")
    choice = str(resolution.get("choice") or "")
    if choice == "approved":
        if request.status != ApprovalStatus.GRANTED.value:
            raise TaskRuntimeApprovalError("runtime approval grant is no longer executable")
        runtime_metadata = resolution.get("runtime_metadata")
        if not isinstance(runtime_metadata, dict):
            raise TaskRuntimeApprovalError("runtime approval metadata is invalid")
        # The resolution JSON lives in the same durable row as the original
        # continuation, but it is still mutable data.  Re-derive its authority
        # at the last read before execution so a stale writer or manual DB
        # edit cannot swap the approved tool/arguments after staging.
        runtime_metadata = _freeze_approved_runtime_metadata(
            runtime_metadata,
            request=request,
        )
    elif choice == "rejected":
        if request.status != ApprovalStatus.DENIED.value:
            raise TaskRuntimeApprovalError("runtime rejection state is inconsistent")
        runtime_metadata = {}
    else:
        raise TaskRuntimeApprovalError("runtime approval choice is invalid")
    return RuntimeTaskApprovalResume(
        choice=choice,
        checkpoint=checkpoint,
        runtime_metadata=dict(runtime_metadata),
    )


async def validate_task_runtime_resume_delivery(
    db: AsyncSession,
    *,
    task_id: str,
    agent_id: str | None,
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
    runtime_hitl_id: str,
    runtime_hitl_generation: int,
    runtime_hitl_checkpoint_digest: str,
    runtime_hitl_checkpoint_signature: str,
    runtime_hitl_resolution_signature: str,
    allow_resuming: bool = False,
) -> bool:
    """Fence a broker delivery to one exact signed approval generation.

    Celery messages can be delayed or duplicated.  Without this check an old
    delivery for generation N could acquire the Task execution lease after the
    Task had already advanced to generation N+1 and accidentally resume the
    newer approval.
    """

    if (
        not task_id
        or not runtime_hitl_id
        or isinstance(runtime_hitl_generation, bool)
        or not isinstance(runtime_hitl_generation, int)
        or runtime_hitl_generation < 1
        or len(str(runtime_hitl_checkpoint_digest or "")) != 64
        or len(str(runtime_hitl_checkpoint_signature or "")) != 64
        or len(str(runtime_hitl_resolution_signature or "")) != 64
    ):
        return False
    await lock_task_runtime_approval_boundary(db, task_id=task_id)

    request = (await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.id == runtime_hitl_id,
            HitlRequest.origin_task_id == task_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None:
        return False
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.entity_id == request.entity_id,
            Task.conversation_id == request.origin_conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status in TERMINAL_STATUSES:
        return False

    context = request.context if isinstance(request.context, dict) else {}
    checkpoint_raw = context.get(TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY)
    resolution = context.get(TASK_RUNTIME_RESOLUTION_CONTEXT_KEY)
    if not isinstance(checkpoint_raw, dict) or not isinstance(resolution, dict):
        return False
    try:
        checkpoint = RuntimeTaskHitlCheckpoint.from_dict(checkpoint_raw)
        checkpoint_signature = _validate_checkpoint_signature(
            checkpoint=checkpoint,
            request=request,
            resolution=resolution,
        )
        resolution_signature = _validate_runtime_resolution_signature(resolution)
        conversation = await _load_scope_conversation(db, task=task)
        await _validate_checkpoint_scope(
            db,
            checkpoint=checkpoint,
            task=task,
            request=request,
            conversation=conversation,
        )
    except (TaskRuntimeApprovalError, TypeError, ValueError):
        return False

    details = task.details if isinstance(task.details, dict) else {}
    marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
    choice = str(resolution.get("choice") or "")
    expected_status = (
        ApprovalStatus.GRANTED.value
        if choice == "approved"
        else ApprovalStatus.DENIED.value
    )
    marker_state = marker.get("state") if isinstance(marker, dict) else None
    state_is_executable = bool(
        marker_state == "resume_queued"
        and request.status == expected_status
        and choice in {"approved", "rejected"}
    )
    if allow_resuming and marker_state == "resuming" and choice == "approved":
        execution = context.get(TASK_RUNTIME_EXECUTION_CONTEXT_KEY)
        try:
            execution_signature = (
                _validate_runtime_execution_signature(execution)
                if isinstance(execution, dict)
                else None
            )
        except TaskRuntimeApprovalError:
            execution_signature = None
        provider_execution_started = context.get("kind") == "provider"
        state_is_executable = bool(
            isinstance(execution, dict)
            and execution_signature
            and execution.get("phase") == "executing"
            and execution.get("checkpoint_digest") == checkpoint.digest()
            and execution.get("resolution_signature") == resolution_signature
            and marker.get("execution_attempt_id") == execution.get("attempt_id")
            and marker.get("execution_signature") == execution_signature
            and request.status
            in (
                {
                    ApprovalStatus.CONSUMED.value,
                    ApprovalStatus.GRANTED.value,
                    ApprovalStatus.EXPIRED.value,
                }
                if provider_execution_started
                else {ApprovalStatus.CONSUMED.value}
            )
        )

    return bool(
        isinstance(marker, dict)
        and task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}
        and state_is_executable
        and marker.get("hitl_id") == runtime_hitl_id == checkpoint.hitl_id
        and marker.get("generation")
        == runtime_hitl_generation
        == checkpoint.generation
        and marker.get("checkpoint_digest")
        == runtime_hitl_checkpoint_digest
        == checkpoint.digest()
        and marker.get("checkpoint_signature")
        == runtime_hitl_checkpoint_signature
        == checkpoint_signature
        and marker.get("resolution_signature")
        == runtime_hitl_resolution_signature
        == resolution_signature
        and str(agent_id or "") == str(checkpoint.agent_id or "")
        and str(scheduled_run_id or "")
        == str(checkpoint.scheduled_run_id or "")
        and str(scheduled_job_id or "")
        == str(checkpoint.scheduled_job_id or "")
    )


async def publish_task_runtime_approval_message(
    *,
    entity_id: str,
    owner_user_id: str | None,
    workspace_id: str | None,
    message_id: str | None,
    conversation_id: str | None,
) -> None:
    """Push a committed personal approval card; Workspace Chat self-publishes."""

    if workspace_id or not owner_user_id or not message_id or not conversation_id:
        return
    from packages.core.services.realtime import push_conversation_message

    await push_conversation_message(
        owner_user_id,
        {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "message_kind": "hitl_request",
            "author_kind": "agent",
            "has_pending_action": False,
        },
        entity_id=entity_id,
    )


def task_runtime_hitl_details(
    projection: dict[str, Any],
) -> dict[str, Any]:
    """Return the public-safe Task.details pointer for a private checkpoint."""

    return {
        key: projection.get(key)
        for key in (
            "hitl_id",
            "tool_name",
            "generation",
            "checkpoint_digest",
            "checkpoint_signature",
            "state",
        )
    }


__all__ = [
    "TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY",
    "TASK_RUNTIME_EXECUTION_CONTEXT_KEY",
    "TASK_RUNTIME_HITL_DETAIL_KEY",
    "RuntimeTaskApprovalResume",
    "TaskRuntimeApprovalError",
    "claim_task_runtime_approval_execution",
    "complete_task_runtime_approval_execution",
    "ensure_task_runtime_approval_resolvable",
    "expire_task_runtime_approval_after_checkpoint_failure",
    "expire_task_runtime_approval_for_terminal",
    "expire_task_runtime_approval_request",
    "existing_task_runtime_approval_resolution",
    "fail_task_runtime_approval_origin",
    "fail_task_runtime_approval_task",
    "load_task_runtime_approval_resume",
    "lock_task_runtime_approval_boundary",
    "mark_task_runtime_approval_execution_ambiguous",
    "persist_task_runtime_approval_gate",
    "project_task_runtime_approval_card",
    "publish_task_runtime_approval_message",
    "stage_task_runtime_approval_resolution",
    "task_runtime_approval_binding_digest",
    "task_runtime_hitl_details",
    "validate_task_runtime_resume_delivery",
]
