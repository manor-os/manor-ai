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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
    HITL_REQUIRED_PAYLOAD_FIELDS,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_SCOPE_PROMOTED_FROM,
    ApprovalOriginKind,
    ApprovalOutcome,
    ApprovalStatus,
    AuthorizeScope,
    HitlType,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.hitl_request import HitlRequest
from packages.core.services.runtime_authorization import (
    AuthorizationRule,
    PermissionDecision,
)
from packages.core.governance.approval_scope import approval_scope_key

logger = logging.getLogger(__name__)

ApprovalContextLoader = Callable[[], Awaitable[dict[str, Any]]]


class ApprovalAuthorityError(PermissionError):
    """The actor is not allowed to resolve this HITL request."""

    def __init__(self, decision: PermissionDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason or "HITL resolution permission denied.")


class ApprovalDecisionRequiredError(PermissionError):
    """The paused origin needs its dedicated decision, not a generic resume."""


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
    args_hash: Optional[str] = None  # for tool_call dedup
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
    scope = approval_scope_key(subject.action_key, subject.resource_id)
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
        pending_kind = str(origin.context.get("pending_kind") or PendingActionKind.HUMAN_INPUT.value)
        return f"lease:{origin.lease_id}:{pending_kind}"
    if origin.kind == ApprovalOriginKind.TASK and origin.task_id:
        action = scope or subject.capability_id or subject.resource_kind or "action"
        return f"task:{origin.task_id}:{action}"
    if origin.kind == ApprovalOriginKind.OPERATION:
        op_id = str(origin.context.get("draft_id") or origin.message_id or "")
        return f"op:{op_id}"
    if origin.kind == ApprovalOriginKind.CHANNEL and origin.message_id:
        return f"channel:{origin.message_id}"
    # tool_call (or anything else): scoped subject + conversation + args
    subj = scope or subject.capability_id or subject.resource_kind or "action"
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
        resource_id=subject.resource_id,
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
    permission_decision: PermissionDecision,
    spent_credits: Optional[dict] = None,
    reason: Optional[str] = None,
    intrinsic_rule: Optional[str] = None,
    intrinsic_reason: Optional[str] = None,
    intrinsic_high_risk_approved: bool = False,
    policy_hitl_preauthorized: bool = False,
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

    ``policy_hitl_preauthorized`` satisfies an approval-only policy prompt for
    the exact operation. Hard policy blocks and explicit step approval remain
    authoritative.

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

    decision = await _policy_decision(db, subject, origin, spent_credits=spent_credits)

    # 1. Hard block — never approvable, no request minted.
    if not decision.allowed and not decision.pause_for_hitl:
        return ApprovalDecision(
            ApprovalOutcome.DENY,
            decision.reason,
            decision.matched_rule,
        )

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
    policy_auto_approved = bool(decision.allowed and decision.matched_rule) and subject.resource_kind != "platform"
    policy_hitl_needs_human = bool(decision.pause_for_hitl and not policy_hitl_preauthorized)
    needs_human = bool(
        policy_hitl_needs_human
        or subject.requires_approval
        or (subject.risk_level == _HIGH and origin.kind == ApprovalOriginKind.STEP and not intrinsic_high_risk_approved)
    )
    if not needs_human:
        return ApprovalDecision(
            ApprovalOutcome.ALLOW,
            decision.reason,
            decision.matched_rule,
        )

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

    # 3. A standing grant (unified "Always") lives on the workspace policy
    #    auto-approve set, which `decide()` already honored above.
    if policy_auto_approved:
        return ApprovalDecision(
            ApprovalOutcome.ALLOW,
            "standing grant",
            decision.matched_rule,
        )

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
            return ApprovalDecision(
                ApprovalOutcome.ALLOW,
                "operator approved",
                req.matched_rule,
                req,
            )
        if req.status == ApprovalStatus.DENIED:
            return ApprovalDecision(
                ApprovalOutcome.DENY,
                req.reason or "operator rejected",
                req.matched_rule,
                req,
            )
        # still pending → reuse it, no duplicate card
        return ApprovalDecision(
            ApprovalOutcome.NEEDS_HUMAN,
            req.reason,
            req.matched_rule,
            req,
        )

    materialized_origin = await origin.materialize_context()
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
    db,
    request: HitlRequest,
    *,
    by_user_id: Optional[str],
    via: str,
    standing: bool = False,
    changed_by: Optional[str] = None,
    authority_prechecked: bool = False,
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

    if standing and request.workspace_id:
        from packages.core.governance.service import (
            add_auto_approve_action,
            add_auto_approve_capability,
        )

        who = changed_by or by_user_id or "operator"
        # Consent scope: prefer the concrete action the card displayed.
        # Capability is the fallback for subjects with no action_key (e.g. a
        # subagent step gated on file.write). Capability-first here would
        # silently widen one "Always approve workspace.automation.create"
        # click into auto-approving every automation.* action for the whole
        # workspace — broader than what the user was shown.
        if request.action_key:
            await add_auto_approve_action(
                db,
                entity_id=request.entity_id,
                workspace_id=request.workspace_id,
                action_key=request.action_key,
                resource_id=request.resource_id,
                changed_by=who,
            )
        elif request.capability_id:
            await add_auto_approve_capability(
                db,
                entity_id=request.entity_id,
                workspace_id=request.workspace_id,
                capability_id=request.capability_id,
                changed_by=who,
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


async def restore_consumed_grant_for_step(
    db,
    *,
    entity_id: str,
    step_id: str,
    reason: str,
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
            select(HitlRequest)
            .where(
                HitlRequest.entity_id == entity_id,
                HitlRequest.dedup_key == key,
                HitlRequest.status == ApprovalStatus.CONSUMED.value,
            )
            .order_by(HitlRequest.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if req is None:
        return None
    req.status = ApprovalStatus.GRANTED.value
    req.consumed_at = None
    req.resolved_reason = reason
    return req


async def deny_approval(
    db,
    request: HitlRequest,
    *,
    by_user_id: Optional[str],
    via: str,
    reason: Optional[str] = None,
    authority_prechecked: bool = False,
) -> HitlRequest:
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
    db,
    *,
    step_id: Optional[str] = None,
    task_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    reason: str = "origin_terminal",
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
        (
            await db.execute(
                select(HitlRequest).where(
                    HitlRequest.status.in_([s.value for s in APPROVAL_LIVE_STATUSES]),
                    or_(*conds),
                )
            )
        )
        .scalars()
        .all()
    )
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
    db,
    *,
    workspace_ids: list[str],
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
