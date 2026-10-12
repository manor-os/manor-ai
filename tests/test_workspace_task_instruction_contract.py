"""Workspace composite Task creation preserves the downstream output contract."""
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.ai.runtime import workspace_task_actions
from packages.core.ai.runtime.planning import runtime_planner_task_prompt
from packages.core.ai.tools.manor_tool import _manor_handler
from packages.core.constants.task_actors import TaskActor
from packages.core.models.task import Task
from packages.core.models.workspace import AgentSubscription
from packages.core.services import task_service


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [None, "", "  ", "Save the requested English document with its original title."])
async def test_composite_task_instructions_reach_planner_without_losing_rules(monkeypatch, canonical):
    subscriptions = Mock()
    subscriptions.scalars.return_value.all.return_value = [AgentSubscription(
        id="subscription", entity_id="entity", workspace_id="workspace",
        agent_id="writer", service_key="content_creation", status="active",
    )]
    db = SimpleNamespace(
        commit=AsyncMock(), refresh=AsyncMock(), execute=AsyncMock(return_value=subscriptions),
    )

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr("packages.core.database.async_session", session)
    monkeypatch.setattr(workspace_task_actions, "_load_workspace", AsyncMock(return_value=SimpleNamespace(id="workspace")))
    monkeypatch.setattr(workspace_task_actions, "_resolve_owner_binding", AsyncMock(return_value=(None, None, [])))
    monkeypatch.setattr(workspace_task_actions, "_resolve_goal_links_for_task", AsyncMock(return_value=([], "none")))
    monkeypatch.setattr(workspace_task_actions, "runtime_tool_is_user_requested", AsyncMock(return_value=True))
    monkeypatch.setattr(task_service, "agent_log_authorship", AsyncMock(return_value=("user", {}, TaskActor.USER)))
    task_log = AsyncMock()
    monkeypatch.setattr(task_service, "add_task_log", task_log)
    locale = AsyncMock(return_value="zh")
    monkeypatch.setattr(task_service, "_task_log_locale", locale)
    monkeypatch.setattr("packages.core.services.workspace_service.record_activity", AsyncMock())
    dispatch = Mock()
    monkeypatch.setattr("packages.core.tasks.ai_tasks.plan_and_run_task.delay", dispatch)
    saved = []

    async def create_task(_db, entity_id, **fields):
        fields.pop("creator_agent_id")
        fields.pop("origin_task_id")  # host provenance, not a Task column
        task = Task(id="task", entity_id=entity_id, status="pending", **fields)
        saved.append(task)
        return task

    async def update_task(_db, task_id, entity_id, **fields):
        assert task_id == saved[0].id
        assert entity_id == saved[0].entity_id
        saved[0].status = fields["status"]
        saved[0].details = fields["details"]
        return saved[0]

    monkeypatch.setattr(task_service, "create_task", create_task)
    monkeypatch.setattr(task_service, "update_task", update_task)
    instructions = (
        "使用通用知识创建并保存 LANG-P1-20260911.md，"
        "用一个标题和三个简短要点解释月亮为什么有月相。文档正文明确使用英语。"
    )
    rules = [
        {"description": value} for value in (
            "Use general knowledge only.", "No search.", "No business data access.",
            "No outbound messages.", "Complete via background Task with an execution plan.",
        )
    ]
    params = {"action": "create_task", "params": {
        "title": "LANG-P1-20260911", "description": "", "start": True,
        "instructions": instructions, "rules": rules,
    }}
    if canonical is not None:
        params["params"]["runtime_instructions"] = canonical
    original = deepcopy(params)

    result = json.loads(await _manor_handler(
        entity_id="entity", _user_id_from_context="user", workspace_id="workspace",
        conversation_id="conversation", action="workspace", params=params,
    ))

    locale.assert_awaited_once_with(db, "user")
    assert task_log.await_args.args[3] == "工作区 AI 已记录任务要求。"
    assert result["created"] is True
    assert result["dispatched"] is True
    assert result["dispatch_mode"] == "planner"
    # Subscription resolution plus the transactional Task execution outbox.
    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()
    dispatch.assert_not_called()
    assert params == original
    assert saved[0].description == ""
    assert saved[0].creator_id == "user"
    runtime = saved[0].details["runtime_context"]
    expected = canonical if canonical and canonical.strip() else instructions
    assert runtime["instructions"] == expected
    assert [rule["description"] for rule in runtime["rules"]] == [rule["description"] for rule in rules]
    assert runtime["captured_from"] == {"conversation_id": "conversation", "user_id": "user"}

    # This is the actual task/user message builder used by Planner._generate_plan.
    prompt = runtime_planner_task_prompt(saved[0])
    binding_prompt = prompt.split("Details JSON:", 1)[0]
    assert expected in binding_prompt
    assert all(rule["description"] in binding_prompt for rule in rules)
    if canonical and canonical.strip():
        assert instructions not in prompt
    else:
        assert "LANG-P1-20260911.md" in prompt
        assert "月亮为什么有月相" in prompt
        assert "文档正文明确使用英语" in prompt
