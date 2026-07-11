from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from packages.core.ai.runtime.artifacts import (
    runtime_artifact_tracking_scope,
    runtime_current_artifact_urls,
    runtime_extract_artifact_urls_from_tool_result,
    runtime_input_with_artifact_context,
    runtime_reference_allowed_by_artifacts,
    runtime_record_tool_result_artifacts,
)
from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
from packages.core.ai.runtime import skills as runtime_skills
from packages.core.ai.runtime.task_agent import runtime_execute_task_agent_turn
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_injected_tool_context_args,
    runtime_tool_call_context_from_kwargs,
)


def test_runtime_tool_context_carries_runtime_artifact_urls() -> None:
    kwargs = runtime_injected_tool_context_args(
        runtime_artifact_urls=["/api/v1/fs/ent_1/generated/start.png"],
        dependency_artifact_urls=["/api/v1/fs/ent_1/dependencies/ref.mp4"],
    )

    assert "_runtime_artifact_urls_from_context" in RUNTIME_TOOL_CONTEXT_KEYS
    assert "_dependency_artifact_urls_from_context" in RUNTIME_TOOL_CONTEXT_KEYS

    context = runtime_tool_call_context_from_kwargs(kwargs)
    assert context.runtime_artifact_urls == frozenset({"/api/v1/fs/ent_1/generated/start.png"})
    assert context.dependency_artifact_urls == frozenset({"/api/v1/fs/ent_1/dependencies/ref.mp4"})


def test_runtime_tool_context_merges_current_artifact_scope() -> None:
    with runtime_artifact_tracking_scope():
        runtime_record_tool_result_artifacts({"result_url": "/api/v1/fs/ent_1/generated/start.png"})
        kwargs = runtime_injected_tool_context_args(dependency_artifact_urls=["/api/v1/fs/ent_1/dependencies/ref.mp4"])

    context = runtime_tool_call_context_from_kwargs(kwargs)
    assert context.runtime_artifact_urls == frozenset({"/api/v1/fs/ent_1/generated/start.png"})
    assert context.dependency_artifact_urls == frozenset({"/api/v1/fs/ent_1/dependencies/ref.mp4"})


def test_runtime_extracts_generic_tool_artifact_refs_without_external_urls() -> None:
    refs = runtime_extract_artifact_urls_from_tool_result(
        {
            "status": "completed",
            "result_url": "https://manor.test/api/v1/fs/ent_1/Workspaces/Story/images/hero.png",
            "entry_url": "/api/v1/fs/ent_1/Workspaces/Story/code/index.html",
            "url": "https://example.com/not-a-runtime-artifact.png",
            "files": [
                {"fs_path": "Workspaces/Story/images/logo.png"},
                {"path": "rendered/page_01.png", "generated": True},
                {"url": "https://example.com/reference.pdf"},
            ],
            "artifacts": [
                {"saved_to": "/workspace/Workspaces/Story/exports/deck.pptx"},
            ],
            "notes": "Saved Workspaces/Story/videos/scene_01.mp4 and https://example.com/file.pdf",
        }
    )

    assert refs == {
        "https://manor.test/api/v1/fs/ent_1/Workspaces/Story/images/hero.png",
        "/api/v1/fs/ent_1/Workspaces/Story/code/index.html",
        "Workspaces/Story/images/logo.png",
        "rendered/page_01.png",
        "Workspaces/Story/exports/deck.pptx",
        "Workspaces/Story/videos/scene_01.mp4",
    }


def test_runtime_reference_allowed_by_artifacts_matches_url_and_fs_path_variants() -> None:
    allowed = [
        "https://manor.test/api/v1/fs/ent_1/Workspaces/Story/images/start.png",
        "Workspaces/Story/audio/voice.wav",
    ]

    assert runtime_reference_allowed_by_artifacts(
        allowed,
        "/api/v1/fs/ent_1/Workspaces/Story/images/start.png",
    )
    assert runtime_reference_allowed_by_artifacts(
        allowed,
        "/api/v1/fs/ent_1/Workspaces/Story/audio/voice.wav",
    )
    assert not runtime_reference_allowed_by_artifacts(
        allowed,
        "/api/v1/fs/ent_1/Workspaces/Story/images/other.png",
    )


def test_runtime_input_with_artifact_context_exposes_workspace_paths_for_skills() -> None:
    text = runtime_input_with_artifact_context(
        "把这些图拼成 PPT",
        runtime_artifact_urls=["/api/v1/fs/ent_1/Workspaces/Story/images/page_01.png"],
        dependency_artifact_urls=["Workspaces/Story/images/logo.png"],
    )

    assert "## Runtime Artifacts Available For This Run" in text
    assert "do not regenerate substitutes" in text
    assert "/api/v1/fs/ent_1/Workspaces/Story/images/page_01.png" in text
    assert "`/workspace/Workspaces/Story/images/page_01.png`" in text
    assert "`/workspace/Workspaces/Story/images/logo.png`" in text


def test_runtime_invoke_skill_action_passes_runtime_artifacts_to_skill(monkeypatch) -> None:
    captured: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_runtime_invoke_skill(db, skill, entity_id, input_text, **kwargs):
        del db, kwargs
        captured.update(
            {
                "skill": skill,
                "entity_id": entity_id,
                "input_text": input_text,
            }
        )
        return {"content": "ok"}

    from packages.core import database

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)

    result = asyncio.run(
        runtime_skills.runtime_invoke_skill_action(
            entity_id="ent_1",
            skill="pptx",
            input_text="把这些图拼成 PPT",
            runtime_context=SimpleNamespace(
                runtime_artifact_urls=frozenset({"/api/v1/fs/ent_1/Workspaces/Story/images/page_01.png"}),
                dependency_artifact_urls=frozenset(),
                manual_skill_selected=False,
            ),
        )
    )

    assert result == "ok"
    assert captured["skill"] == "pptx"
    assert "## Runtime Artifacts Available For This Run" in captured["input_text"]
    assert "`/workspace/Workspaces/Story/images/page_01.png`" in captured["input_text"]


def test_runtime_skill_input_with_params_merges_structured_options() -> None:
    text = runtime_skills.runtime_skill_input_with_params(
        "Create a 5-page hotel industry growth deck.",
        {"render": "full_page_image"},
    )

    payload = json.loads(text)
    assert payload == {
        "prompt": "Create a 5-page hotel industry growth deck.",
        "params": {"render": "full_page_image"},
    }

    text = runtime_skills.runtime_skill_input_with_params(
        '{"prompt":"Create deck","params":{"theme":"luxury"}}',
        {"render": "full_page_image"},
    )
    payload = json.loads(text)
    assert payload["prompt"] == "Create deck"
    assert payload["params"] == {"theme": "luxury", "render": "full_page_image"}


def test_runtime_agentic_loop_carries_runtime_artifacts_between_tool_calls() -> None:
    captured_contexts: list[tuple[str, object]] = []
    observed_after_first: list[frozenset[str]] = []

    async def fake_agentic_loop(**kwargs):
        executor = kwargs["tool_executor"]
        await executor("generate_image", {"prompt": "start"})
        observed_after_first.append(runtime_current_artifact_urls())
        await executor(
            "generate_video",
            {"first_frame_url": "/api/v1/fs/ent_1/generated/start.png"},
        )
        return "done"

    async def fake_tool_executor(name: str, args: object) -> str:
        captured_contexts.append(
            (
                name,
                runtime_tool_call_context_from_kwargs(runtime_injected_tool_context_args()),
            )
        )
        if name == "generate_image":
            return json.dumps(
                {
                    "status": "completed",
                    "result_url": "/api/v1/fs/ent_1/generated/start.png",
                    "extra": {"style": "https://manor.test/api/v1/fs/ent_1/generated/style.png"},
                }
            )
        return json.dumps({"status": "pending", "job_id": "job_1"})

    with patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop):
        assert (
            asyncio.run(
                runtime_execute_agentic_loop(
                    runtime_envelope=None,
                    system_prompt="system",
                    user_message="生成一段视频",
                    tools=[],
                    entity_id="ent_1",
                    agent_id="agent_1",
                    tool_executor=fake_tool_executor,
                )
            )
            == "done"
        )

    assert captured_contexts[0][0] == "generate_image"
    assert captured_contexts[0][1].runtime_artifact_urls == frozenset()
    assert observed_after_first == [
        frozenset(
            {
                "/api/v1/fs/ent_1/generated/start.png",
                "https://manor.test/api/v1/fs/ent_1/generated/style.png",
            }
        )
    ]
    assert captured_contexts[1][0] == "generate_video"
    assert captured_contexts[1][1].runtime_artifact_urls == frozenset(
        {
            "/api/v1/fs/ent_1/generated/start.png",
            "https://manor.test/api/v1/fs/ent_1/generated/style.png",
        }
    )


def test_runtime_task_agent_turn_recovers_runtime_artifacts_from_prior_tool_messages() -> None:
    from packages.core.ai.engine import ChatMessage

    captured_contexts: list[object] = []

    class FakeEngine:
        async def chat(self, messages, **kwargs):
            del messages, kwargs
            return ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "tc_1",
                        "name": "generate_video",
                        "arguments": {"first_frame_url": "/api/v1/fs/ent_1/generated/start.png"},
                    }
                ],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )

    async def fake_execute_tool(name: str, args: dict, **kwargs):
        del name, args, kwargs
        captured_contexts.append(runtime_tool_call_context_from_kwargs(runtime_injected_tool_context_args()))
        return json.dumps({"status": "pending", "job_id": "job_1"})

    messages = [
        ChatMessage(role="user", content="继续做下一段"),
        ChatMessage(
            role="tool",
            content=json.dumps({"result_url": "/api/v1/fs/ent_1/generated/start.png"}),
            tool_call_id="tc_prev",
        ),
    ]
    schema = {"type": "function", "function": {"name": "generate_video"}}

    with patch("packages.core.ai.runtime.task_agent.runtime_execute_tool", new=fake_execute_tool):
        result = asyncio.run(
            runtime_execute_task_agent_turn(
                engine=FakeEngine(),
                messages=messages,
                tools=[schema],
                loaded_tool_names={"generate_video"},
                system_prompt="Task system",
                runtime_envelope=None,
                entity_id="ent_1",
                agent_id="agent_1",
                active_user_message="继续做下一段",
            )
        )

    assert result.had_tool_calls is True
    assert captured_contexts[0].runtime_artifact_urls == frozenset({"/api/v1/fs/ent_1/generated/start.png"})
