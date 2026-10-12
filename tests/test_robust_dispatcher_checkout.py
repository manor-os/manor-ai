"""Checkout-path robustness: skip stamping, head-of-line blocking, strict refs.

* D4 — a PENDING step the dispatcher keeps declining used to look identical to
  a step nobody has looked at. The skip is now stamped on ``step.error`` so
  the executor's watchdog can escalate a structurally undispatchable step and
  the UI can say why nothing is happening.
* D5 — the candidate window (``max_n * 4``, oldest first) filled up with
  dependent steps whose upstream was still running; a newer plan's root step
  never entered the window and the whole entity looked idle.
* D6 — a consumer whose params feed an external publish must never receive a
  degraded ref value (sibling field / JSON dump of the producer result).
* D7 — one predicate for "may this agent run provider.action_key" shared with
  the planner.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.constants.execution import (
    DISPATCH_SKIP_KEY,
    DISPATCH_SKIPPED_ERROR_TYPE,
    DispatchSkipReason,
)
from packages.core.dispatcher.service import (
    Dispatcher,
    _agent_allows_action_step,
    _stamp_dispatch_skip,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import Agent, AgentSubscription, Workspace


SERVICE_KEY = "writer"


@pytest.fixture(autouse=True)
def _no_blueprint_setup_gate(monkeypatch):
    monkeypatch.setattr(
        "packages.core.services.workspace_readiness."
        "evaluate_current_workspace_blocking_setup",
        AsyncMock(return_value=None),
    )


async def _seed_entity(
    db,
    *,
    with_subscription: bool = True,
    bind_worker: bool = True,
    worker_capabilities: dict | None = None,
    workspace_budget: tuple[Decimal, Decimal] | None = None,
) -> dict:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    worker = Worker(
        id=generate_ulid(), entity_id=entity_id, kind="internal",
        display_name="Internal worker",
        capabilities=worker_capabilities or {"supported_kinds": ["llm"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True, status="active",
    )
    workspace = Workspace(
        id=workspace_id, entity_id=entity_id, name="Robust WS", status="active",
    )
    if workspace_budget is not None:
        workspace.monthly_budget_usd, workspace.monthly_spent_usd = workspace_budget
        workspace.auto_pause_on_budget = True
    db.add_all([worker, workspace])
    if with_subscription:
        db.add(Agent(id=agent_id, entity_id=entity_id, name="Writer", slug="writer", status="active"))
        db.add(
            AgentSubscription(
                id=subscription_id, entity_id=entity_id, agent_id=agent_id,
                workspace_id=workspace_id, name="Writer", service_key=SERVICE_KEY,
                status="active",
            )
        )
        if bind_worker:
            db.add(
                SubscriptionWorker(
                    subscription_id=subscription_id, worker_id=worker.id,
                    priority=100, is_preferred=True,
                )
            )
    await db.flush()
    from tests.approval_settings_test_utils import (
        action_rule, initialize_test_account_approval, initialize_test_approval_owners,
    )

    actor = await initialize_test_account_approval(db, entity_id=entity_id,
        user_rules=[action_rule("unclassified.action")])
    await initialize_test_approval_owners(db, workspace, user_id=actor.id)
    return {
        "entity_id": entity_id, "workspace_id": workspace_id, "worker": worker,
        "agent_id": agent_id, "subscription_id": subscription_id,
    }


def _plan(seeded: dict, *, plan_dag: dict | None = None) -> ExecutionPlan:
    return ExecutionPlan(
        id=generate_ulid(), entity_id=seeded["entity_id"],
        workspace_id=seeded["workspace_id"], status="running",
        execution_mode="live", approval_required=False,
        plan_dag=plan_dag or {"steps": []},
    )


def _step(
    seeded: dict, plan: ExecutionPlan, *, key: str = "write", status: str = "pending",
    depends_on: list[str] | None = None, params: dict | None = None,
    error: dict | None = None, created_at: datetime | None = None,
    result: dict | None = None, kind: str = "llm",
) -> ExecutionStep:
    return ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=seeded["entity_id"],
        workspace_id=seeded["workspace_id"], service_key=SERVICE_KEY,
        step_key=key, kind=kind, params=params or {"prompt": "write"},
        depends_on=depends_on or [], step_status=status, risk_level="low",
        attempt_count=0, max_attempts=3, error=error, result=result,
        **({"created_at": created_at} if created_at else {}),
    )


# ── D4: skip stamping (pure helper) ──────────────────────────────────


def test_stamp_creates_a_dispatch_skipped_error_when_there_is_none():
    step = SimpleNamespace(error=None)
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

    _stamp_dispatch_skip(
        step, DispatchSkipReason.WORKER_NOT_BOUND, "no worker bound",
        structural=True, now=now,
    )

    assert step.error == {
        "type": DISPATCH_SKIPPED_ERROR_TYPE,
        "message": "no worker bound",
        DISPATCH_SKIP_KEY: {
            "reason": "worker_not_bound",
            "message": "no worker bound",
            "since": now.isoformat(),
            "structural": True,
        },
    }


def test_stamp_preserves_since_for_the_same_reason_and_resets_it_on_change():
    step = SimpleNamespace(error=None)
    first = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    later = first + timedelta(minutes=5)

    _stamp_dispatch_skip(step, DispatchSkipReason.WORKER_NOT_BOUND, "a", structural=True, now=first)
    _stamp_dispatch_skip(step, DispatchSkipReason.WORKER_NOT_BOUND, "b", structural=True, now=later)
    assert step.error[DISPATCH_SKIP_KEY]["since"] == first.isoformat()
    assert step.error[DISPATCH_SKIP_KEY]["message"] == "b"

    _stamp_dispatch_skip(step, DispatchSkipReason.WORKSPACE_BUDGET, "c", structural=False, now=later)
    assert step.error[DISPATCH_SKIP_KEY] == {
        "reason": "workspace_budget", "message": "c",
        "since": later.isoformat(), "structural": False,
    }
    assert step.error["type"] == DISPATCH_SKIPPED_ERROR_TYPE


def test_stamp_merges_into_a_copy_of_a_real_prior_error():
    prior = {"type": "ProviderError", "message": "boom", "next_retry_at": "2026-01-01T00:00:00+00:00"}
    step = SimpleNamespace(error=prior)
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

    _stamp_dispatch_skip(
        step, DispatchSkipReason.SUBSCRIPTION_UNRESOLVED, "no sub",
        structural=True, now=now,
    )

    assert step.error is not prior, "JSONB change detection needs a new object"
    assert DISPATCH_SKIP_KEY not in prior
    assert step.error["type"] == "ProviderError"
    assert step.error["message"] == "boom"
    assert step.error["next_retry_at"] == prior["next_retry_at"]
    assert step.error[DISPATCH_SKIP_KEY]["reason"] == "subscription_unresolved"


# ── D4: skip stamping through checkout ───────────────────────────────


@pytest.mark.asyncio
async def test_checkout_stamps_subscription_unresolved(db_session):
    seeded = await _seed_entity(db_session, with_subscription=False)
    plan = _plan(seeded)
    step = _step(seeded, plan)
    db_session.add_all([plan, step])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []

    assert step.step_status == "pending"
    assert step.error["type"] == DISPATCH_SKIPPED_ERROR_TYPE
    skip = step.error[DISPATCH_SKIP_KEY]
    assert skip["reason"] == DispatchSkipReason.SUBSCRIPTION_UNRESOLVED.value
    assert skip["structural"] is True
    assert SERVICE_KEY in skip["message"]
    first_since = skip["since"]

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []
    assert step.error[DISPATCH_SKIP_KEY]["since"] == first_since


@pytest.mark.asyncio
async def test_checkout_stamps_worker_not_bound(db_session):
    seeded = await _seed_entity(db_session, bind_worker=False)
    plan = _plan(seeded)
    step = _step(seeded, plan)
    db_session.add_all([plan, step])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []

    skip = step.error[DISPATCH_SKIP_KEY]
    assert skip["reason"] == DispatchSkipReason.WORKER_NOT_BOUND.value
    assert skip["structural"] is True


@pytest.mark.asyncio
async def test_checkout_stamps_workspace_budget_as_soft(db_session):
    seeded = await _seed_entity(
        db_session, workspace_budget=(Decimal("1"), Decimal("5")),
    )
    plan = _plan(seeded)
    step = _step(seeded, plan)
    db_session.add_all([plan, step])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []

    skip = step.error[DISPATCH_SKIP_KEY]
    assert skip["reason"] == DispatchSkipReason.WORKSPACE_BUDGET.value
    assert skip["structural"] is False


@pytest.mark.asyncio
async def test_checkout_stamps_capability_unsupported(db_session):
    seeded = await _seed_entity(
        db_session,
        worker_capabilities={
            "supported_kinds": ["llm"], "max_risk_level": "high",
            "supported_capabilities": ["browser.control"],
        },
    )
    plan = _plan(seeded)
    step = _step(seeded, plan)
    db_session.add_all([plan, step])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []

    skip = step.error[DISPATCH_SKIP_KEY]
    assert skip["reason"] == DispatchSkipReason.CAPABILITY_UNSUPPORTED.value
    assert skip["structural"] is True


@pytest.mark.asyncio
async def test_checkout_merges_skip_into_a_real_prior_error(db_session):
    seeded = await _seed_entity(db_session, bind_worker=False)
    plan = _plan(seeded)
    step = _step(seeded, plan, error={"type": "ProviderError", "message": "boom"})
    db_session.add_all([plan, step])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []

    assert step.error["type"] == "ProviderError"
    assert step.error["message"] == "boom"
    assert step.error[DISPATCH_SKIP_KEY]["reason"] == DispatchSkipReason.WORKER_NOT_BOUND.value


@pytest.mark.asyncio
async def test_retry_backoff_skip_is_not_stamped(db_session):
    seeded = await _seed_entity(db_session)
    plan = _plan(seeded)
    backoff = {
        "type": "ProviderError", "message": "boom",
        "next_retry_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    step = _step(seeded, plan, error=dict(backoff))
    db_session.add_all([plan, step])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []

    assert step.error == backoff


@pytest.mark.asyncio
async def test_lease_grant_clears_a_dispatch_skipped_error(db_session):
    seeded = await _seed_entity(db_session)
    plan = _plan(seeded)
    step = _step(
        seeded, plan,
        error={
            "type": DISPATCH_SKIPPED_ERROR_TYPE, "message": "waiting",
            DISPATCH_SKIP_KEY: {
                "reason": "awaiting_worker", "message": "waiting",
                "since": datetime.now(timezone.utc).isoformat(), "structural": False,
            },
        },
    )
    db_session.add_all([plan, step])
    await db_session.flush()

    leases = await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1)

    assert len(leases) == 1
    assert step.step_status == "running"
    assert step.error is None


@pytest.mark.asyncio
async def test_lease_grant_strips_the_skip_key_but_keeps_the_real_error(db_session):
    seeded = await _seed_entity(db_session)
    plan = _plan(seeded)
    step = _step(
        seeded, plan,
        error={
            "type": "ProviderError", "message": "boom",
            DISPATCH_SKIP_KEY: {
                "reason": "worker_not_bound", "message": "x",
                "since": datetime.now(timezone.utc).isoformat(), "structural": True,
            },
        },
    )
    db_session.add_all([plan, step])
    await db_session.flush()

    leases = await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1)

    assert len(leases) == 1
    assert step.error == {"type": "ProviderError", "message": "boom"}


# ── D5: head-of-line blocking ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dependent_steps_waiting_on_upstream_do_not_starve_a_newer_root_step(db_session):
    seeded = await _seed_entity(db_session)
    old_plan = _plan(seeded)
    new_plan = _plan(seeded)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    upstream = _step(seeded, old_plan, key="research", status="running", created_at=long_ago)
    upstream.attempt_count = 1
    # More waiting dependents than the max_n * 4 candidate window holds.
    dependents = [
        _step(
            seeded, old_plan, key=f"write_{i}", depends_on=["research"],
            created_at=long_ago + timedelta(seconds=i),
        )
        for i in range(6)
    ]
    root = _step(seeded, new_plan, key="outline")
    db_session.add_all([old_plan, new_plan, upstream, *dependents, root])
    await db_session.flush()

    leases = await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1)

    assert [leased.id for _lease, leased in leases] == [root.id]
    assert all(dep.step_status == "pending" for dep in dependents)


@pytest.mark.asyncio
async def test_dependency_exclusion_still_leases_a_step_whose_deps_are_done(db_session):
    seeded = await _seed_entity(db_session)
    plan = _plan(seeded)
    upstream = _step(seeded, plan, key="research", status="done", result={"text": "facts"})
    consumer = _step(seeded, plan, key="write", depends_on=["research"])
    db_session.add_all([plan, upstream, consumer])
    await db_session.flush()

    leases = await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1)

    assert [leased.id for _lease, leased in leases] == [consumer.id]


@pytest.mark.asyncio
async def test_dependency_exclusion_keeps_the_failed_upstream_skip_path(db_session):
    """A dependent of a FAILED step is excluded by SQL too; the Python check is
    the authoritative second pass, so nothing about the step changes until the
    executor's cycle handles the failure."""
    seeded = await _seed_entity(db_session)
    plan = _plan(seeded)
    upstream = _step(seeded, plan, key="research", status="failed")
    consumer = _step(seeded, plan, key="write", depends_on=["research"])
    db_session.add_all([plan, upstream, consumer])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []
    assert consumer.step_status == "pending"


# ── D6: strict refs for publish consumers ────────────────────────────


def _publish_dag(*, expects: list[str] | None) -> dict:
    post = {"key": "post", "kind": "llm", "service_key": SERVICE_KEY, "depends_on": ["draft"]}
    if expects:
        post["expects"] = expects
    return {
        "steps": [
            {"key": "draft", "kind": "llm", "service_key": SERVICE_KEY, "depends_on": []},
            post,
        ]
    }


@pytest.mark.asyncio
async def test_publish_consumer_fails_instead_of_receiving_a_degraded_ref(db_session):
    seeded = await _seed_entity(db_session)
    plan = _plan(seeded, plan_dag=_publish_dag(expects=["publish"]))
    draft = _step(seeded, plan, key="draft", status="done", result={"text": "hello"})
    post = _step(
        seeded, plan, key="post", depends_on=["draft"],
        params={"prompt": "Post: ${{ steps.draft.result.tweet_text }}"},
    )
    db_session.add_all([plan, draft, post])
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1) == []

    assert post.step_status == "failed"
    assert post.error["type"] == "ReferenceError"
    assert "tweet_text" in post.error["message"]


@pytest.mark.asyncio
async def test_ordinary_consumer_still_gets_the_degraded_ref(db_session):
    seeded = await _seed_entity(db_session)
    plan = _plan(seeded, plan_dag=_publish_dag(expects=None))
    draft = _step(seeded, plan, key="draft", status="done", result={"text": "hello"})
    post = _step(
        seeded, plan, key="post", depends_on=["draft"],
        params={"prompt": "Post: ${{ steps.draft.result.tweet_text }}"},
    )
    db_session.add_all([plan, draft, post])
    await db_session.flush()

    leases = await Dispatcher().checkout_steps_for_worker(db_session, seeded["worker"], max_n=1)

    assert [leased.id for _lease, leased in leases] == [post.id]
    assert post.params == {"prompt": "Post: hello"}


# ── D7: one action-binding predicate ─────────────────────────────────


@pytest.mark.asyncio
async def test_agent_allows_action_step_delegates_to_the_shared_predicate(db_session, monkeypatch):
    seen: dict = {}

    async def _fake(db, *, agent_id, provider, action_key):
        seen.update({"agent_id": agent_id, "provider": provider, "action_key": action_key})
        return False, {"type": "ActionBindingDenied", "message": "shared says no"}

    monkeypatch.setattr(
        "packages.core.services.agent_action_binding.agent_action_binding_allowed", _fake,
    )

    allowed, error = await _agent_allows_action_step(
        db_session, agent_id="agent_1", provider="gmail", action_key="send_email",
    )

    assert (allowed, error) == (False, {"type": "ActionBindingDenied", "message": "shared says no"})
    assert seen == {"agent_id": "agent_1", "provider": "gmail", "action_key": "send_email"}


@pytest.mark.asyncio
async def test_agent_allows_action_step_matches_the_shared_predicate_verdict(db_session):
    from packages.core.services.agent_action_binding import agent_action_binding_allowed

    kwargs = {"agent_id": None, "provider": "gmail", "action_key": "send_email"}
    assert await _agent_allows_action_step(db_session, **kwargs) == (
        await agent_action_binding_allowed(db_session, **kwargs)
    )
