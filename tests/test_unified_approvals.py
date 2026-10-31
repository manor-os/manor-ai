"""The unified approval core: gate-matrix parity + this week's incidents.

resolve_approval() is the single decision every gate will delegate to. These
tests pin the security-critical behavior BEFORE any gate is rewired onto it:

  * hard blocks stay hard and mint no request;
  * high-risk / requires_approval / policy-HITL need a human (one request);
  * a granted request lets the SAME subject through (approval sticks — the fix
    for the #289 / #317 loops), cross-plane by construction;
  * one open request per subject (dedup — the fix for badge inflation);
  * a terminal origin expires its open requests (the orphan-card fix);
  * versioned exact rules apply uniformly to future operations while pending
    requests and deny rules remain authoritative.
"""
import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.constants.approvals import (
    ApprovalConsumeOutcome,
    ApprovalStatus,
)
from packages.core.governance import WorkspacePolicy, get_policy, update_policy
from packages.core.governance.approval_policy import ApprovalPolicyOverride
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.governance.approvals import (
    ApprovalConsumptionError,
    ApprovalOrigin,
    ApprovalSubject,
    claim_approval_consumption,
    consume_approval,
    count_open_requests,
    grant_approval,
    resolve_origin_requests,
)
from packages.core.ledger import event_types as ledger_et
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services.runtime_authorization import (
    AuthorizationRule,
    PermissionDecision,
)


async def resolve_approval(*args, **kwargs):
    """Governance tests run inside an explicitly authorized system worker."""
    kwargs.setdefault(
        "permission_decision",
        PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER),
    )
    from packages.core.governance.approvals import resolve_approval as _resolve

    return await _resolve(*args, **kwargs)


async def _ws(db, *, user_id: str = "op"):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db.add(Entity(id=entity_id, name="Approval test", settings={"approval_defaults": account_defaults()}))
    db.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"op-{entity_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db.add(UserMembership(
        user_id=user_id,
        entity_id=entity_id,
        role="owner",
        status="active",
    ))
    db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Gov WS", operating_model={}))
    await db.flush()
    await update_policy(db, entity_id=entity_id, workspace_id=workspace_id, policy=WorkspacePolicy())
    return entity_id, workspace_id


def _policy(*rules):
    return WorkspacePolicy(approval=ApprovalPolicyOverride(rules=[
        {"id": f"rule-{index}", "effect": effect, "selector": {"kind": "action", "action_key": action}}
        for index, (effect, action) in enumerate(rules)
    ]))


def _subject(entity_id, workspace_id, **kw):
    base = dict(
        entity_id=entity_id, workspace_id=workspace_id,
        action_key="social_post.publish", capability_id="external.social",
        risk_level="medium", kind="action",
    )
    base.update(kw)
    return ApprovalSubject(**base)


def _step_origin(**kw):
    return ApprovalOrigin(kind="step", step_id=kw.pop("step_id", generate_ulid()), **kw)


# ── gate matrix ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_low_risk_no_rule_allows(db_session):
    e, w = await _ws(db_session)
    d = await resolve_approval(
        db_session, subject=_subject(e, w, action_key="workspace.file.modify", risk_level="low"), origin=_step_origin(),
    )
    assert d.outcome == "allow"


@pytest.mark.asyncio
async def test_explicit_proposal_review_suspends_standing_grant_but_accepts_one_time(db_session):
    e, w = await _ws(db_session)
    await update_policy(
        db_session, entity_id=e, workspace_id=w,
        policy=_policy(("allow", "workspace.proposal.task")),
        changed_by="op",
    )
    subject = _subject(
        e, w, action_key="workspace.proposal.task", capability_id=None,
        resource_kind="proposal", resource_id=generate_ulid(), requires_approval=True,
    )
    origin = ApprovalOrigin(kind="operation", context={"plane": "strategist_proposal"})
    kwargs = dict(
        subject=subject, origin=origin, require_explicit_approval=True,
        intrinsic_rule="proposal.review", intrinsic_approval_preauthorized=True,
        policy_hitl_preauthorized=True,
    )
    decision = await resolve_approval(db_session, **kwargs)
    assert decision.outcome == "needs_human"
    assert decision.request is not None
    await grant_approval(db_session, decision.request, by_user_id="op", via="proposal_card")
    granted = await resolve_approval(db_session, **kwargs)
    assert granted.outcome == "allow"
    assert granted.request.id == decision.request.id


@pytest.mark.asyncio
async def test_explicit_proposal_review_does_not_override_policy_deny(db_session):
    e, w = await _ws(db_session)
    await update_policy(
        db_session, entity_id=e, workspace_id=w,
        policy=_policy(("deny", "social_post.publish")),
        changed_by="op",
    )
    decision = await resolve_approval(
        db_session, subject=_subject(e, w), origin=_step_origin(),
        require_explicit_approval=True,
    )
    assert decision.outcome == "deny"
    assert decision.request is None


@pytest.mark.asyncio
async def test_hard_block_denies_and_mints_no_request(db_session):
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=_policy(("deny", "social_post.publish")),
                        changed_by="t")
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="high"), origin=_step_origin())
    assert d.outcome == "deny"
    assert d.request is None
    assert await count_open_requests(db_session, workspace_id=w) == 0


@pytest.mark.asyncio
async def test_high_risk_needs_human_and_creates_one_request(db_session):
    e, w = await _ws(db_session)
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="high"), origin=_step_origin())
    assert d.outcome == "needs_human"
    assert d.request is not None and d.request.status == "pending"
    assert await count_open_requests(db_session, workspace_id=w) == 1


@pytest.mark.asyncio
async def test_requires_approval_flag_needs_human(db_session):
    e, w = await _ws(db_session)
    d = await resolve_approval(
        db_session, subject=_subject(e, w, risk_level="low", requires_approval=True), origin=_step_origin(),
    )
    assert d.outcome == "needs_human"


@pytest.mark.asyncio
async def test_policy_hitl_needs_human(db_session):
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=_policy(("ask", "social_post.publish")),
                        changed_by="t")
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="medium"), origin=_step_origin())
    assert d.outcome == "needs_human"


@pytest.mark.asyncio
async def test_policy_auto_approve_allows_high_risk(db_session):
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=_policy(("allow", "social_post.publish")),
                        changed_by="t")
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="high"), origin=_step_origin())
    assert d.outcome == "allow"
    assert await count_open_requests(db_session, workspace_id=w) == 0


@pytest.mark.asyncio
async def test_policy_auto_approve_cannot_waive_platform_scoped_baseline(db_session):
    """A workspace's auto_approve rule — even an unrestricted wildcard "*"
    — must not be able to waive baseline approval for a platform-scoped
    subject (resource_kind="platform"). Platform actions aren't
    workspace-governed resources: no workspace policy, however permissive,
    can stand in for the actor's own confirmation. Mirrors
    test_policy_auto_approve_allows_high_risk above, which pins the
    OPPOSITE (correct) behavior for an ordinary (non-platform) subject —
    same auto_approve mechanism, this is the one carve-out."""
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=_policy(("allow", "social_post.publish")),
                        changed_by="t")
    d = await resolve_approval(
        db_session,
        subject=_subject(
            e, w, risk_level="high", resource_kind="platform", requires_approval=True,
        ),
        origin=_step_origin(),
    )
    assert d.outcome == "needs_human"
    assert await count_open_requests(db_session, workspace_id=w) == 1


# ── incident replays ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grant_makes_the_same_subject_pass__289_317(db_session):
    """The core fix: a human grant on a needs-human request lets the SAME step
    through on the next resolve. One approval, and it sticks — no loop."""
    e, w = await _ws(db_session)
    step = generate_ulid()
    subject = _subject(e, w, risk_level="high")

    d1 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d1.outcome == "needs_human"

    await grant_approval(db_session, d1.request, by_user_id="op", via="step_resume")

    d2 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d2.outcome == "allow"                      # approval stuck
    # resolve does NOT consume — the grant survives until the caller actually
    # proceeds (lease/tool run). A second resolve before proceeding still
    # allows: an approved-but-not-yet-dispatched step can never re-pause.
    assert d2.request.status == "granted"
    d2b = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d2b.outcome == "allow"

    # The gate consumes at the point of irreversible proceed…
    await consume_approval(db_session, d2.request)
    assert d2.request.status == "consumed"
    # The same operation cannot be executed again after consuming its grant.
    d3 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d3.outcome == "deny"
    # A new execution intent gets a new source identity and its own decision.
    d4 = await resolve_approval(db_session, subject=subject, origin=_step_origin())
    assert d4.outcome == "needs_human"


@pytest.mark.asyncio
async def test_approval_consumption_has_one_winner_under_concurrency(db_session):
    """Two stale GRANTED snapshots authorize at most one provider effect."""

    operator_id = generate_ulid()
    entity_id, workspace_id = await _ws(db_session, user_id=operator_id)
    decision = await resolve_approval(
        db_session,
        subject=_subject(entity_id, workspace_id, risk_level="high"),
        origin=_step_origin(),
    )
    await grant_approval(
        db_session,
        decision.request,
        by_user_id=operator_id,
        via="chat_card",
    )
    request_id = decision.request.id
    await db_session.commit()

    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    provider_effects: list[str] = []
    async with sessions() as first, sessions() as second:
        await first.execute(text("SET LOCAL lock_timeout = '2s'"))
        await second.execute(text("SET LOCAL lock_timeout = '2s'"))
        first_snapshot = await first.get(HitlRequest, request_id)
        second_snapshot = await second.get(HitlRequest, request_id)
        assert first_snapshot is not None and second_snapshot is not None
        assert first_snapshot.status == ApprovalStatus.GRANTED.value
        assert second_snapshot.status == ApprovalStatus.GRANTED.value

        async def claim_and_run_provider(session, request):
            outcome = await claim_approval_consumption(session, request)
            await session.commit()
            if outcome is ApprovalConsumeOutcome.CONSUMED:
                provider_effects.append(request.id)
            return outcome

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                claim_and_run_provider(first, first_snapshot),
                claim_and_run_provider(second, second_snapshot),
            ),
            timeout=5,
        )

    assert sorted(outcomes) == sorted((
        ApprovalConsumeOutcome.CONSUMED,
        ApprovalConsumeOutcome.ALREADY_CONSUMED,
    ))
    assert provider_effects == [request_id]
    assert await db_session.scalar(
        select(HitlRequest.status).where(HitlRequest.id == request_id)
    ) == ApprovalStatus.CONSUMED.value
    assert await db_session.scalar(
        select(func.count())
        .select_from(WorkspaceEvent)
        .where(
            WorkspaceEvent.entity_id == entity_id,
            WorkspaceEvent.event_type == ledger_et.APPROVAL_CONSUMED,
        )
    ) == 1

    # The compatibility wrapper still returns the request for a successful
    # first spend, but a replay is explicit and fail-closed rather than a
    # silent no-op that a caller could mistake for authorization.
    stale_snapshot = await db_session.get(HitlRequest, request_id)
    assert stale_snapshot is not None
    with pytest.raises(ApprovalConsumptionError) as exc_info:
        await consume_approval(db_session, stale_snapshot)
    assert exc_info.value.outcome is ApprovalConsumeOutcome.ALREADY_CONSUMED


@pytest.mark.asyncio
async def test_dedup_one_open_request_per_step(db_session):
    """Re-tripping the gate for the same step reuses the request — no duplicate
    cards, so the badge count is honest (the 3-vs-1 fix)."""
    e, w = await _ws(db_session)
    step = generate_ulid()
    subject = _subject(e, w, risk_level="high")

    d1 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    d2 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d1.request.id == d2.request.id
    assert await count_open_requests(db_session, workspace_id=w) == 1


@pytest.mark.asyncio
async def test_terminal_origin_expires_open_requests(db_session):
    """When the step reaches terminal, its open request is expired — no orphan
    'no longer attached to a waiting step' cards."""
    e, w = await _ws(db_session)
    step = generate_ulid()
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="high"), origin=_step_origin(step_id=step))
    assert d.outcome == "needs_human"
    closed = await resolve_origin_requests(db_session, step_id=step)
    assert closed == 1
    assert await count_open_requests(db_session, workspace_id=w) == 0


@pytest.mark.asyncio
async def test_retired_standing_grant_cannot_mutate_pending_or_policy(db_session):
    e, w = await _ws(db_session)
    subject = _subject(e, w, risk_level="high")
    decision = await resolve_approval(db_session, subject=subject, origin=_step_origin())
    before = await get_policy(db_session, w)
    with pytest.raises(ValueError, match="versioned"):
        await grant_approval(db_session, decision.request, by_user_id="op", via="always", standing=True)
    assert decision.request.status == "pending"
    assert await get_policy(db_session, w) == before
    assert await count_open_requests(db_session, workspace_id=w) == 1


@pytest.mark.asyncio
async def test_versioned_exact_rule_applies_to_future_operations_without_resolving_pending(db_session):
    from packages.core.governance.approval_policy import ApprovalScope, PolicyDraft
    from packages.core.governance.approval_policy_service import read_approval_settings, update_approval_policy

    e, w = await _ws(db_session)
    subject = _subject(e, w, risk_level="high")
    origin = _step_origin()
    pending = await resolve_approval(db_session, subject=subject, origin=origin)
    scope = ApprovalScope(kind="workspace", id=w, entity_id=e)
    actor = await db_session.get(User, "op")
    snapshot = await read_approval_settings(db_session, scope, actor)
    await update_approval_policy(db_session, scope, PolicyDraft(mode=None, user_rules=[{
        "effect": "allow", "selector": {"kind": "action", "action_key": "social_post.publish"},
    }]), snapshot["version"], actor)
    still_pending = await resolve_approval(db_session, subject=subject, origin=origin)
    assert still_pending.outcome == "needs_human"
    assert still_pending.request.id == pending.request.id
    future = await resolve_approval(db_session, subject=subject, origin=_step_origin())
    assert future.outcome == "allow"
    assert await count_open_requests(db_session, workspace_id=w) == 1


@pytest.mark.asyncio
async def test_standing_grant_cannot_override_hard_block(db_session):
    """A standing grant never beats never_allow — hard blocks stay hard."""
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=_policy(("deny", "social_post.publish"), ("allow", "social_post.publish")),
                        changed_by="t")
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="high"), origin=_step_origin())
    assert d.outcome == "deny"
