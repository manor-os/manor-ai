"""External worker attempt-token and expiry fencing regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.routers.workers import (
    HeartbeatActiveLease,
    HeartbeatCapacity,
    HeartbeatCompletedLease,
    HeartbeatRequest,
    _build_lease_dto_for_worker,
    _heartbeat_inner,
    _merge_worker_capabilities,
)
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    WorkLeaseStatus,
)
from packages.core.dispatcher.service import Dispatcher, LeaseNotActive
from packages.core.contracts.task_output import TaskOutputValueKind
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker


def test_attempt_token_capability_cannot_be_downgraded_by_heartbeat() -> None:
    merged = _merge_worker_capabilities(
        {
            "supported_kinds": ["action"],
            "lease_attempt_token_version": 1,
        },
        {
            "supported_kinds": ["action"],
            "lease_attempt_token_version": 0,
        },
    )

    assert merged["lease_attempt_token_version"] == 1


async def _seed_active_lease(
    db,
    *,
    attempt_token: str | None,
    kind: str = "llm",
    lease_until: datetime | None = None,
) -> dict[str, str]:
    entity_id = generate_ulid()
    worker_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    now = datetime.now(timezone.utc)
    worker = Worker(
        id=worker_id,
        entity_id=entity_id,
        kind="custom_http",
        display_name="Fenced external worker",
        capabilities={
            "supported_kinds": [kind],
            "max_concurrent_leases": 1,
            "max_risk_level": "high",
            "lease_attempt_token_version": 1,
        },
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        status="running",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
    )
    step = ExecutionStep(
        id=step_id,
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="external_attempt",
        kind=kind,
        provider="demo" if kind == "action" else None,
        action_key="publish" if kind == "action" else None,
        params={"message": "once"} if kind == "action" else {"prompt": "work"},
        depends_on=[],
        step_status="running",
        risk_level="low",
        attempt_count=1,
        max_attempts=3,
        current_lease_id=lease_id,
        started_at=now,
    )
    lease = WorkLease(
        id=lease_id,
        step_id=step_id,
        plan_id=plan_id,
        entity_id=entity_id,
        worker_id=worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=lease_until or now + timedelta(minutes=5),
        execution_claim_id=attempt_token,
    )
    db.add_all([worker, plan, step, lease])
    await db.commit()
    return {
        "entity_id": entity_id,
        "worker_id": worker_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "lease_id": lease_id,
    }


async def _mutate(
    db,
    transition: str,
    lease_id: str,
    *,
    attempt_token: str | None,
    expected_worker_id: str | None = None,
):
    common = {
        "expected_worker_id": expected_worker_id,
        "expected_execution_claim_id": attempt_token,
        "require_execution_claim_id_if_present": True,
    }
    dispatcher = Dispatcher()
    if transition == "complete_lease":
        return await dispatcher.complete_lease(
            db,
            lease_id,
            result={"text": "done"},
            **common,
        )
    if transition == "fail_lease":
        return await dispatcher.fail_lease(
            db,
            lease_id,
            error={"type": "WorkerFailure", "message": "failed"},
            will_retry=False,
            **common,
        )
    if transition == "lease_needs_human":
        return await dispatcher.lease_needs_human(
            db,
            lease_id,
            prompt="Operator input needed",
            **common,
        )
    if transition == "extend_lease":
        return await dispatcher.extend_lease(
            db,
            lease_id,
            progress=0.5,
            **common,
        )
    raise AssertionError(f"unknown transition {transition}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition",
    ["complete_lease", "fail_lease", "lease_needs_human", "extend_lease"],
)
@pytest.mark.parametrize("reported", [None, "wrong-attempt"])
async def test_tokenized_external_mutation_rejects_missing_or_wrong_token(
    db_session,
    transition: str,
    reported: str | None,
):
    current = generate_ulid()
    seeded = await _seed_active_lease(db_session, attempt_token=current)

    message = "requires its current attempt token" if reported is None else "execution claim is stale"
    with pytest.raises(LeaseNotActive, match=message):
        await _mutate(
            db_session,
            transition,
            seeded["lease_id"],
            attempt_token=reported,
            expected_worker_id=seeded["worker_id"],
        )
    await db_session.rollback()

    lease = await db_session.get(WorkLease, seeded["lease_id"])
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.ACTIVE.value
    assert lease.execution_claim_id == current
    assert step.step_status == "running"
    assert step.current_lease_id == lease.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition",
    ["complete_lease", "fail_lease", "lease_needs_human", "extend_lease"],
)
async def test_tokenized_external_mutation_accepts_current_token(
    db_session,
    transition: str,
):
    current = generate_ulid()
    seeded = await _seed_active_lease(db_session, attempt_token=current)

    lease = await _mutate(
        db_session,
        transition,
        seeded["lease_id"],
        attempt_token=current,
        expected_worker_id=seeded["worker_id"],
    )

    assert lease.id == seeded["lease_id"]
    if transition == "extend_lease":
        assert lease.execution_claim_id == current
    else:
        assert lease.execution_claim_id is None
        assert lease.execution_claimed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition",
    ["complete_lease", "fail_lease", "lease_needs_human", "extend_lease"],
)
async def test_legacy_external_mutation_remains_compatible_without_token(
    db_session,
    transition: str,
):
    seeded = await _seed_active_lease(db_session, attempt_token=None)

    lease = await _mutate(
        db_session,
        transition,
        seeded["lease_id"],
        attempt_token=None,
        expected_worker_id=seeded["worker_id"],
    )

    assert lease.id == seeded["lease_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition",
    ["complete_lease", "fail_lease", "lease_needs_human", "extend_lease"],
)
async def test_legacy_external_mutation_rejects_foreign_worker_under_lease_lock(
    db_session,
    transition: str,
):
    seeded = await _seed_active_lease(db_session, attempt_token=None)
    foreign_worker_id = generate_ulid()

    with pytest.raises(LeaseNotActive, match="is not owned by worker"):
        await _mutate(
            db_session,
            transition,
            seeded["lease_id"],
            attempt_token=None,
            expected_worker_id=foreign_worker_id,
        )
    await db_session.rollback()

    lease = await db_session.get(WorkLease, seeded["lease_id"])
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.ACTIVE.value
    assert lease.worker_id == seeded["worker_id"]
    assert lease.heartbeat_count == 0
    assert lease.progress is None
    assert step.step_status == "running"
    assert step.current_lease_id == lease.id


@pytest.mark.asyncio
@pytest.mark.parametrize("report_kind", ["done", "failed", "active"])
async def test_heartbeat_cannot_mutate_another_workers_legacy_lease(
    db_session,
    report_kind: str,
):
    seeded = await _seed_active_lease(db_session, attempt_token=None)
    foreign_worker = Worker(
        id=generate_ulid(),
        entity_id=seeded["entity_id"],
        kind="custom_http",
        display_name="Foreign heartbeat worker",
        capabilities={"supported_kinds": ["llm"]},
        preferences={},
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    db_session.add(foreign_worker)
    await db_session.commit()

    completed: list[HeartbeatCompletedLease] = []
    active: list[HeartbeatActiveLease] = []
    if report_kind == "active":
        active.append(
            HeartbeatActiveLease(
                lease_id=seeded["lease_id"],
                progress=0.75,
            )
        )
    else:
        completed.append(
            HeartbeatCompletedLease(
                lease_id=seeded["lease_id"],
                status=report_kind,
                result={"text": "foreign result"},
                error={"type": "ForeignFailure", "message": "foreign"},
                task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
            )
        )

    response = await _heartbeat_inner(
        HeartbeatRequest(
            active_leases=active,
            completed_since_last=completed,
            capacity=HeartbeatCapacity(can_accept_leases=0),
        ),
        foreign_worker,
        db_session,
    )

    lease = await db_session.get(WorkLease, seeded["lease_id"])
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.ACTIVE.value
    assert lease.worker_id == seeded["worker_id"]
    assert lease.heartbeat_count == 0
    assert lease.progress is None
    assert lease.result is None
    assert lease.error is None
    assert step.step_status == "running"
    assert step.current_lease_id == lease.id
    if report_kind == "active":
        assert [item.type for item in response.instructions] == ["cancel_lease"]


@pytest.mark.asyncio
async def test_owner_change_between_snapshot_and_locked_lease_is_rejected(
    db_session,
):
    """The ownership fence is evaluated from the row locked for mutation."""

    seeded = await _seed_active_lease(db_session, attempt_token=None)
    successor_worker_id = generate_ulid()
    db_session.add(
        Worker(
            id=successor_worker_id,
            entity_id=seeded["entity_id"],
            kind="custom_http",
            display_name="Successor owner",
            capabilities={"supported_kinds": ["llm"]},
            monthly_spent_usd=Decimal("0"),
            auto_pause_on_budget=True,
            status="active",
        )
    )
    await db_session.commit()
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as owner_db, sessions() as report_db:
        await owner_db.execute(
            select(ExecutionPlan)
            .where(ExecutionPlan.id == seeded["plan_id"])
            .with_for_update()
        )
        lease_to_reassign = await owner_db.get(WorkLease, seeded["lease_id"])
        assert lease_to_reassign is not None
        lease_to_reassign.worker_id = successor_worker_id
        await owner_db.flush()

        stale_report = asyncio.create_task(
            Dispatcher().extend_lease(
                report_db,
                seeded["lease_id"],
                progress=0.9,
                expected_worker_id=seeded["worker_id"],
                require_execution_claim_id_if_present=True,
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(stale_report), timeout=0.1)

        await owner_db.commit()
        with pytest.raises(LeaseNotActive, match="is not owned by worker"):
            await asyncio.wait_for(stale_report, timeout=2)
        await report_db.rollback()

    async with sessions() as verify_db:
        lease = await verify_db.get(WorkLease, seeded["lease_id"])
        assert lease is not None
        assert lease.worker_id == successor_worker_id
        assert lease.status == WorkLeaseStatus.ACTIVE.value
        assert lease.heartbeat_count == 0
        assert lease.progress is None


@pytest.mark.asyncio
async def test_checkout_mints_unique_attempt_tokens_from_persisted_capability(
    db_session,
):
    entity_id = generate_ulid()
    from tests.approval_settings_test_utils import action_rule, initialize_test_account_approval

    await initialize_test_account_approval(db_session, entity_id=entity_id,
        user_rules=[action_rule("unclassified.action")])

    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="custom_http",
        display_name="Token-aware worker",
        capabilities={
            "supported_kinds": ["llm"],
            "supported_providers": None,
            "max_concurrent_leases": 2,
            "max_risk_level": "high",
            "lease_attempt_token_version": 1,
        },
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        status="running",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
    )
    steps = [
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan.id,
            entity_id=entity_id,
            step_key=f"work_{index}",
            kind="llm",
            params={
                "prompt": f"work {index}",
                ACTION_EFFECT_GENERATION_PARAM_KEY: index + 1,
            },
            depends_on=[],
            step_status="pending",
            risk_level="low",
            attempt_count=0,
            max_attempts=3,
        )
        for index in range(2)
    ]
    db_session.add_all([worker, plan, *steps])
    await db_session.flush()

    leased = await Dispatcher().checkout_steps_for_worker(
        db_session,
        worker,
        max_n=2,
        plan_id=plan.id,
    )

    assert len(leased) == 2
    tokens = {lease.execution_claim_id for lease, _step in leased}
    assert None not in tokens
    assert len(tokens) == 2
    for lease, step in leased:
        dto = _build_lease_dto_for_worker(
            lease=lease,
            step=step,
            plan=SimpleNamespace(execution_mode="live"),
        )
        assert dto.attempt_token == lease.execution_claim_id
        assert "attempt_token" not in dto.params
        assert ACTION_EFFECT_GENERATION_PARAM_KEY not in dto.params


@pytest.mark.asyncio
async def test_expired_tokenized_external_action_is_never_blindly_retried(
    db_session,
):
    attempt_token = generate_ulid()
    seeded = await _seed_active_lease(
        db_session,
        attempt_token=attempt_token,
        kind="action",
        lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    expired = await Dispatcher().expire_leases(
        db_session,
        now=datetime.now(timezone.utc),
    )

    assert expired == 1
    lease = await db_session.get(WorkLease, seeded["lease_id"])
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert lease.status == WorkLeaseStatus.EXPIRED.value
    # Expiry proves neither that the daemon stopped nor that provider I/O did
    # not finish. Keep the claim as the durable quiescence fence; the token is
    # still absent from provider arguments and reconciliation evidence.
    assert lease.execution_claim_id == attempt_token
    assert step.step_status == "waiting_human"
    assert step.current_lease_id is None
    unknown = [
        item
        for item in step.evidence_refs
        if isinstance(item, dict)
        and (item.get("fields") or {}).get("external_attempt_state")
        == "expired_outcome_unknown"
    ]
    assert len(unknown) == 1
    assert unknown[0]["effect"] == "pending"
    assert unknown[0]["fields"]["lease_id"] == lease.id
    assert unknown[0]["fields"]["owner_step_id"] == step.id
    assert "attempt_token" not in unknown[0]["fields"]
