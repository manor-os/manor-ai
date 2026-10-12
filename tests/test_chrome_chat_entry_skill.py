from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.prompt_adapter import ChatContext
from packages.core.ai.runtime.prompt_sections import available_skills_section
from packages.core.ai.runtime.skill_invocation_policy import (
    SkillInvocationPolicy,
    render_skill_invocation_policy,
    trusted_skill_invocation_policy,
)
from packages.core.ai.runtime.skills import descriptor_from_skill




@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_chat_entry_exposes_trusted_before_tools_skill_before_discovery():
    descriptor = descriptor_from_skill(
        SimpleNamespace(
            id="fixture-skill-id",
            entity_id=None,
            slug="fixture-browser",
            name="fixture-browser",
            description="Operate the paired fixture browser.",
            tools=["mcp__fixture_browser__read_page"],
            config={
                "source": "builtin",
                "invocation_policy": {
                    "mode": "required_before_tools",
                    "semantic_trigger": "the user asks to operate the paired fixture browser",
                    "result_authority": "primary_source",
                },
            },
            system_prompt="FULL FIXTURE EXECUTION INSTRUCTIONS",
        ),
        source="builtin",
    )
    ctx = ChatContext(
        active_user_message="Use the paired fixture browser to update the current record.",
        runtime_skill_descriptors=[descriptor],
        tool_names=["invoke_skill", "search_tools"],
    )

    section = await available_skills_section(ctx)

    assert section is not None
    assert 'invoke_skill(skill_id="fixture-skill-id"' in section
    assert "as your first tool call" in section
    assert "before any other tool discovery or execution" in section
    assert "FULL FIXTURE EXECUTION INSTRUCTIONS" not in section
    assert ctx.auto_forced_tool_calls == []


def test_before_tools_policy_preserves_semantic_selection_and_existing_answer_policy():
    config = {
        "mode": "required_before_tools",
        "semantic_trigger": "the user asks to operate their local browser",
        "result_authority": "primary_source",
    }
    policy = SkillInvocationPolicy.from_config(config)
    rendered = render_skill_invocation_policy(skill_id="browser-skill", policy=policy)

    assert policy.to_dict() == config
    assert "as your first tool call" in rendered
    assert "semantic meaning, not literal keyword matching" in rendered

    answer_policy = SkillInvocationPolicy.from_config(
        {**config, "mode": "required_before_answer"}
    )
    answer_rendered = render_skill_invocation_policy(
        skill_id="reference-skill", policy=answer_policy
    )
    assert "before answering" in answer_rendered
    assert "as your first tool call" not in answer_rendered


def test_entity_skill_cannot_inject_a_before_tools_policy():
    skill = SimpleNamespace(
        entity_id="entity-1",
        config={
            "source": "builtin",
            "invocation_policy": {
                "mode": "required_before_tools",
                "semantic_trigger": "the user asks any question",
                "result_authority": "primary_source",
            },
        },
    )

    assert trusted_skill_invocation_policy(skill) is None
