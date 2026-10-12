"""Unified approval resolution — one decision function every gate calls.

``resolve_approval`` is the single place that answers "allow / deny / needs a
human?" for any gated action, on any plane. It replaces the parallel logic that
lived independently in the runtime tool guard and the dispatcher step gate.

Design invariants (see the redesign RFC):

  * ONE decision. Every gate delegates here instead of re-implementing risk /
    policy / grant checks. Approving once therefore satisfies every gate that
    shares the subject.
  * Permission before consent. Actor role, workspace membership, and delegated
    Agent/tool bindings are hard, non-approvable inputs. They are re-evaluated
    before an approval token or standing grant can be consumed.
  * Hard blocks stay hard. Deny rules, risk ceilings and budget caps cannot
    be overridden by mode changes or human consent.
  * ONE policy model. Every scope inherits the account default and stores only
    its own overrides. Writes require the shared versioned settings service.
  * ONE decision per operation. Durable run/call or source identities keep a
    pending request stable across mode changes and prevent consumed replay.
  * Lifecycle owns cleanup. When an origin (step/task) reaches a terminal state,
    ``resolve_origin_requests`` expires its open requests, so nothing is
    orphaned and counts derive from open requests, not stale messages.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value

from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
    HITL_REQUIRED_PAYLOAD_FIELDS,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_SCOPE_PROMOTED_FROM,
    ApprovalConsumeOutcome,
    ApprovalOriginKind,
    ApprovalOutcome,
    ApprovalStatus,
    AuthorizeScope,
    HitlType,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.execution import (
    COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
)
from packages.core.models.hitl_request import HitlRequest
from packages.core.services.runtime_authorization import (
    AuthorizationRule,
    PermissionDecision,
)
from packages.core.governance.approval_scope import approval_scope_key

logger = logging.getLogger(__name__)

ApprovalContextLoader = Callable[[], Awaitable[dict[str, Any]]]
_APPROVAL_RESOURCE_IDS_CONTEXT_KEY = "_approval_resource_ids"


class ApprovalAuthorityError(PermissionError):
    """The actor is not allowed to resolve this HITL request."""

    def __init__(self, decision: PermissionDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason or "HITL resolution permission denied.")


class ApprovalDecisionRequiredError(PermissionError):
    """The paused origin needs its dedicated decision, not a generic resume."""


class ApprovalContinuationBindingError(PermissionError):
    """A mirrored approval no longer names its exact durable continuation."""


class ApprovalConsumptionError(RuntimeError):
    """The one-time approval was already consumed by another caller."""

    def __init__(
        self,
        request_id: str,
        outcome: ApprovalConsumeOutcome,
    ) -> None:
        self.request_id = request_id
        self.outcome = outcome
        super().__init__(
            f"Approval {request_id} could not be consumed: {outcome.value}."
        )


def coordinator_outer_approval_id(request: HitlRequest) -> str | None:
    """Return the canonical parent approval for a coordinator-owned tool HITL."""

    from packages.core.constants.execution import (
        COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION,
        COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
    )

    context = request.context if isinstance(request.context, dict) else {}
    binding = context.get(COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY)
    if (
        not isinstance(binding, dict)
        or binding.get("version") != COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION
    ):
        return None
    outer_id = str(binding.get("request_id") or "").strip()
    return outer_id or None


def is_coordinator_owned_nested_approval(request: HitlRequest) -> bool:
    """Whether this Runtime request must be resolved through its parent card."""

    from packages.core.constants.execution import (
        COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
    )

    context = request.context if isinstance(request.context, dict) else {}
    return COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY in context


async def is_coordinator_owned_nested_approval_durable(
    db,
    request: HitlRequest,
) -> bool:
    """Detect a nested approval even before its parent mirror is linked.

    Runtime mints the exact tool-call request before the child loop can attach
    the outer Task approval. The server-authored child Step identity closes
    that small crash/race window so no generic Chat/queue surface can grant the
    request independently.
    """

    if is_coordinator_owned_nested_approval(request):
        return True
    if (
        request.origin_kind != ApprovalOriginKind.TOOL_CALL.value
        or not request.origin_step_id
    ):
        return False
    from packages.core.constants.execution import COORDINATOR_CHILD_PARAM_KEY
    from packages.core.models.execution import ExecutionStep

    step = (await db.execute(
        select(ExecutionStep).where(
            ExecutionStep.id == request.origin_step_id,
            ExecutionStep.entity_id == request.entity_id,
        )
    )).scalar_one_or_none()
    params = step.params if step is not None and isinstance(step.params, dict) else {}
    return params.get(COORDINATOR_CHILD_PARAM_KEY) is True


async def _coordinator_nested_request_for_mirror(
    db,
    request: HitlRequest,
) -> HitlRequest | None:
    """Return the exact delegated Runtime approval bound to a lease request.

    Coordinator children execute through the ordinary Chat tool guard, which
    owns a ``tool_call`` HitlRequest.  The parent Task pause owns a second,
    lease-scoped UI request.  Dispatcher records a server-derived link between
    the two.  Every resolution surface funnels through ``grant_approval`` /
    ``deny_approval``; revalidating the link here makes the human verdict
    atomic across both objects and prevents generic ``confirm=True`` flags from
    authorizing an unrelated child.
    """

    from packages.core.constants.execution import (
        COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
        COORDINATOR_CHILD_ITEM_PARAM_KEY,
        COORDINATOR_CHILD_PARAM_KEY,
        COORDINATOR_CHILD_RELEASED_PARAM_KEY,
        COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY,
        COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION,
        COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
        COORDINATOR_PARAM_KEY,
        COORDINATOR_PARENT_STEP_PARAM_KEY,
        COORDINATOR_TYPED_CHILD_PARAM_KEY,
        COORDINATOR_TYPED_VERSION,
        COORDINATOR_VERSION_PARAM_KEY,
        COORDINATOR_WAITING_CHILD_PARAM_KEY,
        ExecutionStepStatus,
        WorkLeaseStatus,
    )
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.models.worker import WorkLease

    if request.origin_kind != ApprovalOriginKind.LEASE.value:
        return None
    context = request.context if isinstance(request.context, dict) else {}
    binding = context.get(COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY)
    if binding is None:
        return None
    if not isinstance(binding, dict) or binding.get("version") != COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION:
        raise ApprovalContinuationBindingError(
            "Coordinator approval continuation has an invalid version."
        )
    typed_child_binding = binding.get("typed_child") is True

    exact_origin = {
        "parent_step_id": request.origin_step_id,
        "plan_id": request.origin_plan_id,
        "task_id": request.origin_task_id,
    }
    if any(
        (binding.get(key) is None) != (value is None)
        or (
            binding.get(key) is not None
            and value is not None
            and str(binding.get(key)) != str(value)
        )
        for key, value in exact_origin.items()
    ):
        raise ApprovalContinuationBindingError(
            "Coordinator approval continuation does not match its lease origin."
        )
    parent_lease_id = str(binding.get("parent_lease_id") or "").strip()
    parent_execution_claim_id = str(
        binding.get("parent_execution_claim_id") or ""
    ).strip()
    pending_kind = str(context.get("pending_kind") or "human_input")
    if (
        not parent_lease_id
        or not parent_execution_claim_id
        or request.dedup_key != f"lease:{parent_lease_id}:{pending_kind}"
    ):
        raise ApprovalContinuationBindingError(
            "Coordinator approval continuation does not match its lease claim."
        )

    nested_id = str(binding.get("request_id") or "").strip()
    child_id = str(binding.get("child_step_id") or "").strip()
    if not nested_id or not child_id:
        raise ApprovalContinuationBindingError(
            "Coordinator approval continuation is incomplete."
        )
    # The outer approval row is already locked by the decision surface.  Lock
    # the Task/Plan mutexes *before* the lease so this path cannot deadlock with
    # Plan cancellation (Workspace -> Task -> Plan -> Lease).  Child
    # persistence does not acquire Task/Plan after its lease lock, so the
    # remainder keeps the shared Lease -> parent -> child -> nested order.
    locked_task = None
    if request.origin_task_id:
        locked_task = (await db.execute(
            select(Task)
            .where(
                Task.id == request.origin_task_id,
                Task.entity_id == request.entity_id,
                Task.workspace_id == request.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if locked_task is None:
            raise ApprovalContinuationBindingError(
                "Coordinator approval Task no longer exists."
            )

    plan_filters = [
        ExecutionPlan.id == request.origin_plan_id,
        ExecutionPlan.entity_id == request.entity_id,
        ExecutionPlan.workspace_id == request.workspace_id,
    ]
    if request.origin_task_id:
        plan_filters.append(ExecutionPlan.task_id == request.origin_task_id)
    else:
        plan_filters.append(ExecutionPlan.task_id.is_(None))
    locked_plan = (await db.execute(
        select(ExecutionPlan)
        .where(*plan_filters)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked_plan is None:
        raise ApprovalContinuationBindingError(
            "Coordinator approval Plan no longer exists."
        )

    lease = (await db.execute(
        select(WorkLease)
        .where(WorkLease.id == parent_lease_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        lease is None
        or lease.step_id != request.origin_step_id
        or lease.plan_id != request.origin_plan_id
        or lease.entity_id != request.entity_id
        or lease.workspace_id != request.workspace_id
        or lease.status != WorkLeaseStatus.NEEDS_HUMAN.value
    ):
        raise ApprovalContinuationBindingError(
            "Coordinator parent lease is no longer waiting on this approval."
        )
    parent = (await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.id == request.origin_step_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    parent_params = parent.params if parent is not None and isinstance(parent.params, dict) else {}
    if (
        parent is None
        or parent.entity_id != request.entity_id
        or parent.workspace_id != request.workspace_id
        or parent.plan_id != request.origin_plan_id
        or parent.step_status != ExecutionStepStatus.WAITING_HUMAN.value
    ):
        raise ApprovalContinuationBindingError(
            "Coordinator parent is no longer waiting on this approval."
        )
    if typed_child_binding:
        coordinator_parent_step_key = str(
            parent_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) or ""
        ).strip()
        if (
            child_id != parent.id
            or parent_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
            or parent_params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is not True
            or parent_params.get(COORDINATOR_CHILD_RELEASED_PARAM_KEY) is not True
            or not coordinator_parent_step_key
            or str(binding.get("coordinator_parent_step_key") or "")
            != coordinator_parent_step_key
        ):
            raise ApprovalContinuationBindingError(
                "Coordinator typed child is no longer waiting on this approval."
            )
        # The Task/Plan mutexes above stabilize lifecycle changes. Read the
        # V2 root without taking a child -> root row lock (the live release
        # path uses root -> child) and require it to be parked for this exact
        # child. This both closes orphan-child approval and preserves the
        # global lock order under concurrent cancellation/recovery.
        coordinator_root = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == request.origin_plan_id,
                ExecutionStep.entity_id == request.entity_id,
                ExecutionStep.workspace_id == request.workspace_id,
                ExecutionStep.step_key == coordinator_parent_step_key,
            ).execution_options(populate_existing=True)
        )).scalar_one_or_none()
        root_params = (
            coordinator_root.params
            if coordinator_root is not None
            and isinstance(coordinator_root.params, dict)
            else {}
        )
        waiting_child = root_params.get(COORDINATOR_WAITING_CHILD_PARAM_KEY)
        if (
            coordinator_root is None
            or coordinator_root.id == parent.id
            or str(binding.get("coordinator_parent_step_id") or "")
            != coordinator_root.id
            or coordinator_root.step_status != ExecutionStepStatus.PAUSED.value
            or root_params.get(COORDINATOR_PARAM_KEY) is not True
            or root_params.get(COORDINATOR_VERSION_PARAM_KEY)
            != COORDINATOR_TYPED_VERSION
            or not isinstance(waiting_child, dict)
            or waiting_child.get("version") != 1
            or str(waiting_child.get("child_step_id") or "") != parent.id
            or str(waiting_child.get("child_step_key") or "") != parent.step_key
            or str(waiting_child.get("checklist_item_key") or "")
            != str(parent_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or "")
        ):
            raise ApprovalContinuationBindingError(
                "Coordinator root is no longer parked for this typed child."
            )
    elif parent_params.get(COORDINATOR_PARAM_KEY) is not True:
        raise ApprovalContinuationBindingError(
            "Coordinator parent is no longer waiting on this approval."
        )

    # A V2 typed child owns its own lease pause, so parent and child are the
    # same locked row. Legacy V1 delegated approvals keep the separate root /
    # dynamic-child rows and their original lock order.
    child = parent if typed_child_binding else (await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.id == child_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if child is None:
        raise ApprovalContinuationBindingError(
            "Coordinator child no longer exists."
        )
    child_params = child.params if isinstance(child.params, dict) else {}
    child_result = child.result if isinstance(child.result, dict) else {}
    child_idempotency_key = str(
        child_params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY) or ""
    ).strip()
    invalid_child = (
        child.entity_id != request.entity_id
        or child.workspace_id != request.workspace_id
        or child.plan_id != request.origin_plan_id
        or child.step_status != ExecutionStepStatus.WAITING_HUMAN.value
        or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
        or not str(child_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or "").strip()
        or not child_idempotency_key
        or str(binding.get("child_step_key") or "") != child.step_key
        or str(binding.get("service_key") or "") != str(child.service_key or "")
        or str(binding.get("idempotency_key") or "") != child_idempotency_key
    )
    if typed_child_binding:
        invalid_child = invalid_child or (
            child.id != parent.id
            or child_params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is not True
            or child_params.get(COORDINATOR_CHILD_RELEASED_PARAM_KEY) is not True
        )
    else:
        invalid_child = invalid_child or (
            child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) != parent.step_key
            or str(child_result.get("approval_token") or "") != nested_id
        )
    if invalid_child:
        raise ApprovalContinuationBindingError(
            "Coordinator child is no longer waiting on this exact approval."
        )

    nested = (await db.execute(
        select(HitlRequest)
        .where(HitlRequest.id == nested_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if nested is None:
        raise ApprovalContinuationBindingError(
            "Delegated Runtime approval no longer exists."
        )
    if (
        nested.origin_kind != ApprovalOriginKind.TOOL_CALL.value
        or nested.entity_id != request.entity_id
        or nested.workspace_id != request.workspace_id
        or str(nested.origin_task_id or "") != str(request.origin_task_id or "")
    ):
        raise ApprovalContinuationBindingError(
            "Delegated Runtime approval is outside the parent Task scope."
        )
    nested_context = nested.context if isinstance(nested.context, dict) else {}
    resume = nested_context.get("delegation_resume")
    reverse = nested_context.get(COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY)
    invalid_reverse = (
        not isinstance(reverse, dict)
        or reverse.get("version") != COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION
        or str(reverse.get("request_id") or "") != request.id
        or str(reverse.get("nested_request_id") or "") != nested.id
        or str(nested.origin_step_id or "") != child.id
        or str(reverse.get("child_step_id") or "") != child.id
        or str(reverse.get("parent_step_id") or "") != parent.id
        or str(reverse.get("parent_lease_id") or "") != lease.id
        or str(reverse.get("parent_execution_claim_id") or "")
        != parent_execution_claim_id
    )
    if typed_child_binding:
        from packages.core.ai.runtime.approval_messages import approval_args_hash

        nested_tool = str(nested_context.get("tool") or "").strip()
        nested_args_hash = str(nested_context.get("args_hash") or "").strip()
        continuation = nested_context.get("continuation")
        continuation_arguments = (
            continuation.get("arguments")
            if isinstance(continuation, dict)
            else None
        )
        invalid_continuation = invalid_reverse or (
            reverse.get("typed_child") is not True
            or str(reverse.get("coordinator_parent_step_id") or "")
            != str(binding.get("coordinator_parent_step_id") or "")
            or str(reverse.get("coordinator_parent_step_key") or "")
            != str(binding.get("coordinator_parent_step_key") or "")
            or not nested_tool
            or not nested_args_hash
            or not isinstance(continuation, dict)
            or continuation.get("version") != 1
            or str(continuation.get("tool") or "").strip() != nested_tool
            or not isinstance(continuation_arguments, dict)
            or approval_args_hash(continuation_arguments) != nested_args_hash
            or str(binding.get("nested_tool") or "") != nested_tool
            or str(binding.get("nested_args_hash") or "") != nested_args_hash
            or str(reverse.get("nested_tool") or "") != nested_tool
            or str(reverse.get("nested_args_hash") or "") != nested_args_hash
        )
    else:
        invalid_continuation = (
            not isinstance(resume, dict)
            or resume.get("version") != 1
            or str(resume.get("approval_token") or "") != nested.id
            or str(resume.get("entity_id") or "") != str(request.entity_id or "")
            or str(resume.get("workspace_id") or "") != str(request.workspace_id or "")
            or str(resume.get("conversation_id") or "")
            != str(nested.origin_conversation_id or "")
            or str(resume.get("step_id") or "") != child.id
            or str(resume.get("task_id") or "") != str(request.origin_task_id or "")
            or str(resume.get("service_key") or "") != str(child.service_key or "")
            or str(resume.get("delegation_idempotency_key") or "")
            != child_idempotency_key
        )
    if invalid_reverse or invalid_continuation:
        raise ApprovalContinuationBindingError(
            "Delegated Runtime approval lost its exact tool continuation."
        )
    return nested


@dataclass(frozen=True)
class ApprovalSubject:
    """WHAT needs approval — plane-agnostic."""

    entity_id: str
    action_key: Optional[str] = None
    capability_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    risk_level: str = "medium"
    kind: str = "action"  # policy "kind" axis (action / subagent / ...)
    requires_approval: bool = False  # step-intrinsic flag
    workspace_id: Optional[str] = None
    resource_ids: tuple[str, ...] = ()
    user_id: Optional[str] = None
    room_id: Optional[str] = None
    account_id: Optional[str] = None
    operation: Optional[str] = None
    impact: Optional[str] = None
    category: Optional[str] = None


@dataclass(frozen=True)
class ApprovalOrigin:
    """WHERE the block is happening — so the right surface renders/resumes it."""

    kind: str  # an ``ApprovalOriginKind`` value
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    step_id: Optional[str] = None
    plan_id: Optional[str] = None
    task_id: Optional[str] = None
    lease_id: Optional[str] = None  # for lease-origin (mid-execution) dedup
    args_hash: Optional[str] = None  # semantic content, never operation identity
    operation_id: Optional[str] = None
    # Host-owned execution identity; never read from model-authored context.
    workflow_run_id: Optional[str] = None
    context: dict = field(default_factory=dict)
    context_loader: Optional[ApprovalContextLoader] = field(
        default=None,
        repr=False,
        compare=False,
    )

    async def materialize_context(self) -> "ApprovalOrigin":
        """Load card-only context at the point a new HITL row is minted.

        Fields used by :func:`dedup_key_for` must remain in ``context`` or in
        their dedicated origin attributes. The loader is deliberately not run
        for allow/deny decisions or when an existing request is reused.
        """

        if self.context_loader is None:
            return self
        loaded = await self.context_loader()
        return replace(
            self,
            context={**self.context, **dict(loaded or {})},
            context_loader=None,
        )


@dataclass
class ApprovalDecision:
    outcome: ApprovalOutcome
    reason: Optional[str] = None
    matched_rule: Optional[str] = None
    request: Optional[HitlRequest] = None  # present when outcome == needs_human

    @property
    def allowed(self) -> bool:
        return self.outcome is ApprovalOutcome.ALLOW


_HIGH = "high"

#: Payload keys the RECORD LAYER writes, as opposed to copy a producer
#: supplies. Excluded from the "did this card come with copy?" test so that
#: validation stays idempotent — it runs once in ``mint_approval_request`` and
#: again in ``_create_pending_request`` on its own output.
_RECORD_LAYER_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        PAYLOAD_KEY_SCOPE,
        PAYLOAD_KEY_SCOPE_PROMOTED_FROM,
    }
)


def _args_hash(payload: Any) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        blob = str(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


_MAX_DEDUP_KEY_LENGTH = 200


def _bounded_dedup_key(value: str) -> str:
    if len(value) <= _MAX_DEDUP_KEY_LENGTH:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    prefix_length = _MAX_DEDUP_KEY_LENGTH - len(digest) - 1
    return f"{value[:prefix_length]}:{digest}"


def dedup_key_for(subject: ApprovalSubject, origin: ApprovalOrigin) -> str:
    """Deduplicate an operation using its durable execution identity.

    Explicit runtime call IDs and source-derived operation IDs are preferred.
    Lifecycle-only legacy producers retain their existing source keys until
    their owning continuation supplies a durable operation identity.
    """
    if origin.operation_id:
        return _bounded_dedup_key(f"operation:{origin.operation_id}")
    scope = approval_scope_key(subject.action_key, subject.resource_id)
    if subject.resource_ids:
        targets_hash = hashlib.sha256(json.dumps(subject.resource_ids).encode()).hexdigest()[:24]
        scope = f"{scope}:targets:{targets_hash}"
    if subject.resource_kind == "proposal" and subject.resource_id:
        return _bounded_dedup_key(f"proposal:{subject.resource_id}")
    if subject.resource_kind == "proposal_item" and subject.resource_id:
        return _bounded_dedup_key(f"proposal_item:{subject.resource_id}")
    if origin.kind == ApprovalOriginKind.STEP and origin.step_id:
        return _bounded_dedup_key(f"step:{origin.step_id}")
    if origin.kind == ApprovalOriginKind.LEASE and origin.lease_id:
        # One request per (lease, pause reason): a lease can legitimately pause
        # for different reasons (login vs confirm), but re-tripping the SAME
        # reason must reuse the card instead of minting a duplicate.
        pending_kind = str(origin.context.get("pending_kind") or PendingActionKind.HUMAN_INPUT.value)
        return _bounded_dedup_key(f"lease:{origin.lease_id}:{pending_kind}")
    if origin.kind == ApprovalOriginKind.TASK and origin.task_id:
        action = scope or subject.capability_id or subject.resource_kind or "action"
        return _bounded_dedup_key(f"task:{origin.task_id}:{action}")
    if origin.kind == ApprovalOriginKind.OPERATION:
        op_id = str(origin.context.get("draft_id") or origin.message_id or "")
        return _bounded_dedup_key(f"op:{op_id}")
    if origin.kind == ApprovalOriginKind.CHANNEL and origin.message_id:
        return _bounded_dedup_key(f"channel:{origin.message_id}")
    # tool_call (or anything else): scoped subject + conversation + args
    subj = scope or subject.capability_id or subject.resource_kind or "action"
    conv = origin.conversation_id or ""
    ah = origin.args_hash or (_args_hash(origin.context.get("arguments")) if origin.context.get("arguments") else "")
    step_scope = f":step:{origin.step_id}" if origin.step_id else ""
    return _bounded_dedup_key(f"tool:{conv}:{subj}:{ah}{step_scope}")


async def _policy_decision(db, subject: ApprovalSubject, origin: ApprovalOrigin, *, spent_credits: Optional[dict]):
    from packages.core.governance.service import check_step_policy

    user_id = subject.user_id
    if not subject.workspace_id and not subject.room_id and origin.conversation_id:
        from packages.core.models.task import Conversation
        conversation = await db.get(Conversation, origin.conversation_id)
        if conversation is None or str(conversation.entity_id) != subject.entity_id:
            from packages.core.governance.policy import PolicyDecision
            return PolicyDecision(False, reason="Conversation approval owner does not match", matched_rule="approval.owner")
        user_id = conversation.user_id
    return await check_step_policy(
        db, entity_id=subject.entity_id, workspace_id=subject.workspace_id,
        user_id=user_id, room_id=subject.room_id, kind=subject.kind,
        action_key=subject.action_key, resource_id=subject.resource_id,
        resource_kind=subject.resource_kind, resource_ids=subject.resource_ids,
        risk_level=subject.risk_level, capability_id=subject.capability_id,
        account_id=subject.account_id, operation=subject.operation,
        impact=subject.impact, category=subject.category,
        spent_credits_per_kind=spent_credits, task_id=origin.task_id,
        workflow_run_id=origin.workflow_run_id,
    )


def _consent_requirements(decision) -> list[str]:
    """Only applicable asks matter; unrelated revision changes do not revoke consent."""
    explanation = decision.explanation or {}
    decisions = explanation.get("target_decisions") or [explanation]
    return sorted({json.dumps({key: item.get(key) for key in (
        "scope", "rule_id", "source", "target", "reason_code",
    )}, sort_keys=True, separators=(",", ":")) for item in decisions if item.get("outcome") == "ask"})


def _operation_requirements(decision, subject, *, hitl_type, payload, require_explicit_approval):
    requirements = _consent_requirements(decision)
    if subject.requires_approval or require_explicit_approval or hitl_type != HitlType.AUTHORIZE.value:
        review = {key: value for key, value in (payload or {}).items() if key not in _RECORD_LAYER_PAYLOAD_KEYS}
        requirements.append(json.dumps({
            "source": "human_review", "hitl_type": hitl_type,
            "declared_review": bool(subject.requires_approval),
            "explicit_review": bool(require_explicit_approval),
            "review_digest": hashlib.sha256(json.dumps(review, sort_keys=True, default=str).encode()).hexdigest(),
        }, sort_keys=True, separators=(",", ":")))
    return sorted(requirements)


async def resolve_approval(
    db,
    *,
    subject: ApprovalSubject,
    origin: ApprovalOrigin,
    permission_decision: PermissionDecision,
    spent_credits: Optional[dict] = None,
    reason: Optional[str] = None,
    intrinsic_rule: Optional[str] = None,
    intrinsic_reason: Optional[str] = None,
    intrinsic_high_risk_approved: bool = False,
    intrinsic_approval_preauthorized: bool = False,
    policy_hitl_preauthorized: bool = False,
    require_explicit_approval: bool = False,
    hitl_type: str = HitlType.AUTHORIZE.value,
    payload: Optional[dict] = None,
) -> ApprovalDecision:
    """The one decision. See module docstring.

    ``intrinsic_rule``/``intrinsic_reason`` let a plane label its own
    intrinsic (non-policy) trigger — e.g. the runtime guard's
    ``direct_chat_baseline``. Without them, step-plane ``step.*`` names are
    synthesized.

    ``intrinsic_high_risk_approved`` suppresses only the synthetic step-level
    high-risk prompt when an upstream governance action already approved this
    exact operation. Policy HITL/deny decisions and explicit
    ``requires_approval`` flags still apply.

    ``policy_hitl_preauthorized`` can satisfy a mode prompt for the exact
    operation. Explicit policy rules and step review remain authoritative.

    ``intrinsic_approval_preauthorized`` satisfies ``requires_approval`` for a
    verified execution origin. It cannot override permission or hard policy
    decisions, and callers must not use it for explicit human-review nodes.

    ``require_explicit_approval`` suspends automatic consent for this operation
    (for example a Proposal created while Workspace autonomy is paused).
    Permission/policy denials still win; only an exact one-time grant can proceed.

    ``hitl_type``/``payload`` let a caller that knows something this function
    cannot — e.g. the dispatcher gate, which knows the step already failed and
    why — mint an ``error`` card instead of the default ``authorize`` one.
    They are threaded into the mint so the row is written correctly the first
    time; writing them back in a second UPDATE would leave a window where the
    card renders as a bare "needs approval", which is the loop this phase
    exists to kill. Ignored when an existing open request is reused — the card
    already on screen wins.

    ``permission_decision`` is the caller's infra authorization result. A deny
    is final and mints no HITL request; policy and human consent are evaluated
    only inside an already-authorized actor/Agent envelope.
    """
    # 0. Actor / role / agent-binding permission is a hard boundary. HITL is
    # consent inside an existing authority envelope; it can never manufacture
    # authority the actor or delegated agent does not have.
    if not permission_decision.allowed:
        return ApprovalDecision(
            ApprovalOutcome.DENY,
            permission_decision.reason or "Runtime permission denied.",
            permission_decision.matched_rule or AuthorizationRule.DENIED.value,
        )

    if not origin.operation_id:
        operation_id = None
        if origin.kind == ApprovalOriginKind.STEP and origin.step_id:
            operation_id = f"step:{origin.plan_id or ''}:{origin.step_id}"
        elif subject.resource_kind in {"proposal", "proposal_item"} and subject.resource_id:
            operation_id = f"{subject.resource_kind}:{subject.resource_id}"
        elif origin.kind == ApprovalOriginKind.OPERATION and origin.context.get("draft_id"):
            operation_id = f"draft:{origin.context['draft_id']}"
        elif origin.kind == ApprovalOriginKind.CHANNEL and origin.message_id:
            operation_id = f"channel:{origin.message_id}"
        if operation_id:
            origin = replace(origin, operation_id=operation_id)

    decision = await _policy_decision(db, subject, origin, spent_credits=spent_credits)

    # 1. Hard block — never approvable, no request minted.
    if not decision.allowed and not decision.pause_for_hitl:
        return ApprovalDecision(
            ApprovalOutcome.DENY,
            decision.reason,
            decision.matched_rule,
        )

    current_requirements = set(_operation_requirements(decision, subject,
        hitl_type=hitl_type, payload=payload, require_explicit_approval=require_explicit_approval))
    # Existing decisions belong to an operation, not to the current mode. A
    # relaxed default cannot approve an already-pending human decision.
    key = dedup_key_for(subject, origin)
    req = await _find_operation_request(db, subject.entity_id, key) if origin.operation_id else await _find_open_request(db, subject.entity_id, key)
    if req is not None:
        if origin.operation_id and (
            req.action_key != subject.action_key
            or req.resource_id != subject.resource_id
            or (req.context or {}).get("args_hash") != origin.args_hash
        ):
            return ApprovalDecision(ApprovalOutcome.DENY, "The operation payload changed", "approval.operation_changed", req)
        if req.status in {ApprovalStatus.CONSUMED, ApprovalStatus.EXPIRED}:
            return ApprovalDecision(ApprovalOutcome.DENY, "This operation cannot be replayed", "approval.operation_terminal", req)
        if req.status == ApprovalStatus.GRANTED:
            if getattr(req, "hitl_type", None) == HitlType.ERROR.value:
                return ApprovalDecision(ApprovalOutcome.DENY,
                    "Retry the failed step through its recovery decision", "approval.operation_terminal", req)
            approved_requirements = set((req.context or {}).get("approval_requirements") or [])
            if current_requirements.issubset(approved_requirements):
                return ApprovalDecision(ApprovalOutcome.ALLOW, "operator approved", req.matched_rule, req)
            req.status = ApprovalStatus.EXPIRED.value
            req.resolved_reason = "approval_requirements_changed"
            await db.flush()
            req = None
        if req is not None:
            if req.status == ApprovalStatus.DENIED:
                return ApprovalDecision(ApprovalOutcome.DENY, req.reason or "operator rejected", req.matched_rule, req)
            return ApprovalDecision(ApprovalOutcome.NEEDS_HUMAN, req.reason, req.matched_rule, req)

    # Ordinary action consent comes entirely from the inherited evaluator.
    # Intrinsic gates are reserved for explicitly declared human/review nodes.
    applicable = (decision.explanation or {}).get("target_decisions") or [decision.explanation or {}]
    exact_grant_covers_baseline = policy_hitl_preauthorized and all(
        item.get("source") == "mode" for item in applicable if item.get("outcome") == "ask"
    ) and bool(decision.explanation)
    policy_hitl_needs_human = bool(decision.pause_for_hitl and not exact_grant_covers_baseline)
    needs_human = bool(
        require_explicit_approval
        or policy_hitl_needs_human
        or (subject.requires_approval and not intrinsic_approval_preauthorized)
        or hitl_type not in {HitlType.AUTHORIZE.value, HitlType.ERROR.value}
    )
    if not needs_human:
        return ApprovalDecision(ApprovalOutcome.ALLOW, decision.reason, decision.matched_rule)

    # Name the trigger so the caller can render the right card and pick the
    # right error type. A real policy HITL rule wins; otherwise the caller's
    # intrinsic label applies, falling back to synthetic "step.*" names —
    # parity with the pre-unification dispatcher gates, which the blueprint
    # report and the dispatcher tests key off ("step.*" ⇒ intrinsic step
    # approval, any other rule ⇒ a governance policy pause).
    subj = subject.action_key or subject.capability_id or subject.resource_kind or "action"
    if policy_hitl_needs_human and decision.matched_rule:
        hitl_rule, hitl_reason = decision.matched_rule, decision.reason
    elif intrinsic_rule:
        hitl_rule = intrinsic_rule
        hitl_reason = intrinsic_reason or f"Approval required for {subj!r}."
    elif subject.requires_approval:
        hitl_rule = "step.requires_approval"
        hitl_reason = f"Step requires operator approval before dispatching {subj!r}."
    else:  # subject.risk_level == high
        hitl_rule = "step.high_risk"
        hitl_reason = f"High-risk step needs one-time operator approval before dispatching {subj!r}."

    # A human approval needs a surface to render on: for steps that surface is
    # the workspace chat; for tool calls the conversation itself. A subject
    # with NEITHER has nowhere to show a card, so signal needs_human with NO
    # request — the caller fails closed rather than minting an unresolvable,
    # orphaned row.
    if not subject.workspace_id and not origin.conversation_id:
        return ApprovalDecision(
            ApprovalOutcome.NEEDS_HUMAN,
            hitl_reason,
            hitl_rule,
        )

    materialized_origin = await origin.materialize_context()
    materialized_origin = replace(materialized_origin, context={
        **materialized_origin.context,
        "approval_requirements": sorted(current_requirements),
        "policy_evaluation": decision.explanation,
    })
    req = await _create_pending_request(
        db,
        subject=subject,
        origin=materialized_origin,
        dedup_key=key,
        reason=reason or hitl_reason,
        matched_rule=hitl_rule,
        # Defaults to "authorize": this function IS the authorization
        # decision, so absent a caller that knows better every request it
        # mints is by construction an "authorize" ask.
        hitl_type=hitl_type,
        payload=payload,
    )
    return ApprovalDecision(
        ApprovalOutcome.NEEDS_HUMAN,
        req.reason,
        req.matched_rule,
        req,
    )


# ── request lifecycle ──────────────────────────────────────────────


async def find_requests_by_dedup(db, *, entity_id: str, dedup_key: str):
    """ALL requests ever minted for a dedup key, newest first — for callers
    whose semantics depend on terminal history (e.g. provider approvals:
    an already-decided registration must not re-mint)."""
    return (
        (
            await db.execute(
                select(HitlRequest)
                .where(
                    HitlRequest.entity_id == entity_id,
                    HitlRequest.dedup_key == dedup_key,
                )
                .order_by(HitlRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


def validate_hitl_payload(hitl_type: str, payload: Optional[dict]) -> dict:
    """Reject a card that cannot answer what / why / what-to-do.

    Empty payload is allowed and skips the required-field check: callers
    predating the type system still render from ``reason``. Once every
    producer supplies a payload this should become unconditional — drop the
    early return and require ``HITL_REQUIRED_PAYLOAD_FIELDS`` for every
    minted request.

    ``authorize`` always comes back carrying a ``scope``, empty payload or
    not. The two affirmative buttons on an approval card ask two different
    questions (this instance vs. a standing capability grant) and the record
    never said which; defaulting here — at the ONE place a payload is
    normalized — means every authorize row answers that, and no producer has
    to know the field exists.

    Exported (not underscored) because the record layer's rule has to be
    enforceable by producers that build a CARD without minting a row — the
    workspace-operation review card is one — or "``review`` requires a diff"
    holds only on the path nothing takes.
    """
    validated = dict(payload or {})
    # "Did the producer supply copy?" — which is what the required-field
    # contract is about. The record layer stamps keys of its own (scope, and
    # the promotion bookkeeping); counting those as producer copy would make
    # validation non-idempotent, and this function runs twice on the mint path.
    substantive = {key: value for key, value in validated.items() if key not in _RECORD_LAYER_PAYLOAD_KEYS}
    if substantive:
        required = HITL_REQUIRED_PAYLOAD_FIELDS.get(hitl_type, ())
        missing = [key for key in required if not substantive.get(key)]
        if missing:
            raise ValueError(f"hitl_type={hitl_type!r} payload missing required field(s): {', '.join(missing)}")
    if hitl_type == HitlType.AUTHORIZE.value:
        scope = str(validated.get(PAYLOAD_KEY_SCOPE) or "").strip()
        if scope and scope not in AuthorizeScope.values():
            raise ValueError(
                f"authorize payload has unknown scope {scope!r}; expected one of {AuthorizeScope.values()}"
            )
        # A request is minted because someone asked to do ONE thing. Standing
        # scope is only ever reached by promotion (see grant_approval), never
        # by a producer declaring it up front.
        validated[PAYLOAD_KEY_SCOPE] = scope or AuthorizeScope.ACTION.value
    return validated


#: Back-compat alias — this module used the private spelling before the
#: card-only producers needed it.
_validate_hitl_payload = validate_hitl_payload


async def mint_approval_request(
    db,
    *,
    subject: ApprovalSubject,
    origin: ApprovalOrigin,
    dedup_key: Optional[str] = None,
    reason: Optional[str] = None,
    matched_rule: Optional[str] = None,
    hitl_type: str = HitlType.AUTHORIZE.value,
    payload: Optional[dict] = None,
) -> HitlRequest:
    """Find-open-or-create for planes that mint requests outside a policy
    decision (e.g. provider-required approvals). Dedup semantics match
    resolve_approval's.

    ``hitl_type``/``payload`` say what kind of human involvement this is and
    carry the copy that answers what / why / what-to-do. They are validated
    at the record layer's single mint entry point, so an unanswerable card
    is never written in the first place.
    """
    key = dedup_key or dedup_key_for(subject, origin)
    # Validate BEFORE the find-open short-circuit: a malformed payload is a
    # programming error and must surface whether or not an open request for
    # this key happens to already exist.
    validated_payload = _validate_hitl_payload(hitl_type, payload)
    existing = await _find_open_request(db, subject.entity_id, key)
    if existing is not None:
        return existing
    materialized_origin = await origin.materialize_context()
    return await _create_pending_request(
        db,
        subject=subject,
        origin=materialized_origin,
        dedup_key=key,
        reason=reason,
        matched_rule=matched_rule,
        hitl_type=hitl_type,
        payload=validated_payload,
    )


async def _find_operation_request(db, entity_id: str, dedup_key: str):
    return (await db.execute(select(HitlRequest).where(
        HitlRequest.entity_id == entity_id, HitlRequest.dedup_key == dedup_key,
    ).order_by(HitlRequest.created_at.desc(), HitlRequest.id.desc()).limit(1).with_for_update())).scalar_one_or_none()


async def _find_open_request(
    db,
    entity_id: str,
    dedup_key: str,
    *,
    for_update: bool = False,
) -> Optional[HitlRequest]:
    """The active request for a subject: still-pending OR granted-but-unconsumed.
    A granted request must remain findable so the next resolve honors it (else
    the grant is invisible and the gate re-requests — the loop we're fixing)."""
    stmt = (
        select(HitlRequest)
        .where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.dedup_key == dedup_key,
            HitlRequest.status.in_([s.value for s in APPROVAL_LIVE_STATUSES]),
        )
        .order_by(HitlRequest.created_at.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _find_live_requests_for_step_origin(
    db,
    entity_id: str,
    step_id: str,
    *,
    for_update: bool = False,
) -> list[HitlRequest]:
    """Return every live request that pauses this exact execution Step."""
    stmt = (
        select(HitlRequest)
        .where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.origin_step_id == step_id,
            HitlRequest.status.in_([s.value for s in APPROVAL_LIVE_STATUSES]),
        )
        .order_by(HitlRequest.created_at.desc())
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt)).scalars().all())


async def _create_pending_request(
    db,
    *,
    subject: ApprovalSubject,
    origin: ApprovalOrigin,
    dedup_key: str,
    reason: Optional[str],
    matched_rule: Optional[str],
    hitl_type: str = HitlType.AUTHORIZE.value,
    payload: Optional[dict] = None,
) -> HitlRequest:
    # Validated here as well as in mint_approval_request: this is the one
    # function that actually constructs the row, so every creating path —
    # including resolve_approval's — passes through this check.
    payload = _validate_hitl_payload(hitl_type, payload)
    context = dict(origin.context or {})
    if origin.args_hash:
        context["args_hash"] = origin.args_hash
    if origin.operation_id:
        context["operation_id"] = origin.operation_id
    context["approval_account_id"] = subject.account_id
    # Only the server's classified subject may define a multi-resource grant.
    # Origin context can contain user/tool data and must not widen promotion.
    context.pop(_APPROVAL_RESOURCE_IDS_CONTEXT_KEY, None)
    if subject.resource_ids:
        context[_APPROVAL_RESOURCE_IDS_CONTEXT_KEY] = list(subject.resource_ids)
    req = HitlRequest(
        entity_id=subject.entity_id,
        workspace_id=subject.workspace_id,
        action_key=subject.action_key,
        capability_id=subject.capability_id,
        resource_kind=subject.resource_kind,
        resource_id=subject.resource_id,
        risk_level=subject.risk_level,
        origin_kind=origin.kind,
        origin_conversation_id=origin.conversation_id,
        origin_message_id=origin.message_id,
        origin_step_id=origin.step_id,
        origin_plan_id=origin.plan_id,
        origin_task_id=origin.task_id,
        status=ApprovalStatus.PENDING.value,
        dedup_key=dedup_key,
        reason=reason,
        matched_rule=matched_rule,
        hitl_type=hitl_type,
        payload=payload,
        context=context,
    )
    try:
        # A dedup race is local to this insert.  Keep it inside a SAVEPOINT so
        # losing the partial-unique-index race cannot roll back the caller's
        # Task/Plan/Lease locks or mutations (lease_needs_human relies on those
        # ownership fences while it links the winning request).
        async with db.begin_nested():
            db.add(req)
            await db.flush()
    except IntegrityError:
        # Lost a race to the partial-unique dedup index — reuse the winner.
        existing = await _find_open_request(db, subject.entity_id, dedup_key)
        if existing is not None:
            return existing
        raise
    # Ledger (M1): a NEW pending request was minted (reuse paths return above).
    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et

    await ledger_adapters.record_approval_event(db, req, ledger_et.APPROVAL_REQUESTED)
    return req


async def grant_approval(
    db,
    request: HitlRequest,
    *,
    by_user_id: Optional[str],
    via: str,
    standing: bool = False,
    changed_by: Optional[str] = None,
    authority_prechecked: bool = False,
    _coordinator_mirror: bool = False,
) -> HitlRequest:
    """Approve a request.

    ``standing=True`` is the **promotion**: the request was minted at
    ``authorize`` scope ``action`` — "may I do this one thing, with the
    content you just read?" — and the user answered a bigger question,
    "you may do this class of thing from now on". It writes the subject into
    the workspace policy auto-approve set (the unified "Always" store both
    planes honor) and records the widened scope on the row, so the audit
    trail says which of the two questions was actually answered.

    The promotion is never second-guessed by capability class after the actor's
    ``manage_standing_grants`` authority is verified. ``never_allow`` remains
    the governance hard block.

    ``authority_prechecked`` is reserved for internal lifecycle mirrors whose
    parent decision already performed the same authority check. User-facing
    entry points must leave it false so this state transition remains the
    enforcement backstop.
    """
    if standing:
        raise ValueError("Always approval requires a versioned approval-settings command")
    if (
        not _coordinator_mirror
        and await is_coordinator_owned_nested_approval_durable(db, request)
    ):
        raise ApprovalContinuationBindingError(
            "Resolve this delegated approval through its canonical Task approval."
        )
    if standing and str(request.hitl_type or "").strip().lower() != HitlType.AUTHORIZE.value:
        raise ValueError("standing approval is only valid for authorize requests")
    if request.status not in APPROVAL_LIVE_STATUSES:
        return request
    if not authority_prechecked:
        from packages.core.services.runtime_authorization import (
            authorize_hitl_resolution,
        )

        authority = await authorize_hitl_resolution(
            db,
            request=request,
            by_user_id=by_user_id,
            standing=standing,
        )
        if not authority.allowed:
            raise ApprovalAuthorityError(authority)
    nested_request = await _coordinator_nested_request_for_mirror(db, request)
    if nested_request is not None:
        if nested_request.status != ApprovalStatus.PENDING.value:
            raise ApprovalContinuationBindingError(
                "Delegated Runtime approval is no longer available to grant."
            )
        # The outer lease card is a Task-resume surface, not authority for an
        # arbitrary tool. Recheck the actor against the nested tool subject
        # before mirroring; only the object identity is inherited.
        from packages.core.services.runtime_authorization import (
            authorize_hitl_resolution,
        )

        nested_authority = await authorize_hitl_resolution(
            db,
            request=nested_request,
            by_user_id=by_user_id,
            standing=standing,
        )
        if not nested_authority.allowed:
            raise ApprovalAuthorityError(nested_authority)
    request.status = ApprovalStatus.GRANTED.value
    request.decided_by_user_id = by_user_id
    request.decided_at = datetime.now(timezone.utc)
    request.decided_via = via
    request.resolved_reason = "approved"

    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et

    await ledger_adapters.record_approval_event(
        db,
        request,
        ledger_et.APPROVAL_GRANTED,
        actor_id=by_user_id,
    )

    if nested_request is not None:
        await grant_approval(
            db,
            nested_request,
            by_user_id=by_user_id,
            via=f"{via}:coordinator_child",
            standing=standing,
            changed_by=changed_by,
            authority_prechecked=True,
            _coordinator_mirror=True,
        )
        if nested_request.origin_conversation_id:
            from packages.core.ai.runtime.approval_store import (
                mark_runtime_hitl_request_resolved,
            )

            await mark_runtime_hitl_request_resolved(
                db,
                conversation_id=nested_request.origin_conversation_id,
                hitl_id=nested_request.id,
                choice="always_approve" if standing else "approve",
            )

    return request


async def grant_open_request_for_step(
    db,
    *,
    entity_id: str,
    step_id: str,
    by_user_id: Optional[str],
    via: str = "step_resume",
    expected_hitl_type: Optional[str] = None,
) -> Optional[HitlRequest]:
    """Grant the open request a step is paused on, if any.

    Explicit HITL surfaces that "resume" a waiting step call this so that
    decision is recorded as the approval — without it the
    dispatcher gate re-pauses the reparked step on its still-pending request,
    which is exactly the "I already resumed/approved it" loop (#317).

    Generic Task/Plan retry controls must not call this helper: retrying an
    execution failure is not consent for a pending human decision.

    Request-backed reviews, recovery errors, and non-Step origins stay on their
    dedicated Chat surfaces. Requestless human Steps are classified by their
    typed payload: plain input may be answered here, while reviews require an
    explicit verdict. Resolving an input card preserves the Chat lock order
    (Message -> Step).
    """
    requests = await _find_live_requests_for_step_origin(
        db,
        entity_id,
        step_id,
        for_update=True,
    )
    if expected_hitl_type is not None:
        expected = str(expected_hitl_type).strip().lower()
        mismatched_request = next(
            (
                candidate
                for candidate in requests
                if str(candidate.hitl_type or HitlType.AUTHORIZE.value)
                .strip()
                .lower()
                != expected
            ),
            None,
        )
        if mismatched_request is not None:
            actual_hitl_type = str(
                mismatched_request.hitl_type or HitlType.AUTHORIZE.value
            ).strip().lower()
            if actual_hitl_type == HitlType.REVIEW.value:
                detail = (
                    "This review requires an explicit approve, request-changes, "
                    "or reject decision."
                )
            elif actual_hitl_type == HitlType.ERROR.value:
                detail = (
                    "This failed step requires an explicit retry or cancel "
                    "decision on its recovery card."
                )
            else:
                detail = (
                    "This step is waiting for a dedicated Workspace Chat decision; "
                    "respond to that card to continue."
                )
            raise ApprovalDecisionRequiredError(
                detail
            )
    dedicated_request = next(
        (
            candidate
            for candidate in requests
            if candidate.origin_kind != ApprovalOriginKind.STEP.value
        ),
        None,
    )
    if dedicated_request is not None:
        raise ApprovalDecisionRequiredError(
            "This step is waiting for a dedicated Workspace Chat card; "
            "respond to that card to continue."
        )
    typed_request = next(
        (
            candidate
            for candidate in requests
            if str(candidate.hitl_type or "").strip().lower()
            in {HitlType.REVIEW.value, HitlType.ERROR.value}
        ),
        None,
    )
    if typed_request is not None:
        typed_hitl_type = str(typed_request.hitl_type or "").strip().lower()
        if typed_hitl_type == HitlType.REVIEW.value:
            raise ApprovalDecisionRequiredError(
                "This review requires an explicit approve, request-changes, "
                "or reject decision."
            )
        raise ApprovalDecisionRequiredError(
            "This failed step requires an explicit retry or cancel decision "
            "on its recovery card."
        )
    req = requests[0] if requests else None
    if req is None:
        # Requestless human Steps still require typed authority. Reviews cannot
        # be collapsed into a generic resume because that would erase the
        # user's actual decision; ordinary input can be recorded as answered.
        from packages.core.models.execution import ExecutionStep
        from packages.core.services.runtime_authorization import (
            authorize_hitl_action,
        )

        origin_step = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.id == step_id,
                ExecutionStep.entity_id == entity_id,
            )
        )).scalar_one_or_none()
        if origin_step is not None:
            from packages.core.services.hitl_options import human_step_hitl_type

            hitl_type = human_step_hitl_type(origin_step.params)
            if origin_step.requires_approval and hitl_type == HitlType.INPUT.value:
                hitl_type = HitlType.AUTHORIZE.value
            if expected_hitl_type is not None and hitl_type != expected:
                raise ApprovalDecisionRequiredError(
                    "This step is waiting for a dedicated Workspace Chat decision; "
                    "respond to that card to continue."
                )
            if hitl_type == HitlType.REVIEW.value:
                raise ApprovalDecisionRequiredError(
                    "This review requires an explicit approve, request-changes, "
                    "or reject decision."
                )
            authority = await authorize_hitl_action(
                db,
                entity_id=entity_id,
                workspace_id=origin_step.workspace_id,
                by_user_id=by_user_id,
                action_key=origin_step.action_key,
                capability_id=origin_step.capability_id,
                hitl_type=hitl_type,
            )
            if not authority.allowed:
                raise ApprovalAuthorityError(authority)

        from packages.core.governance.service import resolve_stale_hitl_cards

        await resolve_stale_hitl_cards(
            db,
            step_ids=[step_id],
            reason=f"answered_via_{via}",
            choice="answered",
            pending_action_kinds=[PendingActionKind.HUMAN_INPUT.value],
            for_update=True,
        )
        return None

    from packages.core.services.step_resume import (
        lock_waiting_step_for_decision,
    )

    waiting_step = await lock_waiting_step_for_decision(
        db,
        entity_id=entity_id,
        workspace_id=req.workspace_id,
        task_id=req.origin_task_id,
        plan_id=req.origin_plan_id,
        step_id=step_id,
    )
    if waiting_step is None:
        return None
    if req.status == ApprovalStatus.PENDING:
        await grant_approval(db, req, by_user_id=by_user_id, via=via)
    from packages.core.governance.service import resolve_stale_hitl_cards

    await resolve_stale_hitl_cards(
        db,
        step_ids=[step_id],
        reason=f"approved_via_{via}",
        choice="approved",
    )
    return req


async def deny_approval(
    db,
    request: HitlRequest,
    *,
    by_user_id: Optional[str],
    via: str,
    reason: Optional[str] = None,
    authority_prechecked: bool = False,
    _coordinator_mirror: bool = False,
) -> HitlRequest:
    if (
        not _coordinator_mirror
        and await is_coordinator_owned_nested_approval_durable(db, request)
    ):
        raise ApprovalContinuationBindingError(
            "Resolve this delegated approval through its canonical Task approval."
        )
    if request.status not in (ApprovalStatus.PENDING,):
        return request
    if not authority_prechecked:
        from packages.core.services.runtime_authorization import (
            authorize_hitl_resolution,
        )

        authority = await authorize_hitl_resolution(
            db,
            request=request,
            by_user_id=by_user_id,
        )
        if not authority.allowed:
            raise ApprovalAuthorityError(authority)
    nested_request = await _coordinator_nested_request_for_mirror(db, request)
    if nested_request is not None:
        if nested_request.status != ApprovalStatus.PENDING.value:
            raise ApprovalContinuationBindingError(
                "Delegated Runtime approval is no longer available to deny."
            )
        from packages.core.services.runtime_authorization import (
            authorize_hitl_resolution,
        )

        nested_authority = await authorize_hitl_resolution(
            db,
            request=nested_request,
            by_user_id=by_user_id,
        )
        if not nested_authority.allowed:
            raise ApprovalAuthorityError(nested_authority)
    request.status = ApprovalStatus.DENIED.value
    request.decided_by_user_id = by_user_id
    request.decided_at = datetime.now(timezone.utc)
    request.decided_via = via
    request.resolved_reason = "rejected"
    if reason:
        request.reason = reason

    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et

    await ledger_adapters.record_approval_event(
        db,
        request,
        ledger_et.APPROVAL_DENIED,
        actor_id=by_user_id,
    )
    if nested_request is not None:
        await deny_approval(
            db,
            nested_request,
            by_user_id=by_user_id,
            via=f"{via}:coordinator_child",
            reason=reason,
            authority_prechecked=True,
            _coordinator_mirror=True,
        )
        if nested_request.origin_conversation_id:
            from packages.core.ai.runtime.approval_store import (
                mark_runtime_hitl_request_resolved,
            )

            await mark_runtime_hitl_request_resolved(
                db,
                conversation_id=nested_request.origin_conversation_id,
                hitl_id=nested_request.id,
                choice="reject",
            )
    return request


async def _claim_granted_approval_row(
    db,
    *,
    request_id: str,
    consumed_at: datetime,
) -> tuple[ApprovalConsumeOutcome, str | None, datetime | None]:
    """Compare-and-swap one grant without holding a cross-resource lock.

    PostgreSQL rechecks the ``status = granted`` predicate after a concurrent
    updater commits.  Consequently exactly one transaction receives a row from
    ``RETURNING``; every contender observes the winner's terminal state.
    """

    claimed = await db.execute(
        update(HitlRequest)
        .where(
            HitlRequest.id == request_id,
            HitlRequest.status == ApprovalStatus.GRANTED.value,
        )
        .values(
            status=ApprovalStatus.CONSUMED.value,
            consumed_at=consumed_at,
        )
        .returning(HitlRequest.status, HitlRequest.consumed_at)
        .execution_options(synchronize_session=False)
    )
    claimed_row = claimed.one_or_none()
    if claimed_row is not None:
        return (
            ApprovalConsumeOutcome.CONSUMED,
            claimed_row.status,
            claimed_row.consumed_at,
        )

    current = await db.execute(
        select(HitlRequest.status, HitlRequest.consumed_at).where(
            HitlRequest.id == request_id,
        )
    )
    current_row = current.one_or_none()
    if current_row is None:
        return ApprovalConsumeOutcome.NOT_GRANTED, None, None
    if current_row.status == ApprovalStatus.CONSUMED.value:
        return (
            ApprovalConsumeOutcome.ALREADY_CONSUMED,
            current_row.status,
            current_row.consumed_at,
        )
    return (
        ApprovalConsumeOutcome.NOT_GRANTED,
        current_row.status,
        current_row.consumed_at,
    )


def _set_approval_snapshot_value(request: HitlRequest, key: str, value: Any) -> None:
    """Refresh an ORM object without scheduling an unconditional UPDATE."""

    if hasattr(request, "_sa_instance_state"):
        set_committed_value(request, key, value)
    else:  # Lightweight request doubles used by boundary unit tests.
        setattr(request, key, value)


async def claim_approval_consumption(
    db,
    request: HitlRequest,
) -> ApprovalConsumeOutcome:
    """Atomically claim a one-time approval for the current transaction.

    This function does not commit.  A caller immediately preceding provider
    I/O must commit the winning claim before performing that I/O, and may
    proceed only when ``CONSUMED`` is returned.
    """

    consumed_at = datetime.now(timezone.utc)
    outcome, current_status, persisted_consumed_at = (
        await _claim_granted_approval_row(
            db,
            request_id=request.id,
            consumed_at=consumed_at,
        )
    )
    if current_status is not None:
        _set_approval_snapshot_value(request, "status", current_status)
        _set_approval_snapshot_value(
            request,
            "consumed_at",
            persisted_consumed_at,
        )
    if outcome is not ApprovalConsumeOutcome.CONSUMED:
        return outcome

    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et

    await ledger_adapters.record_approval_event(
        db,
        request,
        ledger_et.APPROVAL_CONSUMED,
    )
    # A Runtime tool preflight may be capturing the exact server-owned grant
    # that authorized its next provider call. This is emitted from the unified
    # consumption boundary, never reconstructed from a model-authored token.
    from packages.core.ai.runtime.tool_effect_intents import (
        runtime_record_consumed_approval_request_id,
    )

    runtime_record_consumed_approval_request_id(request.id)
    return outcome


async def consume_approval(db, request: HitlRequest) -> HitlRequest:
    """Spend a one-time grant, preserving the historical success return type.

    The legacy no-op for a request that is not granted remains compatible, but
    a replay of an already consumed grant is explicit instead of silently
    looking successful. Callers that must authorize external I/O should use
    :func:`claim_approval_consumption` and branch on every outcome directly.
    """

    outcome = await claim_approval_consumption(db, request)
    if outcome is ApprovalConsumeOutcome.ALREADY_CONSUMED:
        raise ApprovalConsumptionError(request.id, outcome)
    return request


async def resolve_origin_requests(
    db,
    *,
    step_id: Optional[str] = None,
    task_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    reason: str = "origin_terminal",
    exclude_request_ids: Optional[set[str]] = None,
) -> int:
    """Expire every OPEN request attached to a terminal origin — the fix for
    orphaned cards. Covers granted-but-unconsumed rows too: once the origin
    is terminal nothing may run anymore, so a lingering grant must be revoked
    (fail safe) rather than left findable forever. Returns how many closed."""
    from packages.core.constants.execution import (
        COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY,
        COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
    )

    conds = []
    if step_id:
        conds.append(HitlRequest.origin_step_id == step_id)
        conds.append(
            HitlRequest.context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY][
                "parent_step_id"
            ].astext
            == step_id
        )
    if task_id:
        conds.append(HitlRequest.origin_task_id == task_id)
        conds.append(
            HitlRequest.context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY][
                "task_id"
            ].astext
            == task_id
        )
    if plan_id:
        from packages.core.models.execution import ExecutionStep

        conds.append(HitlRequest.origin_plan_id == plan_id)
        # Runtime tool approvals are minted with the child Step id before the
        # coordinator can attach its outer-link context. Plan-terminal cleanup
        # must still find that markerless request after a crash in between.
        conds.append(
            HitlRequest.origin_step_id.in_(
                select(ExecutionStep.id).where(ExecutionStep.plan_id == plan_id)
            )
        )
        conds.append(
            HitlRequest.context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY][
                "plan_id"
            ].astext
            == plan_id
        )
    if not conds:
        return 0
    from sqlalchemy import or_

    preserved = {str(item) for item in (exclude_request_ids or set()) if item}
    discovery = (
        select(HitlRequest.id, HitlRequest.context)
        .where(
            HitlRequest.status.in_([s.value for s in APPROVAL_LIVE_STATUSES]),
            or_(*conds),
        )
        .order_by(HitlRequest.id)
    )
    if preserved:
        discovery = discovery.where(HitlRequest.id.not_in(sorted(preserved)))
    discovered = list((await db.execute(discovery)).all())
    candidate_ids = {str(row.id) for row in discovered}
    for row in discovered:
        context = row.context if isinstance(row.context, dict) else {}
        nested_binding = context.get(COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY)
        outer_binding = context.get(COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY)
        if isinstance(nested_binding, dict):
            linked_id = str(nested_binding.get("request_id") or "").strip()
            if linked_id:
                candidate_ids.add(linked_id)
        if isinstance(outer_binding, dict):
            linked_id = str(outer_binding.get("request_id") or "").strip()
            if linked_id:
                candidate_ids.add(linked_id)
    candidate_ids.difference_update(preserved)
    if not candidate_ids:
        return 0
    # Discovery grants no mutation authority. Reacquire the complete linked
    # request set in one stable id order so reciprocal outer/nested cleanup
    # cannot deadlock by each locking one half of the pair first.
    rows = list((await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.id.in_(sorted(candidate_ids)),
            HitlRequest.status.in_([s.value for s in APPROVAL_LIVE_STATUSES]),
        )
        .order_by(HitlRequest.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    now = datetime.now(timezone.utc)
    if rows:
        from packages.core.ledger import adapters as ledger_adapters
        from packages.core.ledger import event_types as ledger_et
    for r in rows:
        r.status = ApprovalStatus.EXPIRED.value
        r.resolved_reason = reason
        r.decided_at = now
        # Ledger (M1): an origin-expired request is a workspace fact too.
        await ledger_adapters.record_approval_event(db, r, ledger_et.APPROVAL_EXPIRED)
    return len(rows)


async def count_open_requests(db, *, workspace_id: str) -> int:
    """Badge count = open, still-attached requests for the workspace."""
    rows = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.workspace_id == workspace_id,
            HitlRequest.status == ApprovalStatus.PENDING,
            ~HitlRequest.context.has_key(  # type: ignore[attr-defined]
                COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY
            ),
        )
    )).scalars().all())
    count = 0
    for request in rows:
        if await is_coordinator_owned_nested_approval_durable(db, request):
            continue
        count += 1
    return count


async def count_open_requests_by_workspace(
    db,
    *,
    workspace_ids: list[str],
) -> dict[str, int]:
    """Batch variant for the workspace-list sidebar stats."""
    if not workspace_ids:
        return {}
    rows = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.workspace_id.in_(workspace_ids),
            HitlRequest.status == ApprovalStatus.PENDING,
            ~HitlRequest.context.has_key(  # type: ignore[attr-defined]
                COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY
            ),
        )
    )).scalars().all())
    counts: dict[str, int] = {}
    for request in rows:
        if await is_coordinator_owned_nested_approval_durable(db, request):
            continue
        workspace_key = str(request.workspace_id or "")
        counts[workspace_key] = counts.get(workspace_key, 0) + 1
    return counts
