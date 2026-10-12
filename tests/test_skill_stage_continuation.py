"""A staged Skill's declared stage is the agent's to resume within the same turn.

Incident (local E2E, 2026-10-04): "用 Remotion 做一个 8 秒的动画视频…导出 mp4" →
invoke_skill(video-production) saved CANVAS.md and ended with prose asking the
parent to "continue the video workflow with the same canvas" (the Skill is
capped at 30 rounds and resumes per stage). The result read ``completed``,
nothing structured said otherwise, and the parent told the user "only the
canvas is done, the MP4 is not exported yet" after one tool call.

A Skill now declares its resume stages in its own config; the runtime enforces
the stage as validated JSON and holds the turn open until the Skill is invoked
again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction
from packages.core.ai.runtime.skills import (
    _runtime_format_invoke_skill_result,
    runtime_skill_continuation_from_content,
    runtime_skill_continuation_schema,
    runtime_skill_continuation_stages,
)

STAGES = ("canvas_ready", "production_in_progress")
CANVAS = "[CANVAS.md](/viewer/01M44RGWSDQJFGXWN38KSGQN8X)"


def staged(stage: str) -> str:
    return json.dumps({"stage": stage, "text": f"已保存画布 {CANVAS}"}, ensure_ascii=False)


def skill_result(content: str, **extra) -> str:
    return _runtime_format_invoke_skill_result("video-production", {
        "skill": "video-production", "content": content, "stop_reason": "completed",
        "tools_used": ["generate_file"], **extra,
    })


def answer(status: str, refs=(), limitations=()) -> str:
    return json.dumps({"kind": "answer", "text": "回复", "outcome": {
        "status": status, "evidence_refs": list(refs), "limitations": list(limitations),
        "claims": [{"operation": "invoke_skill", "status": status, "evidence_refs": list(refs)}]
        if status in {"completed", "partial", "blocked"} else [],
    }}, ensure_ascii=False)


def test_only_declared_well_formed_stages_open_the_channel():
    assert runtime_skill_continuation_stages({}) == ()
    assert runtime_skill_continuation_stages({"continuation_stages": "canvas_ready"}) == ()
    assert runtime_skill_continuation_stages(
        {"continuation_stages": ["canvas_ready", "delivered", "Bad Stage", 3, "canvas_ready"]}
    ) == ("canvas_ready",)
    schema = runtime_skill_continuation_schema(STAGES)
    assert schema["properties"]["stage"]["enum"] == [*STAGES, "delivered", "blocked"]
    assert runtime_skill_continuation_from_content(STAGES, staged("canvas_ready")) == {"stage": "canvas_ready"}
    for content in (staged("delivered"), staged("blocked"), "继续调用视频制作流程", None):
        assert runtime_skill_continuation_from_content(STAGES, content) is None
    assert runtime_skill_continuation_from_content((), staged("canvas_ready")) is None


def test_video_production_declares_its_resume_stages():
    config = json.loads(Path("packages/core/ai/skills/video-production/config.json").read_text())
    assert runtime_skill_continuation_stages(config) == STAGES


def test_continuation_is_host_owned_never_from_model_json():
    marked = json.loads(skill_result(staged("canvas_ready"), continuation={"stage": "canvas_ready"}))
    assert marked["status"] == "completed"
    assert marked["continuation"]["stage"] == "canvas_ready"
    assert "invoke this Skill again" in marked["continuation"]["required"]
    # A Skill that wrote the key itself, without the host marking it, opens nothing.
    forged = json.dumps({"stage": "canvas_ready", "text": "x", "continuation": {"stage": "canvas_ready"}})
    assert "continuation" not in json.loads(skill_result(forged))


@pytest.mark.asyncio
async def test_incident_canvas_stage_holds_the_turn_until_the_skill_is_resumed():
    contract = ChatEvidenceContract()
    contract.observe_tool_result(
        "invoke_skill", "stage1", skill_result(staged("canvas_ready"), continuation={"stage": "canvas_ready"}),
        {"skill_id": "video-production"},
    )
    decision = await contract.validate(answer("partial", ["stage1"], ["MP4 尚未导出"]))
    assert decision.action is FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "skill_stage_unfinished"
    assert "canvas_ready" in decision.diagnostic["message"]

    # A file saved on the way does not finish the stage; resuming the Skill does.
    contract.observe_tool_result(
        "generate_file", "file1", json.dumps({"created": True, "document": {"document_id": "d1", "fs_path": "a.md"}}),
        {"kind": "document"},
    )
    assert (await contract.validate(answer("partial", ["stage1"], ["MP4 尚未导出"]))).diagnostic["error"] == (
        "skill_stage_unfinished"
    )
    contract.observe_tool_result(
        "invoke_skill", "stage2", skill_result(staged("delivered")), {"skill_id": "video-production"},
    )
    assert contract.pending_sandbox_handoffs() == []


@pytest.mark.asyncio
async def test_each_further_stage_reopens_and_survives_a_checkpoint():
    contract = ChatEvidenceContract()
    contract.observe_tool_result(
        "invoke_skill", "stage1", skill_result(staged("canvas_ready"), continuation={"stage": "canvas_ready"}),
        {"skill_id": "video-production"},
    )
    contract.observe_tool_result(
        "invoke_skill", "stage2",
        skill_result(staged("production_in_progress"), continuation={"stage": "production_in_progress"}),
        {"skill_id": "video-production"},
    )
    pending = contract.pending_sandbox_handoffs()
    assert [(item["kind"], item["stage"], item["call_id"]) for item in pending] == [
        ("skill_continuation", "production_in_progress", "stage2"),
    ]
    restored = ChatEvidenceContract()
    restored.restore_checkpoint_state(contract.checkpoint_state())
    assert [(item["kind"], item["stage"]) for item in restored.pending_sandbox_handoffs()] == [
        ("skill_continuation", "production_in_progress"),
    ]


@pytest.mark.asyncio
async def test_an_observed_failure_after_the_stage_may_be_reported_as_blocked():
    contract = ChatEvidenceContract()
    contract.observe_tool_result(
        "invoke_skill", "stage1", skill_result(staged("canvas_ready"), continuation={"stage": "canvas_ready"}),
        {"skill_id": "video-production"},
    )
    contract.observe_tool_result(
        "invoke_skill", "stage2",
        json.dumps({"status": "failed", "skill": "video-production", "error": "credit_exhausted"}),
        {"skill_id": "video-production"},
    )
    decision = await contract.validate(answer("blocked", ["stage2"], ["视频生成额度不足"]))
    assert decision.diagnostic is None or decision.diagnostic.get("error") != "skill_stage_unfinished"


def _staged_service(monkeypatch, execute):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from test_skill_execution_observations import setup_service

    service = setup_service(monkeypatch, execute, lambda *_: None)
    monkeypatch.setattr(service, "get_skill", AsyncMock(return_value=SimpleNamespace(
        id="skill-1", slug="video-production", name="video-production", entity_id=None,
        config={"continuation_stages": list(STAGES)}, tools=[], system_prompt="Work", skill_type="prompt",
    )))
    return service


def _loop_result(content: str):
    from types import SimpleNamespace

    return SimpleNamespace(content=content, usage={}, tool_calls_made=["generate_file"], rounds=3,
                           stop_reason="completed", control={}, messages=[])


@pytest.mark.asyncio
async def test_staged_skill_runs_under_its_stage_contract_and_reports_the_continuation(monkeypatch):
    seen = {}

    async def execute(**kwargs):
        seen.update(output_schema=kwargs["output_schema"], system_prompt=kwargs["system_prompt"])
        return _loop_result(staged("canvas_ready"))

    result = await _staged_service(monkeypatch, execute).invoke_skill(object(), "skill-1", "entity", "做视频")
    assert seen["output_schema"]["properties"]["stage"]["enum"] == [*STAGES, "delivered", "blocked"]
    assert "## Final reply contract" in seen["system_prompt"]
    assert result["continuation"] == {"stage": "canvas_ready"}


@pytest.mark.asyncio
async def test_terminal_stage_and_caller_contract_open_no_continuation(monkeypatch):
    async def delivered(**kwargs):
        return _loop_result(staged("delivered"))

    result = await _staged_service(monkeypatch, delivered).invoke_skill(object(), "skill-1", "entity", "只要画布")
    assert "continuation" not in result

    caller_schema = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
    seen = {}

    async def with_caller_schema(**kwargs):
        seen.update(output_schema=kwargs["output_schema"], system_prompt=kwargs["system_prompt"])
        return _loop_result(staged("canvas_ready"))

    result = await _staged_service(monkeypatch, with_caller_schema).invoke_skill(
        object(), "skill-1", "entity", "x", output_schema=caller_schema,
    )
    assert seen["output_schema"] is caller_schema
    assert "## Final reply contract" not in seen["system_prompt"]
    assert "continuation" not in result


@pytest.mark.asyncio
async def test_seeded_video_production_row_carries_its_resume_stages(db_session):
    # The loader copies an allowlist of config keys; a key missing from it is
    # dropped silently and the staged contract never reaches the runtime.
    from packages.core.models.base import generate_ulid
    from packages.core.services.builtin_skill_loader import seed_builtin_skills
    from packages.core.services.skill_service import get_skill_by_slug

    await seed_builtin_skills(db_session)
    await db_session.flush()
    row = await get_skill_by_slug(db_session, "video-production", generate_ulid())
    assert row.config["continuation_stages"] == list(STAGES)


def test_a_finished_video_request_is_the_render_approval_and_never_forged():
    # E2E 2026-10-04: video-edit stopped for "Approve and render" although the
    # user had asked to export the MP4, and the parent agent then sent that
    # approval itself. The user's request is the approval; a separate prompt is
    # only for a user who asked to review first, and it is never answered for them.
    from packages.core.ai.runtime.prompt_guidance import runtime_tool_usage_guidance

    skill = Path("packages/core/ai/skills/video-edit/SKILL.md").read_text()
    assert "Do not render in the same turn as the initial review" not in skill
    assert "A request for the\nfinished or exported video is that approval" in skill
    assert "Never write an approval on the user's behalf" in skill
    guidance = runtime_tool_usage_guidance(tool_names={"invoke_skill"}, has_tools=True)
    assert "`continuation` stage" in guidance
    assert "never write it on their behalf" in guidance


def test_every_remotion_skill_inspects_stills_of_its_final_source():
    # E2E 2026-10-04: remotion-create inspected one still, edited the scene, and
    # rendered the MP4 in the same command: all three selling lines shipped
    # stacked on top of each other while the reply said they appear in turn.
    skills = sorted(Path("packages/core/ai/skills").glob("remotion*/SKILL.md"))
    assert len(skills) >= 12
    for path in skills:
        text = path.read_text()
        assert "render stills of the final source" in text, path
        assert "Any source change after an\ninspection" in text or "Any source change after an inspection" in text, path
