"""The unified approval core: gate-matrix parity + this week's incidents.

resolve_approval() is the single decision every gate will delegate to. These
tests pin the security-critical behavior BEFORE any gate is rewired onto it:

  * hard blocks stay hard and mint no request;
  * high-risk / requires_approval / policy-HITL need a human (one request);
  * a granted request lets the SAME subject through (approval sticks — the fix
    for the #289 / #317 loops), cross-plane by construction;
  * one open request per subject (dedup — the fix for badge inflation);
  * a terminal origin expires its open requests (the orphan-card fix);
  * a standing grant ("Always") is honored uniformly and never overrides a hard
    block.
"""
import pytest

from packages.core.constants.approvals import AuthorizeScope
from packages.core.governance import WorkspacePolicy, get_policy, update_policy
from packages.core.governance.approvals import (
    ApprovalOrigin,
    ApprovalSubject,
    consume_approval,
    count_open_requests,
    grant_approval,
    grant_open_request_for_step,
    resolve_approval,
    resolve_origin_requests,
)
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace


async def _ws(db):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Gov WS", operating_model={}))
    await db.flush()
    return entity_id, workspace_id


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
        db_session, subject=_subject(e, w, risk_level="low"), origin=_step_origin(),
    )
    assert d.outcome == "allow"


@pytest.mark.asyncio
async def test_hard_block_denies_and_mints_no_request(db_session):
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=WorkspacePolicy(never_allow_capabilities=["external.social"]),
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
                        policy=WorkspacePolicy(hitl_required_capabilities=["external.social"]),
                        changed_by="t")
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="medium"), origin=_step_origin())
    assert d.outcome == "needs_human"


@pytest.mark.asyncio
async def test_policy_auto_approve_allows_high_risk(db_session):
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=WorkspacePolicy(auto_approve_capabilities=["external.social"]),
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
                        policy=WorkspacePolicy(auto_approve_actions=["*"]),
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
    # …after which a THIRD resolve re-requests (the grant was one-time).
    d3 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d3.outcome == "needs_human"


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
async def test_standing_grant_is_unified_and_sticks(db_session):
    """'Always approve' = a standing grant written to the ONE store both planes
    honor (workspace policy auto-approve). Future subjects allow with no card.

    Subject is this module's default, ``social_post.publish`` — a publish-class
    capability. There is no tier that withholds "Always" from it: the user is
    the authority on what they want standing.
    """
    e, w = await _ws(db_session)
    subject = _subject(e, w, risk_level="high")
    d1 = await resolve_approval(db_session, subject=subject, origin=_step_origin())
    await grant_approval(db_session, d1.request, by_user_id="op", via="always", standing=True)
    # a DIFFERENT step, same subject capability → allowed with no new request
    d2 = await resolve_approval(db_session, subject=subject, origin=_step_origin())
    assert d2.outcome == "allow"
    assert await count_open_requests(db_session, workspace_id=w) == 0
    # The promotion is recorded ON the row: asked at action scope, answered at
    # tool scope. Without this the two buttons remain indistinguishable in the
    # audit trail.
    assert d1.request.payload["scope"] == AuthorizeScope.TOOL.value
    assert d1.request.payload["scope_promoted_from"] == AuthorizeScope.ACTION.value


@pytest.mark.asyncio
async def test_the_user_is_the_authority_on_what_becomes_standing(db_session):
    """"Always" on a publish-class capability produces a real standing grant.

    A tier that made publish/email/message approvable-once-but-never-blanket
    shipped and was rejected: "Always" means the user wants always, and the
    system does not get to second-guess the person who owns the account.
    ``never_allow`` remains the only hard block — and even that is theirs to
    edit through the governance API.

    Both consent-scope branches are exercised, because the withheld tier
    covered both: a subject WITH an action key writes the action, and a
    subject with only a capability writes the capability.
    """
    e, w = await _ws(db_session)

    # ── action-key subject: social_post.publish ──
    subject = _subject(e, w, risk_level="high")  # social_post.publish
    d1 = await resolve_approval(db_session, subject=subject, origin=_step_origin())
    assert d1.outcome == "needs_human"

    await grant_approval(
        db_session, d1.request, by_user_id="op", via="always", standing=True,
    )
    # Nothing was withheld and nothing was booked as withheld.
    assert d1.request.status == "granted"
    assert "standing_grant_refused" not in (d1.request.payload or {})
    assert d1.request.payload["scope"] == AuthorizeScope.TOOL.value
    assert d1.request.payload["scope_promoted_from"] == AuthorizeScope.ACTION.value

    policy = await get_policy(db_session, w)
    assert "social_post.publish" in policy.auto_approve_actions

    # ...so the next identical publish sails through with no new card.
    d2 = await resolve_approval(db_session, subject=subject, origin=_step_origin())
    assert d2.outcome == "allow"
    assert await count_open_requests(db_session, workspace_id=w) == 0

    # ── capability-only subject: external.email ──
    cap_subject = _subject(
        e, w, risk_level="high", action_key=None, capability_id="external.email",
    )
    d3 = await resolve_approval(db_session, subject=cap_subject, origin=_step_origin())
    assert d3.outcome == "needs_human"
    await grant_approval(
        db_session, d3.request, by_user_id="op", via="always", standing=True,
    )
    policy = await get_policy(db_session, w)
    assert "external.email" in policy.auto_approve_capabilities

    d4 = await resolve_approval(db_session, subject=cap_subject, origin=_step_origin())
    assert d4.outcome == "allow"


@pytest.mark.asyncio
async def test_operator_resume_grants_the_steps_open_request__317(db_session):
    """Task-detail Resume / plan Retry on an approval-paused step IS the
    approval: granting by step id lets the reparked step through the gate."""
    e, w = await _ws(db_session)
    step = generate_ulid()
    subject = _subject(e, w, risk_level="high")

    d1 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d1.outcome == "needs_human"

    granted = await grant_open_request_for_step(
        db_session, entity_id=e, step_id=step, by_user_id="op", via="task_resume",
    )
    assert granted is not None and granted.id == d1.request.id
    assert granted.status == "granted"

    d2 = await resolve_approval(db_session, subject=subject, origin=_step_origin(step_id=step))
    assert d2.outcome == "allow"

    # No open request → helper is a harmless no-op (plain failed-step retry).
    await consume_approval(db_session, granted)
    assert await grant_open_request_for_step(
        db_session, entity_id=e, step_id=generate_ulid(), by_user_id="op",
    ) is None


@pytest.mark.asyncio
async def test_standing_grant_cannot_override_hard_block(db_session):
    """A standing grant never beats never_allow — hard blocks stay hard."""
    e, w = await _ws(db_session)
    await update_policy(db_session, entity_id=e, workspace_id=w,
                        policy=WorkspacePolicy(
                            never_allow_capabilities=["external.social"],
                            auto_approve_capabilities=["external.social"],  # even if both set
                        ),
                        changed_by="t")
    d = await resolve_approval(db_session, subject=_subject(e, w, risk_level="high"), origin=_step_origin())
    assert d.outcome == "deny"
