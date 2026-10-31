"""Shared fail-closed admission for retries after external tool effects."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    ExecutionStepStatus,
)
from packages.core.constants.tool_effects import (
    EffectReconciliationDecision,
    ToolEffectEvidenceKind,
    ToolEffectIntentState,
    ToolEffectOutcome,
)
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.models.execution import ExecutionStep


TOOL_EFFECT_RECONCILIATION_KIND = ToolEffectEvidenceKind.RECONCILIATION.value
EFFECT_APPLIED_DECISION = EffectReconciliationDecision.APPLIED.value
EFFECT_NOT_APPLIED_DECISION = EffectReconciliationDecision.NOT_APPLIED.value


def canonical_effect_digest(effect: dict[str, Any]) -> str:
    """Digest the complete durable effect identity and receipt."""

    return hashlib.sha256(json.dumps(
        effect,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _reconciliation_markers(evidence_refs: Any) -> dict[str, dict[str, Any]]:
    markers: dict[str, dict[str, Any]] = {}
    for ref in evidence_refs or []:
        if not isinstance(ref, dict) or ref.get("kind") != TOOL_EFFECT_RECONCILIATION_KIND:
            continue
        fields = ref.get("fields") if isinstance(ref.get("fields"), dict) else {}
        digest = str(fields.get("effect_digest") or "").strip()
        decision = str(fields.get("decision") or "").strip()
        request_id = str(fields.get("approval_request_id") or "").strip()
        if (
            digest
            and request_id
            and decision in {EFFECT_APPLIED_DECISION, EFFECT_NOT_APPLIED_DECISION}
        ):
            markers[digest] = fields
    return markers


async def _validated_reconciliation_markers(
    db: AsyncSession,
    owner_step: ExecutionStep,
    evidence_refs: Any,
) -> dict[str, dict[str, Any]]:
    """Accept only markers backed by a consumed server HITL decision."""

    candidates = _reconciliation_markers(evidence_refs)
    request_ids = {
        str(marker.get("approval_request_id") or "")
        for marker in candidates.values()
    }
    if not request_ids:
        return {}
    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.models.hitl_request import HitlRequest

    requests = list((await db.execute(
        select(HitlRequest).where(HitlRequest.id.in_(sorted(request_ids)))
    )).scalars().all())
    by_id = {str(request.id): request for request in requests}
    validated: dict[str, dict[str, Any]] = {}
    for digest, marker in candidates.items():
        request = by_id.get(str(marker.get("approval_request_id") or ""))
        context = request.context if request is not None and isinstance(request.context, dict) else {}
        resolution = (
            context.get("effect_reconciliation_resolution")
            if isinstance(context.get("effect_reconciliation_resolution"), dict)
            else {}
        )
        try:
            generation_matches = int(resolution.get("generation") or 0) == int(
                marker.get("generation") or 0
            )
        except (TypeError, ValueError):
            generation_matches = False
        if (
            request is None
            or request.status != ApprovalStatus.CONSUMED.value
            or str(resolution.get("plan_id") or "")
            != str(getattr(owner_step, "plan_id", "") or "")
            or str(getattr(owner_step, "id", "") or "")
            not in {
                str(item)
                for item in (resolution.get("recorded_step_ids") or [])
            }
            or digest not in {
                str(item)
                for item in (resolution.get("effect_digests") or [])
            }
            or str(resolution.get("decision_by_digest", {}).get(digest) or "")
            != str(marker.get("decision") or "")
            or not generation_matches
        ):
            continue
        validated[digest] = marker
    return validated


def _validated_marker_for_effect(
    effect: dict[str, Any],
    markers: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    """Return the trusted marker for an effect and whether it is an APPLIED receipt.

    Reconciliation replaces a pending effect with a richer terminal receipt.
    The receipt cannot have the same canonical JSON digest, so the locked
    server path records its source digest.  Honor that inherited identity only
    when a consumed APPLIED decision validates the exact digest and approval;
    an untrusted worker-provided field alone never settles an effect.
    """

    direct = markers.get(canonical_effect_digest(effect))
    if direct is not None:
        return direct, False

    fields = effect.get("fields") if isinstance(effect.get("fields"), dict) else {}
    source_digest = str(fields.get("source_effect_digest") or "").strip()
    legacy_digest = str(fields.get("effect_digest") or "").strip()
    approval_request_id = str(fields.get("approval_request_id") or "").strip()
    inherited = markers.get(source_digest)
    if (
        effect.get("effect") != ToolEffectOutcome.OPERATOR_CONFIRMED.value
        or fields.get("intent_state")
        != ToolEffectIntentState.OPERATOR_CONFIRMED_APPLIED.value
        or fields.get("delivery_confirmed") is not True
        or not source_digest
        or legacy_digest != source_digest
        or not approval_request_id
        or inherited is None
        or inherited.get("decision") != EFFECT_APPLIED_DECISION
        or str(inherited.get("approval_request_id") or "") != approval_request_id
    ):
        return None, False
    return inherited, True


def _checkpoint_contains_tool_result(
    checkpoint: Any,
    tool_call_id: str | None,
    *,
    effect_ref: dict[str, Any] | None = None,
    within_parent_call_id: str | None = None,
) -> bool:
    """Whether replay consumes this exact return, optionally bound to a receipt.

    Call ids alone are only structural continuity, never effect settlement.
    A supplied receipt must describe the same call and exact terminal bytes.
    """

    call_id = str(tool_call_id or "").strip()
    if not call_id or not isinstance(checkpoint, dict):
        return False
    from packages.core.ai.runtime.workspace_delegation_actions import (
        _raw_tool_call_name,
        _successful_tool_payloads,
        _tool_arguments_hash,
        _tool_call_arguments,
        _tool_call_name,
        _tool_result_canonical_sha256,
    )

    def matches(call: dict[str, Any], content: Any) -> bool:
        if content is None:
            return False
        if effect_ref is None:
            return True
        function = call.get("function")
        arguments = (function if isinstance(function, dict) else call).get("arguments", {})
        if _tool_call_name(call) != _raw_tool_call_name(call):
            # A call_tool declaration: compare what it called.
            arguments = _tool_call_arguments(call)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return False
        if not isinstance(arguments, dict):
            return False
        fields = effect_ref.get("fields")
        handoff = effect_ref.get("kind") == ToolEffectEvidenceKind.RESULT_HANDOFF.value
        if (
            (not handoff and effect_ref.get("kind") != ToolEffectEvidenceKind.TOOL_EFFECT.value)
            or (not handoff and effect_ref.get("effect") not in (
                ToolEffectOutcome.ACTION.value, ToolEffectOutcome.PUBLISH.value,
            ))
            or not isinstance(fields, dict)
            or fields.get("intent_state") != ToolEffectIntentState.RETURNED.value
            or fields.get("tool_call_id") != call_id
            or not fields.get("tool")
            or not fields.get("arguments_hash")
            or _tool_call_name(call) != fields["tool"]
            or _tool_arguments_hash(arguments) != fields["arguments_hash"]
            or (
                fields.get("effect_id")
                and not ToolEffectEvidenceFactory.has_valid_effect_id(effect_ref)
            )
        ):
            return False
        raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        canonical = _tool_result_canonical_sha256(raw)
        if (
            hashlib.sha256(raw.encode("utf-8")).hexdigest() != fields.get("result_sha256")
            and not (canonical and canonical == fields.get("result_canonical_sha256"))
        ):
            return False
        if handoff:
            # A server witness proves where the loop continues, not whether
            # the parent's text reports success. Its child receipt does that.
            return True
        payloads = _successful_tool_payloads([
            {"role": "assistant", "tool_calls": [{**call, "id": call_id}]},
            {"role": "tool", "tool_call_id": call_id, "content": raw},
        ])
        return bool(payloads and payloads[0][3].get("_runtime_terminal_result") is True)

    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint, RuntimeTurnAborted
    from packages.core.ai.runtime.nested_agent import _qualified_call_id, runtime_nested_skill_state

    node, path = checkpoint, []
    included = within_parent_call_id is None
    for depth in range(9):
        try:
            parsed = RuntimeAgentCheckpoint.from_dict(node)
        except (TypeError, ValueError):
            parsed = None
            if "pending_tool_call" in node:
                return False
            if depth == 0:
                # Only a Runtime checkpoint can prove a returned call.
                return False
        if parsed is not None:
            pending = parsed.pending_tool_call
            pending_id = _qualified_call_id(str(pending["id"]), path)
            if included and pending_id == call_id:
                returned = parsed.pending_tool_result
                return bool(
                    isinstance(returned, dict) and returned.get("id") == pending.get("id")
                    and matches(pending, returned.get("content"))
                )
            if included and any(_qualified_call_id(str(call.get("id") or ""), path) == call_id for call in parsed.remaining_tool_calls):
                return False
        if included:
            calls, results = [], []
            for index, message in enumerate(node.get("messages") or []):
                if not isinstance(message, dict):
                    continue
                if message.get("role") == "assistant":
                    calls.extend(
                        (index, call) for call in message.get("tool_calls") or []
                        if isinstance(call, dict) and _qualified_call_id(str(call.get("id") or ""), path) == call_id
                    )
                elif message.get("role") == "tool" and _qualified_call_id(str(message.get("tool_call_id") or ""), path) == call_id:
                    results.append((index, message.get("content")))
            if calls or results:
                return bool(
                    len(calls) == len(results) == 1 and calls[0][0] < results[0][0]
                    and matches(calls[0][1], results[0][1])
                )
        if parsed is None:
            return False
        try:
            state = runtime_nested_skill_state(node)
        except RuntimeTurnAborted:
            return False
        if not state or state.get("terminal_failure") == "tool_outcome_unconfirmed":
            return False
        included = included or pending_id == within_parent_call_id
        path.append((pending["id"], state["skill_id"]))
        if state.get("kind") == "pending" and isinstance(state.get("checkpoint"), dict):
            node = state["checkpoint"]
        elif state.get("kind") == "completed" and isinstance(state.get("response"), dict) and isinstance(state.get("messages"), list):
            node = state
        elif (
            state.get("kind") == "transcript" and isinstance(state.get("messages"), list)
            and isinstance(state.get("rounds"), int) and isinstance(state.get("usage"), dict)
            and isinstance(state.get("tool_calls_made"), list)
        ):
            node = state
        else:
            return False
    return False


def _checkpoint_proves_tool_effect(
    checkpoint: Any, effect: dict[str, Any], *, evidence_refs: Any, owner_step: Any,
    within_parent_call_id: str | None = None,
) -> bool:
    """Use direct observations or host-only, flattened child-to-parent handoffs."""
    identity = ToolEffectEvidenceFactory.identity(effect)
    # Exact bytes do not transfer another execution's receipt to this Step.
    # Legacy receipts lacking ownership still need the strict direct call /
    # result proof below; they never gain host-only handoff authority.
    if (
        (identity.owner_step_id and identity.owner_step_id != str(getattr(owner_step, "id", "")))
        or (identity.owner_plan_id and identity.owner_plan_id != str(getattr(owner_step, "plan_id", "")))
    ):
        return False
    owner_params = getattr(owner_step, "params", None)
    if isinstance(owner_params, dict) and ACTION_EFFECT_GENERATION_PARAM_KEY in owner_params:
        try:
            generation = max(0, int(owner_params[ACTION_EFFECT_GENERATION_PARAM_KEY] or 0))
        except (TypeError, ValueError):
            return False
        if identity.effect_generation != str(generation):
            return False
    if _checkpoint_contains_tool_result(
        checkpoint, identity.tool_call_id, effect_ref=effect,
        within_parent_call_id=within_parent_call_id,
    ):
        return True
    source_fields = effect.get("fields")
    if (
        effect.get("kind") != ToolEffectEvidenceKind.TOOL_EFFECT.value
        or effect.get("effect") not in (ToolEffectOutcome.ACTION.value, ToolEffectOutcome.PUBLISH.value)
        or not isinstance(source_fields, dict)
        or not source_fields.get("effect_id")
        or source_fields.get("intent_state") != ToolEffectIntentState.RETURNED.value
        or not source_fields.get("result_sha256")
        or not ToolEffectEvidenceFactory.has_valid_effect_id(effect)
        or identity.owner_step_id != str(getattr(owner_step, "id", ""))
        or identity.owner_plan_id != str(getattr(owner_step, "plan_id", ""))
    ):
        return False
    for witness in evidence_refs or []:
        if not isinstance(witness, dict) or witness.get("kind") != ToolEffectEvidenceKind.RESULT_HANDOFF.value:
            continue
        target = ToolEffectEvidenceFactory.identity(witness)
        fields = witness.get("fields")
        if not isinstance(fields, dict):
            continue
        if (
            target.owner_step_id == identity.owner_step_id
            and target.owner_plan_id == identity.owner_plan_id
            and target.effect_generation == identity.effect_generation
            and target.coordinator_child_step_id == identity.coordinator_child_step_id
            and target.tool == "invoke_skill"
            and target.tool_call_id != identity.tool_call_id
            and fields.get("source_effect_id") == ToolEffectEvidenceFactory.effect_id(effect)
            and fields.get("source_effect_digest") == ToolEffectEvidenceFactory.receipt_digest(effect)
            and _checkpoint_contains_tool_result(
                checkpoint, target.tool_call_id, effect_ref=witness,
                within_parent_call_id=within_parent_call_id,
            )
        ):
            # A witness never points at another witness. Nested completion
            # flattens only proven descendants onto its new parent, so cycles
            # cannot become replay authority; scope traversal is depth-bounded.
            return True
    return False


async def has_ambiguous_external_effect(
    db: AsyncSession,
    evidence_refs: Any,
    *,
    owner_step: ExecutionStep | None = None,
) -> bool:
    """Whether replay could repeat an external effect without proof of safety.

    An effect needs an exact observation or host-owned handoff before replay
    is safe. This detects uncertainty, not who must resolve it: first-party
    file recovery belongs to the system, while an unconfirmed provider
    effect fails the worker-job Step without replay.
    """

    effects = [
        ref
        for ref in (evidence_refs or [])
        if (
            isinstance(ref, dict)
            and ref.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
        )
    ]
    if not effects:
        return False
    if owner_step is None:
        # Markers are scoped to their exact owning Step.  Without it neither a
        # marker nor a child id is replay authority.
        return True
    markers = await _validated_reconciliation_markers(
        db,
        owner_step,
        evidence_refs,
    )
    active_effects = []
    for ref in effects:
        marker, inherited_applied_receipt = _validated_marker_for_effect(ref, markers)
        decision = marker.get("decision") if marker is not None else None
        if (
            decision == EFFECT_NOT_APPLIED_DECISION
            or inherited_applied_receipt
            or (
                decision == EFFECT_APPLIED_DECISION
                and ref.get("effect") != ToolEffectOutcome.PENDING.value
            )
        ):
            continue
        active_effects.append(ref)
    if not active_effects:
        return False
    if any(str(ref.get("coordinator_child_step_id") or "").strip() for ref in active_effects):
        # Coordinator child Steps were retired with the Plan engine; no
        # durable child can prove a child-scoped receipt safe to replay.
        return True
    checkpoint = getattr(owner_step, "runtime_checkpoint", None)
    owner_done = (
        str(getattr(owner_step, "step_status", ""))
        == ExecutionStepStatus.DONE.value
    )
    return any(
        not (
            owner_done
            and effect.get("effect") != ToolEffectOutcome.PENDING.value
        )
        and not _checkpoint_proves_tool_effect(
            checkpoint, effect, evidence_refs=evidence_refs, owner_step=owner_step,
        )
        for effect in active_effects
    )


__all__ = [
    "canonical_effect_digest",
    "has_ambiguous_external_effect",
    "EFFECT_APPLIED_DECISION",
    "EFFECT_NOT_APPLIED_DECISION",
    "TOOL_EFFECT_RECONCILIATION_KIND",
]
