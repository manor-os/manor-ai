"""Durable recovery and fencing for Agent Initiative delivery intents."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.contracts.agent_initiative import (
    DeliveryPolicy,
    InterruptionPolicy,
)
from packages.core.models.agent_initiative import (
    AgentInitiative,
    AgentInitiativeDelivery,
    AgentInitiativeRun,
    AgentInitiativeState,
)
from packages.core.models.base import generate_ulid
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.workspace import AgentSubscription
from packages.core.services.agent_initiative_delivery import (
    _deliver_notification,
    claim_initiative_delivery_run,
    deliver_initiative_run,
    pending_initiative_delivery_run_ids,
    record_initiative_delivery_claim_failure,
)


pytestmark = pytest.mark.oss_regression


async def _seed_initiative(db, suffix: str) -> AgentInitiative:
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=f"entity-{suffix}",
        agent_id=f"agent-{suffix}",
        owner_user_id=f"owner-{suffix}",
        name=f"Agent {suffix}",
        status="active",
    )
    initiative = AgentInitiative(
        id=generate_ulid(),
        entity_id=subscription.entity_id,
        workspace_id=None,
        agent_subscription_id=subscription.id,
        owner_user_id=subscription.owner_user_id,
        name=f"Initiative {suffix}",
        instruction="Report a material update.",
        trigger_kind="interval",
        trigger_config={
            "version": "1",
            "kind": "interval",
            "every_seconds": 3600,
            "timezone": "UTC",
        },
        condition_config={"version": "1"},
        decision_policy={"version": "1", "mode": "always"},
        delivery_policy={
            "version": "1",
            "surface": {
                "kind": "conversation",
                "conversation_id": f"conversation-{suffix}",
            },
            "notification": {"enabled": False},
        },
        interruption_policy={
            "version": "1",
            "quiet_hours": {"mode": "none"},
        },
        status="active",
        revision=1,
    )
    db.add(subscription)
    await db.flush()
    db.add(initiative)
    await db.flush()
    db.add(AgentInitiativeState(initiative_id=initiative.id, version=1))
    await db.flush()
    return initiative


def _run(
    initiative: AgentInitiative,
    *,
    occurrence: str,
    created_at: datetime,
) -> AgentInitiativeRun:
    return AgentInitiativeRun(
        id=generate_ulid(),
        initiative_id=initiative.id,
        occurrence_key=occurrence,
        trigger_type="scheduled",
        status="succeeded",
        candidate_decision={
            "version": "1",
            "decision": "notify",
            "message": "Material update.",
            "reason_code": "material_update",
            "severity": "info",
            "evidence_refs": [],
        },
        final_decision="notify",
        message="Material update.",
        completed_at=created_at,
        created_at=created_at,
    )


def _delivery(
    run: AgentInitiativeRun,
    *,
    kind: str,
    due_at: datetime,
    status: str = "pending",
    attempt_count: int = 0,
) -> AgentInitiativeDelivery:
    return AgentInitiativeDelivery(
        id=generate_ulid(),
        initiative_run_id=run.id,
        kind=kind,
        recipient_user_id="owner",
        status=status,
        idempotency_key=f"initiative:{run.id}:{kind}",
        attempt_count=attempt_count,
        next_attempt_at=due_at,
        created_at=run.created_at,
    )


async def _persist_run(db, run: AgentInitiativeRun, *deliveries) -> None:
    db.add(run)
    await db.flush()
    db.add_all(list(deliveries))


@pytest.mark.asyncio
async def test_delivery_claims_chat_then_notification_without_borrowing_attempts(
    db_session,
) -> None:
    now = datetime.now(timezone.utc)
    initiative = await _seed_initiative(db_session, "ordered-claims")
    run = _run(initiative, occurrence="ordered", created_at=now)
    surface = _delivery(run, kind="conversation", due_at=now)
    notification = _delivery(run, kind="notification", due_at=now)
    await _persist_run(db_session, run, surface, notification)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    surface_claim = await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    )
    assert surface_claim is not None
    assert surface_claim.kind == "conversation"
    assert await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    ) is None

    await db_session.execute(
        update(AgentInitiativeDelivery)
        .where(
            AgentInitiativeDelivery.id == surface.id,
            AgentInitiativeDelivery.claim_token == surface_claim.claim_token,
        )
        .values(status="delivered", locked_until=None, claim_token=None)
    )
    await db_session.commit()
    notification_claim = await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    )

    assert notification_claim is not None
    assert notification_claim.kind == "notification"
    rows = list((await db_session.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.initiative_run_id == run.id)
        .order_by(AgentInitiativeDelivery.kind)
        .execution_options(populate_existing=True)
    )).scalars().all())
    by_kind = {row.kind: row for row in rows}
    assert by_kind["conversation"].attempt_count == 1
    assert by_kind["notification"].attempt_count == 1


@pytest.mark.asyncio
async def test_delivery_claim_is_lease_fenced_and_stale_worker_cannot_write(
    db_session,
) -> None:
    now = datetime.now(timezone.utc)
    initiative = await _seed_initiative(db_session, "claim-fence")
    run = _run(initiative, occurrence="claim-fence", created_at=now)
    surface = _delivery(run, kind="conversation", due_at=now)
    await _persist_run(db_session, run, surface)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    old_claim = await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    )
    assert old_claim is not None
    await db_session.execute(
        update(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.id == surface.id)
        .values(locked_until=now - timedelta(seconds=1))
    )
    await db_session.commit()
    new_claim = await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    )
    assert new_claim is not None
    assert new_claim.claim_token != old_claim.claim_token

    stale = await record_initiative_delivery_claim_failure(
        sessions,
        run_id=run.id,
        claim_token=old_claim.claim_token,
        error=RuntimeError("stale worker"),
        now=now,
    )
    assert stale == "claim_lost"
    current = (await db_session.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.id == surface.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert current.status == "processing"
    assert current.claim_token == new_claim.claim_token
    assert current.attempt_count == 2

    recovered = await record_initiative_delivery_claim_failure(
        sessions,
        run_id=run.id,
        claim_token=new_claim.claim_token,
        error=RuntimeError("temporary outage"),
        now=now,
    )
    assert recovered == "retry_scheduled"
    await db_session.refresh(current)
    assert current.status == "error"
    assert current.claim_token is None
    assert current.locked_until is None
    assert current.next_attempt_at > now


@pytest.mark.asyncio
async def test_delivery_retry_limit_quarantines_only_the_attempted_step(
    db_session,
) -> None:
    now = datetime.now(timezone.utc)
    initiative = await _seed_initiative(db_session, "retry-limit")
    run = _run(initiative, occurrence="retry-limit", created_at=now)
    surface = _delivery(
        run,
        kind="conversation",
        due_at=now,
        status="error",
        attempt_count=4,
    )
    notification = _delivery(run, kind="notification", due_at=now)
    await _persist_run(db_session, run, surface, notification)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    claim = await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    )
    assert claim is not None and claim.kind == "conversation"
    outcome = await record_initiative_delivery_claim_failure(
        sessions,
        run_id=run.id,
        claim_token=claim.claim_token,
        error=RuntimeError("persistent outage"),
        now=now,
    )
    assert outcome == "quarantined"

    rows = list((await db_session.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.initiative_run_id == run.id)
        .execution_options(populate_existing=True)
    )).scalars().all())
    by_kind = {row.kind: row for row in rows}
    assert by_kind["conversation"].status == "quarantined"
    assert by_kind["conversation"].attempt_count == 5
    assert by_kind["conversation"].suppressed_reason == "delivery_retry_exhausted"
    assert by_kind["notification"].status == "pending"
    assert by_kind["notification"].attempt_count == 0


@pytest.mark.asyncio
async def test_delivery_sweep_backoff_prevents_old_poison_rows_from_starving_new_work(
    db_session,
) -> None:
    now = datetime.now(timezone.utc)
    # The recovery sweep is intentionally global. This DB fixture preserves
    # committed rows across tests, so terminalize earlier tests' unfinished
    # delivery intents before asserting this batch's exact membership.
    await db_session.execute(
        update(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.status.in_(("pending", "error", "processing")))
        .values(
            status="quarantined",
            suppressed_reason="test_fixture_isolation",
            locked_until=None,
            claim_token=None,
            delivered_at=now,
        )
    )
    await db_session.commit()
    initiative = await _seed_initiative(db_session, "fair-sweep")
    poison_run_ids: list[str] = []
    deliveries: list[AgentInitiativeDelivery] = []
    for index in range(101):
        run = _run(
            initiative,
            occurrence=f"poison-{index}",
            created_at=now - timedelta(days=1) + timedelta(microseconds=index),
        )
        poison_run_ids.append(run.id)
        db_session.add(run)
        deliveries.append(_delivery(
            run,
            kind="conversation",
            due_at=now,
            status="error",
        ))
    healthy = _run(initiative, occurrence="healthy", created_at=now)
    db_session.add(healthy)
    deliveries.append(_delivery(healthy, kind="conversation", due_at=now))
    await db_session.flush()
    db_session.add_all(deliveries)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    listed_ids = await pending_initiative_delivery_run_ids(
        sessions,
        limit=100,
        now=now,
    )
    assert len(listed_ids) == 100
    assert healthy.id not in set(listed_ids)

    await db_session.execute(
        update(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.initiative_run_id.in_(listed_ids))
        .values(
            status="error",
            next_attempt_at=now + timedelta(minutes=1),
            locked_until=None,
            claim_token=None,
            error="poison",
        )
    )
    await db_session.commit()
    second = await pending_initiative_delivery_run_ids(
        sessions,
        limit=100,
        now=now,
    )

    second_ids = set(second)
    assert healthy.id in second_ids
    relevant_ids = second_ids.intersection({healthy.id, *poison_run_ids})
    assert relevant_ids == {healthy.id, poison_run_ids[-1]}


@pytest.mark.asyncio
async def test_malformed_persisted_delivery_contract_is_terminal(
    db_session,
) -> None:
    now = datetime.now(timezone.utc)
    initiative = await _seed_initiative(db_session, "bad-contract")
    initiative.delivery_policy = {"version": "1", "surface": {}}
    run = _run(initiative, occurrence="bad-contract", created_at=now)
    surface = _delivery(run, kind="conversation", due_at=now)
    await _persist_run(db_session, run, surface)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    result = await deliver_initiative_run(sessions, run_id=run.id)

    assert result["recovery"] == "quarantined"
    persisted = (await db_session.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.id == surface.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert persisted.status == "quarantined"
    assert persisted.suppressed_reason == "invalid_delivery_contract"
    assert persisted.attempt_count == 1


@pytest.mark.asyncio
async def test_sweep_terminalizes_an_exhausted_stale_lease_without_sixth_attempt(
    db_session,
) -> None:
    now = datetime.now(timezone.utc)
    initiative = await _seed_initiative(db_session, "stale-exhausted")
    run = _run(initiative, occurrence="stale-exhausted", created_at=now)
    surface = _delivery(
        run,
        kind="conversation",
        due_at=now - timedelta(minutes=10),
        status="processing",
        attempt_count=5,
    )
    surface.locked_until = now - timedelta(minutes=1)
    surface.claim_token = "stale-claim"
    await _persist_run(db_session, run, surface)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    due_ids = await pending_initiative_delivery_run_ids(
        sessions,
        now=now,
    )
    claim = await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    )

    assert run.id in due_ids
    assert claim is None
    persisted = (await db_session.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.id == surface.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert persisted.status == "quarantined"
    assert persisted.attempt_count == 5
    assert persisted.claim_token is None
    assert persisted.locked_until is None
    assert persisted.suppressed_reason == (
        "delivery_retry_exhausted_after_worker_interruption"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("lineage_matches", [True, False])
async def test_exhausted_notification_reconciles_only_exact_durable_parent(
    db_session,
    lineage_matches: bool,
) -> None:
    now = datetime.now(timezone.utc)
    suffix = f"notif-reconcile-{'t' if lineage_matches else 'f'}"
    initiative = await _seed_initiative(
        db_session,
        suffix,
    )
    run = _run(
        initiative,
        occurrence=suffix,
        created_at=now,
    )
    run.task_id = generate_ulid()
    surface = _delivery(
        run,
        kind="conversation",
        due_at=now - timedelta(minutes=10),
        status="delivered",
        attempt_count=1,
    )
    surface.message_id = generate_ulid()
    notification_delivery = _delivery(
        run,
        kind="notification",
        due_at=now - timedelta(minutes=10),
        status="processing",
        attempt_count=5,
    )
    notification_delivery.recipient_user_id = initiative.owner_user_id
    notification_delivery.locked_until = now - timedelta(minutes=1)
    notification_delivery.claim_token = "interrupted-final-attempt"
    await _persist_run(db_session, run, surface, notification_delivery)
    await db_session.flush()

    subscription = await db_session.get(
        AgentSubscription,
        initiative.agent_subscription_id,
    )
    assert subscription is not None
    notification = Notification(
        entity_id=initiative.entity_id,
        user_id=initiative.owner_user_id,
        workspace_id=initiative.workspace_id,
        idempotency_key=notification_delivery.idempotency_key,
        type="agent_initiative_update",
        title="Recovered update",
        content="Material update.",
        meta={
            "agent_id": subscription.agent_id,
            "agent_subscription_id": initiative.agent_subscription_id,
            "initiative_id": initiative.id,
            "initiative_run_id": run.id,
            "message_id": (
                surface.message_id if lineage_matches else generate_ulid()
            ),
            "task_id": run.task_id,
        },
        dispatch_status="dispatched",
    )
    db_session.add(notification)
    await db_session.flush()
    db_session.add(NotificationOutboxEvent(
        notification_id=notification.id,
        payload={"channels": ["telegram"]},
        status="pending",
        available_at=now + timedelta(days=1),
    ))
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    claim = await claim_initiative_delivery_run(
        sessions,
        run_id=run.id,
        now=now,
    )

    assert claim is None
    persisted = (await db_session.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.id == notification_delivery.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert persisted.attempt_count == 5
    assert persisted.claim_token is None
    assert persisted.locked_until is None
    if lineage_matches:
        assert persisted.status == "accepted"
        assert persisted.notification_id == notification.id
        assert persisted.suppressed_reason is None
    else:
        assert persisted.status == "quarantined"
        assert persisted.notification_id is None
        assert persisted.suppressed_reason == "notification_idempotency_conflict"


@pytest.mark.asyncio
async def test_deferred_stale_surface_worker_does_not_claim_or_quarantine_retry(
    db_session,
) -> None:
    now = datetime.now(timezone.utc)
    initiative = await _seed_initiative(db_session, "deferred")
    run = _run(initiative, occurrence="deferred-stale-worker", created_at=now)
    surface = _delivery(
        run,
        kind="conversation",
        due_at=now - timedelta(minutes=1),
        status="error",
        attempt_count=1,
    )
    await _persist_run(db_session, run, surface)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    result = await deliver_initiative_run(
        sessions,
        run_id=run.id,
        kind="conversation",
        claim_token="stale-worker-token",
    )

    assert result == {
        "run_id": run.id,
        "surface": "deferred",
        "notification": "not_attempted",
    }
    persisted = (await db_session.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.id == surface.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert persisted.status == "error"
    assert persisted.attempt_count == 1
    assert persisted.claim_token is None


@pytest.mark.asyncio
async def test_stale_notification_worker_cannot_revive_newer_terminal_decision(
    monkeypatch,
) -> None:
    from packages.core.services import agent_initiative_delivery as delivery_service
    from packages.core.services import notify as notify_service

    delivery = SimpleNamespace(
        status="processing",
        idempotency_key="initiative:run-1:notification",
    )
    context = SimpleNamespace(
        initiative=SimpleNamespace(
            id="initiative-1",
            entity_id="entity-1",
            owner_user_id="user-1",
            workspace_id=None,
            agent_subscription_id="subscription-1",
            name="Digest",
        ),
        run=SimpleNamespace(id="run-1", message="Update", task_id="task-1"),
        delivery=delivery,
        delivery_policy=DeliveryPolicy.model_validate({
            "version": "1",
            "surface": {
                "kind": "conversation",
                "conversation_id": "conversation-1",
            },
            "notification": {"enabled": True, "channels": ["inapp"]},
        }),
        interruption_policy=InterruptionPolicy.model_validate({
            "version": "1",
            "quiet_hours": {"mode": "none"},
        }),
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(status="delivered", message_id="message-1")

    class FakeDb:
        commits = 0

        async def execute(self, _statement):
            return ScalarResult()

        async def rollback(self):
            return None

        async def commit(self):
            self.commits += 1

    class SessionContext:
        def __init__(self):
            self.db = FakeDb()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_args):
            return None

    async def load_context(*_args, **_kwargs):
        return context

    async def validate_authority(*_args, **_kwargs):
        return (
            SimpleNamespace(agent_id="agent-1"),
            SimpleNamespace(preferences={}),
            SimpleNamespace(id="conversation-1"),
        )

    async def no_existing_notification(*_args, **_kwargs):
        return None

    async def notify_then_lose_lease(**_kwargs):
        delivery.status = "quarantined"

    monkeypatch.setattr(delivery_service, "_load_locked_context", load_context)
    monkeypatch.setattr(
        delivery_service,
        "_validate_context_authority",
        validate_authority,
    )
    monkeypatch.setattr(
        delivery_service,
        "_notification_already_exists",
        no_existing_notification,
    )
    monkeypatch.setattr(notify_service, "notify", notify_then_lose_lease)

    status = await _deliver_notification(
        lambda: SessionContext(),
        run_id="run-1",
        claim_token="stale-token",
    )

    assert status == "quarantined"
    assert delivery.status == "quarantined"


def test_recovery_sweep_hands_off_run_id_without_spending_attempt(monkeypatch) -> None:
    from packages.core import database
    from packages.core.services import agent_initiative_delivery as delivery_service
    from packages.core.tasks import ai_tasks

    async def due_run_ids(_session_factory):
        return ["run-1", "run-2"]

    handed_off: list[tuple[tuple, dict]] = []

    def enqueue(*args, **kwargs):
        handed_off.append((args, kwargs))
        if args == ("run-2",):
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(
        delivery_service,
        "pending_initiative_delivery_run_ids",
        due_run_ids,
    )
    monkeypatch.setattr(ai_tasks, "_enqueue_agent_initiative_delivery", enqueue)

    result = ai_tasks.recover_pending_agent_initiative_deliveries.run()

    assert handed_off == [(("run-1",), {}), (("run-2",), {})]
    assert result == {"found": 2, "queued": 1, "publish_failed": 1}
