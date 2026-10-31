"""Dispatcher core — atomic step→worker matchmaker + lease lifecycle.

Concurrency model: one ``checkout_steps_for_worker`` call per worker
heartbeat. ``SELECT … FOR UPDATE SKIP LOCKED`` plus the partial unique
index ``uq_one_active_lease_per_step`` together guarantee that two
worker heartbeats arriving at the same instant never see the same
runnable step.

Capability matching:
  * worker.capabilities.supported_kinds       must include step.kind
  * worker.capabilities.supported_capabilities
                                               optional Runtime capability
                                               allowlist for step.capability_id
  * worker.capabilities.supported_providers   either null (all) or
                                               must include step.provider
  * worker.capabilities.max_risk_level        must be >= step.risk_level
  * step.service_key                           must map to a subscription
                                               that's bound to this
                                               worker via subscription_workers
  * agent_mcp_bindings.allowed_tools           must include step.action_key
                                               (deferred — checked in M3.5
                                               when integrated with PlanExecutor)

Step state transitions managed here:
  pending          → running         (checkout)
  running          → done            (complete_lease)
  running          → failed          (fail_lease, attempts exhausted)
  running          → pending         (fail_lease, retry remaining;
                                       fail_lease puts step back in queue)
  running          → waiting_human   (lease_needs_human)
  running          → failed          (lease_needs_human / fail_lease /
                                       expire_leases, unconfirmed external
                                       effect: never replayed)
  running          → pending         (expire_leases, retry remaining)
  running          → failed          (expire_leases, attempts exhausted)
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskLogType
from packages.core.i18n import t
from packages.core.services.task_response_language import resolve_plan_response_locale
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    DISPATCH_SKIP_KEY,
    DISPATCH_SKIPPED_ERROR_TYPE,
    LEASE_REVOKED_ERROR_TYPE,
    LEASE_SUPERSEDED_ERROR_TYPE,
    DispatchSkipReason,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    WorkLeaseStatus,
    WorkerStatus,
)
from packages.core.constants.tool_effects import ToolEffectOutcome
from packages.core.constants.integrations import INTEGRATION_ACCOUNT_SELECTION_ARGUMENT
from packages.core.ai.runtime.outcome import attempt_forbids_replay
from packages.core.ai.pending_action import (
    KIND_HUMAN_INPUT,
    LEASE_HITL_CLOSEABLE_KINDS,
)
from packages.core.ai.runtime import (
    runtime_capability_id_for_action_key,
    runtime_planner_action_binding_for,
    runtime_planner_action_specs_from_tools_cached,
)
from packages.core.contracts.envelope import (
    envelope_indicates_failure,
)
from packages.core.contracts.task_output import (
    TaskOutputValueKind,
    TaskOutputProtocolError,
    output_contract_for_schema,
)
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.dispatcher.action_params import executable_action_params
from packages.core.dispatcher.validation import (
    SchemaError,
    output_schema_is_advisory,
    validate_step_input,
    validate_step_output,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import (
    SubscriptionWorker,
    Worker,
    WorkerActivityLog,
    WorkLease,
)
from packages.core.models.workspace import Workspace
from packages.core.contracts.tool_effects import (
    ToolEffectEvidenceFactory,
)
from packages.core.services.integration_account_service import (
    IntegrationAccountFanoutResultFactory,
)

logger = logging.getLogger(__name__)

INTERNAL_FILE_RECOVERY_FAILED = "InternalFileRecoveryFailed"
#: A worker job crossed an external-action boundary without a confirmed
#: outcome. The Step fails without replay; a person checks the provider.
EXTERNAL_EFFECT_UNCONFIRMED = "ExternalEffectUnconfirmed"


def _external_effect_unconfirmed_failure(step_key: str) -> dict[str, Any]:
    """The non-retryable failure for an external action with no confirmed outcome."""
    return {
        "reason": (
            f"Step '{step_key}' started an external action whose outcome was not confirmed. "
            "The action may or may not have happened, so Manor will not run it again. "
            "Check the result with the provider before repeating it."
        ),
        "retryable": False,
        "requires_human": True,
        "blockers": [],
    }


def _step_policy_subject(
    step: Any, *, capability_id: str | None = None, requires_approval: bool = False, user_id: str | None = None,
):
    """The canonical approval subject of one step (gate, review lookup, card, denial).

    A step naming a raw tool is canonicalised to its classified action, so
    every place that names the step's action names the same key. ``user_id``
    is the owner of an ad-hoc step (``_adhoc_step_owner``): it is judged under
    that user's own rules.
    """
    from packages.core.governance.approvals import ApprovalSubject, canonical_step_subject

    return canonical_step_subject(ApprovalSubject(
        entity_id=step.entity_id,
        workspace_id=step.workspace_id,
        user_id=user_id,
        action_key=step.action_key,
        capability_id=capability_id,
        risk_level=getattr(step, "risk_level", None) or "low",
        kind=getattr(step, "kind", None) or "action",
        requires_approval=requires_approval,
        provider=getattr(step, "provider", None),
        arguments=dict(getattr(step, "params", None) or {}),
    ))


#: Per step provider, an async filter that may rewrite a completed lease's
#: result before it is stored: e.g. a screenshot moved out of the step row
#: into short-lived storage (packages/core/services/computer/results.py).
LEASE_RESULT_FILTERS: dict[str, Any] = {}


def register_lease_result_filter(provider: str, result_filter: Any) -> None:
    LEASE_RESULT_FILTERS[provider] = result_filter


def _load_lease_result_filters() -> None:
    return None


async def _filter_lease_result(step: Any, result: Any) -> Any:
    """The result as stored: a provider's filter applied; the raw result if it fails."""
    if result is MISSING_RESULT:
        return result
    _load_lease_result_filters()
    result_filter = LEASE_RESULT_FILTERS.get(str(getattr(step, "provider", "") or ""))
    if result_filter is None:
        return result
    try:
        return await result_filter(step, result)
    except Exception:  # noqa: BLE001 - storing the raw result beats losing it
        logger.warning("dispatcher: result filter for %s failed", step.provider, exc_info=True)
        return result


def _adhoc_step_owner(plan: Any, step: Any) -> str | None:
    """The user an ad-hoc step (no Workspace) was sent for, from server-written plan state.

    ``submit_local_action`` records it; a worker can never write it. Such a
    step is judged under that user's rules and its approval belongs to them.
    """
    if getattr(step, "workspace_id", None) or getattr(plan, "workspace_id", None):
        return None
    state = plan.dispatcher_state if isinstance(getattr(plan, "dispatcher_state", None), dict) else {}
    owner = str(state.get("owner_user_id") or "").strip()
    return owner or None


def _owner_acted_in_person(plan: Any, owner_user_id: str | None) -> bool:
    """The step is its owner's own action, taken at a signed-in session.

    Approval asks a person about what an agent does for them; a person's own
    click is not asked about. Only server code that authenticated the person
    (not an agent token) writes this consent, and only for themselves.
    """
    state = plan.dispatcher_state if isinstance(getattr(plan, "dispatcher_state", None), dict) else {}
    consent = state.get("consent")
    return bool(
        owner_user_id
        and isinstance(consent, dict)
        and consent.get("kind") == "human"
        and consent.get("user_id") == owner_user_id
    )


async def _carried_tool_call_grant(db: AsyncSession, *, plan: Any, step: Any, subject: Any, owner_user_id: str | None):
    """The approved tool call this step was dispatched for, verified and spent on this step only.

    An in-run agent's tool call already crossed the runtime gate; when a person
    approved it, the step it dispatches must not ask again. The plan's
    server-written ``consent`` names that decision and the tool call's
    arguments; it counts only for the same user's approved tool call of
    exactly this action and these arguments, and only for one step.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
    from packages.core.models.hitl_request import HitlRequest

    state = plan.dispatcher_state if isinstance(getattr(plan, "dispatcher_state", None), dict) else {}
    consent = state.get("consent")
    if not owner_user_id or not isinstance(consent, dict) or consent.get("kind") != "granted":
        return None
    request = (
        await db.execute(
            select(HitlRequest)
            .where(HitlRequest.id == str(consent.get("request_id") or ""))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        request is None
        or request.entity_id != step.entity_id
        or request.origin_kind != ApprovalOriginKind.TOOL_CALL.value
        or request.status not in (ApprovalStatus.GRANTED.value, ApprovalStatus.CONSUMED.value)
        or request.action_key not in {subject.action_key, step.action_key}
    ):
        return None
    context = dict(request.context or {})
    if owner_user_id not in {context.get("requested_by"), request.decided_by_user_id}:
        return None
    # The step must be built from exactly the arguments the person approved:
    # the consent carries the tool call's arguments, which must hash to the
    # request's own.
    approved_hash = str(context.get("args_hash") or "")
    arguments = consent.get("arguments")
    if approved_hash:
        from packages.core.ai.runtime.approval_messages import approval_args_hash

        if not isinstance(arguments, dict) or approval_args_hash(
            arguments, tool_name=str(consent.get("tool_name") or ""),
        ) != approved_hash:
            return None
    carried_to = context.get("carried_to_step")
    if carried_to and carried_to != step.id:
        return None
    if carried_to != step.id:
        request.context = {**context, "carried_to_step": step.id}
        flag_modified(request, "context")
    return request


async def _failed_step_review_operation_id(db: AsyncSession, step: ExecutionStep) -> str | None:
    """A recovery decision belongs to a failed lease, never its spent execution grant."""
    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.dispatcher.step_state import step_approval_operation_id

    operation_id = step_approval_operation_id(step)
    # Grants are minted under the canonical key; rows minted before
    # canonicalisation keep the step's raw key.
    action_keys = {step.action_key, _step_policy_subject(step).action_key} - {None}
    prior = await db.scalar(select(HitlRequest.id).where(
        HitlRequest.entity_id == step.entity_id,
        HitlRequest.workspace_id == step.workspace_id,
        HitlRequest.origin_kind == ApprovalOriginKind.STEP.value,
        HitlRequest.origin_step_id == step.id,
        HitlRequest.origin_plan_id == step.plan_id,
        (HitlRequest.action_key.in_(action_keys) if action_keys else HitlRequest.action_key.is_(None)),
        HitlRequest.dedup_key == f"operation:{operation_id}",
        HitlRequest.status.in_([ApprovalStatus.CONSUMED.value, ApprovalStatus.EXPIRED.value]),
    ).order_by(HitlRequest.created_at.desc(), HitlRequest.id.desc()).limit(1))
    if prior is None:
        return None
    lease_id = await db.scalar(select(WorkLease.id).where(
        WorkLease.entity_id == step.entity_id,
        WorkLease.step_id == step.id,
        WorkLease.plan_id == step.plan_id,
        WorkLease.status.in_([WorkLeaseStatus.FAILED.value, WorkLeaseStatus.EXPIRED.value]),
    ).order_by(WorkLease.created_at.desc(), WorkLease.id.desc()).limit(1))
    return f"{operation_id}:failure:{lease_id}" if lease_id else None


def _is_system_file_effect(ref: Any) -> bool:
    """File execution failures belong to Manor, not a customer verdict.

    This classifies who owns recovery, never whether a write committed. Only
    the narrower journal contract below can supply that execution evidence.
    """
    fields = ref.get("fields") if isinstance(ref, dict) else None
    return bool(
        isinstance(fields, dict) and ref.get("kind") == "tool_effect"
        and isinstance(fields.get("tool"), str) and fields["tool"] and not fields["tool"].startswith("mcp__")
        and fields.get("provider") in (None, "") and fields.get("capability_id") == "file.write"
        and isinstance(fields.get("action_key"), str) and fields["action_key"] in (
            "workspace.file.create", "workspace.file.modify", "workspace.file.write", "workspace.file.delete",
        )
        and fields.get("classification") in (None, "action")
        and ToolEffectEvidenceFactory.has_valid_effect_id(ref)
    )


def _failure_checkpoint(
    step: ExecutionStep, supplied: Any, *, persisted_evidence: list | None = None,
) -> dict | None:
    """Never replace an exact pending call with a transcript that lost it."""
    current = step.runtime_checkpoint
    if not isinstance(supplied, dict):
        return copy.deepcopy(current) if isinstance(current, dict) else None
    if supplied == current:
        return copy.deepcopy(current)
    from packages.core.dispatcher.effect_safety import _checkpoint_contains_tool_result, _checkpoint_proves_tool_effect

    evidence = list(getattr(step, "evidence_refs", None) or [])
    authoritative = evidence if persisted_evidence is None else persisted_evidence
    proven_ids = set()
    for effect in evidence:
        if not isinstance(effect, dict) or effect.get("kind") != "tool_effect":
            continue
        fields = effect.get("fields")
        if not isinstance(fields, dict):
            return copy.deepcopy(current) if isinstance(current, dict) else None
        call_id = fields.get("tool_call_id")
        proven = [ref for ref in authoritative if (
            isinstance(ref, dict) and ref.get("kind") == "tool_effect"
            and ref.get("effect") in ("action", "publish")
            and ToolEffectEvidenceFactory.identity(ref) == ToolEffectEvidenceFactory.identity(effect)
        )]
        if not any(_checkpoint_proves_tool_effect(
            supplied, ref, evidence_refs=authoritative, owner_step=step,
        ) for ref in proven):
            # Worker-supplied transcript/evidence is not a new proof of a
            # pending host action. Only a previously persisted exact return
            # can replace that frame; otherwise use the journal/current state.
            return copy.deepcopy(current) if isinstance(current, dict) else None
        proven_ids.add(str(call_id or ""))
    if isinstance(current, dict):
        from packages.core.ai.runtime.nested_agent import runtime_checkpoint_leaf_call_id

        try:
            pending_id = runtime_checkpoint_leaf_call_id(current)
        except (KeyError, TypeError, ValueError):
            pending_id = None
        recorded_ids = {
            str((ref.get("fields") or {}).get("tool_call_id") or "")
            for ref in getattr(step, "evidence_refs", None) or []
            if isinstance(ref, dict) and isinstance(ref.get("fields"), dict)
            and _checkpoint_contains_tool_result(current, ref["fields"].get("tool_call_id"))
        }
        if pending_id:
            recorded_ids.add(pending_id)
        if any(not _checkpoint_contains_tool_result(supplied, call_id) for call_id in recorded_ids - proven_ids):
            # A later exact pending frame is acceptable only if it contains
            # the earlier call's observation. Otherwise retain the host frame.
            return copy.deepcopy(current)
    return copy.deepcopy(supplied)


async def _recover_internal_file_effects(
    db: AsyncSession, lease: WorkLease, step: ExecutionStep,
) -> tuple[bool, list[dict[str, Any]]]:
    """Observe committed local writes under the caller's execution fence.

    This never executes a tool or infers a negative outcome. Storage/query
    failures propagate; only absent or mismatched proof is an evidence gap.
    """
    from packages.core.ai.runtime.nested_agent import runtime_checkpoint_tool_owner
    from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash, _tool_result_canonical_sha256
    from packages.core.services.runtime_file_effect_recovery import (
        is_runtime_file_effect, read_runtime_file_effect_receipt,
    )

    evidence = _merge_durable_evidence_refs(step.evidence_refs, lease.evidence_refs)
    markers = [ref for ref in evidence if isinstance(ref, dict) and ref.get("kind") in (
        "tool_effect_reconciliation", "tool_result_handoff",
    )]
    recovered = False
    unresolved = []
    for ref in evidence:
        if not _is_system_file_effect(ref) or not await _has_ambiguous_external_effect(
            db, [ref, *markers], owner_step=step,
        ):
            continue
        if not is_runtime_file_effect(ref):
            unresolved.append(ref)
            continue
        fields = ref.get("fields") or {}
        checkpoint = copy.deepcopy(step.runtime_checkpoint)
        try:
            leaf = runtime_checkpoint_tool_owner(checkpoint, str(fields.get("tool_call_id") or ""))
            pending = leaf["pending_tool_call"]
            matches = (
                pending.get("name") == fields.get("tool")
                and _tool_arguments_hash(pending.get("arguments") or {}) == fields.get("arguments_hash")
            )
        except (KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            unresolved.append(ref)
            continue
        task_id = await db.scalar(select(ExecutionPlan.task_id).where(ExecutionPlan.id == step.plan_id))
        receipt = await read_runtime_file_effect_receipt(
            db, entity_id=step.entity_id, workspace_id=step.workspace_id,
            task_id=task_id, plan_id=step.plan_id, step_id=step.id, effect_ref=ref,
            runtime_checkpoint=checkpoint,
        )
        if receipt is None or receipt.operation_status != "committed":
            unresolved.append(ref)
            continue
        serialized_observation = json.dumps(receipt.observation, ensure_ascii=False)
        leaf["pending_tool_result"] = {"id": pending["id"], "content": serialized_observation}
        leaf.setdefault("tool_continuations", {}).setdefault(pending["id"], {})["runtime_file_recovery"] = {
            "journal_ids": list(receipt.journal_ids),
            "effect_id": ToolEffectEvidenceFactory.effect_id(ref),
        }
        terminal = ToolEffectEvidenceFactory.bind_effect_id(ToolEffectEvidenceFactory.effect(
            ToolEffectOutcome.ACTION,
            {
                **fields, "intent_state": "returned", "runtime_file_journal_ids": list(receipt.journal_ids),
                "result_sha256": hashlib.sha256(serialized_observation.encode("utf-8")).hexdigest(),
                "result_canonical_sha256": _tool_result_canonical_sha256(serialized_observation),
                "runtime_file_observation": copy.deepcopy(receipt.observation),
                "runtime_file_original_effect": copy.deepcopy(ref),
            },
        ))
        if ref.get("coordinator_child_step_id"):
            terminal["coordinator_child_step_id"] = ref["coordinator_child_step_id"]
            terminal = ToolEffectEvidenceFactory.bind_effect_id(terminal)
        # A lost native response and this explicit journal observation have
        # different result hashes. Retain the original receipt inside the
        # replacement instead of leaving an obsolete executable observation.
        step.evidence_refs = _merge_durable_evidence_refs(
            [item for item in step.evidence_refs or [] if item != ref], [terminal],
        )
        lease.evidence_refs = _merge_durable_evidence_refs(
            [item for item in lease.evidence_refs or [] if item != ref], [terminal],
        )
        step.runtime_checkpoint = checkpoint
        recovered = True
    return recovered, unresolved


def _internal_file_recovery_error(
    error: dict, effects: list[dict[str, Any]], *, runtime_checkpoint: dict | None = None,
    output_repair_exhausted: bool = False,
) -> dict:
    from packages.core.ai.runtime.nested_agent import runtime_checkpoint_tool_owner
    from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash
    from packages.core.contracts.file_engine import FilePatchOperation, PATCH_OPERATION_ALIASES

    def display_path(value: Any) -> str | None:
        return value.strip()[:200] if isinstance(value, str) and value.strip() and value.isprintable() else None

    file_operations = []
    summaries: list[str] = []
    for ref in effects:
        fields = ref.get("fields") if isinstance(ref.get("fields"), dict) else {}
        observation = fields.get("runtime_file_observation") or {}
        files = observation.get("files") if isinstance(observation, dict) else None
        candidates = [fields.get(key) for key in ("path", "file_path", "target_path", "project_path")]
        if isinstance(files, list):
            candidates.extend(item.get("path") for item in files if isinstance(item, dict))
        tool = fields.get("tool")
        operation = {
            "patch_file": "patch", "replace_file": "replace", "delete_file": "delete",
            "generate_file": "create", "video_edit": "video edit",
        }.get(tool, "file operation") if isinstance(tool, str) else "file operation"
        context: dict[str, Any] = {"operation": operation}
        # Only the selected host frame may supply missing display metadata.
        # Its target describes the attempted call, never proves it committed.
        try:
            leaf = runtime_checkpoint_tool_owner(runtime_checkpoint, str(fields.get("tool_call_id") or ""))
            pending = leaf["pending_tool_call"]
            arguments = pending.get("arguments")
            matches = (
                pending.get("name") == tool and isinstance(arguments, dict)
                and _tool_arguments_hash(arguments) == fields.get("arguments_hash")
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            matches = False
        if matches:
            context["source"] = "host_pending_checkpoint"
            candidates.extend(arguments.get(key) for key in ("path", "file_path", "target_path", "project_path", "output_path"))
            if source_path := display_path(arguments.get("source_path")):
                context["source_path"] = source_path
            if tool == "generate_file" and (name := display_path(arguments.get("name"))):
                context["requested_name"] = name
            patches = arguments.get("operations")
            if tool == "patch_file" and isinstance(patches, list):
                context["patch_operation_count"] = len(patches)
                allowed = FilePatchOperation.values() + list(PATCH_OPERATION_ALIASES)
                context["patch_operation_types"] = list(dict.fromkeys(
                    item["op"] for item in patches if isinstance(item, dict)
                    and isinstance(item.get("op"), str) and item["op"] in allowed
                ))[:8]
                kinds = ", ".join(context["patch_operation_types"])
                operation += f" ({len(patches)} operations{': ' + kinds if kinds else ''})"
            elif tool == "video_edit" and arguments.get("operation") in (
                "start", "prepare", "push_assets", "write_files", "patch_files", "sync", "review", "render", "close",
            ):
                context["operation"] = operation = f"video edit: {arguments['operation']}"
        paths: list[str] = []
        for value in candidates:
            path = display_path(value)
            if path and path not in paths:
                paths.append(path)
        context["targets"] = paths[:3]
        file_operations.append({**copy.deepcopy(fields), "display_context": context})
        summary = operation + (f" for {', '.join(paths[:3])}" if paths else "")
        if context.get("requested_name") and not paths:
            summary += f" (requested name: {context['requested_name']})"
        if summary not in summaries:
            summaries.append(summary)
    target = "; ".join(summaries[:3]) or "internal file operation"
    reason = (
        f"The file operation ({target}) has a verified result, but the AI could not finish "
        "the required output within its retry budget. Execution stopped with the file evidence retained."
        if output_repair_exhausted else
        f"The system could not verify the exact result of the internal file operation ({target}). "
        "Execution stopped without repeating it, and its recorded evidence was retained."
    )
    return {
        **error, "type": INTERNAL_FILE_RECOVERY_FAILED, "message": reason,
        "cause": error,
        "recovery": {
            "status": "failed", "reason": "output_repair_exhausted" if output_repair_exhausted else "exact_result_unavailable",
            "effect_ids": [ToolEffectEvidenceFactory.effect_id(ref) for ref in effects],
            "file_operations": file_operations,
        },
        "failure": {"reason": reason, "retryable": False, "requires_human": False, "blockers": []},
    }


def _worker_lease_limit(capabilities: Any) -> int:
    """Return the persisted worker concurrency ceiling, failing closed."""

    raw_limit = (
        capabilities.get("max_concurrent_leases", 1)
        if isinstance(capabilities, dict)
        else 1
    )
    if isinstance(raw_limit, bool):
        return 1
    try:
        parsed = int(raw_limit)
    except (TypeError, ValueError):
        return 1
    return min(max(parsed, 1), 64)


async def _lock_worker_lease_admission(
    db: AsyncSession,
    *,
    worker_id: str,
) -> None:
    """Serialize capacity accounting without taking the Worker row first.

    Checkout later locks Workspace/Plan/Step rows.  A Worker row lock here
    would invert the completion path's Workspace -> ... -> Worker order, so a
    transaction-scoped advisory lock owns only this worker's admission lane.
    """

    bind = db.get_bind()
    if bind is None or getattr(bind.dialect, "name", None) != "postgresql":
        return

    from packages.core.constants.advisory_locks import AdvisoryLockNamespace

    with db.no_autoflush:
        await db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                ":namespace, hashtext(:worker_id))"
            ),
            {
                "namespace": int(AdvisoryLockNamespace.WORKER_LEASE_ADMISSION),
                "worker_id": worker_id,
            },
        )


def _merge_durable_evidence_refs(
    existing: Any,
    incoming: Any,
) -> list[dict[str, Any]]:
    """Merge worker reports without erasing pre-I/O effect intents."""

    return ToolEffectEvidenceFactory.merge_untrusted(existing, incoming)


def _expired_external_action_attempt_evidence(
    lease: WorkLease,
    step: ExecutionStep,
) -> dict[str, Any]:
    """Materialize the exact dispatched action as an unknown effect.

    The tokenized WorkLease is committed before its DTO is returned, so it is
    the durable pre-handler intent for an external process. If that process
    outlives the lease without reporting a receipt, the provider outcome is
    unknown; record the attempt identity as an unconfirmed effect instead of
    silently replaying it.
    """

    raw_params = step.params if isinstance(step.params, dict) else {}
    effect_generation = str(
        raw_params.get(ACTION_EFFECT_GENERATION_PARAM_KEY)
        or step.attempt_count
        or 1
    )
    arguments = executable_action_params(
        raw_params,
        human_input_response=step.human_input_response,
    )
    arguments_hash = hashlib.sha256(json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    provider = str(step.provider or "")
    action_key = str(step.action_key or "")
    tool = ".".join(part for part in (provider, action_key) if part)
    return ToolEffectEvidenceFactory.bind_effect_id(
        ToolEffectEvidenceFactory.effect(
            ToolEffectOutcome.PENDING,
            {
                "tool": tool or step.step_key,
                "provider": provider or None,
                "action_key": action_key or None,
                "integration_id": step.integration_id,
                "arguments_hash": arguments_hash,
                "tool_call_id": f"external-lease:{lease.id}",
                "effect_generation": effect_generation,
                "owner_plan_id": step.plan_id,
                "owner_step_id": step.id,
                "external_attempt_state": "expired_outcome_unknown",
                "lease_id": lease.id,
                "delivery_confirmed": False,
            },
        )
    )


async def _has_ambiguous_external_effect(
    db: AsyncSession,
    evidence_refs: Any,
    *,
    owner_step: ExecutionStep,
) -> bool:
    """Compatibility wrapper around the shared retry/lease safety gate."""

    from packages.core.dispatcher.effect_safety import (
        has_ambiguous_external_effect,
    )

    return await has_ambiguous_external_effect(
        db,
        evidence_refs,
        owner_step=owner_step,
    )


class _MissingResult:
    """Internal marker for an omitted worker ``result`` field.

    ``None`` is a valid JSON value for nullable hard contracts, so API/SDK
    boundaries pass this marker when the field was absent instead of collapsing
    omission and explicit null into the same Python value.
    """

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "MISSING_RESULT"


MISSING_RESULT: Any = _MissingResult()


def _reported_worker_failure(
    result: Any,
    *,
    step_kind: str,
) -> dict[str, Any] | None:
    """Interpret the closed StepResult status only for agent step kinds."""
    if step_kind not in {"llm", "subagent"} or not isinstance(result, dict):
        return None
    raw_status = result.get("status")
    if raw_status is None:
        return None
    from packages.core.contracts.envelope import StepResultStatus

    try:
        status = StepResultStatus(str(raw_status))
    except ValueError:
        return {
            "type": "InvalidStepResultStatus",
            "message": f"invalid StepResult status {raw_status!r}",
        }
    if status is not StepResultStatus.FAILED:
        return None
    failure = result.get("failure")
    reason = (
        str(failure.get("reason") or "").strip()
        if isinstance(failure, dict)
        else ""
    )
    error: dict[str, Any] = {
        "type": "WorkerReportedFailure",
        "message": reason or str(
            result.get("summary") or "step reported a failed StepResult"
        ),
    }
    if isinstance(failure, dict) and failure:
        error["failure"] = failure
    return error


_RISK_RANK = {"low": 0, "medium": 1, "high": 2}
_DEFAULT_LEASE_TTL = timedelta(seconds=300)
# execute_lease shares CeleryQueue.HEAVY with run_strategist_review
# (hard limit 1800s). The 300s TTL is in-process liveness after execute
# starts; a never-heartbeated lease is still queued, not dead.
_QUEUED_LEASE_GRACE = timedelta(seconds=1800)


class DispatchError(Exception):
    """Generic dispatcher failure."""


class NoMatchingSteps(DispatchError):
    """No runnable steps matched the worker's capabilities."""


class LeaseNotActive(DispatchError):
    """The lease being completed/failed/extended isn't in 'active' state."""


class RuntimeActionSchemaUnavailable(DispatchError):
    """An official action has neither materialized nor actor-scoped input schema."""


def _counts_towards_worker_quarantine(error: dict | None) -> bool:
    """Return whether a failure indicates an unhealthy worker runtime.

    Local diagnostics are expected operator/setup feedback: missing browser
    permissions, not logged in, or Chrome inspection disabled. Those should
    fail the step and surface to the UI, but they should not quarantine the
    whole local worker.

    Recognized transient failures are excluded for the same reason, and for
    a sharper one: they are now retried up to six times in a row, and five
    consecutive failures quarantine a non-internal worker. Counting them
    would let one offline local worker take the whole queue down.
    """
    if not isinstance(error, dict):
        return True
    err_type = str(error.get("type") or "")
    err_code = str(error.get("code") or "")
    if err_type == "LocalDiagnosticFailed" or err_code == "local_diagnostic_failed":
        return False
    from packages.core.governance.error_signatures import classify_execution_error

    hint = classify_execution_error(error)
    if hint is not None and hint.is_transient:
        return False
    return True


def _validation_failure_debug(result: Any) -> dict[str, Any]:
    try:
        preview = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        preview = str(result)

    debug: dict[str, Any] = {"result_preview": preview[:1200]}
    if isinstance(result, dict):
        debug["result_keys"] = sorted(str(key) for key in result.keys())[:40]
    return debug


def _attach_knowledge_artifacts_for_contract(
    result: dict[str, Any],
    projection: Any,
    *,
    output_contract: Any,
    step_kind: str | None = None,
) -> dict[str, Any]:
    """Attach knowledge handles without widening a native payload contract.

    ``attach_knowledge_artifacts`` predates bare PlanStep/action contracts and
    unconditionally adds envelope-era keys (``knowledge_artifacts``,
    ``document_id``, ``viewer_url``, ...).  Those additions are useful on the
    legacy StepResult and unmarked free-form agent paths, but they can turn a
    validated ``additionalProperties: false`` native payload into a value that
    no longer obeys its schema.  Keep the producer's original keys and permit
    only fields explicitly declared by a native structured schema; the
    Knowledge projection itself is still completed for indexing/evidence.
    """
    from packages.core.services.artifact_knowledge import attach_knowledge_artifacts

    enriched = attach_knowledge_artifacts(result, projection)
    if not output_contract.preserves_native_payload_for(step_kind):
        return enriched

    payload_schema = (
        getattr(output_contract, "payload_schema", None)
        or getattr(output_contract, "schema", None)
    )
    properties = (
        payload_schema.get("properties")
        if isinstance(payload_schema, dict)
        else None
    )
    declared_keys = (
        {str(key) for key in properties}
        if isinstance(properties, dict)
        else set()
    )
    original_keys = {str(key) for key in result}
    return {
        key: value
        for key, value in enriched.items()
        if str(key) in original_keys or str(key) in declared_keys
    }


def _target_worker_id_for_plan(plan: ExecutionPlan) -> str | None:
    """Return an explicit worker routing constraint for ad-hoc local actions."""
    for container in (plan.dispatcher_state, plan.plan_dag):
        if isinstance(container, dict):
            target = container.get("target_worker_id")
            if target:
                return str(target)
    return None


def _plan_source(plan: ExecutionPlan) -> str:
    for container in (plan.dispatcher_state, plan.plan_dag):
        if isinstance(container, dict):
            source = container.get("source")
            if source:
                return str(source)
    return ""


def _step_runtime_capability_id(step: ExecutionStep) -> str | None:
    materialized = str(getattr(step, "capability_id", None) or "").strip()
    if materialized:
        return materialized
    return runtime_capability_id_for_action_key(
        getattr(step, "action_key", None),
        provider=getattr(step, "provider", None),
    )


async def _hydrate_runtime_action_binding_schemas(
    db: AsyncSession,
    step: ExecutionStep,
) -> bool:
    """Refresh action schemas without crossing the resolved actor boundary."""

    if step.kind != "action":
        return False
    provider = str(step.provider or "").strip()
    action_key = str(step.action_key or "").strip()
    if not provider or not action_key:
        return False

    from packages.core.models.mcp import MCPServer

    from packages.core.services.official_remote_mcp import OfficialRemoteMCPProvider

    official_provider = provider in {
        item.value for item in OfficialRemoteMCPProvider
    }
    server = (
        await db.execute(
            select(MCPServer).where(
                MCPServer.server_key == provider,
                MCPServer.status == "active",
            ).limit(1)
        )
    ).scalar_one_or_none()
    if server is None:
        if official_provider:
            raise RuntimeActionSchemaUnavailable(
                f"Active MCP server is unavailable for {provider}.{action_key}"
            )
        return False
    tools_cached = server.tools_cached

    if official_provider:
        from packages.core.models.execution import ExecutionPlan
        from packages.core.models.task import Task
        from packages.core.services.mcp_account_tool_catalog import (
            actor_provider_mcp_tool_caches,
        )
        from packages.core.services.task_requester_identity import (
            TaskRequesterIdentityError,
            resolve_task_execution_user_id,
        )

        task = (
            await db.execute(
                select(Task)
                .join(ExecutionPlan, ExecutionPlan.task_id == Task.id)
                .where(ExecutionPlan.id == step.plan_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        try:
            actor_user_id = (
                await resolve_task_execution_user_id(db, task)
                if task is not None
                else None
            )
        except TaskRequesterIdentityError:
            logger.info(
                "Dispatcher step %s has no executable user; account MCP catalog is unavailable",
                step.id,
            )
            actor_user_id = None
        discovered_caches: dict[str, dict[str, Any]] = {}
        catalog_lookup_failed = False
        if actor_user_id:
            try:
                discovered_caches = await actor_provider_mcp_tool_caches(
                    db,
                    provider_endpoints={provider: server.endpoint},
                    user_id=str(actor_user_id),
                    entity_id=step.entity_id,
                )
            except Exception:
                catalog_lookup_failed = True
                logger.warning(
                    "Dispatcher could not load actor-scoped MCP catalog for %s; preserving materialized schemas",
                    provider,
                    exc_info=True,
                )
        tools_cached = discovered_caches.get(provider)
        if not tools_cached:
            if catalog_lookup_failed and isinstance(
                step.expected_input_schema,
                dict,
            ):
                return False
            raise RuntimeActionSchemaUnavailable(
                f"Actor-scoped schema is unavailable for {provider}.{action_key}"
            )
    if not tools_cached:
        return False

    action_specs = runtime_planner_action_specs_from_tools_cached(tools_cached)
    binding = runtime_planner_action_binding_for(
        provider=provider,
        action_key=action_key,
        provider_actions={provider: list(action_specs.keys())},
        provider_action_specs={provider: action_specs},
    )
    if binding is None:
        if official_provider:
            raise RuntimeActionSchemaUnavailable(
                f"Actor-scoped action is unavailable for {provider}.{action_key}"
            )
        return False

    hydrated = False
    selection = (step.params or {}).get(INTEGRATION_ACCOUNT_SELECTION_ARGUMENT)
    input_schema = binding.input_schema_for(
        step.integration_id,
        selection=selection,
    )
    output_schema = binding.output_schema_for(
        step.integration_id,
        selection=selection,
    )
    if input_schema is not None and step.expected_input_schema != input_schema:
        step.expected_input_schema = input_schema
        hydrated = True
    if output_schema is not None and step.expected_output_schema != output_schema:
        step.expected_output_schema = output_schema
        hydrated = True
    return hydrated


async def _agent_allows_action_step(
    db: AsyncSession,
    *,
    agent_id: str | None,
    provider: str | None,
    action_key: str | None,
) -> tuple[bool, dict[str, Any] | None]:
    """Check the resolved service agent owns the MCP action being leased.

    Thin wrapper over the predicate the planner also uses
    (``agent_action_binding_allowed``): a step that planned fine must not be
    able to fail at lease time on a second, drifting reading of the same
    binding tables. Looked up at call time so tests can patch the shared
    function.
    """
    from packages.core.services import agent_action_binding

    return await agent_action_binding.agent_action_binding_allowed(
        db,
        agent_id=agent_id,
        provider=provider,
        action_key=action_key,
    )


def _stamp_dispatch_skip(
    step: ExecutionStep,
    reason: DispatchSkipReason,
    message: str,
    *,
    structural: bool,
    now: datetime,
) -> None:
    """Record why the dispatcher declined to lease ``step`` this pass.

    The record lives under ``step.error[DISPATCH_SKIP_KEY]`` so a real prior
    execution error (retry feedback, ``next_retry_at``) survives the stamp;
    when there is no real error the whole ``error`` becomes a
    ``DispatchSkipped`` marker. ``since`` is preserved while the reason is
    unchanged so the executor's watchdog can measure how long the step has
    been undispatchable for one reason. ``step.error`` is always reassigned:
    JSONB change detection only sees a new object.
    """
    existing = step.error if isinstance(step.error, dict) else None
    prior = (existing or {}).get(DISPATCH_SKIP_KEY)
    since = now.isoformat()
    if (
        isinstance(prior, dict)
        and str(prior.get("reason") or "") == reason.value
        and prior.get("since")
    ):
        since = str(prior["since"])
    record = {
        "reason": reason.value,
        "message": message,
        "since": since,
        "structural": bool(structural),
    }
    if existing is None or existing.get("type") == DISPATCH_SKIPPED_ERROR_TYPE:
        step.error = {
            "type": DISPATCH_SKIPPED_ERROR_TYPE,
            "message": message,
            DISPATCH_SKIP_KEY: record,
        }
        return
    merged = dict(existing)
    merged[DISPATCH_SKIP_KEY] = record
    step.error = merged


def _clear_dispatch_skip_on_grant(step: ExecutionStep) -> None:
    """A lease went out: the skip record is stale. Keep any real error."""
    error = step.error
    if not isinstance(error, dict):
        return
    if error.get("type") == DISPATCH_SKIPPED_ERROR_TYPE:
        step.error = None
    elif DISPATCH_SKIP_KEY in error:
        step.error = {k: v for k, v in error.items() if k != DISPATCH_SKIP_KEY}


def _worker_supported_capability_ids(capabilities: dict[str, Any]) -> set[str] | None:
    """Return explicit worker Runtime capability allowlist, if declared."""
    raw = (
        capabilities.get("supported_capabilities")
        or capabilities.get("runtime_capabilities")
        or capabilities.get("capability_ids")
    )
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in raw if str(value or "").strip()}


def _step_intrinsic_approval_reason(step: ExecutionStep, capability_id: str | None) -> tuple[str, str] | None:
    """Return (matched_rule, reason) when the step itself requires HITL."""
    if getattr(step, "requires_approval", False):
        subject = step.action_key or capability_id or step.step_key
        return (
            "step.requires_approval",
            f"Step {step.step_key!r} requires operator approval before dispatching {subject!r}.",
        )
    if (step.risk_level or "low") == "high":
        subject = step.action_key or capability_id or step.step_key
        return (
            "step.high_risk",
            f"Step {step.step_key!r} is high risk and needs one-time operator approval before dispatching {subject!r}.",
        )
    return None


# ── Public API ────────────────────────────────────────────────────────


class Dispatcher:
    """Stateless service object — one per process.

    Methods take a DB session (caller controls transaction boundaries).
    Concurrency is handled at the SQL layer via FOR UPDATE SKIP LOCKED;
    no in-process locking required.
    """

    def __init__(self, *, lease_ttl: timedelta = _DEFAULT_LEASE_TTL):
        self._lease_ttl = lease_ttl

    # ── Checkout (the hot path) ───────────────────────────────────────

    async def checkout_steps_for_worker(
        self,
        db: AsyncSession,
        worker: Worker,
        *,
        max_n: int = 1,
        plan_id: Optional[str] = None,
    ) -> list[tuple[WorkLease, ExecutionStep]]:
        """Atomically claim up to ``max_n`` runnable steps for this worker.

        ``max_n`` is only the caller's requested batch size.  The persisted
        ``max_concurrent_leases`` ceiling and current ACTIVE lease count are
        authoritative.

        ``plan_id`` filter is for testing / scoped runs; production
        callers leave it None and the worker picks across all plans.

        Returns a list of (lease, step) tuples. Caller commits.
        """
        from packages.core.observability import span

        async with span(
            "dispatcher.checkout",
            attributes={
                "worker.id": worker.id,
                "worker.kind": worker.kind,
                "max_n": max_n,
                "plan_id": plan_id,
            },
        ) as _sp:
            return await self._checkout_steps_for_worker_inner(
                db,
                worker,
                max_n=max_n,
                plan_id=plan_id,
                _sp=_sp,
            )

    async def _checkout_steps_for_worker_inner(
        self,
        db: AsyncSession,
        worker: Worker,
        *,
        max_n: int,
        plan_id: Optional[str],
        _sp: Any = None,
    ) -> list[tuple[WorkLease, ExecutionStep]]:
        requested_max_n = max(int(max_n), 0)
        if requested_max_n == 0:
            return []

        # Per-step SKIP LOCKED prevents two workers from claiming one Step, but
        # it does not prevent two concurrent heartbeats for the *same* worker
        # from each claiming a different Step.  Serialize only that worker's
        # admission transaction, then derive remaining capacity from persisted
        # state.  The advisory lock is intentionally acquired before any
        # Workspace/Plan/Step row lock and does not lock the Worker row.
        await _lock_worker_lease_admission(db, worker_id=worker.id)
        with db.no_autoflush:
            stored_worker = (await db.execute(
                select(Worker.status, Worker.capabilities).where(
                    Worker.id == worker.id,
                )
            )).one_or_none()
        if stored_worker is None or stored_worker.status != WorkerStatus.ACTIVE:
            return []

        active_lease_count = int((await db.scalar(
            select(func.count(WorkLease.id)).where(
                WorkLease.worker_id == worker.id,
                WorkLease.status == WorkLeaseStatus.ACTIVE.value,
            )
        )) or 0)
        remaining_capacity = max(
            _worker_lease_limit(stored_worker.capabilities) - active_lease_count,
            0,
        )
        max_n = min(requested_max_n, remaining_capacity)
        if max_n == 0:
            return []

        # Use the same fresh persisted snapshot for every capability gate;
        # the caller's ORM instance may predate an operator update.
        capabilities = dict(stored_worker.capabilities or {})
        supported_kinds = capabilities.get("supported_kinds") or []
        if not supported_kinds:
            return []

        # ── Worker-level budget gate ──
        # Workspace gate happens per-step inside the loop because one
        # checkout can pull steps across multiple workspaces.
        from packages.core.budget import check_worker_budget

        worker_ok, worker_reason = check_worker_budget(worker)
        if not worker_ok:
            logger.info("dispatcher: worker %s skipped — %s", worker.id, worker_reason)
            return []

        max_risk = _RISK_RANK.get(capabilities.get("max_risk_level") or "low", 0)
        allowed_risk_levels = [
            level for level, rank in _RISK_RANK.items()
            if rank <= max_risk
        ]
        supported_providers = capabilities.get("supported_providers")
        # supported_providers=None means "any provider"; missing key
        # treated the same; explicit empty list means "no providers".
        supported_capabilities = _worker_supported_capability_ids(capabilities)
        # supported_capabilities=None means "any Runtime capability"; an
        # explicit empty list means this worker accepts no capability-scoped
        # leases.

        # Subquery: subscription_ids this worker is bound to.
        bound_subs = select(SubscriptionWorker.subscription_id).where(
            SubscriptionWorker.worker_id == worker.id,
        )

        # Pre-build the candidate query. The authoritative dependency check
        # is still ``_check_dependency_status`` in Python below; the SQL
        # exclusion only keeps steps with an unfinished dependency out of the
        # bounded candidate window. Without it, a handful of older dependents
        # waiting on one running upstream fill the whole ``max_n * 4`` window
        # on every heartbeat and a newer plan's root step is never even seen.
        from sqlalchemy.orm import aliased

        from packages.core.services.task_execution_origin import plan_manual_execution_predicate

        dependency = aliased(ExecutionStep, name="dependency")
        dependency_keys = (
            select(func.jsonb_array_elements_text(ExecutionStep.depends_on))
            .correlate(ExecutionStep)
        )
        unfinished_dependency = (
            select(dependency.id)
            .where(
                dependency.plan_id == ExecutionStep.plan_id,
                dependency.step_key.in_(dependency_keys),
                dependency.step_status != ExecutionStepStatus.DONE.value,
            )
            .correlate(ExecutionStep)
            .exists()
        )

        stmt = (
            select(ExecutionStep, ExecutionPlan)
            .join(ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id)
            .where(
                ExecutionStep.step_status == ExecutionStepStatus.PENDING,
                ExecutionStep.entity_id == worker.entity_id,
                ExecutionStep.kind.in_(supported_kinds),
                ExecutionStep.risk_level.in_(allowed_risk_levels),
                ExecutionStep.attempt_count < ExecutionStep.max_attempts,
                ExecutionPlan.status.in_((ExecutionPlanStatus.DRAFT, ExecutionPlanStatus.RUNNING,)),
                # Keep inactive Workspace rows out of the bounded candidate
                # window.  Filtering only after LIMIT makes old paused/deleted
                # steps reappear on every heartbeat and can starve runnable
                # work behind them.
                or_(
                    ExecutionStep.workspace_id.is_(None),
                    exists().where(
                        and_(
                            Workspace.id == ExecutionStep.workspace_id,
                            Workspace.entity_id == ExecutionStep.entity_id,
                            or_(
                                Workspace.status == "active",
                                and_(
                                    Workspace.status == "paused",
                                    plan_manual_execution_predicate(),
                                ),
                            ),
                            Workspace.deleted_at.is_(None),
                        )
                    ),
                ),
                # No active lease for this step (the partial unique index
                # is the hard guarantee; this filter is the soft one
                # that lets the DB skip already-leased rows fast).
                ~exists().where(
                    and_(
                        WorkLease.step_id == ExecutionStep.id,
                        WorkLease.status == WorkLeaseStatus.ACTIVE,
                    )
                ),
                # Head-of-line blocking guard (see ``unfinished_dependency``).
                ~unfinished_dependency,
            )
            .order_by(ExecutionStep.created_at.asc())
            .limit(max_n * 4)  # room to skip un-mappable rows
        )
        dispatcher_target_worker_id = ExecutionPlan.dispatcher_state["target_worker_id"].astext
        plan_target_worker_id = ExecutionPlan.plan_dag["target_worker_id"].astext
        if (getattr(worker, "capabilities", None) or {}).get("targeted_only"):
            # A dedicated worker (a machine that is not the user's own, e.g.
            # a cloud desktop) takes only work addressed to it.
            stmt = stmt.where(
                or_(dispatcher_target_worker_id == worker.id, plan_target_worker_id == worker.id)
            )
        else:
            stmt = stmt.where(
                or_(dispatcher_target_worker_id.is_(None), dispatcher_target_worker_id == worker.id),
                or_(plan_target_worker_id.is_(None), plan_target_worker_id == worker.id),
            )
        if supported_providers is not None:
            provider_filters = [
                ExecutionStep.provider == "browser_setup",
                ExecutionStep.provider.in_(supported_providers),
            ]
            stmt = stmt.where(
                or_(
                    ExecutionStep.kind != "action",
                    *provider_filters,
                )
            )
        if plan_id is not None:
            stmt = stmt.where(ExecutionStep.plan_id == plan_id)

        # Discover a bounded set without locking Steps/Plans, then acquire
        # Workspace lifecycle boundaries first. Re-run admission under those
        # locks: a pause committed after discovery must not become a lease.
        candidate_scopes = (await db.execute(stmt.with_only_columns(
            ExecutionStep.id, ExecutionStep.workspace_id,
        ))).all()
        if not candidate_scopes:
            return []
        workspace_ids = {scope.workspace_id for scope in candidate_scopes if scope.workspace_id}
        locked_workspace_ids = set()
        if workspace_ids:
            locked_workspace_ids = set((await db.execute(
                select(Workspace.id)
                .where(Workspace.id.in_(workspace_ids), Workspace.entity_id == worker.entity_id)
                .order_by(Workspace.id.asc())
                .with_for_update(skip_locked=True)
            )).scalars().all())
        rows = (await db.execute(
            stmt.where(
                ExecutionStep.id.in_([scope.id for scope in candidate_scopes]),
                or_(
                    ExecutionStep.workspace_id.is_(None),
                    ExecutionStep.workspace_id.in_(locked_workspace_ids),
                ),
            ).with_for_update(skip_locked=True)
        )).all()
        if not rows:
            return []

        # Resolve subscription bindings once.
        bound_sub_ids = set((await db.execute(bound_subs)).scalars().all())

        leased: list[tuple[WorkLease, ExecutionStep]] = []
        for step, plan in rows:
            if len(leased) >= max_n:
                break
            now = datetime.now(timezone.utc)
            local_worker_plan = _plan_source(plan) == "local_worker"

            # Materialize the canonical Runtime capability as soon as a
            # candidate is inspected.  Some cloud-only provider/connection
            # filters intentionally skip a step before the governance gate;
            # leaving the derived id unset in that case makes persisted step
            # metadata disagree with the action contract and worker filters.
            step_capability_id = _step_runtime_capability_id(step)
            if step_capability_id and not getattr(step, "capability_id", None):
                step.capability_id = step_capability_id

            target_worker_id = _target_worker_id_for_plan(plan)
            if target_worker_id and target_worker_id != worker.id:
                continue

            from packages.core.dispatcher.retry_policy import (
                apply_retry_policy_to_step,
                resolve_retry_policy_for_step,
                step_retry_ready,
            )

            retry_policy = await resolve_retry_policy_for_step(db, step, plan=plan)
            apply_retry_policy_to_step(step, retry_policy)
            if not step_retry_ready(step, now=now):
                continue
            if step.attempt_count >= step.max_attempts:
                step.step_status = ExecutionStepStatus.FAILED.value
                step.error = {
                    "type": "RetryPolicyExhausted",
                    "message": "attempt_count reached max_attempts before checkout",
                    "retry_policy": {
                        "source": retry_policy.source,
                        "strategy": retry_policy.strategy,
                        "max_attempts": retry_policy.max_attempts,
                    },
                }
                step.finished_at = now
                continue

            # Capability check beyond what SQL can express.
            step_risk = _RISK_RANK.get(step.risk_level or "low", 0)
            if step_risk > max_risk:
                _stamp_dispatch_skip(
                    step,
                    DispatchSkipReason.RISK_EXCEEDS_WORKER,
                    (
                        f"step risk level {step.risk_level!r} exceeds worker "
                        f"ceiling {capabilities.get('max_risk_level') or 'low'!r}"
                    ),
                    structural=True,
                    now=now,
                )
                continue

            # ── Workspace-level budget gate ──
            # Re-checked per step because one checkout can cross many
            # workspaces. Cheap (single SELECT) and avoids leasing a
            # step we'd refuse to bill for anyway.
            from packages.core.budget import check_workspace_budget

            ws_ok, ws_reason = await check_workspace_budget(db, step.workspace_id)
            if not ws_ok:
                logger.info("dispatcher: skipping step %s — %s", step.step_key, ws_reason)
                _stamp_dispatch_skip(
                    step,
                    DispatchSkipReason.WORKSPACE_BUDGET,
                    ws_reason or "workspace monthly budget is exhausted",
                    structural=False,
                    now=now,
                )
                continue

            if supported_capabilities is not None and not local_worker_plan:
                if not step_capability_id or step_capability_id not in supported_capabilities:
                    _stamp_dispatch_skip(
                        step,
                        DispatchSkipReason.CAPABILITY_UNSUPPORTED,
                        (
                            f"worker does not support capability "
                            f"{step_capability_id or '(none declared)'!r}"
                        ),
                        structural=True,
                        now=now,
                    )
                    continue

            # ── Unified approval gate ──
            # One decision for BOTH the intrinsic step-approval check and the
            # governance policy: resolve_approval() answers allow / deny /
            # needs-human against the single HitlRequest store. A grant made
            # in any surface satisfies this gate (no more per-plane loops), a
            # re-trip reuses one card (dedup), and hard blocks (deny / risk
            # ceiling / budget cap) stay hard — they cannot be approved past.
            #
            # NOTE: this deliberately evaluates policy BEFORE the intrinsic
            # high-risk/requires_approval prompt, closing a prior hole where a
            # high-risk step could be offered an approvable HITL card even when
            # a never_allow / risk-ceiling / budget rule should hard-deny it.
            #
            granted_approval_request = None
            from packages.core.budget import (
                get_workspace_spent_credits_per_kind,
            )
            from packages.core.constants.approvals import (
                ApprovalOriginKind,
                HitlType,
            )
            from packages.core.governance.approvals import (
                ApprovalOrigin,
                resolve_approval,
            )
            from packages.core.governance.error_signatures import (
                classify_execution_error,
            )
            from packages.core.governance.service import post_hitl_card
            from packages.core.constants.runtime_principal import RuntimePrincipalKind
            from packages.core.services.runtime_authorization import authorize_runtime_action
            from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationAccess

            spent_credits_per_kind = (
                await get_workspace_spent_credits_per_kind(db, step.workspace_id)
                if step.workspace_id else None
            )

            # A step that already ran and failed is NOT a fresh "may I?" —
            # the operator said yes once and the work still didn't happen.
            # Re-asking the same authorize question is exactly the 15-approval
            # loop from the incident. When the real failure survived (Task 3),
            # mint an ERROR card carrying what broke and what to do about it.
            _hitl_type = HitlType.AUTHORIZE.value
            _payload: dict = {}
            _prior = step.last_execution_error
            failure_review_operation_id = None
            if isinstance(step.error, dict) and isinstance(_prior, dict) and _prior:
                failure_review_operation_id = await _failed_step_review_operation_id(db, step)
            if (step.attempt_count or failure_review_operation_id) and step.error and isinstance(_prior, dict) and _prior:
                _hint = classify_execution_error(_prior)
                _raw = str(_prior.get("message") or "").strip()
                _hitl_type = HitlType.ERROR.value
                _payload = {
                    "what_happened": (
                        _hint.what_happened if _hint
                        else (_raw or "The step failed.")
                    ),
                    # Required by HITL_REQUIRED_PAYLOAD_FIELDS and must be
                    # non-empty, so an error reported with no message still
                    # yields a card the record layer will accept.
                    "why": _raw or "The worker reported no failure detail.",
                    "action_to_take": (
                        _hint.action_to_take if _hint
                        else "Review the step output, then retry or cancel."
                    ),
                    "action_link": _hint.action_link if _hint else None,
                    "is_transient": bool(_hint and _hint.is_transient),
                }
            proposal_high_risk_approved = False
            if (
                (step.risk_level or "low") == "high"
                and plan.task_id
                and step.workspace_id
            ):
                from packages.core.proposals.external_authorization import (
                    proposal_youtube_public_step_is_authorized,
                )

                proposal_high_risk_approved = (
                    await proposal_youtube_public_step_is_authorized(
                        db,
                        entity_id=step.entity_id,
                        workspace_id=step.workspace_id,
                        task_id=plan.task_id,
                    )
                )
            approval_capability_id = step_capability_id
            if proposal_high_risk_approved and not approval_capability_id:
                # Planner-authored subagent steps may omit action metadata.
                # The exact Proposal scope still identifies this as the
                # external.social operation for governance policy evaluation.
                approval_capability_id = "external.social"
            permission_decision = await authorize_runtime_action(
                db,
                entity_id=step.entity_id,
                user_id=None,
                workspace_id=step.workspace_id,
                action_key=step.action_key,
                capability_id=approval_capability_id,
                access=RuntimeAuthorizationAccess.ACTION,
                principal_kind=RuntimePrincipalKind.SYSTEM_WORKER,
                task_id=plan.task_id,
            )
            from packages.core.dispatcher.step_state import step_approval_operation_id
            adhoc_owner_id = _adhoc_step_owner(plan, step)
            step_subject = _step_policy_subject(
                step, capability_id=approval_capability_id,
                requires_approval=bool(getattr(step, "requires_approval", False)),
                user_id=adhoc_owner_id,
            )
            step_origin = ApprovalOrigin(
                kind=ApprovalOriginKind.STEP.value,
                step_id=step.id,
                plan_id=step.plan_id,
                task_id=plan.task_id,
                operation_id=failure_review_operation_id or step_approval_operation_id(step),
                args_hash=hashlib.sha256(json.dumps(step.params or {}, sort_keys=True, default=str).encode()).hexdigest(),
                # An ad-hoc step's approval is its owner's to give (no Workspace chat).
                context={"requested_by": adhoc_owner_id} if adhoc_owner_id else {},
            )
            carried_grant = await _carried_tool_call_grant(
                db, plan=plan, step=step, subject=step_subject, owner_user_id=adhoc_owner_id,
            )
            in_person = _owner_acted_in_person(plan, adhoc_owner_id)
            decision = await resolve_approval(
                db,
                subject=step_subject,
                origin=step_origin,
                # A person already approved the tool call that dispatched this
                # step: ask nothing more. Hard blocks (deny, ceilings) still
                # apply, so only the decision is previewed, never minted.
                preview=carried_grant is not None or in_person,
                spent_credits=spent_credits_per_kind,
                permission_decision=permission_decision,
                intrinsic_high_risk_approved=proposal_high_risk_approved,
                policy_hitl_preauthorized=proposal_high_risk_approved,
                require_explicit_approval=failure_review_operation_id is not None,
                hitl_type=_hitl_type,
                payload=_payload,
            )
            if (carried_grant is not None or in_person) and decision.outcome == "needs_human":
                from packages.core.constants.approvals import ApprovalOutcome
                from packages.core.governance.approvals import ApprovalDecision

                decision = (
                    ApprovalDecision(
                        ApprovalOutcome.ALLOW,
                        f"Approved with its tool call ({carried_grant.id}).",
                        "approved_tool_call",
                    )
                    if carried_grant is not None
                    else ApprovalDecision(ApprovalOutcome.ALLOW, "The owner's own action.", "owner_in_person")
                )
            if decision.outcome == "needs_human":
                request_id = decision.request.id if decision.request else None
                # The card renders what the ROW says, not what this pass
                # recomputed. They agree on a fresh mint (the row was minted
                # from _hitl_type/_payload just above), and when an open
                # request is REUSED the row is the only truth — resolve_approval
                # ignores the incoming type in that case, so re-deriving here
                # would put a type on screen that the record never held.
                card_hitl_type = (
                    decision.request.hitl_type if decision.request else _hitl_type
                )
                card_payload = (
                    decision.request.payload if decision.request else _payload
                ) or {}
                # Preserve the two historical error types: an intrinsic
                # step-approval trigger ("step.*") vs a governance policy
                # pause. The blueprint report keys its paused/denied counts
                # off these, and the dispatcher tests assert them.
                approval_error_type = (
                    "StepApprovalRequired"
                    if (decision.matched_rule or "").startswith("step.")
                    else "GovernancePolicyHITL"
                )
                if step.workspace_id:
                    step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
                    step.human_input_prompt = decision.reason
                    step.current_lease_id = None
                    step.error = {
                        "type": approval_error_type,
                        "message": decision.reason,
                        "matched_rule": decision.matched_rule,
                        "capability_id": approval_capability_id,
                        "approval_request_id": request_id,
                    }
                    await post_hitl_card(
                        entity_id=step.entity_id,
                        workspace_id=step.workspace_id,
                        plan_id=step.plan_id,
                        step_id=step.id,
                        step_key=step.step_key,
                        kind=step.kind,
                        action_key=step_subject.action_key,
                        capability_id=approval_capability_id,
                        matched_rule=decision.matched_rule,
                        reason=decision.reason,
                        approval_request_id=request_id,
                        hitl_type=card_hitl_type,
                        payload=card_payload,
                        # What the card deep-links to. plan.task_id is the
                        # same value the request's origin carries, so card
                        # and record point at one place.
                        task_id=plan.task_id,
                        # Same transaction as the request + the step's
                        # waiting_human transition: card, request, and pause
                        # commit (or roll back) atomically — a crash cannot
                        # strand a card pointing at a never-committed request.
                        db=db,
                    )
                elif adhoc_owner_id and request_id:
                    # An ad-hoc step waits on a request its owner decides
                    # (/approvals/<id>), reached on every channel the owner
                    # chose for approvals, not only the bell.
                    from packages.core.services.notify import stage_routed_notification

                    step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
                    step.human_input_prompt = decision.reason
                    step.current_lease_id = None
                    step.error = {
                        "type": approval_error_type,
                        "message": decision.reason,
                        "matched_rule": decision.matched_rule,
                        "capability_id": approval_capability_id,
                        "approval_request_id": request_id,
                    }
                    await stage_routed_notification(
                        db, entity_id=step.entity_id, user_id=adhoc_owner_id, type="task_hitl_requested",
                        title="Approval needed",
                        body=decision.reason or f"Approve {step_subject.action_key}?",
                        link=f"/approvals/{request_id}",
                        meta={"approval_request_id": request_id, "action_key": step_subject.action_key},
                        idempotency_key=f"adhoc-step-approval:{request_id}",
                    )
                else:
                    # No workspace to render an approval card in and no owner
                    # to ask — fail closed rather than dispatch an unapproved step.
                    step.step_status = ExecutionStepStatus.FAILED.value
                    step.error = {
                        "type": "StepApprovalUnavailable",
                        "message": decision.reason,
                        "matched_rule": decision.matched_rule,
                        "capability_id": approval_capability_id,
                    }
                    step.finished_at = now
                logger.info(
                    "dispatcher: step %s needs approval (%s) — %s",
                    step.step_key, decision.matched_rule, decision.reason,
                )
                continue
            if decision.outcome == "deny":
                # A hard block is a rule only a person can change: name it,
                # hand back the soften patch, and mark the failure
                # human-required so the plan parks on a recovery card
                # instead of replanning into the same wall.
                from packages.core.governance.hard_block_provenance import (
                    governance_denial_step_error,
                )

                step.step_status = ExecutionStepStatus.FAILED.value
                step.error = await governance_denial_step_error(
                    db,
                    workspace_id=step.workspace_id,
                    action_key=step_subject.action_key,
                    capability_id=approval_capability_id,
                    matched_rule=decision.matched_rule,
                    reason=decision.reason,
                    category=step_subject.category,
                )
                step.finished_at = now
                logger.info(
                    "dispatcher: step %s blocked by policy (%s) — %s",
                    step.step_key, decision.matched_rule, decision.reason,
                )
                continue
            # allow → fall through to subscription gating + lease. If the
            # allow rests on a one-time operator grant, carry the request
            # so it is consumed only when the lease actually goes out.
            granted_approval_request = decision.request

            # Subscription gating — for kinds that need an owner.
            if step.kind in ("llm", "action", "subagent", "code"):
                ws_sub = await _resolve_subscription_for_step(db, step)
                if ws_sub is not None:
                    if ws_sub.id not in bound_sub_ids:
                        # Worker not bound to this subscription — skip.
                        _stamp_dispatch_skip(
                            step,
                            DispatchSkipReason.WORKER_NOT_BOUND,
                            (
                                f"the agent subscribed to service "
                                f"{step.service_key or ws_sub.service_key!r} is not "
                                f"bound to worker {worker.id}"
                            ),
                            structural=True,
                            now=now,
                        )
                        continue
                    step.resolved_subscription_id = ws_sub.id
                    step.resolved_agent_id = ws_sub.agent_id
                    if step.kind == "action":
                        allowed_action, action_error = await _agent_allows_action_step(
                            db,
                            agent_id=ws_sub.agent_id,
                            provider=step.provider,
                            action_key=step.action_key,
                        )
                        if not allowed_action:
                            step.step_status = ExecutionStepStatus.FAILED.value
                            step.error = action_error or {
                                "type": "ActionBindingDenied",
                                "message": "agent action binding denied",
                            }
                            step.finished_at = now
                            step.current_lease_id = None
                            continue
                elif step.service_key or step.workspace_id:
                    # Plan/workspace-routed steps must resolve to a
                    # subscription before they can be leased.
                    _stamp_dispatch_skip(
                        step,
                        DispatchSkipReason.SUBSCRIPTION_UNRESOLVED,
                        (
                            f"no active agent is subscribed to service "
                            f"{step.service_key!r} in this workspace"
                        ),
                        structural=True,
                        now=now,
                    )
                    continue

            # Dependency check — if any dep failed/cancelled, mark
            # the step skipped instead of leasing.
            dep_status = await _check_dependency_status(db, step)
            if dep_status == "blocked_by_failure":
                step.step_status = ExecutionStepStatus.SKIPPED.value
                step.finished_at = now
                continue
            if dep_status == "waiting":
                continue

            try:
                await _hydrate_runtime_action_binding_schemas(db, step)
            except RuntimeActionSchemaUnavailable as exc:
                step.step_status = ExecutionStepStatus.FAILED.value
                step.error = {
                    "type": "RuntimeActionSchemaUnavailable",
                    "message": str(exc),
                }
                step.finished_at = now
                continue

            # Optional input-schema validation, before the lease goes out.
            try:
                input_params = step.params or {}
                if step.kind == "action":
                    input_params = executable_action_params(
                        input_params,
                        human_input_response=step.human_input_response,
                    )
                validate_step_input(step, input_params)
            except SchemaError as exc:
                step.step_status = ExecutionStepStatus.FAILED.value
                step.error = {
                    "type": "InputSchemaError",
                    "message": str(exc),
                    "errors": exc.errors,
                }
                step.finished_at = now
                continue

            if granted_approval_request is not None:
                from packages.core.governance.approvals import consume_approval

                await consume_approval(db, granted_approval_request)
                step.error = None
                step.human_input_prompt = None

            # The step is being leased: whatever skip reason an earlier pass
            # stamped no longer applies. A real prior error stays for retry
            # feedback.
            _clear_dispatch_skip_on_grant(step)

            from packages.core.workers.protocol import (
                supports_lease_attempt_token,
            )

            lease = WorkLease(
                id=generate_ulid(),
                step_id=step.id,
                plan_id=step.plan_id,
                entity_id=step.entity_id,
                workspace_id=step.workspace_id,
                worker_id=worker.id,
                subscription_id=step.resolved_subscription_id,
                lease_until=now + _QUEUED_LEASE_GRACE + self._lease_ttl,
                status=WorkLeaseStatus.ACTIVE.value,
                execution_claim_id=(
                    generate_ulid()
                    if supports_lease_attempt_token(capabilities)
                    else None
                ),
            )
            db.add(lease)

            # Step state transition.
            step.step_status = ExecutionStepStatus.RUNNING.value
            step.current_lease_id = lease.id
            step.attempt_count = (step.attempt_count or 0) + 1
            if step.started_at is None:
                step.started_at = now

            # Explicit runtime budget for this attempt. Resolved at dispatch
            # time from the same step/plan/workspace layering the retry policy
            # uses; the worker re-resolves it onto its work item and enforces
            # it in-process (see packages/core/services/step_deadline.py).
            from packages.core.services.step_deadline import resolve_step_deadline

            step_deadline = await resolve_step_deadline(db, step, plan=plan)

            # Activity log for audit.
            db.add(
                WorkerActivityLog(
                    worker_id=worker.id,
                    event="lease_grant",
                    lease_id=lease.id,
                    payload_summary={
                        "step_key": step.step_key,
                        "kind": step.kind,
                        "capability_id": _step_runtime_capability_id(step),
                        "plan_id": step.plan_id,
                        "max_runtime_seconds": step_deadline.max_runtime_seconds,
                        "max_runtime_source": step_deadline.source,
                    },
                )
            )

            leased.append((lease, step))

        if leased:
            worker.last_heartbeat_at = datetime.now(timezone.utc)

        if _sp is not None:
            try:
                _sp.set_attribute("leases.granted", len(leased))
            except Exception:
                pass

        await db.flush()
        return leased

    # ── Completion ────────────────────────────────────────────────────

    async def complete_lease(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        result: Any = MISSING_RESULT,
        cost: Optional[dict] = None,
        evidence_refs: Optional[list[str | dict[str, Any]]] = None,
        metadata: Optional[dict] = None,
        task_output_value_kind: TaskOutputValueKind | str | None = None,
        runtime_checkpoint: Optional[dict] = None,
        expected_worker_id: str | None = None,
        expected_execution_claim_id: str | None = None,
        require_execution_claim_id_if_present: bool = False,
    ) -> WorkLease:
        """Worker reports success. Validates output against step schema,
        marks lease + step done, releases credential subleases. Caller commits.

        ``runtime_checkpoint`` is the worker's transcript for this attempt. It
        is only used when completion turns into a failure here (schema /
        envelope / fan-out / artifact sync): the retry then resumes from it
        instead of starting over.
        """
        from packages.core.observability import span

        async with span(
            "dispatcher.complete_lease",
            attributes={
                "lease.id": lease_id,
                "cost.usd": (cost or {}).get("usd"),
            },
        ):
            lease = await self._complete_lease_inner(
                db,
                lease_id,
                result=result,
                cost=cost,
                evidence_refs=evidence_refs,
                metadata=metadata,
                task_output_value_kind=task_output_value_kind,
                runtime_checkpoint=runtime_checkpoint,
                expected_worker_id=expected_worker_id,
                expected_execution_claim_id=expected_execution_claim_id,
                require_execution_claim_id_if_present=(
                    require_execution_claim_id_if_present
                ),
            )
            return lease

    async def _complete_lease_inner(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        result: Any,
        cost: Optional[dict],
        evidence_refs: Optional[list[str | dict[str, Any]]],
        metadata: Optional[dict],
        task_output_value_kind: TaskOutputValueKind | str | None = None,
        runtime_checkpoint: Optional[dict] = None,
        expected_worker_id: str | None = None,
        expected_execution_claim_id: str | None = None,
        require_execution_claim_id_if_present: bool = False,
    ) -> WorkLease:
        lease, step = await self._lock_active_execution(
            db,
            lease_id,
            expected_worker_id=expected_worker_id,
            expected_execution_claim_id=expected_execution_claim_id,
            require_execution_claim_id_if_present=(
                require_execution_claim_id_if_present
            ),
            lock_workspace=True,
        )
        now = datetime.now(timezone.utc)
        result = await _filter_lease_result(step, result)

        if not self._lease_owns_step(lease, step):
            if result is not MISSING_RESULT:
                lease.result = result
            if cost:
                lease.cost = cost
            if evidence_refs is not None:
                lease.evidence_refs = _merge_durable_evidence_refs(
                    lease.evidence_refs, evidence_refs
                )
            return await self._supersede_lease(
                db, lease, step, surface="complete_lease", now=now,
            )

        output_contract = output_contract_for_schema(step.expected_output_schema)
        # Execution evidence survives a later output-contract failure. Losing
        # a real receipt here would turn schema repair into another publish.
        runtime_checkpoint = _failure_checkpoint(
            SimpleNamespace(
                id=step.id, plan_id=step.plan_id, params=step.params,
                runtime_checkpoint=step.runtime_checkpoint,
                evidence_refs=_merge_durable_evidence_refs(step.evidence_refs, evidence_refs),
            ),
            runtime_checkpoint,
            persisted_evidence=copy.deepcopy(step.evidence_refs or []),
        )
        if evidence_refs is not None:
            lease.evidence_refs = _merge_durable_evidence_refs(
                lease.evidence_refs, evidence_refs
            )
            step.evidence_refs = _merge_durable_evidence_refs(
                step.evidence_refs, evidence_refs
            )
        omitted_result = result is MISSING_RESULT
        if omitted_result:
            # Keep legacy/no-schema completion behavior unchanged while making
            # hard-contract omission an explicit validation failure. A worker
            # that sent JSON ``null`` never enters this branch.
            result = None
            if output_contract.requires_submission_result_for(step.kind):
                omission_error = {
                    "type": "OutputSchemaError",
                    "message": (
                        f"step {step.step_key}: {output_contract.kind.value} "
                        "contract requires submit_result.result; field was omitted"
                    ),
                    "errors": [
                        {
                            "path": "$.result",
                            "message": "required result field was omitted",
                        }
                    ],
                }
                lease.result = None
                step.result = None
                return await self.fail_lease(
                    db,
                    lease_id,
                    error=omission_error,
                    runtime_checkpoint=runtime_checkpoint,
                    expected_worker_id=expected_worker_id,
                    expected_execution_claim_id=expected_execution_claim_id,
                    require_execution_claim_id_if_present=(
                        require_execution_claim_id_if_present
                    ),
                )
        # A hard contract must validate even when the worker returned Python
        # None: omitted ``result`` and explicit JSON null are distinct at the
        # submit boundary, and only the schema can decide whether null is
        # legal. The old ``result is not None`` guard marked omitted payloads
        # complete without checking required fields.
        # Preserve the pre-sentinel legacy behavior for an omitted result on
        # advisory/envelope rows: only an explicitly supplied JSON null should
        # enter schema validation there. Hard/structured omissions already
        # returned through the controlled gate above.
        if not omitted_result and (result is not None or output_contract.has_schema):
            try:
                result = coerce_step_output_for_schema(
                    step.expected_output_schema,
                    result,
                    step_kind=step.kind,
                    task_output_value_kind=task_output_value_kind,
                )
                validate_step_output(step, result)
            except TaskOutputProtocolError as exc:
                lease.result = result
                if cost:
                    lease.cost = cost
                return await self.fail_lease(
                    db,
                    lease_id,
                    error={
                        "type": "WorkerProtocolError",
                        "message": str(exc),
                        "errors": [
                            {
                                "path": "$.task_output_value_kind",
                                "message": str(exc),
                            }
                        ],
                    },
                    will_retry=False,
                    runtime_checkpoint=runtime_checkpoint,
                    expected_worker_id=expected_worker_id,
                    expected_execution_claim_id=expected_execution_claim_id,
                    require_execution_claim_id_if_present=(
                        require_execution_claim_id_if_present
                    ),
                )
            except SchemaError as exc:
                if output_schema_is_advisory(step.kind, step.expected_output_schema):
                    # Unmarked legacy free-form schema: accept the real output
                    # instead of dead-failing an already-running old plan.
                    # New plan step and task.expected_output contracts
                    # are provenance-marked and never enter this branch.
                    logger.warning(
                        "[dispatcher] step %s kind=%s output failed its (advisory) "
                        "schema; accepting real output anyway: %s",
                        step.step_key, step.kind, exc,
                    )
                else:
                    # Structured kinds: schema is a hard contract — fail.
                    lease.result = (
                        result
                        if output_contract.preserves_native_payload or isinstance(result, dict)
                        else {"value": result}
                    )
                    if cost:
                        lease.cost = cost
                    return await self.fail_lease(
                        db,
                        lease_id,
                        error={
                            "type": "OutputSchemaError",
                            "message": str(exc),
                            "errors": exc.errors,
                            **_validation_failure_debug(result),
                        },
                        runtime_checkpoint=runtime_checkpoint,
                        expected_worker_id=expected_worker_id,
                        expected_execution_claim_id=expected_execution_claim_id,
                        require_execution_claim_id_if_present=(
                            require_execution_claim_id_if_present
                        ),
                    )

        if (
            isinstance(step.expected_output_schema, dict)
            and step.expected_output_schema.get(
                IntegrationAccountFanoutResultFactory.SCHEMA_MARKER
            )
            is True
            and not IntegrationAccountFanoutResultFactory.output_is_complete(result)
        ):
            lease.result = result
            if cost:
                lease.cost = cost
            failed_lease = await self.fail_lease(
                db,
                lease_id,
                error={
                    "type": "IntegrationAccountFanoutIncomplete",
                    "message": (
                        "The all-account action did not return a complete, "
                        "internally consistent account result."
                    ),
                    **_validation_failure_debug(result),
                },
                runtime_checkpoint=runtime_checkpoint,
                expected_worker_id=expected_worker_id,
                expected_execution_claim_id=expected_execution_claim_id,
                require_execution_claim_id_if_present=(
                    require_execution_claim_id_if_present
                ),
            )
            step.result = result
            return failed_lease

        # ── StepResult envelope: status IS the control signal ──
        # For envelope-shaped steps (llm/subagent) the envelope's `status` is
        # the success/failure contract — `build_step_result_envelope` never
        # infers "succeeded", so a step that produced no content lands on
        # "failed" and must not be recorded as done. Without this the step
        # renders as Done holding an empty result, dependents run on that empty
        # input, and the retry budget is never touched. "partial" stays a
        # success: partial output is usable output. The gate is the SCHEMA, so
        # a custom/action schema carrying `status` as ordinary payload data
        # (e.g. a provider receipt) is unaffected.
        if output_contract.is_envelope and envelope_indicates_failure(result):
            lease.result = result
            if cost:
                lease.cost = cost
            error: dict[str, Any] = {
                "type": "StepResultFailed",
                "message": str(result.get("summary") or "step reported a failed StepResult"),
            }
            failure = result.get("failure")
            will_retry = None
            if isinstance(failure, dict) and failure:
                error["failure"] = failure
                if failure.get("retryable") is False or attempt_forbids_replay(failure):
                    will_retry = False
            logger.warning(
                "[dispatcher] step %s returned a failed StepResult envelope: %s",
                step.step_key, error["message"],
            )
            failed_lease = await self.fail_lease(
                db,
                lease_id,
                error=error,
                will_retry=will_retry,
                runtime_checkpoint=runtime_checkpoint,
                expected_worker_id=expected_worker_id,
                expected_execution_claim_id=expected_execution_claim_id,
                require_execution_claim_id_if_present=(
                    require_execution_claim_id_if_present
                ),
            )
            # Keep the envelope on the step too — the operator needs to see
            # what actually came back, not just the derived error.
            step.result = result
            return failed_lease

        # A filesystem path is storage provenance, not a deliverable handle.
        # Before announcing success, make every local artifact a Knowledge
        # Document and add its canonical viewer id to the step result. This is
        # the shared backstop for code/action/subagent producers that did not
        # use the standard generated-file helper themselves.
        from packages.core.contracts.artifact_delivery import (
            readonly_artifact_delivery,
            reused_artifact_evidence,
        )

        if readonly_artifact_delivery(step.params):
            from packages.core.models.task import Task
            from packages.core.services.artifact_delivery import (
                ArtifactDeliveryError,
                submitted_artifact_refs,
                verify_artifact_delivery_refs,
            )
            from packages.core.services.task_requester_identity import resolve_task_execution_user_id

            task = (await db.execute(select(Task).join(
                ExecutionPlan, ExecutionPlan.task_id == Task.id,
            ).where(ExecutionPlan.id == step.plan_id))).scalar_one_or_none()
            user_id = await resolve_task_execution_user_id(db, task) if task is not None else None
            try:
                delivery = await verify_artifact_delivery_refs(
                    db, entity_id=step.entity_id, workspace_id=step.workspace_id,
                    user_id=user_id, refs=submitted_artifact_refs(result, output_schema=step.expected_output_schema),
                )
            except ArtifactDeliveryError as exc:
                lease.result = result
                step.result = result
                return await self.fail_lease(
                    db, lease_id,
                    error={"type": "ArtifactDeliveryError", "code": exc.code, "message": str(exc), "details": exc.details},
                    runtime_checkpoint=runtime_checkpoint,
                    expected_worker_id=expected_worker_id,
                    expected_execution_claim_id=expected_execution_claim_id,
                    require_execution_claim_id_if_present=require_execution_claim_id_if_present,
                )
            if isinstance(result, dict):
                result = _attach_knowledge_artifacts_for_contract(
                    result, delivery.projection, output_contract=output_contract, step_kind=step.kind,
                )
            # This evidence is constructed under the live server lease fence.
            # Worker-supplied lookalikes are stripped by merge_untrusted; they
            # cannot mint a reused-file delivery or bypass its read boundary.
            persisted_result = result if output_contract.preserves_native_payload or isinstance(result, dict) else (
                {"value": result} if result is not None else None
            )
            verified_refs = reused_artifact_evidence(
                step, persisted_result, delivery.snapshots, lease_id=lease.id, completed_at=now,
            )
            step.evidence_refs = _merge_durable_evidence_refs(
                [*(step.evidence_refs or []), *verified_refs], [],
            )
            lease.evidence_refs = _merge_durable_evidence_refs(
                [*(lease.evidence_refs or []), *verified_refs], [],
            )

        # Lease side
        lease.status = WorkLeaseStatus.COMPLETED.value
        lease.execution_claim_id = None
        lease.execution_claimed_at = None
        lease.result = result
        if cost:
            lease.cost = cost
        if evidence_refs:
            lease.evidence_refs = _merge_durable_evidence_refs(
                lease.evidence_refs, evidence_refs
            )

        # Step side — mirror the result so PlanExecutor can pick it
        # up without joining work_leases on every cycle.
        step.step_status = ExecutionStepStatus.DONE.value
        step.runtime_checkpoint = None
        # Explicit PlanStep payload contracts are bare values: persist the
        # validated JSON value unchanged (including arrays, scalars, and
        # null). Schema-less and explicit StepResult-envelope rows retain the
        # historical ``value`` wrapper; any declared non-envelope schema keeps
        # its native JSON type even when its legacy provenance marker is absent.
        step.result = (
            result
            if output_contract.preserves_native_payload
            else (result if isinstance(result, dict) else ({"value": result} if result is not None else None))
        )
        step.finished_at = now
        if cost:
            step.cost = cost
        if evidence_refs:
            step.evidence_refs = _merge_durable_evidence_refs(
                step.evidence_refs, evidence_refs
            )
        step.error = None
        step.current_lease_id = None  # lease is terminal

        # Sublease cleanup is M3.5 (when integrated with the Vault).
        # For now, just zero out the index — real revoke happens in the
        # next iteration when CredentialService.release_sublease lands.
        lease.credential_leases = []

        activity_summary = {
            "step_key": step.step_key,
            "duration_s": _duration(step),
            "capability_id": _step_runtime_capability_id(step),
        }
        if metadata:
            activity_summary["metadata"] = metadata
        db.add(
            WorkerActivityLog(
                worker_id=lease.worker_id,
                event="lease_complete",
                lease_id=lease.id,
                payload_summary=activity_summary,
            )
        )
        worker = (await db.execute(select(Worker).where(Worker.id == lease.worker_id))).scalar_one_or_none()
        if worker is not None and (worker.consecutive_failures or 0) > 0:
            worker.consecutive_failures = 0
        await db.flush()

        # ── Cost rollup (M8) — runs in the same transaction so the
        # workspace.monthly_spent_usd lands atomically with the lease
        # state transition. The accumulator may emit a chat alert if
        # we just crossed 80% / 100% thresholds.
        from packages.core.budget import accumulate_step_cost

        await accumulate_step_cost(db, step, lease)

        locale = await _step_response_locale(db, step)
        await _safe_chat_step_done(step, cost=cost, locale=locale, db=db)
        dur = _duration(step)
        step_label = step.step_key.replace("_", " ").title()
        result_preview = ""
        if isinstance(step.result, dict) and step.result:
            text = step.result.get("text") or step.result.get("value") or ""
            if isinstance(text, str) and text.strip():
                result_preview = text.strip()[:300]
        elif isinstance(step.result, str) and step.result.strip():
            result_preview = step.result.strip()[:300]
        log_content = (
            t("plan.step.completed", locale=locale, step=step_label)
            + (t("plan.log.duration", locale=locale, seconds=f"{dur:.1f}") if dur else "")
            + (f"\n\n{result_preview}" if result_preview else "")
        )
        log_meta_extra = {"duration_s": dur, "cost": cost}
        if isinstance(metadata, dict):
            if metadata.get("runtime") is not None:
                log_meta_extra["runtime"] = metadata["runtime"]
                if metadata.get("runtime_event_summary") is not None:
                    log_meta_extra["runtime_event_summary"] = metadata["runtime_event_summary"]
            else:
                log_meta_extra["worker_metadata"] = metadata
        await _safe_task_log(
            step,
            TaskLogType.STEP_COMPLETED,
            log_content,
            metadata=_step_log_meta(step, lease, **log_meta_extra),
            db=db,
        )
        return lease

    async def fail_lease(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        error: dict,
        will_retry: Optional[bool] = None,
        evidence_refs: Optional[list[str | dict[str, Any]]] = None,
        runtime_checkpoint: Optional[dict] = None,
        expected_worker_id: str | None = None,
        expected_execution_claim_id: str | None = None,
        require_execution_claim_id_if_present: bool = False,
    ) -> WorkLease:
        """Worker reports failure. Step retries if attempts remain.

        ``will_retry`` overrides the auto-retry decision (used by the
        worker when it knows the failure is permanent — bad credentials,
        unsupported action, etc.).

        ``evidence_refs`` is what the attempt actually did before failing. A
        recorded ``tool_effect`` (an external action receipt) turns the
        failure human-required: the action must not be repeated blindly.

        ``runtime_checkpoint`` is the attempt's transcript. It is kept on the
        step when the step goes back to PENDING (retry), or as evidence when an
        unconfirmed effect fails it; an ordinary terminal failure clears it.
        """
        lease, step = await self._lock_active_execution(
            db,
            lease_id,
            expected_worker_id=expected_worker_id,
            expected_execution_claim_id=expected_execution_claim_id,
            require_execution_claim_id_if_present=(
                require_execution_claim_id_if_present
            ),
        )
        now = datetime.now(timezone.utc)

        if not self._lease_owns_step(lease, step):
            if evidence_refs is not None:
                lease.evidence_refs = _merge_durable_evidence_refs(
                    lease.evidence_refs, evidence_refs
                )
            return await self._supersede_lease(
                db, lease, step, surface="fail_lease", now=now,
                reported_error=error,
            )

        persisted_evidence = copy.deepcopy(step.evidence_refs or [])
        # Evidence lands BEFORE the captured-effect decision below reads it:
        # a receipt reported with the failure must block the retry exactly
        # like one reported earlier through complete_lease.
        if evidence_refs is not None:
            lease.evidence_refs = _merge_durable_evidence_refs(
                lease.evidence_refs, evidence_refs
            )
            step.evidence_refs = _merge_durable_evidence_refs(
                step.evidence_refs, evidence_refs
            )

        from packages.core.governance.error_signatures import classify_execution_error
        from packages.core.dispatcher.retry_policy import (
            advance_transient_retry,
            apply_retry_policy_to_step,
            apply_transient_backoff,
            error_with_retry_policy,
            resolve_retry_policy_for_step,
        )

        retry_policy = await resolve_retry_policy_for_step(db, step)
        apply_retry_policy_to_step(step, retry_policy)

        # The report is now fenced to this exact lease. Its usable transcript
        # must be visible to replay-safety checks, but must not erase a durable
        # pre-I/O frame merely because a generic failure transcript lost it.
        step.runtime_checkpoint = _failure_checkpoint(step, runtime_checkpoint, persisted_evidence=persisted_evidence)
        file_recovered, unresolved_files = await _recover_internal_file_effects(db, lease, step)
        runtime_checkpoint = step.runtime_checkpoint
        internal_recovery_failed = bool(unresolved_files)

        captured_effect = await _has_ambiguous_external_effect(
            db,
            _merge_durable_evidence_refs(
                step.evidence_refs,
                lease.evidence_refs,
            ),
            owner_step=step,
        )
        file_effects = [ref for ref in step.evidence_refs or [] if _is_system_file_effect(ref)]
        file_contract_failure = bool(file_effects) and error.get("type") in {
            "MissingOutputContract", "OutputSchemaError", "WorkerProtocolError", "InternalFileContinuationInterrupted",
        }
        if internal_recovery_failed:
            step.result = lease.result
            error = _internal_file_recovery_error(error, unresolved_files, runtime_checkpoint=step.runtime_checkpoint)
            will_retry = False
        elif captured_effect:
            step.result = lease.result
            error = {**error, "failure": _external_effect_unconfirmed_failure(step.step_key)}
            will_retry = False
        elif file_recovered or file_contract_failure:
            # Continue the existing model frame after its proven observation;
            # this neither creates a new Plan nor replenishes the attempt budget.
            will_retry = step.attempt_count < step.max_attempts
            if not will_retry:
                internal_recovery_failed = True
                error = _internal_file_recovery_error(
                    error, file_effects, runtime_checkpoint=step.runtime_checkpoint, output_repair_exhausted=True,
                )
        retry = will_retry if will_retry is not None else step.attempt_count < step.max_attempts
        stored_error, next_retry_at = error_with_retry_policy(
            error,
            policy=retry_policy,
            now=now,
            attempt_count=step.attempt_count,
        )

        # Transient infrastructure failures refund the attempt budget and back
        # off. A consumed approval remains terminal even if no effect was seen.
        failure_control = error.get("failure")
        has_submitted_retry_control = (
            error.get("type") == "StepResultFailed"
            and isinstance(failure_control, dict)
            and isinstance(failure_control.get("retryable"), bool)
        )
        # A model's summary is not proof that no provider/tool work ran.
        # Structured failure control keeps the normal attempt budget and must
        # not mint an attempt refund by matching transient-error prose.
        # Runtime/provider failures retain that budget-refund path.
        _hint = None if has_submitted_retry_control or captured_effect or file_effects else classify_execution_error(error)
        transient_streak = (
            advance_transient_retry(step.last_execution_error, now=now)
            if _hint is not None and _hint.is_transient
            else None
        )
        if transient_streak is not None and transient_streak.exhausted:
            # Budget spent. Record the final streak so the escalation is
            # legible, then fall through to the ordinary failure path —
            # which puts the step back in front of the gate, where it now
            # gets an actionable `error` card instead of another
            # "needs approval".
            stored_error["transient_retry"] = transient_streak.as_marker()
            transient_streak = None
        if transient_streak is not None:
            stored_error, next_retry_at = apply_transient_backoff(
                stored_error, streak=transient_streak, now=now,
            )
            # Overrides an explicit will_retry=False on purpose: "transient"
            # means the failure is expected to clear once the user fixes
            # something external, which is the opposite of terminal. Safe
            # only because the streak is bounded.
            retry = True
            # The attempt never executed — give the budget back.
            step.attempt_count = max(0, (step.attempt_count or 0) - 1)

        # The report ends this attempt. An ambiguous external effect fails the
        # Step without replay (see above); its checkpoint is retained below.
        lease.status = WorkLeaseStatus.FAILED.value
        lease.execution_claim_id = None
        lease.execution_claimed_at = None
        lease.error = stored_error
        lease.credential_leases = []

        # Keep the REAL failure where the approval gate cannot overwrite it.
        # `error` gets clobbered on every re-gate (it means "why is this
        # currently blocked"); this field means "why did it actually fail",
        # which is what the card must tell the user.
        step.last_execution_error = stored_error

        if retry:
            step.step_status = ExecutionStepStatus.PENDING.value  # back in the queue
            step.error = stored_error  # surface the most recent error
            step.current_lease_id = None
            if isinstance(runtime_checkpoint, dict):
                step.runtime_checkpoint = dict(runtime_checkpoint)
        else:
            # Running out of attempts is an execution outcome, not a user
            # question. Real input/approval has its own durable HITL path in
            # lease_needs_human.
            step.step_status = ExecutionStepStatus.FAILED.value
            step.error = stored_error
            step.finished_at = now
            step.current_lease_id = None
            step.human_input_prompt = None
            if captured_effect or internal_recovery_failed:
                if isinstance(runtime_checkpoint, dict):
                    step.runtime_checkpoint = dict(runtime_checkpoint)
            else:
                step.runtime_checkpoint = None

        db.add(
            WorkerActivityLog(
                worker_id=lease.worker_id,
                event="lease_fail",
                lease_id=lease.id,
                payload_summary={
                    "step_key": step.step_key,
                    "capability_id": _step_runtime_capability_id(step),
                    "will_retry": retry,
                    "error_type": stored_error.get("type"),
                    # Present only on a backed-off transient retry — this is
                    # the audit trail for the refunded attempt budget.
                    "transient_retry": (
                        transient_streak.as_marker() if transient_streak else None
                    ),
                },
            )
        )
        locale = await _step_response_locale(db, step)
        await _safe_chat_step_failed(
            step,
            error=stored_error,
            will_retry=retry,
            locale=locale,
            db=db,
        )
        err_type = stored_error.get("type", "unknown")
        err_msg = stored_error.get("message", "")[:200]
        await _safe_task_log(
            step,
            "step_failed" if not retry else "step_retrying",
            t("plan.step.failed", locale=locale, step=step.step_key, error=err_type)
            + (t("plan.step.retrying", locale=locale) if retry else "")
            + (f"\n{err_msg}" if err_msg else ""),
            metadata=_step_log_meta(
                step,
                lease,
                error=stored_error,
                will_retry=retry,
                next_retry_at=next_retry_at.isoformat() if next_retry_at else None,
                retry_policy={
                    "source": retry_policy.source,
                    "strategy": retry_policy.strategy,
                    "max_attempts": retry_policy.max_attempts,
                    "auto_human_on_exhausted": retry_policy.auto_human_on_exhausted,
                },
            ),
            db=db,
        )
        # Quarantine workers that fail repeatedly — keeps a buggy
        # external worker from holding the queue hostage.
        worker = (await db.execute(select(Worker).where(Worker.id == lease.worker_id))).scalar_one_or_none()
        if worker is not None and _counts_towards_worker_quarantine(stored_error):
            worker.consecutive_failures = (worker.consecutive_failures or 0) + 1
            if worker.kind != "internal" and worker.consecutive_failures >= 5:
                worker.status = WorkerStatus.QUARANTINED.value
                db.add(
                    WorkerActivityLog(
                        worker_id=worker.id,
                        event="quarantine",
                        payload_summary={"reason": "consecutive_failures >= 5"},
                    )
                )

        await db.flush()
        return lease

    async def extend_lease(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        extra_seconds: Optional[float] = None,
        progress: Optional[float] = None,
        expected_worker_id: str | None = None,
        expected_execution_claim_id: str | None = None,
        require_execution_claim_id_if_present: bool = False,
    ) -> WorkLease:
        """Worker is still alive — bump ``lease_until``.

        ``extra_seconds`` defaults to the dispatcher's default TTL.
        Increments ``heartbeat_count`` so we can audit chatty workers.
        Caller commits.
        """
        lease, step = await self._lock_active_execution(
            db,
            lease_id,
            expected_worker_id=expected_worker_id,
            expected_execution_claim_id=expected_execution_claim_id,
            require_execution_claim_id_if_present=(
                require_execution_claim_id_if_present
            ),
        )
        if not self._lease_owns_step(lease, step):
            return await self._supersede_lease(
                db, lease, step, surface="extend_lease",
                now=datetime.now(timezone.utc),
            )
        delta = timedelta(seconds=extra_seconds) if extra_seconds else self._lease_ttl
        lease.lease_until = datetime.now(timezone.utc) + delta
        lease.heartbeat_count = (lease.heartbeat_count or 0) + 1
        lease.last_heartbeat_at = datetime.now(timezone.utc)
        lease.extended_count = (lease.extended_count or 0) + 1
        if progress is not None:
            lease.progress = max(0.0, min(1.0, float(progress)))
        await db.flush()
        return lease

    async def lease_needs_human(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        prompt: str,
        pending_action: Optional[dict] = None,
        evidence_refs: Optional[list[str | dict[str, Any]]] = None,
        runtime_checkpoint: Optional[dict] = None,
        expected_worker_id: str | None = None,
        expected_execution_claim_id: str | None = None,
        require_execution_claim_id_if_present: bool = False,
    ) -> WorkLease:
        """Worker hit a CAPTCHA / approval / 2FA wall. Lease pauses;
        step → waiting_human; chat surfaces an interactive prompt.

        A pause reported after an external action whose outcome was not
        confirmed fails the Step instead (``ExternalEffectUnconfirmed``):
        nothing can settle that question for a worker job, and replaying the
        action could repeat it.

        ``pending_action`` is the optional generic structured payload
        (``{kind, options, …}``) defined in
        packages/core/ai/pending_action.py. When provided, the chat
        message renders the kind-specific button card (sign-in,
        question form, confirm) instead of a free-form text input.
        ``prompt`` is still used for log lines + step.human_input_prompt
        so existing UIs that read that field continue to work.
        """
        lease, step = await self._lock_active_execution(
            db,
            lease_id,
            expected_worker_id=expected_worker_id,
            expected_execution_claim_id=expected_execution_claim_id,
            require_execution_claim_id_if_present=(
                require_execution_claim_id_if_present
            ),
        )
        if not self._lease_owns_step(lease, step):
            return await self._supersede_lease(
                db, lease, step, surface="lease_needs_human",
                now=datetime.now(timezone.utc),
            )
        merged_step_evidence = _merge_durable_evidence_refs(
            step.evidence_refs,
            evidence_refs,
        )
        selected_checkpoint = _failure_checkpoint(
            SimpleNamespace(id=step.id, plan_id=step.plan_id, params=step.params, runtime_checkpoint=step.runtime_checkpoint, evidence_refs=merged_step_evidence),
            runtime_checkpoint,
            persisted_evidence=copy.deepcopy(step.evidence_refs or []),
        )
        rejected_checkpoint = isinstance(runtime_checkpoint, dict) and selected_checkpoint != runtime_checkpoint
        effect_owner = SimpleNamespace(
            id=step.id,
            step_key=step.step_key,
            plan_id=step.plan_id,
            entity_id=step.entity_id,
            workspace_id=step.workspace_id,
            params=step.params,
            step_status=step.step_status,
            evidence_refs=merged_step_evidence,
            runtime_checkpoint=None if rejected_checkpoint else selected_checkpoint,
        )
        runtime_checkpoint = selected_checkpoint
        # A file recovery gap is owned by the system. Do not replace it with
        # a card asking the customer to guess whether a filesystem write ran.
        # Proven earlier writes leave the real approval/input path below intact.
        markers = [ref for ref in merged_step_evidence if isinstance(ref, dict) and ref.get("kind") in (
            "tool_effect_reconciliation", "tool_result_handoff",
        )]
        for effect in merged_step_evidence:
            if _is_system_file_effect(effect) and await _has_ambiguous_external_effect(
                db, [effect, *markers], owner_step=effect_owner,
            ):
                return await self.fail_lease(
                    db, lease_id,
                    error={"type": "InternalFileContinuationInterrupted", "message": prompt},
                    evidence_refs=evidence_refs, runtime_checkpoint=runtime_checkpoint,
                    expected_worker_id=expected_worker_id,
                    expected_execution_claim_id=expected_execution_claim_id,
                    require_execution_claim_id_if_present=require_execution_claim_id_if_present,
                )
        # An external action crossed its boundary without a confirmed outcome.
        # Nothing can settle that question for a worker job, so a pause would
        # wait forever. Fail the Step without replay and say what to check.
        if await _has_ambiguous_external_effect(db, merged_step_evidence, owner_step=effect_owner):
            return await self.fail_lease(
                db, lease_id,
                error={
                    "type": EXTERNAL_EFFECT_UNCONFIRMED,
                    "message": (
                        f"Step '{step.step_key}' stopped after starting an external action whose "
                        "outcome was not confirmed. The action may or may not have happened; "
                        "check it with the provider before running it again."
                    ),
                    "worker_prompt": prompt,
                },
                will_retry=False,
                evidence_refs=evidence_refs, runtime_checkpoint=runtime_checkpoint,
                expected_worker_id=expected_worker_id,
                expected_execution_claim_id=expected_execution_claim_id,
                require_execution_claim_id_if_present=require_execution_claim_id_if_present,
            )
        plan_task_id = (await db.execute(
            select(ExecutionPlan.task_id).where(ExecutionPlan.id == step.plan_id)
        )).scalar_one_or_none()

        # ── Mint FIRST, mutate after ───────────────────────────────────────
        # Order is load-bearing, not stylistic. ``mint_approval_request`` can
        # lose a race on the dedup index, and its recovery path calls
        # db.rollback() — which would discard anything already written in this
        # transaction. Writing lease.status / step.step_status / the activity
        # log before the mint would let that rollback silently undo the pause
        # while this method goes on to post a card announcing it, leaving an
        # ACTIVE lease behind a card that says it is paused. Path B (the step
        # gate) is mint-first for the same reason; keep this one aligned.
        #
        # The race is not reachable from the test fixtures: the partial unique
        # index that raises IntegrityError lives only in the migration, not in
        # the model's __table_args__ (deliberately — see
        # models/hitl_request.py), so create_all() builds a schema that
        # cannot produce it. This ordering is the whole defense; do not reorder
        # on the grounds that no test fails.
        #
        # No policy evaluation here: the human is being asked for information,
        # not permission — that decision belongs to resolve_approval.
        pending_kind = str((pending_action or {}).get("kind") or KIND_HUMAN_INPUT.value)
        approval_context: dict[str, Any] = {"pending_kind": pending_kind}
        # Bound on every path: the notifier call below reads it outside the
        # branches that assign it.
        approval_request = None
        # Only mint what the chat endpoint can close. A kind outside this set
        # (e.g. workspace_operation_review, which internal.py can raise through
        # this same path) has no resolve branch that decides its request, so a
        # row minted for it would sit PENDING forever: its step stays
        # waiting_human → its plan never finalizes → resolve_origin_requests,
        # whose only production caller is that terminal path, never runs.
        # Phase 2 brings these kinds in by giving their branches a verdict.
        if step.workspace_id and pending_kind in LEASE_HITL_CLOSEABLE_KINDS:
            from packages.core.constants.approvals import (
                LEASE_KIND_HITL_TYPES,
                ApprovalOriginKind,
                HitlType,
            )
            from packages.core.governance.approvals import (
                ApprovalOrigin,
                ApprovalSubject,
                mint_approval_request,
            )

            approval_request = await mint_approval_request(
                db,
                subject=ApprovalSubject(
                    entity_id=step.entity_id,
                    workspace_id=step.workspace_id,
                    action_key=step.action_key,
                    capability_id=_step_runtime_capability_id(step),
                    risk_level=step.risk_level or "medium",
                    kind=step.kind,
                ),
                origin=ApprovalOrigin(
                    kind=ApprovalOriginKind.LEASE.value,
                    lease_id=lease.id,
                    step_id=step.id,
                    plan_id=step.plan_id,
                    task_id=plan_task_id,
                    context=approval_context,
                ),
                reason=prompt,
                matched_rule=f"lease.{pending_kind}",
                # Say what this pause actually is. The record layer's default
                # is "authorize", which for a CAPTCHA wall or a missing field
                # is simply false — and false in the direction that made every
                # read surface count it as governance friction. The comment
                # above ("the human is being asked for information, not
                # permission") is now written into the row.
                hitl_type=LEASE_KIND_HITL_TYPES.get(
                    pending_kind, HitlType.AUTHORIZE.value,
                ),
            )

        # Apply worker evidence only after minting: mint can recover a dedup
        # race with rollback, so writes made before it are not durable.
        step.evidence_refs = _merge_durable_evidence_refs(
            step.evidence_refs,
            evidence_refs,
        )
        lease.evidence_refs = _merge_durable_evidence_refs(
            lease.evidence_refs,
            evidence_refs,
        )
        lease.status = WorkLeaseStatus.NEEDS_HUMAN.value
        # A worker-originated need-human report is a terminal reporting
        # boundary, so its valid token can be released atomically.
        lease.execution_claim_id = None
        lease.execution_claimed_at = None
        lease.credential_leases = []  # release while waiting

        step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
        step.human_input_prompt = prompt
        step.current_lease_id = None
        # A request_input pause keeps the subagent transcript so the answer
        # resumes the loop; any other pause starts the step over on resume.
        step.runtime_checkpoint = (
            dict(runtime_checkpoint) if isinstance(runtime_checkpoint, dict) else None
        )

        db.add(
            WorkerActivityLog(
                worker_id=lease.worker_id,
                event="lease_needs_human",
                lease_id=lease.id,
                payload_summary={
                    "step_key": step.step_key,
                    "capability_id": _step_runtime_capability_id(step),
                    "pending_action_kind": (pending_action or {}).get("kind"),
                },
            )
        )
        await db.flush()

        # The notifier is the single writer of ``approval_request_id`` onto the
        # card — including the fallback card it builds itself when the caller
        # passed no pending_action (apps/api/routers/workers.py never does).
        # Injecting it into ``pending_action`` here would miss that branch.
        locale = await _step_response_locale(db, step)
        await _safe_chat_step_needs_human(
            step,
            locale=locale,
            prompt=prompt,
            pending_action=pending_action,
            approval_request_id=approval_request.id if approval_request else None,
            db=db,
        )
        await _safe_task_log(
            step,
            "step_needs_human",
            t("plan.step.waiting", locale=locale, step=step.step_key, prompt=prompt[:200]),
            metadata=_step_log_meta(
                step,
                lease,
                prompt=prompt,
                hitl={
                    "kind": "human_input",
                    "fields": [{"name": "response", "type": "textarea", "required": True}],
                },
            ),
            db=db,
        )
        await _safe_task_event(
            step,
            "task.hitl_requested",
            {
                "plan_id": step.plan_id,
                "step_ids": [step.id],
                "lease_id": lease.id,
                "worker_id": lease.worker_id,
                "prompt": prompt,
                "task_status": "in_progress",
            },
            db=db,
        )
        return lease

    # ── Periodic sweep ───────────────────────────────────────────────

    async def expire_leases(
        self,
        db: AsyncSession,
        *,
        now: Optional[datetime] = None,
        plan_ids: Optional[set[str]] = None,
    ) -> int:
        """Reclaim leases past lease_until. Caller commits.

        Requeue within the attempt budget only when replay is proven safe.
        Recover exact internal file observations or retain a system failure;
        an unconfirmed provider effect fails the Step without replay
        (``ExternalEffectUnconfirmed``). Expiry alone never proves that an
        action can repeat. Returns count expired.
        """
        now = now or datetime.now(timezone.utc)
        candidate_ids = list(
            (
                await db.execute(
                    select(WorkLease.id)
                    .where(
                        WorkLease.status == WorkLeaseStatus.ACTIVE,
                        WorkLease.lease_until < now,
                    )
                    .order_by(WorkLease.id.asc())
                )
            )
            .scalars()
            .all()
        )

        expired_count = 0
        expired_plan_ids: set[str] = set()
        for lease_id in candidate_ids:
            try:
                lease, step = await self._lock_active_execution(db, lease_id)
            except (DispatchError, LeaseNotActive):
                continue
            lease_until = lease.lease_until
            if lease_until is None or lease_until >= now:
                continue
            expired_count += 1
            lease.credential_leases = []
            if plan_ids is not None and lease.plan_id:
                plan_ids.add(lease.plan_id)
            if lease.plan_id:
                expired_plan_ids.add(lease.plan_id)

            from packages.core.dispatcher.retry_policy import (
                apply_retry_policy_to_step,
                error_with_retry_policy,
                resolve_retry_policy_for_step,
            )

            retry_policy = await resolve_retry_policy_for_step(db, step)
            apply_retry_policy_to_step(step, retry_policy)
            if not self._lease_owns_step(lease, step):
                await self._supersede_lease(db, lease, step, surface="expire_leases", now=now)
                continue
            _, unresolved_files = await _recover_internal_file_effects(db, lease, step)
            internal_recovery_failed = bool(unresolved_files)
            expired_file_effects = [
                ref for ref in _merge_durable_evidence_refs(step.evidence_refs, lease.evidence_refs)
                if _is_system_file_effect(ref)
            ]
            if expired_file_effects and step.attempt_count >= step.max_attempts:
                internal_recovery_failed = True
            step_evidence = list(step.evidence_refs or [])
            lease_evidence = list(lease.evidence_refs or [])
            captured_effect = await _has_ambiguous_external_effect(
                db,
                (*step_evidence, *lease_evidence),
                owner_step=step,
            )
            worker_kind = await db.scalar(
                select(Worker.kind).where(Worker.id == lease.worker_id)
            )
            plan_execution_mode = await db.scalar(
                select(ExecutionPlan.execution_mode).where(
                    ExecutionPlan.id == lease.plan_id
                )
            )
            unreported_external_attempt = bool(
                not captured_effect
                and lease.execution_claim_id
                and worker_kind != "internal"
                and step.kind == "action"
                and plan_execution_mode == "live"
            )
            if unreported_external_attempt:
                # The committed tokenized lease is the server's pre-handler
                # intent. Expiry fences DB settlement, but it cannot prove the
                # external process stopped provider I/O. Persist that exact
                # ambiguity as evidence on the failed Step.
                unknown_effect = _expired_external_action_attempt_evidence(
                    lease,
                    step,
                )
                step_evidence = _merge_durable_evidence_refs(
                    step_evidence,
                    [unknown_effect],
                )
                lease_evidence = _merge_durable_evidence_refs(
                    lease_evidence,
                    [unknown_effect],
                )
                captured_effect = True
            if captured_effect:
                # A per-round checkpoint can persist the receipt on the Step,
                # while a worker report can persist it on the Lease.  Keep
                # both sources and project their union onto both rows so the
                # failed attempt's record names every unconfirmed effect.
                for ref in lease_evidence:
                    if ref not in step_evidence:
                        step_evidence.append(ref)
                step.evidence_refs = step_evidence
                lease.evidence_refs = _merge_durable_evidence_refs(
                    lease_evidence,
                    step_evidence,
                )
            expired_error, next_retry_at = error_with_retry_policy(
                {
                    "type": EXTERNAL_EFFECT_UNCONFIRMED,
                    "message": (
                        f"The worker lease for step '{step.step_key}' expired after an external "
                        "action started, so its outcome is unknown. The action may or may not "
                        "have happened; check it with the provider before running it again."
                    ),
                    "cause": {"type": "lease_expired", "message": "lease expired before completion"},
                    "failure": _external_effect_unconfirmed_failure(step.step_key),
                }
                if captured_effect
                else {"type": "lease_expired", "message": "lease expired before completion"},
                policy=retry_policy,
                now=now,
                attempt_count=step.attempt_count,
            )
            if internal_recovery_failed:
                expired_error = _internal_file_recovery_error(
                    expired_error, unresolved_files or expired_file_effects,
                    runtime_checkpoint=step.runtime_checkpoint,
                    output_repair_exhausted=not unresolved_files and not captured_effect,
                )
            if captured_effect:
                expired_error.pop("next_retry_at", None)
                next_retry_at = None
                lease.error = expired_error

            # Same reasoning as fail_lease: a lease that expired without a
            # report IS a real execution failure (the worker died / never came
            # back), and the step goes straight back in front of the approval
            # gate — which would otherwise be the last thing the user hears.
            step.last_execution_error = expired_error

            if internal_recovery_failed:
                lease.status = WorkLeaseStatus.EXPIRED.value
                step.step_status = ExecutionStepStatus.FAILED.value
                step.error = expired_error
                step.finished_at = now
                step.current_lease_id = None
                # A worker may still finish an in-flight filesystem boundary.
                # Its expired lease cannot settle the Step or repeat the call;
                # preserve the claim as evidence until that worker exits.
            elif captured_effect:
                # Nothing can settle an unconfirmed external effect for a
                # worker job, so the Step fails without replay. The checkpoint
                # and claim stay as evidence; the expired lease cannot settle
                # the Step even if a live holder later reports.
                lease.status = WorkLeaseStatus.EXPIRED.value
                step.step_status = ExecutionStepStatus.FAILED.value
                step.error = expired_error
                step.finished_at = now
                step.current_lease_id = None
                step.human_input_prompt = None
            elif step.attempt_count < step.max_attempts:
                lease.status = WorkLeaseStatus.EXPIRED.value
                step.step_status = ExecutionStepStatus.PENDING.value
                step.error = expired_error
                step.current_lease_id = None
            else:
                # A worker timeout with no ambiguous effect is not a user
                # request, regardless of the old retry-policy flag.
                lease.status = WorkLeaseStatus.EXPIRED.value
                step.step_status = ExecutionStepStatus.FAILED.value
                step.error = {"type": "lease_expired", "message": "all attempts expired", **expired_error}
                step.finished_at = now
                step.current_lease_id = None
                step.human_input_prompt = None

            db.add(
                WorkerActivityLog(
                    worker_id=lease.worker_id,
                    event="lease_expire",
                    lease_id=lease.id,
                    payload_summary={
                        "step_key": step.step_key,
                        "attempts_remaining": step.max_attempts - step.attempt_count,
                        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                        "captured_effect": captured_effect,
                        "unreported_external_attempt": unreported_external_attempt,
                    },
                )
            )

        await db.flush()
        return expired_count

    # ── Revocation ───────────────────────────────────────────────────

    async def revoke_active_leases(
        self,
        db: AsyncSession,
        *,
        plan_id: Optional[str] = None,
        step_ids: Optional[list[str]] = None,
        reason: str,
    ) -> list[WorkLease]:
        """Cancel every ACTIVE / NEEDS_HUMAN lease in scope. Caller commits.

        Called by ``cancel_plan`` / ``cancel_step`` before the steps are
        flipped, and by the plan deadline. The worker's heartbeat stops
        extending a lease that is no longer active and cancels the body; a
        late report on the revoked lease is refused by ``_get_active_lease``.
        Steps are deliberately not touched here — the caller owns that.
        """
        if plan_id is None and not step_ids:
            return []
        conditions = [
            WorkLease.status.in_(
                (WorkLeaseStatus.ACTIVE.value, WorkLeaseStatus.NEEDS_HUMAN.value),
            ),
        ]
        if plan_id is not None:
            conditions.append(WorkLease.plan_id == plan_id)
        if step_ids:
            conditions.append(WorkLease.step_id.in_([str(item) for item in step_ids]))
        leases = list(
            (
                await db.execute(
                    select(WorkLease)
                    .where(*conditions)
                    .order_by(WorkLease.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for lease in leases:
            lease.status = WorkLeaseStatus.CANCELLED.value
            lease.error = {"type": LEASE_REVOKED_ERROR_TYPE, "message": reason}
            lease.credential_leases = []
            db.add(
                WorkerActivityLog(
                    worker_id=lease.worker_id,
                    event="lease_revoke",
                    lease_id=lease.id,
                    payload_summary={
                        "step_id": lease.step_id,
                        "plan_id": lease.plan_id,
                        "reason": reason,
                    },
                )
            )
        if leases:
            logger.info(
                "dispatcher: revoked %d lease(s) (plan=%s steps=%s): %s",
                len(leases), plan_id, step_ids, reason,
            )
        await db.flush()
        return leases

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _lease_owns_step(lease: WorkLease, step: ExecutionStep) -> bool:
        """Whether ``lease`` is still the attempt the step is running.

        A step reset by a retry surface (PENDING, ``current_lease_id`` None),
        cancelled, or re-leased to a newer attempt no longer belongs to this
        lease; a report through it must not overwrite the step.
        """
        return (
            step.current_lease_id == lease.id
            and step.step_status == ExecutionStepStatus.RUNNING.value
        )

    async def _supersede_lease(
        self,
        db: AsyncSession,
        lease: WorkLease,
        step: ExecutionStep,
        *,
        surface: str,
        now: datetime,
        reported_error: Optional[dict] = None,
    ) -> WorkLease:
        """Close a stale lease for audit and leave the step alone."""
        message = (
            f"lease {lease.id} no longer owns step {step.step_key} "
            f"(step is {step.step_status!r}, current lease "
            f"{step.current_lease_id!r}); {surface} ignored"
        )
        if lease.status == WorkLeaseStatus.ACTIVE.value:
            lease.status = WorkLeaseStatus.CANCELLED.value
        lease.execution_claim_id = None
        lease.execution_claimed_at = None
        superseded: dict[str, Any] = {
            "type": LEASE_SUPERSEDED_ERROR_TYPE,
            "message": message,
            "surface": surface,
        }
        if reported_error is not None:
            superseded["reported_error"] = reported_error
        lease.error = superseded
        lease.credential_leases = []
        db.add(
            WorkerActivityLog(
                worker_id=lease.worker_id,
                event="lease_superseded",
                lease_id=lease.id,
                payload_summary={
                    "step_key": step.step_key,
                    "step_status": step.step_status,
                    "current_lease_id": step.current_lease_id,
                    "surface": surface,
                    "reported_error_type": (
                        reported_error.get("type")
                        if isinstance(reported_error, dict)
                        else None
                    ),
                },
            )
        )
        logger.info("dispatcher: %s", message)
        await db.flush()
        return lease

    async def _get_active_lease(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        for_update: bool = False,
    ) -> WorkLease:
        stmt = select(WorkLease).where(WorkLease.id == lease_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        lease = (await db.execute(stmt)).scalar_one_or_none()
        if lease is None:
            raise DispatchError(f"lease {lease_id} not found")
        if lease.status != WorkLeaseStatus.ACTIVE:
            raise LeaseNotActive(f"lease {lease_id} is {lease.status!r}, not active")
        return lease

    async def _get_step(
        self,
        db: AsyncSession,
        step_id: str,
        *,
        for_update: bool = False,
    ) -> ExecutionStep:
        stmt = select(ExecutionStep).where(ExecutionStep.id == step_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        step = (await db.execute(stmt)).scalar_one_or_none()
        if step is None:
            raise DispatchError(f"step {step_id} not found")
        return step

    async def _lock_active_execution(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        expected_worker_id: str | None = None,
        expected_execution_claim_id: str | None = None,
        require_execution_claim_id_if_present: bool = False,
        lock_workspace: bool = False,
    ) -> tuple[WorkLease, ExecutionStep]:
        """Lock and fence one terminal worker report.

        Reports use Task -> Plan -> Lease -> Step. Completion additionally
        requests the Workspace lock first because its cost roll-up later locks
        Workspace then Worker; cancel/retry and monthly reset use that same
        Workspace-first prefix. Non-cost report paths deliberately do not lock
        Workspace: the expiry sweeper may process several workspaces in one
        transaction and must not accumulate unordered lifecycle locks.
        """

        lease_snapshot = (await db.execute(
            select(WorkLease).where(WorkLease.id == lease_id)
        )).scalar_one_or_none()
        if lease_snapshot is None:
            raise DispatchError(f"lease {lease_id} not found")
        plan_snapshot = (await db.execute(
            select(ExecutionPlan).where(
                ExecutionPlan.id == lease_snapshot.plan_id
            )
        )).scalar_one_or_none()
        if plan_snapshot is None:
            raise DispatchError(f"plan {lease_snapshot.plan_id} not found")
        if lock_workspace and plan_snapshot.workspace_id:
            from packages.core.models.workspace import Workspace

            locked_workspace = (await db.execute(
                select(Workspace)
                .where(
                    Workspace.id == plan_snapshot.workspace_id,
                    Workspace.entity_id == plan_snapshot.entity_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if locked_workspace is None:
                raise DispatchError(
                    f"workspace {plan_snapshot.workspace_id} not found"
                )
        if plan_snapshot.task_id:
            from packages.core.models.task import Task

            locked_task = (await db.execute(
                select(Task)
                .where(Task.id == plan_snapshot.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if locked_task is not None and (
                locked_task.entity_id != plan_snapshot.entity_id
                or locked_task.workspace_id != plan_snapshot.workspace_id
            ):
                raise DispatchError(
                    f"task {plan_snapshot.task_id} crosses the lease scope"
                )
        locked_plan = (await db.execute(
            select(ExecutionPlan)
            .where(ExecutionPlan.id == plan_snapshot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if locked_plan is None:
            raise DispatchError(f"plan {plan_snapshot.id} not found")
        if (
            locked_plan.entity_id != plan_snapshot.entity_id
            or locked_plan.workspace_id != plan_snapshot.workspace_id
            or locked_plan.task_id != plan_snapshot.task_id
        ):
            raise LeaseNotActive(
                f"lease {lease_id} plan ownership changed while locking"
            )
        lease = await self._get_active_lease(db, lease_id, for_update=True)
        if (
            lease.plan_id != locked_plan.id
            or lease.step_id != lease_snapshot.step_id
            or lease.entity_id != locked_plan.entity_id
            or lease.workspace_id != locked_plan.workspace_id
        ):
            raise LeaseNotActive(f"lease {lease_id} ownership changed")
        expected_owner = str(expected_worker_id or "").strip()
        if expected_owner and lease.worker_id != expected_owner:
            raise LeaseNotActive(
                f"lease {lease_id} is not owned by worker {expected_owner}"
            )
        expected_claim = str(expected_execution_claim_id or "").strip()
        actual_claim = str(lease.execution_claim_id or "").strip()
        if require_execution_claim_id_if_present and actual_claim and not expected_claim:
            raise LeaseNotActive(
                f"lease {lease_id} requires its current attempt token"
            )
        if expected_claim:
            now = datetime.now(timezone.utc)
            if (
                not actual_claim
                or actual_claim != expected_claim
                or lease.lease_until is None
                or lease.lease_until <= now
            ):
                raise LeaseNotActive(
                    f"lease {lease_id} execution claim is stale or no longer active"
                )
        step = await self._get_step(db, lease.step_id, for_update=True)
        if (
            step.plan_id != locked_plan.id
            or step.entity_id != locked_plan.entity_id
            or step.workspace_id != locked_plan.workspace_id
        ):
            raise LeaseNotActive(f"lease {lease_id} step ownership changed")
        return lease, step


# ── Module helpers ────────────────────────────────────────────────────


async def _step_response_locale(db: AsyncSession, step: ExecutionStep) -> str:
    plan = await db.get(ExecutionPlan, step.plan_id)
    return await resolve_plan_response_locale(db, plan) if plan is not None else "en"


async def _resolve_subscription_for_step(
    db: AsyncSession,
    step: ExecutionStep,
) -> Optional[Any]:
    """Find the active subscription for a routed step.

    The caller caches both ``resolved_subscription_id`` and
    ``resolved_agent_id`` so downstream worker context uses the workspace-bound
    agent persona, not a default entity context.
    """
    from packages.core.models.workspace import AgentSubscription

    if step.resolved_subscription_id:
        # A materialized pin is an immutable execution identity, not a cache
        # hint.  If any scoped field changed or the subscription was disabled,
        # fail closed; silently falling back to another row can run the step as
        # a different agent under load or during a Blueprint update.
        return (
            await db.execute(
                select(AgentSubscription)
                .where(
                    AgentSubscription.id == step.resolved_subscription_id,
                    AgentSubscription.status == "active",
                    AgentSubscription.entity_id == step.entity_id,
                    AgentSubscription.workspace_id == step.workspace_id,
                    AgentSubscription.service_key == step.service_key,
                    AgentSubscription.agent_id == step.resolved_agent_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    if not step.service_key or not step.workspace_id:
        return None

    sub = (
        await db.execute(
            select(AgentSubscription)
            .where(
                AgentSubscription.entity_id == step.entity_id,
                AgentSubscription.workspace_id == step.workspace_id,
                AgentSubscription.service_key == step.service_key,
                AgentSubscription.status == "active",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return sub


async def _check_dependency_status(
    db: AsyncSession,
    step: ExecutionStep,
) -> str:
    """Inspect ``step.depends_on`` against the current sibling steps.

    Returns:
      'ready'              all deps done — step can be leased
      'waiting'            some dep still pending/running — skip this cycle
      'blocked_by_failure' a dep failed/cancelled/skipped — caller marks
                            this step skipped
    """
    deps = step.depends_on or []
    if not deps:
        return "ready"

    rows = list(
        (
            await db.execute(
                select(ExecutionStep.step_key, ExecutionStep.step_status).where(
                    ExecutionStep.plan_id == step.plan_id,
                    ExecutionStep.step_key.in_(deps),
                )
            )
        ).all()
    )
    by_key = {k: s for k, s in rows}

    for dep in deps:
        s = by_key.get(dep)
        if s is None:
            # Plan invariant violated — depends_on references a step
            # that doesn't exist. Treat as blocked so the cohort can
            # still terminate (won't loop forever waiting).
            return "blocked_by_failure"
        if s in {"failed", "cancelled", "skipped"}:
            return "blocked_by_failure"
        if s != "done":
            return "waiting"
    return "ready"


def _duration(step: ExecutionStep) -> Optional[float]:
    if step.started_at and step.finished_at:
        return (step.finished_at - step.started_at).total_seconds()
    return None


def _iso_or_none(value) -> Optional[str]:
    return value.isoformat() if value else None


def _step_log_meta(
    step: ExecutionStep,
    lease: WorkLease | None = None,
    *,
    error: dict | None = None,
    **extra,
) -> dict:
    """Standard observability envelope for task logs emitted from steps."""
    err = error or step.error or {}
    meta = {
        "plan_id": step.plan_id,
        "step_id": step.id,
        "step_key": step.step_key,
        "kind": step.kind,
        "service_key": getattr(step, "service_key", None),
        "agent_id": getattr(step, "resolved_agent_id", None),
        "agent_subscription_id": getattr(step, "resolved_subscription_id", None),
        "provider": getattr(step, "provider", None),
        "action_key": getattr(step, "action_key", None),
        "capability_id": _step_runtime_capability_id(step),
        "lease_id": lease.id if lease else step.current_lease_id,
        "worker_id": lease.worker_id if lease else None,
        "attempt_count": step.attempt_count,
        "retry_count": step.attempt_count,
        "max_attempts": step.max_attempts,
        "lease_until": _iso_or_none(lease.lease_until if lease else None),
        "error_type": err.get("type") if isinstance(err, dict) else None,
        "error_message": err.get("message") if isinstance(err, dict) else None,
        # Runtime-budget diagnostics, promoted next to the retry diagnostics so
        # a StepDeadlineExceeded is readable on the Task detail page without
        # opening the raw error blob.
        "max_runtime_seconds": err.get("max_runtime_seconds") if isinstance(err, dict) else None,
        "elapsed_seconds": err.get("elapsed_seconds") if isinstance(err, dict) else None,
        "max_runtime_source": err.get("max_runtime_source") if isinstance(err, dict) else None,
        "error": err or None,
    }
    meta.update(extra)
    return {key: value for key, value in meta.items() if value is not None}


async def _enrich_step_log_author_meta(db: AsyncSession, step: ExecutionStep, metadata: dict | None) -> dict:
    """Attach the user-facing step owner to dispatcher-authored task logs."""
    meta = dict(metadata or {})
    if getattr(step, "service_key", None):
        meta.setdefault("service_key", step.service_key)
    if getattr(step, "resolved_agent_id", None):
        meta.setdefault("agent_id", step.resolved_agent_id)
    if getattr(step, "resolved_subscription_id", None):
        meta.setdefault("agent_subscription_id", step.resolved_subscription_id)

    if not meta.get("agent_name") and step.resolved_subscription_id:
        try:
            from packages.core.models.workspace import AgentSubscription

            sub = (
                await db.execute(
                    select(AgentSubscription).where(
                        AgentSubscription.id == step.resolved_subscription_id,
                    )
                )
            ).scalar_one_or_none()
            if sub:
                from packages.core.constants.agents import (
                    agent_display_name_from_service_key,
                )

                meta.setdefault("agent_id", sub.agent_id)
                # A nameless subscription is still a specific agent; its key
                # is the only identity available, but a raw key is not a name.
                meta["agent_name"] = sub.name or agent_display_name_from_service_key(
                    sub.service_key or step.service_key
                )
        except Exception:
            logger.debug("dispatcher: failed to enrich task log author from subscription", exc_info=True)

    return {key: value for key, value in meta.items() if value is not None}


# ── Task log + Chat notifications ────────────────────────────────────
# These run after the execution rows are staged and share the caller's
# transaction. A visible receipt/log must never commit ahead of (or disappear
# behind) the lease transition it describes. Realtime fanout is queued for
# after commit by workspace_chat.service and remains best-effort.


async def _safe_task_log(
    step: ExecutionStep,
    log_type: str,
    content: str,
    metadata: dict | None = None,
    *,
    db: AsyncSession,
) -> None:
    """Stage one TaskLog atomically with its lease/step transition."""

    from packages.core.constants.agents import MANOR_AGENT_NAME
    from packages.core.constants.task_actors import TaskActor
    from packages.core.models.execution import ExecutionPlan
    from packages.core.services.task_service import add_task_log

    task_id = (
        await db.execute(
            select(ExecutionPlan.task_id).where(ExecutionPlan.id == step.plan_id)
        )
    ).scalar_one_or_none()
    if not task_id:
        return
    enriched_metadata = await _enrich_step_log_author_meta(db, step, metadata)
    created_by = enriched_metadata.get("agent_name") or enriched_metadata.get(
        "agent_id"
    )
    # A step with no resolvable agent ran under the master agent, not under
    # "system" — the dispatcher schedules it, but something did the work.
    actor = TaskActor.AGENT if created_by else TaskActor.MANOR
    await add_task_log(
        db,
        task_id,
        log_type,
        content,
        actor=actor,
        created_by=created_by or MANOR_AGENT_NAME,
        metadata=enriched_metadata,
    )


async def _safe_task_event(
    step: ExecutionStep,
    event_type: str,
    payload: dict,
    *,
    db: AsyncSession,
) -> None:
    """Stage a durable task event with the dispatcher state transition."""
    from packages.core.models.execution import ExecutionPlan
    from packages.core.services import event_emitter

    plan = (
        await db.execute(select(ExecutionPlan).where(ExecutionPlan.id == step.plan_id))
    ).scalar_one_or_none()
    if not plan or not plan.task_id:
        return
    event_payload = {
        **payload,
        "task_id": plan.task_id,
        "plan_id": plan.id,
    }
    await event_emitter.emit_in_session(
        db,
        step.entity_id,
        event_type,
        source="dispatcher",
        payload=event_payload,
        workspace_id=step.workspace_id,
        deliver_after_commit=True,
    )


async def _safe_chat_step_done(
    step: ExecutionStep,
    *,
    cost: Optional[dict] = None,
    locale: str = "en",
    db: AsyncSession,
) -> None:
    if not step.workspace_id:
        return
    try:
        from packages.core.workspace_chat import notifiers as chat_notify

        # Resolve agent name from subscription
        agent_name = None
        if step.resolved_subscription_id:
            try:
                from packages.core.models.workspace import AgentSubscription

                sub = (
                    await db.execute(
                        select(AgentSubscription).where(
                            AgentSubscription.id == step.resolved_subscription_id,
                        )
                    )
                ).scalar_one_or_none()
                if sub:
                    agent_name = sub.name or sub.service_key
            except Exception:
                pass  # best-effort — don't block on agent lookup failure

        # Extract result summary from step
        result_summary = None
        if step.result:
            result_summary = chat_notify.summarize_result_for_chat(step.result, max_chars=2000, locale=locale)

        description = getattr(step, "description", None) or step.step_key.replace("_", " ").title()
        duration = _duration(step)
        cost_usd = (cost or {}).get("usd")

        await chat_notify.notify_step_done(
            entity_id=step.entity_id,
            workspace_id=step.workspace_id,
            plan_id=step.plan_id,
            step_id=step.id,
            step_key=step.step_key,
            kind=step.kind,
            description=description,
            duration_seconds=duration,
            cost_usd=cost_usd,
            subscription_id=step.resolved_subscription_id,
            agent_name=agent_name,
            result_summary=result_summary,
            result=step.result,
            locale=locale,
            db=db,
        )
    except Exception:
        logger.exception("dispatcher: atomic chat notify (done) failed")
        raise


async def _safe_chat_step_failed(
    step: ExecutionStep,
    *,
    error: dict,
    will_retry: bool,
    locale: str = "en",
    db: AsyncSession,
) -> None:
    if not step.workspace_id:
        return
    try:
        from packages.core.workspace_chat import notifiers as chat_notify

        await chat_notify.notify_step_failed(
            entity_id=step.entity_id,
            workspace_id=step.workspace_id,
            plan_id=step.plan_id,
            step_id=step.id,
            step_key=step.step_key,
            error=error,
            will_retry=will_retry,
            locale=locale,
            subscription_id=step.resolved_subscription_id,
            db=db,
        )
    except Exception:
        logger.exception("dispatcher: atomic chat notify (failed) failed")
        raise


async def _safe_chat_step_needs_human(
    step: ExecutionStep,
    *,
    prompt: str,
    pending_action: Optional[dict] = None,
    approval_request_id: Optional[str] = None,
    locale: str = "en",
    db: AsyncSession,
) -> None:
    if not step.workspace_id:
        return
    try:
        from packages.core.workspace_chat import notifiers as chat_notify

        await chat_notify.notify_step_needs_human(
            entity_id=step.entity_id,
            workspace_id=step.workspace_id,
            plan_id=step.plan_id,
            step_id=step.id,
            step_key=step.step_key,
            prompt=prompt,
            subscription_id=step.resolved_subscription_id,
            pending_action=pending_action,
            approval_request_id=approval_request_id,
            locale=locale,
            db=db,
        )
    except Exception:
        logger.exception("dispatcher: atomic chat notify (needs_human) failed")
        raise
