from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.skill_forcing import runtime_auto_skill_forced_tool_calls
from packages.core.services import skill_service
from tests.test_prompt_builder_routing import CHROME_RECORDING_REQUEST


@pytest.mark.asyncio
async def test_explicit_chrome_intent_forces_chrome_before_local_coding(monkeypatch) -> None:
    skills = [
        SimpleNamespace(slug="local_coding_operations", name="local_coding_operations"),
        SimpleNamespace(slug="chrome", name="chrome"),
    ]

    async def list_skills(*_args, **_kwargs):
        return skills

    monkeypatch.setattr(skill_service, "list_skills", list_skills)
    ctx = SimpleNamespace(
        manual_skill_selected=False,
        db=object(),
        entity_id="entity-1",
        agent_id=None,
        tool_names=("invoke_skill",),
        active_user_message=CHROME_RECORDING_REQUEST,
        workspace_id=None,
    )

    calls = await runtime_auto_skill_forced_tool_calls(ctx, CHROME_RECORDING_REQUEST)

    assert calls[0]["arguments"]["skill"] == "chrome"
