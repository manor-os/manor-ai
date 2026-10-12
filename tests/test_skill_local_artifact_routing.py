"""Skill routing uses the current operation, not injected artifact metadata.

These are routing/forwarding tests, not claims that a renderer is available or
that an external action has permission to execute.
"""

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.artifacts import runtime_artifact_tracking_scope
from packages.core.ai.runtime.skill_routing import should_route_external_action_to_integration
from packages.core.ai.runtime.skills import (
    runtime_external_action_skill_skip_result,
    runtime_invoke_skill_action,
)


SKILL_ID = "01M36ABH8VXTSN9Q5CB6G59RHJ"
ARTIFACT = "Workspaces/IG Repurposing Pipeline/Instagram/p1-hook.mp4"


@pytest.mark.parametrize(
    "operation",
    [
        "Merge the existing Instagram Reel clips and save a local MP4. Do not publish or upload to Instagram.",
        "Read the existing Instagram clips, edit them into a video, and save the MP4 in Knowledge.",
        "Create a TikTok video from these clips and save it in this workspace. Never publish it.",
        "剪辑合成这三个抖音素材，保存为本地视频文件，不要发布或上传到抖音。",
        "将 Instagram 的三段素材合成为视频并保存到当前工作区，不要发布到 Instagram。",
    ],
)
@pytest.mark.parametrize("skill", ["video-edit", "video_edit"])
def test_local_video_production_does_not_require_platform_route(operation, skill):
    assert not should_route_external_action_to_integration(
        active_user_message=operation,
        operation_text=operation,
        skill=skill,
        manual_skill_selected=False,
    )


@pytest.mark.parametrize(
    "operation",
    [
        "Create a video and upload it to Instagram",
        "Merge these clips and publish the final video to TikTok",
        "Read the latest posts from Instagram",
        "Fetch recent posts from Instagram",
        "剪辑合成这个视频，然后发布到抖音",
        "读取抖音最新发布的视频",
    ],
)
def test_platform_actions_still_require_platform_route(operation):
    assert should_route_external_action_to_integration(
        active_user_message=operation,
        operation_text=operation,
        skill="video-edit",
        manual_skill_selected=False,
    )


def test_request_wording_does_not_preempt_selected_skill():
    result = runtime_external_action_skill_skip_result(
        active_user_message="Upload this video to Instagram",
        skill="video-edit",
        manual_skill_selected=False,
    )

    assert result is None


@pytest.fixture
def skill_service_boundary(monkeypatch):
    """Keep runtime_invoke_skill real; isolate only DB and final service I/O."""
    from packages.core import database
    from packages.core.services import skill_service

    calls = []
    resolutions = []
    state = {"result": {
        "content": "Video instructions loaded",
        "stop_reason": "instructions_loaded",
        "instructions_only": True,
    }}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def get_skill(_db, requested):
        resolutions.append(requested)
        return SimpleNamespace(
            id=SKILL_ID,
            slug="video-edit",
            name="video-edit",
            status="active",
            entity_id="entity",
            tools=["video_edit", "read_file", "generate_file"],
        )

    async def invoke_skill(db, skill_id, entity_id, input_text, **kwargs):
        calls.append(
            {
                "db": db,
                "skill_id": skill_id,
                "entity_id": entity_id,
                "input_text": input_text,
                **kwargs,
            }
        )
        return state["result"]

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(skill_service, "get_skill", get_skill)
    monkeypatch.setattr(skill_service, "invoke_skill", invoke_skill)
    return calls, resolutions, state


def runtime_context(active_message):
    return SimpleNamespace(
        active_user_message=active_message,
        agent_id="agent",
        workspace_id="workspace",
        conversation_id="conversation",
        allowed_tool_names={"video_edit", "read_file"},
        tool_profile="workspace_operator",
        manual_skill_selected=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_ref", [SKILL_ID, "video-edit"])
@pytest.mark.parametrize("artifact_source", ["context", "tracking", "dependency"])
async def test_short_operation_survives_artifact_context(
    skill_service_boundary, skill_ref, artifact_source
):
    calls, resolutions, _state = skill_service_boundary
    context = runtime_context("合成")
    if artifact_source == "context":
        context.runtime_artifact_urls = [ARTIFACT]
    elif artifact_source == "dependency":
        context.dependency_artifact_urls = [ARTIFACT]
    with runtime_artifact_tracking_scope(
        runtime_artifact_urls=[ARTIFACT] if artifact_source == "tracking" else []
    ):
        result = await runtime_invoke_skill_action(
            entity_id="entity",
            skill_id=skill_ref,
            runtime_context=context,
            user_id="user",
        )

    assert result == "Video instructions loaded"
    assert resolutions == [skill_ref]
    assert len(calls) == 1
    assert calls[0]["input_text"].startswith("合成\n\n")
    assert ARTIFACT in calls[0]["input_text"]
    assert calls[0]["agent_id"] == "agent"
    assert calls[0]["enforce_agent_access"] is True
    assert calls[0]["allowed_tool_names"] == {"video_edit", "read_file"}
    assert calls[0]["workspace_id"] == "workspace"
    assert calls[0]["user_id"] == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_ref", [SKILL_ID, "video-edit"])
async def test_params_and_task_context_do_not_replace_current_operation(
    skill_service_boundary, skill_ref
):
    calls, _resolutions, _state = skill_service_boundary
    params = {"reference_description": "Instagram publish and upload reference"}
    result = await runtime_invoke_skill_action(
        entity_id="entity",
        skill_id=skill_ref,
        input_text="合成",
        skill_params=params,
        runtime_context=runtime_context("Prepare these clips, then publish to Instagram"),
    )

    assert result == "Video instructions loaded"
    assert len(calls) == 1
    assert json.loads(calls[0]["input_text"]) == {"prompt": "合成", "params": params}


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_ref", [SKILL_ID, "video-edit"])
async def test_actual_publish_operation_is_not_hidden_by_local_params(
    skill_service_boundary, skill_ref
):
    calls, resolutions, _state = skill_service_boundary
    result = await runtime_invoke_skill_action(
        entity_id="entity",
        skill_id=skill_ref,
        input_text="Create a video and publish it to Instagram",
        skill_params={"reference_description": "local file only"},
        runtime_context=runtime_context("合成"),
    )

    assert result == "Video instructions loaded"
    assert len(calls) == 1
    assert resolutions == [skill_ref]


@pytest.mark.asyncio
async def test_local_route_cannot_override_service_authorization(skill_service_boundary):
    calls, _resolutions, state = skill_service_boundary
    state["result"] = {"error": "Skill not allowed", "code": "skill_not_allowed"}
    context = runtime_context("合成")
    context.runtime_artifact_urls = [ARTIFACT]
    result = await runtime_invoke_skill_action(
        entity_id="entity",
        skill_id=SKILL_ID,
        runtime_context=context,
    )

    assert "Skill not allowed" in result
    assert len(calls) == 1
    assert calls[0]["enforce_agent_access"] is True
    assert calls[0]["manual_skill_selected"] is False
