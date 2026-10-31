from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ledger import event_types as et
from packages.core.ledger.service import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.goal import Goal
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Task
from packages.core.models.user import OAuthAccount, User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services import account_incident_delivery, hitl_reminders
from packages.core.services.account_incidents import open_incident
from packages.core.models.notification import Notification
from packages.core.services import workspace_digest
from packages.core.services.notification_service import delete_notification
from packages.core.services.notify import notify as real_notify
from packages.core.services.settings_service import update_user_preferences
from packages.core.services.workspace_digest import (
    Digest, DigestItem, build_user_digest, render_digest, run_workspace_digest_sweep,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=1)


async def _member(db, entity_id: str) -> User:
    user = User(id=generate_ulid(), entity_id=entity_id, email=f"u-{generate_ulid()}@example.com",
                password_hash="x", role="member", status="active")
    db.add_all([user, UserMembership(user_id=user.id, entity_id=entity_id, role="member", status="active")])
    await db.flush()
    return user


async def _workspace(db, entity_id: str, name: str, *, staff: User | None = None) -> Workspace:
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name=name, status="active")
    db.add(workspace)
    if staff is not None:
        db.add(WorkspaceStaff(workspace_id=workspace.id, user_id=staff.id, role="member", status="active"))
    await db.flush()
    return workspace


async def _event(db, workspace: Workspace, event_type: str, source_id: str, *, at: datetime, **fields):
    await record_event(
        db, entity_id=workspace.entity_id, workspace_id=workspace.id, event_type=event_type,
        source_kind=fields.pop("source_kind", "task"), source_id=source_id,
        idempotency_key=f"test:{generate_ulid()}", occurred_at=at, **fields,
    )


async def _task(db, workspace: Workspace, title: str) -> Task:
    task = Task(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                title=title, status="completed")
    db.add(task)
    await db.flush()
    return task


@pytest.mark.asyncio
async def test_completed_tasks_since_the_last_digest_in_readable_workspaces(db_session):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    hidden = await _workspace(db_session, entity_id, "Finance")
    fresh = await _task(db_session, growth, "Publish the launch post")
    old = await _task(db_session, growth, "Old work")
    secret = await _task(db_session, hidden, "Close the books")
    await _event(db_session, growth, et.EXECUTION_COMPLETED, fresh.id, at=NOW - timedelta(hours=2))
    await _event(db_session, growth, et.EXECUTION_COMPLETED, old.id, at=SINCE - timedelta(hours=1))
    await _event(db_session, hidden, et.EXECUTION_COMPLETED, secret.id, at=NOW - timedelta(hours=2))
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    completed = digest.changed["completed"]
    assert [item.workspace for item in completed] == ["Growth"]
    assert "Publish the launch post" in completed[0].text
    assert completed[0].link == f"/tasks/{fresh.id}"
    assert digest.changed_more["completed"] == 0


@pytest.mark.asyncio
async def test_only_proposals_the_system_approved_count_as_auto_started(db_session):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    at = NOW - timedelta(hours=3)
    await _event(db_session, growth, et.PROPOSAL_ITEM_APPROVED, generate_ulid(), at=at,
                 source_kind="proposal", actor_kind="system", payload={"title": "Draft the newsletter"})
    await _event(db_session, growth, et.PROPOSAL_ITEM_APPROVED, generate_ulid(), at=at,
                 source_kind="proposal", actor_kind="user", payload={"title": "Hand-approved work"})
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    texts = [item.text for item in digest.changed["auto_started"]]
    assert len(texts) == 1 and "Draft the newsletter" in texts[0]


@pytest.mark.asyncio
async def test_a_re_run_task_is_listed_and_counted_once(db_session):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    task = await _task(db_session, growth, "Publish the launch post")
    for hours in (5, 2):
        await _event(db_session, growth, et.EXECUTION_COMPLETED, task.id, at=NOW - timedelta(hours=hours))
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert [item.link for item in digest.changed["completed"]] == [f"/tasks/{task.id}"]
    assert digest.changed_more["completed"] == 0


@pytest.mark.asyncio
async def test_a_human_request_the_system_approved_is_not_auto_started(db_session):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    await _event(db_session, growth, et.PROPOSAL_ITEM_APPROVED, generate_ulid(), at=NOW - timedelta(hours=3),
                 source_kind="proposal", actor_kind="system",
                 payload={"title": "Ask the owner for a logo", "kind": "human_request"})
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert digest.changed["auto_started"] == []
    assert digest.changed_more["auto_started"] == 0


@pytest.mark.asyncio
async def test_a_goals_pace_change_spans_its_first_and_last_event_in_the_window(db_session):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    busy = Goal(id=generate_ulid(), entity_id=entity_id, workspace_id=growth.id, title="MRR",
                metric_key="mrr", target_value=5000)
    returned = Goal(id=generate_ulid(), entity_id=entity_id, workspace_id=growth.id, title="Signups",
                    metric_key="signups", target_value=100)
    db_session.add_all([busy, returned])
    await db_session.flush()
    start = NOW - timedelta(hours=20)

    async def pace(goal: Goal, old: str, new: str, at: datetime) -> None:
        await _event(db_session, growth, et.GOAL_PACE_CHANGED, goal.id, at=at, source_kind="goal",
                     payload={"old_pace": old, "new_pace": new})

    # Signups moved and came back, before everything else.
    await pace(returned, "on_track", "behind", start - timedelta(hours=2))
    await pace(returned, "behind", "on_track", start - timedelta(hours=1))
    # MRR: on track → behind, then 100 flips that end behind again.
    await pace(busy, "on_track", "behind", start)
    for n in range(1, 101):
        old, new = ("behind", "at_risk") if n % 2 else ("at_risk", "behind")
        await pace(busy, old, new, start + timedelta(minutes=n))
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert [item.text for item in digest.changed["goals"]] == ["Goal “MRR” is now behind pace (was on track)"]
    assert digest.changed_more["goals"] == 0


@pytest.mark.asyncio
async def test_goal_movement_is_one_line_per_goal(db_session):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    won = Goal(id=generate_ulid(), entity_id=entity_id, workspace_id=growth.id, title="1k followers",
               metric_key="follower_count", target_value=1000)
    slipping = Goal(id=generate_ulid(), entity_id=entity_id, workspace_id=growth.id, title="MRR",
                    metric_key="mrr", target_value=5000)
    db_session.add_all([won, slipping])
    await db_session.flush()
    for goal, event_type, payload, hours in (
        (won, et.GOAL_PACE_CHANGED, {"old_pace": "on_track", "new_pace": "ahead"}, 5),
        (won, et.GOAL_ACHIEVED, {"value": 1000}, 4),
        (slipping, et.GOAL_PACE_CHANGED, {"old_pace": "on_track", "new_pace": "behind"}, 6),
        (slipping, et.GOAL_PACE_CHANGED, {"old_pace": "behind", "new_pace": "at_risk"}, 3),
    ):
        await _event(db_session, growth, event_type, goal.id, at=NOW - timedelta(hours=hours),
                     source_kind="goal", payload=payload)
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert [item.text for item in digest.changed["goals"]] == [
        "Goal “MRR” is now at risk (was on track)",
        "Goal “1k followers” achieved",
    ]


@pytest.mark.asyncio
async def test_open_cards_waiting_on_the_person_are_listed(db_session, monkeypatch):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)

    def _request(question: str, status: str) -> HitlRequest:
        at = NOW - timedelta(hours=5)
        return HitlRequest(id=generate_ulid(), entity_id=entity_id, workspace_id=growth.id,
                           action_key="task.request_input", origin_kind="task", status=status,
                           origin_task_id=generate_ulid(),
                           dedup_key=f"test:{generate_ulid()}", hitl_type="input",
                           payload={"question": question}, created_at=at, updated_at=at)

    mine = _request("Which date works?", "pending")
    answered = _request("Already answered?", "granted")
    someone_else = _request("Ask someone else?", "pending")
    db_session.add_all([mine, answered, someone_else])
    await db_session.commit()
    # Recipients depend on the card's Task (and Workspace), never on the card itself.
    recipients = {mine.origin_task_id: [user.id], answered.origin_task_id: [user.id],
                  someone_else.origin_task_id: [generate_ulid()]}

    async def _recipients(_db, request):
        return recipients[request.origin_task_id]

    monkeypatch.setattr(hitl_reminders, "reminder_recipients", _recipients)

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert [item.text for item in digest.waiting] == ["Which date works?"]
    assert digest.waiting[0].workspace == "Growth" and digest.waiting[0].link
    assert digest.waiting_more == 0


def _card(workspace: Workspace, question: str, *, at: datetime, task: Task | None = None) -> HitlRequest:
    return HitlRequest(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                       action_key="task.request_input", origin_kind="task", status="pending",
                       origin_task_id=task.id if task else None,
                       dedup_key=f"test:{generate_ulid()}", hitl_type="input",
                       payload={"question": question}, created_at=at, updated_at=at)


async def _own_task(db, workspace: Workspace, user: User) -> Task:
    task = Task(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                title="Plan the launch", creator_id=user.id)
    db.add(task)
    await db.flush()
    return task


@pytest.mark.asyncio
async def test_a_readable_workspace_card_for_someone_else_is_not_listed(db_session):
    """The real recipient rule: a Workspace card goes to the entity's admins, not this member."""
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    task = await _own_task(db_session, growth, user)
    db_session.add_all([
        _card(growth, "Which date works?", at=NOW - timedelta(hours=5), task=task),
        _card(growth, "Approve the budget?", at=NOW - timedelta(hours=6)),
    ])
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert [item.text for item in digest.waiting] == ["Which date works?"]


@pytest.mark.asyncio
async def test_a_persons_newer_card_is_found_behind_many_older_cards_for_others(db_session):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    task = await _own_task(db_session, growth, user)
    db_session.add_all([
        *[_card(growth, f"Other {n}?", at=NOW - timedelta(hours=10, minutes=n)) for n in range(60)],
        _card(growth, "Which date works?", at=NOW - timedelta(hours=1), task=task),
    ])
    await db_session.commit()

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert [item.text for item in digest.waiting] == ["Which date works?"]
    assert digest.waiting_more == 0 and not digest.waiting_more_is_lower_bound


@pytest.mark.asyncio
async def test_recipients_and_links_are_resolved_once_per_run(db_session, monkeypatch):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    task = await _own_task(db_session, growth, user)
    db_session.add_all([
        _card(growth, "Which date works?", at=NOW - timedelta(hours=2), task=task),
        _card(growth, "Which venue?", at=NOW - timedelta(hours=1), task=task),
    ])
    await db_session.commit()
    real_recipients, real_link = hitl_reminders.reminder_recipients, hitl_reminders.card_link
    recipient_calls: list[str] = []
    link_calls: list[str] = []

    async def counting_recipients(db, request):
        recipient_calls.append(request.id)
        return await real_recipients(db, request)

    async def counting_link(db, request):
        link_calls.append(request.id)
        return await real_link(db, request)

    monkeypatch.setattr(hitl_reminders, "reminder_recipients", counting_recipients)
    monkeypatch.setattr(hitl_reminders, "card_link", counting_link)
    recipients: dict = {}
    links: dict = {}

    for _ in range(2):  # two people of one run sharing these cards
        digest = await build_user_digest(db_session, user, since=SINCE, now=NOW,
                                         recipients=recipients, links=links)
        assert [item.text for item in digest.waiting] == ["Which date works?", "Which venue?"]

    assert len(recipient_calls) == 1
    assert len(link_calls) == 2


@pytest.mark.asyncio
async def test_overflow_past_a_full_scan_is_a_lower_bound(db_session, monkeypatch):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    growth = await _workspace(db_session, entity_id, "Growth", staff=user)
    task = await _own_task(db_session, growth, user)
    db_session.add_all([
        _card(growth, f"Question {n}?", at=NOW - timedelta(hours=n + 1), task=task) for n in range(3)
    ])
    await db_session.commit()
    monkeypatch.setattr(workspace_digest, "WAITING_SCAN", 3)
    monkeypatch.setattr(workspace_digest, "WAITING_LIMIT", 1)

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert len(digest.waiting) == 1
    assert digest.waiting_more == 2 and digest.waiting_more_is_lower_bound
    _title, body = render_digest(digest, local_date=date(2026, 10, 22))
    assert "…and more" in body and "…and 2 more" not in body


@pytest.mark.asyncio
async def test_broken_accounts_the_person_owns_or_depends_on_are_listed(db_session, monkeypatch):
    entity_id = generate_ulid()
    user = await _member(db_session, entity_id)
    someone = await _member(db_session, entity_id)
    growth = Workspace(id=generate_ulid(), entity_id=entity_id, name="Growth", status="active")
    broken = OAuthAccount(user_id=user.id, provider="google_calendar", provider_user_id=generate_ulid(),
                          profile=open_incident({}, reason="auth_failed", now=NOW))
    healthy = OAuthAccount(user_id=user.id, provider="github", provider_user_id=generate_ulid(), profile={})

    def _integration(provider: str, owner: str) -> Integration:
        return Integration(id=generate_ulid(), entity_id=entity_id, provider=provider, status="active",
                           owner_user_id=owner, config=open_incident({}, reason="auth_failed", now=NOW))

    db_session.add_all([
        growth, WorkspaceStaff(workspace_id=growth.id, user_id=user.id, role="owner", status="active"),
        broken, healthy,
        _integration("linkedin", user.id),
        _integration("gmail", someone.id),  # Growth, which the person owns, uses Gmail
        _integration("slack", someone.id),  # nothing the person owns uses Slack
    ])
    await db_session.commit()
    monkeypatch.setattr(account_incident_delivery, "_workspace_providers", AsyncMock(return_value={"gmail"}))

    digest = await build_user_digest(db_session, user, since=SINCE, now=NOW)

    assert sorted(item.text for item in digest.accounts) == [
        "Gmail needs reconnecting", "Google Calendar needs reconnecting", "Linkedin needs reconnecting",
    ]
    assert {item.link for item in digest.accounts} == {"/integrations"}
    assert {item.workspace for item in digest.accounts} == {"Accounts"}


def test_render_omits_empty_sections_and_shows_overflow():
    empty = Digest(changed={"completed": [], "goals": [], "auto_started": []},
                   changed_more={"completed": 0, "goals": 0, "auto_started": 0},
                   waiting=[], waiting_more=0, accounts=[], next_steps=[])
    assert empty.is_empty()

    digest = Digest(
        changed={"completed": [DigestItem("Growth", "Finished “Launch post”")], "goals": [], "auto_started": []},
        changed_more={"completed": 3, "goals": 0, "auto_started": 0},
        waiting=[], waiting_more=0,
        accounts=[DigestItem("Accounts", "Slack needs reconnecting", "/integrations")],
        next_steps=[],
    )
    assert not digest.is_empty()
    title, body = render_digest(digest, local_date=date(2026, 10, 22))
    assert title == "Your Workspaces · Oct 22"
    assert "What changed" in body and "Accounts" in body
    assert "Waiting on you" not in body
    assert "• Growth: Finished “Launch post”" in body
    assert "…and 3 more" in body
    assert "• Accounts: Slack needs reconnecting" in body


class _SessionContext:
    """The test's session, handed out as if it were a fresh one each time."""

    def __init__(self, db) -> None:
        self.db = db

    async def __aenter__(self):
        # A fresh session reads rows as they are now, not an identity-mapped copy.
        self.db.expunge_all()
        return self.db

    async def __aexit__(self, *_args):
        return False


_NON_EMPTY = Digest(
    changed={"completed": [DigestItem("Growth", "Finished “Launch post”")], "goals": [], "auto_started": []},
    changed_more={"completed": 0, "goals": 0, "auto_started": 0},
    waiting=[], waiting_more=0, accounts=[], next_steps=[],
)
_EMPTY = Digest(
    changed={"completed": [], "goals": [], "auto_started": []},
    changed_more={"completed": 0, "goals": 0, "auto_started": 0},
    waiting=[], waiting_more=0, accounts=[], next_steps=[],
)
# 09:05 in Shanghai (UTC+8, no DST), 10:05 an hour later.
SHANGHAI_DUE = datetime(2026, 10, 22, 1, 5, tzinfo=timezone.utc)
SHANGHAI_CATCH_UP = datetime(2026, 10, 22, 2, 40, tzinfo=timezone.utc)  # 10:40 there
SHANGHAI_PAST_NOON = datetime(2026, 10, 22, 4, 5, tzinfo=timezone.utc)  # 12:05 there


async def _sweep_user(db, monkeypatch, digest: Digest | None, *, timezone_name: str = "Asia/Shanghai"):
    """A committed user, a sweep wired to ``db``, and the calls made for that user only.

    The database is shared, so other people may be swept too: the fake builder
    answers only for ``user`` and everyone else is empty. ``digest=None``
    keeps the real builder.
    """
    user = await _member(db, generate_ulid())
    user.timezone = timezone_name
    await db.commit()
    builds: list[dict] = []

    async def fake_build(_db, who, *, since, now, scans=None, **_caches):
        if who.id != user.id:
            return _EMPTY
        builds.append({"since": since, "now": now, "scans": scans})
        return digest

    sent = AsyncMock()
    monkeypatch.setattr(workspace_digest, "async_session", lambda: _SessionContext(db))
    if digest is not None:
        monkeypatch.setattr(workspace_digest, "build_user_digest", fake_build)
    monkeypatch.setattr(workspace_digest, "notify", sent)
    return user, builds, sent


def _calls_for(sent: AsyncMock, user: User):
    return [call for call in sent.await_args_list if call.kwargs["user_id"] == user.id]


@pytest.mark.asyncio
async def test_a_person_gets_their_digest_at_nine_in_their_own_time_zone(db_session, monkeypatch):
    user, _builds, sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)

    [call] = _calls_for(sent, user)
    assert call.kwargs["type"] == "workspace_digest"
    assert call.kwargs["severity"] == "info"
    assert call.kwargs["entity_id"] == user.entity_id
    assert call.kwargs["idempotency_key"] == f"workspace-digest:{user.id}:2026-10-22"
    assert call.kwargs["title"] == "Your Workspaces · Oct 22"

    sent.reset_mock()
    await run_workspace_digest_sweep(now=SHANGHAI_DUE - timedelta(minutes=10))  # 08:55 there: not yet
    assert _calls_for(sent, user) == []


@pytest.mark.asyncio
async def test_a_missed_morning_tick_is_caught_up_until_noon(db_session, monkeypatch):
    user, _builds, sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)

    await run_workspace_digest_sweep(now=SHANGHAI_CATCH_UP)

    [call] = _calls_for(sent, user)
    assert call.kwargs["idempotency_key"] == f"workspace-digest:{user.id}:2026-10-22"


@pytest.mark.asyncio
async def test_someone_who_already_has_todays_digest_is_not_rebuilt(db_session, monkeypatch):
    user, builds, sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)
    db_session.add(Notification(
        entity_id=user.entity_id, user_id=user.id, type="workspace_digest", title="Today",
        created_at=SHANGHAI_DUE,  # 09:05 local today
    ))
    await db_session.commit()

    await run_workspace_digest_sweep(now=SHANGHAI_CATCH_UP)

    assert builds == []
    assert _calls_for(sent, user) == []


@pytest.mark.asyncio
async def test_after_noon_local_time_a_missing_digest_is_not_sent(db_session, monkeypatch):
    user, builds, sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)

    await run_workspace_digest_sweep(now=SHANGHAI_PAST_NOON)

    assert builds == []
    assert _calls_for(sent, user) == []


@pytest.mark.asyncio
async def test_an_empty_digest_is_not_sent(db_session, monkeypatch):
    user, builds, sent = await _sweep_user(db_session, monkeypatch, _EMPTY)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)

    assert len(builds) == 1
    assert _calls_for(sent, user) == []


@pytest.mark.asyncio
async def test_without_stored_state_since_is_the_last_digest_or_a_day_back(db_session, monkeypatch):
    # Empty, so nothing is sent and no state is stored between the two ticks.
    user, builds, _sent = await _sweep_user(db_session, monkeypatch, _EMPTY)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)
    assert builds[-1]["since"] == SHANGHAI_DUE - timedelta(hours=24)

    last = SHANGHAI_DUE - timedelta(hours=23, minutes=58)
    db_session.add(Notification(
        entity_id=user.entity_id, user_id=user.id, type="workspace_digest", title="Yesterday",
        created_at=last,
    ))
    await db_session.commit()
    await run_workspace_digest_sweep(now=SHANGHAI_DUE)
    assert builds[-1]["since"] == last


@pytest.mark.asyncio
async def test_two_ticks_in_one_window_send_one_digest(db_session, monkeypatch):
    user, _builds, sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)
    await run_workspace_digest_sweep(now=SHANGHAI_DUE + timedelta(minutes=10))

    keys = {call.kwargs["idempotency_key"] for call in _calls_for(sent, user)}
    assert keys == {f"workspace-digest:{user.id}:2026-10-22"}


@pytest.mark.asyncio
async def test_the_real_notify_leaves_one_digest_after_repeated_ticks(db_session, monkeypatch):
    user, _builds, _sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)
    monkeypatch.setattr(workspace_digest, "notify", real_notify)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)
    await run_workspace_digest_sweep(now=SHANGHAI_DUE + timedelta(minutes=10))

    rows = (await db_session.execute(
        select(Notification).where(Notification.user_id == user.id, Notification.type == "workspace_digest")
    )).scalars().all()
    assert len(rows) == 1


async def _digest_rows(db, user: User) -> list[Notification]:
    return list((await db.execute(
        select(Notification).where(Notification.user_id == user.id, Notification.type == "workspace_digest")
    )).scalars().all())


async def _stored_preferences(db, user_id: str) -> dict:
    return (await db.execute(select(User.preferences).where(User.id == user_id))).scalar_one()


def _digest_state(last_local_date: str, until: datetime) -> dict:
    return {"digest_state": {"last_local_date": last_local_date, "until": until.isoformat()}}


@pytest.mark.asyncio
async def test_deleting_todays_digest_does_not_send_it_again(db_session, monkeypatch):
    user, builds, _sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)
    monkeypatch.setattr(workspace_digest, "notify", real_notify)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)
    [row] = await _digest_rows(db_session, user)
    assert await delete_notification(db_session, row.id, user.id, entity_ids=[user.entity_id])
    await db_session.commit()
    await run_workspace_digest_sweep(now=SHANGHAI_CATCH_UP)

    assert len(builds) == 1
    assert await _digest_rows(db_session, user) == []


@pytest.mark.asyncio
async def test_since_is_the_stored_window_end_and_the_new_one_is_stored(db_session, monkeypatch):
    user, builds, _sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)
    until = SHANGHAI_DUE - timedelta(days=1)
    user.preferences = _digest_state("2026-10-21", until)
    db_session.add(Notification(
        entity_id=user.entity_id, user_id=user.id, type="workspace_digest", title="Yesterday",
        created_at=until + timedelta(minutes=3),  # inserted after that sweep's window end
    ))
    await db_session.commit()

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)

    assert builds[-1]["since"] == until
    assert builds[-1]["now"] == SHANGHAI_DUE
    stored = await _stored_preferences(db_session, user.id)
    assert stored["digest_state"] == {
        "last_local_date": "2026-10-22", "until": SHANGHAI_DUE.isoformat(),
    }


@pytest.mark.asyncio
async def test_the_lookback_is_capped_at_seven_days(db_session, monkeypatch):
    user, builds, _sent = await _sweep_user(db_session, monkeypatch, _NON_EMPTY)
    user.preferences = _digest_state("2026-09-22", SHANGHAI_DUE - timedelta(days=30))
    await db_session.commit()

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)

    assert builds[-1]["since"] == SHANGHAI_DUE - timedelta(days=7)


@pytest.mark.asyncio
async def test_an_event_after_the_window_end_reaches_the_next_digest(db_session, monkeypatch):
    user, _builds, _sent = await _sweep_user(db_session, monkeypatch, None)
    growth = await _workspace(db_session, user.entity_id, "Growth", staff=user)
    early = await _task(db_session, growth, "Early task")
    late = await _task(db_session, growth, "Late task")
    await _event(db_session, growth, et.EXECUTION_COMPLETED, early.id, at=SHANGHAI_DUE - timedelta(hours=1))
    await db_session.commit()
    user_id, entity_id = user.id, user.entity_id
    bodies: list[str] = []

    async def slow_notify(**kwargs):
        if kwargs["user_id"] != user_id:
            return
        bodies.append(kwargs["body"])
        # The row lands a minute after the sweep's ``now``.
        db_session.add(Notification(
            entity_id=entity_id, user_id=user_id, type=kwargs["type"], title=kwargs["title"],
            created_at=SHANGHAI_DUE + timedelta(minutes=1),
        ))
        await db_session.commit()

    monkeypatch.setattr(workspace_digest, "notify", slow_notify)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE)
    await _event(db_session, growth, et.EXECUTION_COMPLETED, late.id, at=SHANGHAI_DUE + timedelta(seconds=30))
    await db_session.commit()
    await run_workspace_digest_sweep(now=SHANGHAI_DUE + timedelta(days=1))

    assert len(bodies) == 2
    assert "Early task" in bodies[0] and "Late task" not in bodies[0]
    assert "Late task" in bodies[1] and "Early task" not in bodies[1]


@pytest.mark.asyncio
async def test_a_database_error_for_one_person_leaves_the_rest_of_the_page(db_session, monkeypatch):
    people = [await _member(db_session, generate_ulid()) for _ in range(2)]
    for person in people:
        person.timezone = "Asia/Shanghai"
    await db_session.commit()
    first, second = sorted(person.id for person in people)

    async def build(db, who, *, since, now, scans=None, **_caches):
        if who.id == first:
            await db.execute(text("SELECT 1/0"))
        return _NON_EMPTY if who.id == second else _EMPTY

    sent = AsyncMock()
    monkeypatch.setattr(workspace_digest, "async_session", lambda: _SessionContext(db_session))
    monkeypatch.setattr(workspace_digest, "build_user_digest", build)
    monkeypatch.setattr(workspace_digest, "notify", sent)

    await run_workspace_digest_sweep(now=SHANGHAI_DUE, batch=1_000_000)  # one page

    assert [c.kwargs["user_id"] for c in sent.await_args_list if c.kwargs["user_id"] in (first, second)] == [second]


@pytest.mark.asyncio
async def test_recording_a_digest_keeps_preferences_another_session_changed(db_session):
    user = await _member(db_session, generate_ulid())
    user.preferences = {"theme": "dark", "notifications": {"default_channels": ["email"]}}
    await db_session.commit()
    async with AsyncSession(db_session.bind) as other:
        row = (await other.execute(select(User).where(User.id == user.id))).scalar_one()
        row.preferences = {**row.preferences, "language": "fr"}
        await other.commit()

    await workspace_digest._record_digest_sent(
        db_session, user.id, local_date=date(2026, 10, 22), until=SHANGHAI_DUE,
    )
    await db_session.commit()

    assert await _stored_preferences(db_session, user.id) == {
        "theme": "dark",
        "language": "fr",
        "notifications": {"default_channels": ["email"]},
        "digest_state": {"last_local_date": "2026-10-22", "until": SHANGHAI_DUE.isoformat()},
    }


@pytest.mark.asyncio
async def test_a_stale_notification_settings_save_keeps_the_digest_state(db_session):
    """Replays the notification settings PUT's merge with a branch read before the digest was recorded."""
    user = await _member(db_session, generate_ulid())
    user.preferences = {"notifications": {"default_channels": ["inapp"]}}
    await db_session.commit()
    stale = dict((await _stored_preferences(db_session, user.id))["notifications"])

    await workspace_digest._record_digest_sent(
        db_session, user.id, local_date=date(2026, 10, 22), until=SHANGHAI_DUE,
    )
    await db_session.commit()
    await update_user_preferences(db_session, user.id, {"notifications": {**stale, "default_channels": ["email"]}})
    await db_session.commit()

    stored = await _stored_preferences(db_session, user.id)
    assert stored["notifications"] == {"default_channels": ["email"]}
    assert stored["digest_state"] == {"last_local_date": "2026-10-22", "until": SHANGHAI_DUE.isoformat()}

