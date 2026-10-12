"""New Task audit copy follows its actor and one execution-language snapshot."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from packages.core.ai import task_runner
from packages.core.ai.engine import LLMConfig
from packages.core.ai.runtime import RuntimeTaskAgentTurnResult
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.constants.task import TASK_STATUSES
from packages.core.i18n import SUPPORTED_LOCALES, TRANSLATIONS, get_locale, set_locale, t
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task, TaskLog
from packages.core.models.user import User
from packages.core.services import task_service, workspace_runtime


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


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["en", "zh", "fr"])
@pytest.mark.parametrize("failed", [False, True])
async def test_real_task_runner_keeps_log_snapshot_when_account_changes_mid_run(
    db_session, monkeypatch, quiet_events, locale, failed,
):
    entity_id = generate_ulid()
    user = make_user(entity_id, locale)
    workspace_id = None
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
                agent_id=MANOR_AGENT_ID,
                title="Return the requested English content", description="Keep original structured text.",
                status="pending", priority=3, task_type="general", details={"max_turns": 3},
                creator_id=user.id, owner_id=user.id, delegate_service_keys=[])
    db_session.add_all([user, task])
    await db_session.commit()

    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Reuse this fixture's per-test NullPool engine, never a cross-loop global pool.
    async_session = async_sessionmaker(db_session.bind, expire_on_commit=False)

    @asynccontextmanager
    async def no_billing(**kwargs):
        yield

    monkeypatch.setattr(task_runner, "runtime_task_llm_billing_context", no_billing)
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", AsyncMock(return_value=
        workspace_runtime.WorkspaceRuntimeEnvelope(workspace_id=workspace_id, task_id=task.id,
                                                   is_master=False, bound_tool_names=set(), mcp_allowed_names=set())))
    monkeypatch.setattr(task_runner, "runtime_record_task_runner_execution_evidence", AsyncMock())
    monkeypatch.setattr(task_runner, "runtime_persist_task_runner_runtime_events", AsyncMock())
    monkeypatch.setattr(task_runner, "runtime_emit_task_runner_status_event", Mock())
    body = 'English artifact body {"status":"completed"}'
    reason = "Original model reason 保留原文"
    next_locale = "ja" if locale != "ja" else "de"
    calls = 0

    async def turn(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Another device confirms a language after this execution's first log.
            async with async_session() as db:
                current = await db.get(User, user.id)
                current.locale = next_locale
                await db.commit()
        return RuntimeTaskAgentTurnResult(
            messages=kwargs["messages"], tools=kwargs["tools"], loaded_tool_names=set(),
            response_text=body if calls > 1 else "", tool_names=["write_file"] if calls == 1 else [],
            usage={}, had_tool_calls=calls == 1,
        )

    monkeypatch.setattr(task_runner, "runtime_execute_task_agent_turn", turn)
    runner = task_runner.TaskRunner(engine=SimpleNamespace(config=LLMConfig(model="test-model")), session_factory=async_session)
    monkeypatch.setattr(runner, "_supervise", AsyncMock(return_value={"verdict": "failed" if failed else "done", "reason": reason}))
    result = await runner.run(task.id)
    expected_status = "failed" if failed else "completed"
    assert result["status"] == expected_status, result
    assert result["response"] == body
    assert result["turns_used"] == 2
    assert result["supervisor_verdict"]["verdict"] == ("failed" if failed else "done")

    async with async_session() as db:
        logs = list((await db.execute(
            select(TaskLog).where(TaskLog.task_id == task.id)
            .order_by(TaskLog.created_at, TaskLog.id)
        )).scalars())
        persisted = await db.get(Task, task.id)
        account = await db.get(User, user.id)
        assert account.locale == next_locale
        assert persisted.status == expected_status
        assert persisted.actual_output["response"] == body
    by_type = {}
    for log in logs:
        by_type.setdefault(log.log_type, []).append(log.content)
    assert by_type["ai_agent_turn"][0] == t("task.execution.turn_tools", locale=locale, turn=1, max_turns=3, tools="write_file")
    assert by_type["ai_agent_turn"][1] == t("task.execution.turn_response", locale=locale, turn=2, max_turns=3, chars=len(body))
    assert by_type["ai_supervisor_verdict"] == [t("task.execution.supervisor_verdict", locale=locale,
        verdict=t(f"task.verdict.{'failed' if failed else 'done'}", locale=locale), reason=reason)]
    assert by_type["status_change"] == [t("task.log.status_changed", locale=locale,
        old_status=t("task.status.in_progress", locale=locale), new_status=t(f"task.status.{expected_status}", locale=locale))]
    terminal = by_type["ai_execution_failed" if failed else "ai_execution_completed"]
    assert terminal == [t("task.execution.finished", locale=locale, status=t(f"task.status.{expected_status}", locale=locale),
                         turns=2, seconds=result["duration_ms"] / 1000)]
    assert by_type["comment"] == [body]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
@pytest.mark.parametrize("initiative", [False, True])
async def test_rejected_resume_localizes_visible_response_but_preserves_machine_contract(monkeypatch, locale, initiative):
    import json
    import time

    marker_key = task_runner.TASK_RUNTIME_HITL_DETAIL_KEY
    task = SimpleNamespace(id="task", entity_id="entity", agent_id="agent",
                           details={marker_key: {"hitl_id": "hitl", "generation": 2, "state": "resuming"}})
    resume = SimpleNamespace(checkpoint=SimpleNamespace(execution_user_id="exact-requester", hitl_id="hitl", generation=2, turns_used=1))
    resolve = AsyncMock(return_value=locale)
    monkeypatch.setattr(task_runner, "resolve_response_locale", resolve)
    captured = {}

    async def finalize(*args, **kwargs):
        captured.update(kwargs)
        # A concurrent preference change must not affect this rejection event.
        resolve.return_value = "de" if locale != "de" else "ja"
        set_locale(resolve.return_value)
        return task, True

    monkeypatch.setattr(task_runner, "_finalize_with_terminal_guard", finalize)
    monkeypatch.setattr(task_runner, "commit_fenced_execution_boundary", AsyncMock())
    turn = AsyncMock(side_effect=AssertionError("A rejected action must not run an agent"))
    monkeypatch.setattr(task_runner, "runtime_execute_task_agent_turn", turn)
    runner = task_runner.TaskRunner()
    log = AsyncMock()
    monkeypatch.setattr(runner, "_log", log)
    db = SimpleNamespace(commit=AsyncMock())
    result = await runner._finish_rejected_runtime_resume(
        db, task=task, resume=resume,
        initiative_marker={} if initiative else None, task_start=time.monotonic(),
    )
    # Reuse the current transaction rather than acquiring another admission slot.
    resolve.assert_awaited_once_with("exact-requester", db=db)
    turn.assert_not_awaited()
    expected_status = "completed" if initiative else "cancelled"
    expected_verdict = "done" if initiative else "failed"
    assert result["status"] == captured["status"] == expected_status
    assert captured["log_locale"] == locale
    assert captured["actual_output"]["verdict"] == expected_verdict
    assert captured["details"][marker_key]["state"] == "finished"
    assert captured["details"][marker_key]["result_status"] == expected_status
    assert result["supervisor_verdict"] == {"verdict": expected_verdict,
        "reason": t("task.execution.action_rejected_response", locale=locale)}
    if initiative:
        assert json.loads(result["response"]) == {"version": "1", "decision": "silent",
            "reason_code": "runtime_action_rejected", "evidence_refs": []}
    else:
        assert result["response"] == t("task.execution.action_rejected_response", locale=locale)
    assert captured["actual_output"]["response"] == result["response"]
    assert captured["details"]["ai_result"]["response"] == result["response"]
    assert log.await_args.args == ("task", "ai_execution_completed", t("task.execution.action_rejected", locale=locale))
