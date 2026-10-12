from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.chrome_routing import detect_chrome_local_browser_route
from packages.core.ai.runtime.skill_forcing import (
    runtime_auto_skill_forced_tool_calls,
    runtime_forced_tool_calls_for_turn,
)
from packages.core.services import skill_service

CHROME_RECORDING_REQUEST = (
    "请调用 Chrome skill 打开 https://example.com，并录制一个无音频的操作片段。"
    "必须返回 recording_id。"
)


@pytest.mark.asyncio
async def test_explicit_chrome_does_not_use_keyword_auto_forcing(monkeypatch) -> None:
    async def list_skills(*_args, **_kwargs):
        raise AssertionError("Chrome text must route through structured discovery")

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

    assert calls == []


def test_terse_current_page_request_is_explicit_chrome_intent() -> None:
    assert detect_chrome_local_browser_route("用 Chrome 总结当前页") is not None


@pytest.mark.parametrize(
    "message",
    [
        "check Gmail inbox",
        "check Linkedin emmanuel 然后写回复 附上我们的calendar链接",
        "future_crm_v9 reconcile the west account",
    ],
)
@pytest.mark.asyncio
async def test_named_integration_does_not_use_keyword_auto_forcing(
    monkeypatch,
    message,
) -> None:
    async def list_skills(*_args, **_kwargs):
        raise AssertionError("provider text must not trigger direct Skill lookup")

    monkeypatch.setattr(skill_service, "list_skills", list_skills)
    ctx = SimpleNamespace(
        manual_skill_selected=False,
        db=object(),
        entity_id="entity-1",
        agent_id=None,
        tool_names=("invoke_skill", "search_tools"),
        active_user_message=message,
        workspace_id=None,
    )

    assert await runtime_auto_skill_forced_tool_calls(
        ctx,
        message,
    ) == []


STANDUP_WITH_INCIDENTAL_CODEX = (
    "Simon今日完成：1. jr music workflow 新版完成 and description about how to do "
    "2. Chrome extension + Chrome skill 重新调整，对照codex内置 "
    "3. chat 对话机制调整；跨对话轮次信息共享调整；"
)


@pytest.mark.asyncio
async def test_standup_does_not_auto_force_local_coding_skill(monkeypatch) -> None:
    async def list_skills(*_args, **_kwargs):
        return [
            SimpleNamespace(id="skill-aider", slug="mcp_aider", name="mcp_aider"),
            SimpleNamespace(
                id="skill-coding",
                slug="local-coding-operations",
                name="local-coding-operations",
            ),
        ]

    monkeypatch.setattr(skill_service, "list_skills", list_skills)
    ctx = SimpleNamespace(
        manual_skill_selected=False,
        db=object(),
        entity_id="entity-1",
        agent_id=None,
        tool_names=("invoke_skill",),
        active_user_message=STANDUP_WITH_INCIDENTAL_CODEX,
        workspace_id=None,
    )

    assert await runtime_auto_skill_forced_tool_calls(
        ctx,
        STANDUP_WITH_INCIDENTAL_CODEX,
    ) == []


@pytest.mark.asyncio
async def test_local_coding_intent_does_not_use_keyword_auto_forcing(monkeypatch) -> None:
    message = "请使用本地codex cli, 在本地Downloads/sub2api下帮我在 DEV_GUIDE.md 最后加上文字"

    async def list_skills(*_args, **_kwargs):
        raise AssertionError("Local coding intent must use structured discovery")

    monkeypatch.setattr(skill_service, "list_skills", list_skills)
    ctx = SimpleNamespace(
        manual_skill_selected=False,
        db=object(),
        entity_id="entity-1",
        agent_id=None,
        tool_names=("invoke_skill",),
        active_user_message=message,
        workspace_id=None,
    )

    calls = await runtime_auto_skill_forced_tool_calls(ctx, message)

    assert calls == []


def test_explicit_local_coding_selection_preserves_its_exact_skill_id() -> None:
    message = "Make the requested document edit."
    ctx = SimpleNamespace(
        initial_messages=[],
        auto_forced_tool_calls=[{
            "name": "invoke_skill", "arguments": {"skill_id": "skill-mcp-pack"},
        }],
    )
    refs = [{"id": "skill-coding", "slug": "local-coding-operations"}]

    assert runtime_forced_tool_calls_for_turn(ctx, refs, message) == [{
        "name": "invoke_skill",
        "arguments": {"skill_id": "skill-coding", "input": message},
    }]


@pytest.mark.asyncio
async def test_local_coding_auto_force_skips_mcp_guidance_packs(monkeypatch) -> None:
    message = "请使用本地codex cli, 在本地Downloads/sub2api下帮我在 DEV_GUIDE.md 最后加上文字"

    async def list_skills(*_args, **_kwargs):
        return [
            SimpleNamespace(id="skill-aider", slug="mcp_aider", name="mcp_aider"),
            SimpleNamespace(id="skill-codex", slug="mcp_codex_cli", name="mcp_codex_cli"),
        ]

    monkeypatch.setattr(skill_service, "list_skills", list_skills)
    ctx = SimpleNamespace(
        manual_skill_selected=False,
        db=object(),
        entity_id="entity-1",
        agent_id=None,
        tool_names=("invoke_skill",),
        active_user_message=message,
        workspace_id=None,
    )

    assert await runtime_auto_skill_forced_tool_calls(ctx, message) == []
