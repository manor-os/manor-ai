"""New Task audit copy follows its actor and one execution-language snapshot."""
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from packages.core.constants.task import TASK_STATUSES
from packages.core.i18n import SUPPORTED_LOCALES, TRANSLATIONS, get_locale, set_locale, t
from packages.core.models.base import generate_ulid
from packages.core.models.task import TaskLog
from packages.core.models.user import User
from packages.core.services import task_service


@pytest.fixture(autouse=True)
def restore_request_locale():
    previous = get_locale()
    set_locale("en")
    yield
    set_locale(previous)


@pytest.fixture
def quiet_events(monkeypatch):
    monkeypatch.setattr("packages.core.services.event_emitter.emit", Mock())
    monkeypatch.setattr("packages.core.services.realtime.queue_task_update_after_commit", Mock())


def make_user(entity_id, locale):
    user_id = generate_ulid()
    return User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.local",
                password_hash="unused", role="owner", status="active", locale=locale)


@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
def test_execution_templates_cover_every_language_without_translating_payloads(locale):
    keys = [key for key in TRANSLATIONS["en"] if key.startswith(("task.log.", "task.status.", "task.verdict."))]
    keys += [f"task.execution.{suffix}" for suffix in (
        "finished", "turn_tools", "turn_response", "turn_failed", "turn_waiting_approval",
        "unknown_tool", "supervisor_verdict", "supervisor_exhaustion", "finalized", "action_rejected",
    )]
    assert all(key in TRANSLATIONS[locale] for key in keys)
    assert all(f"task.status.{status}" in TRANSLATIONS[locale] for status in TASK_STATUSES)
    payload = 'Original 中文 {"status":"completed"}'
    for key, field in (
        ("task.log.created", "title"), ("task.execution.turn_tools", "tools"),
        ("task.execution.turn_waiting_approval", "tool"), ("task.execution.turn_failed", "error"),
        ("task.execution.supervisor_verdict", "reason"), ("task.execution.supervisor_exhaustion", "reason"),
    ):
        values = {"turn": 2, "max_turns": 3, "verdict": t("task.verdict.done", locale=locale), field: payload}
        visible = t(key, locale=locale, **values)
        assert payload in visible
        assert "{turn}" not in visible
        if locale != "en":
            assert visible != t(key, locale="en", **values)
    assert "13.7" in t("task.execution.finished", locale=locale, status=t("task.status.completed", locale=locale), turns=2, seconds=13.7)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
async def test_new_creation_and_manual_status_logs_use_saved_actor_not_viewer(db_session, quiet_events, locale):
    creator = make_user(generate_ulid(), locale)
    updater = make_user(creator.entity_id, "fr" if locale != "fr" else "zh")
    db_session.add_all([creator, updater])
    await db_session.flush()
    title = 'Original task title {"locale":"en"}'
    task = await task_service.create_task(db_session, creator.entity_id, title=title, creator_id=creator.id)
    created = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == task.id, TaskLog.log_type == "create"))).scalar_one()
    assert created.content == t("task.log.created", locale=locale, title=title)
    assert created.created_by == creator.id
    assert task.title == title

    await task_service.update_task(db_session, task.id, creator.entity_id, user_id=updater.id,
                                   status="in_progress", dispatch_plan=False)
    changed = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == task.id, TaskLog.log_type == "status_change"))).scalar_one()
    assert changed.content == t("task.log.status_changed", locale=updater.locale,
                                old_status=t("task.status.pending", locale=updater.locale),
                                new_status=t("task.status.in_progress", locale=updater.locale))
    assert task.status == "in_progress"
    assert created.content == t("task.log.created", locale=locale, title=title)


