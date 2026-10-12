"""Durable operator reconciliation for ambiguous external actions."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
from packages.core.constants.tool_effects import (
    EffectReconciliationDecision,
    ToolEffectEvidenceKind,
    ToolEffectIntentState,
    ToolEffectOutcome,
    ToolEffectSettlementOutcome,
)
from packages.core.contracts.tool_effects import (
    EffectReconciliationDecisionFactory,
    ToolEffectEvidenceFactory,
)
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    STEP_TERMINAL_STATUSES,
    WorkLeaseStatus,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.task import TaskStatus
from packages.core.dispatcher.effect_safety import (
    ambiguous_effect_reconciliation_snapshot,
    canonical_effect_digest,
    effect_action_group_ids,
)
from packages.core.dispatcher.validation import SchemaError, validate_step_output
from packages.core.governance.approvals import (
    consume_approval,
    deny_approval,
    grant_approval,
)
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Workspace
from packages.core.plans.step_reset import reset_never_ran_downstream_steps
from packages.core.services.task_state_machine import apply_task_status_transition


class EffectReconciliationError(ValueError):
    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        commit_refreshed_card: bool = False,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.commit_refreshed_card = commit_refreshed_card


@dataclass(frozen=True)
class EffectReconciliationResult:
    plan_id: str
    task_id: str | None
    reset_step_ids: tuple[str, ...]
    completed_step_ids: tuple[str, ...]
    replayed: bool = False
    should_dispatch: bool = True


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _decision_fingerprint(
    *,
    request_id: str,
    plan_id: str,
    decisions: dict[str, str],
    supplied_results: dict[str, Any],
    note: str | None,
) -> str:
    """Bind an HTTP retry to the exact operator decision and result payload."""

    return _canonical_sha256({
        "request_id": request_id,
        "plan_id": plan_id,
        "decision_by_digest": {
            str(digest): str(decisions[digest]) for digest in sorted(decisions)
        },
        "supplied_results": {
            str(digest): supplied_results[digest]
            for digest in sorted(supplied_results)
        },
        "note": str(note or ""),
    })


def _replayed_result(
    *,
    request: HitlRequest,
    resolution: Any,
    decision_fingerprint: str,
    user_id: str,
) -> EffectReconciliationResult | None:
    """Return a trusted prior result for a byte-equivalent retried decision."""

    if not isinstance(resolution, dict) or resolution.get("version") != 2:
        return None
    expected_status = (
        ApprovalStatus.DENIED.value
        if resolution.get("cancelled") is True
        else ApprovalStatus.CONSUMED.value
    )
    if (
        request.status != expected_status
        or str(resolution.get("request_id") or "") != str(request.id)
        or str(resolution.get("entity_id") or "") != str(request.entity_id)
        or str(resolution.get("workspace_id") or "")
        != str(request.workspace_id or "")
        or str(resolution.get("origin_plan_id") or "")
        != str(request.origin_plan_id or "")
        or str(resolution.get("resolved_by") or "") != str(user_id)
        or str(resolution.get("decision_fingerprint") or "")
        != decision_fingerprint
    ):
        return None
    result_plan_id = str(resolution.get("result_plan_id") or "")
    if not result_plan_id:
        return None
    return EffectReconciliationResult(
        plan_id=result_plan_id,
        task_id=(
            str(resolution["task_id"])
            if resolution.get("task_id") is not None
            else None
        ),
        reset_step_ids=tuple(
            sorted(str(item) for item in (resolution.get("reset_step_ids") or []))
        ),
        completed_step_ids=tuple(
            sorted(str(item) for item in (resolution.get("completed_step_ids") or []))
        ),
        replayed=True,
        should_dispatch=(
            bool(resolution["should_dispatch"])
            if isinstance(resolution.get("should_dispatch"), bool)
            else resolution.get("cancelled") is not True
        ),
    )


def _effect_refs(step: ExecutionStep) -> list[dict[str, Any]]:
    return [
        ref
        for ref in (step.evidence_refs or [])
        if (
            isinstance(ref, dict)
            and ref.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
        )
    ]


def _parse_single_note_result(note: str | None) -> Any:
    text = str(note or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _marker(
    *,
    digest: str,
    decision: str,
    generation: int,
    request_id: str,
    user_id: str,
    plan_id: str,
    recorded_on_step_id: str,
    note: str | None,
) -> dict[str, Any]:
    return ToolEffectEvidenceFactory.reconciliation({
            "effect_digest": digest,
            "decision": decision,
            "generation": generation,
            "approval_request_id": request_id,
            "user_id": user_id,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "plan_id": plan_id,
            "recorded_on_step_id": recorded_on_step_id,
            **({"note": note[:2000]} if note else {}),
    })


def _settlement_for(
    effect: dict[str, Any],
    marker: dict[str, Any],
    *,
    applied_result: Any = None,
) -> dict[str, Any]:
    fields = effect.get("fields") if isinstance(effect.get("fields"), dict) else {}
    marker_fields = marker["fields"]
    applied = (
        marker_fields["decision"] == EffectReconciliationDecision.APPLIED.value
    )
    settlement = ToolEffectEvidenceFactory.settlement(
        (
            ToolEffectSettlementOutcome.OPERATOR_CONFIRMED_APPLIED
            if applied
            else ToolEffectSettlementOutcome.OPERATOR_CONFIRMED_NOT_APPLIED
        ),
        {
            key: fields.get(key)
            for key in (
                "tool",
                "arguments_hash",
                "tool_call_id",
                "effect_generation",
                "effect_id",
                "owner_plan_id",
                "owner_step_id",
                "owner_task_id",
                "owner_service_key",
                "owner_subscription_id",
                "owner_idempotency_key",
                "provider",
                "action_key",
                "integration_id",
            )
            if fields.get(key) is not None
        } | {
            "decision": marker_fields["decision"],
            "approval_request_id": marker_fields["approval_request_id"],
            "generation": marker_fields["generation"],
            "effect_digest": marker_fields["effect_digest"],
            **({"delivery_confirmed": True} if applied else {}),
        },
        coordinator_child_step_id=(
            str(effect["coordinator_child_step_id"])
            if effect.get("coordinator_child_step_id")
            else None
        ),
    )
    if applied:
        settlement["fields"]["result_sha256"] = hashlib.sha256(json.dumps(
            applied_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
    return settlement


def _terminal_effect_for_applied(
    effect: dict[str, Any],
    marker: dict[str, Any],
    result: Any,
) -> dict[str, Any]:
    """Preserve an applied effect after its pending intent is settled."""

    fields = effect.get("fields") if isinstance(effect.get("fields"), dict) else {}
    return ToolEffectEvidenceFactory.effect(
        ToolEffectOutcome.OPERATOR_CONFIRMED,
        {
            **fields,
            "intent_state": ToolEffectIntentState.OPERATOR_CONFIRMED_APPLIED.value,
            "delivery_confirmed": True,
            # The receipt is a server-derived replacement for the exact
            # pending effect above.  Keep its source digest explicit: the
            # receipt's own canonical JSON necessarily differs once the
            # operator result and reconciliation metadata are attached.
            "source_effect_digest": marker["fields"]["effect_digest"],
            "effect_digest": marker["fields"]["effect_digest"],
            "approval_request_id": marker["fields"]["approval_request_id"],
            "result": deepcopy(result),
        },
        coordinator_child_step_id=(
            str(effect["coordinator_child_step_id"])
            if effect.get("coordinator_child_step_id")
            else None
        ),
    )


def _append_server_evidence(
    existing: Any,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append trusted reconciliation evidence and settle exact pending intents."""

    return ToolEffectEvidenceFactory.append_server_owned(existing, incoming)


def _reset_step(step: ExecutionStep) -> None:
    from packages.core.plans.step_reset import renew_step_approval_intent
    renew_step_approval_intent(step)
    step.step_status = ExecutionStepStatus.PENDING.value
    step.current_lease_id = None
    step.error = None
    step.last_execution_error = None
    step.result = None
    step.runtime_checkpoint = None
    step.human_input_prompt = None
    step.human_input_response = None
    step.started_at = None
    step.finished_at = None
    step.attempt_count = 0


def _fence_plan_execution(
    *,
    plan: ExecutionPlan,
    leases: list[WorkLease],
    steps: list[ExecutionStep],
    approval_request_id: str,
    cancel_steps: bool,
) -> set[str]:
    """Revoke every live claim before terminalizing or parking a Plan.

    Reconciliation may resolve an older Plan while changing its current
    successor.  The successor rows are locked with the source rows; clearing
    only the source lease would let a stale successor worker commit after the
    Plan/Task was cancelled or parked.
    """

    fenced_lease_ids: set[str] = set()
    for lease in leases:
        if lease.plan_id != plan.id:
            continue
        claim_is_live = bool(str(lease.execution_claim_id or "").strip())
        if (
            lease.status
            not in {
                WorkLeaseStatus.ACTIVE.value,
                WorkLeaseStatus.NEEDS_HUMAN.value,
            }
            and not (
                claim_is_live
                and lease.status
                not in {
                    WorkLeaseStatus.COMPLETED.value,
                    WorkLeaseStatus.CANCELLED.value,
                }
            )
        ):
            continue
        fenced_lease_ids.add(lease.id)
        lease.status = WorkLeaseStatus.CANCELLED.value
        lease.credential_leases = []
        lease.execution_claim_id = None
        lease.error = {
            "type": "EffectReconciliationPlanFence",
            "approval_request_id": approval_request_id,
            "plan_id": plan.id,
        }

    cancelled_step_ids: set[str] = set()
    terminal_step_values = {status.value for status in STEP_TERMINAL_STATUSES}
    for step in steps:
        if step.plan_id != plan.id:
            continue
        owned_fenced_lease = str(step.current_lease_id or "") in fenced_lease_ids
        if owned_fenced_lease:
            step.current_lease_id = None
        if cancel_steps and step.step_status not in terminal_step_values:
            step.step_status = ExecutionStepStatus.CANCELLED.value
            step.error = {
                "type": "UserCancelledEffectReconciliation",
                "message": "The Plan was cancelled during effect reconciliation",
            }
            step.last_execution_error = dict(step.error)
            step.human_input_prompt = None
            step.finished_at = datetime.now(timezone.utc)
            cancelled_step_ids.add(step.id)
        elif owned_fenced_lease and step.step_status == ExecutionStepStatus.RUNNING.value:
            step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
            step.error = {
                "type": "HistoricalAppliedEffectRequiresContinuation",
                "message": (
                    "The active execution was fenced because an earlier Plan's "
                    "external effect was confirmed applied."
                ),
            }
            step.last_execution_error = dict(step.error)
            step.human_input_prompt = step.error["message"]
            step.finished_at = None
    return cancelled_step_ids


def _candidate_result(
    target: ExecutionStep,
    effects: list[dict[str, Any]],
    supplied: Any,
    *,
    allow_step_result: bool = True,
) -> Any:
    if supplied is not None:
        return supplied
    for effect in effects:
        fields = effect.get("fields") if isinstance(effect.get("fields"), dict) else {}
        if "result" in fields:
            return fields["result"]
    if allow_step_result and target.result is not None:
        return target.result
    return None


def _pending_effect_checkpoint(
    target: ExecutionStep,
    effect: dict[str, Any],
):
    """Return the exact pre-I/O checkpoint bound to one pending effect."""

    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint

    try:
        if not isinstance(target.runtime_checkpoint, dict):
            raise TypeError("runtime checkpoint is not an object")
        checkpoint = RuntimeAgentCheckpoint.from_dict(target.runtime_checkpoint)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EffectReconciliationError(
            409,
            "The agentic action has no exact pre-I/O continuation; restarting "
            "it could repeat earlier external work.",
        ) from exc
    fields = effect.get("fields") if isinstance(effect.get("fields"), dict) else {}
    call_id = str(fields.get("tool_call_id") or "")
    tool_name = str(fields.get("tool") or "")
    from packages.core.ai.runtime.workspace_delegation_actions import (
        _tool_arguments_hash,
    )

    checkpoint_arguments = checkpoint.pending_tool_call.get("arguments")
    arguments_hash = _tool_arguments_hash(
        checkpoint_arguments if isinstance(checkpoint_arguments, dict) else {}
    )
    params = target.params if isinstance(target.params, dict) else {}
    try:
        target_generation = int(
            params.get(ACTION_EFFECT_GENERATION_PARAM_KEY) or 0
        )
        effect_generation = int(fields.get("effect_generation") or 0)
    except (TypeError, ValueError):
        target_generation = -1
        effect_generation = -2
    if (
        not call_id
        or str(checkpoint.pending_tool_call.get("id") or "") != call_id
        or str(checkpoint.pending_tool_call.get("name") or "") != tool_name
        or str(fields.get("arguments_hash") or "") != arguments_hash
        or effect_generation != target_generation
        or (
            fields.get("owner_step_id") is not None
            and str(fields.get("owner_step_id") or "") != str(target.id)
        )
        or (
            fields.get("owner_plan_id") is not None
            and str(fields.get("owner_plan_id") or "") != str(target.plan_id)
        )
    ):
        raise EffectReconciliationError(
            409,
            "The recorded action no longer matches its exact pending agent call",
        )
    return checkpoint


def _validate_applied_result(
    target: ExecutionStep,
    result: Any,
    *,
    evidence_refs: Any | None = None,
) -> None:
    if result is None:
        raise EffectReconciliationError(
            409,
            "The action was confirmed as applied, but no durable provider result "
            "is available. Paste the exact provider result as JSON and try again.",
        )
    params = target.params if isinstance(target.params, dict) else {}
    if params.get(COORDINATOR_CHILD_PARAM_KEY) is True:
        from packages.core.workers.coordinator_tools import _delegation_contract_error

        contract = params.get("source_contract")
        contract = contract if isinstance(contract, dict) else {}
        error = _delegation_contract_error(
            contract,
            result=result,
            evidence_refs=(
                evidence_refs
                if evidence_refs is not None
                else target.evidence_refs
            ),
        )
        if error:
            raise EffectReconciliationError(
                409,
                "The confirmed child result does not satisfy its delivery contract: "
                + error,
            )
        return
    try:
        validate_step_output(target, result)
    except SchemaError as exc:
        raise EffectReconciliationError(
            409,
            "The confirmed provider result does not satisfy the Step output contract: "
            + str(exc),
        ) from exc


def _checkpoint_with_applied_result(
    target: ExecutionStep,
    effect: dict[str, Any],
    result: Any,
) -> dict[str, Any]:
    """Inject one verified provider result into its exact pre-I/O call."""

    checkpoint = _pending_effect_checkpoint(target, effect)
    call_id = str(checkpoint.pending_tool_call.get("id") or "")
    if isinstance(checkpoint.pending_tool_result, dict):
        raise EffectReconciliationError(409, "The pending action was already reconciled")
    checkpoint.pending_tool_result = {
        "id": call_id,
        "content": json.dumps(result, ensure_ascii=False, default=str),
    }
    return checkpoint.to_dict()


async def _refresh_reconciliation_card(
    db: AsyncSession,
    *,
    request: HitlRequest,
    snapshot: dict[str, Any],
) -> None:
    """Atomically refresh a reused request and every unresolved card for it."""

    context = dict(request.context or {})
    context["effect_reconciliation"] = deepcopy(snapshot)
    request.context = context
    payload = dict(request.payload or {})
    payload["reconciliation"] = deepcopy(snapshot)
    request.payload = payload
    messages = list((await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == request.entity_id,
            Conversation.workspace_id == request.workspace_id,
            Message.resolved_at.is_(None),
            Message.pending_action["approval_request_id"].as_string()
            == str(request.id),
        )
        .order_by(Message.id)
        .with_for_update(of=Message)
        .execution_options(populate_existing=True)
    )).scalars().all())
    for message in messages:
        pending_action = (
            dict(message.pending_action)
            if isinstance(message.pending_action, dict)
            else {}
        )
        card_payload = (
            dict(pending_action.get("payload") or {})
            if isinstance(pending_action.get("payload"), dict)
            else {}
        )
        card_payload["reconciliation"] = deepcopy(snapshot)
        pending_action["payload"] = card_payload
        pending_action["options"] = (
            EffectReconciliationDecisionFactory.card_options(snapshot)
        )
        message.pending_action = pending_action
    await db.flush()


async def _expire_dispatch_recovery_surface(
    db: AsyncSession,
    *,
    request_id: str,
    task: Task,
    user_id: str,
) -> None:
    """Close only the recovery card created by a failed post-commit enqueue."""

    recovery_request = (await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.id == request_id,
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.workspace_id == task.workspace_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.action_key == "task.recover",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        recovery_request is not None
        and recovery_request.status == ApprovalStatus.PENDING.value
    ):
        now = datetime.now(timezone.utc)
        recovery_request.status = ApprovalStatus.EXPIRED.value
        recovery_request.resolved_reason = "dispatch_recovered_by_effect_replay"
        recovery_request.decided_at = now

        messages = list((await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.entity_id == task.entity_id,
                Conversation.workspace_id == task.workspace_id,
                Message.resolved_at.is_(None),
                Message.pending_action["approval_request_id"].as_string()
                == request_id,
            )
            .order_by(Message.id)
            .with_for_update(of=Message)
            .execution_options(populate_existing=True)
        )).scalars().all())
        for message in messages:
            message.resolved_at = now
            message.resolved_by_user_id = user_id
            message.resolution = {
                "choice": "superseded",
                "reason": "dispatch_recovered_by_effect_replay",
            }


async def reconcile_external_effects(
    db: AsyncSession,
    *,
    approval_request_id: str,
    plan_id: str,
    decisions: dict[str, str],
    user_id: str,
    note: str | None = None,
    supplied_results: dict[str, Any] | None = None,
) -> EffectReconciliationResult:
    """Apply one exact, locked reconciliation decision and resume its Plan.

    The initial reads discover the lock scope only.  Every authority-bearing
    value is re-read after acquiring the canonical Workspace -> Task -> Plan
    -> Lease -> Step -> HitlRequest locks.
    """

    request_preview = await db.get(HitlRequest, approval_request_id)
    plan_preview = await db.get(ExecutionPlan, plan_id)
    if request_preview is None or plan_preview is None:
        raise EffectReconciliationError(409, "Effect reconciliation is stale")
    if (
        request_preview.entity_id != plan_preview.entity_id
        or str(request_preview.workspace_id or "")
        != str(plan_preview.workspace_id or "")
        or str(request_preview.origin_plan_id or "") != str(plan_id)
    ):
        raise EffectReconciliationError(409, "Recovery request is outside this Plan scope")

    workspace = None
    if plan_preview.workspace_id:
        workspace = (await db.execute(
            select(Workspace)
            .where(
                Workspace.id == plan_preview.workspace_id,
                Workspace.entity_id == plan_preview.entity_id,
                Workspace.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if workspace is None:
            raise EffectReconciliationError(409, "Recovery Workspace is no longer active")

    task = None
    task_plans: list[ExecutionPlan] = []
    if plan_preview.task_id:
        task = (await db.execute(
            select(Task)
            .where(
                Task.id == plan_preview.task_id,
                Task.entity_id == plan_preview.entity_id,
                Task.workspace_id == plan_preview.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None:
            raise EffectReconciliationError(409, "Recovery Task no longer exists")

    historical_reconciliation = False
    resume_plan = None
    active_others: list[ExecutionPlan] = []
    if task is not None:
        task_plans = list((await db.execute(
            select(ExecutionPlan)
            .where(
                ExecutionPlan.task_id == task.id,
                ExecutionPlan.entity_id == task.entity_id,
            )
            .order_by(ExecutionPlan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        plan = next((item for item in task_plans if item.id == plan_id), None)
        latest_plan = max(
            task_plans,
            key=lambda item: (item.created_at, item.id),
            default=None,
        )
        active_others = [
            item
            for item in task_plans
            if item.id != plan_id
            and item.status in {
                ExecutionPlanStatus.DRAFT.value,
                ExecutionPlanStatus.PENDING.value,
                ExecutionPlanStatus.PENDING_APPROVAL.value,
                ExecutionPlanStatus.RUNNING.value,
                ExecutionPlanStatus.PAUSED.value,
                ExecutionPlanStatus.NEEDS_ATTENTION.value,
            }
        ]
        if latest_plan is None:
            raise EffectReconciliationError(409, "Recovery Task has no Plan history")
        historical_reconciliation = latest_plan.id != plan_id
        resume_plan = latest_plan
    else:
        plan = (await db.execute(
            select(ExecutionPlan)
            .where(
                ExecutionPlan.id == plan_id,
                ExecutionPlan.entity_id == plan_preview.entity_id,
                ExecutionPlan.workspace_id == plan_preview.workspace_id,
                ExecutionPlan.task_id.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
    if plan is None:
        raise EffectReconciliationError(409, "Recovery Plan no longer exists")

    locked_plan_ids = sorted(
        {item.id for item in task_plans} if task is not None else {plan.id}
    )
    all_locked_leases = list((await db.execute(
        select(WorkLease)
        .where(WorkLease.plan_id.in_(locked_plan_ids))
        .order_by(WorkLease.plan_id, WorkLease.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    all_locked_steps = list((await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id.in_(locked_plan_ids))
        .order_by(
            ExecutionStep.plan_id,
            ExecutionStep.created_at,
            ExecutionStep.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    leases = [lease for lease in all_locked_leases if lease.plan_id == plan.id]
    steps = [step for step in all_locked_steps if step.plan_id == plan.id]
    request = (await db.execute(
        select(HitlRequest)
        .where(HitlRequest.id == approval_request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None:
        raise EffectReconciliationError(409, "Effect reconciliation is stale")
    context = request.context if isinstance(request.context, dict) else {}
    expected = context.get("effect_reconciliation")
    if not isinstance(expected, dict):
        raise EffectReconciliationError(409, "Recovery request has no durable effect snapshot")
    if (
        request.entity_id != plan.entity_id
        or str(request.workspace_id or "") != str(plan.workspace_id or "")
        or str(request.origin_plan_id or "") != str(plan.id)
        or str(expected.get("plan_id") or "") != str(plan.id)
    ):
        raise EffectReconciliationError(409, "Recovery request no longer belongs to this Plan")

    normalized_decisions = EffectReconciliationDecisionFactory.normalize_map(
        decisions
    )
    if normalized_decisions is None:
        raise EffectReconciliationError(
            400,
            "Every effect digest must have a recognized reconciliation decision",
        )
    supplied_results = {
        str(digest): value
        for digest, value in (supplied_results or {}).items()
    }
    decision_fingerprint = _decision_fingerprint(
        request_id=request.id,
        plan_id=plan.id,
        decisions=normalized_decisions,
        supplied_results=supplied_results,
        note=note,
    )
    if request.status != ApprovalStatus.PENDING.value:
        replayed = _replayed_result(
            request=request,
            resolution=context.get("effect_reconciliation_resolution"),
            decision_fingerprint=decision_fingerprint,
            user_id=user_id,
        )
        if replayed is not None:
            replay_plan = (
                next(
                    (
                        candidate
                        for candidate in task_plans
                        if candidate.id == replayed.plan_id
                    ),
                    None,
                )
                if task is not None
                else plan
            )
            replay_error_type = (
                str(replay_plan.last_error.get("type") or "")
                if replay_plan is not None and isinstance(replay_plan.last_error, dict)
                else ""
            )
            if (
                replay_plan is not None
                and replay_plan.status == ExecutionPlanStatus.NEEDS_ATTENTION.value
                and replay_error_type == "PlanContinuationDispatchFailed"
            ):
                replay_plan.status = ExecutionPlanStatus.RUNNING.value
                replay_plan.completed_at = None
                replay_plan.last_error = None
                if task is not None:
                    details = dict(task.details or {})
                    pending_dispatch = details.get("_pending_plan_dispatch")
                    recovery_request_id = ""
                    if (
                        isinstance(pending_dispatch, dict)
                        and str(pending_dispatch.get("plan_id") or "")
                        == replay_plan.id
                    ):
                        recovery_request_id = str(
                            pending_dispatch.get("recovery_request_id") or ""
                        ).strip()
                        details.pop("_pending_plan_dispatch", None)
                        task.details = details
                    if recovery_request_id:
                        await _expire_dispatch_recovery_surface(
                            db,
                            request_id=recovery_request_id,
                            task=task,
                            user_id=user_id,
                        )
                    await apply_task_status_transition(
                        task,
                        TaskStatus.IN_PROGRESS.value,
                        db=db,
                        actor_kind="user",
                        actor_id=user_id,
                    )
                    task.completed_at = None
                from packages.core.plans.wakeup import stage_plan_cycle_wake

                await stage_plan_cycle_wake(
                    db,
                    replay_plan.id,
                    reason="effect_reconciliation_legacy_dispatch_recovery",
                )
                await db.flush()
            return replayed
        raise EffectReconciliationError(
            409,
            "Effect reconciliation is already resolved with a different decision",
        )

    if task is not None and task.status in {
        TaskStatus.COMPLETED.value,
        TaskStatus.CANCELLED.value,
    }:
        raise EffectReconciliationError(409, "Recovery Task is already terminal")
    if (
        not historical_reconciliation
        and active_others
    ):
        raise EffectReconciliationError(409, "This Task has more than one active Plan")
    if (
        not historical_reconciliation
        and plan.status not in {
            ExecutionPlanStatus.RUNNING.value,
            ExecutionPlanStatus.PAUSED.value,
            ExecutionPlanStatus.FAILED.value,
            ExecutionPlanStatus.NEEDS_ATTENTION.value,
        }
    ):
        raise EffectReconciliationError(409, "Recovery Plan is no longer recoverable")

    by_id = {step.id: step for step in steps}
    if request.origin_kind == ApprovalOriginKind.TASK.value:
        if (
            task is None
            or (
                not historical_reconciliation
                and plan.status not in {
                    ExecutionPlanStatus.PAUSED.value,
                    ExecutionPlanStatus.FAILED.value,
                    ExecutionPlanStatus.NEEDS_ATTENTION.value,
                }
            )
            or any(
                lease.status == WorkLeaseStatus.ACTIVE.value
                for lease in leases
            )
            or request.action_key != "task.recover"
            or request.resource_kind != "task"
            or str(request.resource_id or "") != str(task.id)
            or str(request.origin_task_id or "") != str(task.id)
        ):
            raise EffectReconciliationError(409, "Task recovery origin no longer matches")
    elif request.origin_kind == ApprovalOriginKind.LEASE.value:
        origin_step = by_id.get(str(request.origin_step_id or ""))
        matching_leases = [
            lease
            for lease in leases
            if lease.step_id == getattr(origin_step, "id", None)
            and request.dedup_key
            == f"lease:{lease.id}:{PendingActionKind.EFFECT_RECONCILIATION.value}"
        ]
        if (
            origin_step is None
            or origin_step.step_status != ExecutionStepStatus.WAITING_HUMAN.value
            or str(request.origin_task_id or "") != str(plan.task_id or "")
            or str(context.get("pending_kind") or "")
            != PendingActionKind.EFFECT_RECONCILIATION.value
            or len(matching_leases) != 1
            or matching_leases[0].status
            not in {
                WorkLeaseStatus.NEEDS_HUMAN.value,
                WorkLeaseStatus.FAILED.value,
                WorkLeaseStatus.EXPIRED.value,
                WorkLeaseStatus.CANCELLED.value,
            }
        ):
            raise EffectReconciliationError(409, "Lease recovery origin no longer matches")
    else:
        raise EffectReconciliationError(409, "Unsupported recovery origin")

    live = await ambiguous_effect_reconciliation_snapshot(
        db,
        steps,
        plan_id=plan.id,
    )
    expected_digests = {
        str(item.get("effect_digest") or "")
        for item in (expected.get("effects") or [])
        if isinstance(item, dict) and item.get("effect_digest")
    }
    live_digests = {
        str(item.get("effect_digest") or "")
        for item in ((live or {}).get("effects") or [])
        if isinstance(item, dict) and item.get("effect_digest")
    }
    try:
        expected_generation = int(expected.get("generation") or 0)
        live_generation = int((live or {}).get("generation") or 0)
    except (TypeError, ValueError) as exc:
        raise EffectReconciliationError(409, "Recovery generation is invalid") from exc
    if (
        not expected_digests
        or live_digests != expected_digests
        or live_generation != expected_generation
    ):
        if isinstance(live, dict) and live.get("effects"):
            await _refresh_reconciliation_card(
                db,
                request=request,
                snapshot=live,
            )
        raise EffectReconciliationError(
            409,
            "Effect evidence changed after this recovery card was created; refresh it before deciding",
            commit_refreshed_card=bool(
                isinstance(live, dict) and live.get("effects")
            ),
        )
    if set(normalized_decisions) != expected_digests:
        raise EffectReconciliationError(
            400,
            "A reconciliation decision is required for every recorded external effect",
        )
    if EffectReconciliationDecision.CANCEL.value in normalized_decisions.values() and set(
        normalized_decisions.values()
    ) != {EffectReconciliationDecision.CANCEL.value}:
        raise EffectReconciliationError(
            400,
            "Cancel must apply to the complete reconciliation request",
        )

    occurrences: dict[str, list[tuple[ExecutionStep, dict[str, Any]]]] = {
        digest: [] for digest in expected_digests
    }
    for step in steps:
        for effect in _effect_refs(step):
            digest = canonical_effect_digest(effect)
            if digest in occurrences:
                occurrences[digest].append((step, effect))
    if any(not rows for rows in occurrences.values()):
        raise EffectReconciliationError(409, "One or more recorded effects disappeared")

    try:
        generation = expected_generation
    except (TypeError, ValueError) as exc:
        raise EffectReconciliationError(409, "Recovery generation is invalid") from exc
    supplied_results = supplied_results or {}
    single_note_result = (
        _parse_single_note_result(note) if len(expected_digests) == 1 else None
    )
    target_for_digest: dict[str, ExecutionStep] = {}
    for digest in sorted(expected_digests):
        rows = occurrences[digest]
        child_ids = {
            str(effect.get("coordinator_child_step_id") or "")
            for _, effect in rows
            if effect.get("coordinator_child_step_id")
        }
        if len(child_ids) > 1:
            raise EffectReconciliationError(409, "Effect is attributed to multiple child Steps")
        if child_ids:
            child_id = next(iter(child_ids))
            target = by_id.get(child_id)
            target_params = target.params if target is not None and isinstance(target.params, dict) else {}
            parent_key = str(target_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) or "")
            parent = next((step for step in steps if step.step_key == parent_key), None)
            target_has_effect = target is not None and any(
                canonical_effect_digest(effect) == digest
                for effect in _effect_refs(target)
            )
            allowed_owner_ids = {
                item for item in (
                    getattr(target, "id", None),
                    getattr(parent, "id", None),
                ) if item
            }
            if (
                target is None
                or target_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
                or not parent_key
                or parent is None
                or not isinstance(parent.params, dict)
                or parent.params.get(COORDINATOR_PARAM_KEY) is not True
                or target.plan_id != plan.id
                or target.entity_id != plan.entity_id
                or target.workspace_id != plan.workspace_id
                or not target_has_effect
                or any(owner.id not in allowed_owner_ids for owner, _ in rows)
            ):
                raise EffectReconciliationError(409, "Effect child ownership is invalid")
        else:
            owner_ids = {owner.id for owner, _ in rows}
            if len(owner_ids) != 1:
                raise EffectReconciliationError(
                    409,
                    "Effect digest is shared by multiple independent Steps",
                )
            target = rows[0][0]
        target_for_digest[digest] = target

    digests_by_target: dict[str, list[str]] = {}
    for digest, target in target_for_digest.items():
        digests_by_target.setdefault(target.id, []).append(digest)
    for target_id, digests in digests_by_target.items():
        target_decisions = {normalized_decisions[digest] for digest in digests}
        if len(target_decisions) != 1:
            raise EffectReconciliationError(
                409,
                "All effects owned by the same Step require the same decision",
            )

    action_groups_by_target = {
        target_id: effect_action_group_ids(
            _effect_refs(by_id[target_id]),
            owner_plan_id=plan.id,
            owner_step_id=target_id,
        )
        for target_id in digests_by_target
    }

    unsafe_not_applied = False
    for digest, decision in normalized_decisions.items():
        if decision != EffectReconciliationDecision.NOT_APPLIED.value:
            continue
        effect_leases = [
            lease
            for lease in leases
            if any(
                isinstance(ref, dict)
                and ref.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
                and canonical_effect_digest(ref) == digest
                for ref in (lease.evidence_refs or [])
            )
        ]
        def lease_is_quiescent(effect_lease: WorkLease) -> bool:
            if effect_lease.status == WorkLeaseStatus.NEEDS_HUMAN.value:
                return True
            lease_error = (
                effect_lease.error
                if isinstance(effect_lease.error, dict)
                else {}
            )
            failure = (
                lease_error.get("failure")
                if isinstance(lease_error.get("failure"), dict)
                else {}
            )
            return (
                effect_lease.status
                in {
                    WorkLeaseStatus.FAILED.value,
                    WorkLeaseStatus.EXPIRED.value,
                }
                and effect_lease.execution_claim_id is None
                and failure.get("requires_human") is True
                and failure.get("retryable") is False
            )

        if not effect_leases or any(
            not lease_is_quiescent(lease) for lease in effect_leases
        ):
            unsafe_not_applied = True
            break
    if unsafe_not_applied:
        # Request origin is irrelevant: deadline/revoke can project the same
        # old call onto a Task card. NEEDS_HUMAN is a worker-owned stop.
        # FAILED/EXPIRED is accepted only after the execution claim is gone
        # and the dispatcher recorded a non-retryable human-reconciliation
        # boundary. CANCELLED alone never proves quiescence.
        raise EffectReconciliationError(
            409,
            "The prior provider call is not known to be quiescent; it "
            "cannot be retried without risking a duplicate external action",
        )

    # Validate every APPLIED target before mutating any row.  This keeps a
    # multi-target request atomic even when the last supplied result is bad.
    candidates_by_target: dict[str, Any] = {}
    resume_checkpoints_by_target: dict[str, dict[str, Any]] = {}
    retry_checkpoints_by_target: dict[str, dict[str, Any]] = {}
    for target_id, digests in digests_by_target.items():
        decision = normalized_decisions[digests[0]]
        target = by_id[target_id]
        target_effects = [
            effect
            for digest in digests
            for owner, effect in occurrences[digest]
            if owner.id == target.id
        ]
        target_params = target.params if isinstance(target.params, dict) else {}
        is_agentic = (
            target.kind in {"llm", "subagent"}
            or target_params.get(COORDINATOR_CHILD_PARAM_KEY) is True
            or target_params.get(COORDINATOR_PARAM_KEY) is True
        )
        one_agentic_action = len({
            action_groups_by_target[target_id].get(digest, digest)
            for digest in digests
        }) == 1
        exact_effect = next(
            (effect for effect in target_effects if (effect.get("fields") or {}).get("effect_id")),
            target_effects[0],
        )
        if decision == EffectReconciliationDecision.CANCEL.value:
            continue
        if decision == EffectReconciliationDecision.NOT_APPLIED.value:
            if is_agentic:
                if not one_agentic_action:
                    raise EffectReconciliationError(
                        409,
                        "An agentic Step can retry only its one exact pending action",
                    )
                checkpoint = _pending_effect_checkpoint(target, exact_effect)
                if isinstance(checkpoint.pending_tool_result, dict):
                    raise EffectReconciliationError(
                        409,
                        "The pending action already has a durable result",
                    )
                retry_checkpoints_by_target[target_id] = checkpoint.to_dict()
            continue
        supplied_for_target = [
            supplied_results[digest]
            for digest in digests
            if digest in supplied_results
        ]
        if supplied_for_target:
            signatures = {
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                for value in supplied_for_target
            }
            if len(signatures) != 1:
                raise EffectReconciliationError(
                    400,
                    "Applied effects on one Step must provide one identical result",
                )
            supplied = supplied_for_target[0]
        else:
            supplied = single_note_result
        candidate = _candidate_result(
            target,
            target_effects,
            supplied,
            allow_step_result=(
                not is_agentic
                and all(
                    effect.get("effect") != ToolEffectOutcome.PENDING.value
                    for effect in target_effects
                )
            ),
        )
        prospective = [dict(item) for item in (target.evidence_refs or []) if isinstance(item, dict)]
        for digest in digests:
            effect = next(
                effect
                for owner, effect in occurrences[digest]
                if owner.id == target.id
            )
            marker = _marker(
                digest=digest,
                decision=decision,
                generation=generation,
                request_id=request.id,
                user_id=user_id,
                plan_id=plan.id,
                recorded_on_step_id=target.id,
                note=note,
            )
            prospective = _append_server_evidence(
                prospective,
                [_settlement_for(effect, marker, applied_result=candidate)],
            )
        if is_agentic:
            if not one_agentic_action or candidate is None:
                raise EffectReconciliationError(
                    409,
                    "An agentic Step requires one exact provider result for its "
                    "single pending action before it can continue.",
                )
            resume_checkpoints_by_target[target_id] = _checkpoint_with_applied_result(
                target,
                exact_effect,
                candidate,
            )
        else:
            _validate_applied_result(target, candidate, evidence_refs=prospective)
        candidates_by_target[target_id] = candidate

    reset_ids: set[str] = set()
    completed_ids: set[str] = set()
    cancelled_ids: set[str] = set()
    recorded_step_ids: set[str] = set()
    parent_keys_to_reset: set[str] = set()
    parent_keys_to_fail: set[str] = set()
    for target_id, digests in sorted(digests_by_target.items()):
        target = by_id[target_id]
        decision = normalized_decisions[digests[0]]
        candidate = candidates_by_target.get(target_id)
        if historical_reconciliation:
            # Resolve evidence on an older Plan without reviving or rewriting
            # its lifecycle. A validated NOT_APPLIED marker clears the
            # Task-history fence; APPLIED remains a durable cross-Plan fence.
            pass
        elif decision == EffectReconciliationDecision.CANCEL.value:
            target.step_status = ExecutionStepStatus.FAILED.value
            target.error = {
                "type": "UserCancelledEffectReconciliation",
                "message": "User cancelled recovery for an ambiguous external action",
            }
            target.last_execution_error = dict(target.error)
            target.current_lease_id = None
            target.human_input_prompt = None
            target.finished_at = datetime.now(timezone.utc)
            cancelled_ids.add(target.id)
        elif decision == EffectReconciliationDecision.APPLIED.value:
            resume_checkpoint = resume_checkpoints_by_target.get(target_id)
            if resume_checkpoint is not None:
                _reset_step(target)
                target.runtime_checkpoint = resume_checkpoint
                reset_ids.add(target.id)
            else:
                target.result = candidate
                target.step_status = ExecutionStepStatus.DONE.value
                target.error = None
                target.last_execution_error = None
                target.current_lease_id = None
                target.runtime_checkpoint = None
                target.human_input_prompt = None
                target.finished_at = datetime.now(timezone.utc)
                completed_ids.add(target.id)
            target_params = dict(target.params or {})
            target_params.pop(COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY, None)
            target.params = target_params
        else:
            params = dict(target.params or {})
            try:
                current_generation = int(
                    params.get(ACTION_EFFECT_GENERATION_PARAM_KEY) or 0
                )
            except (TypeError, ValueError):
                current_generation = 0
            next_generation = max(generation, current_generation + 1)
            params[ACTION_EFFECT_GENERATION_PARAM_KEY] = next_generation
            if params.get(COORDINATOR_CHILD_PARAM_KEY) is True:
                from packages.core.workers.coordinator_tools import _child_idempotency_key

                parent_key = str(params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) or "")
                item_key = str(params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or "")
                if not parent_key or not item_key:
                    raise EffectReconciliationError(409, "Coordinator child identity is incomplete")
                params[COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY] = _child_idempotency_key(
                    plan.id,
                    parent_key,
                    item_key,
                    next_generation,
                )
                params.pop(COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY, None)
            target.params = params
            retry_checkpoint = retry_checkpoints_by_target.get(target_id)
            _reset_step(target)
            if retry_checkpoint is not None:
                target.runtime_checkpoint = retry_checkpoint
            reset_ids.add(target.id)

        for digest in digests:
            for owner, effect in occurrences[digest]:
                if decision == EffectReconciliationDecision.CANCEL.value:
                    recorded_step_ids.add(owner.id)
                    continue
                marker = _marker(
                    digest=digest,
                    decision=decision,
                    generation=generation,
                    request_id=request.id,
                    user_id=user_id,
                    plan_id=plan.id,
                    recorded_on_step_id=owner.id,
                    note=note,
                )
                incoming = [
                    _settlement_for(
                        effect,
                        marker,
                        applied_result=candidate,
                    ),
                    marker,
                ]
                if (
                    decision == EffectReconciliationDecision.APPLIED.value
                    and effect.get("effect") == ToolEffectOutcome.PENDING.value
                ):
                    incoming.append(_terminal_effect_for_applied(effect, marker, candidate))
                owner.evidence_refs = _append_server_evidence(
                    owner.evidence_refs,
                    incoming,
                )
                recorded_step_ids.add(owner.id)
        target_params = target.params if isinstance(target.params, dict) else {}
        if (
            not historical_reconciliation
            and target_params.get(COORDINATOR_CHILD_PARAM_KEY) is True
        ):
            parent_key = str(target_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) or "")
            if parent_key:
                if decision == EffectReconciliationDecision.CANCEL.value:
                    parent_keys_to_fail.add(parent_key)
                else:
                    parent_keys_to_reset.add(parent_key)

    for parent_key in parent_keys_to_fail:
        parent = next((step for step in steps if step.step_key == parent_key), None)
        if parent is None:
            raise EffectReconciliationError(409, "Coordinator parent Step is missing")
        parent.step_status = ExecutionStepStatus.FAILED.value
        parent.error = {
            "type": "UserCancelledEffectReconciliation",
            "message": "A delegated external action recovery was cancelled",
        }
        parent.last_execution_error = dict(parent.error)
        parent.current_lease_id = None
        parent.human_input_prompt = None
        parent.finished_at = datetime.now(timezone.utc)
        cancelled_ids.add(parent.id)

    for parent_key in parent_keys_to_reset:
        parent = next((step for step in steps if step.step_key == parent_key), None)
        if parent is None:
            raise EffectReconciliationError(409, "Coordinator parent Step is missing")
        if parent.id not in reset_ids:
            _reset_step(parent)
        reset_ids.add(parent.id)

    if not historical_reconciliation:
        revived = reset_never_ran_downstream_steps(
            steps,
            retried_step_keys={
                step.step_key for step in steps if step.id in reset_ids | completed_ids
            },
            reset_step=_reset_step,
        )
        reset_ids.update(step.id for step in revived)

    touched_ids = reset_ids | completed_ids | cancelled_ids
    if historical_reconciliation:
        # The old Plan is never revived, but its exact decision surface must
        # not leave a NEEDS_HUMAN lease hanging after the request is consumed.
        touched_ids.update(target.id for target in target_for_digest.values())
    for lease in leases:
        if (
            lease.step_id in touched_ids
            and lease.status in {
                WorkLeaseStatus.ACTIVE.value,
                WorkLeaseStatus.NEEDS_HUMAN.value,
            }
        ):
            lease.status = WorkLeaseStatus.CANCELLED.value
            lease.credential_leases = []
            lease.execution_claim_id = None
            lease.error = {
                "type": "EffectReconciled",
                "approval_request_id": request.id,
            }

    result_plan = resume_plan if historical_reconciliation else plan
    cancelled = set(normalized_decisions.values()) == {
        EffectReconciliationDecision.CANCEL.value
    }
    historical_applied_requires_lineage = (
        historical_reconciliation
        and EffectReconciliationDecision.APPLIED.value
        in normalized_decisions.values()
    )
    if result_plan is not None and (
        cancelled or historical_applied_requires_lineage
    ):
        cancelled_ids.update(_fence_plan_execution(
            plan=result_plan,
            leases=all_locked_leases,
            steps=all_locked_steps,
            approval_request_id=request.id,
            cancel_steps=cancelled,
        ))
    result_sha256_by_digest = {
        digest: _canonical_sha256(candidates_by_target[target_for_digest[digest].id])
        for digest in sorted(expected_digests)
        if target_for_digest[digest].id in candidates_by_target
    }
    action_generation_by_step: dict[str, int] = {}
    for target in target_for_digest.values():
        params = target.params if isinstance(target.params, dict) else {}
        try:
            action_generation_by_step[target.id] = int(
                params.get(ACTION_EFFECT_GENERATION_PARAM_KEY) or 0
            )
        except (TypeError, ValueError):
            action_generation_by_step[target.id] = 0
    resolution = {
        "version": 2,
        "request_id": request.id,
        "entity_id": request.entity_id,
        "workspace_id": request.workspace_id,
        "origin_plan_id": plan.id,
        "result_plan_id": result_plan.id if result_plan is not None else plan.id,
        "task_id": task.id if task is not None else plan.task_id,
        "plan_id": plan.id,
        "snapshot_generation": expected_generation,
        "generation": generation,
        "effect_digests": sorted(expected_digests),
        "decision_by_digest": {
            digest: normalized_decisions[digest] for digest in sorted(expected_digests)
        },
        "decision_fingerprint": decision_fingerprint,
        "result_sha256_by_digest": result_sha256_by_digest,
        "recorded_step_ids": sorted(recorded_step_ids),
        "reset_step_ids": sorted(reset_ids),
        "completed_step_ids": sorted(completed_ids),
        "cancelled_step_ids": sorted(cancelled_ids),
        "action_generation_by_step": action_generation_by_step,
        "cancelled": cancelled,
        "should_dispatch": (
            not cancelled and not historical_applied_requires_lineage
        ),
        "resolved_by": user_id,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    request.context = {
        **context,
        "effect_reconciliation_resolution": resolution,
    }
    if cancelled:
        await deny_approval(
            db,
            request,
            by_user_id=user_id,
            via="effect_reconciliation",
            reason="user cancelled external-effect reconciliation",
        )
    else:
        await grant_approval(db, request, by_user_id=user_id, via="effect_reconciliation")
        await consume_approval(db, request)

    if historical_reconciliation and result_plan is not None:
        if cancelled:
            result_plan.status = ExecutionPlanStatus.CANCELLED.value
            result_plan.completed_at = datetime.now(timezone.utc)
            result_plan.last_error = {
                "type": "UserCancelledEffectReconciliation",
                "message": "User cancelled recovery for Task execution history",
            }
            if task is not None:
                await apply_task_status_transition(
                    task,
                    TaskStatus.CANCELLED.value,
                    db=db,
                    actor_kind="user",
                    actor_id=user_id,
                )
        elif historical_applied_requires_lineage:
            # Knowing that an older effect happened settles the ambiguity but
            # does not authorize a newer Plan to replay or silently inherit
            # it. Until a server-owned cross-Plan carry lineage exists, park
            # the successor explicitly instead of enqueueing it into the same
            # history fence and producing an infinite recovery loop.
            result_plan.status = ExecutionPlanStatus.NEEDS_ATTENTION.value
            result_plan.completed_at = None
            result_plan.last_error = {
                "type": "HistoricalAppliedEffectRequiresContinuation",
                "message": (
                    "A prior Plan's external action was confirmed applied. "
                    "The successor cannot safely infer or replay that result."
                ),
                "source_plan_id": plan.id,
                "approval_request_id": request.id,
            }
            if task is not None:
                await apply_task_status_transition(
                    task,
                    TaskStatus.WAITING_ON_CUSTOMER.value,
                    db=db,
                    actor_kind="user",
                    actor_id=user_id,
                )
                task.completed_at = None
        else:
            result_plan.status = ExecutionPlanStatus.RUNNING.value
            result_plan.started_at = result_plan.started_at or datetime.now(timezone.utc)
            result_plan.completed_at = None
            result_plan.last_error = None
            if task is not None:
                await apply_task_status_transition(
                    task,
                    TaskStatus.IN_PROGRESS.value,
                    db=db,
                    actor_kind="user",
                    actor_id=user_id,
                )
                task.completed_at = None
    elif not historical_reconciliation:
        if cancelled:
            plan.status = ExecutionPlanStatus.CANCELLED.value
            plan.completed_at = datetime.now(timezone.utc)
            plan.last_error = {
                "type": "UserCancelledEffectReconciliation",
                "message": "User cancelled recovery for an ambiguous external action",
            }
        else:
            plan.status = ExecutionPlanStatus.RUNNING.value
            plan.started_at = datetime.now(timezone.utc)
            plan.completed_at = None
            plan.last_error = None
    if task is not None and not historical_reconciliation:
        await apply_task_status_transition(
            task,
            (
                TaskStatus.CANCELLED.value
                if cancelled
                else TaskStatus.IN_PROGRESS.value
            ),
            db=db,
            actor_kind="user",
            actor_id=user_id,
        )
        if not cancelled:
            task.completed_at = None
    should_dispatch = not cancelled and not historical_applied_requires_lineage
    if should_dispatch:
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        await stage_plan_cycle_wake(
            db,
            (result_plan or plan).id,
            reason="effect_reconciliation_resolved",
        )
    await db.flush()
    return EffectReconciliationResult(
        plan_id=result_plan.id if result_plan is not None else plan.id,
        task_id=task.id if task is not None else plan.task_id,
        reset_step_ids=tuple(sorted(reset_ids)),
        completed_step_ids=tuple(sorted(completed_ids)),
        replayed=False,
        should_dispatch=should_dispatch,
    )


__all__ = [
    "EffectReconciliationError",
    "EffectReconciliationResult",
    "reconcile_external_effects",
]
