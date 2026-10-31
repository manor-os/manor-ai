"""Lease lifecycle robustness: revoke, ownership, evidence + checkpoint on failure.

Three failure classes these tests pin:

* a cancelled step left its lease ACTIVE, so the worker kept running and
  its late ``complete_lease`` overwrote the cancelled step (D1);
* a step reset by a retry surface (or re-leased) was still writable through
  the OLD lease — last writer wins, the reset was silently undone (D2);
* a failed attempt that had already performed an external action was retried
  (no evidence reached ``fail_lease``), and a usable transcript was thrown
  away on retry (D3).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.constants.execution import (
    LEASE_REVOKED_ERROR_TYPE,
    LEASE_SUPERSEDED_ERROR_TYPE,
    WorkLeaseStatus,
)
from packages.core.dispatcher.service import Dispatcher, LeaseNotActive
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker, WorkerActivityLog


# ── helpers ───────────────────────────────────────────────────────────


async def _seed(
    db,
    *,
    lease_status: str = "active",
    step_status: str = "running",
    kind: str = "llm",
    params: dict | None = None,
    expected_output_schema: dict | None = None,
    attempt_count: int = 1,
    max_attempts: int = 3,
    plan_id: str | None = None,
    entity_id: str | None = None,
    worker_id: str | None = None,
    step_key: str = "draft",
) -> dict:
    """One lease on one step. Reuses plan/entity/worker when given."""
    entity_id = entity_id or generate_ulid()
    new_plan = plan_id is None
    plan_id = plan_id or generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    now = datetime.now(timezone.utc)
    rows: list = []
    if worker_id is None:
        worker_id = generate_ulid()
        rows.append(
            Worker(
                id=worker_id, entity_id=entity_id, kind="internal",
                display_name="Internal worker",
                capabilities={"supported_kinds": [kind], "max_risk_level": "high"},
                monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True,
                status="active",
            )
        )
    if new_plan:
        rows.append(
            ExecutionPlan(
                id=plan_id, entity_id=entity_id, status="running",
                execution_mode="live", approval_required=False,
                plan_dag={"steps": []},
            )
        )
    rows.append(
        ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=entity_id,
            step_key=step_key, kind=kind,
            params=params or {"prompt": "do it"}, depends_on=[],
            step_status=step_status, risk_level="low",
            attempt_count=attempt_count, max_attempts=max_attempts,
            current_lease_id=lease_id if step_status == "running" else None,
            started_at=now, expected_output_schema=expected_output_schema,
        )
    )
    rows.append(
        WorkLease(
            id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
            worker_id=worker_id, status=lease_status,
            lease_until=now + timedelta(minutes=5),
            credential_leases=[{"integration_id": "x"}],
        )
    )
    db.add_all(rows)
    await db.flush()
    return {
        "entity_id": entity_id, "plan_id": plan_id, "step_id": step_id,
        "lease_id": lease_id, "worker_id": worker_id,
    }


async def _activity_events(db, lease_id: str) -> list[str]:
    rows = (await db.execute(
        select(WorkerActivityLog.event).where(WorkerActivityLog.lease_id == lease_id)
    )).scalars().all()
    return list(rows)


def _reset_step_to_pending(step: ExecutionStep) -> None:
    """What a retry surface does to a step: the old lease is not touched."""
    step.step_status = "pending"
    step.current_lease_id = None
    step.attempt_count = 0
    step.error = None
    step.result = None
    step.last_execution_error = None
    step.human_input_prompt = None


# ── D1: revoke_active_leases ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_by_plan_cancels_active_and_needs_human_leases_only(db_session):
    active = await _seed(db_session)
    paused = await _seed(
        db_session, lease_status="needs_human", step_status="waiting_human",
        plan_id=active["plan_id"], entity_id=active["entity_id"],
        worker_id=active["worker_id"], step_key="paused",
    )
    finished = await _seed(
        db_session, lease_status="completed", step_status="done",
        plan_id=active["plan_id"], entity_id=active["entity_id"],
        worker_id=active["worker_id"], step_key="finished",
    )
    other_plan = await _seed(db_session)

    revoked = await Dispatcher().revoke_active_leases(
        db_session, plan_id=active["plan_id"], reason="plan cancelled by user",
    )

    assert {lease.id for lease in revoked} == {active["lease_id"], paused["lease_id"]}
    for lease in revoked:
        assert lease.status == WorkLeaseStatus.CANCELLED.value
        assert lease.error == {
            "type": LEASE_REVOKED_ERROR_TYPE,
            "message": "plan cancelled by user",
        }
        assert lease.credential_leases == []
        assert "lease_revoke" in await _activity_events(db_session, lease.id)

    untouched = await db_session.get(WorkLease, finished["lease_id"])
    assert untouched.status == "completed"
    assert untouched.error is None
    foreign = await db_session.get(WorkLease, other_plan["lease_id"])
    assert foreign.status == "active"


@pytest.mark.asyncio
async def test_revoke_by_step_ids_scopes_to_those_steps(db_session):
    first = await _seed(db_session)
    second = await _seed(
        db_session, plan_id=first["plan_id"], entity_id=first["entity_id"],
        worker_id=first["worker_id"], step_key="second",
    )

    revoked = await Dispatcher().revoke_active_leases(
        db_session, step_ids=[second["step_id"]], reason="step cancelled",
    )

    assert [lease.id for lease in revoked] == [second["lease_id"]]
    assert (await db_session.get(WorkLease, first["lease_id"])).status == "active"


@pytest.mark.asyncio
async def test_revoke_without_scope_touches_nothing(db_session):
    seeded = await _seed(db_session)
    assert await Dispatcher().revoke_active_leases(db_session, reason="no scope") == []
    assert (await db_session.get(WorkLease, seeded["lease_id"])).status == "active"


@pytest.mark.asyncio
async def test_cancel_step_revokes_the_paused_lease(db_session):
    from packages.core.services.step_resume import cancel_step

    seeded = await _seed(
        db_session, lease_status="needs_human", step_status="waiting_human",
    )
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    step.human_input_prompt = "Which account?"
    await db_session.flush()

    plan_id = await cancel_step(
        db_session,
        entity_id=seeded["entity_id"],
        step_id=seeded["step_id"],
        enqueue=False,
    )

    assert plan_id == seeded["plan_id"]
    lease = await db_session.get(WorkLease, seeded["lease_id"])
    assert lease.status == WorkLeaseStatus.CANCELLED.value
    assert lease.error["type"] == LEASE_REVOKED_ERROR_TYPE
    assert step.step_status == "failed"
    assert step.error["type"] == "UserSkipped"


# ── D2: ownership guard ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_lease_on_superseded_lease_leaves_reset_step_untouched(db_session):
    seeded = await _seed(db_session)
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    _reset_step_to_pending(step)
    await db_session.flush()

    lease = await Dispatcher().complete_lease(
        db_session, seeded["lease_id"], result={"text": "late arrival"},
        cost={"usd": 0.5},
    )

    assert lease.status == WorkLeaseStatus.CANCELLED.value
    assert lease.error["type"] == LEASE_SUPERSEDED_ERROR_TYPE
    assert lease.result == {"text": "late arrival"}, "audit copy of what came back"
    assert lease.cost == {"usd": 0.5}
    assert "lease_superseded" in await _activity_events(db_session, lease.id)

    assert step.step_status == "pending"
    assert step.result is None
    assert step.current_lease_id is None
    assert step.finished_at is None
    assert step.attempt_count == 0


@pytest.mark.asyncio
async def test_fail_lease_on_superseded_lease_does_not_spend_an_attempt(db_session):
    seeded = await _seed(db_session)
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    _reset_step_to_pending(step)
    await db_session.flush()

    lease = await Dispatcher().fail_lease(
        db_session, seeded["lease_id"], error={"type": "Boom", "message": "late"},
    )

    assert lease.status == WorkLeaseStatus.CANCELLED.value
    assert lease.error["type"] == LEASE_SUPERSEDED_ERROR_TYPE
    assert lease.error["reported_error"] == {"type": "Boom", "message": "late"}
    assert step.step_status == "pending"
    assert step.error is None
    assert step.last_execution_error is None
    assert step.attempt_count == 0
    worker = await db_session.get(Worker, seeded["worker_id"])
    assert (worker.consecutive_failures or 0) == 0


@pytest.mark.asyncio
async def test_lease_needs_human_on_superseded_lease_does_not_pause_the_step(db_session):
    seeded = await _seed(db_session)
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    _reset_step_to_pending(step)
    await db_session.flush()

    lease = await Dispatcher().lease_needs_human(
        db_session, seeded["lease_id"], prompt="Which account?",
    )

    assert lease.status == WorkLeaseStatus.CANCELLED.value
    assert lease.error["type"] == LEASE_SUPERSEDED_ERROR_TYPE
    assert step.step_status == "pending"
    assert step.human_input_prompt is None


@pytest.mark.asyncio
async def test_extend_lease_on_a_re_leased_step_cancels_the_old_lease(db_session):
    seeded = await _seed(db_session)
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    new_lease = WorkLease(
        id=generate_ulid(), step_id=step.id, plan_id=step.plan_id,
        entity_id=step.entity_id, worker_id=seeded["worker_id"], status="active",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(new_lease)
    step.current_lease_id = new_lease.id
    await db_session.flush()

    old = await Dispatcher().extend_lease(db_session, seeded["lease_id"], progress=0.5)

    assert old.status == WorkLeaseStatus.CANCELLED.value
    assert old.error["type"] == LEASE_SUPERSEDED_ERROR_TYPE
    assert old.heartbeat_count == 0
    assert step.step_status == "running"
    assert step.current_lease_id == new_lease.id
    assert new_lease.status == "active"


@pytest.mark.asyncio
async def test_owning_lease_still_completes_and_extends_normally(db_session):
    seeded = await _seed(db_session)

    extended = await Dispatcher().extend_lease(db_session, seeded["lease_id"])
    assert extended.status == "active"
    assert extended.heartbeat_count == 1

    lease = await Dispatcher().complete_lease(
        db_session, seeded["lease_id"], result={"text": "done"},
    )
    assert lease.status == "completed"
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert step.step_status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition",
    ["complete_lease", "fail_lease", "lease_needs_human"],
)
async def test_stale_execution_claim_cannot_transition_an_active_lease(
    db_session,
    transition,
):
    seeded = await _seed(db_session)
    lease = await db_session.get(WorkLease, seeded["lease_id"])
    live_claim = generate_ulid()
    lease.execution_claim_id = live_claim
    await db_session.commit()

    kwargs = {
        "complete_lease": {"result": {"text": "stale success"}},
        "fail_lease": {"error": {"type": "Boom", "message": "stale failure"}},
        "lease_needs_human": {"prompt": "stale question"},
    }[transition]
    kwargs["expected_execution_claim_id"] = generate_ulid()

    with pytest.raises(LeaseNotActive, match="execution claim is stale"):
        await getattr(Dispatcher(), transition)(
            db_session,
            seeded["lease_id"],
            **kwargs,
        )

    db_session.expire_all()
    lease = await db_session.get(WorkLease, seeded["lease_id"])
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.ACTIVE.value
    assert lease.execution_claim_id == live_claim
    assert step.step_status == "running"
    assert step.current_lease_id == lease.id
    assert step.result is None
    assert step.error is None
    assert step.human_input_prompt is None


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_taken_over_by_rotating_its_claim(db_session):
    seeded = await _seed(db_session)
    lease = await db_session.get(WorkLease, seeded["lease_id"])
    successor_claim = generate_ulid()
    lease.execution_claim_id = successor_claim
    lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(LeaseNotActive, match="execution claim is stale"):
        await Dispatcher().complete_lease(
            db_session,
            seeded["lease_id"],
            result={"text": "must be re-leased"},
            expected_execution_claim_id=successor_claim,
        )

    db_session.expire_all()
    lease = await db_session.get(WorkLease, seeded["lease_id"])
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.ACTIVE.value
    assert lease.execution_claim_id == successor_claim
    assert step.step_status == "running"
    assert step.result is None


# ── D3: evidence + runtime checkpoint on failure ──────────────────────


@pytest.mark.asyncio
async def test_fail_lease_with_tool_effect_evidence_blocks_retry(db_session):
    seeded = await _seed(db_session, attempt_count=1, max_attempts=3)
    effect = {"kind": "tool_effect", "tool": "twitter.post", "receipt": "tw_1"}
    checkpoint = {
        "schema_version": 4,
        "messages": [{"role": "user", "content": "publish"}],
        "pending_tool_call": {
            "id": "publish-1",
            "name": "twitter.post",
            "arguments": {"body": "ready"},
        },
    }
    # Only a checkpoint already persisted by the host may be retained as an
    # executable continuation after a reported external effect.
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    step.runtime_checkpoint = checkpoint
    await db_session.flush()

    lease = await Dispatcher().fail_lease(
        db_session, seeded["lease_id"],
        error={"type": "OutputSchemaError", "message": "missing url"},
        evidence_refs=[effect],
        runtime_checkpoint=checkpoint,
    )

    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.evidence_refs == [effect]
    assert step.evidence_refs == [effect]
    assert step.step_status == "failed", "a recorded external effect must not be repeated"
    assert lease.status == WorkLeaseStatus.FAILED.value
    assert step.runtime_checkpoint == checkpoint
    assert step.error["type"] == "OutputSchemaError"
    assert "may or may not have happened" in step.error["failure"]["reason"]
    assert step.error["failure"]["requires_human"] is True
    assert step.error["failure"]["retryable"] is False


@pytest.mark.asyncio
async def test_fail_lease_does_not_trust_unpersisted_effect_checkpoint(db_session):
    seeded = await _seed(db_session, attempt_count=1, max_attempts=3)
    effect = {"kind": "tool_effect", "tool": "twitter.post", "receipt": "tw_1"}
    reported_checkpoint = {
        "schema_version": 4,
        "messages": [{"role": "user", "content": "publish"}],
        "pending_tool_call": {
            "id": "publish-1", "name": "twitter.post", "arguments": {"body": "ready"},
        },
    }

    lease = await Dispatcher().fail_lease(
        db_session, seeded["lease_id"],
        error={"type": "OutputSchemaError", "message": "missing url"},
        evidence_refs=[effect], runtime_checkpoint=reported_checkpoint,
    )

    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.FAILED.value
    assert step.step_status == "failed"
    assert step.evidence_refs == [effect]
    assert step.runtime_checkpoint is None, "worker report alone must not mint replay authority"


@pytest.mark.asyncio
async def test_fail_lease_without_evidence_still_retries(db_session):
    seeded = await _seed(db_session, attempt_count=1, max_attempts=3)
    await Dispatcher().fail_lease(
        db_session, seeded["lease_id"],
        error={"type": "OutputSchemaError", "message": "missing url"},
    )
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert step.step_status == "pending"


@pytest.mark.asyncio
async def test_fail_lease_runtime_checkpoint_persists_on_retry(db_session):
    seeded = await _seed(db_session, attempt_count=1, max_attempts=3)
    checkpoint = {"schema_version": "worker-transcript/1", "messages": [{"role": "user", "content": "hi"}]}

    await Dispatcher().fail_lease(
        db_session, seeded["lease_id"],
        error={"type": "EmptyModelOutput", "message": "nothing"},
        runtime_checkpoint=checkpoint,
    )

    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert step.step_status == "pending"
    assert step.runtime_checkpoint == checkpoint


@pytest.mark.asyncio
async def test_exhausted_output_failure_does_not_fabricate_human_input(db_session):
    seeded = await _seed(
        db_session, attempt_count=1, max_attempts=1,
        params={"prompt": "x", "retry_policy": {"max_attempts": 1, "auto_human_on_exhausted": True}},
    )
    checkpoint = {"schema_version": "worker-transcript/1", "messages": []}

    await Dispatcher().fail_lease(
        db_session, seeded["lease_id"],
        error={"type": "EmptyModelOutput", "message": "nothing"},
        runtime_checkpoint=checkpoint,
    )

    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert step.step_status == "failed"
    assert step.finished_at is not None
    assert step.human_input_prompt is None
    assert step.runtime_checkpoint is None
    assert step.error["type"] == "EmptyModelOutput"


@pytest.mark.asyncio
async def test_exhausted_expired_lease_does_not_fabricate_human_input(db_session):
    seeded = await _seed(
        db_session, attempt_count=1, max_attempts=1,
        params={"prompt": "x", "retry_policy": {"max_attempts": 1, "auto_human_on_exhausted": True}},
    )
    lease = await db_session.get(WorkLease, seeded["lease_id"])
    lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()

    # Other expired leases can exist in the test transaction; this assertion
    # is about the seeded lease's own transition, not a global sweep count.
    await Dispatcher().expire_leases(db_session)

    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.EXPIRED.value
    assert step.step_status == "failed"
    assert step.finished_at is not None
    assert step.human_input_prompt is None
    assert step.error["type"] == "lease_expired"


@pytest.mark.asyncio
async def test_fail_lease_runtime_checkpoint_is_cleared_on_terminal_failure(db_session):
    seeded = await _seed(db_session, attempt_count=1, max_attempts=3)
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    step.runtime_checkpoint = {"schema_version": "worker-transcript/1", "messages": [{"role": "user", "content": "old"}]}
    await db_session.flush()

    await Dispatcher().fail_lease(
        db_session, seeded["lease_id"],
        error={"type": "CreditExhaustedError", "message": "no credits"},
        will_retry=False,
        runtime_checkpoint={"schema_version": "worker-transcript/1", "messages": [{"role": "user", "content": "new"}]},
    )

    assert step.step_status == "failed"
    assert step.runtime_checkpoint is None


@pytest.mark.asyncio
async def test_fail_lease_without_checkpoint_keeps_the_existing_one_on_retry(db_session):
    """A request_input checkpoint written by lease_needs_human survives an
    unrelated failure report that carries no transcript."""
    seeded = await _seed(db_session, attempt_count=1, max_attempts=3)
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    existing = {"schema_version": "runtime-agent/1", "pending_tool_call": {"id": "c1"}}
    step.runtime_checkpoint = existing
    await db_session.flush()

    await Dispatcher().fail_lease(
        db_session, seeded["lease_id"], error={"type": "Boom", "message": "x"},
    )

    assert step.step_status == "pending"
    assert step.runtime_checkpoint == existing


@pytest.mark.asyncio
async def test_complete_lease_forwards_runtime_checkpoint_to_schema_failure(db_session):
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    seeded = await _seed(
        db_session, kind="action", attempt_count=1, max_attempts=3,
        params={"x": 1}, expected_output_schema=schema,
    )
    checkpoint = {"schema_version": "worker-transcript/1", "messages": [{"role": "user", "content": "hi"}]}

    lease = await Dispatcher().complete_lease(
        db_session, seeded["lease_id"], result={"wrong": 1},
        runtime_checkpoint=checkpoint,
    )

    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == "failed"
    assert step.error["type"] == "OutputSchemaError"
    assert step.step_status == "pending"
    assert step.runtime_checkpoint == checkpoint
