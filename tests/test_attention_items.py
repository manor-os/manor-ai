"""Attention items follow platform state: open, age, resolve; never send."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.attention.reconcile import reconcile_watcher
from packages.core.attention.watchers.account_broken import AccountBrokenWatcher
from packages.core.attention.watchers.decision_pending import DecisionPendingWatcher
from packages.core.attention.watchers.needs_input import NeedsInputWatcher
from packages.core.models.attention import AttentionItem
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import HumanCommitment
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def _request(entity_id: str, workspace_id: str, *, created_at: datetime, **fields) -> HitlRequest:
    return HitlRequest(**{
        "id": generate_ulid(), "entity_id": entity_id, "workspace_id": workspace_id,
        "action_key": "workspace.automation.create", "risk_level": "medium", "origin_kind": "tool_call",
        "status": "pending", "hitl_type": "authorize", "dedup_key": f"attention:{generate_ulid()}",
        "payload": {"question": "Create the weekly report automation?"}, "context": {},
        "created_at": created_at, **fields,
    })


async def _items(db, source: str, entity_id: str) -> list[AttentionItem]:
    # Tests share a database: only ever look at this test's entity.
    return list((await db.execute(
        select(AttentionItem)
        .where(AttentionItem.source == source, AttentionItem.entity_id == entity_id)
        .order_by(AttentionItem.subject_id)
    )).scalars().all())


@pytest.mark.asyncio
async def test_waiting_work_opens_ages_and_resolves(db_session):
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="W", status="active"))
    approval = _request(entity_id, workspace_id, created_at=NOW - 2 * H)
    # No written question: the title names the action and its target, never
    # the reason code.
    file_hold = _request(entity_id, workspace_id, created_at=NOW - H, payload={"scope": "action"},
                         reason="approval.conversation_hold", action_key="workspace.file.create",
                         resource_kind="file", resource_id="review.md")
    # A Strategist cohort's card is a decision, not a generic input.
    cohort_card = _request(entity_id, workspace_id, created_at=NOW - 2 * H,
                           resource_kind="proposal", resource_id=generate_ulid())
    commitment = HumanCommitment(
        entity_id=entity_id, workspace_id=workspace_id, request_kind="input",
        source_kind="chat", source_id="c1", expected_input="Brand colours for the deck",
        status="waiting", requested_at=NOW - 30 * H,
    )
    db_session.add_all([approval, file_hold, cohort_card, commitment])
    await db_session.flush()

    watcher = NeedsInputWatcher()
    assert await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id) == {"opened": 3, "updated": 0, "resolved": 0}
    titles = {item.subject_id: item.title for item in await _items(db_session, watcher.key, entity_id)}
    assert titles[file_hold.id] == "Approval needed: Create workspace file: review.md"
    file_hold.status = "granted"
    await db_session.flush()
    assert (await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id))["resolved"] == 1
    items = {item.subject_type: item for item in await _items(db_session, watcher.key, entity_id) if item.state == "open"}
    assert set(items) == {"hitl_request", "human_commitment"}
    assert items["hitl_request"].title.startswith("Approval needed: Create the weekly report")
    assert (items["hitl_request"].stage, items["human_commitment"].stage) == (0, 1)
    assert items["hitl_request"].urgency == "act_today"

    # Same state: nothing changes. Older: the stage moves on the same row.
    assert await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id) == {"opened": 0, "updated": 0, "resolved": 0}
    assert await reconcile_watcher(db_session, watcher, now=NOW + 23 * H, entity_id=entity_id) == {
        "opened": 0, "updated": 1, "resolved": 0,
    }
    assert items["hitl_request"].stage == 1

    # Answered: resolved on the next sweep, never deleted.
    approval.status = "granted"
    commitment.status = "fulfilled"
    await db_session.flush()
    assert await reconcile_watcher(db_session, watcher, now=NOW + 24 * H, entity_id=entity_id) == {
        "opened": 0, "updated": 0, "resolved": 2,
    }
    assert {item.state for item in await _items(db_session, watcher.key, entity_id)} == {"resolved"}


@pytest.mark.asyncio
async def test_undecided_cohort_is_one_item_until_decided(db_session):
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    tasks = [
        Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, title=title,
             status="proposed", details={"strategist_review_id": "rv_1"}, created_at=NOW - 80 * H)
        for title in ("Draft the newsletter", "Schedule the posts")
    ]
    db_session.add_all([Workspace(id=workspace_id, entity_id=entity_id, name="W", status="active"), *tasks])
    await db_session.flush()

    watcher = DecisionPendingWatcher()
    await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id)
    [item] = await _items(db_session, watcher.key, entity_id)
    assert (item.kind, item.urgency, item.stage, item.subject_id) == ("decision_pending", "decide", 1, "rv_1")
    assert item.evidence["task_ids"] == sorted(task.id for task in tasks)

    for task in tasks:
        task.status = "pending"
    await db_session.flush()
    assert (await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id))["resolved"] == 1


@pytest.mark.asyncio
async def test_broken_account_is_an_item_in_each_workspace_that_uses_it(db_session, monkeypatch):
    from packages.core.services import account_incident_delivery

    entity_id = generate_ulid()
    uses, other = generate_ulid(), generate_ulid()
    integration = Integration(
        id=generate_ulid(), entity_id=entity_id, provider="gmail", status="active",
        config={"health_incident": {"opened_at": (NOW - H).isoformat(), "reason": "invalid_grant"}},
    )
    db_session.add(integration)
    await db_session.flush()

    async def scan(db, scanned_entity_id, now):
        if scanned_entity_id != entity_id:
            return []  # another test's incident in the shared database
        return [(uses, "Support", ["owner"], {"gmail"}), (other, "Content", ["owner"], {"instagram"})]

    monkeypatch.setattr(account_incident_delivery, "scan_entity_workspaces", scan)
    watcher = AccountBrokenWatcher()
    await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id)
    [item] = await _items(db_session, watcher.key, entity_id)
    assert (item.workspace_id, item.subject_type, item.title) == (uses, "integration", "Gmail needs reconnecting")

    integration.config = {}
    await db_session.flush()
    assert (await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id))["resolved"] == 1


@pytest.mark.asyncio
async def test_workspace_readers_list_open_items_and_others_cannot(client: AsyncClient):
    from packages.core.ai.tools.workspace_agent_tools import _workspace_list_attention_handler
    from packages.core.attention.sweep import run_attention_sweep
    from packages.core.database import async_session

    async def register(name: str) -> tuple[dict, str]:
        response = await register_user_and_get_token(client, json={
            "username": name, "email": f"{name}@test.com",
            "password": "TestPassword123!", "entity_name": f"{name} Corp",
        })
        assert response.status_code == 200
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        me = await client.get("/api/v1/auth/me", headers=headers)
        return headers, me.json()["entity_id"]

    owner, entity_id = await register("attention_owner")
    outsider, _ = await register("attention_outsider")
    created = await client.post("/api/v1/workspaces", json={"name": "Attention"}, headers=owner)
    assert created.status_code in (200, 201), created.text
    workspace_id = created.json()["id"]
    async with async_session() as db:
        db.add(_request(entity_id, workspace_id, created_at=datetime.now(timezone.utc)))
        await db.commit()

    await run_attention_sweep()

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/attention-items", headers=owner)
    assert listed.status_code == 200, listed.text
    assert [item["kind"] for item in listed.json()["items"]] == ["needs_input"]
    denied = await client.get(f"/api/v1/workspaces/{workspace_id}/attention-items", headers=outsider)
    assert denied.status_code == 404

    # The in-product Workspace agent reads the same items from chat.
    answer = json.loads(await _workspace_list_attention_handler(entity_id=entity_id, workspace_id=workspace_id))
    assert answer["count"] == 1 and answer["items"][0]["urgency"] == "act_today"


@pytest.mark.asyncio
async def test_workspace_state_watchers_follow_their_source_fields(db_session):
    from packages.core.attention.watchers.workspace_state import (
        AutomationPausedWatcher,
        BudgetThresholdWatcher,
        GoalOffPaceWatcher,
        SetupIncompleteWatcher,
    )
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="W", status="active",
        budget_alert_state="warning_80",
        settings={"_blueprint": {"setup_monitor": {
            "ready": False, "fingerprint": "f1", "checked_at": NOW.isoformat(),
            "incomplete": [{"key": "gmail", "provider": "gmail", "label": "Gmail"}],
        }}},
    )
    job = ScheduledJob(job_id=f"job-{generate_ulid()}", entity_id=entity_id, workspace_id=workspace.id,
                       name="Weekly report", enabled=False, consecutive_errors=3, execution_type="workflow")
    goal = Goal(entity_id=entity_id, workspace_id=workspace.id, title="Grow followers", metric_key="followers",
                target_value=1000, status="active", pace_status="behind")
    db_session.add_all([workspace, job, goal])
    await db_session.flush()

    watchers = [SetupIncompleteWatcher(), AutomationPausedWatcher(), BudgetThresholdWatcher(), GoalOffPaceWatcher()]
    for watcher in watchers:
        await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id)
    items = {item.kind: item for watcher in watchers for item in await _items(db_session, watcher.key, entity_id)}
    assert items["setup_incomplete"].title == "Setup still needs: Gmail"
    assert items["automation_paused"].title == "Automation paused after 3 failed runs: Weekly report"
    assert (items["budget_threshold"].urgency, items["budget_threshold"].stage) == ("fyi", 0)
    assert (items["goal_off_pace"].urgency, items["goal_off_pace"].title) == ("fyi", "Goal behind: Grow followers")

    # Worse: the same rows move up. Fixed: they resolve.
    workspace.budget_alert_state = "critical_100"
    goal.pace_status = "at_risk"
    await db_session.flush()
    for watcher in watchers[2:]:
        await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id)
    assert (items["budget_threshold"].urgency, items["budget_threshold"].stage) == ("act_today", 1)
    assert (items["goal_off_pace"].urgency, items["goal_off_pace"].title) == ("act_today", "Goal at risk: Grow followers")

    workspace.settings = {"_blueprint": {"setup_monitor": {"ready": True, "incomplete": []}}}
    workspace.budget_alert_state = "normal"
    job.enabled = True
    goal.pace_status = "on_track"
    await db_session.flush()
    for watcher in watchers:
        await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id)
    assert {item.state for item in items.values()} == {"resolved"}


@pytest.mark.asyncio
async def test_work_needing_a_person_but_not_platform_faults(db_session):
    from packages.core.attention.watchers.work import WorkBlockedWatcher, WorkOverdueWatcher
    from packages.core.models.task import TaskLog

    entity_id, workspace_id = generate_ulid(), generate_ulid()

    def blocked_task(title: str, blocked_by: str) -> tuple[Task, TaskLog]:
        task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, title=title, status="blocked")
        log = TaskLog(task_id=task.id, log_type="ai_execution_failed", content="stopped",
                      meta={"error": "agent_loop_reported_blocked" if blocked_by == "agent" else "platform_fault",
                            "coordinator_outcome": {"details": {
                                "blocked_by": blocked_by,
                                "submission": {"blocked_reason": "Need the client's logo files from the owner."},
                            }}})
        return task, log

    needs_person, needs_person_log = blocked_task("Design the launch banner", "agent")
    platform, platform_log = blocked_task("Render the video", "platform")
    late = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, title="Send the proposal",
                status="in_progress", deadline=NOW - 2 * H)
    done_late = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, title="Old",
                     status="completed", deadline=NOW - 2 * H)
    db_session.add_all([needs_person, needs_person_log, platform, platform_log, late, done_late])
    await db_session.flush()

    blocked, overdue = WorkBlockedWatcher(), WorkOverdueWatcher()
    await reconcile_watcher(db_session, blocked, now=NOW, entity_id=entity_id)
    await reconcile_watcher(db_session, overdue, now=NOW, entity_id=entity_id)
    [blocked_item] = await _items(db_session, blocked.key, entity_id)
    assert (blocked_item.subject_id, blocked_item.summary) == (
        needs_person.id, "Need the client's logo files from the owner.",
    )
    assert [item.subject_id for item in await _items(db_session, overdue.key, entity_id)] == [late.id]

    needs_person.status = "in_progress"
    late.deadline = NOW + 24 * H
    await db_session.flush()
    assert (await reconcile_watcher(db_session, blocked, now=NOW, entity_id=entity_id))["resolved"] == 1
    assert (await reconcile_watcher(db_session, overdue, now=NOW, entity_id=entity_id))["resolved"] == 1


@pytest.mark.asyncio
async def test_one_sweep_opens_many_items_through_the_real_upsert(db_session):
    """The partial-index upsert keeps working once Postgres caches a generic plan.

    The sixth execution of a prepared statement may use a generic plan, which
    cannot prove a parameterized ``WHERE state = $n`` implies the index's
    ``state = 'open'``. A sweep that opened more than five items then failed.
    """
    from packages.core.attention.contracts import DesiredItem

    entity_id = generate_ulid()

    class ManyWatcher:
        key = "test_many"

        async def desired(self, db, *, now):
            return [
                DesiredItem(entity_id=entity_id, workspace_id=None, kind="test_many", subject_type="test",
                            subject_id=f"s{index}", urgency="fyi", title=f"Item {index}", opened_at=now)
                for index in range(8)
            ]

    assert (await reconcile_watcher(db_session, ManyWatcher(), now=NOW, entity_id=entity_id))["opened"] == 8
    assert (await reconcile_watcher(db_session, ManyWatcher(), now=NOW, entity_id=entity_id))["opened"] == 0
    assert len(await _items(db_session, ManyWatcher.key, entity_id)) == 8


async def test_a_workflow_paused_at_a_person_wait_is_one_item_until_it_moves_on(db_session):
    """Approval and input waits are items; timer waits and parked tool approvals are not."""
    from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
    from packages.core.services.workflow_runtime_approvals import CONTEXT_KEY

    entity_id, workspace_id = generate_ulid(), generate_ulid()
    workflow = WorkflowDefinition(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
                                  name="Weekly report", steps=[])
    db_session.add_all([Workspace(id=workspace_id, entity_id=entity_id, name="W", status="active"), workflow])

    def paused(step_id: str, result: dict) -> WorkflowRun:
        return WorkflowRun(
            id=generate_ulid(), workflow_id=workflow.id, entity_id=entity_id, workspace_id=workspace_id,
            status="paused", current_step_id=step_id,
            step_results={step_id: {"status": "paused", "step_id": step_id, **result}},
            execution_trace=[{"node_id": step_id, "status": "paused",
                              "completed_at": (NOW - 25 * H).isoformat()}],
        )

    approval = paused("owner_approval", {"wait_type": "approval", "review_title": "Approve the draft"})
    timer = paused("cool_down", {"wait_type": "timer", "output": "Waiting for timer"})
    # A wait that also has a pending HitlRequest (a tool approval parks the
    # run on one) is already an item: the run must not be a second one.
    parked = paused("publish", {"wait_type": "approval", "code": "runtime_approval_required"})
    parked_request = _request(entity_id, workspace_id, created_at=NOW - H, context={
        CONTEXT_KEY: {"payload": {"workflow_run_id": parked.id}},
    })
    db_session.add_all([approval, timer, parked, parked_request])
    await db_session.flush()

    watcher = NeedsInputWatcher()
    await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id)
    items = await _items(db_session, watcher.key, entity_id)
    by_type = {item.subject_type: item for item in items}
    assert sorted(by_type) == ["hitl_request", "workflow_run"]
    run_item = by_type["workflow_run"]
    assert (run_item.subject_id, run_item.title, run_item.stage) == (
        f"{approval.id}:owner_approval", "Approval needed: Weekly report · Approve the draft", 1,
    )
    assert (run_item.evidence["workflow_run_id"], run_item.evidence["label"]) == (
        approval.id, "Weekly report · Approve the draft",
    )

    # Approved: the run moves on and the item resolves.
    approval.status = "running"
    approval.current_step_id = "publish"
    await db_session.flush()
    await reconcile_watcher(db_session, watcher, now=NOW, entity_id=entity_id)
    await db_session.refresh(run_item)
    assert (run_item.state, run_item.resolved_at) == ("resolved", NOW)
