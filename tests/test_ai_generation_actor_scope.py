"""Actor language scope survives generated-content service boundaries."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_workflow_generation_keeps_actor_and_explicit_output_language(monkeypatch):
    from packages.core.services import workflow_generator

    calls = []

    async def completion(messages, **kwargs):
        calls.append((messages, kwargs))
        return SimpleNamespace(content=json.dumps({
            "name": "生成周报",
            "steps": [{
                "id": "report",
                "type": "llm",
                "name": "撰写报告",
                "config": {"prompt": "Write the customer report in English."},
            }],
        }))

    monkeypatch.setattr(workflow_generator, "runtime_execute_text_completion", completion)
    monkeypatch.setattr(workflow_generator, "resolve_response_locale", AsyncMock(return_value="zh"))
    result = await workflow_generator.generate_workflow(
        "创建中文界面标签，但客户报告用英语", None, "entity_1", user_id="operator_1",
    )

    assert calls[0][1]["user_id"] == "operator_1"
    assert calls[0][1]["entity_id"] == "entity_1"
    assert "客户报告用英语" in calls[0][0][-1]["content"]
    assert result["steps"][0]["config"]["prompt"] == "Write the customer report in English."


@pytest.mark.asyncio
async def test_briefing_validation_retry_keeps_actor_and_recipient_language(monkeypatch):
    from packages.core.briefing import prompt

    calls = []

    async def completion(system_prompt, user_prompt, **kwargs):
        calls.append((system_prompt, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(content="invalid JSON")
        return SimpleNamespace(content=json.dumps({
            "briefing_id": "briefing_1",
            "headline": "有一封邮件待回复",
            "items": [],
        }))

    monkeypatch.setattr(prompt, "runtime_execute_briefing_completion", completion)
    result = await prompt.generate_briefing_via_llm(
        workspace_name="Ops",
        briefing_id="briefing_1",
        signals=[{"source": "gmail", "source_ref": "email_1", "body_text": "Can you help?"}],
        memory_snippets=[],
        goals_snapshot=[],
        entity_id="entity_1",
        workspace_id="workspace_1",
        user_id="operator_1",
    )

    assert result.headline == "有一封邮件待回复"
    assert len(calls) == 2
    assert all(call[1]["user_id"] == "operator_1" for call in calls)
    assert all(call[1]["workspace_id"] == "workspace_1" for call in calls)
    assert "recipient's language" in calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "expected_user_id", "expected_surface"),
    [
        ("web", "operator_1", None),
        ("workspace", "operator_1", None),
        ("webchat", None, "public_customer_chat"),
        ("email", None, "external_channel_chat"),
    ],
)
async def test_background_summary_preserves_operator_or_customer_scope(
    monkeypatch, channel, expected_user_id, expected_surface,
):
    from packages.core import database
    from packages.core.services import conversation_history

    conversation = SimpleNamespace(
        entity_id="entity_1", workspace_id="workspace_1", user_id="operator_1",
        channel=channel, summary="Earlier facts.",
    )
    captured = {}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            captured["committed"] = True

        async def execute(self, statement):
            if getattr(statement, "is_update", False):
                conversation.summary = statement.compile().params["summary"]
                return SimpleNamespace(rowcount=1)
            return SimpleNamespace(scalar_one_or_none=lambda: "workspace_1")

    async def get_conversation(_db, _conversation_id):
        return conversation

    async def completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="Updated facts.")

    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(conversation_history, "_get_conversation_for_history", get_conversation)
    monkeypatch.setattr(conversation_history, "runtime_execute_conversation_summary_completion", completion)
    await conversation_history.update_conversation_summary(
        "conversation_1", [SimpleNamespace(role="user", content="Keep this decision.")],
    )

    assert captured["user_id"] == expected_user_id
    assert captured["runtime_surface"] == expected_surface
    assert captured["prior_summary"] == "Earlier facts."
    assert captured["committed"] is True
    assert conversation.summary == "Updated facts."


@pytest.mark.asyncio
async def test_prompt_preview_router_passes_authenticated_actor(monkeypatch):
    from apps.api.routers import agents

    captured = {}

    async def preview(**kwargs):
        captured.update(kwargs)
        return "你好"

    monkeypatch.setattr(agents, "preview_agent_prompt", preview)
    result = await agents.preview_prompt(
        SimpleNamespace(system_prompt="Be concise.", test_message="Hello"),
        user=SimpleNamespace(id="operator_1", entity_id="entity_1"),
    )

    assert result == {"response": "你好"}
    assert captured["user_id"] == "operator_1"
    assert captured["entity_id"] == "entity_1"


@pytest.mark.asyncio
async def test_agent_draft_questions_and_edit_keep_actor(monkeypatch):
    from packages.core.services import agent_generator, agent_service

    calls = []
    agent = SimpleNamespace(
        id="agent_1", entity_id="entity_1", name="Helper", description="",
        category="", tags=[], system_prompt="Help the operator.",
    )

    async def completion(messages, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=json.dumps({
            "name": "助手", "description": "帮助用户", "system_prompt": "帮助用户完成工作。",
        }))

    async def get_agent(_db, _agent_id):
        return agent

    async def update_agent(_db, _agent_id, _entity_id, **_fields):
        return agent

    monkeypatch.setattr(agent_generator, "runtime_execute_text_completion", completion)
    monkeypatch.setattr(agent_generator, "resolve_response_locale", AsyncMock(return_value="zh"))
    monkeypatch.setattr(agent_service, "get_agent", get_agent)
    monkeypatch.setattr(agent_service, "update_agent", update_agent)
    frames = [frame async for frame in agent_generator.generate_agent_draft_streaming(
        "Create a helper", "entity_1", user_id="operator_1",
    )]
    await agent_generator.agent_clarifying_questions(
        "Create a helper", entity_id="entity_1", user_id="operator_1",
    )
    await agent_generator.update_agent_via_ai(
        "agent_1", "Be concise", "entity_1", object(), user_id="operator_1",
    )

    assert frames[-1][0] == "draft"
    assert frames[-1][1]["name"] == "助手"
    assert len(calls) == 3
    assert all(call["user_id"] == "operator_1" for call in calls)


@pytest.mark.asyncio
async def test_skill_generation_review_and_patch_keep_actor(monkeypatch):
    from packages.core.ai.runtime import skills
    from packages.core.services import skill_generator

    calls = []
    spec = {"name": "weekly-report", "system_prompt": "撰写周报。"}

    async def completion(messages, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="PASS" if len(calls) == 2 else json.dumps(spec))

    monkeypatch.setattr(skills, "runtime_execute_text_completion", completion)
    draft = await skill_generator.generate_skill_draft(
        "Create a weekly report skill", "entity_1", user_id="operator_1",
    )
    existing = SimpleNamespace(
        name="weekly-report", slug="weekly-report", display_name="周报", description="",
        system_prompt="撰写周报。", tools=[], input_schema={}, output_format="markdown",
        category="reporting", tags=[], version="1.0.0",
    )
    patch = await skill_generator.build_skill_update_patch(
        existing, "Include next steps", "entity_1", user_id="operator_1",
    )
    await skills.runtime_skill_clarifying_questions(
        "Create a report skill", entity_id="entity_1", user_id="operator_1",
    )

    assert draft.spec == spec
    assert patch["version"] == "1.0.1"
    assert len(calls) == 4
    assert all(call["user_id"] == "operator_1" for call in calls)


@pytest.mark.asyncio
async def test_skill_generation_uses_creator_locale_without_changing_ownership(monkeypatch):
    from packages.core.ai.runtime import skills
    from packages.core.services import skill_generator

    calls = []
    persisted = {}

    async def completion(messages, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="PASS" if len(calls) == 2 else json.dumps({
            "name": "weekly-report", "system_prompt": "撰写周报。",
        }))

    async def persist(draft, **kwargs):
        persisted.update(kwargs)
        return SimpleNamespace(id="skill_1")

    monkeypatch.setattr(skills, "runtime_execute_text_completion", completion)
    monkeypatch.setattr(skill_generator, "persist_generated_skill_draft", persist)
    monkeypatch.setattr(skill_generator, "resolve_response_locale", AsyncMock(return_value="zh"))
    await skill_generator.generate_skill(
        "Create a weekly report skill", "entity_1", object(), owner_user_id="operator_1",
    )

    assert all(call["user_id"] == "operator_1" for call in calls)
    assert persisted["owner_user_id"] == "operator_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["draft", "create", "update"])
async def test_skill_tool_generation_actions_keep_runtime_actor(monkeypatch, action):
    from packages.core.ai.tools import skill_tools

    captured = {}

    async def runtime_action(**kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(skill_tools, f"runtime_{action}_skill_action", runtime_action)
    handler = getattr(skill_tools, f"_{action}_skill_handler")
    assert await handler(entity_id="entity_1", user_id="operator_1") == "done"
    assert captured["user_id"] == "operator_1"
