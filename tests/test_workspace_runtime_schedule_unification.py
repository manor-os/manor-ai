"""Workspace runtime switch contracts for Strategist and Goal schedules."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _register(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_strategist_activity_is_persisted_as_a_timeline_stage(monkeypatch) -> None:
    import packages.core.database as database
    from packages.core.strategist import service

    posted: list[dict] = []
    events: list[str] = []

    class _SessionContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            events.append("commit")
            return None

    async def _post_message(_db, **kwargs):
        events.append("post")
        posted.append(kwargs)
        return SimpleNamespace(
            id="msg_test",
            message_kind=kwargs["message_kind"],
            author_kind=kwargs["author_kind"],
            pending_action=None,
        )

    async def _publish_message_event(*_args, **_kwargs):
        events.append("publish")

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext())
    monkeypatch.setattr(service.chat_service, "post_message", _post_message)
    monkeypatch.setattr(
        service.chat_service,
        "publish_workspace_chat_message_event",
        _publish_message_event,
    )

    activity_id = await service._post_strategist_activity(
        SimpleNamespace(id="ws_test", entity_id="entity_test"),
        stage="analyzing_context",
        body="Analyzing workspace context",
        review_id="rv_test",
    )

    assert activity_id == "msg_test"
    started_at = posted[0]["meta"]["strategist_activity"]["started_at"]
    assert isinstance(started_at, str)
    assert posted == [{
        "entity_id": "entity_test",
        "workspace_id": "ws_test",
        "body": "Analyzing workspace context",
        "message_kind": "strategist_activity",
        "author_kind": "agent",
        "refs": [{"type": "workspace", "id": "ws_test"}],
        "meta": {
            "strategist_activity": {
                "stage": "analyzing_context",
                "state": "running",
                "started_at": started_at,
                "review_id": "rv_test",
            },
        },
        "publish_event": False,
    }]
    assert events == ["post", "commit", "publish"]


@pytest.mark.asyncio
async def test_strategist_failure_finishes_only_its_review_activity(
    db_session, monkeypatch,
) -> None:
    import packages.core.database as database
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workspace import Workspace
    from packages.core.strategist import service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Activity isolation",
        status="active",
        settings={},
    )
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        scope="workspace_main",
        channel="web",
        status="active",
    )
    review_a_messages = [
        Message(
            id=generate_ulid(),
            conversation_id=conversation.id,
            role="assistant",
            content=stage,
            author_kind="agent",
            message_kind="strategist_activity",
            meta={"strategist_activity": {
                "review_id": "rv_a",
                "stage": stage,
                "state": "running",
                "started_at": "2026-08-20T00:00:00+00:00",
            }},
        )
        for stage in ("collecting_feedback", "analyzing_context")
    ]
    review_b_message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="generating_plan",
        author_kind="agent",
        message_kind="strategist_activity",
        meta={"strategist_activity": {
            "review_id": "rv_b",
            "stage": "generating_plan",
            "state": "running",
            "started_at": "2026-08-20T00:00:00+00:00",
        }},
    )
    db_session.add_all([workspace, conversation, *review_a_messages, review_b_message])
    await db_session.commit()

    async def _publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        database,
        "async_session",
        async_sessionmaker(
            db_session.bind,
            class_=AsyncSession,
            expire_on_commit=False,
        ),
    )
    monkeypatch.setattr(
        service.chat_service,
        "publish_workspace_chat_message_event",
        _publish,
    )

    finished = await service.finish_strategist_review_activity(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id="rv_a",
        state="failed",
    )

    assert finished is True
    for message in review_a_messages:
        await db_session.refresh(message)
        assert message.meta["strategist_activity"]["state"] == "failed"
        assert message.meta["strategist_activity"]["finished_at"]
    await db_session.refresh(review_b_message)
    assert review_b_message.meta["strategist_activity"]["state"] == "running"


@pytest.mark.asyncio
async def test_strategist_recovery_failure_dominates_stale_worker_skip(
    db_session, monkeypatch,
) -> None:
    import packages.core.database as database
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workspace import Workspace
    from packages.core.strategist import service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Activity terminal precedence",
        status="active",
        settings={},
    )
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        scope="workspace_main",
        channel="web",
        status="active",
    )
    activity = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="generating_plan",
        author_kind="agent",
        message_kind="strategist_activity",
        meta={"strategist_activity": {
            "review_id": "rv_recovered",
            "stage": "generating_plan",
            "state": "running",
            "started_at": "2026-08-20T00:00:00+00:00",
        }},
    )
    db_session.add_all([workspace, conversation, activity])
    await db_session.commit()

    async def _publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        database,
        "async_session",
        async_sessionmaker(
            db_session.bind,
            class_=AsyncSession,
            expire_on_commit=False,
        ),
    )
    monkeypatch.setattr(
        service.chat_service,
        "publish_workspace_chat_message_event",
        _publish,
    )

    stale_finished = await service._set_strategist_activity_terminal(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        message_id=activity.id,
        state="skipped",
    )
    recovery_finished = await service.finish_strategist_review_activity(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id="rv_recovered",
        state="failed",
    )
    stale_retry_finished = await service._set_strategist_activity_terminal(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        message_id=activity.id,
        state="skipped",
    )

    await db_session.refresh(activity)
    assert stale_finished is True
    assert recovery_finished is True
    assert stale_retry_finished is False
    assert activity.meta["strategist_activity"]["state"] == "failed"


@pytest.mark.asyncio
async def test_strategist_activity_event_includes_transition_state(monkeypatch) -> None:
    from packages.core.workspace_chat import service as chat_service

    published: list[dict] = []

    async def _publish(_entity_id, data):
        published.append(data)

    monkeypatch.setattr(chat_service, "_publish_workspace_chat_event", _publish)
    await chat_service.publish_workspace_chat_message_event(
        "entity_test",
        workspace_id="ws_test",
        message=SimpleNamespace(
            id="msg_test",
            message_kind="strategist_activity",
            author_kind="agent",
            pending_action=None,
            meta={"strategist_activity": {
                "review_id": "rv_test",
                "stage": "analyzing_context",
                "state": "completed",
            }},
        ),
    )

    assert published[0]["strategist_activity"] == {
        "review_id": "rv_test",
        "stage": "analyzing_context",
        "state": "completed",
    }


@pytest.mark.asyncio
async def test_strategist_review_refreshes_cached_workspace_before_status_gate(
    db_session,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.strategist import service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Concurrent pause",
        status="active",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()
    assert workspace.status == "active"

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as pause_db:
        paused_workspace = await pause_db.get(Workspace, workspace.id)
        assert paused_workspace is not None
        paused_workspace.status = "paused"
        await pause_db.commit()

    # The review session still has the pre-pause object in its identity map.
    assert workspace.status == "active"

    result = await service.run_review(db_session, workspace.id, trigger="manual")

    assert result == {
        "workspace_id": workspace.id,
        "skipped": True,
        "reason": "workspace_inactive",
        "workspace_status": "paused",
    }
    assert workspace.status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["pause", "lease_lost"])
async def test_strategist_review_rechecks_lifecycle_after_model_generation(
    db_session,
    monkeypatch,
    concurrent_change: str,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.workspace import Workspace
    from packages.core.review import begin_review
    from packages.core.strategist import service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Pause during generation",
        status="active",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()
    review = None
    review_kwargs: dict = {}
    if concurrent_change == "lease_lost":
        review = await begin_review(
            db_session,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            trigger="manual",
            lease_owner="worker-a",
        )
        await db_session.commit()
        review_kwargs = {
            "briefing_markdown": "fixed briefing",
            "review_run": review,
            "review_lease_owner": "worker-a",
        }

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    persisted_tasks = False

    async def _post_activity(*_args, **_kwargs):
        return "msg_generating"

    async def _finish_activity(*_args, **_kwargs):
        return True

    async def _refresh_measurements(*_args, **_kwargs):
        return {"measured": [], "errors": []}

    async def _reconcile(*_args, **_kwargs):
        return []

    async def _gather_context(*_args, **_kwargs):
        return SimpleNamespace(
            open_proposed_tasks=[],
            open_proposed_items=[],
            allowed_service_keys=[],
        )

    async def _starter_cleanup(*_args, **_kwargs):
        return {}

    async def _generate_proposal(*_args, **_kwargs):
        async with session_factory() as concurrent_db:
            if concurrent_change == "pause":
                paused = await concurrent_db.get(Workspace, workspace.id)
                assert paused is not None
                paused.status = "paused"
            else:
                stolen = await concurrent_db.get(ReviewRun, review.id)
                assert stolen is not None
                stolen.lease_owner = "worker-b"
                stolen.lease_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=90
                )
            await concurrent_db.commit()
        return SimpleNamespace()

    async def _suppress_workflow_runs(*_args, **_kwargs):
        return None

    async def _persist_tasks(*_args, **_kwargs):
        nonlocal persisted_tasks
        persisted_tasks = True
        return []

    @asynccontextmanager
    async def _billing_context(**_kwargs):
        yield

    monkeypatch.setattr(service, "_post_strategist_activity", _post_activity)
    monkeypatch.setattr(service, "_finish_strategist_activity", _finish_activity)
    monkeypatch.setattr(
        service,
        "_refresh_internal_goal_measurements_for_review",
        _refresh_measurements,
    )
    monkeypatch.setattr(service, "reconcile_active_work_batches", _reconcile)
    monkeypatch.setattr(service, "gather_context", _gather_context)
    monkeypatch.setattr(
        service,
        "_resolve_fulfilled_starter_document_proposals",
        _starter_cleanup,
    )
    monkeypatch.setattr(service, "_evaluate_skip_conditions", lambda _ctx: None)
    monkeypatch.setattr(service, "generate_proposal", _generate_proposal)
    monkeypatch.setattr(service, "_sanitize_governance_language", lambda *_args: None)
    monkeypatch.setattr(service, "_suppress_starter_document_proposals", lambda *_args: None)
    monkeypatch.setattr(service, "_suppress_meta_learning_proposals", lambda *_args: None)
    monkeypatch.setattr(service, "_enforce_allowlists", lambda *_args: None)
    monkeypatch.setattr(service, "_enforce_proposal_shape", lambda *_args: None)
    monkeypatch.setattr(
        service,
        "_suppress_active_workflow_run_proposals",
        _suppress_workflow_runs,
    )
    monkeypatch.setattr(
        service,
        "runtime_strategist_review_billing_context",
        _billing_context,
    )
    monkeypatch.setattr(service, "_persist_tasks", _persist_tasks)

    if concurrent_change == "pause":
        result = await service.run_review(
            db_session,
            workspace.id,
            trigger="manual",
            **review_kwargs,
        )
        assert result == {
            "workspace_id": workspace.id,
            "skipped": True,
            "reason": "workspace_inactive",
            "workspace_status": "paused",
        }
    else:
        with pytest.raises(
            service.StrategistLeaseLost,
            match=f"review {review.id} lost its execution lease",
        ):
            await service.run_review(
                db_session,
                workspace.id,
                trigger="manual",
                **review_kwargs,
            )
    assert persisted_tasks is False


@pytest.mark.asyncio
async def test_strategist_review_lease_renews_without_long_transaction(
    db_session,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.review import begin_review, renew_review_lease

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Review lease",
        status="active",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()
    review = await begin_review(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger="scheduled",
        lease_owner="worker-a",
    )
    await db_session.commit()

    renewal_time = review.created_at + timedelta(seconds=30)
    renewed = await renew_review_lease(
        db_session,
        review_id=review.id,
        lease_owner="worker-a",
        now=renewal_time,
    )

    assert renewed is True
    assert review.lease_expires_at == renewal_time + timedelta(seconds=90)


@pytest.mark.asyncio
async def test_strategist_lease_loss_marks_review_and_activity_failed(
    db_session,
    monkeypatch,
) -> None:
    import packages.core.strategist as strategist_package
    from packages.core import consolidators
    from packages.core.models.base import generate_ulid
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.workspace import Workspace
    from packages.core.review import briefing as briefing_module
    from packages.core.review import briefing_render
    from packages.core.services import feature_flags
    from packages.core.strategist import orchestrator, service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Lease loss failure receipt",
        status="active",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()

    finished: list[tuple[str, str]] = []

    async def _enabled(*_args, **_kwargs):
        return True

    async def _post_activity(*_args, **_kwargs):
        return "msg_collecting"

    async def _finish_activity(*, review_id, state, **_kwargs):
        finished.append((review_id, state))
        return True

    async def _run_all(*_args, **_kwargs):
        return []

    async def _build_briefing(*_args, **_kwargs):
        return SimpleNamespace(model_dump=lambda **_kwargs: {})

    async def _lose_lease(*_args, review_run, **_kwargs):
        raise service.StrategistLeaseLost(
            f"review {review_run.id} lost its execution lease"
        )

    monkeypatch.setattr(feature_flags, "is_enabled", _enabled)
    monkeypatch.setattr(service, "_post_strategist_activity", _post_activity)
    monkeypatch.setattr(
        service,
        "finish_strategist_review_activity",
        _finish_activity,
    )
    monkeypatch.setattr(consolidators, "run_all", _run_all)
    monkeypatch.setattr(briefing_module, "build_briefing", _build_briefing)
    monkeypatch.setattr(
        briefing_render,
        "render_briefing_markdown",
        lambda _briefing: "brief",
    )
    monkeypatch.setattr(strategist_package, "run_review", _lose_lease)

    with pytest.raises(service.StrategistLeaseLost, match="lost its execution lease"):
        await orchestrator.run_strategist_review_cycle(
            db_session,
            workspace.id,
            "scheduled",
            execution_owner="worker-a",
        )

    review = (
        await db_session.execute(
            select(ReviewRun).where(ReviewRun.workspace_id == workspace.id)
        )
    ).scalar_one()
    assert review.status == "failed"
    assert review.completed_at is not None
    assert review.error == f"review {review.id} lost its execution lease"
    assert finished == [(review.id, "failed")]


@pytest.mark.asyncio
@pytest.mark.parametrize("card_write_fails", [False, True])
async def test_strategist_commits_review_success_with_task_cohort(
    db_session,
    monkeypatch,
    card_write_fails,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.review import begin_review
    from packages.core.strategist import service
    from packages.core.strategist.proposal import Deliverable, Proposal, ProposedTask
    from packages.core.workspace_chat import service as chat_service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Atomic review cohort",
        status="active",
        settings={},
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        name="Ops Agent",
        status="active",
    )
    db_session.add_all([
        workspace,
        agent,
        AgentSubscription(
            id=generate_ulid(),
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            agent_id=agent.id,
            service_key="ops",
            status="active",
        ),
    ])
    await db_session.commit()
    review = await begin_review(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger="manual",
        lease_owner="worker-a",
    )
    await db_session.commit()
    workspace_id = workspace.id
    workspace_entity_id = workspace.entity_id
    review_id = review.id

    async def _generate_proposal(*_args, review_id, **_kwargs):
        return Proposal(
            review_id=review_id,
            summary="Prepare one bounded operating packet.",
            tasks=[
                ProposedTask(
                    task_key="operating_packet",
                    title="Prepare operating packet",
                    owner_service_key="ops",
                    deliverables=[
                        Deliverable(
                            name="packet",
                            kind="value",
                            shape="TextResult",
                            acceptance="The operating packet is complete.",
                            usage="The operator reviews the proposed packet.",
                        )
                    ],
                )
            ],
        )

    async def _post_activity(_workspace, *, stage, **_kwargs):
        return f"msg_{stage}"

    async def _finish_activity(*_args, **_kwargs):
        return True

    async def _wire_governance(*_args, **_kwargs):
        return {}

    observed_during_card_write: dict[str, object] = {}
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def _post_proposal_chat(*_args, db, **_kwargs):
        assert db is db_session
        async with session_factory() as observer_db:
            persisted_review = await observer_db.get(ReviewRun, review_id)
            persisted_tasks = list(
                (
                    await observer_db.execute(
                        select(Task).where(Task.workspace_id == workspace_id)
                    )
                ).scalars()
            )
            persisted_cards = list(
                (
                    await observer_db.execute(
                        select(Message)
                        .join(
                            Conversation,
                            Conversation.id == Message.conversation_id,
                        )
                        .where(
                            Conversation.workspace_id == workspace_id,
                            Message.message_kind == "proposal",
                        )
                    )
                ).scalars()
            )
            observed_during_card_write.update(
                review_status=persisted_review.status if persisted_review else None,
                task_count=len(persisted_tasks),
                card_count=len(persisted_cards),
            )
        if card_write_fails:
            raise RuntimeError("proposal card write failed")
        return await chat_service.post_message(
            db,
            entity_id=workspace_entity_id,
            workspace_id=workspace_id,
            body="Atomic proposal card",
            message_kind="proposal",
            author_kind="agent",
            publish_event=False,
        )

    async def _no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "generate_proposal", _generate_proposal)
    monkeypatch.setattr(service, "_post_strategist_activity", _post_activity)
    monkeypatch.setattr(service, "_finish_strategist_activity", _finish_activity)
    monkeypatch.setattr(service, "_wire_proposal_governance", _wire_governance)
    monkeypatch.setattr(service, "_post_proposal_chat", _post_proposal_chat)
    monkeypatch.setattr(service, "_publish_proposal_chat_message", _no_op)
    monkeypatch.setattr(service, "_notify_proposal_users", _no_op)

    if card_write_fails:
        with pytest.raises(RuntimeError, match="proposal card write failed"):
            await service.run_review(
                db_session,
                workspace_id,
                trigger="manual",
                briefing_markdown="fixed briefing",
                review_run=review,
                review_lease_owner="worker-a",
            )
        await db_session.rollback()
    else:
        result = await service.run_review(
            db_session,
            workspace_id,
            trigger="manual",
            briefing_markdown="fixed briefing",
            review_run=review,
            review_lease_owner="worker-a",
        )
        assert result["task_count"] == 1

    assert observed_during_card_write == {
        "review_status": "running",
        "task_count": 0,
        "card_count": 0,
    }
    async with session_factory() as observer_db:
        persisted_review = await observer_db.get(ReviewRun, review_id)
        persisted_tasks = list(
            (
                await observer_db.execute(
                    select(Task).where(Task.workspace_id == workspace_id)
                )
            ).scalars()
        )
        persisted_cards = list(
            (
                await observer_db.execute(
                    select(Message)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(
                        Conversation.workspace_id == workspace_id,
                        Message.message_kind == "proposal",
                    )
                )
            ).scalars()
        )
    assert persisted_review is not None
    assert persisted_review.status == (
        "running" if card_write_fails else "succeeded"
    )
    assert len(persisted_tasks) == (0 if card_write_fails else 1)
    assert len(persisted_cards) == (0 if card_write_fails else 1)


def test_recent_legacy_review_is_not_recoverable_during_rolling_deploy() -> None:
    from packages.core.review import review_lease_is_expired

    now = datetime.now(timezone.utc)
    review = SimpleNamespace(
        lease_expires_at=None,
        created_at=now,
    )

    assert review_lease_is_expired(review, now=now) is False
    review.created_at = now - timedelta(minutes=16)
    assert review_lease_is_expired(review, now=now) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("same_delivery_redelivery", [False, True])
async def test_strategist_review_recovers_abandoned_review_and_activity(
    db_session,
    monkeypatch,
    same_delivery_redelivery,
) -> None:
    import packages.core.strategist as strategist_package
    from packages.core import consolidators
    from packages.core.models.base import generate_ulid
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.workspace import Workspace
    from packages.core.review import begin_review
    from packages.core.review import briefing as briefing_module
    from packages.core.review import briefing_render
    from packages.core.services import feature_flags
    from packages.core.strategist import orchestrator, service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Abandoned review recovery",
        status="active",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()
    abandoned = await begin_review(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger="scheduled",
    )
    abandoned_id = abandoned.id
    abandoned.lease_owner = "dead-worker"
    abandoned.lease_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=90 if same_delivery_redelivery else -1
    )
    await db_session.commit()

    finished_activity_ids: list[str] = []

    async def _enabled(*_args, **_kwargs):
        return True

    async def _finish_activity(*, review_id, **_kwargs):
        finished_activity_ids.append(review_id)
        return True

    async def _post_activity(*_args, **_kwargs):
        return "msg_collecting"

    async def _run_all(*_args, **_kwargs):
        return []

    async def _build_briefing(*_args, **_kwargs):
        return SimpleNamespace(model_dump=lambda **_kwargs: {})

    async def _run_review(*_args, **_kwargs):
        return {
            "workspace_id": workspace.id,
            "skipped": True,
            "reason": "open_proposals",
        }

    monkeypatch.setattr(feature_flags, "is_enabled", _enabled)
    monkeypatch.setattr(
        service,
        "finish_strategist_review_activity",
        _finish_activity,
    )
    monkeypatch.setattr(service, "_post_strategist_activity", _post_activity)
    monkeypatch.setattr(consolidators, "run_all", _run_all)
    monkeypatch.setattr(briefing_module, "build_briefing", _build_briefing)
    monkeypatch.setattr(
        briefing_render,
        "render_briefing_markdown",
        lambda _briefing: "brief",
    )
    monkeypatch.setattr(strategist_package, "run_review", _run_review)

    result = await orchestrator.run_strategist_review_cycle(
        db_session,
        workspace.id,
        "scheduled",
        execution_owner=(
            "dead-worker" if same_delivery_redelivery else "replacement-worker"
        ),
    )

    await db_session.refresh(abandoned)
    reviews = list(
        (
            await db_session.execute(
                select(ReviewRun).where(ReviewRun.workspace_id == workspace.id)
            )
        ).scalars()
    )
    assert result["skipped"] is True
    assert abandoned.status == "failed"
    assert abandoned.completed_at is not None
    assert abandoned_id in finished_activity_ids
    assert len(reviews) == 2
    assert {review.status for review in reviews} == {"failed", "skipped"}


@pytest.mark.asyncio
async def test_strategist_review_preserves_live_foreign_lease(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.review import begin_review
    from packages.core.services import feature_flags
    from packages.core.strategist import orchestrator

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Live review lease",
        status="active",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()
    live_review = await begin_review(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger="scheduled",
        lease_owner="live-worker",
    )
    live_review.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=90)
    await db_session.commit()

    async def _enabled(*_args, **_kwargs):
        return True

    monkeypatch.setattr(feature_flags, "is_enabled", _enabled)

    result = await orchestrator.run_strategist_review_cycle(
        db_session,
        workspace.id,
        "scheduled",
        execution_owner="other-worker",
    )

    await db_session.refresh(live_review)
    assert result == {
        "workspace_id": workspace.id,
        "skipped": True,
        "reason": "review_already_running",
    }
    assert live_review.status == "running"
    assert live_review.lease_owner == "live-worker"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "activity_state"),
    [("succeeded", "completed"), ("skipped", "skipped")],
)
async def test_strategist_review_deduplicates_terminal_delivery(
    db_session,
    monkeypatch,
    terminal_status,
    activity_state,
) -> None:
    import packages.core.strategist as strategist_package
    from packages.core.models.base import generate_ulid
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.workspace import Workspace
    from packages.core.review import begin_review, complete_review, mark_review_skipped
    from packages.core.services import feature_flags
    from packages.core.strategist import orchestrator, service

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Terminal delivery receipt",
        status="active",
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()
    review = await begin_review(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger="scheduled",
        delivery_id="celery-delivery",
        lease_owner="celery-delivery",
    )
    if terminal_status == "succeeded":
        await complete_review(db_session, review)
    else:
        await mark_review_skipped(db_session, review, reason="open_proposals")
    await db_session.commit()

    finished: list[tuple[str, str]] = []

    async def _enabled(*_args, **_kwargs):
        return True

    async def _finish(*, review_id, state, **_kwargs):
        finished.append((review_id, state))
        return True

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("terminal delivery must not execute another review")

    monkeypatch.setattr(feature_flags, "is_enabled", _enabled)
    monkeypatch.setattr(service, "finish_strategist_review_activity", _finish)
    monkeypatch.setattr(strategist_package, "run_review", _must_not_run)

    result = await orchestrator.run_strategist_review_cycle(
        db_session,
        workspace.id,
        "scheduled",
        execution_owner="celery-delivery",
    )

    reviews = list(
        (
            await db_session.execute(
                select(ReviewRun).where(ReviewRun.workspace_id == workspace.id)
            )
        ).scalars()
    )
    assert result == {
        "workspace_id": workspace.id,
        "review_id": review.id,
        "status": terminal_status,
        "deduplicated": True,
    }
    assert finished == [(review.id, activity_state)]
    assert len(reviews) == 1


@pytest.mark.asyncio
async def test_legacy_review_failure_uses_its_exact_activity_review_id(monkeypatch) -> None:
    import packages.core.strategist as strategist_package
    from packages.core.services import feature_flags
    from packages.core.strategist import orchestrator, service
    from packages.core.strategist.triggers import ReviewTriggerKind

    calls: list[tuple[str, str]] = []

    workspace = SimpleNamespace(
        id="ws_test",
        entity_id="entity_test",
        status="active",
    )

    class _Db:
        execute_calls = 0

        async def execute(self, _query):
            self.execute_calls += 1
            value = workspace if self.execute_calls == 1 else None
            return SimpleNamespace(scalar_one_or_none=lambda: value)

    async def _disabled(*_args, **_kwargs):
        return False

    async def _post_activity(_workspace, *, review_id, **_kwargs):
        calls.append(("start", review_id))
        return "msg_collecting"

    async def _run_review(
        *_args,
        activity_review_id,
        initial_activity_message_id,
        **_kwargs,
    ):
        calls.append(("run", activity_review_id))
        assert initial_activity_message_id == "msg_collecting"
        raise RuntimeError("model unavailable")

    async def _finish_review(*, review_id, state, **_kwargs):
        calls.append((state, review_id))
        return True

    monkeypatch.setattr(feature_flags, "is_enabled", _disabled)
    monkeypatch.setattr(strategist_package, "run_review", _run_review)
    monkeypatch.setattr(service, "_post_strategist_activity", _post_activity)
    monkeypatch.setattr(service, "finish_strategist_review_activity", _finish_review)
    monkeypatch.setattr(orchestrator, "generate_ulid", lambda: "review_test")

    with pytest.raises(RuntimeError, match="model unavailable"):
        await orchestrator.run_strategist_review_cycle(
            _Db(),
            "ws_test",
            ReviewTriggerKind.HUMAN_REQUESTED,
        )

    assert calls == [
        ("start", "rv_review_test"),
        ("run", "rv_review_test"),
        ("failed", "rv_review_test"),
    ]


@pytest.mark.asyncio
async def test_v2_collecting_activity_starts_before_consolidators(monkeypatch) -> None:
    import packages.core.strategist as strategist_package
    from packages.core import consolidators
    from packages.core.review import briefing as briefing_module
    from packages.core.review import briefing_render
    from packages.core.services import feature_flags
    from packages.core.strategist import orchestrator, service
    from packages.core.strategist.triggers import ReviewTriggerKind

    events: list[str] = []
    workspace = SimpleNamespace(
        id="ws_test",
        entity_id="entity_test",
        status="active",
    )
    review = SimpleNamespace(id="rv_test", briefing=None)

    class _Db:
        execute_calls = 0

        async def execute(self, _query):
            self.execute_calls += 1
            value = workspace if self.execute_calls == 1 else None
            return SimpleNamespace(scalar_one_or_none=lambda: value)

        async def commit(self):
            events.append("claim")

        async def flush(self):
            events.append("briefing-flush")

        async def get(self, *_args, **_kwargs):
            return None

        async def rollback(self):
            events.append("rollback")

    async def _enabled(*_args, **_kwargs):
        return True

    async def _begin_review(*_args, **_kwargs):
        return review

    async def _post_activity(_workspace, *, stage, review_id, **_kwargs):
        events.append(f"activity:{stage}:{review_id}")
        return "msg_collecting"

    async def _run_all(*_args, **_kwargs):
        events.append("consolidators")
        return []

    async def _build_briefing(*_args, **_kwargs):
        events.append("briefing")
        return SimpleNamespace(model_dump=lambda **_kwargs: {})

    async def _run_review(
        *_args,
        initial_activity_message_id,
        **_kwargs,
    ):
        events.append(f"review:{initial_activity_message_id}")
        return {"workspace_id": "ws_test", "skipped": True}

    monkeypatch.setattr(feature_flags, "is_enabled", _enabled)
    monkeypatch.setattr("packages.core.review.begin_review", _begin_review)
    monkeypatch.setattr(service, "_post_strategist_activity", _post_activity)
    monkeypatch.setattr(consolidators, "run_all", _run_all)
    monkeypatch.setattr(briefing_module, "build_briefing", _build_briefing)
    monkeypatch.setattr(briefing_render, "render_briefing_markdown", lambda _briefing: "brief")
    monkeypatch.setattr(strategist_package, "run_review", _run_review)

    await orchestrator.run_strategist_review_cycle(
        _Db(),
        "ws_test",
        ReviewTriggerKind.SCHEDULED,
    )

    assert events.index("activity:collecting_feedback:rv_test") < events.index(
        "consolidators",
    )
    assert "review:msg_collecting" in events


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_fails", [False, True], ids=["published", "publish-failed"])
async def test_workspace_lifecycle_receipt_commits_before_realtime_publish(
    monkeypatch,
    publish_fails: bool,
) -> None:
    from apps.api.routers import workspaces as workspace_router
    from packages.core.workspace_chat import service as chat_service

    events: list[str] = []

    class _Session:
        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    async def _post_message(_db, **_kwargs):
        events.append("post")
        return SimpleNamespace(
            id="msg_lifecycle",
            message_kind="strategist_activity",
            author_kind="system",
            pending_action=None,
        )

    async def _publish_message_event(*_args, **_kwargs):
        events.append("publish")
        if publish_fails:
            raise RuntimeError("realtime unavailable")

    monkeypatch.setattr(
        chat_service,
        "post_workspace_lifecycle_activity",
        _post_message,
    )
    monkeypatch.setattr(
        chat_service,
        "publish_workspace_chat_message_event",
        _publish_message_event,
    )

    message_id = await workspace_router._persist_workspace_lifecycle_activity(
        _Session(),
        SimpleNamespace(id="ws_test", entity_id="entity_test"),
        action="pause",
        transition_id="transition_test",
    )

    assert message_id == "msg_lifecycle"
    assert events == ["post", "commit", "publish"]


@pytest.mark.asyncio
async def test_workspace_chat_can_reconcile_one_exact_message(
    client: AsyncClient,
) -> None:
    headers = await _register(client, "workspace_activity_reconcile")
    first = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Realtime receipt"},
    )
    second = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Other workspace"},
    )
    assert first.status_code == 201
    assert second.status_code == 201

    message = await client.post(
        f"/api/v1/workspaces/{first.json()['id']}/chat/messages",
        headers=headers,
        json={"body": ""},
    )
    assert message.status_code == 201
    message_id = message.json()["id"]

    receipt = await client.get(
        f"/api/v1/workspaces/{first.json()['id']}/chat/messages/{message_id}",
        headers=headers,
    )
    assert receipt.status_code == 200
    assert receipt.json()["id"] == message_id

    cross_workspace = await client.get(
        f"/api/v1/workspaces/{second.json()['id']}/chat/messages/{message_id}",
        headers=headers,
    )
    assert cross_workspace.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_status", "expected_activity_state"),
    [
        ("succeeded", "completed"),
        ("skipped", "skipped"),
        ("failed", "failed"),
        ("expired", "failed"),
    ],
)
async def test_workspace_chat_reconciles_strategist_activity_from_review_receipt(
    client: AsyncClient,
    db_session,
    review_status,
    expected_activity_state,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.task import Message
    from packages.core.models.workspace import Workspace
    from packages.core.workspace_chat import service as chat_service

    headers = await _register(client, f"activity_receipt_{review_status}")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": f"Activity receipt {review_status}"},
    )
    assert created.status_code == 201
    workspace_id = created.json()["id"]
    workspace = await db_session.get(Workspace, workspace_id)
    assert workspace is not None

    review_id = generate_ulid()
    now = datetime.now(timezone.utc)
    expired = review_status == "expired"
    review = ReviewRun(
        id=review_id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger_kind="scheduled",
        status="running" if expired else review_status,
        delivery_id=f"delivery-{review_status}",
        lease_owner=f"delivery-{review_status}" if expired else None,
        lease_expires_at=now - timedelta(seconds=1) if expired else None,
        completed_at=None if expired else now,
    )
    db_session.add(review)
    message = await chat_service.post_message(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        body="Strategist activity",
        message_kind="strategist_activity",
        author_kind="agent",
        meta={
            "strategist_activity": {
                "review_id": review_id,
                "stage": "generating_plan",
                "state": "running",
                "started_at": now.isoformat(),
            }
        },
    )
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workspaces/{workspace.id}/chat/messages/{message.id}",
        headers=headers,
    )
    assert response.status_code == 200
    activity = response.json()["meta"]["strategist_activity"]
    assert activity["state"] == expected_activity_state
    assert activity["finished_at"]

    persisted = await db_session.get(Message, message.id, populate_existing=True)
    assert persisted is not None
    assert persisted.meta["strategist_activity"]["state"] == "running"


@pytest.mark.asyncio
async def test_workspace_runtime_switch_controls_goal_measurement_schedule(
    client: AsyncClient,
    db_session,
) -> None:
    from packages.core.models.scheduler import ScheduledJob

    headers = await _register(client, "workspace_runtime_goal_switch")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Unified runtime"},
    )
    assert workspace_response.status_code == 201
    workspace = workspace_response.json()

    goal_response = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace["id"],
            "title": "Collect weekly feedback",
            "metric_key": "feedback_count",
            "target_value": 10,
            "measurement_source": {
                "provider": "workspace_internal",
                "params": {"mode": "linked_task_impact"},
            },
            "measurement_cadence": "daily",
        },
    )
    assert goal_response.status_code == 201
    goal_id = goal_response.json()["id"]

    async def goal_job():
        return (await db_session.execute(
            select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal_id}")
        )).scalar_one_or_none()

    # The workspace starts with autonomous runtime disabled.
    assert await goal_job() is None

    enabled = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/heartbeat/enable?cadence=daily",
        headers=headers,
    )
    assert enabled.status_code == 200
    assert (await goal_job()) is not None

    disabled = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/heartbeat/disable",
        headers=headers,
    )
    assert disabled.status_code == 200
    assert await goal_job() is None
