"""Unified approval resolution — one decision function every gate calls.

``resolve_approval`` is the single place that answers "allow / deny / needs a
human?" for any gated action, on any plane. It replaces the parallel logic that
lived independently in the runtime tool guard and the dispatcher step gate.

Design invariants (see the redesign RFC):

  * ONE decision. Every gate delegates here instead of re-implementing risk /
    policy / grant checks. Approving once therefore satisfies every gate that
    shares the subject.
  * Hard blocks stay hard. ``never_allow`` / risk ceiling / budget caps are not
    approvable and no grant can override them — this function returns ``deny``
    for them and never mints a request.
  * ONE "Always". A standing grant is written to the workspace policy
    auto-approve set — the single store both planes already consult through
    ``decide()`` — so "Always approve" is honored uniformly, not as a per-plane
    preference.
  * ONE open request per subject. ``dedup_key`` + a partial unique index mean a
    re-tripped gate reuses the existing request/card instead of minting a
    duplicate.
  * Lifecycle owns cleanup. When an origin (step/task) reaches a terminal state,
    ``resolve_origin_requests`` expires its open requests, so nothing is
    orphaned and counts derive from open requests, not stale messages.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
    HITL_REQUIRED_PAYLOAD_FIELDS,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_SCOPE_PROMOTED_FROM,
    ApprovalOriginKind,
    ApprovalStatus,
    AuthorizeScope,
    HitlType,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.hitl_request import HitlRequest

logger = logging.getLogger(__name__)

Outcome = Literal["allow", "deny", "needs_human"]


@dataclass(frozen=True)
class ApprovalSubject:
    """WHAT needs approval — plane-agnostic."""
    entity_id: str
    action_key: Optional[str] = None
    capability_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    risk_level: str = "medium"
    kind: str = "action"                 # policy "kind" axis (action / subagent / ...)
    requires_approval: bool = False      # step-intrinsic flag
    workspace_id: Optional[str] = None


@dataclass(frozen=True)
class ApprovalOrigin:
    """WHERE the block is happening — so the right surface renders/resumes it."""
    kind: str                            # an ``ApprovalOriginKind`` value
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    step_id: Optional[str] = None
    plan_id: Optional[str] = None
    task_id: Optional[str] = None
    lease_id: Optional[str] = None       # for lease-origin (mid-execution) dedup
    args_hash: Optional[str] = None      # for tool_call dedup
    context: dict = field(default_factory=dict)


@dataclass
class ApprovalDecision:
    outcome: Outcome
    reason: Optional[str] = None
    matched_rule: Optional[str] = None
    request: Optional[HitlRequest] = None  # present when outcome == needs_human

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


_HIGH = "high"

#: Payload keys the RECORD LAYER writes, as opposed to copy a producer
#: supplies. Excluded from the "did this card come with copy?" test so that
#: validation stays idempotent — it runs once in ``mint_approval_request`` and
#: again in ``_create_pending_request`` on its own output.
_RECORD_LAYER_PAYLOAD_KEYS: frozenset[str] = frozenset({
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_SCOPE_PROMOTED_FROM,
})


def _args_hash(payload: Any) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        blob = str(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def dedup_key_for(subject: ApprovalSubject, origin: ApprovalOrigin) -> str:
    """Deterministic key: at most one OPEN request per key.

    A step is keyed by its id (one approval per step, whatever gate trips). A
    tool call is keyed by conversation + action + args, so re-asking for the
    identical call reuses the card but a changed payload mints a fresh one.
    A proposal cohort (M8) is keyed by its ProposalRecord id — one request
    covers the whole cohort, and the approve/reject chat-card path can find
    the open request back from the proposal id alone. Per-item requests
    (M13 experiment items — governed individually, not as a cohort) are
    keyed by the ProposalItemRecord id.
    A mid-execution lease pause is keyed by lease id + pause reason."""
    if subject.resource_kind == "proposal" and subject.resource_id:
        return f"proposal:{subject.resource_id}"
    if subject.resource_kind == "proposal_item" and subject.resource_id:
        return f"proposal_item:{subject.resource_id}"
    if origin.kind == ApprovalOriginKind.STEP and origin.step_id:
        return f"step:{origin.step_id}"
    if origin.kind == ApprovalOriginKind.LEASE and origin.lease_id:
        # One request per (lease, pause reason): a lease can legitimately pause
        # for different reasons (login vs confirm), but re-tripping the SAME
        # reason must reuse the card instead of minting a duplicate.
        pending_kind = str(
            origin.context.get("pending_kind") or PendingActionKind.HUMAN_INPUT.value
        )
        return f"lease:{origin.lease_id}:{pending_kind}"
    if origin.kind == ApprovalOriginKind.OPERATION:
        op_id = str(origin.context.get("draft_id") or origin.message_id or "")
        return f"op:{op_id}"
    if origin.kind == ApprovalOriginKind.CHANNEL and origin.message_id:
        return f"channel:{origin.message_id}"
    # tool_call (or anything else): subject + conversation + args
    subj = subject.action_key or subject.capability_id or subject.resource_kind or "action"
    conv = origin.conversation_id or ""
    ah = origin.args_hash or (_args_hash(origin.context.get("arguments")) if origin.context.get("arguments") else "")
    return f"tool:{conv}:{subj}:{ah}"


async def _policy_decision(db, subject: ApprovalSubject, origin: ApprovalOrigin, *, spent_credits: Optional[dict]):
    """Reuse the existing policy engine for hard blocks + policy HITL +
    workspace auto-approve. Returns a PolicyDecision-like object."""
    from packages.core.governance.service import check_step_policy

    return await check_step_policy(
        db,
        workspace_id=subject.workspace_id,
        kind=subject.kind,
        action_key=subject.action_key,
        risk_level=subject.risk_level,
        capability_id=subject.capability_id,
        spent_credits_per_kind=spent_credits,
        task_id=origin.task_id,
    )


async def resolve_approval(
    db,
    *,
    subject: ApprovalSubject,
    origin: ApprovalOrigin,
    spent_credits: Optional[dict] = None,
    reason: Optional[str] = None,
    intrinsic_rule: Optional[str] = None,
    intrinsic_reason: Optional[str] = None,
    hitl_type: str = HitlType.AUTHORIZE.value,
    payload: Optional[dict] = None,
) -> ApprovalDecision:
    """The one decision. See module docstring.

    ``intrinsic_rule``/``intrinsic_reason`` let a plane label its own
    intrinsic (non-policy) trigger — e.g. the runtime guard's
    ``direct_chat_baseline``. Without them, step-plane ``step.*`` names are
    synthesized.

    ``hitl_type``/``payload`` let a caller that knows something this function
    cannot — e.g. the dispatcher gate, which knows the step already failed and
    why — mint an ``error`` card instead of the default ``authorize`` one.
    They are threaded into the mint so the row is written correctly the first
    time; writing them back in a second UPDATE would leave a window where the
    card renders as a bare "needs approval", which is the loop this phase
    exists to kill. Ignored when an existing open request is reused — the card
    already on screen wins.
    """
    decision = await _policy_decision(db, subject, origin, spent_credits=spent_credits)

    # 1. Hard block — never approvable, no request minted.
    if not decision.allowed and not decision.pause_for_hitl:
        return ApprovalDecision("deny", decision.reason, decision.matched_rule)

    # 2. Does a human need to say yes? Policy asked for HITL, OR the subject
    #    is intrinsically approval-required, OR — for plan steps only — it is
    #    high-risk. The intrinsic high-risk trigger is deliberately
    #    step-origin-only: a step is a pre-declared unit an operator approves
    #    as a whole, while a tool call usually runs INSIDE an envelope that
    #    was already approved (a leased step, an in-flight chat turn) — making
    #    high-risk re-trigger there would re-ask for what was just approved,
    #    the exact loop this core exists to kill. Runtime callers express
    #    their own intrinsic trigger via subject.requires_approval.
    # Workspace-plane standing grants (auto_approve_actions/capabilities,
    # including a wildcard "*" rule) cannot waive baseline approval for
    # platform-scoped subjects (resource_kind == "platform") — those actions
    # are not workspace-governed resources, so no workspace policy rule,
    # however permissive, can legitimately stand in for the actor's own
    # confirmation. Only the actor's own direct-chat standing consent
    # (handled separately, via the user-preference check in
    # guard_runtime_tool_action) can waive it. Concretely: a rule author
    # who is NOT a platform admin must not be able to auto-approve a
    # platform action just by owning the workspace's policy.
    policy_auto_approved = (
        bool(decision.allowed and decision.matched_rule)
        and subject.resource_kind != "platform"
    )
    needs_human = bool(
        decision.pause_for_hitl
        or subject.requires_approval
        or (subject.risk_level == _HIGH and origin.kind == ApprovalOriginKind.STEP)
    )
    if not needs_human:
        return ApprovalDecision("allow", decision.reason, decision.matched_rule)

    # Name the trigger so the caller can render the right card and pick the
    # right error type. A real policy HITL rule wins; otherwise the caller's
    # intrinsic label applies, falling back to synthetic "step.*" names —
    # parity with the pre-unification dispatcher gates, which the blueprint
    # report and the dispatcher tests key off ("step.*" ⇒ intrinsic step
    # approval, any other rule ⇒ a governance policy pause).
    subj = subject.action_key or subject.capability_id or subject.resource_kind or "action"
    if decision.pause_for_hitl and decision.matched_rule:
        hitl_rule, hitl_reason = decision.matched_rule, decision.reason
    elif intrinsic_rule:
        hitl_rule = intrinsic_rule
        hitl_reason = intrinsic_reason or f"Approval required for {subj!r}."
    elif subject.requires_approval:
        hitl_rule = "step.requires_approval"
        hitl_reason = f"Step requires operator approval before dispatching {subj!r}."
    else:  # subject.risk_level == high
        hitl_rule = "step.high_risk"
        hitl_reason = (
            f"High-risk step needs one-time operator approval before "
            f"dispatching {subj!r}."
        )

    # A human approval needs a surface to render on: for steps that surface is
    # the workspace chat; for tool calls the conversation itself. A subject
    # with NEITHER has nowhere to show a card, so signal needs_human with NO
    # request — the caller fails closed rather than minting an unresolvable,
    # orphaned row.
    if not subject.workspace_id and not origin.conversation_id:
        return ApprovalDecision("needs_human", hitl_reason, hitl_rule)

    # 3. A standing grant (unified "Always") lives on the workspace policy
    #    auto-approve set, which `decide()` already honored above.
    if policy_auto_approved:
        return ApprovalDecision("allow", "standing grant", decision.matched_rule)

    # 4. One-time grant path — find/create the single open request for this subject.
    key = dedup_key_for(subject, origin)
    req = await _find_open_request(db, subject.entity_id, key)
    if req is not None:
        if req.status == ApprovalStatus.GRANTED:
            # Allow, but do NOT consume here: the caller consumes at the point
            # of irreversible proceed (lease creation / tool execution). If the
            # gate passes but the action doesn't actually run this pass (e.g.
            # no worker bound), the grant must survive for the next pass —
            # burning it at decision time would re-pause an approved step, the
            # very loop this core exists to kill.
            return ApprovalDecision("allow", "operator approved", req.matched_rule, req)
        if req.status == ApprovalStatus.DENIED:
            return ApprovalDecision("deny", req.reason or "operator rejected", req.matched_rule, req)
        # still pending → reuse it, no duplicate card
        return ApprovalDecision("needs_human", req.reason, req.matched_rule, req)

    req = await _create_pending_request(
        db, subject=subject, origin=origin, dedup_key=key,
        reason=reason or hitl_reason, matched_rule=hitl_rule,
        # Defaults to "authorize": this function IS the authorization
        # decision, so absent a caller that knows better every request it
        # mints is by construction an "authorize" ask.
        hitl_type=hitl_type, payload=payload,
    )
    return ApprovalDecision("needs_human", req.reason, req.matched_rule, req)


# ── request lifecycle ──────────────────────────────────────────────

async def find_requests_by_dedup(db, *, entity_id: str, dedup_key: str):
    """ALL requests ever minted for a dedup key, newest first — for callers
    whose semantics depend on terminal history (e.g. provider approvals:
    an already-decided registration must not re-mint)."""
    return (
        await db.execute(
            select(HitlRequest).where(
                HitlRequest.entity_id == entity_id,
                HitlRequest.dedup_key == dedup_key,
            ).order_by(HitlRequest.created_at.desc())
        )
    ).scalars().all()


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
    substantive = {
        key: value for key, value in validated.items()
        if key not in _RECORD_LAYER_PAYLOAD_KEYS
    }
    if substantive:
        required = HITL_REQUIRED_PAYLOAD_FIELDS.get(hitl_type, ())
        missing = [key for key in required if not substantive.get(key)]
        if missing:
            raise ValueError(
                f"hitl_type={hitl_type!r} payload missing required field(s): "
                f"{', '.join(missing)}"
            )
    if hitl_type == HitlType.AUTHORIZE.value:
        scope = str(validated.get(PAYLOAD_KEY_SCOPE) or "").strip()
        if scope and scope not in AuthorizeScope.values():
            raise ValueError(
                f"authorize payload has unknown scope {scope!r}; expected one "
                f"of {AuthorizeScope.values()}"
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
    db, *, subject: ApprovalSubject, origin: ApprovalOrigin,
    dedup_key: Optional[str] = None, reason: Optional[str] = None,
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
    return await _create_pending_request(
        db, subject=subject, origin=origin, dedup_key=key,
        reason=reason, matched_rule=matched_rule,
        hitl_type=hitl_type, payload=validated_payload,
    )

async def _find_open_request(db, entity_id: str, dedup_key: str) -> Optional[HitlRequest]:
    """The active request for a subject: still-pending OR granted-but-unconsumed.
    A granted request must remain findable so the next resolve honors it (else
    the grant is invisible and the gate re-requests — the loop we're fixing)."""
    return (
        await db.execute(
            select(HitlRequest).where(
                HitlRequest.entity_id == entity_id,
                HitlRequest.dedup_key == dedup_key,
                HitlRequest.status.in_([s.value for s in APPROVAL_LIVE_STATUSES]),
            ).order_by(HitlRequest.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def _create_pending_request(
    db, *, subject: ApprovalSubject, origin: ApprovalOrigin,
    dedup_key: str, reason: Optional[str], matched_rule: Optional[str],
    hitl_type: str = HitlType.AUTHORIZE.value,
    payload: Optional[dict] = None,
) -> HitlRequest:
    # Validated here as well as in mint_approval_request: this is the one
    # function that actually constructs the row, so every creating path —
    # including resolve_approval's — passes through this check.
    payload = _validate_hitl_payload(hitl_type, payload)
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
        context=dict(origin.context or {}),
    )
    db.add(req)
    try:
        await db.flush()
    except IntegrityError:
        # Lost a race to the partial-unique dedup index — reuse the winner.
        await db.rollback()
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
    db, request: HitlRequest, *, by_user_id: Optional[str], via: str,
    standing: bool = False, changed_by: Optional[str] = None,
) -> HitlRequest:
    """Approve a request.

    ``standing=True`` is the **promotion**: the request was minted at
    ``authorize`` scope ``action`` — "may I do this one thing, with the
    content you just read?" — and the user answered a bigger question,
    "you may do this class of thing from now on". It writes the subject into
    the workspace policy auto-approve set (the unified "Always" store both
    planes honor) and records the widened scope on the row, so the audit
    trail says which of the two questions was actually answered.

    The promotion is never second-guessed by capability class: the user is the
    authority on what they want standing, and ``never_allow`` — which they can
    themselves edit through the governance API — is the only hard block.
    """
    if request.status not in APPROVAL_LIVE_STATUSES:
        return request
    request.status = ApprovalStatus.GRANTED.value
    request.decided_by_user_id = by_user_id
    request.decided_at = datetime.now(timezone.utc)
    request.decided_via = via
    request.resolved_reason = "approved"

    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et
    await ledger_adapters.record_approval_event(
        db, request, ledger_et.APPROVAL_GRANTED, actor_id=by_user_id,
    )

    if standing and request.workspace_id:
        from packages.core.governance.service import (
            add_auto_approve_action, add_auto_approve_capability,
        )
        who = changed_by or by_user_id or "operator"
        # Consent scope: prefer the CONCRETE action the card displayed; the
        # capability is the fallback for subjects with no action_key (e.g. a
        # subagent step gated on file.write). Capability-first here would
        # silently widen one "Always approve workspace.automation.create"
        # click into auto-approving every automation.* action for the whole
        # workspace — broader than what the user was shown.
        if request.action_key:
            await add_auto_approve_action(
                db, entity_id=request.entity_id, workspace_id=request.workspace_id,
                action_key=request.action_key, changed_by=who,
            )
        elif request.capability_id:
            await add_auto_approve_capability(
                db, entity_id=request.entity_id, workspace_id=request.workspace_id,
                capability_id=request.capability_id, changed_by=who,
            )
        # Name what just happened on the row itself: this request was asked at
        # action scope and answered at tool scope. Reassigned rather than
        # mutated in place — a JSONB dict mutated in place is not seen as
        # dirty, and the promotion would never reach the database.
        request.payload = {
            **(request.payload or {}),
            PAYLOAD_KEY_SCOPE: AuthorizeScope.TOOL.value,
            PAYLOAD_KEY_SCOPE_PROMOTED_FROM: AuthorizeScope.ACTION.value,
        }
    return request


async def grant_open_request_for_step(
    db, *, entity_id: str, step_id: str,
    by_user_id: Optional[str], via: str = "step_resume",
) -> Optional[HitlRequest]:
    """Grant the open request a step is paused on, if any.

    Operator surfaces that "resume" a waiting step (task-detail Resume, retry
    buttons) call this so the resume IS the approval — without it the
    dispatcher gate re-pauses the reparked step on its still-pending request,
    which is exactly the "I already resumed/approved it" loop (#317)."""
    req = await _find_open_request(db, entity_id, f"step:{step_id}")
    if req is not None and req.status == ApprovalStatus.PENDING:
        await grant_approval(db, req, by_user_id=by_user_id, via=via)
    return req


async def restore_consumed_grant_for_step(
    db, *, entity_id: str, step_id: str, reason: str,
) -> Optional[HitlRequest]:
    """Give the operator's one-time grant back when the authorized work
    never actually happened.

    A step's grant is consumed the instant its lease goes out. If that lease
    then fails for a TRANSIENT reason — the user's local worker was offline, so
    nothing ran at all — the authorization still stands; what failed was the
    infrastructure. Re-asking for it is the fifteen-approvals loop.

    Flipping the consumed row back to ``granted`` means the next gate pass
    honors it through the ordinary find-open path. That is deliberate: no
    "skip the approval gate" branch is introduced, so hard policy blocks
    (never_allow / risk ceiling / budget) are still evaluated on every pass
    exactly as before.

    Returns the restored request, or None when there is nothing to restore.
    A still-live request is left alone — the card on screen wins.
    """
    key = f"step:{step_id}"
    live = await _find_open_request(db, entity_id, key)
    if live is not None:
        return None
    req = (
        await db.execute(
            select(HitlRequest).where(
                HitlRequest.entity_id == entity_id,
                HitlRequest.dedup_key == key,
                HitlRequest.status == ApprovalStatus.CONSUMED.value,
            ).order_by(HitlRequest.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if req is None:
        return None
    req.status = ApprovalStatus.GRANTED.value
    req.consumed_at = None
    req.resolved_reason = reason
    return req


async def deny_approval(
    db, request: HitlRequest, *, by_user_id: Optional[str], via: str,
    reason: Optional[str] = None,
) -> HitlRequest:
    if request.status not in (ApprovalStatus.PENDING,):
        return request
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
        db, request, ledger_et.APPROVAL_DENIED, actor_id=by_user_id,
    )
    return request


async def consume_approval(db, request: HitlRequest) -> HitlRequest:
    """Mark a one-time grant as spent — the gate proceeded on it."""
    if request.status == ApprovalStatus.GRANTED:
        request.status = ApprovalStatus.CONSUMED.value
        request.consumed_at = datetime.now(timezone.utc)
        from packages.core.ledger import adapters as ledger_adapters
        from packages.core.ledger import event_types as ledger_et
        await ledger_adapters.record_approval_event(db, request, ledger_et.APPROVAL_CONSUMED)
    return request


async def resolve_origin_requests(
    db, *, step_id: Optional[str] = None, task_id: Optional[str] = None,
    plan_id: Optional[str] = None, reason: str = "origin_terminal",
) -> int:
    """Expire every OPEN request attached to a terminal origin — the fix for
    orphaned cards. Covers granted-but-unconsumed rows too: once the origin
    is terminal nothing may run anymore, so a lingering grant must be revoked
    (fail safe) rather than left findable forever. Returns how many closed."""
    conds = []
    if step_id:
        conds.append(HitlRequest.origin_step_id == step_id)
    if task_id:
        conds.append(HitlRequest.origin_task_id == task_id)
    if plan_id:
        conds.append(HitlRequest.origin_plan_id == plan_id)
    if not conds:
        return 0
    from sqlalchemy import or_

    rows = (
        await db.execute(
            select(HitlRequest).where(
                HitlRequest.status.in_([s.value for s in APPROVAL_LIVE_STATUSES]), or_(*conds),
            )
        )
    ).scalars().all()
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
    return int(
        (
            await db.execute(
                select(func.count(HitlRequest.id)).where(
                    HitlRequest.workspace_id == workspace_id,
                    HitlRequest.status == ApprovalStatus.PENDING,
                )
            )
        ).scalar_one()
    )


async def count_open_requests_by_workspace(
    db, *, workspace_ids: list[str],
) -> dict[str, int]:
    """Batch variant for the workspace-list sidebar stats."""
    if not workspace_ids:
        return {}
    rows = (
        await db.execute(
            select(HitlRequest.workspace_id, func.count(HitlRequest.id))
            .where(
                HitlRequest.workspace_id.in_(workspace_ids),
                HitlRequest.status == ApprovalStatus.PENDING,
            )
            .group_by(HitlRequest.workspace_id)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}
