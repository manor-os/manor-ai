from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Task
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services import hitl_reminders
from packages.core.services.hitl_reminders import due_stage
from packages.core.services.notification_workspace_callbacks import resolve_workspace_hitl_recipients

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


class _SessionContext:
    def __init__(self, db_session) -> None:
        self.db_session = db_session

    async def __aenter__(self):
        return self.db_session

    async def __aexit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _use_test_session(db_session, monkeypatch):
    monkeypatch.setattr(hitl_reminders, "async_session", lambda: _SessionContext(db_session))


async def _user(db, entity_id: str, *, role: str) -> User:
    user = User(id=generate_ulid(), entity_id=entity_id, email=f"u-{generate_ulid()}@example.com",
                password_hash="x", role=role, status="active")
    db.add_all([user, UserMembership(user_id=user.id, entity_id=entity_id, role=role, status="active")])
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_workspace_policy_wins_and_an_empty_policy_opts_out(db_session):
    entity_id = generate_ulid()
    admin = User(id=generate_ulid(), entity_id=entity_id, email=f"a-{generate_ulid()}@example.com",
                 password_hash="x", role="admin", status="active")
    chosen = generate_ulid()
    with_policy = Workspace(id=generate_ulid(), entity_id=entity_id, name="A", status="active",
                            settings={"notification_policy": {"hitl_notify_user_ids": [chosen]}})
    opted_out = Workspace(id=generate_ulid(), entity_id=entity_id, name="B", status="active",
                          settings={"notification_policy": {"hitl_notify_user_ids": []}})
    plain = Workspace(id=generate_ulid(), entity_id=entity_id, name="C", status="active", settings={})
    db_session.add_all([admin, with_policy, opted_out, plain])
    await db_session.flush()
    assert await resolve_workspace_hitl_recipients(db_session, entity_id=entity_id, workspace_id=with_policy.id) == [chosen]
    assert await resolve_workspace_hitl_recipients(db_session, entity_id=entity_id, workspace_id=opted_out.id) == []
    assert await resolve_workspace_hitl_recipients(db_session, entity_id=entity_id, workspace_id=plain.id) == [admin.id]


def test_two_reminders_at_24h_and_96h_spaced_by_72h_then_silence():
    assert due_stage(created_at=T0, state={}, now=T0 + 23 * H) is None
    assert due_stage(created_at=T0, state={}, now=T0 + 24 * H) == 1
    first = {"sent": 1, "last_at": (T0 + 24 * H).isoformat()}
    assert due_stage(created_at=T0, state=first, now=T0 + 95 * H) is None
    assert due_stage(created_at=T0, state=first, now=T0 + 96 * H) == 2
    assert due_stage(created_at=T0, state={"sent": 2, "last_at": (T0 + 96 * H).isoformat()},
                     now=T0 + 400 * H) is None


def test_a_late_first_reminder_still_waits_72h_for_the_final_one():
    late = {"sent": 1, "last_at": (T0 + 100 * H).isoformat()}
    assert due_stage(created_at=T0, state=late, now=T0 + 120 * H) is None
    assert due_stage(created_at=T0, state=late, now=T0 + 172 * H) == 2


def test_requests_older_than_the_window_are_never_reminded():
    assert due_stage(created_at=T0, state={}, now=T0 + 24 * 6 * H) is None


@pytest.mark.asyncio
async def test_sweep_batches_one_message_per_person_and_workspace_and_records_state(db_session, monkeypatch):
    """Batches never span Workspaces, so notify() always gets a workspace_id to check read access."""
    entity_id = generate_ulid()
    growth = Workspace(id=generate_ulid(), entity_id=entity_id, name="Growth", status="active")
    support = Workspace(id=generate_ulid(), entity_id=entity_id, name="Support", status="active")
    now = T0 + 25 * H
    requests = [
        HitlRequest(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                    action_key="task.request_input", origin_kind="task", status="pending",
                    dedup_key=f"test:{generate_ulid()}",
                    hitl_type="input", payload={"question": f"Question {n}?", "why": "w"},
                    created_at=T0, updated_at=T0)
        for n, workspace in enumerate((growth, growth, support))
    ]
    db_session.add_all([growth, support, *requests])
    await db_session.commit()
    monkeypatch.setattr(hitl_reminders, "reminder_recipients", AsyncMock(return_value=["user-a"]))
    sent = AsyncMock()
    monkeypatch.setattr(hitl_reminders, "notify", sent)

    try:
        await hitl_reminders.run_hitl_reminder_sweep(now=now)
        calls = {c.kwargs["workspace_id"]: c.kwargs for c in sent.await_args_list
                 if c.kwargs["entity_id"] == entity_id}
        assert set(calls) == {growth.id, support.id}
        batch = calls[growth.id]
        assert batch["user_id"] == "user-a" and batch["type"] == "task_hitl_reminder"
        assert batch["severity"] == "info" and batch["title"] == "2 items are waiting on you in Growth"
        assert "Question 0?" in batch["body"] and "Question 1?" in batch["body"]
        assert "Question 2?" not in batch["body"]
        assert "Question 2?" in calls[support.id]["title"]
        for request in requests:
            await db_session.refresh(request)
            assert request.context["reminders"]["sent"] == 1

        sent.reset_mock()
        await hitl_reminders.run_hitl_reminder_sweep(now=now + H)
        assert [c for c in sent.await_args_list if c.kwargs["entity_id"] == entity_id] == []

        await hitl_reminders.run_hitl_reminder_sweep(now=now + 72 * H)
        calls = [c.kwargs for c in sent.await_args_list if c.kwargs["entity_id"] == entity_id]
        assert len(calls) == 2 and all("over 4 days" in call["body"] for call in calls)
        for request in requests:
            await db_session.refresh(request)
            assert request.context["reminders"]["sent"] == 2
    finally:
        await db_session.execute(delete(HitlRequest).where(HitlRequest.entity_id == entity_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_an_answer_recorded_during_the_sweep_survives_and_is_not_reminded(db_session, monkeypatch):
    """The answer path locks the row and writes context; a stale sweep write must not erase it."""
    from conftest import TEST_DATABASE_URL
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    entity_id = generate_ulid()
    request = HitlRequest(id=generate_ulid(), entity_id=entity_id, workspace_id=generate_ulid(),
                          action_key="task.request_input", origin_kind="task", status="pending",
                          dedup_key=f"test:{generate_ulid()}", hitl_type="input",
                          payload={"question": "Which date?"}, created_at=T0, updated_at=T0)
    db_session.add(request)
    await db_session.commit()
    answer = {"text": "Friday", "answered_by": "user-a"}

    async def answer_meanwhile(_db, _request):
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, class_=AsyncSession)() as other:
                await other.execute(update(HitlRequest).where(HitlRequest.id == request.id)
                                    .values(status="granted", context={"answer": answer}))
                await other.commit()
        finally:
            await engine.dispose()
        return ["user-a"]

    monkeypatch.setattr(hitl_reminders, "reminder_recipients", answer_meanwhile)
    sent = AsyncMock()
    monkeypatch.setattr(hitl_reminders, "notify", sent)
    try:
        await hitl_reminders.run_hitl_reminder_sweep(now=T0 + 25 * H)
        assert [c for c in sent.await_args_list if c.kwargs["entity_id"] == entity_id] == []
        await db_session.refresh(request)
        assert request.status == "granted"
        assert request.context["answer"] == answer
        assert "reminders" not in request.context
    finally:
        await db_session.execute(delete(HitlRequest).where(HitlRequest.entity_id == entity_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_rows_that_are_never_due_do_not_block_due_ones(db_session, monkeypatch):
    """Old never-reminded rows sort first; with a small limit they must not starve a due row."""
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    now = T0 + 300 * H

    def _request(created_at, question):
        return HitlRequest(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
                           action_key="task.request_input", origin_kind="task", status="pending",
                           dedup_key=f"test:{generate_ulid()}", hitl_type="input",
                           payload={"question": question}, created_at=created_at, updated_at=created_at)

    stuck = [_request(now - 130 * H, f"Old {n}?") for n in range(3)]
    nobody = _request(now - 26 * H, "Nobody to ask?")
    due = _request(now - 25 * H, "Due now?")
    db_session.add_all([*stuck, nobody, due])
    await db_session.commit()

    async def recipients(_db, request):
        return [] if request.id == nobody.id else ["user-a"]

    monkeypatch.setattr(hitl_reminders, "reminder_recipients", recipients)
    sent = AsyncMock()
    monkeypatch.setattr(hitl_reminders, "notify", sent)
    try:
        await hitl_reminders.run_hitl_reminder_sweep(now=now, limit=2)
        calls = [c.kwargs for c in sent.await_args_list if c.kwargs["entity_id"] == entity_id]
        assert len(calls) == 1 and "Due now?" in calls[0]["title"]
        await db_session.refresh(nobody)
        assert nobody.context["reminders"]["sent"] == 2
        assert nobody.context["reminders"]["skipped"] == "no_recipients"
        for request in stuck:
            await db_session.refresh(request)
            assert "reminders" not in request.context
    finally:
        await db_session.execute(delete(HitlRequest).where(HitlRequest.entity_id == entity_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_a_task_card_reminds_the_tasks_people_not_the_workspace_admins(db_session):
    entity_id = generate_ulid()
    await _user(db_session, entity_id, role="admin")
    creator = await _user(db_session, entity_id, role="member")
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Growth", status="active", settings={})
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(WorkspaceStaff(workspace_id=workspace.id, user_id=creator.id, role="member", status="active"))
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, title="Launch note",
                status="waiting_on_customer", task_type="general", details={}, creator_id=creator.id)
    db_session.add(task)
    await db_session.flush()
    request = HitlRequest(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                          origin_task_id=task.id, action_key="task.request_input", origin_kind="task",
                          status="pending", dedup_key=f"test:{generate_ulid()}", hitl_type="input",
                          payload={"question": "Which date?"})

    assert await hitl_reminders.reminder_recipients(db_session, request) == [creator.id]


@pytest.mark.asyncio
async def test_a_task_card_reminder_is_written_to_the_task_timeline(db_session, monkeypatch):
    from sqlalchemy import select

    from packages.core.constants.task import TaskLogType
    from packages.core.models.task import TaskLog

    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Growth", status="active", settings={})
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, title="Launch note",
                status="waiting_on_customer", task_type="general", details={})
    db_session.add_all([workspace, task])
    await db_session.flush()
    request = HitlRequest(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                          origin_task_id=task.id, action_key="task.request_input", origin_kind="task",
                          status="pending", dedup_key=f"test:{generate_ulid()}", hitl_type="input",
                          payload={"question": "Which date?"}, created_at=T0, updated_at=T0)
    db_session.add(request)
    await db_session.commit()
    monkeypatch.setattr(hitl_reminders, "reminder_recipients", AsyncMock(return_value=["user-a"]))
    monkeypatch.setattr(hitl_reminders, "notify", AsyncMock())
    try:
        await hitl_reminders.run_hitl_reminder_sweep(now=T0 + 25 * H)
        logs = (await db_session.execute(select(TaskLog).where(
            TaskLog.task_id == task.id, TaskLog.log_type == TaskLogType.AI_HITL_REMINDER.value,
        ))).scalars().all()
        assert len(logs) == 1
        assert logs[0].meta["hitl_request_id"] == request.id and logs[0].meta["stage"] == 1
        assert "Which date?" in logs[0].content
    finally:
        await db_session.execute(delete(HitlRequest).where(HitlRequest.entity_id == entity_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_a_task_card_without_people_follows_the_workspace_opt_out(db_session):
    """No creator or assignee: the Workspace policy decides, and an explicit [] means nobody."""
    entity_id = generate_ulid()
    admin = await _user(db_session, entity_id, role="admin")
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Ops", status="active",
                          settings={"notification_policy": {"hitl_notify_user_ids": []}})
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(WorkspaceStaff(workspace_id=workspace.id, user_id=admin.id, role="owner", status="active"))
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, title="Nightly sync",
                status="waiting_on_customer", task_type="general", details={})
    db_session.add(task)
    await db_session.flush()
    request = HitlRequest(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                          origin_task_id=task.id, action_key="task.request_input", origin_kind="task",
                          status="pending", dedup_key=f"test:{generate_ulid()}", hitl_type="input",
                          payload={"question": "Retry?"})

    assert await hitl_reminders.reminder_recipients(db_session, request) == []
