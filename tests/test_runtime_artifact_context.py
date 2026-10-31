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


def test_task_creation_receipt_does_not_turn_task_title_into_artifact() -> None:
    refs = runtime_extract_artifact_urls_from_tool_result({
        "created": True,
        "task": {
            "id": "task-1",
            "title": "Create and register Knowledge document: V3 Smoke 交付.md",
            "description": "Write Workspaces/Demo/documents/V3 Smoke 交付.md",
        },
        "files": [{"fs_path": "Workspaces/Demo/documents/real.md"}],
    })

    assert refs == {"Workspaces/Demo/documents/real.md"}


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

    async def fake_runtime_invoke_skill(db, skill_id, entity_id, input_text, **kwargs):
        del db
        captured.update(
            {
                "skill": skill_id,
                "entity_id": entity_id,
                "input_text": input_text,
                "kwargs": kwargs,
            }
        )
        return {"content": "ok", "stop_reason": "completed"}

    from packages.core import database
    from packages.core.services import skill_service

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(
        skill_service,
        "get_skill",
        lambda _db, _skill_id: asyncio.sleep(0, result=SimpleNamespace(
            status="active", entity_id=None, slug="pptx", name="pptx",
        )),
    )
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)

    result = asyncio.run(
        runtime_skills.runtime_invoke_skill_action(
            entity_id="ent_1",
            skill_id="pptx",
            input_text="把这些图拼成 PPT",
            runtime_context=SimpleNamespace(
                runtime_artifact_urls=frozenset({"/api/v1/fs/ent_1/Workspaces/Story/images/page_01.png"}),
                dependency_artifact_urls=frozenset(),
                manual_skill_selected=False,
            ),
        )
    )

    assert json.loads(result)["content"] == "ok"
    assert captured["skill"] == "pptx"
    assert "## Runtime Artifacts Available For This Run" in captured["input_text"]
    assert "`/workspace/Workspaces/Story/images/page_01.png`" in captured["input_text"]
    assert captured["kwargs"]["context_artifact_urls"] == (
        "/api/v1/fs/ent_1/Workspaces/Story/images/page_01.png",
    )
    assert "reference_artifact_urls" not in captured["kwargs"]


def test_chrome_skill_receives_exact_parent_document_and_dependency_artifact(monkeypatch) -> None:
    captured: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_runtime_invoke_skill(_db, _skill_id, _entity_id, input_text, **_kwargs):
        captured["input_text"] = input_text
        return {"content": "ok", "stop_reason": "completed"}

    from packages.core import database
    from packages.core.services import skill_service

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(
        skill_service,
        "get_skill",
        lambda _db, _skill_id: asyncio.sleep(0, result=SimpleNamespace(
            status="active", entity_id=None, slug="chrome", name="Chrome",
        )),
    )
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)
    result = asyncio.run(runtime_skills.runtime_invoke_skill_action(
        entity_id="ent_1", skill_id="chrome",
        input_text="Upload the supplied clip.mp4 to my draft.",
        runtime_context=SimpleNamespace(
            runtime_artifact_urls=frozenset(),
            dependency_artifact_urls=frozenset({"/api/v1/fs/ent_1/Workspaces/Story/videos/clip.mp4"}),
            manual_skill_selected=False,
            workspace_id=None,
            runtime_envelope=SimpleNamespace(metadata={
                "runtime_attachment_context": {"refs": [{
                    "name": "clip.mp4", "document_id": "01EXACTDOCUMENTID123",
                    "path": "Workspaces/Story/videos/clip.mp4",
                }]},
                "runtime_history_attachment_refs": [{
                    "name": "previous.mp4", "document_id": "01PREVIOUSDOCUMENTID",
                    "path": "Workspaces/Story/videos/previous.mp4",
                }],
            }),
        ),
    ))
    assert json.loads(result)["content"] == "ok"
    assert '"document_id": "01EXACTDOCUMENTID123"' in captured["input_text"]
    assert '"document_id": "01PREVIOUSDOCUMENTID"' in captured["input_text"]
    assert "Workspaces/Story/videos/previous.mp4" in captured["input_text"]
    assert "/api/v1/fs/ent_1/Workspaces/Story/videos/clip.mp4" in captured["input_text"]


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


def test_runtime_skill_defaults_preserve_params_embedded_in_input_json() -> None:
    params = runtime_skills.runtime_skill_params_with_embedded_input(
        json.dumps(
            {
                "prompt": "Create a 20-second slideshow.",
                "params": {
                    "target_duration": "20s",
                    "render_mode": "slideshow",
                },
            }
        ),
        None,
    )
    params, error = runtime_skills.runtime_skill_params_with_schema_defaults(
        params,
        {
            "type": "object",
            "properties": {
                "target_duration": {"type": "string", "default": "120s"},
                "render_mode": {
                    "type": "string",
                    "enum": ["video", "slideshow"],
                    "default": "video",
                },
                "music_mode": {"type": "string", "default": "off"},
            },
        },
    )

    assert error is None
    assert params == {
        "target_duration": "20s",
        "render_mode": "slideshow",
        "music_mode": "off",
    }


def test_runtime_invoke_skill_action_applies_schema_defaults_before_execution(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_get_skill(db, skill_id):
        del db
        assert skill_id == "skill_stickman"
        return SimpleNamespace(
            id=skill_id,
            slug="stickman-video-creator",
            name="Stickman Video Creator",
            status="active",
            entity_id="ent_1",
            input_schema={
                "type": "object",
                "properties": {
                    "target_duration": {"type": "string", "default": "120s"},
                    "render_mode": {
                        "type": "string",
                        "enum": ["video", "slideshow"],
                        "default": "video",
                    },
                    "music_mode": {"type": "string", "default": "off"},
                },
            },
        )

    async def fake_runtime_invoke_skill(db, skill_id, entity_id, input_text, **kwargs):
        del db, kwargs
        captured.update(
            skill_id=skill_id,
            entity_id=entity_id,
            input_text=input_text,
        )
        return {"content": "ok", "stop_reason": "completed"}

    from packages.core import database
    from packages.core.services import skill_service

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)

    result = asyncio.run(
        runtime_skills.runtime_invoke_skill_action(
            entity_id="ent_1",
            skill_id="skill_stickman",
            input_text="生成一个火柴人视频",
            runtime_context=SimpleNamespace(
                active_user_message="生成一个火柴人视频",
                manual_skill_selected=False,
                workspace_id="ws_current",
            ),
        )
    )

    assert json.loads(result)["content"] == "ok"
    payload = json.loads(captured["input_text"])
    assert payload == {
        "prompt": "生成一个火柴人视频",
        "params": {
            "target_duration": "120s",
            "render_mode": "video",
            "music_mode": "off",
        },
    }

    result = asyncio.run(
        runtime_skills.runtime_invoke_skill_action(
            entity_id="ent_1",
            skill_id="skill_stickman",
            input_text=json.dumps(
                {
                    "prompt": "Create a 20-second slideshow.",
                    "params": {
                        "target_duration": "20s",
                        "render_mode": "slideshow",
                    },
                }
            ),
            runtime_context=SimpleNamespace(
                active_user_message="Create a 20-second slideshow.",
                manual_skill_selected=False,
                workspace_id="ws_current",
                task_id="task_current",
            ),
        )
    )

    assert json.loads(result)["content"] == "ok"
    payload = json.loads(captured["input_text"])
    assert payload["params"] == {
        "target_duration": "20s",
        "render_mode": "slideshow",
        "music_mode": "off",
    }


def test_stickman_schema_defaults_do_not_change_other_skill_invocation(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_get_skill(db, skill_id):
        del db
        return SimpleNamespace(
            id=skill_id,
            slug="youtube-studio-publisher",
            name="YouTube Studio Publisher",
            status="active",
            entity_id="ent_1",
            input_schema={
                "type": "object",
                "properties": {"video_file": {"type": "string"}},
                "required": ["video_file"],
            },
        )

    async def fake_runtime_invoke_skill(db, skill_id, entity_id, input_text, **kwargs):
        del db, skill_id, entity_id, kwargs
        captured["input_text"] = input_text
        return {"content": "ok", "stop_reason": "completed"}

    from packages.core import database
    from packages.core.services import skill_service

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)

    result = asyncio.run(
        runtime_skills.runtime_invoke_skill_action(
            entity_id="ent_1",
            skill_id="skill_youtube",
            input_text="Publish the completed video from this conversation.",
            runtime_context=SimpleNamespace(
                active_user_message="Publish the completed video from this conversation.",
                manual_skill_selected=False,
                workspace_id="ws_current",
            ),
        )
    )

    assert json.loads(result)["content"] == "ok"
    assert captured["input_text"] == (
        "Publish the completed video from this conversation."
    )


def test_runtime_invoke_skill_action_normalizes_stickman_duration_alias_before_defaults(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_get_skill(db, skill_id):
        del db
        return SimpleNamespace(
            id=skill_id,
            slug="stickman-video-creator",
            name="Stickman Video Creator",
            status="active",
            entity_id="ent_1",
            input_schema={
                "type": "object",
                "properties": {
                    "target_duration": {"type": "string", "default": "120s"},
                    "render_mode": {
                        "type": "string",
                        "enum": ["video", "slideshow"],
                        "default": "video",
                    },
                },
            },
        )

    async def fake_runtime_invoke_skill(db, skill_id, entity_id, input_text, **kwargs):
        del db, skill_id, entity_id, kwargs
        captured["input_text"] = input_text
        return {"content": "ok", "stop_reason": "completed"}

    from packages.core import database
    from packages.core.services import skill_service

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)

    result = asyncio.run(
        runtime_skills.runtime_invoke_skill_action(
            entity_id="ent_1",
            skill_id="skill_stickman",
            input_text="生成一个 20 秒的火柴人视频",
            skill_params={"duration_seconds": 20},
            runtime_context=SimpleNamespace(
                active_user_message="生成一个 20 秒的火柴人视频",
                manual_skill_selected=False,
            ),
        )
    )

    assert json.loads(result)["content"] == "ok"
    payload = json.loads(captured["input_text"])
    assert payload["params"] == {
        "target_duration": "20s",
        "render_mode": "video",
    }


def test_stickman_duration_alias_rejects_conflicting_canonical_value() -> None:
    params, error = runtime_skills.runtime_skill_params_with_aliases(
        "stickman-video-creator",
        {"duration_seconds": 20, "target_duration": "30s"},
    )

    assert params is None
    assert error == (
        "Invalid skill parameters: 'duration_seconds' conflicts with 'target_duration'"
    )


def test_stickman_narration_scope_distinguishes_direct_chat_from_workspace_task() -> None:
    calls: list[dict] = []

    async def fake_runtime_execute_tool(name, args, **kwargs):
        del kwargs
        calls.append({"name": name, "args": dict(args)})
        return json.dumps({"status": "completed"})

    def execute(*, workspace_id=None, task_id=None, conversation_id=None):
        executor = runtime_skills.runtime_prompt_skill_registered_tool_executor(
            entity_id="ent_1",
            user_id="user_1",
            workspace_id=workspace_id,
            task_id=task_id,
            conversation_id=conversation_id,
            skill_slug="stickman-video-creator",
        )
        with patch(
            "packages.core.ai.runtime.tool_registry.runtime_execute_tool",
            new=fake_runtime_execute_tool,
        ):
            return asyncio.run(
                executor(
                    "generate_file",
                    {
                        "kind": "audio",
                        "purpose": "narration",
                        "name": "runs/example/audio/segment-001.wav",
                        "narration_voice_mode": "random_per_task",
                    },
                )
            )

    execute(conversation_id="conversation_1")
    execute(
        workspace_id="workspace_1",
        task_id="task_1",
        conversation_id="conversation_2",
    )

    assert calls[0]["args"]["narration_voice_mode"] == "random_per_conversation"
    assert calls[0]["args"]["conversation_id"] == "conversation_1"
    assert calls[1]["args"]["narration_voice_mode"] == "random_per_task"
    assert calls[1]["args"]["workspace_id"] == "workspace_1"
    assert calls[1]["args"]["task_id"] == "task_1"


def test_runtime_invoke_skill_action_rejects_invalid_schema_param_before_execution(
    monkeypatch,
) -> None:
    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_get_skill(db, skill_id):
        del db
        return SimpleNamespace(
            id=skill_id,
            slug="stickman-video-creator",
            name="Stickman Video Creator",
            status="active",
            entity_id="ent_1",
            input_schema={
                "type": "object",
                "properties": {
                    "render_mode": {
                        "type": "string",
                        "enum": ["video", "slideshow"],
                        "default": "video",
                    },
                },
            },
        )

    async def unexpected_runtime_invoke_skill(*args, **kwargs):
        raise AssertionError("invalid params must fail before Skill execution")

    from packages.core import database
    from packages.core.services import skill_service

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(
        runtime_skills,
        "runtime_invoke_skill",
        unexpected_runtime_invoke_skill,
    )

    result = asyncio.run(
        runtime_skills.runtime_invoke_skill_action(
            entity_id="ent_1",
            skill_id="skill_stickman",
            input_text="生成一个火柴人视频",
            skill_params={"render_mode": "cinematic"},
            runtime_context=SimpleNamespace(
                active_user_message="生成一个火柴人视频",
                manual_skill_selected=False,
                workspace_id="ws_current",
            ),
        )
    )

    assert result == (
        "Error invoking skill 'skill_stickman': Invalid skill parameter "
        "'render_mode': expected one of 'video', 'slideshow'"
    )


def test_workspace_chat_skill_catalog_prefers_current_workspace_for_duplicate_slug(
    monkeypatch,
) -> None:
    def skill(skill_id: str, workspace_id: str | None):
        return SimpleNamespace(
            id=skill_id,
            entity_id="ent_1",
            workspace_id=workspace_id,
            status="active",
            slug="stickman-video-creator",
            name="Stickman Video Creator",
            display_name="Stickman Video Creator",
            description="Create a complete stickman video.",
            tools=[],
            category="media",
            output_format="file",
            config={},
            tags=["stickman", "video"],
        )

    async def fake_list_skills(db, entity_id):
        del db
        assert entity_id == "ent_1"
        return [
            skill("skill_other_workspace", "ws_other"),
            skill("skill_current_workspace", "ws_current"),
            skill("skill_entity_shared", None),
        ]

    from packages.core.services import skill_service

    monkeypatch.setattr(skill_service, "list_skills", fake_list_skills)

    descriptors = asyncio.run(
        runtime_skills.resolve_skill_descriptors(
            object(),
            entity_id="ent_1",
            agent_id=None,
            workspace_id="ws_current",
            surface=runtime_skills.ChatSurface.WORKSPACE_CHAT,
            invoke_skill_visible=True,
            active_user_message="Create a stickman video.",
            limit=8,
        )
    )

    assert [(item.id, item.slug) for item in descriptors] == [
        ("skill_current_workspace", "stickman-video-creator")
    ]


def test_runtime_agentic_loop_carries_runtime_artifacts_between_tool_calls() -> None:
    from packages.core.ai.agentic_loop import AgenticResult

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
        return AgenticResult(content="done", messages=[], usage={}, rounds=1)

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
            ).content
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


def test_text_references_end_at_cjk_punctuation_and_unbalanced_parentheses():
    # Incident (local E2E, 2026-10-04): a Chinese Skill reply linked its review
    # sheet as ``[Contact sheet](/api/v1/fs/E/…/contact-sheet.jpg)。请回复“Approve``.
    # The whole tail became one required reference for the next invoke_skill,
    # which then failed with reference_missing before doing any work.
    result = json.dumps({"stage": "blocked", "text": (
        "项目：Videos/Lumen-Tea-6s；规格：1920×1080、30fps；审查证据："
        "[Contact sheet](/api/v1/fs/E/Videos/Lumen-Tea-6s/snapshots/contact-sheet.jpg)。"
        "请回复“Approve and render”。另见 /api/v1/fs/E/documents/报告（终版）.docx 与 videos/《品牌》片头.mp4。"
    )}, ensure_ascii=False)
    refs = runtime_extract_artifact_urls_from_tool_result(result)
    assert "/api/v1/fs/E/Videos/Lumen-Tea-6s/snapshots/contact-sheet.jpg" in refs
    assert "/api/v1/fs/E/documents/报告（终版）.docx" in refs
    assert len(refs) == 2
