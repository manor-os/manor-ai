"""Park native tool approvals on their exact Workflow invocation.

Only a public request pointer enters step_results. The existing HitlRequest
owns the signed private continuation; canonical approval and Runtime consumption
remain the only grant/spend path. A missing continuation never replays an Agent.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, select

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.hitl_envelope import parse_hitl_envelope
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun

CONTEXT_KEY = "workflow_runtime_approval_v1"
RESULT_KEY = "runtime_approval"
_DOMAIN = b"manor.workflow-runtime-approval.v1\0"
_ENQUEUE_KEY = "workflow_runtime_approval_wakes"
_TIKTOK_PUBLISH_TOOLS = frozenset({
    "mcp__tiktok__post_video",
    "mcp__tiktok__post_photo",
})


class WorkflowRuntimeApprovalError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _signature(value: dict) -> str:
    from packages.core.config import get_settings
    return hmac.new(get_settings().JWT_SECRET_KEY.encode(), _DOMAIN + _digest(value).encode(), hashlib.sha256).hexdigest()


def _workflow_binding_digest(request: HitlRequest) -> str:
    """Permit only a TikTok review to update its reviewed continuation."""

    context = request.context if isinstance(request.context, dict) else {}
    from packages.core.services.task_runtime_approvals import task_runtime_approval_binding_digest

    if context.get("tool") not in _TIKTOK_PUBLISH_TOOLS:
        return task_runtime_approval_binding_digest(request)
    payload = {
        "version": 1,
        "request_id": str(request.id or ""),
        "entity_id": str(request.entity_id or ""),
        "workspace_id": str(request.workspace_id or ""),
        "task_id": str(request.origin_task_id or ""),
        "conversation_id": str(request.origin_conversation_id or ""),
        "action_key": str(request.action_key or ""),
        "capability_id": str(request.capability_id or ""),
        "hitl_type": str(request.hitl_type or ""),
        "risk_level": str(request.risk_level or ""),
        "resource_kind": str(request.resource_kind or ""),
        "resource_id": str(request.resource_id or ""),
        "tool": str(context.get("tool") or ""),
        "requested_by": str(context.get("requested_by") or ""),
        "provider": str(context.get("provider") or ""),
        "provider_approval_id": str(context.get("provider_approval_id") or ""),
        "operation_id": str(context.get("operation_id") or ""),
    }
    return _digest(payload)


def workflow_approval_owned(request: Any) -> bool:
    return CONTEXT_KEY in (getattr(request, "context", None) or {})


def workflow_approval_run_id(request: Any) -> str | None:
    """The Workflow run a runtime approval was raised by, if it names one."""
    context = getattr(request, "context", None) or {}
    record = context.get(CONTEXT_KEY)
    payload = record.get("payload") if isinstance(record, dict) else None
    if isinstance(payload, dict) and payload.get("workflow_run_id"):
        return str(payload["workflow_run_id"])
    operation = str(context.get("operation_id") or "")
    if not operation.startswith("runtime:") or ":call:" not in operation:
        return None
    identity = operation[len("runtime:"):].split(":call:", 1)[0]
    if identity.startswith("workflow:"):
        identity = identity[len("workflow:"):]
    return identity if len(identity) == 26 else None


async def workflow_approval_origin(db, request) -> bool:
    """Recognize legacy Workflow gates too, without granting them a checkpoint."""
    if workflow_approval_owned(request):
        return True
    identity = workflow_approval_run_id(request)
    if identity is None:
        return False
    run = await db.get(WorkflowRun, identity)
    return bool(run is not None and run.entity_id == request.entity_id)


ENDED_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


async def expire_ended_workflow_run_approvals(db, *, run: Any) -> int:
    """Close the approvals an ended run left open; nothing can resume them.

    A run that fails while parking its approval (or ends any other way) would
    otherwise leave a pending request counted as "needs you" forever, with no
    card to open. Requests being decided concurrently are skipped (the decision
    finds the run ended); readers also ignore them.
    """
    if str(getattr(run, "status", "") or "") not in ENDED_RUN_STATUSES:
        return 0
    from sqlalchemy import or_

    from packages.core.services.task_runtime_approvals import _expire_locked_runtime_request

    operation = HitlRequest.context["operation_id"].as_string()
    requests = (await db.execute(select(HitlRequest).where(
        HitlRequest.entity_id == run.entity_id,
        HitlRequest.status == ApprovalStatus.PENDING.value,
        or_(
            operation.like(f"runtime:{run.id}:call:%"),
            operation.like(f"runtime:workflow:{run.id}:call:%"),
            HitlRequest.context[(CONTEXT_KEY, "payload", "workflow_run_id")].as_string() == run.id,
        ),
    ).with_for_update(skip_locked=True).execution_options(populate_existing=True))).scalars().all()
    expired = 0
    for request in requests:
        if await _expire_locked_runtime_request(db, request, reason=f"workflow_run_{run.status}"):
            expired += 1
    return expired


def _private(request: Any) -> dict:
    record = (request.context or {}).get(CONTEXT_KEY)
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        raise WorkflowRuntimeApprovalError("Workflow approval has no saved continuation")
    payload = record["payload"]
    signature = record.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(signature, _signature(payload)):
        raise WorkflowRuntimeApprovalError("Workflow approval continuation is invalid")
    from packages.core.services.task_runtime_approvals import task_runtime_approval_binding_digest
    if (payload.get("request_id") != request.id
        or payload.get("binding_digest") not in {
            _workflow_binding_digest(request),
            task_runtime_approval_binding_digest(request),
        }
        or payload.get("operation_id") != (request.context or {}).get("operation_id")):
        raise WorkflowRuntimeApprovalError("Workflow approval subject changed")
    return payload


def _marker(payload: dict) -> dict:
    return {key: payload[key] for key in (
        "version", "request_id", "workflow_run_id", "step_id", "execution_node_id", "checkpoint_digest",
    )}


async def _scope(db, request, payload, *, statuses: set[str], parked: bool) -> WorkflowRun:
    from packages.core.ai.runtime.workflow_tools import runtime_workflow_run_context
    from packages.core.services.workflow_run_trace import workflow_definition_changed
    run = (await db.execute(select(WorkflowRun).where(
        WorkflowRun.id == payload["workflow_run_id"], WorkflowRun.entity_id == request.entity_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if run is None or run.status not in statuses:
        raise WorkflowRuntimeApprovalError("Workflow approval origin is no longer executable")
    context = runtime_workflow_run_context(run)
    if (
        run.started_by != payload["user_id"] or run.started_by != (request.context or {}).get("requested_by")
        or (run.workspace_id or None) != payload["workspace_id"]
        or (run.workspace_id or None) != (request.workspace_id or None)
        or context.get("conversation_id") != request.origin_conversation_id
        or context.get("conversation_id") != payload["conversation_id"]
        or context.get("task_id") != payload["task_id"]
        or context.get("task_id") != (request.origin_task_id or None)
        or run.current_step_id != payload["step_id"]
        or _digest(run.definition_snapshot or {}) != payload["definition_digest"]
        or _digest(run.execution_snapshot or {}) != payload["execution_digest"]
    ):
        raise WorkflowRuntimeApprovalError("Workflow approval scope changed")
    workflow = await db.get(WorkflowDefinition, run.workflow_id)
    if workflow is None or workflow_definition_changed(workflow, run):
        raise WorkflowRuntimeApprovalError("Workflow approval definition changed")
    conversation = await db.get(Conversation, request.origin_conversation_id)
    if conversation is None or conversation.entity_id != run.entity_id or conversation.status != "active":
        raise WorkflowRuntimeApprovalError("Workflow approval conversation is unavailable")
    if run.workspace_id:
        from packages.core.governance.service import workflow_run_is_executable

        if not await workflow_run_is_executable(
            db,
            entity_id=run.entity_id,
            workspace_id=run.workspace_id,
            user_id=run.started_by,
            workflow_run_id=run.id,
            statuses=statuses,
        ):
            raise WorkflowRuntimeApprovalError("Workflow approval Workspace is unavailable")
    if parked:
        result = (run.step_results or {}).get(payload["step_id"], {})
        if result.get(RESULT_KEY) != _marker(payload) or result.get("status") != "paused":
            raise WorkflowRuntimeApprovalError("Workflow is no longer waiting on this approval")
    return run


async def persist_workflow_runtime_approval_gate(
    db, *, step: dict, runtime_context: dict, entity_id: str, user_id: str,
    control: dict, tool_arguments: dict | None = None,
) -> dict:
    from packages.core.ai.runtime.approval_messages import approval_args_hash
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
    from packages.core.services.workflow_run_trace import workflow_execution_view
    data = parse_hitl_envelope(control.get("blocking_result"))
    hitl_id = str(((data or {}).get("hitl") or {}).get("id") or "")
    request = (await db.execute(select(HitlRequest).where(
        HitlRequest.id == hitl_id, HitlRequest.entity_id == entity_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if request is None or request.status != ApprovalStatus.PENDING:
        raise WorkflowRuntimeApprovalError("Workflow tool has no pending native approval")
    run_id = str(runtime_context.get("workflow_run_id") or "")
    step_id = str(step.get("id") or "")
    execution_id = str(runtime_context.get("workflow_execution_node_id") or step_id)
    # Inline loop/stage executions do not yet own a durable parent continuation.
    # Refuse them explicitly rather than restart already-completed iterations.
    if execution_id != step_id:
        raise WorkflowRuntimeApprovalError("This nested Workflow approval has no resumable parent checkpoint; no action was run")
    checkpoint = copy.deepcopy(control.get("checkpoint"))
    if tool_arguments is None:
        if not isinstance(checkpoint, dict):
            raise WorkflowRuntimeApprovalError("Workflow Agent approval has no saved checkpoint; do not replay the Agent")
        parsed = RuntimeAgentCheckpoint.from_dict(checkpoint)
        from packages.core.ai.runtime.nested_agent import runtime_checkpoint_leaf_call_id, runtime_pending_checkpoint_leaf

        leaf = runtime_pending_checkpoint_leaf(checkpoint)
        call = copy.deepcopy(leaf["pending_tool_call"])
        if parsed.pending_tool_call.get("id") != control.get("tool_call_id") or call.get("name") != control.get("tool_name"):
            raise WorkflowRuntimeApprovalError("Nested Skill approval has no exact resumable checkpoint; do not replay the Agent")
        call["id"] = runtime_checkpoint_leaf_call_id(checkpoint)
        runtime_run_id = run_id
    else:
        call = {"id": f"execution:{execution_id}" if runtime_context.get("workflow_execution_node_id") else f"node:{step_id}",
                "name": control.get("tool_name"), "arguments": copy.deepcopy(tool_arguments)}
        checkpoint = None
        runtime_run_id = f"workflow:{run_id}"
    request_context = request.context or {}
    if (
        request_context.get("tool") != call.get("name")
        or request_context.get("args_hash") != approval_args_hash(call.get("arguments") or {}, tool_name=call["name"])
        or request_context.get("operation_id") != f"runtime:{runtime_run_id}:call:{call['id']}"
    ):
        raise WorkflowRuntimeApprovalError("Workflow approval does not match the exact pending call")
    run = await db.get(WorkflowRun, run_id)
    if run is None:
        raise WorkflowRuntimeApprovalError("Workflow approval origin is missing")
    workflow = await db.get(WorkflowDefinition, run.workflow_id)
    view = workflow_execution_view(workflow, run)
    if view is None or not any(item.get("id") == step_id for item in view.steps):
        raise WorkflowRuntimeApprovalError("Workflow approval node is not a durable top-level node")
    payload = {
        "version": 1, "request_id": request.id, "workflow_run_id": run_id,
        "step_id": step_id, "execution_node_id": execution_id,
        "user_id": user_id, "workspace_id": runtime_context.get("workspace_id"),
        "agent_id": runtime_context.get("workflow_agent_id") or None,
        "conversation_id": runtime_context.get("conversation_id"), "task_id": runtime_context.get("task_id"),
        "definition_digest": _digest(run.definition_snapshot or {}),
        "execution_digest": _digest(run.execution_snapshot or {}),
        "binding_digest": _workflow_binding_digest(request),
        "operation_id": request_context.get("operation_id"),
        "runtime_run_id": runtime_run_id, "call": call, "checkpoint": checkpoint,
        "checkpoint_digest": _digest(checkpoint if checkpoint is not None else call),
        "hitl_data": data,
    }
    await _scope(db, request, payload, statuses={"running"}, parked=False)
    existing = request_context.get(CONTEXT_KEY)
    if existing is not None and existing != {"payload": payload, "signature": _signature(payload)}:
        raise WorkflowRuntimeApprovalError("Workflow approval already belongs to another continuation")
    request.context = {**request_context, CONTEXT_KEY: {"payload": payload, "signature": _signature(payload)}}
    await db.flush()
    return {"status": "paused", "code": "runtime_approval_required", "resume_strategy": "blocked",
            RESULT_KEY: _marker(payload), "output": "Approval is required before this step can continue."}


async def project_workflow_runtime_approval(db, *, run, step, result) -> None:
    if run.status != "paused" or not isinstance(result, dict) or result.get("code") != "runtime_approval_required":
        return
    marker = result.get(RESULT_KEY) or {}
    request = await db.get(HitlRequest, marker.get("request_id"))
    if request is None:
        raise WorkflowRuntimeApprovalError("Workflow approval is missing")
    payload = _private(request)
    if marker != _marker(payload) or payload["workflow_run_id"] != run.id:
        raise WorkflowRuntimeApprovalError("Workflow approval projection scope changed")
    event_id = f"workflow-runtime-hitl:{request.id}"
    if (await db.execute(select(Message.id).where(
        Message.conversation_id == request.origin_conversation_id,
        Message.response_surface_event_id == event_id,
    ))).scalar_one_or_none():
        return
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.hitl_requests import hitl_requests_from_data
    await add_message(db, request.origin_conversation_id, role="assistant",
        content="This Workflow needs approval before it can continue.",
        meta={"hitl_requests": hitl_requests_from_data(payload["hitl_data"]),
              "workflow_run_id": run.id, "workflow_step_id": step["id"], "workflow_runtime_hitl": True},
        response_surface_event_id=event_id, message_kind="hitl_request",
        refs=[{"type": "workflow_run", "id": run.id}, {"type": "workflow_step", "id": step["id"]}])


async def ensure_workflow_approval_current(db, *, request) -> WorkflowRun:
    payload = _private(request)
    return await _scope(db, request, payload, statuses={"paused"}, parked=True)


def workflow_approval_effect_window(run: WorkflowRun, request: HitlRequest) -> dict | None:
    """Project the existing frozen deadline, never a new approval or extension."""
    from packages.core.ai.runtime.effect_deadline import workflow_effect_deadline_covers_action
    from packages.core.ai.runtime.workflow_tools import runtime_workflow_run_context

    context = runtime_workflow_run_context(run)
    deadline_at = context.get("workflow_effect_deadline_at")
    if not deadline_at or not workflow_effect_deadline_covers_action(
        request.action_key or "", context.get("workflow_effect_deadline_action_keys") or [],
    ):
        return None
    now = datetime.now(timezone.utc)
    return {
        "deadline_at": deadline_at,
        "server_now": now.isoformat(),
        "expired": now >= datetime.fromisoformat(deadline_at.replace("Z", "+00:00")),
        "run_url": f"/flows/{run.workflow_id}/runs/{run.id}",
    }


def _dispatch(session) -> None:
    if session.in_nested_transaction():
        return
    from packages.core.ai.workflow_runner import WorkflowRunner
    for run_id, token, _owner in session.info.pop(_ENQUEUE_KEY, []):
        WorkflowRunner.enqueue_resume(run_id, 0, continuation_token=token)


def _discard(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_ENQUEUE_KEY, None)


def _discard_nested(session, transaction) -> None:
    if not transaction.nested:
        return

    def belongs(owner):
        while owner is not None:
            if owner is transaction:
                return True
            owner = owner.parent
        return False

    session.info[_ENQUEUE_KEY] = [item for item in session.info.get(_ENQUEUE_KEY, []) if not belongs(item[2])]


def _queue_wake(session, run_id: str, token: str) -> None:
    session.info.setdefault(_ENQUEUE_KEY, []).append((run_id, token, session.get_nested_transaction()))
    if not session.info.get(_ENQUEUE_KEY + "_listener"):
        event.listen(session, "after_commit", _dispatch)
        event.listen(session, "after_rollback", _discard)
        event.listen(session, "after_soft_rollback", _discard_nested)
        session.info[_ENQUEUE_KEY + "_listener"] = True


async def stage_workflow_approval_resolution(db, *, request, choice, runtime_metadata=None) -> dict:
    run = await ensure_workflow_approval_current(db, request=request)
    payload = _private(request)
    approved = choice in {"approve", "always_approve"}
    if request.status != (ApprovalStatus.GRANTED if approved else ApprovalStatus.DENIED):
        raise WorkflowRuntimeApprovalError("Workflow approval decision is inconsistent")
    results = dict(run.step_results or {})
    previous = dict(results[payload["step_id"]])
    if approved:
        previous["resume_strategy"] = "reexecute"
        previous["approval_resolution"] = "approved"
        now = datetime.now(timezone.utc)
        run.continuation_token = generate_ulid()
        run.continuation_due_at = run.continuation_next_attempt_at = now
        _queue_wake(db.sync_session, run.id, run.continuation_token)
    else:
        previous.update(status="failed", code="runtime_approval_rejected", error="The requested action was not approved. No action was run.")
        run.status = "failed"
        run.error = previous["error"]
        run.completed_at = datetime.now(timezone.utc)
        from packages.core.ai.workflow_runner import mark_workflow_terminal_effects_pending
        mark_workflow_terminal_effects_pending(run)
    results[payload["step_id"]] = previous
    run.step_results = results
    from packages.core.services.workflow_chat_projection import project_workflow_run_status
    await project_workflow_run_status(db, run=run)
    return {"approval_kind": "workflow_runtime", "approval_outcome": "approved" if approved else "rejected",
            "workflow_runtime_resume": {"run_id": run.id, "step_id": payload["step_id"], "request_id": request.id}}


async def load_workflow_approval_resume(db, *, previous_result, runtime_context, entity_id, user_id) -> dict | None:
    marker = (previous_result or {}).get(RESULT_KEY)
    if not isinstance(marker, dict):
        return None
    request = (await db.execute(select(HitlRequest).where(
        HitlRequest.id == marker.get("request_id"), HitlRequest.entity_id == entity_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if request is None or request.status != ApprovalStatus.GRANTED:
        raise WorkflowRuntimeApprovalError("Workflow approval is not granted or was already consumed")
    payload = _private(request)
    run = await _scope(db, request, payload, statuses={"running"}, parked=True)
    current = (run.step_results or {}).get(payload["step_id"], {})
    if (marker != _marker(payload) or current.get("approval_resolution") != "approved"
        or payload["workflow_run_id"] != runtime_context.get("workflow_run_id")
        or payload["step_id"] != runtime_context.get("workflow_step_id") or payload["user_id"] != user_id):
        raise WorkflowRuntimeApprovalError("Workflow approval resume scope changed")
    saved = copy.deepcopy(payload)
    if (request.context or {}).get("tool") in _TIKTOK_PUBLISH_TOOLS:
        continuation = (request.context or {}).get("continuation")
        arguments = continuation.get("arguments") if isinstance(continuation, dict) else None
        if not isinstance(arguments, dict):
            raise WorkflowRuntimeApprovalError("TikTok approval has no reviewed continuation")
        saved["call"]["arguments"] = copy.deepcopy(arguments)
    saved["call"]["arguments"]["approval_token"] = request.id
    if saved["checkpoint"] is not None:
        from packages.core.ai.runtime.nested_agent import runtime_pending_checkpoint_leaf

        leaf = runtime_pending_checkpoint_leaf(saved["checkpoint"])
        leaf["pending_tool_call"]["arguments"] = copy.deepcopy(saved["call"]["arguments"])
    return saved


async def validate_workflow_approval_consumption(db, *, request, arguments, runtime_envelope) -> None:
    payload = _private(request)
    await _scope(db, request, payload, statuses={"running"}, parked=True)
    from packages.core.ai.runtime.approval_service import _is_workflow_runtime_surface
    if (not _is_workflow_runtime_surface(getattr(runtime_envelope, "surface", None))
        or arguments.get("_workflow_run_id_from_context") != payload["workflow_run_id"]
        or arguments.get("_workflow_step_id_from_context") != payload["step_id"]
        or arguments.get("_runtime_tool_call_id_from_context") != payload["call"]["id"]):
        raise WorkflowRuntimeApprovalError("Workflow approval cannot execute in another origin")


async def resolve_workflow_approval(
    db, *, run, request_id: str, user_id: str, choice: str, submitted_review=None,
):
    marker = ((run.step_results or {}).get(run.current_step_id) or {}).get(RESULT_KEY)
    if not isinstance(marker, dict) or marker.get("request_id") != request_id or marker.get("workflow_run_id") != run.id:
        raise WorkflowRuntimeApprovalError("Workflow is not waiting on this approval")
    request = await db.get(HitlRequest, request_id)
    if request is None or request.entity_id != run.entity_id:
        raise WorkflowRuntimeApprovalError("Workflow approval is unavailable")
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn
    return await resolve_runtime_approval_turn(db, conversation_id=request.origin_conversation_id,
        entity_id=run.entity_id, user_id=user_id, hitl_id=request_id, action=choice,
        submitted_review=submitted_review)
