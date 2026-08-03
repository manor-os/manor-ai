"""Phase 3 Task A — the read surfaces tell "要许可" from "要信息".

Every surface that shows open ``HitlRequest`` rows used to select on
``status == pending`` and nothing else. So a CAPTCHA wall or a "your local
worker is offline" error aged in an operator's *approval* queue, counted as
governance friction in two consolidators, and reached the strategist LLM as
an approval it could reason about. ``hitl_type`` had existed for a phase and
no reader looked at it.

These tests pin, per surface, that an ``authorize`` row and an ``error`` row
now land in different places:

* ``/human-queue``            → ``approvals`` vs ``information_requests``
* strategist briefing         → listed vs counted-and-named
* human_participation         → ``approval_bottleneck`` vs
                                ``information_request_stalled``
* risk_governance             → counted vs excluded (and the exclusion said)

plus the anti-drift guards on the one object they all share
(``GOVERNANCE_HITL_TYPES`` / ``is_governance_hitl`` / its SQL twin), and the
mint-side fix without which the discriminator would be a lie: path-C lease
pauses were being *stored* as ``authorize``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.consolidators import REGISTRY, SnapshotContext
from packages.core.constants.approvals import (
    GOVERNANCE_HITL_TYPES,
    LEASE_KIND_HITL_TYPES,
    NON_GOVERNANCE_HITL_TYPES,
    HitlType,
    is_governance_hitl,
)
from packages.core.models.hitl_request import (
    HitlRequest,
    governance_hitl_clause,
)
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.review import begin_review, events_in_window
from packages.core.review.briefing import ReviewBriefingModel, build_briefing
from packages.core.review.briefing_render import render_briefing_markdown

ENTITY_ID = "01HITLREADSURFACE000000000"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _workspace(db, *, entity_id: str = ENTITY_ID) -> Workspace:
    workspace = Workspace(entity_id=entity_id, name="HITL read WS")
    db.add(workspace)
    await db.flush()
    return workspace


def _request(
    workspace_id: str,
    *,
    entity_id: str = ENTITY_ID,
    hitl_type: str,
    action_key: str,
    age_hours: float = 0.0,
    **overrides,
) -> HitlRequest:
    kwargs = dict(
        entity_id=entity_id,
        workspace_id=workspace_id,
        action_key=action_key,
        origin_kind="step",
        risk_level="medium",
        status="pending",
        hitl_type=hitl_type,
        reason=f"{hitl_type} for {action_key}",
        dedup_key=f"dk_{hitl_type}_{action_key}_{generate_ulid()}",
        created_at=_utcnow() - timedelta(hours=age_hours),
    )
    kwargs.update(overrides)
    return HitlRequest(**kwargs)


async def _ctx(db, review) -> SnapshotContext:
    workspace = await db.get(Workspace, review.workspace_id)
    return SnapshotContext(
        review=review, workspace=workspace,
        events=await events_in_window(db, review),
    )


# ── the discriminator itself ───────────────────────────────────────────

def test_governance_types_partition_the_vocabulary():
    """Every HitlType is on exactly one side. A type added without a
    decision would show up here as a gap, not as silent governance."""
    assert GOVERNANCE_HITL_TYPES | NON_GOVERNANCE_HITL_TYPES == set(HitlType.values())
    assert not (GOVERNANCE_HITL_TYPES & NON_GOVERNANCE_HITL_TYPES)
    assert GOVERNANCE_HITL_TYPES == {"authorize", "review"}


def test_is_governance_hitl_answers_per_type():
    assert is_governance_hitl(HitlType.AUTHORIZE.value) is True
    assert is_governance_hitl(HitlType.REVIEW.value) is True
    assert is_governance_hitl(HitlType.ERROR.value) is False
    assert is_governance_hitl(HitlType.INPUT.value) is False
    assert is_governance_hitl(HitlType.CHOICE.value) is False
    # Unknown / missing → governance: the safe failure is the pre-existing
    # behavior (shown, counted), never hiding work from an operator.
    assert is_governance_hitl(None) is True
    assert is_governance_hitl("some_future_type") is True


@pytest.mark.asyncio
async def test_sql_twin_agrees_with_python_for_every_type(db_session):
    """The Python helper and the SQL clause are two spellings of one rule;
    this is the guard that keeps the second from drifting."""
    workspace = await _workspace(db_session)
    rows = {
        value: _request(workspace.id, hitl_type=value, action_key=f"a.{value}")
        for value in HitlType.values()
    }
    db_session.add_all(rows.values())
    await db_session.flush()

    selected = {
        row.hitl_type for row in (await db_session.execute(
            select(HitlRequest).where(
                HitlRequest.workspace_id == workspace.id,
                governance_hitl_clause(),
            )
        )).scalars().all()
    }
    assert selected == {
        value for value in HitlType.values() if is_governance_hitl(value)
    }


def test_lease_kind_hitl_types_covers_exactly_the_mintable_kinds():
    """Keys must equal the set lease_needs_human can mint — otherwise a kind
    silently falls back to the record layer's ``authorize`` default, which is
    the bug this map exists to fix."""
    from packages.core.ai.pending_action import LEASE_HITL_CLOSEABLE_KINDS

    assert set(LEASE_KIND_HITL_TYPES) == set(LEASE_HITL_CLOSEABLE_KINDS)
    assert set(LEASE_KIND_HITL_TYPES.values()) <= set(HitlType.values())
    # needs_confirmation really is a permission ask and stays governance.
    assert LEASE_KIND_HITL_TYPES["needs_confirmation"] == HitlType.AUTHORIZE.value
    assert LEASE_KIND_HITL_TYPES["needs_login"] == HitlType.INPUT.value


# ── surface 1: /human-queue ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_human_queue_splits_approvals_from_information_requests(
    client: AsyncClient,
):
    import packages.core.database as db_module

    resp = await register_user_and_get_token(client, json={
        "username": "hitl_queue_owner",
        "email": "hitl_queue_owner@test.com",
        "password": "pass123",
        "entity_name": "HITL Queue Corp",
    })
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    entity_id = (await client.get("/api/v1/auth/me", headers=headers)).json()["entity_id"]

    ws_resp = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "HITL Queue WS"},
    )
    workspace_id = ws_resp.json()["id"]

    async with db_module.async_session() as db:
        db.add_all([
            _request(
                workspace_id, entity_id=entity_id,
                hitl_type=HitlType.AUTHORIZE.value, action_key="external.publish",
                matched_rule="social_post.publish",
            ),
            _request(
                workspace_id, entity_id=entity_id,
                hitl_type=HitlType.ERROR.value, action_key="chrome.fill",
                matched_rule="step.high_risk",
            ),
        ])
        await db.commit()

    body = (await client.get(
        f"/api/v1/workspaces/{workspace_id}/human-queue", headers=headers,
    )).json()

    assert [a["action_key"] for a in body["approvals"]] == ["external.publish"]
    assert [r["action_key"] for r in body["information_requests"]] == ["chrome.fill"]
    # Neither list is a dumping ground: each row says what it is, and the
    # rule that paused it is now on the wire too.
    assert body["approvals"][0]["hitl_type"] == "authorize"
    assert body["approvals"][0]["matched_rule"] == "social_post.publish"
    assert body["information_requests"][0]["hitl_type"] == "error"


# ── surface 2: strategist briefing ─────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_lists_approvals_and_only_counts_the_rest(db_session):
    """The LLM must not be told "2 approvals aging" when one is a broken
    worker. It is told about the approval, and told a number for the rest."""
    workspace = await _workspace(db_session)
    db_session.add_all([
        _request(
            workspace.id, hitl_type=HitlType.AUTHORIZE.value,
            action_key="external.publish", age_hours=30,
        ),
        _request(
            workspace.id, hitl_type=HitlType.ERROR.value,
            action_key="worker.offline", age_hours=40,
        ),
        _request(
            workspace.id, hitl_type=HitlType.INPUT.value,
            action_key="chrome.login", age_hours=5,
        ),
    ])
    await db_session.flush()

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    briefing = await build_briefing(db_session, review, [])
    assert isinstance(briefing, ReviewBriefingModel)

    assert [a["action_key"] for a in briefing.open_approvals] == ["external.publish"]
    assert briefing.open_approvals[0]["hitl_type"] == "authorize"
    assert briefing.non_governance_hitl_open == 2

    # What the model actually reads: one approval line, and the withheld
    # rows acknowledged rather than silently dropped.
    text = render_briefing_markdown(briefing)
    approvals_block = text.split("## Open approvals", 1)[1]
    assert "worker.offline" not in approvals_block
    assert "2 other request(s) are waiting" in approvals_block


@pytest.mark.asyncio
async def test_briefing_governance_filter_runs_before_the_limit(db_session):
    """MAX_OPEN_APPROVALS is a budget for approvals, not for rows. Filtering
    after the LIMIT would return a short list while approvals were waiting."""
    from packages.core.review.briefing import MAX_OPEN_APPROVALS

    workspace = await _workspace(db_session)
    # Non-governance rows are the OLDEST, so a LIMIT-then-filter order would
    # spend the whole budget on them.
    db_session.add_all([
        _request(
            workspace.id, hitl_type=HitlType.ERROR.value,
            action_key=f"broken.{i}", age_hours=100 + i,
        )
        for i in range(MAX_OPEN_APPROVALS)
    ] + [
        _request(
            workspace.id, hitl_type=HitlType.AUTHORIZE.value,
            action_key=f"publish.{i}", age_hours=10 + i,
        )
        for i in range(MAX_OPEN_APPROVALS)
    ])
    await db_session.flush()

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    briefing = await build_briefing(db_session, review, [])

    assert len(briefing.open_approvals) == MAX_OPEN_APPROVALS
    assert all(a["hitl_type"] == "authorize" for a in briefing.open_approvals)
    assert briefing.non_governance_hitl_open == MAX_OPEN_APPROVALS


# ── surface 3: human_participation consolidator ────────────────────────

@pytest.mark.asyncio
async def test_human_participation_separates_bottleneck_from_stall(db_session):
    """Both waits are real; only one of them is governance friction."""
    workspace = await _workspace(db_session)
    stale_approval = _request(
        workspace.id, hitl_type=HitlType.AUTHORIZE.value,
        action_key="external.publish", age_hours=48,
    )
    stale_error = _request(
        workspace.id, hitl_type=HitlType.ERROR.value,
        action_key="worker.offline", age_hours=48,
    )
    fresh_error = _request(
        workspace.id, hitl_type=HitlType.ERROR.value,
        action_key="worker.flaky", age_hours=1,
    )
    db_session.add_all([stale_approval, stale_error, fresh_error])
    await db_session.flush()

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["human_participation"].run(
        db_session, await _ctx(db_session, review),
    )

    bottlenecks = [o for o in report.observations if o.type == "approval_bottleneck"]
    assert [o.evidence_refs for o in bottlenecks] == [
        [f"approval_request:{stale_approval.id}"]
    ]
    stalls = [
        o for o in report.observations if o.type == "information_request_stalled"
    ]
    assert [o.evidence_refs for o in stalls] == [
        [f"approval_request:{stale_error.id}"]
    ]
    assert "not to approve anything" in stalls[0].description

    # The counts say the same thing the observations do.
    assert report.metrics["open_approvals"] == 1
    assert report.metrics["open_information_requests"] == 2
    assert "1 open approval(s)" in report.summary
    assert "2 open information request(s)" in report.summary
    # Nothing was dropped on the way — coverage still sees all three rows.
    assert report.coverage.records_examined >= 3


# ── surface 4: risk_governance consolidator ────────────────────────────

@pytest.mark.asyncio
async def test_risk_governance_excludes_non_governance_and_says_so(db_session):
    workspace = await _workspace(db_session)
    db_session.add_all([
        _request(
            workspace.id, hitl_type=HitlType.AUTHORIZE.value,
            action_key="external.publish", age_hours=30,
        ),
        _request(
            workspace.id, hitl_type=HitlType.ERROR.value,
            action_key="worker.offline", age_hours=90,
        ),
    ])
    await db_session.flush()

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["risk_governance"].run(
        db_session, await _ctx(db_session, review),
    )

    assert report.metrics["open_approvals"] == 1
    assert report.metrics["non_governance_hitl_open"] == 1
    # The oldest row in the table is the error at 90h. If it leaked into the
    # governance ages, this would read ~90 instead of ~30.
    assert report.metrics["oldest_open_approval_age_hours"] == pytest.approx(
        30.0, abs=1.0,
    )
    assert "1 open approval(s)" in report.summary
    assert "1 non-governance HITL request(s) excluded" in report.summary
