"""Dispatcher state, Chat receipts, and TaskLog commit as one unit."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.constants.execution import WorkLeaseStatus
from packages.core.dispatcher.service import Dispatcher, LeaseNotActive
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Conversation, Message, Task, TaskLog
from packages.core.models.worker import WorkLease, Worker
from packages.core.models.workspace import Workspace
from packages.core.workspace_chat import service as chat_service


async def _counts(session_factory, *, entity_id: str, workspace_id: str, task_id: str):
    async with session_factory() as db:
        message_count = await db.scalar(
            select(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id,
            )
        )
        log_count = await db.scalar(
            select(func.count(TaskLog.id)).where(TaskLog.task_id == task_id)
        )
        return int(message_count or 0), int(log_count or 0)


async def _message_count(session_factory, *, entity_id: str, workspace_id: str) -> int:
    async with session_factory() as db:
        count = await db.scalar(
            select(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id,
            )
        )
        return int(count or 0)


@pytest.mark.asyncio
async def test_complete_lease_receipts_rollback_commit_and_do_not_duplicate(
    db_session,
    monkeypatch,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    now = datetime.now(timezone.utc)

    db_session.add_all(
        [
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Atomic dispatcher receipts",
                status="active",
                operating_model={},
                settings={},
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Atomic receipt task",
                status="in_progress",
                details={},
                delegate_service_keys=[],
            ),
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Atomic receipt worker",
                capabilities={"supported_kinds": ["action"]},
                preferences={},
                status="active",
                consecutive_failures=0,
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
            ),
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="publish",
                kind="action",
                params={"prompt": "publish"},
                depends_on=[],
                step_status="running",
                risk_level="low",
                attempt_count=1,
                max_attempts=3,
                current_lease_id=lease_id,
                started_at=now,
            ),
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                worker_id=worker_id,
                status=WorkLeaseStatus.ACTIVE.value,
                lease_until=now + timedelta(minutes=5),
                credential_leases=[],
            ),
        ]
    )
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    publish = AsyncMock()
    monkeypatch.setattr(chat_service, "_publish_workspace_chat_event", publish)

    post_message = chat_service.post_message

    async def fail_post(*_args, **_kwargs):
        raise RuntimeError("chat persistence failed")

    monkeypatch.setattr(chat_service, "post_message", fail_post)
    with pytest.raises(RuntimeError, match="chat persistence failed"):
        await Dispatcher().complete_lease(
            db_session,
            lease_id,
            result={"text": "Must not commit without its receipt"},
        )
    await db_session.rollback()
    monkeypatch.setattr(chat_service, "post_message", post_message)

    async with session_factory() as probe:
        assert await probe.scalar(
            select(WorkLease.status).where(WorkLease.id == lease_id)
        ) == WorkLeaseStatus.ACTIVE.value
    assert await _counts(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    ) == (0, 0)

    await Dispatcher().complete_lease(
        db_session,
        lease_id,
        result={"text": "Published once"},
    )
    assert await _counts(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    ) == (0, 0)

    await db_session.rollback()
    assert await _counts(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    ) == (0, 0)

    await Dispatcher().complete_lease(
        db_session,
        lease_id,
        result={"text": "Published once"},
    )
    await db_session.commit()
    await asyncio.sleep(0)
    assert await _counts(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    ) == (2, 1)
    assert publish.await_count == 2

    async with session_factory() as duplicate_db:
        with pytest.raises(LeaseNotActive):
            await Dispatcher().complete_lease(
                duplicate_db,
                lease_id,
                result={"text": "Published twice"},
            )
        await duplicate_db.rollback()

    assert await _counts(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    ) == (2, 1)


@pytest.mark.asyncio
async def test_hitl_receipts_request_and_event_commit_as_one_unit(
    db_session,
    monkeypatch,
):
    from packages.core.models.event import EventLog
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.services import event_emitter

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    now = datetime.now(timezone.utc)

    db_session.add_all(
        [
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Atomic HITL receipts",
                status="active",
                operating_model={},
                settings={},
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Atomic HITL task",
                status="in_progress",
                details={},
                delegate_service_keys=[],
            ),
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Atomic HITL worker",
                capabilities={"supported_kinds": ["subagent"]},
                preferences={},
                status="active",
                consecutive_failures=0,
            ),
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="publish",
                kind="subagent",
                params={"prompt": "publish"},
                depends_on=[],
                step_status="running",
                risk_level="medium",
                capability_id="browser.control",
                attempt_count=1,
                max_attempts=3,
                current_lease_id=lease_id,
                started_at=now,
            ),
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                worker_id=worker_id,
                status=WorkLeaseStatus.ACTIVE.value,
                lease_until=now + timedelta(minutes=5),
                credential_leases=[],
            ),
        ]
    )
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    publish = AsyncMock()
    monkeypatch.setattr(chat_service, "_publish_workspace_chat_event", publish)
    monkeypatch.setattr(
        event_emitter,
        "_deliver_committed_external_events",
        AsyncMock(),
    )

    async def durable_counts() -> tuple[int, int, int, int]:
        async with session_factory() as probe:
            message_count = await probe.scalar(
                select(func.count(Message.id))
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.entity_id == entity_id,
                    Conversation.workspace_id == workspace_id,
                )
            )
            log_count = await probe.scalar(
                select(func.count(TaskLog.id)).where(TaskLog.task_id == task_id)
            )
            request_count = await probe.scalar(
                select(func.count(HitlRequest.id)).where(
                    HitlRequest.origin_step_id == step_id
                )
            )
            event_count = await probe.scalar(
                select(func.count(EventLog.id)).where(
                    EventLog.entity_id == entity_id,
                    EventLog.event_type == "task.hitl_requested",
                )
            )
            return (
                int(message_count or 0),
                int(log_count or 0),
                int(request_count or 0),
                int(event_count or 0),
            )

    await Dispatcher().lease_needs_human(
        db_session,
        lease_id,
        prompt="Confirm the external publish",
        pending_action={"kind": "needs_confirmation"},
    )
    assert await durable_counts() == (0, 0, 0, 0)
    await db_session.rollback()
    assert await durable_counts() == (0, 0, 0, 0)
    assert publish.await_count == 0

    await Dispatcher().lease_needs_human(
        db_session,
        lease_id,
        prompt="Confirm the external publish",
        pending_action={"kind": "needs_confirmation"},
    )
    await db_session.commit()
    await asyncio.sleep(0)
    assert await durable_counts() == (1, 1, 1, 1)
    assert publish.await_count == 1

    async with session_factory() as duplicate_db:
        with pytest.raises(LeaseNotActive):
            await Dispatcher().lease_needs_human(
                duplicate_db,
                lease_id,
                prompt="Do not duplicate",
                pending_action={"kind": "needs_confirmation"},
            )
        await duplicate_db.rollback()
    assert await durable_counts() == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_complete_lease_rolls_back_when_budget_alert_cannot_persist(
    db_session,
    monkeypatch,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Atomic dispatcher budget alert",
                status="active",
                operating_model={},
                settings={},
                monthly_budget_usd=Decimal("1"),
                monthly_spent_usd=Decimal("0.79"),
                auto_pause_on_budget=True,
                budget_alert_state="normal",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Atomic budget completion",
                status="in_progress",
                details={},
                delegate_service_keys=[],
            ),
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Atomic budget worker",
                capabilities={"supported_kinds": ["action"]},
                preferences={},
                status="active",
                consecutive_failures=0,
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
            ),
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="paid_action",
                kind="action",
                params={"prompt": "publish"},
                depends_on=[],
                step_status="running",
                risk_level="low",
                attempt_count=1,
                max_attempts=3,
                current_lease_id=lease_id,
                started_at=now,
            ),
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                worker_id=worker_id,
                status=WorkLeaseStatus.ACTIVE.value,
                lease_until=now + timedelta(minutes=5),
                credential_leases=[],
            ),
        ]
    )
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    post_message = chat_service.post_message

    async def fail_budget_alert(db, **kwargs):
        if kwargs.get("message_kind") == "goal_alert":
            raise RuntimeError("budget alert write failed")
        return await post_message(db, **kwargs)

    monkeypatch.setattr(chat_service, "post_message", fail_budget_alert)
    with pytest.raises(RuntimeError, match="budget alert write failed"):
        await Dispatcher().complete_lease(
            db_session,
            lease_id,
            result={"text": "Paid publish"},
            cost={"usd": 0.02},
        )
    await db_session.rollback()

    async with session_factory() as probe:
        lease = await probe.get(WorkLease, lease_id)
        step = await probe.get(ExecutionStep, step_id)
        workspace = await probe.get(Workspace, workspace_id)
        assert lease is not None and lease.status == WorkLeaseStatus.ACTIVE.value
        assert step is not None and step.step_status == "running"
        assert workspace is not None
        assert workspace.monthly_spent_usd == Decimal("0.79")
        assert workspace.budget_alert_state == "normal"
    assert await _counts(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    ) == (0, 0)

    monkeypatch.setattr(chat_service, "post_message", post_message)
    await Dispatcher().complete_lease(
        db_session,
        lease_id,
        result={"text": "Paid publish"},
        cost={"usd": 0.02},
    )
    await db_session.commit()
    await asyncio.sleep(0)

    async with session_factory() as probe:
        lease = await probe.get(WorkLease, lease_id)
        workspace = await probe.get(Workspace, workspace_id)
        assert lease is not None and lease.status == WorkLeaseStatus.COMPLETED.value
        assert workspace is not None
        assert workspace.monthly_spent_usd == Decimal("0.81")
        assert workspace.budget_alert_state == "warning_80"
    assert await _counts(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    ) == (3, 1)


@pytest.mark.asyncio
async def test_budget_alert_failure_rollback_and_retry_emit_once(
    db_session,
    monkeypatch,
):
    from packages.core.budget import aggregation

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Atomic budget warning",
        status="active",
        operating_model={},
        settings={},
        monthly_budget_usd=Decimal("1"),
        monthly_spent_usd=Decimal("0.79"),
        auto_pause_on_budget=True,
        budget_alert_state="normal",
    )
    db_session.add(workspace)
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    publish = AsyncMock()
    monkeypatch.setattr(chat_service, "_publish_workspace_chat_event", publish)

    post_message = chat_service.post_message

    async def fail_post(*_args, **_kwargs):
        raise RuntimeError("budget alert persistence failed")

    monkeypatch.setattr(chat_service, "post_message", fail_post)
    with pytest.raises(RuntimeError, match="budget alert persistence failed"):
        await aggregation.accumulate_workspace_ai_cost(
            db_session,
            workspace_id=workspace_id,
            cost_usd=0.02,
        )
    await db_session.rollback()
    monkeypatch.setattr(chat_service, "post_message", post_message)

    async with session_factory() as probe:
        persisted = await probe.get(Workspace, workspace_id)
        assert persisted is not None
        assert persisted.monthly_spent_usd == Decimal("0.79")
        assert persisted.budget_alert_state == "normal"
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 0
    assert publish.await_count == 0

    result = await aggregation.accumulate_workspace_ai_cost(
        db_session,
        workspace_id=workspace_id,
        cost_usd=0.02,
    )
    assert result["alert_emitted"] == "warning_80"
    await db_session.commit()
    await asyncio.sleep(0)

    async with session_factory() as probe:
        persisted = await probe.get(Workspace, workspace_id)
        assert persisted is not None
        assert persisted.monthly_spent_usd == Decimal("0.81")
        assert persisted.budget_alert_state == "warning_80"
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 1
    assert publish.await_count == 1

    result = await aggregation.accumulate_workspace_ai_cost(
        db_session,
        workspace_id=workspace_id,
        cost_usd=0.01,
    )
    assert result["alert_emitted"] is None
    await db_session.commit()
    await asyncio.sleep(0)
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 1
    assert publish.await_count == 1


@pytest.mark.asyncio
async def test_llm_usage_budget_alert_failure_propagates_when_strict_and_isolated_otherwise(
    db_session,
    monkeypatch,
):
    from packages.core.services import billing_service, usage_service

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Atomic LLM settlement",
            status="active",
            operating_model={},
            settings={},
            monthly_budget_usd=Decimal("1"),
            monthly_spent_usd=Decimal("0.79"),
            auto_pause_on_budget=True,
            budget_alert_state="normal",
        )
    )
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    monkeypatch.setattr(usage_service, "log_token_usage", AsyncMock())
    monkeypatch.setattr(billing_service, "record_token_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "packages.core.services.plan_enforcement.record_ai_cost",
        AsyncMock(),
    )

    async def fail_post(*_args, **_kwargs):
        raise RuntimeError("LLM budget alert write failed")

    monkeypatch.setattr(chat_service, "post_message", fail_post)
    usage = {
        "model": "test-model",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cost_usd": 0.02,
    }

    with pytest.raises(RuntimeError, match="LLM budget alert write failed"):
        await usage_service.record_llm_usage(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace_id,
            usage=usage,
            source="runtime",
            strict=True,
        )
    await db_session.rollback()

    async with session_factory() as probe:
        workspace = await probe.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.monthly_spent_usd == Decimal("0.79")
        assert workspace.budget_alert_state == "normal"
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 0

    # Legacy observers remain best-effort, but the SAVEPOINT prevents them
    # from committing a threshold state whose matching alert rolled back.
    await usage_service.record_llm_usage(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        usage=usage,
        source="observer",
        strict=False,
    )
    await db_session.commit()
    async with session_factory() as probe:
        workspace = await probe.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.monthly_spent_usd == Decimal("0.79")
        assert workspace.budget_alert_state == "normal"
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 0


@pytest.mark.asyncio
async def test_strict_media_usage_rolls_back_ledger_and_budget_alert_then_retries_once(
    db_session,
    monkeypatch,
):
    from packages.core.models.billing import CreditUsageLog
    from packages.core.models.user import Entity
    from packages.core.services import usage_service

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add_all(
        [
            Entity(id=entity_id, name="Atomic media entity", settings={}),
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Atomic media settlement",
                status="active",
                operating_model={},
                settings={},
                # OSS media accounting floors a positive call at one display
                # credit (= $0.001 with the test rate), enough to cross this cap.
                monthly_budget_usd=Decimal("0.001"),
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
                budget_alert_state="normal",
            ),
        ]
    )
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(
        "packages.core.services.plan_enforcement.record_ai_cost",
        AsyncMock(),
    )
    publish = AsyncMock()
    monkeypatch.setattr(chat_service, "_publish_workspace_chat_event", publish)
    post_message = chat_service.post_message

    async def fail_post(*_args, **_kwargs):
        raise RuntimeError("media budget alert write failed")

    monkeypatch.setattr(chat_service, "post_message", fail_post)
    with pytest.raises(RuntimeError, match="media budget alert write failed"):
        await usage_service.record_media_usage(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace_id,
            kind="video",
            model="test-video",
            cost_usd=0.02,
            units=1,
            strict=True,
        )
    await db_session.rollback()

    async with session_factory() as probe:
        workspace = await probe.get(Workspace, workspace_id)
        usage_count = await probe.scalar(
            select(func.count(CreditUsageLog.id)).where(
                CreditUsageLog.entity_id == entity_id
            )
        )
        assert workspace is not None
        assert workspace.monthly_spent_usd == Decimal("0")
        assert workspace.budget_alert_state == "normal"
        assert int(usage_count or 0) == 0
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 0
    assert publish.await_count == 0

    monkeypatch.setattr(chat_service, "post_message", post_message)
    assert await usage_service.record_media_usage(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        kind="video",
        model="test-video",
        cost_usd=0.02,
        units=1,
        strict=True,
    ) is True
    await db_session.commit()
    await asyncio.sleep(0)

    async with session_factory() as probe:
        workspace = await probe.get(Workspace, workspace_id)
        usage_count = await probe.scalar(
            select(func.count(CreditUsageLog.id)).where(
                CreditUsageLog.entity_id == entity_id
            )
        )
        assert workspace is not None
        assert workspace.budget_alert_state == "critical_100"
        assert int(usage_count or 0) == 1
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 1
    assert publish.await_count == 1


@pytest.mark.asyncio
async def test_chat_realtime_queue_waits_for_outer_commit_and_tracks_savepoints(
    db_session,
    monkeypatch,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Savepoint chat queue",
            status="active",
            operating_model={},
            settings={},
        )
    )
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    publish = AsyncMock()
    monkeypatch.setattr(chat_service, "_publish_workspace_chat_event", publish)

    nested = await db_session.begin_nested()
    rolled_back_outer = await chat_service.post_message(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        body="Nested commit, outer rollback",
        publish_event=False,
    )
    chat_service.queue_workspace_chat_message_event_after_commit(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        message=rolled_back_outer,
    )
    await nested.commit()
    await asyncio.sleep(0)
    assert publish.await_count == 0
    await db_session.rollback()
    await asyncio.sleep(0)
    assert publish.await_count == 0
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 0

    outer_message = await chat_service.post_message(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        body="Outer survives",
        publish_event=False,
    )
    chat_service.queue_workspace_chat_message_event_after_commit(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        message=outer_message,
    )
    nested = await db_session.begin_nested()
    nested_message = await chat_service.post_message(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        body="Nested rolls back",
        publish_event=False,
    )
    chat_service.queue_workspace_chat_message_event_after_commit(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        message=nested_message,
    )
    await nested.rollback()
    await db_session.commit()
    await asyncio.sleep(0)

    assert publish.await_count == 1
    assert publish.await_args.args[1]["message_id"] == outer_message.id
    assert await _message_count(
        session_factory,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ) == 1
