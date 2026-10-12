from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.i18n import t
from packages.core.plans import executor
from packages.core.workspace_chat import notifiers


LANGUAGE_PROGRESS = [
    ("en", "Execution plan started", "Layer"),
    ("zh", "执行计划已启动", "第 1 层"),
    ("es", "Plan de ejecución iniciado", "Nivel"),
    ("fr", "Plan d'exécution démarré", "Niveau"),
    ("de", "Ausführungsplan gestartet", "Ebene"),
    ("ja", "実行計画を開始", "第 1 層"),
    ("vi", "Đã bắt đầu kế hoạch thực thi", "Lớp"),
]


@pytest.mark.parametrize("locale,headline,layer", LANGUAGE_PROGRESS)
@pytest.mark.asyncio
async def test_plan_start_uses_exact_resolved_actor_for_task_and_chat(monkeypatch, locale, headline, layer):
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    plan = SimpleNamespace(
        id="plan", entity_id="entity", task_id="task", workspace_id=None,
        status="draft", execution_mode="live", started_at=None, dispatcher_state={},
    )
    steps = [
        SimpleNamespace(
            plan_id="plan", step_key=f"step_{index}", kind="llm", service_key=None, provider=None,
            action_key=None, capability_id=None, description=f"原始步骤 {index}",
            depends_on=[], step_status="running", result=None, error=None,
        )
        for index in range(2)
    ]
    runner = executor.PlanExecutor(session_factory=session)
    monkeypatch.setattr(runner, "_load_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(runner, "_lock_cycle_mutation_origin", AsyncMock(return_value=plan))
    monkeypatch.setattr(runner, "_cancel_orphaned_coordinator_children", AsyncMock(return_value=False))
    monkeypatch.setattr(runner, "_wake_waiting_coordinator_parents", lambda _steps: False)
    monkeypatch.setattr(runner, "_stage_schedule_self", AsyncMock())
    monkeypatch.setattr(runner, "_cleanup_terminal_approvals_after_commit", AsyncMock())
    monkeypatch.setattr(runner, "_load_task_title", AsyncMock(return_value="原始任务"))
    monkeypatch.setattr(runner, "_all_steps", AsyncMock(return_value=steps))
    billing = AsyncMock(return_value=SimpleNamespace(user_id="exact-requester"))
    monkeypatch.setattr(executor, "runtime_ensure_task_billing_context", billing)
    resolve = AsyncMock(return_value=locale)
    monkeypatch.setattr(executor, "resolve_plan_response_locale", resolve)
    log = AsyncMock()
    monkeypatch.setattr("packages.core.services.task_service.add_task_log", log)
    started = AsyncMock()
    monkeypatch.setattr(executor.chat_notify, "notify_plan_started", started)

    first = await runner.run_cycle("plan")

    resolve.assert_awaited_once_with(db, plan, user_id="exact-requester")
    assert log.await_args.args[1:3] == ("task", "plan_started")
    assert headline in log.await_args.args[3]
    assert layer in log.await_args.args[3]
    assert "原始步骤 0" in log.await_args.args[3]
    assert log.await_args.kwargs["metadata"] == {"plan_id": "plan", "step_count": 2, "execution_mode": "live"}
    assert started.await_args.kwargs["locale"] == locale
    assert started.await_args.kwargs["task_title"] == "原始任务"
    assert plan.status == "running"
    assert first["next_action"] == "schedule_self"

    second = await runner.run_cycle("plan")
    assert second == first
    assert started.await_count == 1
    assert log.await_count == 1
    assert resolve.await_count == 2
    assert billing.await_count == 2

    plan.status = "completed"
    assert await runner.run_cycle("plan") == {"plan_id": "plan", "status": "completed", "next_action": "stop"}
    assert started.await_count == 1


@pytest.mark.parametrize("locale,headline,layer", LANGUAGE_PROGRESS)
@pytest.mark.asyncio
async def test_plan_chat_copies_keep_localized_text_and_language_independent_metadata(monkeypatch, locale, headline, layer):
    post = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)
    steps = [
        {"key": f"step_{index}", "description": f"Original step {index}", "depends_on": [], "status": "pending"}
        for index in range(2)
    ]
    original_steps = deepcopy(steps)
    await notifiers.notify_plan_started(
        entity_id="entity", workspace_id="workspace", plan_id="plan", task_id="task",
        task_title="原始标题 {exact}", step_count=2, execution_mode="dry_run", steps=steps, locale=locale,
    )
    assert post.await_count == 2
    for call in post.await_args_list:
        payload = call.kwargs
        assert headline in payload["body"]
        assert "原始标题 {exact}" in payload["body"]
        assert "`[dry_run]`" in payload["body"]
        assert payload["meta"] == {"step_count": 2, "task_title": "原始标题 {exact}"}
        assert payload["message_kind"] == "agent_update"
        assert payload["refs"] == [{"type": "plan", "id": "plan"}, {"type": "task", "id": "task"}]
    assert layer in post.await_args_list[0].kwargs["body"]
    assert "Original step 0" in post.await_args_list[0].kwargs["body"]
    assert "Original step 0" not in post.await_args_list[1].kwargs["body"]
    assert steps == original_steps


@pytest.mark.parametrize("locale,_,__", LANGUAGE_PROGRESS)
def test_task_progress_templates_preserve_agent_and_human_quote(locale, _, __):
    agent_name = "Atlas / 智能体 {agent_name}"
    human_quote = '保持原文: "yes", {"status":"ready"}'
    started = t("task.execution.started", locale=locale, agent_name=agent_name)
    resumed = t("task.execution.hitl_resumed", locale=locale, response=human_quote)
    assert started.startswith("[AI] ")
    assert resumed.startswith("[AI] ")
    assert agent_name in started
    assert human_quote in resumed
    if locale != "en":
        assert "Execution started" not in started
        assert "Resuming after HITL" not in resumed


@pytest.mark.asyncio
async def test_task_runner_log_preserves_plain_debug_text_and_translated_progress(monkeypatch):
    from packages.core.ai import task_runner

    db = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    log = AsyncMock()
    monkeypatch.setattr(task_runner, "add_task_log", log)
    runner = task_runner.TaskRunner(session_factory=session)
    runner._agent_id = "agent"
    runner._agent_name = "Original Agent"
    raw = '[AI] tool_call=get_report; result={"status":"ready"}'
    await runner._log("task", "ai_agent_turn", raw)
    assert log.await_args.args[3] == raw
    translated = t("task.execution.hitl_resumed", locale="ja", response='Original {"answer":"yes"}')
    await runner._log("task", "ai_hitl_resumed", translated)
    assert log.await_args.args[1:4] == ("task", "ai_hitl_resumed", translated)
    assert log.await_args.kwargs["metadata"]["agent_name"] == "Original Agent"
