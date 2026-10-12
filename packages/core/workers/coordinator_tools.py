"""Coordinator delegation tool for the internal worker (task coordinator mode).

In coordinator mode the planned DAG becomes one ``coordinate`` subagent step
whose model works through a checklist by delegating each item to a workspace
service. Delegation reuses the workspace-chat delegation action
(``runtime_workspace_delegate_service_action``); this module adds what a
plan-driven run needs on top of it:

* every delegation becomes a child ``ExecutionStep`` row under the parent
  plan (inspectable, billable per service, cleaned up by the executor if the
  parent dies), and
* the delegate's typed JSON is returned to the model with only trusted child
  evidence ownership added, so a HITL envelope inside it is promoted to a
  lease pause by the worker's existing ``_pending_action_from_agent_messages``
  scan.
"""
from __future__ import annotations

import json
import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_HANDOFF_ACK_VERSION,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY,
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    COORDINATOR_INPUT_CONTEXT_PARAM_KEY,
    COORDINATOR_INPUT_CONTEXT_VERSION,
    COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY,
    COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION,
    COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_TYPED_VERSION,
    COORDINATOR_VERSION_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    DELEGATE_TO_SERVICE_TOOL_NAME,
    ExecutionStepStatus,
    WorkLeaseStatus,
)
from packages.core.ai.runtime.control import RuntimeToolSuspension
from packages.core.constants.hitl_envelope import parse_hitl_envelope
from packages.core.database import async_session
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionStep
from packages.core.models.worker import WorkLease
from packages.core.plans.coordinator import (
    coordinator_child_idempotency_key,
    coordinator_child_step_key,
)
from packages.core.workers.transcript import (
    build_worker_transcript_checkpoint,
    is_worker_transcript_checkpoint,
    worker_transcript_dropped_tool_result_ids,
    worker_transcript_resume_messages,
)

logger = logging.getLogger(__name__)

_TERMINAL_CHILD_STATUSES = frozenset({
    ExecutionStepStatus.DONE.value,
    ExecutionStepStatus.FAILED.value,
    ExecutionStepStatus.SKIPPED.value,
    ExecutionStepStatus.CANCELLED.value,
})


async def _lock_live_parent_execution(
    db: Any,
    *,
    parent_step_id: str | None,
    parent_lease_id: str | None,
    parent_execution_claim_id: str | None,
    expected_plan_id: str | None = None,
    expected_step_key: str | None = None,
    approval_lock_request_id: str | None = None,
    approval_nested_request_id: str | None = None,
    expected_task_id: str | None = None,
    expected_entity_id: str | None = None,
    expected_workspace_id: str | None = None,
) -> tuple[WorkLease, ExecutionStep] | None:
    """Lock and validate the exact live execution that owns a parent step.

    Ordinary child mutations use ``WorkLease -> parent ExecutionStep -> child
    ExecutionStep``.  Approval resume is the exception: the human decision
    path already owns the outer ``HitlRequest`` before it locks Task/Plan
    execution state.  Passing ``approval_lock_request_id`` gives resume that
    same ``HitlRequest -> Task -> Plan -> WorkLease -> Step`` prefix, so the
    two paths cannot each hold one half of the continuation and wait for the
    other.

    The execution claim is intentionally part of the fence: a reclaimed lease
    has the same lease/step ids, so checking those ids alone lets the old
    process checkpoint or settle work owned by its successor.
    """

    step_id = str(parent_step_id or "").strip()
    lease_id = str(parent_lease_id or "").strip()
    claim_id = str(parent_execution_claim_id or "").strip()
    if not step_id or not lease_id or not claim_id:
        return None

    plan_id = str(expected_plan_id or "").strip()
    task_id = str(expected_task_id or "").strip()
    entity_id = str(expected_entity_id or "").strip()
    workspace_id = str(expected_workspace_id or "").strip()
    approval_request_id = str(approval_lock_request_id or "").strip()
    nested_request_id = str(approval_nested_request_id or "").strip()
    if approval_request_id:
        if (
            not plan_id
            or not task_id
            or not entity_id
            or not workspace_id
            or not nested_request_id
        ):
            return None

        from packages.core.constants.approvals import ApprovalOriginKind
        from packages.core.models.execution import ExecutionPlan
        from packages.core.models.hitl_request import HitlRequest
        from packages.core.models.task import Task

        approval_request = (await db.execute(
            select(HitlRequest)
            .where(
                HitlRequest.id == approval_request_id,
                HitlRequest.entity_id == entity_id,
                HitlRequest.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        approval_context = (
            approval_request.context
            if approval_request is not None
            and isinstance(approval_request.context, dict)
            else {}
        )
        approval_binding = approval_context.get(
            COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY
        )
        pending_kind = str(approval_context.get("pending_kind") or "human_input")
        if (
            approval_request is None
            or approval_request.origin_kind != ApprovalOriginKind.LEASE.value
            or str(approval_request.origin_step_id or "") != step_id
            or str(approval_request.origin_plan_id or "") != plan_id
            or str(approval_request.origin_task_id or "") != task_id
            or approval_request.dedup_key != f"lease:{lease_id}:{pending_kind}"
            or not isinstance(approval_binding, dict)
            or approval_binding.get("version")
            != COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION
            or str(approval_binding.get("request_id") or "")
            != nested_request_id
            or str(approval_binding.get("parent_step_id") or "") != step_id
            or str(approval_binding.get("parent_lease_id") or "") != lease_id
            or str(approval_binding.get("plan_id") or "") != plan_id
            or str(approval_binding.get("task_id") or "") != task_id
            or not str(approval_binding.get("child_step_id") or "").strip()
            or not str(
                approval_binding.get("parent_execution_claim_id") or ""
            ).strip()
        ):
            return None
        task = (await db.execute(
            select(Task)
            .where(
                Task.id == task_id,
                Task.entity_id == entity_id,
                Task.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None:
            return None
        plan = (await db.execute(
            select(ExecutionPlan)
            .where(
                ExecutionPlan.id == plan_id,
                ExecutionPlan.entity_id == entity_id,
                ExecutionPlan.workspace_id == workspace_id,
                ExecutionPlan.task_id == task_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if plan is None:
            return None

    lease = (await db.execute(
        select(WorkLease)
        .where(WorkLease.id == lease_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if (
        lease is None
        or lease.status != WorkLeaseStatus.ACTIVE.value
        or lease.step_id != step_id
        or (plan_id and lease.plan_id != plan_id)
        or (entity_id and lease.entity_id != entity_id)
        or (workspace_id and lease.workspace_id != workspace_id)
        or not str(lease.execution_claim_id or "").strip()
        or str(lease.execution_claim_id) != claim_id
        or lease.lease_until is None
        or lease.lease_until <= now
    ):
        return None

    parent = (await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.id == step_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        parent is None
        or parent.step_status != ExecutionStepStatus.RUNNING.value
        or str(parent.current_lease_id or "") != lease_id
        or (plan_id and parent.plan_id != plan_id)
        or (entity_id and parent.entity_id != entity_id)
        or (workspace_id and parent.workspace_id != workspace_id)
        or (expected_step_key is not None and parent.step_key != expected_step_key)
    ):
        return None
    return lease, parent


async def _coordinator_approval_lock_request_id(
    db: Any,
    *,
    nested_request_id: str | None,
    entity_id: str | None,
    workspace_id: str | None,
) -> str | None:
    """Discover the request row that serializes a coordinator approval resume.

    Discovery is deliberately non-locking and grants no mutation authority.
    Once an outer lease request exists it is the first lock used by every human
    decision surface, so resume must wait on that row too.  Before the outer
    request exists, dispatcher binding still owns Lease -> parent -> child ->
    nested request; returning ``None`` preserves that producer order and avoids
    introducing the mirror-image nested-request -> Lease cycle.  The outer
    request is the point at which a human decision can race resume.

    ``_lock_live_parent_execution`` re-reads and locks the discovered outer row
    before any Task/Plan/Lease/Step mutation is allowed.
    """

    token = str(nested_request_id or "").strip()
    scoped_entity_id = str(entity_id or "").strip()
    scoped_workspace_id = str(workspace_id or "").strip()
    if not token or not scoped_entity_id or not scoped_workspace_id:
        return None

    from packages.core.models.hitl_request import HitlRequest

    request = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id == token,
            HitlRequest.entity_id == scoped_entity_id,
            HitlRequest.workspace_id == scoped_workspace_id,
        )
    )).scalar_one_or_none()
    if request is None:
        return None
    context = request.context if isinstance(request.context, dict) else {}
    reverse = context.get(COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY)
    if (
        not isinstance(reverse, dict)
        or reverse.get("version") != COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION
        or str(reverse.get("nested_request_id") or "") != token
    ):
        return None
    outer_id = (
        str(reverse.get("request_id") or "").strip()
        if isinstance(reverse, dict)
        else ""
    )
    return outer_id or None


def _coordinator_nested_request_matches_execution(
    request: Any,
    *,
    child: ExecutionStep,
    parent: ExecutionStep,
    lease: WorkLease,
    task_id: str,
    idempotency_key: str,
    outer_request_id: str | None,
) -> bool:
    """Validate a locked Runtime request against its exact coordinator run."""

    from packages.core.constants.approvals import ApprovalOriginKind

    child_params = child.params if isinstance(child.params, dict) else {}
    context = (
        request.context
        if isinstance(getattr(request, "context", None), dict)
        else {}
    )
    resume = context.get("delegation_resume")
    reverse = context.get(COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY)
    request_id = str(getattr(request, "id", "") or "").strip()
    expected_task_id = str(task_id or "").strip()
    expected_outer_id = str(outer_request_id or "").strip()
    if (
        not request_id
        or getattr(request, "origin_kind", None) != ApprovalOriginKind.TOOL_CALL.value
        or getattr(request, "entity_id", None) != child.entity_id
        or getattr(request, "workspace_id", None) != child.workspace_id
        or str(getattr(request, "origin_step_id", "") or "") != child.id
        or str(getattr(request, "origin_task_id", "") or "") != expected_task_id
        or child.plan_id != parent.plan_id
        or child.entity_id != parent.entity_id
        or child.workspace_id != parent.workspace_id
        or lease.step_id != parent.id
        or lease.plan_id != parent.plan_id
        or lease.entity_id != parent.entity_id
        or lease.workspace_id != parent.workspace_id
        or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
        or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent.step_key
        or not isinstance(resume, dict)
        or resume.get("version") != 1
        or str(resume.get("approval_token") or "") != request_id
        or str(resume.get("task_id") or "") != expected_task_id
        or str(resume.get("step_id") or "") != child.id
        or str(resume.get("service_key") or "") != str(child.service_key or "")
        or str(resume.get("delegation_idempotency_key") or "")
        != idempotency_key
    ):
        return False
    if reverse is None:
        return not expected_outer_id
    if (
        not isinstance(reverse, dict)
        or reverse.get("version") != COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION
        or str(reverse.get("nested_request_id") or "") != request_id
        or str(reverse.get("child_step_id") or "") != child.id
        or str(reverse.get("parent_step_id") or "") != parent.id
        or str(reverse.get("parent_lease_id") or "") != lease.id
        or str(reverse.get("plan_id") or "") != parent.plan_id
        or str(reverse.get("task_id") or "") != expected_task_id
        or str(reverse.get("child_step_key") or "") != child.step_key
        or str(reverse.get("service_key") or "") != str(child.service_key or "")
        or str(reverse.get("idempotency_key") or "") != idempotency_key
        or not str(reverse.get("parent_execution_claim_id") or "").strip()
    ):
        return False
    reverse_outer_id = str(reverse.get("request_id") or "").strip()
    return reverse_outer_id == expected_outer_id


def build_delegate_service_tool(allowed_services: list[str]) -> dict[str, Any]:
    """OpenAI function schema for ``delegate_to_service``."""
    service_key: dict[str, Any] = {
        "type": "string",
        "description": (
            "Legacy-plan compatibility only. New plans derive the exact active "
            "workspace subscription from the persisted item contract."
        ),
    }
    services = [str(item) for item in (allowed_services or []) if str(item or "").strip()]
    if services:
        service_key["enum"] = services
    return {
        "type": "function",
        "function": {
            "name": DELEGATE_TO_SERVICE_TOOL_NAME,
            "description": (
                "Run one exact persisted checklist item through its typed executor. "
                "Runtime derives its service, action, approval, human-input, or sleep "
                "contract from storage and returns a typed JSON handoff. Read "
                "`result`, `evidence_refs`, `artifacts`, and `knowledge_refs` for the "
                "durable outcome and `status`/`error` for completion control."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "checklist_item_key": {
                        "type": "string",
                        "description": (
                            "The exact bracketed checklist item key. It is the stable "
                            "identity used to resume or reuse this delegation."
                        ),
                    },
                    "service_key": service_key,
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Legacy-plan compatibility only. New plans derive the "
                            "authoritative prompt and dependencies from storage."
                        ),
                    },
                    "max_rounds": {
                        "type": "integer",
                        "description": "Optional cap on the delegate's tool rounds.",
                    },
                },
                # V2 derives every executable field from the persisted source
                # contract. The optional legacy fields keep old materialised
                # coordinator plans resumable during rolling upgrades.
                "required": ["checklist_item_key"],
            },
        },
    }


def _checklist_contract(
    s: dict[str, Any],
    *,
    item_key: str,
    service_key: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve one authoritative source-step contract from the parent snapshot."""

    params = s.get("params") if isinstance(s.get("params"), dict) else {}
    checklist = params.get("checklist") if isinstance(params, dict) else None
    matches = [
        item
        for item in (checklist or [])
        if isinstance(item, dict)
        and str(item.get("delegation_key") or item.get("key") or "") == item_key
    ]
    if len(matches) != 1:
        return None, f"Unknown or ambiguous checklist_item_key {item_key!r}."
    contract = deepcopy(matches[0])
    expected_service = str(contract.get("service_key") or "").strip()
    item_kind = str(contract.get("kind") or "").strip()
    if item_kind in {"llm", "subagent", "action"} and not expected_service:
        return None, f"Checklist item {item_key!r} has no delegatable service."
    if service_key and expected_service != service_key:
        return None, (
            f"Checklist item {item_key!r} belongs to service {expected_service!r}, "
            f"not {service_key!r}."
        )
    return contract, None


def _stable_child_step_key(parent_key: str, item_key: str) -> str:
    return coordinator_child_step_key(parent_key, item_key)


def _child_idempotency_key(
    plan_id: str,
    parent_key: str,
    item_key: str,
    generation: int = 0,
) -> str:
    return coordinator_child_idempotency_key(
        plan_id,
        parent_key,
        item_key,
        generation,
    )


def _effect_generation(params: Any) -> int:
    try:
        return max(
            0,
            int(
                (params if isinstance(params, dict) else {}).get(
                    ACTION_EFFECT_GENERATION_PARAM_KEY
                )
                or 0
            ),
        )
    except (TypeError, ValueError):
        return 0


def _effective_output_schema(contract: dict[str, Any]) -> dict[str, Any] | None:
    schema = contract.get("expected_output_schema")
    if isinstance(schema, dict):
        return deepcopy(schema)
    shape = str(contract.get("output_shape") or "").strip()
    if not shape:
        return None
    try:
        from packages.core.contracts.shapes import get_shape

        return deepcopy(get_shape(shape).json_schema())
    except (KeyError, TypeError, ValueError):
        logger.warning("coordinator child has unknown output shape %r", shape)
        return None


def _max_attempts(contract: dict[str, Any]) -> int:
    try:
        return max(1, min(10, int(contract.get("max_attempts") or 1)))
    except (TypeError, ValueError):
        return 1


def _merge_evidence_refs(
    existing: Any,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append typed receipts once while preserving their first-seen order."""

    from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory

    return ToolEffectEvidenceFactory.merge_untrusted(existing, incoming)


def _reconciled_not_applied_effect_digests(evidence_refs: Any) -> set[str]:
    from packages.core.dispatcher.effect_safety import (
        EFFECT_NOT_APPLIED_DECISION,
        TOOL_EFFECT_RECONCILIATION_KIND,
    )

    return {
        str((ref.get("fields") or {}).get("effect_digest") or "")
        for ref in (evidence_refs or [])
        if (
            isinstance(ref, dict)
            and ref.get("kind") == TOOL_EFFECT_RECONCILIATION_KIND
            and isinstance(ref.get("fields"), dict)
            and (ref.get("fields") or {}).get("decision")
            == EFFECT_NOT_APPLIED_DECISION
        )
    }


def _has_external_effect(evidence_refs: Any) -> bool:
    from packages.core.dispatcher.effect_safety import canonical_effect_digest

    reconciled = _reconciled_not_applied_effect_digests(evidence_refs)
    return any(
        isinstance(ref, dict)
        and ref.get("kind") == "tool_effect"
        and canonical_effect_digest(ref) not in reconciled
        for ref in (evidence_refs or [])
    )


def _has_unsettled_effect_intent(evidence_refs: Any) -> bool:
    from packages.core.dispatcher.effect_safety import canonical_effect_digest

    reconciled = _reconciled_not_applied_effect_digests(evidence_refs)
    return any(
        isinstance(ref, dict)
        and ref.get("kind") == "tool_effect"
        and ref.get("effect") == "pending"
        and canonical_effect_digest(ref) not in reconciled
        for ref in (evidence_refs or [])
    )


def _has_settled_effect_with_dropped_result(
    checkpoint: Any,
    evidence_refs: Any,
) -> bool:
    """Whether transcript compaction erased a settled effect's postcondition.

    A settled receipt proves that the effect happened, but the resumed model
    must also see the result that followed its exact tool call.  Otherwise it
    can reasonably interpret the budget marker as an unknown outcome and
    issue the same write again.  Legacy receipts without a call id fail closed
    whenever any tool result was dropped because they cannot be correlated.
    """

    dropped_call_ids = worker_transcript_dropped_tool_result_ids(checkpoint)
    if not dropped_call_ids:
        return False
    from packages.core.dispatcher.effect_safety import canonical_effect_digest

    reconciled = _reconciled_not_applied_effect_digests(evidence_refs)
    for ref in evidence_refs or []:
        if (
            not isinstance(ref, dict)
            or ref.get("kind") != "tool_effect"
            or ref.get("effect") == "pending"
            or canonical_effect_digest(ref) in reconciled
        ):
            continue
        fields = ref.get("fields") if isinstance(ref.get("fields"), dict) else {}
        tool_call_id = str(fields.get("tool_call_id") or "").strip()
        if not tool_call_id or tool_call_id in dropped_call_ids:
            return True
    return False


def _has_checkpointed_approved_effect_result(
    checkpoint: Any,
    evidence_refs: Any,
    *,
    approval_request: Any,
    child: ExecutionStep,
    task_id: str,
    idempotency_key: str,
) -> bool:
    """Bind a post-approval transcript to one exact consumed Runtime grant.

    This is the only fast path past a ``CONSUMED`` nested request.  Every
    identity is server-owned or derived from the durable transcript; the
    model-visible approval token is never sufficient authority by itself.
    """

    request_id = str(getattr(approval_request, "id", "") or "").strip()
    expected_task_id = str(task_id or "").strip()
    expected_idempotency_key = str(idempotency_key or "").strip()
    service_key = str(child.service_key or "").strip()
    subscription_id = str(child.resolved_subscription_id or "").strip()
    if (
        getattr(approval_request, "consumed_at", None) is None
        or not all(
            (
                request_id,
                expected_task_id,
                expected_idempotency_key,
                str(child.id or "").strip(),
                str(child.plan_id or "").strip(),
                service_key,
                subscription_id,
            )
        )
        or not is_worker_transcript_checkpoint(checkpoint)
    ):
        return False
    context = (
        approval_request.context
        if isinstance(getattr(approval_request, "context", None), dict)
        else {}
    )
    continuation = (
        context.get("continuation")
        if isinstance(context.get("continuation"), dict)
        else {}
    )
    resume = (
        context.get("delegation_resume")
        if isinstance(context.get("delegation_resume"), dict)
        else {}
    )
    tool_name = str(context.get("tool") or "").strip()
    tool_arguments = continuation.get("arguments")
    if (
        continuation.get("version") != 1
        or resume.get("version") != 1
        or not tool_name
        or str(continuation.get("tool") or "").strip() != tool_name
        or str(resume.get("tool_name") or "").strip() != tool_name
        or not isinstance(tool_arguments, dict)
        or str(resume.get("approval_token") or "").strip() != request_id
        or str(getattr(approval_request, "origin_step_id", "") or "").strip()
        != child.id
        or str(getattr(approval_request, "origin_task_id", "") or "").strip()
        != expected_task_id
        or str(resume.get("step_id") or "").strip() != child.id
        or str(resume.get("task_id") or "").strip() != expected_task_id
        or str(resume.get("service_key") or "").strip() != service_key
        or str(resume.get("agent_subscription_id") or "").strip()
        != subscription_id
        or str(resume.get("delegation_idempotency_key") or "").strip()
        != expected_idempotency_key
    ):
        return False

    from packages.core.ai.runtime.workspace_delegation_actions import (
        _tool_arguments_hash,
    )

    expected_arguments_hash = _tool_arguments_hash(tool_arguments)
    calls: dict[str, tuple[str, str]] = {}
    results: dict[str, str] = {}
    for message in checkpoint.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "").strip()
                function = (
                    call.get("function")
                    if isinstance(call.get("function"), dict)
                    else {}
                )
                call_name = str(function.get("name") or "").strip()
                raw_arguments = function.get("arguments")
                try:
                    call_arguments = (
                        json.loads(raw_arguments)
                        if isinstance(raw_arguments, str)
                        else dict(raw_arguments or {})
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    return False
                if (
                    not call_id
                    or not call_name
                    or not isinstance(call_arguments, dict)
                    or call_id in calls
                ):
                    return False
                calls[call_id] = (
                    call_name,
                    _tool_arguments_hash(call_arguments),
                )
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            content = message.get("content")
            if (
                not call_id
                or not isinstance(content, str)
                or not content
                or content
                == "[tool result dropped to fit the retry checkpoint budget]"
                or call_id in results
            ):
                return False
            results[call_id] = sha256(content.encode("utf-8")).hexdigest()

    matches: list[dict[str, Any]] = []
    for ref in evidence_refs or []:
        if (
            not isinstance(ref, dict)
            or ref.get("kind") != "tool_effect"
            or ref.get("effect") not in {"action", "publish"}
            or ref.get("coordinator_child_step_id") != child.id
            or not isinstance(ref.get("fields"), dict)
        ):
            continue
        fields = ref["fields"]
        call_id = str(fields.get("tool_call_id") or "").strip()
        result_sha256 = str(fields.get("result_sha256") or "").strip()
        declared_call = calls.get(call_id)
        if (
            str(fields.get("approval_request_id") or "").strip() != request_id
            or str(fields.get("owner_step_id") or "").strip() != child.id
            or str(fields.get("owner_plan_id") or "").strip() != child.plan_id
            or str(fields.get("owner_task_id") or "").strip()
            != expected_task_id
            or str(fields.get("owner_service_key") or "").strip() != service_key
            or str(fields.get("owner_subscription_id") or "").strip()
            != subscription_id
            or str(fields.get("owner_idempotency_key") or "").strip()
            != expected_idempotency_key
            or str(fields.get("tool") or "").strip() != tool_name
            or str(fields.get("arguments_hash") or "").strip()
            != expected_arguments_hash
            or fields.get("intent_state") != "returned"
            or declared_call != (tool_name, expected_arguments_hash)
            or not result_sha256
            or results.get(call_id) != result_sha256
        ):
            continue
        matches.append(ref)
    return len(matches) == 1


def _stored_child_response(child: ExecutionStep) -> str:
    child_params = child.params if isinstance(child.params, dict) else {}
    if child_params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is True:
        status = (
            "completed"
            if child.step_status == ExecutionStepStatus.DONE.value
            else "failed"
            if child.step_status in _TERMINAL_CHILD_STATUSES
            else "blocked"
        )
        return json.dumps({
            "delegated": True,
            "status": status,
            "checklist_item_key": child_params.get(
                COORDINATOR_CHILD_ITEM_PARAM_KEY
            ),
            "idempotency_key": child_params.get(
                COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY
            ),
            "service_key": child.service_key,
            "kind": child.kind,
            "result": deepcopy(child.result),
            "evidence_refs": deepcopy(child.evidence_refs or []),
            "cost": deepcopy(child.cost or {}),
            "error": deepcopy(child.error),
        }, ensure_ascii=False, default=str)
    if isinstance(child.result, dict):
        return json.dumps(child.result, ensure_ascii=False, default=str)
    if child.result is not None:
        return json.dumps({
            "delegated": True,
            "status": "completed" if child.step_status == ExecutionStepStatus.DONE else "failed",
            "result": child.result,
        }, ensure_ascii=False, default=str)
    error = child.error if isinstance(child.error, dict) else {}
    return json.dumps({
        "error": "delegation_not_replayable",
        "message": str(error.get("message") or "The prior delegation has no reusable result."),
        "status": child.step_status,
    }, ensure_ascii=False, default=str)


def _canonical_payload_hash(value: Any) -> str:
    return sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _stored_child_response_hash(child: ExecutionStep) -> str:
    """Hash the exact semantic JSON envelope replayed to the parent."""

    return _canonical_payload_hash(json.loads(_stored_child_response(child)))


def _tool_result_digest(value: str) -> str:
    """Match AgenticLoop's trusted truncation digest format."""

    return sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _set_child_response_digest(child: ExecutionStep, response: str) -> None:
    params = dict(child.params or {})
    params[COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY] = _tool_result_digest(
        response
    )
    child.params = params


def _clear_child_handoff_ack(child: ExecutionStep) -> None:
    params = dict(child.params or {})
    if COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY in params:
        params.pop(COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY, None)
        child.params = params


def _parent_checkpoint_digest(parent: ExecutionStep) -> str:
    """Identify the exact durable parent transcript that consumed a child.

    A child ACK without this binding survives a parent checkpoint reset.  The
    next parent attempt would then be allowed to submit without ever seeing
    the stored child result.  Only a real worker transcript is eligible; an
    absent, legacy, or malformed checkpoint deliberately invalidates every
    historical ACK and forces storage-backed replay.
    """

    checkpoint = parent.runtime_checkpoint
    if not is_worker_transcript_checkpoint(checkpoint):
        return ""
    return _canonical_payload_hash(checkpoint)


def _child_handoff_ack_matches(
    child: ExecutionStep,
    parent: ExecutionStep,
) -> bool:
    params = child.params if isinstance(child.params, dict) else {}
    ack = params.get(COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY)
    idempotency_key = str(
        params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or ""
    ).strip()
    parent_checkpoint_sha256 = _parent_checkpoint_digest(parent)
    return (
        child.step_status == ExecutionStepStatus.DONE.value
        and isinstance(ack, dict)
        and ack.get("version") == COORDINATOR_CHILD_HANDOFF_ACK_VERSION
        and str(ack.get("idempotency_key") or "") == idempotency_key
        and bool(idempotency_key)
        and str(ack.get("result_sha256") or "")
        == _stored_child_response_hash(child)
        and bool(parent_checkpoint_sha256)
        and str(ack.get("parent_checkpoint_sha256") or "")
        == parent_checkpoint_sha256
    )


def _tool_call_parts(tool_call: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return "", "", {}
    call_id = str(tool_call.get("id") or "").strip()
    function = tool_call.get("function")
    if isinstance(function, dict):
        name = str(function.get("name") or "").strip()
        raw_arguments = function.get("arguments")
    else:
        name = str(tool_call.get("name") or "").strip()
        raw_arguments = tool_call.get("arguments")
    if isinstance(raw_arguments, dict):
        arguments = dict(raw_arguments)
    elif isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except (TypeError, ValueError):
            parsed = None
        arguments = parsed if isinstance(parsed, dict) else {}
    else:
        arguments = {}
    return call_id, name, arguments


async def acknowledge_coordinator_child_handoffs(
    db: Any,
    parent: ExecutionStep,
    messages: list[dict[str, Any]],
) -> int:
    """ACK exact DONE-child results in the parent's transcript transaction.

    ``parent`` must already be locked after its live WorkLease. Child locks are
    then taken in stable step-key order, preserving the coordinator lock order.
    A result is acknowledged only when a Runtime tool message is paired with a
    ``delegate_to_service`` call for the same item/service and its JSON exactly
    matches the durable replay envelope.
    """

    parent_params = parent.params if isinstance(parent.params, dict) else {}
    if parent_params.get(COORDINATOR_PARAM_KEY) is not True:
        return 0
    parent_checkpoint_sha256 = _parent_checkpoint_digest(parent)
    if not parent_checkpoint_sha256:
        return 0

    calls: dict[str, dict[str, Any]] = {}
    tool_payloads: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                call_id, name, arguments = _tool_call_parts(tool_call)
                if call_id and name == DELEGATE_TO_SERVICE_TOOL_NAME:
                    calls[call_id] = arguments
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if call_id not in calls:
                continue
            raw_content = message.get("content")
            if isinstance(raw_content, str):
                try:
                    raw_content = json.loads(raw_content)
                except (TypeError, ValueError):
                    continue
            if isinstance(raw_content, dict):
                tool_payloads[call_id] = raw_content

    if not tool_payloads:
        return 0
    children = list((await db.execute(
        select(ExecutionStep)
        .where(
            ExecutionStep.plan_id == parent.plan_id,
            ExecutionStep.step_status == ExecutionStepStatus.DONE.value,
        )
        .order_by(ExecutionStep.step_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    by_item: dict[str, ExecutionStep] = {}
    for child in children:
        params = child.params if isinstance(child.params, dict) else {}
        item_key = str(params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or "").strip()
        if (
            params.get(COORDINATOR_CHILD_PARAM_KEY) is True
            and params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) == parent.step_key
            and item_key
            and str(params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or "").strip()
        ):
            by_item[item_key] = child

    acknowledged = 0
    for call_id, payload in tool_payloads.items():
        arguments = calls[call_id]
        item_key = str(arguments.get("checklist_item_key") or "").strip()
        service_key = str(arguments.get("service_key") or "").strip()
        child = by_item.get(item_key)
        if child is None or (
            service_key and str(child.service_key or "") != service_key
        ):
            continue
        payload_matches = (
            _canonical_payload_hash(payload) == _stored_child_response_hash(child)
        )
        truncation = payload.get("_tool_result_truncated")
        if not payload_matches and isinstance(truncation, dict):
            child_params = child.params if isinstance(child.params, dict) else {}
            payload_matches = (
                str(truncation.get("tool") or "")
                == DELEGATE_TO_SERVICE_TOOL_NAME
                and str(truncation.get("sha256") or "")
                == str(
                    child_params.get(
                        COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY
                    ) or ""
                )
                and bool(str(truncation.get("sha256") or ""))
            )
        if not payload_matches:
            continue
        params = dict(child.params or {})
        idempotency_key = str(
            params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or ""
        ).strip()
        ack = {
            "version": COORDINATOR_CHILD_HANDOFF_ACK_VERSION,
            "idempotency_key": idempotency_key,
            "result_sha256": _stored_child_response_hash(child),
            "parent_checkpoint_sha256": parent_checkpoint_sha256,
        }
        if params.get(COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY) != ack:
            params[COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY] = ack
            child.params = params
        acknowledged += 1
    return acknowledged


def _is_request_input_wait(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("delegated") is True
        and result.get("status") == "blocked"
        and result.get("blocker_kind") == "request_input"
        and isinstance(result.get("_pending_action"), dict)
    )


def _parent_input_snapshot(parent: ExecutionStep) -> dict[str, Any]:
    """Project the locked parent row exactly as the worker would snapshot it."""

    params = dict(parent.params or {})
    if isinstance(parent.human_input_response, dict):
        params["human_input_response"] = deepcopy(parent.human_input_response)
    return {"params": params}


def _input_context_matches(
    params: dict[str, Any],
    *,
    child_id: str,
    pending_tool_call_id: str,
) -> bool:
    context = params.get(COORDINATOR_INPUT_CONTEXT_PARAM_KEY)
    return (
        isinstance(context, dict)
        and context.get("version") == COORDINATOR_INPUT_CONTEXT_VERSION
        and str(context.get("child_step_id") or "") == child_id
        and str(context.get("pending_tool_call_id") or "")
        == pending_tool_call_id
    )


def _clear_consumed_parent_input(
    parent: ExecutionStep,
    *,
    child_id: str,
    pending_tool_call_id: str,
) -> None:
    """Consume only the answer bound to this exact child request."""

    params = dict(parent.params or {})
    if not _input_context_matches(
        params,
        child_id=child_id,
        pending_tool_call_id=pending_tool_call_id,
    ):
        return
    params.pop("answers", None)
    params.pop("human_input_response", None)
    params.pop(COORDINATOR_INPUT_CONTEXT_PARAM_KEY, None)
    parent.params = params
    parent.human_input_response = None


def _checkpoint_with_parent_answer(
    checkpoint_data: Any,
    parent_snapshot: dict[str, Any],
    *,
    child_id: str,
) -> dict[str, Any] | None:
    """Bind the outer step's persisted answer to the child's pending call."""

    if not isinstance(checkpoint_data, dict):
        return None
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
    from packages.core.ai.runtime.human_input import human_input_answer_result

    try:
        checkpoint = RuntimeAgentCheckpoint.from_dict(checkpoint_data)
    except (TypeError, ValueError):
        return None
    if str(checkpoint.pending_tool_call.get("name") or "") != "request_input":
        return None
    pending_tool_call_id = str(checkpoint.pending_tool_call.get("id") or "").strip()
    if not pending_tool_call_id:
        return None
    # Claiming a parked child persists the answered checkpoint before provider
    # I/O. If that process then dies, its successor can resume the exact same
    # pending call without depending on the parent snapshot still carrying the
    # answer.
    if isinstance(checkpoint.pending_tool_result, dict):
        return checkpoint.to_dict()
    params = (
        parent_snapshot.get("params")
        if isinstance(parent_snapshot.get("params"), dict)
        else {}
    )
    if not _input_context_matches(
        params,
        child_id=child_id,
        pending_tool_call_id=pending_tool_call_id,
    ):
        return None
    response = params.get("human_input_response")
    if not isinstance(response, dict):
        response = {}
    answers = params.get("answers") if isinstance(params.get("answers"), dict) else {}
    # These are replacement channels, never additive fragments.  Old rows that
    # somehow contain both fail closed rather than synthesizing a mixed answer.
    if response and answers:
        return None
    fields = response.get("fields") if isinstance(response.get("fields"), dict) else {}
    if answers:
        fields = dict(answers)
    answer = str(response.get("note") or response.get("response") or "").strip()
    if not answer and fields and len(fields) == 1:
        answer = str(next(iter(fields.values())) or "").strip()
    if not answer and not fields:
        return None
    checkpoint.pending_tool_result = {
        "id": pending_tool_call_id,
        "content": human_input_answer_result(
            answer=answer,
            fields=fields or None,
            choice=response.get("choice"),
            answered_by=response.get("user"),
        ),
    }
    return checkpoint.to_dict()


def _delegation_contract_failure_payload(
    result: Any,
    *,
    contract_error: str,
    item_key: str,
    idempotency_key: str,
    evidence_refs: Any,
) -> dict[str, Any]:
    """Turn a superficially-completed handoff into a retryable typed failure.

    Preserve the attempted result and every observed receipt. In particular, a
    missing publish receipt must not erase a different external effect and make
    an unsafe replay look clean.
    """

    if isinstance(result, dict) and result.get("delegated") is True:
        failure = deepcopy(result)
    else:
        failure = {
            "delegated": True,
            "result": deepcopy(result),
        }
    failure.update({
        "status": "failed",
        "error": "delegation_contract_unsatisfied",
        "message": contract_error,
        "checklist_item_key": item_key,
        "idempotency_key": idempotency_key,
        "retryable": True,
    })
    failure["evidence_refs"] = [
        deepcopy(ref) for ref in (evidence_refs or []) if isinstance(ref, dict)
    ]
    return failure


def _fail_child_delivery_contract(
    child: ExecutionStep,
    *,
    contract_error: str,
    item_key: str,
    idempotency_key: str,
    now: datetime,
) -> None:
    """Demote an invalid DONE child without discarding its replay evidence."""

    _clear_child_handoff_ack(child)
    child.result = _delegation_contract_failure_payload(
        child.result,
        contract_error=contract_error,
        item_key=item_key,
        idempotency_key=idempotency_key,
        evidence_refs=child.evidence_refs,
    )
    child.step_status = ExecutionStepStatus.FAILED.value
    child.error = {
        "type": "DelegationContractUnsatisfied",
        "message": contract_error,
        "retryable": True,
    }
    child.finished_at = now


@dataclass(frozen=True)
class _ChildDispatch:
    child_id: str
    prompt: str
    idempotency_key: str
    output_schema: dict[str, Any] | None
    initial_messages: list[dict[str, Any]] | None = None
    initial_usage: dict[str, Any] | None = None
    initial_rounds: int = 0
    initial_tool_calls_made: list[str] | None = None
    runtime_checkpoint: dict[str, Any] | None = None
    resolved_subscription_id: str | None = None
    resolved_agent_id: str | None = None
    approval_token: str | None = None
    reused_response: str | None = None


async def _insert_or_resume_child_step(
    s: dict[str, Any],
    *,
    contract: dict[str, Any],
    item_key: str,
    service_key: str,
    prompt: str,
) -> _ChildDispatch:
    """Create or claim the one durable child for a source checklist item.

    The parent row lock serializes duplicate calls. A terminal child is always
    replayed from storage. A nonterminal child can resume only from a durable
    transcript (or its exact approval token); an old RUNNING row without a
    checkpoint is ambiguous and is failed closed instead of repeating effects.
    """

    parent_key = str(s.get("step_key") or "")
    plan_id = str(s.get("plan_id") or "")
    child_key = _stable_child_step_key(parent_key, item_key)
    idempotency_key = _child_idempotency_key(plan_id, parent_key, item_key)
    output_schema = _effective_output_schema(contract)
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        parent_lease_id = str(s.get("lease_id") or "").strip()
        parent_execution_claim_id = str(s.get("execution_claim_id") or "").strip()
        # Approval resume must join the human-decision lock order before taking
        # the live parent lease.  This preview identifies only the stable child
        # and its nested request; every value is re-read after the canonical
        # request -> Task -> Plan -> Lease -> Step locks below.
        approval_nested_request_id: str | None = None
        child_preview = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == child_key,
            )
        )).scalar_one_or_none()
        if child_preview is not None and isinstance(child_preview.result, dict):
            approval_nested_request_id = str(
                child_preview.result.get("approval_token") or ""
            ).strip() or None
        approval_lock_request_id = await _coordinator_approval_lock_request_id(
            db,
            nested_request_id=approval_nested_request_id,
            entity_id=str(s.get("entity_id") or "") or None,
            workspace_id=str(s.get("workspace_id") or "") or None,
        )
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=str(s.get("step_id") or "").strip(),
            parent_lease_id=parent_lease_id,
            parent_execution_claim_id=parent_execution_claim_id,
            expected_plan_id=plan_id,
            expected_step_key=parent_key,
            approval_lock_request_id=approval_lock_request_id,
            approval_nested_request_id=approval_nested_request_id,
            expected_task_id=str(s.get("task_id") or "") or None,
            expected_entity_id=str(s.get("entity_id") or "") or None,
            expected_workspace_id=str(s.get("workspace_id") or "") or None,
        )
        if locked is None:
            raise RuntimeError(
                "Coordinator parent no longer owns a live execution claim."
            )
        lease, parent = locked
        params = s.get("params") if isinstance(s.get("params"), dict) else {}
        checklist = [
            item
            for item in (params.get("checklist") or [])
            if isinstance(item, dict)
        ]
        # Pre-stable-identity releases created numbered children with only a
        # parent marker. They cannot be proven to represent this exact
        # checklist item, and may already have performed an external action.
        # Never create a new stable child beside one: require reconciliation
        # or a fresh plan instead of risking duplicate provider I/O.
        legacy_children = []
        existing_rows = list((await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.plan_id == plan_id)
            .order_by(ExecutionStep.step_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        for existing in existing_rows:
            existing_params = (
                existing.params if isinstance(existing.params, dict) else {}
            )
            if (
                existing_params.get(COORDINATOR_CHILD_PARAM_KEY) is True
                and existing_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY)
                == parent_key
                and (
                    not str(
                        existing_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or ""
                    ).strip()
                    or not str(
                        existing_params.get(
                            COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY
                        ) or ""
                    ).strip()
                )
            ):
                legacy_children.append(existing.step_key)
        if legacy_children:
            raise RuntimeError(
                "A legacy coordinator child cannot be safely attributed to "
                "this checklist item; reconcile or replan before delegating "
                "again (" + ", ".join(legacy_children) + ")."
            )
        dependencies = [
            str(key).strip()
            for key in (contract.get("depends_on") or [])
            if str(key or "").strip()
        ]
        dependency_rows: list[tuple[str, str]] = []
        authoritative_dependencies: list[dict[str, Any]] = []
        for dependency_key in dependencies:
            dependency_contracts = [
                item
                for item in checklist
                if str(item.get("delegation_key") or item.get("key") or "")
                == dependency_key
            ]
            if len(dependency_contracts) != 1:
                raise RuntimeError(
                    f"Checklist dependency {dependency_key!r} is missing or ambiguous."
                )
            dependency_rows.append((
                dependency_key,
                _stable_child_step_key(parent_key, dependency_key),
            ))
        for dependency_key, dependency_child_key in sorted(
            dependency_rows,
            key=lambda item: item[1],
        ):
            dependency_child = (await db.execute(
                select(ExecutionStep)
                .where(
                    ExecutionStep.plan_id == plan_id,
                    ExecutionStep.step_key == dependency_child_key,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if (
                dependency_child is None
                or dependency_child.step_status != ExecutionStepStatus.DONE.value
            ):
                status = (
                    dependency_child.step_status
                    if dependency_child is not None
                    else "not_started"
                )
                raise RuntimeError(
                    f"Checklist dependency {dependency_key!r} is {status}; "
                    "it must complete before this item can start."
                )
            dependency_contract = dependency_contracts[0]
            dependency_contract_error = _delegation_contract_error(
                dependency_contract,
                result=dependency_child.result,
                evidence_refs=dependency_child.evidence_refs,
            )
            if dependency_contract_error:
                _fail_child_delivery_contract(
                    dependency_child,
                    contract_error=dependency_contract_error,
                    item_key=dependency_key,
                    idempotency_key=_child_idempotency_key(
                        plan_id,
                        parent_key,
                        dependency_key,
                        _effect_generation(
                            dependency_child.params
                            if isinstance(dependency_child.params, dict)
                            else {}
                        ),
                    ),
                    now=now,
                )
                # Commit the fail-closed demotion before returning the error.
                # Otherwise the context-manager rollback would leave the bad
                # DONE row able to unlock this child on the next call.
                await db.commit()
                raise RuntimeError(
                    f"Checklist dependency {dependency_key!r} no longer satisfies "
                    f"its delivery contract: {dependency_contract_error}."
                )
            authoritative_dependencies.append({
                "checklist_item_key": dependency_key,
                "result": deepcopy(dependency_child.result),
                "evidence_refs": deepcopy(dependency_child.evidence_refs or []),
            })
        if authoritative_dependencies:
            # The coordinator's prose is not the data plane. Append the exact
            # persisted dependency envelopes under the row locks that proved
            # they are DONE, so a parent model cannot omit or rewrite upstream
            # output while delegating the dependent item.
            prompt = (
                f"{prompt}\n\n"
                "Authoritative dependency results (server supplied; use these "
                "values instead of any conflicting paraphrase above):\n"
                + json.dumps(
                    authoritative_dependencies,
                    ensure_ascii=False,
                    default=str,
                )
            )
        child = (await db.execute(
            select(ExecutionStep)
            .where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == child_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if child is not None:
            child_params = child.params if isinstance(child.params, dict) else {}
            idempotency_key = _child_idempotency_key(
                plan_id,
                parent_key,
                item_key,
                _effect_generation(child_params),
            )
            if (
                child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
                or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent_key
                or child_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) != item_key
                or child_params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY)
                != idempotency_key
                or child.service_key != service_key
            ):
                raise RuntimeError(
                    f"Stable coordinator child key {child_key!r} belongs to a different contract."
                )
            if child.step_status == ExecutionStepStatus.DONE.value:
                contract_error = _delegation_contract_error(
                    contract,
                    result=child.result,
                    evidence_refs=child.evidence_refs,
                )
                if contract_error is None:
                    response = _stored_child_response(child)
                    _set_child_response_digest(child, response)
                    await db.commit()
                    return _ChildDispatch(
                        child_id=child.id,
                        prompt=str(child_params.get("prompt") or prompt),
                        idempotency_key=idempotency_key,
                        output_schema=output_schema,
                        reused_response=response,
                    )
                # Treat old/corrupt DONE rows exactly like a fresh invalid
                # handoff. A clean child can be repaired within its attempt
                # budget; captured effects remain protected by the FAILED
                # replay checks immediately below.
                _fail_child_delivery_contract(
                    child,
                    contract_error=contract_error,
                    item_key=item_key,
                    idempotency_key=idempotency_key,
                    now=now,
                )

            if child.step_status in {
                ExecutionStepStatus.SKIPPED.value,
                ExecutionStepStatus.CANCELLED.value,
            }:
                return _ChildDispatch(
                    child_id=child.id,
                    prompt=str(child_params.get("prompt") or prompt),
                    idempotency_key=idempotency_key,
                    output_schema=output_schema,
                    reused_response=_stored_child_response(child),
                )

            if child.step_status == ExecutionStepStatus.FAILED.value:
                if (
                    _has_unsettled_effect_intent(child.evidence_refs)
                    or _has_settled_effect_with_dropped_result(
                        child.runtime_checkpoint,
                        child.evidence_refs,
                    )
                    or (
                        _has_external_effect(child.evidence_refs)
                        and not is_worker_transcript_checkpoint(
                            child.runtime_checkpoint
                        )
                    )
                    or int(child.attempt_count or 0) >= int(child.max_attempts or 1)
                ):
                    return _ChildDispatch(
                        child_id=child.id,
                        prompt=str(child_params.get("prompt") or prompt),
                        idempotency_key=idempotency_key,
                        output_schema=output_schema,
                        reused_response=_stored_child_response(child),
                    )
                prior_error = child.error if isinstance(child.error, dict) else {}
                prompt = (
                    f"{str(child_params.get('prompt') or prompt)}\n\n"
                    "The prior delegated attempt failed without a captured external "
                    "effect. Continue from its durable transcript when available and "
                    "repair this failure before submitting:\n"
                    + json.dumps(prior_error, ensure_ascii=False, default=str)
                )

            checkpoint = child.runtime_checkpoint
            effect_result_was_dropped = _has_settled_effect_with_dropped_result(
                checkpoint,
                child.evidence_refs,
            )
            if (
                child.step_status
                in {
                    ExecutionStepStatus.RUNNING.value,
                    ExecutionStepStatus.WAITING_HUMAN.value,
                    ExecutionStepStatus.PAUSED.value,
                }
                and (
                    _has_unsettled_effect_intent(child.evidence_refs)
                    or effect_result_was_dropped
                )
            ):
                # A prior process durably observed an external effect but did
                # not preserve enough replay authority to continue safely.
                # The transcript is useful evidence, not authority to ask a
                # new model turn whether to repeat the action. End this child
                # and surface the receipts through the parent so Dispatcher
                # parks the lease for reconciliation.
                if effect_result_was_dropped:
                    message = (
                        "The delegated run completed an external action, but "
                        "its exact tool result was dropped from the durable "
                        "retry transcript. Reconcile the recorded receipt "
                        "before attempting the action again."
                    )
                else:
                    message = (
                        "The delegated run recorded an external action before "
                        "it was interrupted. Reconcile the recorded receipt "
                        "before attempting the action again."
                    )
                failure = {
                    "delegated": True,
                    "status": "failed",
                    "error": "delegation_effect_requires_reconciliation",
                    "message": message,
                    "result": child.result,
                    "evidence_refs": list(child.evidence_refs or []),
                    "checklist_item_key": item_key,
                    "idempotency_key": idempotency_key,
                    "stop_parent": True,
                    "terminal_failure": True,
                    "retryable": False,
                }
                child.step_status = ExecutionStepStatus.FAILED.value
                child.result = failure
                child.error = {
                    "type": "DelegatedEffectRequiresReconciliation",
                    "message": failure["message"],
                }
                child.finished_at = now
                await db.commit()
                return _ChildDispatch(
                    child_id=child.id,
                    prompt=str(child_params.get("prompt") or prompt),
                    idempotency_key=idempotency_key,
                    output_schema=output_schema,
                    reused_response=json.dumps(failure, ensure_ascii=False),
                )

            waiting = child.step_status in {
                ExecutionStepStatus.WAITING_HUMAN.value,
                ExecutionStepStatus.PAUSED.value,
            }
            child_result = child.result if isinstance(child.result, dict) else {}
            stored_token = str(child_result.get("approval_token") or "").strip()
            if stored_token and stored_token != str(
                approval_nested_request_id or ""
            ):
                # The discovery value changed before its serialization request
                # was locked.  Retry from a fresh snapshot instead of locking a
                # new request after already owning Lease/Step rows.
                raise RuntimeError(
                    "Coordinator child approval continuation changed while locking."
                )
            approval_resume_running = (
                child.step_status == ExecutionStepStatus.RUNNING.value
                and bool(stored_token)
            )
            effective_approval_token = None
            runtime_resume_checkpoint: dict[str, Any] | None = None
            request_input_resume = _is_request_input_wait(child_result)
            operator_effect_resume = False
            request_input_call_id: str | None = None
            from packages.core.ai.runtime.control import RuntimeAgentCheckpoint

            if isinstance(checkpoint, dict) and not request_input_resume:
                try:
                    reconciled_checkpoint = RuntimeAgentCheckpoint.from_dict(checkpoint)
                except (TypeError, ValueError):
                    reconciled_checkpoint = None
                if (
                    reconciled_checkpoint is not None
                    and (
                        isinstance(reconciled_checkpoint.pending_tool_result, dict)
                        or _effect_generation(child_params) > 0
                    )
                ):
                    runtime_resume_checkpoint = reconciled_checkpoint.to_dict()
                    operator_effect_resume = True
            if request_input_resume:

                try:
                    parked = RuntimeAgentCheckpoint.from_dict(checkpoint)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "Coordinator child has no exact parked input continuation."
                    ) from exc
                if str(parked.pending_tool_call.get("name") or "") != "request_input":
                    raise RuntimeError(
                        "Coordinator child input continuation does not own request_input."
                    )
                request_input_call_id = str(
                    parked.pending_tool_call.get("id") or ""
                ).strip()
                if not request_input_call_id:
                    raise RuntimeError(
                        "Coordinator child input continuation has no stable call id."
                    )
                runtime_resume_checkpoint = _checkpoint_with_parent_answer(
                    checkpoint,
                    _parent_input_snapshot(parent),
                    child_id=child.id,
                )
                if runtime_resume_checkpoint is None:
                    # The child remains parked until the outer structured input
                    # card has written an answer onto the parent step.
                    return _ChildDispatch(
                        child_id=child.id,
                        prompt=str(child_params.get("prompt") or prompt),
                        idempotency_key=idempotency_key,
                        output_schema=output_schema,
                        reused_response=_stored_child_response(child),
                    )
            elif waiting or approval_resume_running:
                if not stored_token:
                    raise RuntimeError(
                        "Coordinator child has no exact parked approval continuation."
                    )
                from packages.core.constants.approvals import (
                    ApprovalOriginKind,
                    ApprovalStatus,
                )
                from packages.core.models.hitl_request import HitlRequest

                nested = (await db.execute(
                    select(HitlRequest).where(
                        HitlRequest.id == stored_token,
                        HitlRequest.entity_id == child.entity_id,
                        HitlRequest.workspace_id == child.workspace_id,
                        HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL.value,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one_or_none()
                if (
                    nested is not None
                    and not _coordinator_nested_request_matches_execution(
                        nested,
                        child=child,
                        parent=parent,
                        lease=lease,
                        task_id=str(s.get("task_id") or ""),
                        idempotency_key=idempotency_key,
                        outer_request_id=approval_lock_request_id,
                    )
                ):
                    raise RuntimeError(
                        "Coordinator child approval lost its exact execution binding."
                    )
                checkpointed_approved_result = bool(
                    approval_resume_running
                    and nested is not None
                    and nested.status == ApprovalStatus.CONSUMED.value
                    and _has_checkpointed_approved_effect_result(
                        checkpoint,
                        child.evidence_refs,
                        approval_request=nested,
                        child=child,
                        task_id=str(s.get("task_id") or ""),
                        idempotency_key=idempotency_key,
                    )
                )
                if nested is None or nested.status == ApprovalStatus.PENDING.value:
                    if approval_resume_running:
                        raise RuntimeError(
                            "Coordinator child resumed before its exact approval was granted."
                        )
                    return _ChildDispatch(
                        child_id=child.id,
                        prompt=str(child_params.get("prompt") or prompt),
                        idempotency_key=idempotency_key,
                        output_schema=output_schema,
                        reused_response=_stored_child_response(child),
                    )
                if (
                    not checkpointed_approved_result
                    and (
                        nested.status != ApprovalStatus.GRANTED.value
                        or nested.consumed_at is not None
                    )
                ):
                    consumed_without_bound_result = (
                        nested.status == ApprovalStatus.CONSUMED.value
                        or nested.consumed_at is not None
                    )
                    failure = {
                        "delegated": True,
                        "status": "failed",
                        "error": (
                            "delegated_approval_result_unbound"
                            if consumed_without_bound_result
                            else "delegated_approval_rejected"
                        ),
                        "message": (
                            "The delegated approval was consumed, but its exact "
                            "child, Plan, Task, service, idempotency key, tool, "
                            "arguments, call, and result receipt could not be "
                            "verified. The action will not be repeated "
                            "automatically."
                            if consumed_without_bound_result
                            else "The exact delegated action was denied or expired."
                        ),
                        "approval_token": stored_token,
                        "evidence_refs": list(child.evidence_refs or []),
                        "stop_parent": consumed_without_bound_result,
                        "retryable": False,
                    }
                    child.step_status = ExecutionStepStatus.FAILED.value
                    child.result = failure
                    child.error = {
                        "type": (
                            "DelegatedApprovalResultUnbound"
                            if consumed_without_bound_result
                            else "DelegatedApprovalRejected"
                        ),
                        "message": failure["message"],
                    }
                    child.finished_at = now
                    if not consumed_without_bound_result:
                        child.runtime_checkpoint = None
                    await db.commit()
                    return _ChildDispatch(
                        child_id=child.id,
                        prompt=str(child_params.get("prompt") or prompt),
                        idempotency_key=idempotency_key,
                        output_schema=output_schema,
                        reused_response=json.dumps(failure, ensure_ascii=False),
                    )
                # The canonical bridge granted this exact Runtime request. The
                # coordinator need not copy a secret token from model prose;
                # the durable child identity supplies it.
                if not checkpointed_approved_result:
                    effective_approval_token = stored_token
            if (
                child.step_status == ExecutionStepStatus.RUNNING.value
                and runtime_resume_checkpoint is None
                and not is_worker_transcript_checkpoint(checkpoint)
            ):
                failure = {
                    "error": "delegation_state_ambiguous",
                    "message": (
                        "The prior delegation stopped while running without a durable "
                        "checkpoint. External effects may already have happened, so it "
                        "will not be repeated automatically."
                    ),
                    "status": "failed",
                    "checklist_item_key": item_key,
                    "idempotency_key": idempotency_key,
                }
                child.step_status = ExecutionStepStatus.FAILED.value
                child.result = failure
                child.error = {
                    "type": "AmbiguousDelegationState",
                    "message": failure["message"],
                }
                child.finished_at = now
                await db.commit()
                return _ChildDispatch(
                    child_id=child.id,
                    prompt=str(child_params.get("prompt") or prompt),
                    idempotency_key=idempotency_key,
                    output_schema=output_schema,
                    reused_response=json.dumps(failure, ensure_ascii=False),
                )

            if (
                is_worker_transcript_checkpoint(checkpoint)
                and checkpoint.get("terminal_failure") == "tool_outcome_unconfirmed"
            ):
                failure = {
                    "delegated": True,
                    "status": "failed",
                    "error": "delegated_tool_outcome_unconfirmed",
                    "message": (
                        "A delegated tool reported an error, but its action may "
                        "have happened. Check the external result before retrying."
                    ),
                    "checklist_item_key": item_key,
                    "idempotency_key": idempotency_key,
                    "stop_parent": True,
                    "retryable": False,
                }
                child.step_status = ExecutionStepStatus.FAILED.value
                child.result = failure
                child.error = {
                    "type": "DelegatedToolOutcomeUnconfirmed",
                    "message": failure["message"],
                }
                child.finished_at = now
                await db.commit()
                return _ChildDispatch(
                    child_id=child.id,
                    prompt=str(child_params.get("prompt") or prompt),
                    idempotency_key=idempotency_key,
                    output_schema=output_schema,
                    reused_response=json.dumps(failure, ensure_ascii=False),
                )

            initial_messages = (
                worker_transcript_resume_messages(checkpoint)
                if is_worker_transcript_checkpoint(checkpoint)
                else None
            )
            if not (
                waiting
                or approval_resume_running
                or request_input_resume
                or operator_effect_resume
            ) and int(
                child.attempt_count or 0
            ) >= int(
                child.max_attempts or 1
            ):
                failure = {
                    "delegated": True,
                    "status": "failed",
                    "error": "delegation_attempts_exhausted",
                    "message": "The delegated checklist item exhausted its retry budget.",
                    "checklist_item_key": item_key,
                    "idempotency_key": idempotency_key,
                }
                child.step_status = ExecutionStepStatus.FAILED.value
                child.result = failure
                child.error = {
                    "type": "DelegationAttemptsExhausted",
                    "message": failure["message"],
                }
                child.finished_at = now
                await db.commit()
                return _ChildDispatch(
                    child_id=child.id,
                    prompt=str(child_params.get("prompt") or prompt),
                    idempotency_key=idempotency_key,
                    output_schema=output_schema,
                    reused_response=json.dumps(failure, ensure_ascii=False),
                )
            child.step_status = ExecutionStepStatus.RUNNING.value
            child.error = None
            child.finished_at = None
            if runtime_resume_checkpoint is not None:
                # Persist the answer injection in the same fenced transaction
                # that claims the child. A crash before the inner loop starts
                # is therefore resumable without replaying request_input.
                child.runtime_checkpoint = runtime_resume_checkpoint
                if request_input_call_id:
                    _clear_consumed_parent_input(
                        parent,
                        child_id=child.id,
                        pending_tool_call_id=request_input_call_id,
                    )
            if not (
                waiting
                or approval_resume_running
                or request_input_resume
                or operator_effect_resume
            ):
                child.attempt_count = int(child.attempt_count or 0) + 1
            await db.commit()
            return _ChildDispatch(
                child_id=child.id,
                prompt=str(child_params.get("prompt") or prompt),
                idempotency_key=idempotency_key,
                output_schema=output_schema,
                initial_messages=initial_messages,
                initial_usage=(
                    dict(checkpoint.get("usage") or {})
                    if is_worker_transcript_checkpoint(checkpoint)
                    else None
                ),
                initial_rounds=(
                    max(0, int(checkpoint.get("rounds") or 0))
                    if is_worker_transcript_checkpoint(checkpoint)
                    else 0
                ),
                initial_tool_calls_made=(
                    [str(item) for item in (checkpoint.get("tool_calls_made") or [])]
                    if is_worker_transcript_checkpoint(checkpoint)
                    else None
                ),
                runtime_checkpoint=runtime_resume_checkpoint,
                resolved_subscription_id=child.resolved_subscription_id,
                resolved_agent_id=child.resolved_agent_id,
                approval_token=effective_approval_token,
            )

        child = ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=str(s.get("entity_id") or ""),
            workspace_id=s.get("workspace_id"),
            step_key=child_key,
            kind="subagent",
            service_key=service_key,
            provider=contract.get("provider"),
            action_key=contract.get("action_key"),
            capability_id=contract.get("capability_id"),
            integration_id=contract.get("integration_id"),
            params={
                "prompt": prompt,
                "description": str(contract.get("description") or f"Delegated to {service_key}"),
                "source_contract": deepcopy(contract),
                COORDINATOR_CHILD_PARAM_KEY: True,
                COORDINATOR_PARENT_STEP_PARAM_KEY: parent_key,
                COORDINATOR_CHILD_ITEM_PARAM_KEY: item_key,
                COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY: idempotency_key,
                ACTION_EFFECT_GENERATION_PARAM_KEY: 0,
            },
            expected_input_schema=deepcopy(contract.get("expected_input_schema")),
            expected_output_schema=output_schema,
            depends_on=[],
            step_status=ExecutionStepStatus.RUNNING.value,
            started_at=now,
            attempt_count=1,
            max_attempts=_max_attempts(contract),
            risk_level=str(contract.get("risk_level") or "low"),
            requires_approval=bool(contract.get("requires_approval")),
        )
        db.add(child)
        await db.commit()
        return _ChildDispatch(
            child_id=child.id,
            prompt=prompt,
            idempotency_key=idempotency_key,
            output_schema=output_schema,
        )


async def _settle_child_step(
    child_id: str,
    *,
    parent_step_id: str | None = None,
    parent_lease_id: str | None = None,
    parent_execution_claim_id: str | None = None,
    result: Any = None,
    error: dict[str, Any] | None = None,
    status: str = ExecutionStepStatus.DONE.value,
    evidence_refs: list[Any] | None = None,
    cost: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
    item_key: str = "",
    idempotency_key: str = "",
) -> "_ChildOutcome | None":
    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=parent_step_id,
            parent_lease_id=parent_lease_id,
            parent_execution_claim_id=parent_execution_claim_id,
        )
        if locked is None:
            logger.info(
                "not settling coordinator child %s: parent execution fence changed",
                child_id,
            )
            return None
        _, parent = locked
        child = (await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.id == child_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        child_params = (
            child.params
            if child is not None and isinstance(child.params, dict)
            else {}
        )
        if (
            child is None
            or (
                child.step_status != ExecutionStepStatus.RUNNING.value
                and not (
                    status == ExecutionStepStatus.WAITING_HUMAN.value
                    and child.step_status == ExecutionStepStatus.WAITING_HUMAN.value
                )
            )
            or child.plan_id != parent.plan_id
            or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
            or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent.step_key
        ):
            return None
        merged_evidence = [
            dict(item) for item in (child.evidence_refs or []) if isinstance(item, dict)
        ]
        if evidence_refs is not None:
            # A later HITL envelope may carry no receipts even though an
            # earlier round already performed an external action. Never erase
            # the checkpointed evidence while parking or settling the child.
            merged_evidence = _merge_evidence_refs(
                merged_evidence,
                [item for item in evidence_refs if isinstance(item, dict)],
            )
        durable_result = deepcopy(result)
        if isinstance(durable_result, dict) and durable_result.get("delegated") is True:
            # The round checkpoint may own authoritative receipts that the
            # final static transcript parser cannot rediscover (for example a
            # dynamically declared MCP write). Return and persist the complete
            # durable evidence set, not only the final envelope's subset.
            durable_result["evidence_refs"] = deepcopy(merged_evidence)
        if status == ExecutionStepStatus.DONE.value and contract is not None:
            contract_error = _delegation_contract_error(
                contract,
                result=durable_result,
                evidence_refs=merged_evidence,
            )
            if contract_error:
                durable_result = _delegation_contract_failure_payload(
                    durable_result,
                    contract_error=contract_error,
                    item_key=item_key,
                    idempotency_key=idempotency_key,
                    evidence_refs=merged_evidence,
                )
                error = {
                    "type": "DelegationContractUnsatisfied",
                    "message": contract_error,
                    "retryable": True,
                }
                status = ExecutionStepStatus.FAILED.value
        # Any newly-settled result invalidates proof that an older envelope
        # reached the parent transcript. The next parent round checkpoint will
        # ACK this exact result after its tool message is durable.
        _clear_child_handoff_ack(child)
        child.step_status = status
        child.result = durable_result
        child.error = dict(error) if error is not None else None
        child.evidence_refs = merged_evidence
        if cost is not None:
            child.cost = dict(cost)
        if status in _TERMINAL_CHILD_STATUSES:
            child.finished_at = datetime.now(timezone.utc)
            # A clean failed attempt can be continued within max_attempts. DONE
            # and hard terminal states no longer need their transcript; FAILED
            # keeps it unless effect evidence makes replay unsafe.
            if status != ExecutionStepStatus.FAILED.value:
                child.runtime_checkpoint = None
        else:
            child.finished_at = None
        _set_child_response_digest(child, _stored_child_response(child))
        await db.commit()
        return _ChildOutcome(
            result=deepcopy(child.result),
            error=deepcopy(child.error),
            status=child.step_status,
            evidence_refs=deepcopy(child.evidence_refs or []),
        )


async def _checkpoint_child_round(
    child_id: str,
    rounds: int,
    messages: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
    tool_calls_made: list[str] | None = None,
    terminal_failure: str | None = None,
    *,
    parent_step_id: str | None = None,
    parent_lease_id: str | None = None,
    parent_execution_claim_id: str | None = None,
    entity_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
) -> None:
    checkpoint = build_worker_transcript_checkpoint(
        messages,
        rounds=rounds,
        usage=usage,
        tool_calls_made=tool_calls_made,
        terminal_failure=terminal_failure,
    )
    if checkpoint is None:
        raise RuntimeError("completed delegated round did not produce a durable checkpoint")
    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=parent_step_id,
            parent_lease_id=parent_lease_id,
            parent_execution_claim_id=parent_execution_claim_id,
        )
        if locked is None:
            raise RuntimeError("coordinator parent execution fence changed")
        lease, parent = locked
        child = (await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.id == child_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        child_params = (
            child.params
            if child is not None and isinstance(child.params, dict)
            else {}
        )
        if (
            child is None
            or child.step_status != ExecutionStepStatus.RUNNING.value
            or child.plan_id != parent.plan_id
            or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
            or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent.step_key
        ):
            raise RuntimeError("coordinator child no longer belongs to the live parent")
        # Extract receipts before committing the checkpoint. If classification
        # itself fails, strict round persistence aborts the loop and leaves no
        # resumable checkpoint, so a successor fails closed as ambiguous.
        from packages.core.ai.runtime.workspace_delegation_actions import (
            _delegation_receipts,
        )

        round_evidence, _, _ = _delegation_receipts(
            messages,
            entity_id=str(entity_id or child.entity_id or ""),
            workspace_id=str(workspace_id or child.workspace_id or ""),
            task_id=task_id,
            authoritative_intents=[
                ref for ref in (child.evidence_refs or []) if isinstance(ref, dict)
            ],
        )
        round_evidence = [
            {**ref, "coordinator_child_step_id": child.id}
            for ref in round_evidence
            if isinstance(ref, dict)
        ]
        if round_evidence:
            # One lease->parent->child transaction makes the receipt and the
            # resumable transcript inseparable. Projecting to the parent and
            # lease lets expiry/failure see a nested effect before the delegate
            # tool has returned its final typed envelope.
            child.evidence_refs = _merge_evidence_refs(
                child.evidence_refs, round_evidence
            )
            parent.evidence_refs = _merge_evidence_refs(
                parent.evidence_refs, round_evidence
            )
            lease.evidence_refs = _merge_evidence_refs(
                lease.evidence_refs, round_evidence
            )
        if _has_unsettled_effect_intent(child.evidence_refs):
            # Keep the exact pre-I/O RuntimeAgentCheckpoint installed by the
            # intent barrier; a generic transcript would strand APPLIED and
            # NOT_APPLIED reconciliation after a handler/barrier error.
            await db.commit()
            raise RuntimeError(
                "delegated external action has no durable terminal receipt; "
                "manual reconciliation is required"
            )
        child.runtime_checkpoint = checkpoint
        # This cumulative watermark is the accounting source of truth for a
        # nested coordinator child. It is committed at the same durability
        # barrier as the transcript, so a crash after any completed inner
        # round cannot make those tokens disappear from the parent roll-up.
        child.cost = _child_cost({
            "usage": checkpoint.get("usage") or {},
            "rounds": checkpoint.get("rounds") or 0,
            "tool_calls_made": checkpoint.get("tool_calls_made") or [],
        })
        await db.commit()


async def _pin_child_service_identity(
    child_id: str,
    subscription_id: str,
    agent_id: str,
    *,
    parent_step_id: str | None,
    parent_lease_id: str | None,
    parent_execution_claim_id: str | None,
) -> None:
    """Persist the exact subscription/agent before child provider I/O."""

    normalized_subscription_id = str(subscription_id or "").strip()
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_subscription_id or not normalized_agent_id:
        raise RuntimeError("delegated service resolved without a stable identity")
    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=parent_step_id,
            parent_lease_id=parent_lease_id,
            parent_execution_claim_id=parent_execution_claim_id,
        )
        if locked is None:
            raise RuntimeError("coordinator parent execution fence changed")
        _, parent = locked
        child = (await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.id == child_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        child_params = (
            child.params
            if child is not None and isinstance(child.params, dict)
            else {}
        )
        if (
            child is None
            or child.step_status != ExecutionStepStatus.RUNNING.value
            or child.plan_id != parent.plan_id
            or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
            or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent.step_key
        ):
            raise RuntimeError("coordinator child no longer belongs to the live parent")
        if (
            child.resolved_subscription_id
            and child.resolved_subscription_id != normalized_subscription_id
        ) or (
            child.resolved_agent_id
            and child.resolved_agent_id != normalized_agent_id
        ):
            raise RuntimeError(
                "delegated service identity changed while resuming its transcript"
            )
        child.resolved_subscription_id = normalized_subscription_id
        child.resolved_agent_id = normalized_agent_id
        await db.commit()


async def _checkpoint_child_human_input(
    child_id: str,
    control: dict[str, Any],
    pause_payload: dict[str, Any],
    *,
    parent_step_id: str | None,
    parent_lease_id: str | None,
    parent_execution_claim_id: str | None,
) -> None:
    """Park an inner request_input checkpoint on its child, never its parent."""

    from packages.core.ai.runtime.control import (
        RuntimeAgentCheckpoint,
        is_human_input_suspension_control,
    )

    checkpoint_data = control.get("checkpoint") if isinstance(control, dict) else None
    if not is_human_input_suspension_control(control) or not isinstance(
        checkpoint_data, dict
    ):
        raise RuntimeError("delegated request_input has no Runtime checkpoint")
    try:
        checkpoint = RuntimeAgentCheckpoint.from_dict(checkpoint_data)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("delegated request_input checkpoint is unreadable") from exc
    if str(checkpoint.pending_tool_call.get("name") or "") != "request_input":
        raise RuntimeError("delegated input checkpoint does not own request_input")
    pending_tool_call_id = str(
        checkpoint.pending_tool_call.get("id") or ""
    ).strip()
    if not pending_tool_call_id:
        raise RuntimeError("delegated input checkpoint has no stable tool-call id")
    if not _is_request_input_wait(pause_payload):
        raise RuntimeError("delegated input pause has no typed pending action")

    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=parent_step_id,
            parent_lease_id=parent_lease_id,
            parent_execution_claim_id=parent_execution_claim_id,
        )
        if locked is None:
            raise RuntimeError("coordinator parent execution fence changed")
        _, parent = locked
        child = (await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.id == child_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        child_params = (
            child.params
            if child is not None and isinstance(child.params, dict)
            else {}
        )
        if (
            child is None
            or child.step_status != ExecutionStepStatus.RUNNING.value
            or child.plan_id != parent.plan_id
            or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
            or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent.step_key
        ):
            raise RuntimeError("coordinator child no longer belongs to the live parent")
        durable_checkpoint = checkpoint.to_dict()
        child.runtime_checkpoint = durable_checkpoint
        child.step_status = ExecutionStepStatus.WAITING_HUMAN.value
        child.result = deepcopy(pause_payload)
        child.error = None
        child.finished_at = None
        child.cost = _child_cost({
            "usage": durable_checkpoint.get("usage") or {},
            "rounds": durable_checkpoint.get("rounds") or 0,
            "tool_calls_made": durable_checkpoint.get("tool_calls_made") or [],
        })
        # Publish the pending-call identity and clear any previous answer in
        # the same fenced transaction as the child checkpoint.  This closes
        # both directions of the crash window: a successor can reconstruct the
        # exact outer card before an answer exists, while an answer from an
        # earlier request can never be consumed by this child.
        parent_params = dict(parent.params or {})
        parent_params.pop("answers", None)
        parent_params.pop("human_input_response", None)
        parent_params[COORDINATOR_INPUT_CONTEXT_PARAM_KEY] = {
            "version": COORDINATOR_INPUT_CONTEXT_VERSION,
            "child_step_id": child.id,
            "pending_tool_call_id": pending_tool_call_id,
        }
        parent.params = parent_params
        parent.human_input_response = None
        await db.commit()


async def _checkpoint_child_effect_intent(
    child_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_call_id: str | None,
    classification: Any | None = None,
    pending_checkpoint: dict[str, Any] | None = None,
    approval_request_id: str | None = None,
    *,
    parent_step_id: str | None,
    parent_lease_id: str | None,
    parent_execution_claim_id: str | None,
    entity_id: str,
    workspace_id: str,
    task_id: str | None,
) -> Any:
    """Persist a mutating/ambiguous tool intent before provider I/O starts."""

    from packages.core.ai.runtime.workspace_delegation_actions import (
        _delegation_effect_intent,
    )

    intent = _delegation_effect_intent(
        tool_name,
        arguments,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        tool_call_id=tool_call_id,
        classification=classification,
        approval_request_id=approval_request_id,
    )
    if intent is None:
        return
    intent = {**intent, "coordinator_child_step_id": child_id}
    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=parent_step_id,
            parent_lease_id=parent_lease_id,
            parent_execution_claim_id=parent_execution_claim_id,
        )
        if locked is None:
            raise RuntimeError("coordinator parent execution fence changed")
        lease, parent = locked
        child = (await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.id == child_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        child_params = (
            child.params
            if child is not None and isinstance(child.params, dict)
            else {}
        )
        if (
            child is None
            or child.step_status != ExecutionStepStatus.RUNNING.value
            or child.plan_id != parent.plan_id
            or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
            or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent.step_key
        ):
            raise RuntimeError("coordinator child no longer belongs to the live parent")
        intent_fields = (
            dict(intent.get("fields") or {})
            if isinstance(intent.get("fields"), dict)
            else {}
        )
        intent["fields"] = {
            **intent_fields,
            "effect_generation": _effect_generation(child_params),
            "owner_step_id": child.id,
            "owner_plan_id": child.plan_id,
            "owner_task_id": str(task_id or "").strip() or None,
            "owner_service_key": str(child.service_key or "").strip() or None,
            "owner_subscription_id": (
                str(child.resolved_subscription_id or "").strip() or None
            ),
            "owner_idempotency_key": (
                str(
                    child_params.get(
                        COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY
                    )
                    or ""
                ).strip()
                or None
            ),
        }
        from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory

        intent = ToolEffectEvidenceFactory.bind_effect_id(intent)
        if isinstance(pending_checkpoint, dict):
            child.runtime_checkpoint = deepcopy(pending_checkpoint)
        child.evidence_refs = _merge_evidence_refs(child.evidence_refs, [intent])
        parent.evidence_refs = _merge_evidence_refs(parent.evidence_refs, [intent])
        lease.evidence_refs = _merge_evidence_refs(lease.evidence_refs, [intent])
        await db.commit()
    return None


@dataclass(frozen=True)
class _ChildOutcome:
    result: dict[str, Any]
    error: dict[str, Any] | None
    status: str
    evidence_refs: list[Any]


def _child_outcome(raw: str) -> _ChildOutcome:
    """Durable child state derived from the delegate's typed JSON handoff."""
    hitl = parse_hitl_envelope(raw)
    if hitl is not None:
        return _ChildOutcome(
            result=dict(hitl),
            error=None,
            status=ExecutionStepStatus.WAITING_HUMAN.value,
            evidence_refs=[],
        )
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        error = {"type": "DelegationFailed", "message": "delegate returned a non-JSON result"}
        return _ChildOutcome(
            result={"error": "delegation_failed", "message": error["message"], "status": "failed"},
            error=error,
            status=ExecutionStepStatus.FAILED.value,
            evidence_refs=[],
        )
    if payload.get("delegated") is True:
        durable = dict(payload)
        durable.setdefault("result", {"text": str(payload.get("content") or "")})
        durable.setdefault("evidence_refs", [])
        durable.setdefault("artifacts", [])
        durable.setdefault("knowledge_refs", [])
        durable.setdefault("usage", {})
        durable.setdefault("stop_reason", None)
        durable.setdefault("error", None)
        run_status = str(durable.get("status") or "completed")
        evidence = list(durable.get("evidence_refs") or [])
        if run_status == "blocked":
            return _ChildOutcome(
                result=durable,
                error=None,
                status=ExecutionStepStatus.WAITING_HUMAN.value,
                evidence_refs=evidence,
            )
        if run_status != "completed" or durable.get("error"):
            message = str(durable.get("error") or durable.get("stop_reason") or "delegation failed")
            return _ChildOutcome(
                result=durable,
                error={"type": "DelegationFailed", "message": message},
                status=ExecutionStepStatus.FAILED.value,
                evidence_refs=evidence,
            )
        return _ChildOutcome(
            result=durable,
            error=None,
            status=ExecutionStepStatus.DONE.value,
            evidence_refs=evidence,
        )
    if payload.get("error"):
        message = str(payload.get("message") or payload.get("error"))
        return _ChildOutcome(
            result=payload,
            error={"type": "DelegationFailed", "message": message},
            status=ExecutionStepStatus.FAILED.value,
            evidence_refs=list(payload.get("evidence_refs") or []),
        )
    error = {"type": "DelegationFailed", "message": "delegate returned an unrecognised result"}
    return _ChildOutcome(
        result=payload,
        error=error,
        status=ExecutionStepStatus.FAILED.value,
        evidence_refs=[],
    )


def _child_cost(result: Any) -> dict[str, Any]:
    """Map the nested loop's accounting onto the durable child row.

    Runtime billing already charges the nested model calls exactly once. This
    is attribution/roll-up data only; it must not perform another settlement.
    """

    payload = result if isinstance(result, dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    mapped = {
        **cost,
        "llm_tokens_input": int(
            usage.get("prompt_tokens") or usage.get("prompt") or 0
        ),
        "llm_tokens_output": int(
            usage.get("completion_tokens") or usage.get("completion") or 0
        ),
        "llm_rounds": int(payload.get("rounds") or 0),
        "tool_call_count": len(payload.get("tool_calls_made") or []),
    }
    if usage.get("cost_usd") is not None:
        try:
            reported_usd = float(usage.get("cost_usd") or 0)
        except (TypeError, ValueError):
            reported_usd = -1.0
        if math.isfinite(reported_usd) and reported_usd >= 0:
            mapped.setdefault("usd", reported_usd)
            mapped.setdefault("llm_usd", reported_usd)
            # The inner Runtime call already settled provider billing.
            mapped.setdefault("budget_usd", 0.0)
    return mapped


async def coordinator_child_cost_totals(
    s: dict[str, Any],
) -> dict[str, int | float]:
    """Read one fenced, cumulative accounting snapshot for coordinator children.

    Child model calls are billed at their own Runtime boundary. This is only
    the durable Plan attribution that the parent step writes once on terminal
    completion; using child rows rather than an in-memory nested collector
    makes ordinary crash/resume paths lossless and prevents replay double-counts.
    """

    parent_key = str(s.get("step_key") or "").strip()
    plan_id = str(s.get("plan_id") or "").strip()
    if not parent_key or not plan_id:
        return {
            "llm_tokens_input": 0,
            "llm_tokens_output": 0,
            "llm_rounds": 0,
            "tool_call_count": 0,
            "llm_usd": 0.0,
            "unpriced_tokens_input": 0,
            "unpriced_tokens_output": 0,
        }
    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=str(s.get("step_id") or "") or None,
            parent_lease_id=str(s.get("lease_id") or "") or None,
            parent_execution_claim_id=(
                str(s.get("execution_claim_id") or "") or None
            ),
            expected_plan_id=plan_id,
            expected_step_key=parent_key,
        )
        if locked is None:
            raise RuntimeError("coordinator parent execution fence changed")
        rows = (await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.plan_id == plan_id)
            .order_by(ExecutionStep.step_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all()
        totals = {
            "llm_tokens_input": 0,
            "llm_tokens_output": 0,
            "llm_rounds": 0,
            "tool_call_count": 0,
            "llm_usd": 0.0,
            "unpriced_tokens_input": 0,
            "unpriced_tokens_output": 0,
        }
        for child in rows:
            child_params = child.params if isinstance(child.params, dict) else {}
            if (
                child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
                or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent_key
            ):
                continue
            cost = child.cost if isinstance(child.cost, dict) else {}
            for key in (
                "llm_tokens_input",
                "llm_tokens_output",
                "llm_rounds",
                "tool_call_count",
            ):
                try:
                    totals[key] += max(0, int(cost.get(key) or 0))
                except (TypeError, ValueError):
                    continue
            reported_usd = cost.get("llm_usd", cost.get("usd"))
            if reported_usd is not None:
                try:
                    parsed_usd = float(reported_usd)
                except (TypeError, ValueError):
                    parsed_usd = -1.0
                if math.isfinite(parsed_usd) and parsed_usd >= 0:
                    totals["llm_usd"] += parsed_usd
                    continue
            totals["unpriced_tokens_input"] += max(
                0,
                int(cost.get("llm_tokens_input") or 0),
            )
            totals["unpriced_tokens_output"] += max(
                0,
                int(cost.get("llm_tokens_output") or 0),
            )
        return totals


def _prompt_with_authoritative_contract(prompt: str, contract: dict[str, Any]) -> str:
    """Bind the delegate to persisted DAG inputs, not the coordinator's paraphrase."""

    authoritative = {
        key: deepcopy(contract.get(key))
        for key in (
            "key",
            "kind",
            "description",
            "params",
            "depends_on",
            "expects",
            "expected_input_schema",
            "expected_output_schema",
            "output_shape",
            "risk_level",
            "requires_approval",
            "provider",
            "action_key",
            "capability_id",
            "integration_id",
            "skill_refs",
        )
        if contract.get(key) is not None
    }
    return (
        f"{prompt}\n\n"
        "Authoritative persisted checklist contract (do not replace its provider, "
        "action, arguments, approval requirement, or output contract with a "
        "paraphrase):\n"
        f"{json.dumps(authoritative, ensure_ascii=False, default=str)}"
    )


def _typed_child_contract_matches(
    child: ExecutionStep,
    contract: dict[str, Any],
    *,
    parent_key: str,
    item_key: str,
    idempotency_key: str,
) -> bool:
    """Bind a pre-materialised child to the exact persisted source contract."""

    params = child.params if isinstance(child.params, dict) else {}
    source_contract = params.get("source_contract")
    return (
        params.get(COORDINATOR_CHILD_PARAM_KEY) is True
        and params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is True
        and str(params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) or "") == parent_key
        and str(params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or "") == item_key
        and str(params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or "")
        == idempotency_key
        and child.kind == str(contract.get("kind") or "")
        and str(child.service_key or "") == str(contract.get("service_key") or "")
        and str(child.provider or "") == str(contract.get("provider") or "")
        and str(child.action_key or "") == str(contract.get("action_key") or "")
        and str(child.capability_id or "")
        == str(contract.get("capability_id") or "")
        and str(child.integration_id or "")
        == str(contract.get("integration_id") or "")
        and source_contract == contract
    )


def _typed_child_sleep_deadline(
    params: dict[str, Any],
    *,
    now: datetime,
) -> datetime:
    if "until" in params:
        target = params.get("until")
        if isinstance(target, str):
            target = datetime.fromisoformat(target.replace("Z", "+00:00"))
        if not isinstance(target, datetime):
            raise ValueError("sleep until must be an ISO timestamp")
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return target.astimezone(timezone.utc)
    seconds = max(0.0, float(params.get("seconds") or 0))
    return now + timedelta(seconds=seconds)


async def _release_or_replay_typed_child(
    s: dict[str, Any],
    *,
    contract: dict[str, Any],
    item_key: str,
) -> str | RuntimeToolSuspension | None:
    """Release one typed child, or replay its exact durable terminal result.

    V1 coordinator plans do not pre-materialise typed children and return
    ``None`` so the legacy nested-chat path below remains rolling-upgrade safe.
    """

    parent_key = str(s.get("step_key") or "").strip()
    plan_id = str(s.get("plan_id") or "").strip()
    if not parent_key or not plan_id:
        raise RuntimeError("coordinator execution identity is incomplete")
    child_key = _stable_child_step_key(parent_key, item_key)
    idempotency_key = _child_idempotency_key(plan_id, parent_key, item_key)
    source_dependencies = [
        str(value).strip()
        for value in (contract.get("depends_on") or [])
        if str(value or "").strip()
    ]
    locked_step_keys = sorted({
        child_key,
        *(
            _stable_child_step_key(parent_key, dependency_key)
            for dependency_key in source_dependencies
        ),
    })
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=str(s.get("step_id") or "") or None,
            parent_lease_id=str(s.get("lease_id") or "") or None,
            parent_execution_claim_id=(
                str(s.get("execution_claim_id") or "") or None
            ),
            expected_plan_id=plan_id,
            expected_step_key=parent_key,
        )
        if locked is None:
            raise RuntimeError(
                "Coordinator parent no longer owns a live execution claim."
            )
        _, parent = locked
        rows = list((await db.execute(
            select(ExecutionStep)
            .where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key.in_(locked_step_keys),
            )
            .order_by(ExecutionStep.step_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        by_key = {row.step_key: row for row in rows}
        child = by_key.get(child_key)
        child_params = (
            child.params if child is not None and isinstance(child.params, dict)
            else {}
        )
        if child is None or child_params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is not True:
            parent_params = parent.params if isinstance(parent.params, dict) else {}
            if (
                parent_params.get(COORDINATOR_VERSION_PARAM_KEY)
                == COORDINATOR_TYPED_VERSION
            ):
                raise RuntimeError(
                    f"Typed coordinator child {child_key!r} is missing."
                )
            return None
        if not _typed_child_contract_matches(
            child,
            contract,
            parent_key=parent_key,
            item_key=item_key,
            idempotency_key=idempotency_key,
        ):
            raise RuntimeError(
                f"Typed coordinator child {child_key!r} does not match its source contract."
            )

        parent_params = dict(parent.params or {})
        waiting = parent_params.get(COORDINATOR_WAITING_CHILD_PARAM_KEY)
        if isinstance(waiting, dict):
            waiting_child_id = str(waiting.get("child_step_id") or "")
            if waiting_child_id and waiting_child_id != child.id:
                raise RuntimeError(
                    "Coordinator is already waiting for a different typed child."
                )

        if child.step_status in _TERMINAL_CHILD_STATUSES:
            response = _stored_child_response(child)
            _set_child_response_digest(child, response)
            parent_params.pop(COORDINATOR_WAITING_CHILD_PARAM_KEY, None)
            parent.params = parent_params
            await db.commit()
            return response

        prior_results: dict[str, Any] = {}
        for dependency_key in source_dependencies:
            dependency_child = by_key.get(
                _stable_child_step_key(parent_key, dependency_key)
            )
            if (
                dependency_child is None
                or dependency_child.step_status != ExecutionStepStatus.DONE.value
            ):
                status = (
                    dependency_child.step_status
                    if dependency_child is not None
                    else "not_started"
                )
                return json.dumps({
                    "error": "dependency_not_ready",
                    "checklist_item_key": item_key,
                    "dependency": dependency_key,
                    "status": status,
                })
            dependency_params = (
                dependency_child.params
                if isinstance(dependency_child.params, dict)
                else {}
            )
            dependency_contract = dependency_params.get("source_contract")
            if not isinstance(dependency_contract, dict):
                raise RuntimeError(
                    f"Typed dependency {dependency_key!r} has no source contract."
                )
            contract_error = _completed_child_contract_error(
                dependency_contract,
                dependency_child,
            )
            if contract_error:
                raise RuntimeError(
                    f"Typed dependency {dependency_key!r} is invalid: {contract_error}."
                )
            prior_results[dependency_key] = deepcopy(dependency_child.result)

        if child.step_status == ExecutionStepStatus.PAUSED.value and not bool(
            child_params.get(COORDINATOR_CHILD_RELEASED_PARAM_KEY)
        ):
            from packages.core.plans.refs import resolve_refs

            source_params = contract.get("params")
            source_params = deepcopy(source_params) if isinstance(source_params, dict) else {}
            expects = {
                str(value).strip().lower()
                for value in (contract.get("expects") or [])
                if str(value or "").strip()
            }
            resolved_params = resolve_refs(
                source_params,
                prior_results,
                strict=bool({"publish", "send"}.intersection(expects)),
            )
            runtime_params = {
                key: deepcopy(value)
                for key, value in child_params.items()
                if key == "source_contract" or str(key).startswith("_")
            }
            runtime_params[COORDINATOR_CHILD_RELEASED_PARAM_KEY] = True
            resolved_params.update(runtime_params)
            if child.kind == "sleep":
                resolved_params[COORDINATOR_CHILD_WAKE_AT_PARAM_KEY] = (
                    _typed_child_sleep_deadline(resolved_params, now=now).isoformat()
                )
            child.params = resolved_params
            child.step_status = ExecutionStepStatus.PENDING.value
            child.started_at = None
            child.finished_at = None
            child.error = None

        parent_params[COORDINATOR_WAITING_CHILD_PARAM_KEY] = {
            "version": 1,
            "child_step_id": child.id,
            "child_step_key": child.step_key,
            "checklist_item_key": item_key,
        }
        parent.params = parent_params
        await db.commit()
        return RuntimeToolSuspension(
            kind="waiting_child",
            reservation_id=child.id,
            poll_after_seconds=5,
            deadline_at=now + timedelta(days=30),
            reason="awaiting_coordinator_child",
            payload={
                "child_step_id": child.id,
                "child_step_key": child.step_key,
                "checklist_item_key": item_key,
            },
        )


def make_delegate_service_handler(
    s: dict[str, Any],
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """The runtime dynamic-tool handler bound to one coordinator step snapshot."""

    async def delegate_to_service(arguments: dict[str, Any]) -> Any:
        arguments = arguments if isinstance(arguments, dict) else {}
        item_key = str(arguments.get("checklist_item_key") or "").strip()
        service_key = str(arguments.get("service_key") or "").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        if not item_key:
            return json.dumps({"error": "checklist_item_key is required"})
        contract, contract_error = _checklist_contract(
            s,
            item_key=item_key,
            service_key=service_key,
        )
        if contract_error or contract is None:
            return json.dumps({
                "error": "invalid_checklist_item",
                "message": contract_error or "Checklist contract is unavailable.",
            })
        try:
            typed_result = await _release_or_replay_typed_child(
                s,
                contract=contract,
                item_key=item_key,
            )
        except Exception as exc:  # noqa: BLE001 - identity failures fail closed
            logger.warning(
                "coordinator step %s: typed child %s could not be released",
                s.get("step_key"), item_key, exc_info=True,
            )
            return json.dumps({
                "error": "typed_child_release_failed",
                "message": str(exc),
                "checklist_item_key": item_key,
            })
        if typed_result is not None:
            return typed_result

        # Compatibility path for V1 plans materialised before typed children
        # existed. Those rows still need the model-authored service/prompt.
        if not service_key:
            return json.dumps({"error": "service_key is required for a legacy plan"})
        if not prompt:
            return json.dumps({"error": "prompt is required for a legacy plan"})
        source_params = contract.get("params") if isinstance(contract.get("params"), dict) else {}
        # The persisted source contract owns the budget. The coordinator model
        # may request less/more in prose but cannot widen a child's round cap.
        max_rounds = source_params.get("max_rounds")
        authoritative_prompt = _prompt_with_authoritative_contract(prompt, contract)

        try:
            dispatch = await _insert_or_resume_child_step(
                s,
                contract=contract,
                item_key=item_key,
                service_key=service_key,
                prompt=authoritative_prompt,
            )
        except Exception as exc:  # noqa: BLE001 — fail closed on identity collisions
            logger.warning(
                "coordinator step %s: could not claim checklist item %s",
                s.get("step_key"), item_key, exc_info=True,
            )
            return json.dumps({
                "error": "delegation_claim_failed",
                "message": str(exc),
                "checklist_item_key": item_key,
            })
        if dispatch.reused_response is not None:
            return dispatch.reused_response

        async def on_round_end(
            rounds: int,
            messages: list[dict[str, Any]],
            usage: dict[str, Any] | None = None,
            tool_calls_made: list[str] | None = None,
            runtime_state: dict[str, Any] | None = None,
        ) -> None:
            await _checkpoint_child_round(
                dispatch.child_id,
                rounds,
                messages,
                usage,
                tool_calls_made,
                terminal_failure=(
                    runtime_state.get("terminal_failure")
                    if isinstance(runtime_state, dict)
                    else None
                ),
                parent_step_id=str(s.get("step_id") or "") or None,
                parent_lease_id=str(s.get("lease_id") or "") or None,
                parent_execution_claim_id=(
                    str(s.get("execution_claim_id") or "") or None
                ),
                entity_id=str(s.get("entity_id") or "") or None,
                workspace_id=str(s.get("workspace_id") or "") or None,
                task_id=str(s.get("task_id") or "") or None,
            )

        async def on_service_resolved(
            subscription_id: str,
            agent_id: str,
        ) -> None:
            await _pin_child_service_identity(
                dispatch.child_id,
                subscription_id,
                agent_id,
                parent_step_id=str(s.get("step_id") or "") or None,
                parent_lease_id=str(s.get("lease_id") or "") or None,
                parent_execution_claim_id=(
                    str(s.get("execution_claim_id") or "") or None
                ),
            )

        async def on_human_input_suspension(
            control: dict[str, Any],
            pause_payload: dict[str, Any],
        ) -> None:
            await _checkpoint_child_human_input(
                dispatch.child_id,
                control,
                pause_payload,
                parent_step_id=str(s.get("step_id") or "") or None,
                parent_lease_id=str(s.get("lease_id") or "") or None,
                parent_execution_claim_id=(
                    str(s.get("execution_claim_id") or "") or None
                ),
            )

        async def on_tool_effect_intent(
            tool_name: str,
            tool_arguments: dict[str, Any],
            tool_call_id: str | None,
            classification: Any | None,
            pending_checkpoint: dict[str, Any] | None = None,
            *,
            approval_request_id: str | None = None,
        ) -> Any:
            return await _checkpoint_child_effect_intent(
                dispatch.child_id,
                tool_name,
                tool_arguments,
                tool_call_id,
                classification,
                pending_checkpoint,
                approval_request_id,
                parent_step_id=str(s.get("step_id") or "") or None,
                parent_lease_id=str(s.get("lease_id") or "") or None,
                parent_execution_claim_id=(
                    str(s.get("execution_claim_id") or "") or None
                ),
                entity_id=str(s.get("entity_id") or ""),
                workspace_id=str(s.get("workspace_id") or ""),
                task_id=str(s.get("task_id") or "") or None,
            )

        try:
            from packages.core.ai.runtime import workspace_delegation_actions

            delegate_params: dict[str, Any] = {
                "service_key": service_key,
                "prompt": dispatch.prompt,
                "max_rounds": max_rounds,
                "delegation_idempotency_key": dispatch.idempotency_key,
                "skill_refs": [
                    str(ref)
                    for ref in (contract.get("skill_refs") or [])
                    if str(ref or "").strip()
                ],
            }
            if dispatch.approval_token:
                delegate_params["approval_token"] = dispatch.approval_token
            if dispatch.resolved_subscription_id:
                delegate_params["agent_subscription_id"] = (
                    dispatch.resolved_subscription_id
                )
            if dispatch.resolved_agent_id:
                delegate_params["agent_id"] = dispatch.resolved_agent_id
            raw = await workspace_delegation_actions.runtime_workspace_delegate_service_action(
                entity_id=str(s.get("entity_id") or ""),
                workspace_id=str(s.get("workspace_id") or ""),
                user_id=s.get("user_id"),
                conversation_id=s.get("conversation_id"),
                params=delegate_params,
                task_id=s.get("task_id"),
                step_id=dispatch.child_id,
                initial_messages=dispatch.initial_messages,
                initial_usage=dispatch.initial_usage,
                initial_rounds=dispatch.initial_rounds,
                initial_tool_calls_made=dispatch.initial_tool_calls_made,
                runtime_checkpoint=dispatch.runtime_checkpoint,
                on_round_end=on_round_end,
                on_round_end_strict=True,
                on_tool_effect_intent=on_tool_effect_intent,
                on_human_input_suspension=on_human_input_suspension,
                on_service_resolved=on_service_resolved,
                # Coordinator children persist their own cumulative accounting.
                # The parent folds those durable rows once at terminal success;
                # an in-memory nested collector cannot survive crash windows.
                record_nested_usage=False,
                output_schema=dispatch.output_schema,
                parent_step_id=str(s.get("step_id") or "") or None,
                parent_lease_id=str(s.get("lease_id") or "") or None,
                parent_execution_claim_id=(
                    str(s.get("execution_claim_id") or "") or None
                ),
            )
        except Exception as exc:  # noqa: BLE001 — the tool result carries the failure
            logger.warning(
                "coordinator step %s: delegation to %s crashed",
                s.get("step_key"), service_key, exc_info=True,
            )
            failed_result = {
                "error": "delegation_failed",
                "message": str(exc),
                "status": "failed",
                "checklist_item_key": item_key,
                "idempotency_key": dispatch.idempotency_key,
            }
            await _settle_child_step(
                dispatch.child_id,
                parent_step_id=str(s.get("step_id") or "") or None,
                parent_lease_id=str(s.get("lease_id") or "") or None,
                parent_execution_claim_id=(
                    str(s.get("execution_claim_id") or "") or None
                ),
                result=failed_result,
                error={"type": "DelegationFailed", "message": str(exc)},
                status=ExecutionStepStatus.FAILED.value,
            )
            return json.dumps(failed_result)

        raw = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        try:
            raw_payload = json.loads(raw)
        except (TypeError, ValueError):
            raw_payload = None
        if isinstance(raw_payload, dict):
            raw_evidence = raw_payload.get("evidence_refs")
            if isinstance(raw_evidence, list):
                raw_payload["evidence_refs"] = [
                    {**ref, "coordinator_child_step_id": dispatch.child_id}
                    for ref in raw_evidence
                    if isinstance(ref, dict)
                ]
                raw = json.dumps(raw_payload, ensure_ascii=False, default=str)
        outcome = _child_outcome(raw)
        settled = await _settle_child_step(
            dispatch.child_id,
            parent_step_id=str(s.get("step_id") or "") or None,
            parent_lease_id=str(s.get("lease_id") or "") or None,
            parent_execution_claim_id=(
                str(s.get("execution_claim_id") or "") or None
            ),
            result=outcome.result,
            error=outcome.error,
            status=outcome.status,
            evidence_refs=outcome.evidence_refs,
            cost=_child_cost(outcome.result),
            contract=contract,
            item_key=item_key,
            idempotency_key=dispatch.idempotency_key,
        )
        if settled is not None:
            raw = json.dumps(settled.result, ensure_ascii=False, default=str)
        return raw

    return delegate_to_service


async def _coordinator_blocked_resume_forced_tool_call(
    s: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the one exact blocked child continuation for parent resume.

    This is server-derived forced-call metadata, never model-authored
    authority.  It prevents a resumed coordinator from deciding whether to
    recreate/rephrase a blocked delegation: the first operation is always the
    stable child whose canonical Runtime approval was granted.
    """

    parent_key = str(s.get("step_key") or "")
    plan_id = str(s.get("plan_id") or "")
    if not parent_key or not plan_id:
        return None
    from packages.core.constants.approvals import (
        ApprovalOriginKind,
        ApprovalStatus,
    )
    from packages.core.models.hitl_request import HitlRequest

    async with async_session() as db:
        # Discover the exact nested request without taking execution locks.  If
        # it is linked to an outer lease request, that outer row is the mutex
        # already used by Chat/governance decisions and must be locked first.
        # The locked query below revalidates the complete candidate set.
        preview_rows = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.workspace_id == s.get("workspace_id"),
                ExecutionStep.step_status.in_((
                    ExecutionStepStatus.WAITING_HUMAN.value,
                    ExecutionStepStatus.RUNNING.value,
                )),
            ).order_by(ExecutionStep.id)
        )).scalars().all()
        preview_tokens: list[str] = []
        preview_has_input = False
        for preview_child in preview_rows:
            preview_params = (
                preview_child.params
                if isinstance(preview_child.params, dict)
                else {}
            )
            preview_result = (
                preview_child.result
                if isinstance(preview_child.result, dict)
                else {}
            )
            if not (
                preview_params.get(COORDINATOR_CHILD_PARAM_KEY) is True
                and preview_params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is not True
                and preview_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY)
                == parent_key
            ):
                continue
            preview_token = str(
                preview_result.get("approval_token") or ""
            ).strip()
            if preview_token:
                preview_tokens.append(preview_token)
            elif _is_request_input_wait(preview_result):
                preview_has_input = True
        approval_nested_request_id = (
            preview_tokens[0]
            if len(preview_tokens) == 1 and not preview_has_input
            else None
        )
        approval_lock_request_id = await _coordinator_approval_lock_request_id(
            db,
            nested_request_id=approval_nested_request_id,
            entity_id=str(s.get("entity_id") or "") or None,
            workspace_id=str(s.get("workspace_id") or "") or None,
        )
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=str(s.get("step_id") or "") or None,
            parent_lease_id=str(s.get("lease_id") or "") or None,
            parent_execution_claim_id=(
                str(s.get("execution_claim_id") or "") or None
            ),
            expected_plan_id=plan_id,
            expected_step_key=parent_key,
            approval_lock_request_id=approval_lock_request_id,
            approval_nested_request_id=approval_nested_request_id,
            expected_task_id=str(s.get("task_id") or "") or None,
            expected_entity_id=str(s.get("entity_id") or "") or None,
            expected_workspace_id=str(s.get("workspace_id") or "") or None,
        )
        if locked is None:
            return None
        lease, parent = locked
        rows = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.workspace_id == s.get("workspace_id"),
                ExecutionStep.step_status.in_((
                    ExecutionStepStatus.WAITING_HUMAN.value,
                    ExecutionStepStatus.RUNNING.value,
                )),
            )
            .order_by(ExecutionStep.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all()
        candidates: list[tuple[ExecutionStep, str]] = []
        input_candidates: list[ExecutionStep] = []
        for child in rows:
            child_params = child.params if isinstance(child.params, dict) else {}
            child_result = child.result if isinstance(child.result, dict) else {}
            token = str(child_result.get("approval_token") or "").strip()
            if not (
                child_params.get(COORDINATOR_CHILD_PARAM_KEY) is True
                and child_params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is not True
                and child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) == parent_key
            ):
                continue
            if token:
                candidates.append((child, token))
            elif _is_request_input_wait(child_result):
                input_candidates.append(child)
        if input_candidates:
            if candidates or len(input_candidates) != 1:
                return None
            child = input_candidates[0]
            # A child may have committed its exact request_input checkpoint and
            # crashed before the parent received the blocked tool envelope. A
            # successor must force that stable child even before an answer
            # exists; its handler will replay the stored blocked result so the
            # outer card is reconstructed.  The pending call and its parent
            # binding are still server-owned and must match exactly.
            from packages.core.ai.runtime.control import RuntimeAgentCheckpoint

            try:
                parked = RuntimeAgentCheckpoint.from_dict(child.runtime_checkpoint)
            except (TypeError, ValueError):
                return None
            if str(parked.pending_tool_call.get("name") or "") != "request_input":
                return None
            pending_tool_call_id = str(
                parked.pending_tool_call.get("id") or ""
            ).strip()
            if not pending_tool_call_id:
                return None
            parent_snapshot = _parent_input_snapshot(parent)
            parent_params = parent_snapshot["params"]
            if (
                not isinstance(parked.pending_tool_result, dict)
                and not _input_context_matches(
                    parent_params,
                    child_id=child.id,
                    pending_tool_call_id=pending_tool_call_id,
                )
            ):
                return None
            child_params = child.params if isinstance(child.params, dict) else {}
            item_key = str(
                child_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or ""
            ).strip()
            prompt = str(child_params.get("prompt") or "").strip()
            service_key = str(child.service_key or "").strip()
            idempotency_key = str(
                child_params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or ""
            ).strip()
            if not item_key or not prompt or not service_key or not idempotency_key:
                return None
            return {
                "name": DELEGATE_TO_SERVICE_TOOL_NAME,
                "arguments": {
                    "checklist_item_key": item_key,
                    "service_key": service_key,
                    "prompt": prompt,
                },
            }
        if len(candidates) != 1:
            return None
        child, token = candidates[0]
        if token != str(approval_nested_request_id or ""):
            # Candidate identity changed after discovery.  Do not lock a new
            # request while Lease/Step rows from the old snapshot are held.
            return None
        request = (await db.execute(
            select(HitlRequest).where(
                HitlRequest.id == token,
                HitlRequest.entity_id == child.entity_id,
                HitlRequest.workspace_id == child.workspace_id,
                HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL.value,
                HitlRequest.consumed_at.is_(None),
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if request is None:
            return None
        child_params = child.params if isinstance(child.params, dict) else {}
        request_context = request.context if isinstance(request.context, dict) else {}
        resume = request_context.get("delegation_resume")
        item_key = str(
            child_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or ""
        ).strip()
        prompt = str(child_params.get("prompt") or "").strip()
        service_key = str(child.service_key or "").strip()
        idempotency_key = str(
            child_params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or ""
        ).strip()
        if not _coordinator_nested_request_matches_execution(
            request,
            child=child,
            parent=parent,
            lease=lease,
            task_id=str(s.get("task_id") or ""),
            idempotency_key=idempotency_key,
            outer_request_id=approval_lock_request_id,
        ):
            return None
        if (
            not item_key
            or not prompt
            or not service_key
            or not idempotency_key
            or not isinstance(resume, dict)
            or resume.get("version") != 1
            or str(resume.get("approval_token") or "") != token
            or str(resume.get("task_id") or "") != str(s.get("task_id") or "")
            or str(resume.get("step_id") or "") != child.id
            or str(resume.get("service_key") or "") != service_key
            or str(resume.get("delegation_idempotency_key") or "")
            != idempotency_key
        ):
            return None
        if (
            child.step_status == ExecutionStepStatus.RUNNING.value
            and request.status != ApprovalStatus.GRANTED.value
        ):
            return None
        if request.status == ApprovalStatus.PENDING.value:
            placeholder = request_context.get(
                COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY
            )
            stable_placeholder = {
                "version": COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION,
                "request_id": None,
                "nested_request_id": token,
                "child_step_id": child.id,
                "parent_step_id": parent.id,
                "parent_lease_id": placeholder.get("parent_lease_id")
                if isinstance(placeholder, dict)
                else None,
                "parent_execution_claim_id": placeholder.get(
                    "parent_execution_claim_id"
                )
                if isinstance(placeholder, dict)
                else None,
                "plan_id": plan_id,
                "task_id": str(s.get("task_id") or ""),
                "child_step_key": child.step_key,
                "service_key": service_key,
                "idempotency_key": idempotency_key,
            }
            if not isinstance(placeholder, dict) or placeholder != stable_placeholder:
                return None
            rebound_context = dict(request_context)
            rebound_context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY] = {
                **stable_placeholder,
                "parent_lease_id": lease.id,
                "parent_execution_claim_id": lease.execution_claim_id,
            }
            request.context = rebound_context
            await db.commit()
        elif request.status != ApprovalStatus.GRANTED.value:
            return None
        return {
            "name": DELEGATE_TO_SERVICE_TOOL_NAME,
            "arguments": {
                "checklist_item_key": item_key,
                "service_key": service_key,
                "prompt": prompt,
            },
        }


async def _unacknowledged_done_forced_tool_calls(
    s: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replay stored DONE results that never crossed a parent checkpoint."""

    parent_key = str(s.get("step_key") or "").strip()
    plan_id = str(s.get("plan_id") or "").strip()
    if not parent_key or not plan_id:
        return []
    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=str(s.get("step_id") or "") or None,
            parent_lease_id=str(s.get("lease_id") or "") or None,
            parent_execution_claim_id=(
                str(s.get("execution_claim_id") or "") or None
            ),
            expected_plan_id=plan_id,
            expected_step_key=parent_key,
        )
        if locked is None:
            return []
        _, parent = locked
        rows = list((await db.execute(
            select(ExecutionStep)
            .where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_status == ExecutionStepStatus.DONE.value,
            )
            .order_by(ExecutionStep.step_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        forced: list[dict[str, Any]] = []
        for child in rows:
            params = child.params if isinstance(child.params, dict) else {}
            if (
                params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
                or params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent_key
                or _child_handoff_ack_matches(child, parent)
            ):
                continue
            item_key = str(
                params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or ""
            ).strip()
            idempotency_key = str(
                params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or ""
            ).strip()
            if not item_key or not idempotency_key:
                # A malformed/legacy row cannot be safely attributed to one
                # checklist call. Submission remains closed instead of asking
                # the model to guess which result it missed.
                continue
            if params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is True:
                arguments = {"checklist_item_key": item_key}
            else:
                prompt = str(params.get("prompt") or "").strip()
                service_key = str(child.service_key or "").strip()
                if not prompt or not service_key:
                    continue
                arguments = {
                    "checklist_item_key": item_key,
                    "service_key": service_key,
                    "prompt": prompt,
                }
            forced.append({
                "name": DELEGATE_TO_SERVICE_TOOL_NAME,
                "arguments": arguments,
            })
        return forced


async def coordinator_resume_forced_tool_call(
    s: dict[str, Any],
) -> dict[str, Any] | None:
    """Backward-compatible singular blocked-child continuation."""

    return await _coordinator_blocked_resume_forced_tool_call(s)


async def coordinator_resume_forced_tool_calls(
    s: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every server-owned handoff/blocked replay needed on resume."""

    calls = await _unacknowledged_done_forced_tool_calls(s)
    blocked = await _coordinator_blocked_resume_forced_tool_call(s)
    if blocked is not None:
        blocked_item = str(
            (blocked.get("arguments") or {}).get("checklist_item_key") or ""
        )
        if not any(
            str((call.get("arguments") or {}).get("checklist_item_key") or "")
            == blocked_item
            for call in calls
        ):
            calls.append(blocked)
    return calls


async def coordinator_completion_error(
    db: Any,
    parent: ExecutionStep,
) -> str | None:
    """Validate the durable coordinator checklist at the final DONE boundary.

    The caller owns the live parent lock.  Child rows are then locked in stable
    step-key order, matching the coordinator handoff writer.  Keeping this
    check independent of AgenticLoop means Dispatcher completion, recovery,
    and the cooperative submit tool all enforce one storage-backed contract.
    """

    parent_key = str(parent.step_key or "").strip()
    plan_id = str(parent.plan_id or "").strip()
    params = parent.params if isinstance(parent.params, dict) else {}
    if params.get(COORDINATOR_PARAM_KEY) is not True:
        return "The step is not a persisted coordinator root."
    checklist = [
        item
        for item in (params.get("checklist") or [])
        if isinstance(item, dict)
    ]
    if not parent_key or not plan_id or not checklist:
        return "The persisted coordinator checklist is unavailable."
    expected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for contract in checklist:
        item_key = str(
            contract.get("delegation_key") or contract.get("key") or ""
        ).strip()
        if not item_key or item_key in seen:
            return "The persisted coordinator checklist is invalid or ambiguous."
        seen.add(item_key)
        expected.append((item_key, _stable_child_step_key(parent_key, item_key)))
    statuses: dict[str, str] = {}
    contract_errors: list[str] = []
    undelivered: list[str] = []
    contracts_by_key = {
        str(item.get("delegation_key") or item.get("key") or "").strip(): item
        for item in checklist
    }
    for item_key, child_key in sorted(expected, key=lambda item: item[1]):
        child = (await db.execute(
            select(ExecutionStep)
            .where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == child_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        statuses[item_key] = (
            child.step_status if child is not None else "not_started"
        )
        if child is not None and child.step_status == ExecutionStepStatus.DONE.value:
            if (
                params.get(COORDINATOR_VERSION_PARAM_KEY)
                == COORDINATOR_TYPED_VERSION
                and not _typed_child_contract_matches(
                    child,
                    contracts_by_key[item_key],
                    parent_key=parent_key,
                    item_key=item_key,
                    idempotency_key=_child_idempotency_key(
                        plan_id,
                        parent_key,
                        item_key,
                    ),
                )
            ):
                contract_errors.append(
                    f"{item_key}: typed child does not match its persisted source contract"
                )
            contract_error = _completed_child_contract_error(
                contracts_by_key[item_key], child
            )
            if contract_error:
                contract_errors.append(f"{item_key}: {contract_error}")
            if not _child_handoff_ack_matches(child, parent):
                undelivered.append(item_key)
    incomplete = [
        f"{item_key}={statuses[item_key]}"
        for item_key, _ in expected
        if statuses[item_key] != ExecutionStepStatus.DONE.value
    ]
    if incomplete:
        return (
            "Every persisted checklist item must complete before submission; "
            + ", ".join(incomplete)
            + "."
        )
    if contract_errors:
        return (
            "Completed checklist items did not satisfy their own persisted "
            "delivery contracts; " + "; ".join(contract_errors) + "."
        )
    if undelivered:
        return (
            "Completed checklist item results have not yet been durably "
            "delivered to the parent transcript; "
            + ", ".join(undelivered)
            + "."
        )
    return None


async def coordinator_submission_error(s: dict[str, Any]) -> str | None:
    """Return why the parent may not submit yet, under its live lease fence."""

    parent_key = str(s.get("step_key") or "").strip()
    plan_id = str(s.get("plan_id") or "").strip()
    if not parent_key or not plan_id:
        return "The persisted coordinator checklist is unavailable."

    async with async_session() as db:
        locked = await _lock_live_parent_execution(
            db,
            parent_step_id=str(s.get("step_id") or "") or None,
            parent_lease_id=str(s.get("lease_id") or "") or None,
            parent_execution_claim_id=(
                str(s.get("execution_claim_id") or "") or None
            ),
            expected_plan_id=plan_id,
            expected_step_key=parent_key,
        )
        if locked is None:
            return "The coordinator no longer owns a live execution claim."
        _, parent = locked
        return await coordinator_completion_error(db, parent)


def _delegation_contract_error(
    contract: dict[str, Any],
    *,
    result: Any,
    evidence_refs: Any,
) -> str | None:
    """Validate one child handoff independently; sibling receipts never count."""

    evidence = [item for item in (evidence_refs or []) if isinstance(item, dict)]
    if _has_unsettled_effect_intent(evidence):
        return "an external tool intent is still unresolved"

    payload = result if isinstance(result, dict) else {}
    delegated_result = (
        payload.get("result")
        if payload.get("delegated") is True and "result" in payload
        else result
    )
    schema = _effective_output_schema(contract)
    if schema is not None:
        from packages.core.dispatcher.validation import maybe_collect_errors

        errors = maybe_collect_errors(schema, delegated_result)
        if errors:
            return "the persisted result no longer matches its output schema"

    expects = {
        str(item).strip().lower()
        for item in (contract.get("expects") or [])
        if str(item or "").strip()
    }
    if {"publish", "send"}.intersection(expects) and not any(
        (
            item.get("kind") == "tool_effect"
            and item.get("effect") == "publish"
            and isinstance(item.get("fields"), dict)
            and item["fields"].get("delivery_confirmed") is True
        )
        or (
            item.get("kind") == "tool_intent_settlement"
            and item.get("effect") == "operator_confirmed_applied"
            and isinstance(item.get("fields"), dict)
            and item["fields"].get("delivery_confirmed") is True
        )
        for item in evidence
    ):
        return "no terminal provider publish/send receipt was captured"
    if "files" in expects:
        if not any(item.get("kind") == "artifact" for item in evidence):
            return "no saved artifact evidence was captured"
    return None


def _completed_child_contract_error(
    contract: dict[str, Any],
    child: ExecutionStep,
) -> str | None:
    """Validate one persisted child independently; sibling receipts never count."""

    return _delegation_contract_error(
        contract,
        result=child.result,
        evidence_refs=child.evidence_refs,
    )


__all__ = [
    "acknowledge_coordinator_child_handoffs",
    "build_delegate_service_tool",
    "coordinator_child_cost_totals",
    "coordinator_completion_error",
    "coordinator_resume_forced_tool_call",
    "coordinator_resume_forced_tool_calls",
    "coordinator_submission_error",
    "make_delegate_service_handler",
]
