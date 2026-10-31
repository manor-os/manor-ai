"""Dispatcher lease clocks: queued-lease grace and expiry of an evidenced lease.

An expired lease whose step captured an external effect cannot be replayed
automatically, so its step fails; a lease that is still queued gets the queue grace before its
liveness TTL starts counting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.worker import WorkLease, Worker


# ── fixtures / helpers ────────────────────────────────────────────────


async def _seed_lease(
    db,
    *,
    params: dict | None = None,
    lease_seconds: float = 300,
    status: str = "active",
) -> dict:
    """One active lease on a running step, ready to be executed in-process."""
    entity_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    now = datetime.now(timezone.utc)

    db.add_all([
        Worker(
            id=worker_id, entity_id=entity_id, kind="internal",
            display_name="Internal worker",
            capabilities={"supported_kinds": ["llm"], "max_risk_level": "high"},
            monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True,
            status="active",
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, status="running",
            execution_mode="live", approval_required=False, plan_dag={"steps": []},
        ),
        ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=entity_id,
            step_key="blocking_step", kind="llm",
            params=params or {}, depends_on=[], step_status="running",
            risk_level="low", attempt_count=1, max_attempts=3,
            current_lease_id=lease_id, started_at=now,
        ),
        WorkLease(
            id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
            worker_id=worker_id, status=status,
            lease_until=now + timedelta(seconds=lease_seconds),
        ),
    ])
    await db.commit()
    return {
        "entity_id": entity_id, "plan_id": plan_id, "step_id": step_id,
        "lease_id": lease_id, "worker_id": worker_id,
        "lease_until": now + timedelta(seconds=lease_seconds),
    }


async def _reload(db, seeded) -> tuple[ExecutionStep, WorkLease]:
    db.expire_all()
    step = await db.get(ExecutionStep, seeded["step_id"])
    lease = await db.get(WorkLease, seeded["lease_id"])
    return step, lease


# ── the regression that matters ───────────────────────────────────────

@pytest.mark.parametrize("evidence_owner", ["step", "lease"])
@pytest.mark.asyncio
async def test_expired_lease_with_captured_effect_fails_without_replay(
    db_session, evidence_owner,
):
    """Expiry cannot prove an evidenced external action is safe to replay.

    Nothing can settle that question for a worker job, so the Step fails
    (no replay, no unresolvable pause) and says what to check.
    """
    seeded = await _seed_lease(db_session, lease_seconds=-1)
    step, lease = await _reload(db_session, seeded)
    receipt = {
        "kind": "tool_effect",
        "effect": "publish",
        "fields": {
            "provider_id": "post-123",
            "delivery_confirmed": True,
        },
    }
    if evidence_owner == "step":
        step.evidence_refs = [receipt]
    else:
        lease.evidence_refs = [receipt]
    checkpoint = {
        "version": 1,
        "messages": [{"role": "tool", "content": "published post-123"}],
    }
    step.runtime_checkpoint = checkpoint
    await db_session.commit()

    # The sweep is global; other tests' expired leases may share the DB.
    assert await Dispatcher().expire_leases(db_session) >= 1
    await db_session.commit()

    step, lease = await _reload(db_session, seeded)
    assert lease.status == "expired"
    assert lease.error == step.error
    assert step.step_status == "failed"
    assert step.attempt_count < step.max_attempts, (
        "captured effects must suppress automatic retry even with attempts remaining"
    )
    assert step.current_lease_id is None
    assert step.finished_at is not None
    assert step.human_input_prompt is None
    assert step.runtime_checkpoint == checkpoint
    assert step.evidence_refs == [receipt]
    if evidence_owner == "lease":
        assert lease.evidence_refs == [receipt]
    assert step.error["type"] == "ExternalEffectUnconfirmed"
    assert step.error["cause"]["type"] == "lease_expired"
    assert step.error["failure"]["retryable"] is False
    assert step.error["failure"]["requires_human"] is True
    assert "next_retry_at" not in step.error
    assert "may or may not have happened" in step.error["message"]
    # No card or approval row is minted: nothing could resolve it.
    requests = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == step.id)
    )).scalars().all()
    assert requests == []


@pytest.mark.asyncio
async def test_checkout_gives_queued_leases_strategist_queue_grace(db_session):
    """A never-heartbeated lease must survive the queue wait before its worker starts.

    The old grant clock was 300s from checkout, so a queued lease with
    heartbeat_count=0 was expired as dead before the worker started. TTL is
    liveness after execution starts.
    """
    from packages.core.dispatcher.service import (
        Dispatcher,
        _DEFAULT_LEASE_TTL,
        _QUEUED_LEASE_GRACE,
    )

    entity_id = generate_ulid()
    from tests.approval_settings_test_utils import action_rule, initialize_test_account_approval

    await initialize_test_account_approval(db_session, entity_id=entity_id,
        user_rules=[action_rule("unclassified.action")])
    plan_id = generate_ulid()
    step_id = generate_ulid()
    worker_id = generate_ulid()
    granted_after = datetime.now(timezone.utc)

    db_session.add_all([
        Worker(
            id=worker_id, entity_id=entity_id, kind="internal",
            display_name="Internal worker",
            capabilities={"supported_kinds": ["llm"], "max_risk_level": "high"},
            monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True,
            status="active",
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, status="running",
            execution_mode="live", approval_required=False, plan_dag={"steps": []},
        ),
        ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=entity_id,
            step_key="draft", kind="llm",
            params={}, depends_on=[], step_status="pending",
            risk_level="low", attempt_count=0, max_attempts=3,
        ),
    ])
    await db_session.commit()

    dispatcher = Dispatcher()
    worker = await db_session.get(Worker, worker_id)
    leased = await dispatcher.checkout_steps_for_worker(
        db_session, worker, max_n=1, plan_id=plan_id,
    )
    await db_session.commit()
    assert len(leased) == 1
    lease, _step = leased[0]
    assert (lease.heartbeat_count or 0) == 0
    assert lease.last_heartbeat_at is None
    assert lease.lease_until >= granted_after + _QUEUED_LEASE_GRACE
    assert lease.lease_until <= (
        datetime.now(timezone.utc) + _QUEUED_LEASE_GRACE + _DEFAULT_LEASE_TTL
        + timedelta(seconds=2)
    )

    assert await dispatcher.expire_leases(
        db_session, now=datetime.now(timezone.utc) + timedelta(seconds=400),
    ) == 0
    await db_session.refresh(lease)
    assert lease.status == "active"

    assert await dispatcher.expire_leases(
        db_session, now=lease.lease_until + timedelta(seconds=1),
    ) == 1
