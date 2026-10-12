"""Durable Plan-cycle wake delivery and recovery regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.dialects import postgresql

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.plans.wakeup import (
    PLAN_CYCLE_EXECUTION_CLAIM_KEY,
    PLAN_CYCLE_OUTBOX_EVENT,
    claim_plan_cycle_wake,
    hint_pending_plan_cycle,
    plan_cycle_outbox_dedupe_key,
    release_plan_cycle_wake_claim,
    settle_plan_cycle_wake,
    stage_plan_cycle_wake,
)
from packages.core.tasks.runtime_tasks import (
    _runtime_outbox_plan_category_filter,
    consume_plan_cycle_intent_once,
    dispatch_runtime_outbox_once,
    recover_stalled_plan_cycle_wakes_once,
)


async def _plan(db, *, status: str = "running") -> ExecutionPlan:
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        status=status,
        execution_mode="live",
        started_at=datetime.now(timezone.utc),
        plan_dag={"steps": []},
        dispatcher_state={},
    )
    db.add(plan)
    await db.flush()
    return plan


async def _event(db, plan_id: str) -> RuntimeOutboxEvent:
    return (await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == plan_cycle_outbox_dedupe_key(plan_id)
        ).execution_options(populate_existing=True)
    )).scalar_one()


async def _claim(db, wake, token: str = "heavy-owner"):
    claim, reason = await claim_plan_cycle_wake(
        db,
        event_id=wake.event_id,
        plan_id=wake.plan_id,
        wake_generation=wake.wake_generation,
        claim_token=token,
    )
    assert reason == "claimed"
    assert claim is not None
    return claim


@pytest.mark.parametrize(
    ("plan_cycle", "operator"),
    ((False, "!="), (True, "=")),
)
def test_fair_category_filter_renders_partial_index_literal(
    plan_cycle: bool,
    operator: str,
):
    statement = select(RuntimeOutboxEvent.id).where(
        _runtime_outbox_plan_category_filter(plan_cycle=plan_cycle)
    )

    sql = str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    ))

    assert "POSTCOMPILE" not in sql
    assert (
        f"runtime_outbox_events.event_type {operator} "
        "'runtime.dispatch_plan_cycle'"
    ) in sql


@pytest.mark.asyncio
async def test_broker_failure_keeps_plan_cycle_wake_pending(
    db_session,
    monkeypatch,
):
    plan = await _plan(db_session)
    await stage_plan_cycle_wake(db_session, plan.id, reason="lease_completed")
    await db_session.commit()

    def fail_send(*_args, **_kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        fail_send,
    )
    assert await dispatch_runtime_outbox_once() == 0

    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert event.attempt_count == 1
    assert "broker down" in str(event.last_error)


@pytest.mark.asyncio
async def test_post_commit_hint_failure_preserves_durable_intent_and_plan_state(
    db_session,
    monkeypatch,
):
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(
        db_session,
        plan.id,
        reason="same_transaction_transition",
    )
    assert wake is not None
    await db_session.commit()

    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.dispatch_plan_cycle_intent_task.apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(ConnectionError("broker down")),
    )

    # API ``dispatched`` means durable intent exists. The optional latency
    # hint may fail without projecting transport health onto business state.
    assert await hint_pending_plan_cycle(
        db_session,
        plan.id,
        reason="post_commit_test_hint",
    ) is True

    await db_session.refresh(plan)
    assert plan.status == "running"
    assert plan.last_error is None
    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert event.attempt_count == 0
    assert event.payload["wake_generation"] == wake.wake_generation
    assert event.payload["reason"] == "same_transaction_transition"


@pytest.mark.asyncio
async def test_unacknowledged_control_delivery_is_redelivered(
    db_session,
    monkeypatch,
):
    # The publisher commits in its own worker session. Keep this exact-count
    # assertion independent from durable rows intentionally left by earlier
    # tests in the module.
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.commit()
    plan = await _plan(db_session)
    await stage_plan_cycle_wake(db_session, plan.id, reason="lease_completed")
    await db_session.commit()
    sent: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        lambda name, **kwargs: sent.append((name, kwargs)),
    )
    assert await dispatch_runtime_outbox_once() == 1
    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert set(sent[0][1]["kwargs"]) == {
        "event_id", "plan_id", "wake_generation", "reason",
    }

    event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    assert await dispatch_runtime_outbox_once() == 1
    assert [name for name, _kwargs in sent] == [
        PLAN_CYCLE_OUTBOX_EVENT,
        PLAN_CYCLE_OUTBOX_EVENT,
    ]


def test_ack_pending_redelivery_backs_off_exponentially():
    from packages.core.tasks.runtime_tasks import (
        _RUNTIME_OUTBOX_ACK_MAX_SECONDS,
        _runtime_outbox_ack_available_at,
    )

    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    delays = [
        (_runtime_outbox_ack_available_at(now, base_seconds=120, attempt_count=n) - now).total_seconds()
        for n in (1, 2, 3, 4, 5, 6, 40)
    ]

    assert delays[:5] == [120, 240, 480, 960, 1800]
    assert delays[5] == _RUNTIME_OUTBOX_ACK_MAX_SECONDS == 1800
    assert delays[6] == 1800


@pytest.mark.asyncio
async def test_unacknowledged_plan_cycle_redelivery_interval_grows(
    db_session,
    monkeypatch,
):
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.commit()
    plan = await _plan(db_session)
    await stage_plan_cycle_wake(db_session, plan.id, reason="lease_completed")
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        lambda name, **kwargs: None,
    )

    gaps = []
    for _ in range(3):
        before = datetime.now(timezone.utc)
        assert await dispatch_runtime_outbox_once() == 1
        event = await _event(db_session, plan.id)
        gaps.append((event.available_at - before).total_seconds())
        event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()

    assert 115 <= gaps[0] <= 125
    assert 235 <= gaps[1] <= 245
    assert 475 <= gaps[2] <= 485


@pytest.mark.asyncio
async def test_control_handoff_failure_and_retry_never_ack_the_wake(
    db_session,
    monkeypatch,
):
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(db_session, plan.id)
    assert wake is not None
    await db_session.commit()

    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks.run_plan,
        "apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(ConnectionError("heavy broker down")),
    )
    result = await consume_plan_cycle_intent_once(
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
    )
    assert result["status"] == "deferred"
    event = await _event(db_session, plan.id)
    assert event.delivered_at is None

    handed_off: list[dict] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "apply_async",
        lambda **kwargs: handed_off.append(kwargs),
    )
    result = await consume_plan_cycle_intent_once(
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
    )
    assert result["status"] == "dispatched"
    assert handed_off[0]["kwargs"]["wake_generation"] == wake.wake_generation
    assert (await _event(db_session, plan.id)).delivered_at is None


@pytest.mark.asyncio
async def test_delayed_control_message_cannot_reopen_acknowledged_generation(
    db_session,
    monkeypatch,
):
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(db_session, plan.id)
    assert wake is not None
    claim = await _claim(db_session, wake)
    await db_session.commit()
    assert await settle_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
        claim_token=claim.claim_token,
        next_action="stop",
    ) is True
    await db_session.commit()

    from packages.core.tasks import ai_tasks

    handed_off: list[dict] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "apply_async",
        lambda **kwargs: handed_off.append(kwargs),
    )
    result = await consume_plan_cycle_intent_once(
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
    )

    assert result == {"status": "closed", "reason": "already_acknowledged"}
    assert handed_off == []
    event = await _event(db_session, plan.id)
    assert event.delivered_at is not None
    assert event.last_error is None


@pytest.mark.asyncio
async def test_concurrent_duplicate_staging_keeps_one_generation_row(db_session):
    plan = await _plan(db_session)
    await db_session.commit()

    from packages.core.database import create_worker_session

    async def stage(reason: str) -> None:
        session_factory = create_worker_session()
        async with session_factory() as db:
            await stage_plan_cycle_wake(db, plan.id, reason=reason)
            await db.commit()

    await asyncio.gather(stage("duplicate-a"), stage("duplicate-b"))
    count = await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(
        RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == plan.id,
    ))
    assert count == 1
    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert event.payload["reason"] in {"duplicate-a", "duplicate-b"}


@pytest.mark.asyncio
async def test_concurrent_heavy_claims_execute_one_generation_once(db_session):
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(db_session, plan.id, reason="single-flight")
    assert wake is not None
    await db_session.commit()

    from packages.core.database import create_worker_session

    async def claim(token: str):
        session_factory = create_worker_session()
        async with session_factory() as db:
            result = await claim_plan_cycle_wake(
                db,
                event_id=wake.event_id,
                plan_id=plan.id,
                wake_generation=wake.wake_generation,
                claim_token=token,
            )
            await db.commit()
            return result

    # Broker redelivery reuses the Celery task id, so even identical claim
    # tokens must not be treated as permission for concurrent re-entry.
    first, second = await asyncio.gather(claim("same-heavy"), claim("same-heavy"))
    claims = [item for item in (first, second) if item[0] is not None]
    losers = [item for item in (first, second) if item[0] is None]
    assert len(claims) == 1
    assert len(losers) == 1
    assert losers[0][1] == "execution_claim_held"

    event = await _event(db_session, plan.id)
    stored_claim = event.payload[PLAN_CYCLE_EXECUTION_CLAIM_KEY]
    assert stored_claim["token"] == claims[0][0].claim_token
    assert stored_claim["wake_generation"] == wake.wake_generation
    assert event.delivered_at is None


@pytest.mark.asyncio
async def test_explicit_retry_release_allows_same_task_to_reclaim(db_session):
    plan = await _plan(db_session)
    plan_id = plan.id
    wake = await stage_plan_cycle_wake(db_session, plan.id, reason="retry")
    assert wake is not None
    claim = await _claim(db_session, wake, token="same-task-id")
    await db_session.commit()

    duplicate, reason = await claim_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan_id,
        wake_generation=wake.wake_generation,
        claim_token="same-task-id",
    )
    assert duplicate is None
    assert reason == "execution_claim_held"
    await db_session.rollback()

    assert await release_plan_cycle_wake_claim(
        db_session,
        event_id=wake.event_id,
        plan_id=plan_id,
        wake_generation=wake.wake_generation,
        claim_token=claim.claim_token,
        delay_seconds=0,
    ) is True
    await db_session.commit()

    retry, reason = await claim_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan_id,
        wake_generation=wake.wake_generation,
        claim_token="same-task-id",
    )
    assert reason == "claimed"
    assert retry is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "busy_status",
    ["cycle_busy", "supervisor_review_in_progress"],
)
async def test_busy_cycle_rearms_same_generation_without_ack_churn(
    db_session,
    busy_status,
    monkeypatch,
):
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(db_session, plan.id, reason="single-flight")
    assert wake is not None
    claim = await _claim(db_session, wake)
    await db_session.commit()

    assert await settle_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
        claim_token=claim.claim_token,
        next_action="schedule_self",
        result_status=busy_status,
        delay_seconds=2,
    ) is True
    await db_session.commit()

    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert event.payload["wake_generation"] == wake.wake_generation
    assert event.payload["reason"] == "cycle_single_flight_retry"
    assert PLAN_CYCLE_EXECUTION_CLAIM_KEY not in event.payload

    from packages.core.tasks import ai_tasks

    handed_off: list[dict] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "apply_async",
        lambda **kwargs: handed_off.append(kwargs),
    )
    control = await consume_plan_cycle_intent_once(
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
    )
    assert control == {"status": "suppressed", "reason": "not_due"}
    assert handed_off == []

    heavy_claim, reason = await claim_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
        claim_token="delayed-heavy-duplicate",
    )
    assert heavy_claim is None
    assert reason == "not_due"


@pytest.mark.asyncio
async def test_settle_requires_exact_live_execution_claim(db_session):
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(db_session, plan.id, reason="claim-fence")
    assert wake is not None
    claim = await _claim(db_session, wake)
    await db_session.commit()

    assert await settle_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
        claim_token="stale-owner",
        next_action="stop",
    ) is False
    await db_session.commit()
    assert (await _event(db_session, plan.id)).delivered_at is None

    assert await settle_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
        claim_token=claim.claim_token,
        next_action="stop",
    ) is True
    await db_session.commit()
    assert (await _event(db_session, plan.id)).delivered_at is not None


@pytest.mark.asyncio
async def test_old_cycle_ack_cannot_close_newer_generation(db_session):
    plan = await _plan(db_session)
    first = await stage_plan_cycle_wake(db_session, plan.id, reason="first")
    assert first is not None
    claim = await _claim(db_session, first)
    await db_session.commit()
    before = datetime.now(timezone.utc)
    second = await stage_plan_cycle_wake(
        db_session,
        plan.id,
        reason="second",
        delay_seconds=30,
    )
    await db_session.commit()
    assert second is not None
    event = await _event(db_session, plan.id)
    assert event.payload[PLAN_CYCLE_EXECUTION_CLAIM_KEY]["token"] == claim.claim_token

    assert await settle_plan_cycle_wake(
        db_session,
        event_id=first.event_id,
        plan_id=plan.id,
        wake_generation=first.wake_generation,
        claim_token=claim.claim_token,
        next_action="stop",
    ) is False
    await db_session.commit()
    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert event.payload["wake_generation"] == second.wake_generation
    assert PLAN_CYCLE_EXECUTION_CLAIM_KEY not in event.payload
    assert event.available_at >= before + timedelta(seconds=29)


@pytest.mark.asyncio
async def test_schedule_self_replaces_exact_generation_and_due_time(db_session):
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(db_session, plan.id, reason="initial")
    assert wake is not None
    claim = await _claim(db_session, wake)
    await db_session.commit()
    before = datetime.now(timezone.utc)

    assert await settle_plan_cycle_wake(
        db_session,
        event_id=wake.event_id,
        plan_id=plan.id,
        wake_generation=wake.wake_generation,
        claim_token=claim.claim_token,
        next_action="schedule_self",
        delay_seconds=30,
    ) is True
    await db_session.commit()
    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert event.payload["wake_generation"] != wake.wake_generation
    assert event.payload["reason"] == "cycle_schedule_self"
    assert event.available_at >= before + timedelta(seconds=29)


@pytest.mark.asyncio
async def test_backstop_wakes_all_terminal_steps_without_human_wait_starvation(
    db_session,
    monkeypatch,
):
    # Human-only rows sort first and outnumber the scan size. They are excluded
    # in SQL, so the terminal Plan still receives its finalization cycle.
    waiting_plan_ids: set[str] = set()
    for index in range(3):
        waiting_plan = await _plan(db_session)
        waiting_plan_ids.add(waiting_plan.id)
        waiting_plan.created_at = datetime.now(timezone.utc) - timedelta(
            minutes=10 - index
        )
        db_session.add(ExecutionStep(
            id=generate_ulid(),
            plan_id=waiting_plan.id,
            entity_id=waiting_plan.entity_id,
            step_key=f"wait_{index}",
            kind="human",
            params={},
            depends_on=[],
            step_status="waiting_human",
        ))
    coordinator_wait_plan = await _plan(db_session)
    waiting_plan_ids.add(coordinator_wait_plan.id)
    db_session.add_all([
        ExecutionStep(
            id=generate_ulid(),
            plan_id=coordinator_wait_plan.id,
            entity_id=coordinator_wait_plan.entity_id,
            step_key="coordinator_root",
            kind="subagent",
            params={"_coordinator": True},
            depends_on=[],
            step_status="paused",
        ),
        ExecutionStep(
            id=generate_ulid(),
            plan_id=coordinator_wait_plan.id,
            entity_id=coordinator_wait_plan.entity_id,
            step_key="coordinator_child",
            kind="human",
            params={"_coordinator_child": True},
            depends_on=[],
            step_status="waiting_human",
        ),
    ])
    coordinator_resume_plan = await _plan(db_session)
    db_session.add_all([
        ExecutionStep(
            id=generate_ulid(),
            plan_id=coordinator_resume_plan.id,
            entity_id=coordinator_resume_plan.entity_id,
            step_key="coordinator_root",
            kind="subagent",
            params={"_coordinator": True},
            depends_on=[],
            step_status="paused",
        ),
        ExecutionStep(
            id=generate_ulid(),
            plan_id=coordinator_resume_plan.id,
            entity_id=coordinator_resume_plan.entity_id,
            step_key="coordinator_child",
            kind="human",
            params={"_coordinator_child": True},
            depends_on=[],
            step_status="done",
            result={"answer": "approved"},
        ),
    ])
    terminal_plan = await _plan(db_session)
    db_session.add(ExecutionStep(
        id=generate_ulid(),
        plan_id=terminal_plan.id,
        entity_id=terminal_plan.entity_id,
        step_key="finished",
        kind="llm",
        params={},
        depends_on=[],
        step_status="done",
        result={"text": "done"},
    ))
    await db_session.commit()
    sent: list[dict] = []

    def assert_committed_before_publish(_name, **kwargs):
        if _name != PLAN_CYCLE_OUTBOX_EVENT:
            return
        import os

        import psycopg2
        from sqlalchemy.engine import make_url

        url = make_url(os.environ["TEST_DATABASE_URL"])
        with psycopg2.connect(
            dbname=url.database,
            user=url.username,
            password=url.password,
            host=url.host,
            port=url.port,
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM runtime_outbox_events WHERE id = %s",
                (kwargs["kwargs"]["event_id"],),
            )
            assert cursor.fetchone() == (1,)
        sent.append(kwargs)

    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        assert_committed_before_publish,
    )

    assert await recover_stalled_plan_cycle_wakes_once(batch_size=20) >= 1
    assert await dispatch_runtime_outbox_once(batch_size=20) >= 1
    sent_plan_ids = {item["kwargs"]["plan_id"] for item in sent}
    assert terminal_plan.id in sent_plan_ids
    assert coordinator_resume_plan.id in sent_plan_ids
    assert not waiting_plan_ids & sent_plan_ids
    event = await _event(db_session, terminal_plan.id)
    assert event.delivered_at is None


@pytest.mark.asyncio
async def test_backstop_age_fallback_repairs_unstamped_state_change(
    db_session,
    monkeypatch,
):
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    plan = await _plan(db_session)
    plan.updated_at = old
    db_session.add(ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=plan.entity_id,
        step_key="runnable",
        kind="llm",
        params={},
        depends_on=[],
        step_status="pending",
        updated_at=old,
    ))
    db_session.add(RuntimeOutboxEvent(
        event_type=PLAN_CYCLE_OUTBOX_EVENT,
        aggregate_id=plan.id,
        dedupe_key=plan_cycle_outbox_dedupe_key(plan.id),
        payload={
            "version": 1,
            "plan_id": plan.id,
            "wake_generation": generate_ulid(),
            "reason": "old_ack",
        },
        available_at=old,
        delivered_at=old + timedelta(minutes=1),
    ))
    await db_session.commit()
    sent: list[dict] = []

    def capture_plan_cycle(name, **kwargs):
        if name == PLAN_CYCLE_OUTBOX_EVENT:
            sent.append(kwargs)

    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        capture_plan_cycle,
    )

    assert await recover_stalled_plan_cycle_wakes_once(batch_size=20) >= 1
    assert await dispatch_runtime_outbox_once(batch_size=20) >= 1
    assert plan.id in {item["kwargs"]["plan_id"] for item in sent}
    event = await _event(db_session, plan.id)
    assert event.delivered_at is None
    assert event.payload["reason"] == "stalled_plan_backstop"


@pytest.mark.asyncio
async def test_backstop_does_not_apply_relative_sleep_delay_twice(db_session):
    plan = await _plan(db_session)
    db_session.add(ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=plan.entity_id,
        step_key="relative_sleep",
        kind="sleep",
        params={"seconds": 300},
        depends_on=[],
        step_status="pending",
    ))
    await db_session.commit()
    before = datetime.now(timezone.utc)

    assert await recover_stalled_plan_cycle_wakes_once(batch_size=20) >= 1

    event = await _event(db_session, plan.id)
    assert event.payload["reason"] == "stalled_plan_backstop"
    assert event.available_at < before + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_hot_plan_rows_cannot_starve_non_plan_outbox_events(
    db_session,
    monkeypatch,
):
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.commit()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    for _index in range(5):
        plan = await _plan(db_session)
        wake = await stage_plan_cycle_wake(db_session, plan.id, reason="hot_plan")
        assert wake is not None
        event = await _event(db_session, plan.id)
        event.available_at = old
        event.created_at = old
    generic = RuntimeOutboxEvent(
        event_type="runtime.allocate_sandbox",
        aggregate_id=generate_ulid(),
        dedupe_key=f"fairness:{generate_ulid()}",
        payload={"reservation_id": generate_ulid(), "reservation_version": 1},
        available_at=old + timedelta(minutes=1),
        created_at=old + timedelta(minutes=1),
    )
    db_session.add(generic)
    await db_session.commit()
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        lambda name, **kwargs: sent.append((name, kwargs)),
    )

    assert await dispatch_runtime_outbox_once(batch_size=2) == 2
    assert any(name == "runtime.allocate_sandbox" for name, _kwargs in sent)


@pytest.mark.asyncio
async def test_generic_backlog_cannot_starve_plan_cycle_events(
    db_session,
    monkeypatch,
):
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.commit()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    for index in range(5):
        db_session.add(RuntimeOutboxEvent(
            event_type="runtime.allocate_sandbox",
            aggregate_id=generate_ulid(),
            dedupe_key=f"generic-backlog:{index}:{generate_ulid()}",
            payload={"reservation_id": generate_ulid(), "reservation_version": 1},
            available_at=old,
            created_at=old,
        ))
    plan = await _plan(db_session)
    wake = await stage_plan_cycle_wake(db_session, plan.id, reason="must_progress")
    assert wake is not None
    plan_event = await _event(db_session, plan.id)
    plan_event.available_at = old + timedelta(minutes=1)
    plan_event.created_at = old + timedelta(minutes=1)
    await db_session.commit()
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        lambda name, **kwargs: sent.append((name, kwargs)),
    )

    assert await dispatch_runtime_outbox_once(batch_size=2) == 2
    assert any(
        name == PLAN_CYCLE_OUTBOX_EVENT
        and kwargs["kwargs"]["plan_id"] == plan.id
        for name, kwargs in sent
    )
