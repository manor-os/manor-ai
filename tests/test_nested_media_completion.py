"""Mock providers, real parent/child loops and their model-facing handoff.

These tests prove continuation and receipt visibility, not real media quality.
"""
from __future__ import annotations

import json

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime import agentic_llm
from packages.core.ai.runtime.skills import (
    RuntimeSkillToolObservations,
    runtime_format_invoke_skill_result,
)
from packages.core.ai.runtime.workspace_response import WorkspaceChatResponseContract
from packages.core.services.workspace_execution_evidence import ExecutionEvidenceScope


pytestmark = pytest.mark.unit


def tool(name):
    return {"type": "function", "function": {
        "name": name, "parameters": {"type": "object"},
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("child_finishes_edit", [True, False])
async def test_parent_and_child_finish_edit_after_audio_without_regeneration(
    monkeypatch, child_finishes_edit,
):
    original_request = "Add narration and captions to existing.mp4; preserve its footage."
    executed = []
    child_rounds = 0
    parent_rounds = 0
    validations = []
    narration = {
        "kind": "audio", "status": "completed", "document_id": "narration-doc",
        "fs_path": "narration.mp3", "warnings": ["Audio only; not yet in the video."],
    }
    composed = {
        "status": "completed", "document_id": "edited-doc", "fs_path": "edited.mp4",
    }
    inspection = {
        "status": "completed", "document_id": "edited-doc",
        "narration_verified": True, "captions_verified": True,
    }

    def call(name, arguments, prefix):
        return {"id": f"{prefix}-{name}", "name": name, "arguments": arguments}

    def compose(prefix):
        return call("compose_video_timeline", {
            "source": "existing.mp4", "narration": "narration.mp3",
            "captions": [{"text": "Test caption", "start": 0, "end": 2}],
            "output": "edited.mp4",
        }, prefix)

    async def model(messages, tools, **kwargs):
        nonlocal child_rounds, parent_rounds
        if messages[0]["content"].startswith("child"):
            child_rounds += 1
            assert any(m.get("content") == original_request for m in messages)
            if child_rounds == 1:
                return "", [call("generate_file", {"kind": "audio"}, "child")], {}
            assert any("narration.mp3" in str(m.get("content")) for m in messages)
            if not child_finishes_edit:
                return "Only narration was generated; assembly and inspection remain.", [], {}
            if child_rounds == 2:
                return "", [compose("child")], {}
            if child_rounds == 3:
                return "", [call("read_file", {"path": "edited.mp4"}, "child")], {}
            return "Edited existing footage and inspected the result.", [], {}

        parent_rounds += 1
        if parent_rounds == 1:
            return "", [call("invoke_skill", {"input": original_request}, "parent")], {}
        handoff = json.loads(next(
            m["content"] for m in messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "parent-invoke_skill"
        ))
        assert handoff["stop_reason"] == "completed"
        assert handoff["execution"]["scope"] == "skill_invocation"
        assert handoff["execution"]["delivery_assessment"] == "not_assessed"
        observed = json.dumps(handoff["execution"]["tool_observations"])
        assert "narration-doc" in observed
        assert "Audio only; not yet in the video." in observed
        if child_finishes_edit:
            assert "edited-doc" in observed and "captions_verified" in observed
        else:
            assert "edited-doc" not in observed
            if parent_rounds == 2:
                return "", [compose("parent")], {}
            if parent_rounds == 3:
                return "", [call("read_file", {"path": "edited.mp4"}, "parent")], {}
        return json.dumps({
            "kind": "answer", "text": "Edited the existing footage and inspected edited.mp4.",
        }), [], {}

    async def execute(name, arguments):
        executed.append(name)
        if name == "invoke_skill":
            assert arguments["input"] == original_request
            observations = RuntimeSkillToolObservations()

            def observe(name, result, _duration, _arguments=None):
                observations.record(name, result)

            child = await loop_module.agentic_loop(
                system_prompt="child: edit the original footage, then inspect the output",
                user_message=arguments["input"],
                tools=[tool("generate_file"), tool("compose_video_timeline"), tool("read_file")],
                tool_executor=execute, on_tool_end=observe, max_rounds=6,
            )
            return runtime_format_invoke_skill_result("editor", {
                "content": child.content, "stop_reason": child.stop_reason,
                "tools_used": child.tool_calls_made, "rounds": child.rounds,
                "tool_observations": observations.snapshot(child.messages),
            })
        if name == "generate_file":
            assert arguments["kind"] == "audio", "An edit must not regenerate the video"
            return json.dumps(narration)
        if name == "compose_video_timeline":
            assert arguments["source"] == "existing.mp4"
            assert arguments["narration"] == "narration.mp3"
            assert arguments["captions"]
            return json.dumps(composed)
        assert name == "read_file" and arguments["path"] == "edited.mp4"
        return json.dumps(inspection)

    class Contract(WorkspaceChatResponseContract):
        async def validate(self, content):
            validations.append(content)
            return await super().validate(content)

    async def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("The regression test must never call a real provider")

    monkeypatch.setattr(agentic_llm, "chat_completion", unexpected_provider)
    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", unexpected_provider)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    parent = await loop_module.agentic_loop(
        system_prompt="parent: preserve the user's edit request when delegating",
        user_message=original_request,
        tools=[tool("invoke_skill"), tool("compose_video_timeline"), tool("read_file")],
        tool_executor=execute, max_rounds=6,
        final_response_contract=Contract(ExecutionEvidenceScope("e", "w", "u", "c", "m")),
    )
    assert parent.stop_reason == "completed"
    assert parent.error is None
    assert validations and "edited.mp4" in parent.content
    assert executed == ["invoke_skill", "generate_file", "compose_video_timeline", "read_file"]
    assert child_rounds == (4 if child_finishes_edit else 2)
