"""Attention delivery: told once per stage in Workspace chat, replacing the older senders."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.attention import delivery
from packages.core.attention.reconcile import reconcile_watcher
from packages.core.attention.watchers.account_broken import AccountBrokenWatcher
from packages.core.attention.watchers.decision_pending import DecisionPendingWatcher
from packages.core.attention.watchers.needs_input import NeedsInputWatcher
from packages.core.attention.watchers.work import WorkBlockedWatcher, WorkOverdueWatcher
from packages.core.attention.watchers.workspace_state import AutomationPausedWatcher, GoalOffPaceWatcher
from packages.core.models.attention import AttentionItem
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.goal import Goal
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.notification import Notification
from packages.core.models.proposal import ProposalRecord
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Message, Task, TaskLog
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services import account_incident_delivery, feature_flags, hitl_reminders, scheduler_service
from packages.core.workspace_chat.service import ensure_main_conversation

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


class _SessionContext:
    def __init__(self, db_session) -> None:
        self.db_session = db_session

    async def __aenter__(self):
        return self.db_session

    async def __aexit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _shared_session(db_session, monkeypatch):
    for module in (delivery, hitl_reminders, account_incident_delivery):
        monkeypatch.setattr(module, "async_session", lambda: _SessionContext(db_session))


@pytest.fixture
def notices(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)
    monkeypatch.setattr(hitl_reminders, "notify", sent)
    monkeypatch.setattr(account_incident_delivery, "notify", sent)
    return sent


async def _entity(db, *, delivery_on: bool, locale: str = "en") -> tuple[str, User]:
    entity_id = generate_ulid()
    user = User(id=generate_ulid(), entity_id=entity_id, email=f"{generate_ulid()}@example.test",
                password_hash="x", role="owner", status="active", locale=locale)
    db.add(user)
    await db.flush()
    if delivery_on:
        await feature_flags.seed_known_flags(db)
        await feature_flags.set_override(
            db, key=delivery.FLAG, scope="tenant", scope_id=entity_id, enabled=True,
            set_by_admin_id=None, set_reason="test",
        )
    return entity_id, user


async def _workspace(db, entity_id: str, user: User, name: str = "Support") -> Workspace:
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name=name, status="active",
                          settings={"notification_policy": {"hitl_notify_user_ids": [user.id]}})
    db.add(workspace)
    await db.flush()
    return workspace


def _request(entity_id: str, workspace_id: str, **fields) -> HitlRequest:
    return HitlRequest(**{
        "id": generate_ulid(), "entity_id": entity_id, "workspace_id": workspace_id,
        "action_key": "workspace.automation.create", "risk_level": "medium", "origin_kind": "tool_call",
        "status": "pending", "hitl_type": "authorize", "dedup_key": f"delivery:{generate_ulid()}",
        "payload": {"question": "Create the weekly report automation?"}, "context": {},
        "created_at": T0, **fields,
    })


async def _chat(db, entity_id: str, workspace_id: str) -> list[str]:
    conversation = await ensure_main_conversation(db, entity_id=entity_id, workspace_id=workspace_id)
    return list((await db.execute(
        select(Message.content).where(
            Message.conversation_id == conversation.id, Message.meta["attention_item_ids"].is_not(None),
        ).order_by(Message.created_at)
    )).scalars().all())


async def _sweep(db, watcher, entity_id: str, now: datetime) -> dict[str, int]:
    await reconcile_watcher(db, watcher, now=now, entity_id=entity_id)
    return await delivery.deliver_attention(now=now)


@pytest.mark.asyncio
async def test_a_waiting_request_is_told_at_each_reminder_stage_once(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=True, locale="zh")
    workspace = await _workspace(db_session, entity_id, user)
    request = _request(entity_id, workspace.id)
    db_session.add(request)
    await db_session.flush()
    watcher = NeedsInputWatcher()

    # The card is already in the chat when it opens: nothing more to say.
    await _sweep(db_session, watcher, entity_id, T0 + H)
    assert await _chat(db_session, entity_id, workspace.id) == []

    await _sweep(db_session, watcher, entity_id, T0 + 25 * H)
    await _sweep(db_session, watcher, entity_id, T0 + 26 * H)
    [told] = await _chat(db_session, entity_id, workspace.id)
    assert "还在等你处理：Create the weekly report automation?（已等超过一天）" in told
    [call] = notices.await_args_list
    assert (call.kwargs["user_id"], call.kwargs["type"]) == (user.id, "task_hitl_reminder")
    # The reminder sweep's own state moves too, so turning the flag off
    # never repeats this stage.
    await db_session.refresh(request)
    assert request.context["reminders"]["sent"] == 1
    assert await hitl_reminders.run_hitl_reminder_sweep(now=T0 + 26 * H) == 0

    await _sweep(db_session, watcher, entity_id, T0 + 97 * H)
    assert len(await _chat(db_session, entity_id, workspace.id)) == 2
    assert "超过 4 天" in (await _chat(db_session, entity_id, workspace.id))[1]
    assert await hitl_reminders.run_hitl_reminder_sweep(now=T0 + 98 * H) == 0
    assert notices.await_count == 2


@pytest.mark.asyncio
async def test_with_the_flag_off_nothing_is_told_and_the_reminders_run(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=False)
    workspace = await _workspace(db_session, entity_id, user)
    db_session.add(_request(entity_id, workspace.id))
    await db_session.flush()

    await _sweep(db_session, NeedsInputWatcher(), entity_id, T0 + 25 * H)
    assert await _chat(db_session, entity_id, workspace.id) == []
    assert notices.await_count == 0
    assert await hitl_reminders.run_hitl_reminder_sweep(now=T0 + 25 * H) == 1


@pytest.mark.asyncio
async def test_a_stage_the_reminders_already_sent_is_not_told_again(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=True)
    workspace = await _workspace(db_session, entity_id, user)
    request = _request(entity_id, workspace.id, context={"reminders": {"sent": 1, "last_at": (T0 + 24 * H).isoformat()}})
    db_session.add(request)
    await db_session.flush()

    await _sweep(db_session, NeedsInputWatcher(), entity_id, T0 + 30 * H)
    assert await _chat(db_session, entity_id, workspace.id) == []
    [item] = (await db_session.execute(
        select(AttentionItem).where(AttentionItem.entity_id == entity_id)
    )).scalars().all()
    assert item.told_stage == 1


@pytest.mark.asyncio
async def test_a_broken_account_is_told_in_each_workspace_and_once_per_person(db_session, notices, monkeypatch):
    entity_id, user = await _entity(db_session, delivery_on=True)
    support = await _workspace(db_session, entity_id, user, "Support")
    content = await _workspace(db_session, entity_id, user, "Content")
    integration = Integration(
        id=generate_ulid(), entity_id=entity_id, provider="gmail", status="active", owner_user_id=user.id,
        config={"health_incident": {"opened_at": T0.isoformat(), "reason": "invalid_grant"}},
    )
    db_session.add(integration)
    await db_session.flush()

    async def scan(db, scanned_entity_id, now):
        if scanned_entity_id != entity_id:
            return []
        return [(support.id, "Support", [user.id], {"gmail"}), (content.id, "Content", [user.id], {"gmail"})]

    monkeypatch.setattr(account_incident_delivery, "scan_entity_workspaces", scan)
    # The incident notice is attention's to send now.
    assert await account_incident_delivery.deliver_account_incidents(now=T0) == 0

    await _sweep(db_session, AccountBrokenWatcher(), entity_id, T0 + H)
    for workspace in (support, content):
        [told] = await _chat(db_session, entity_id, workspace.id)
        assert "Gmail needs reconnecting" in told and "(/integrations)" in told
    [call] = notices.await_args_list
    assert (call.kwargs["type"], call.kwargs["title"]) == ("integration_account_issue", "Gmail needs reconnecting")
    await db_session.refresh(integration)
    assert integration.config["health_incident"]["notified_at"]


@pytest.mark.asyncio
async def test_only_the_worse_goal_pace_is_told_and_only_in_chat(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=True)
    workspace = await _workspace(db_session, entity_id, user)
    goal = Goal(entity_id=entity_id, workspace_id=workspace.id, title="Grow followers", metric_key="followers",
                target_value=1000, current_value=120, status="active", pace_status="behind")
    db_session.add(goal)
    await db_session.flush()

    await _sweep(db_session, GoalOffPaceWatcher(), entity_id, T0)
    assert await _chat(db_session, entity_id, workspace.id) == []  # behind is fyi

    goal.pace_status = "at_risk"
    await db_session.flush()
    await _sweep(db_session, GoalOffPaceWatcher(), entity_id, T0 + H)
    [told] = await _chat(db_session, entity_id, workspace.id)
    assert "Goal at risk: Grow followers (120 of 1000)" in told
    assert notices.await_count == 0


@pytest.mark.asyncio
async def test_a_paused_workflow_wait_is_told_with_a_link_to_its_run(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=True)
    workspace = await _workspace(db_session, entity_id, user)
    run_id = generate_ulid()
    db_session.add(AttentionItem(
        entity_id=entity_id, workspace_id=workspace.id, kind="needs_input", source="needs_input",
        subject_type="workflow_run", subject_id=f"{run_id}:owner_approval",
        fingerprint=f"needs_input:{workspace.id}:workflow_run:{run_id}:owner_approval", stage=1,
        urgency="act_today", title="Approval needed: Weekly report · Approve the draft", state="open",
        opened_at=T0, evidence={"workflow_run_id": run_id, "step_id": "owner_approval", "wait_type": "approval"},
    ))
    await db_session.flush()

    await delivery.deliver_attention(now=T0 + 25 * H)
    [told] = await _chat(db_session, entity_id, workspace.id)
    assert f"/workspaces/{workspace.id}/workflow-runs/{run_id}" in told
    [call] = notices.await_args_list
    assert (call.kwargs["user_id"], call.kwargs["type"]) == (user.id, "task_hitl_reminder")


@pytest.mark.asyncio
async def test_an_undecided_proposal_is_told_in_place_of_its_cards_reminders(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=True)
    workspace = await _workspace(db_session, entity_id, user)
    record = ProposalRecord(entity_id=entity_id, workspace_id=workspace.id, review_id="rv_1",
                            summary="Two posts for the launch week", status="open")
    db_session.add(record)
    await db_session.flush()
    card = _request(entity_id, workspace.id, resource_kind="proposal", resource_id=record.id)
    db_session.add_all([card, Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, title="Draft the posts",
        status="proposed", details={"strategist_review_id": "rv_1"}, created_at=T0,
    )])
    await db_session.flush()

    await _sweep(db_session, DecisionPendingWatcher(), entity_id, T0 + H)
    assert await _chat(db_session, entity_id, workspace.id) == []  # the card is the telling
    # The card's reminders are attention's to send now.
    assert await hitl_reminders.run_hitl_reminder_sweep(now=T0 + 25 * H) == 0
    await _sweep(db_session, DecisionPendingWatcher(), entity_id, T0 + 25 * H)
    [told] = await _chat(db_session, entity_id, workspace.id)
    assert "Still waiting for your decision: Two posts for the launch week (waiting over a day)" in told
    [call] = notices.await_args_list
    assert (call.kwargs["user_id"], call.kwargs["type"]) == (user.id, "proposal")
    await db_session.refresh(card)
    assert card.context["reminders"]["sent"] == 1


@pytest.mark.asyncio
async def test_a_paused_automation_is_told_once_whoever_told_it_first(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=True)
    workspace = await _workspace(db_session, entity_id, user)

    def paused(name: str) -> ScheduledJob:
        return ScheduledJob(job_id=f"job-{generate_ulid()}", entity_id=entity_id, workspace_id=workspace.id,
                            user_id=user.id, name=name, enabled=False, consecutive_errors=3,
                            execution_type="workflow", last_run_at=T0)

    weekly, daily = paused("Weekly report"), paused("Daily sync")
    db_session.add_all([weekly, daily])
    await db_session.flush()
    # Daily was paused before the flag was on: its owner already has the notice.
    db_session.add(Notification(entity_id=entity_id, user_id=user.id, type="automation_paused",
                                idempotency_key=f"scheduled-job:auto-paused:{daily.id}:run_1",
                                created_at=T0 + H))
    await db_session.flush()
    # With the flag on, the scheduler leaves the notice to attention.
    await scheduler_service.notify_scheduled_job_auto_paused(db_session, weekly, failure_key="run_2")
    assert (await db_session.execute(
        select(Notification).where(Notification.idempotency_key.like(f"scheduled-job:auto-paused:{weekly.id}:%"))
    )).scalar_one_or_none() is None

    await _sweep(db_session, AutomationPausedWatcher(), entity_id, T0 + 2 * H)
    [told] = await _chat(db_session, entity_id, workspace.id)
    assert "Automation paused after 3 failed runs: Weekly report" in told
    assert f"(/jobs?job={weekly.job_id})" in told and "Daily sync" not in told
    [call] = notices.await_args_list
    assert (call.kwargs["user_id"], call.kwargs["type"]) == (user.id, "automation_paused")


@pytest.mark.asyncio
async def test_overdue_work_is_told_now_and_blocked_work_at_its_reminders(db_session, notices):
    entity_id, user = await _entity(db_session, delivery_on=True)
    workspace = await _workspace(db_session, entity_id, user)
    late = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, title="Send the proposal",
                status="in_progress", creator_id=user.id, deadline=T0 - H)
    stuck = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, title="Design the banner",
                 status="blocked", creator_id=user.id)
    db_session.add_all([late, stuck])
    await db_session.flush()
    db_session.add(TaskLog(task_id=stuck.id, log_type="ai_execution_failed", content="stopped", created_at=T0,
                           meta={"error": "agent_loop_reported_blocked", "coordinator_outcome": {"details": {
                               "blocked_by": "agent", "submission": {"blocked_reason": "Need the logo files"},
                           }}}))
    await db_session.flush()

    async def sweep(now):
        for watcher in (WorkOverdueWatcher(), WorkBlockedWatcher()):
            await reconcile_watcher(db_session, watcher, now=now, entity_id=entity_id)
        await delivery.deliver_attention(now=now)

    # The Task's own outcome said it is blocked; only the deadline is new.
    await sweep(T0 + H)
    [told] = await _chat(db_session, entity_id, workspace.id)
    assert "Past its deadline: Send the proposal" in told and f"(/tasks/{late.id})" in told
    [call] = notices.await_args_list
    assert (call.kwargs["user_id"], call.kwargs["type"]) == (user.id, "task_overdue")

    await sweep(T0 + 25 * H)
    reminder = (await _chat(db_session, entity_id, workspace.id))[1]
    assert "Still blocked: Design the banner (waiting over a day)" in reminder
    assert f"Need the logo files. [Open the task](/tasks/{stuck.id})" in reminder
    assert notices.await_args_list[1].kwargs["type"] == "task_hitl_reminder"
    await sweep(T0 + 26 * H)
    assert len(await _chat(db_session, entity_id, workspace.id)) == 2
