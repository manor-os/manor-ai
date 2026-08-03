"""M1 WorkspaceEventLedger — record_event service + workspace_events model.

Covers the append-only ledger discipline:
* same-transaction persistence with server-side ingested_at
* idempotent append (SAVEPOINT keeps the caller's transaction usable)
* closed event-type vocabulary
* 8KB payload cap with truncation marker
* ULID ids are time-ordered (they are the review watermark cursor)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from packages.core.ledger import ALL_EVENT_TYPES, record_event
from packages.core.ledger import event_types as et
from packages.core.models.workspace_event import WorkspaceEvent


ENTITY_ID = "01TESTENTITY00000000000000"
WORKSPACE_ID = "01TESTWORKSPACE00000000000"


async def _count_events(db, *entity_ids: str) -> int:
    """Count ledger rows for the given entities only.

    The CI smoke run executes the whole test tree against one shared database,
    and many suites now commit ledger rows through the adapters — a global
    COUNT(*) picks those up. Scope every assertion to this file's entities.
    """
    stmt = select(func.count()).select_from(WorkspaceEvent)
    stmt = stmt.where(WorkspaceEvent.entity_id.in_(entity_ids or (ENTITY_ID,)))
    return (await db.execute(stmt)).scalar_one()


def _base_kwargs(**overrides):
    kwargs = dict(
        entity_id=ENTITY_ID,
        workspace_id=WORKSPACE_ID,
        event_type=et.EXECUTION_COMPLETED,
        source_kind="task",
        source_id="task_123",
        idempotency_key="task_123:execution_completed",
    )
    kwargs.update(overrides)
    return kwargs


async def test_record_event_persists_row_with_all_fields(db_session):
    occurred = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
    event = await record_event(
        db_session,
        **_base_kwargs(occurred_at=occurred),
        run_id="run_1",
        root_execution_id="batch_9",
        causation_id="pi_7",
        correlation_id="daily-video:2026-07-23",
        actor_kind="agent",
        actor_id="agent_5",
        status="succeeded",
        goal_refs=["goal:g1"],
        output_refs=["artifact:doc_x"],
        evidence_refs=["runtime_event:re_1"],
        config_versions={"automation_revision": 3},
        payload={"summary": "done"},
        period_key="2026-07-23",
    )

    assert event is not None
    assert event.id and len(event.id) == 26

    row = (
        await db_session.execute(select(WorkspaceEvent).where(WorkspaceEvent.id == event.id))
    ).scalar_one()
    assert row.entity_id == ENTITY_ID
    assert row.workspace_id == WORKSPACE_ID
    assert row.event_type == et.EXECUTION_COMPLETED
    assert row.source_kind == "task"
    assert row.source_id == "task_123"
    assert row.run_id == "run_1"
    assert row.root_execution_id == "batch_9"
    assert row.causation_id == "pi_7"
    assert row.correlation_id == "daily-video:2026-07-23"
    assert row.actor_kind == "agent"
    assert row.actor_id == "agent_5"
    assert row.status == "succeeded"
    assert row.goal_refs == ["goal:g1"]
    assert row.output_refs == ["artifact:doc_x"]
    assert row.evidence_refs == ["runtime_event:re_1"]
    assert row.config_versions == {"automation_revision": 3}
    assert row.payload == {"summary": "done"}
    assert row.period_key == "2026-07-23"
    assert row.idempotency_key == "task_123:execution_completed"
    assert row.occurred_at == occurred
    assert row.ingested_at is not None  # server_default now()


async def test_duplicate_idempotency_key_returns_none_and_keeps_transaction_usable(db_session):
    first = await record_event(db_session, **_base_kwargs())
    assert first is not None

    duplicate = await record_event(db_session, **_base_kwargs())
    assert duplicate is None
    assert await _count_events(db_session) == 1

    # The SAVEPOINT rollback must not poison the caller's transaction:
    # a subsequent different insert in the same session still works.
    third = await record_event(
        db_session,
        **_base_kwargs(
            event_type=et.EXECUTION_STARTED,
            idempotency_key="task_123:execution_started",
        ),
    )
    assert third is not None
    assert await _count_events(db_session) == 2


async def test_same_idempotency_key_different_entity_both_persist(db_session):
    other_entity = "01TESTENTITY22222222222222"
    first = await record_event(db_session, **_base_kwargs())
    second = await record_event(db_session, **_base_kwargs(entity_id=other_entity))

    assert first is not None
    assert second is not None
    assert await _count_events(db_session, ENTITY_ID, other_entity) == 2


async def test_unknown_event_type_raises_value_error(db_session):
    with pytest.raises(ValueError, match="event_type"):
        await record_event(db_session, **_base_kwargs(event_type="totally_made_up"))
    assert "totally_made_up" not in ALL_EVENT_TYPES


async def test_oversized_payload_is_truncated_with_marker(db_session):
    big_payload = {"blob": "x" * 20_000}
    event = await record_event(db_session, **_base_kwargs(), payload=big_payload)

    assert event is not None
    assert event.payload["payload_truncated"] is True
    assert len(event.payload["summary"]) <= 2000
    # The stored payload itself is small now.
    row = (
        await db_session.execute(select(WorkspaceEvent).where(WorkspaceEvent.id == event.id))
    ).scalar_one()
    assert set(row.payload.keys()) == {"payload_truncated", "summary"}


async def test_small_payload_is_stored_verbatim(db_session):
    payload = {"count": 3, "note": "small"}
    event = await record_event(db_session, **_base_kwargs(), payload=payload)
    assert event is not None
    assert event.payload == payload


async def test_ulid_ids_are_monotonically_ordered(db_session):
    first = await record_event(
        db_session, **_base_kwargs(idempotency_key="order:1")
    )
    # ULID ordering is guaranteed across distinct milliseconds; nudge the clock.
    await asyncio.sleep(0.005)
    second = await record_event(
        db_session, **_base_kwargs(idempotency_key="order:2")
    )

    assert first is not None and second is not None
    assert first.id < second.id  # lexicographic == chronological → watermark cursor


async def test_occurred_at_defaults_to_now(db_session):
    before = datetime.now(timezone.utc)
    event = await record_event(db_session, **_base_kwargs())
    after = datetime.now(timezone.utc)

    assert event is not None
    assert before <= event.occurred_at <= after
