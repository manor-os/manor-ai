"""Media jobs await their real outcome; generation alone never ends a task loop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import (
    _auto_tool_calls_from_result,
    agentic_loop,
)
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.final_response import FinalResponseAction, FinalResponseDecision


def test_pending_video_forces_wait_media_jobs():
    result = {"kind": "video", "status": "pending", "job_id": "JOB1", "name": "x.mp4"}
    calls = _auto_tool_calls_from_result(
        result, {"wait_media_jobs", "generate_file"}, source_tool_name="generate_file",
    )
    assert calls == [{"name": "wait_media_jobs", "arguments": {"job_ids": ["JOB1"]}}]


def test_pending_video_forces_wait_even_when_not_preloaded():
    # wait_media_jobs is a deferred tool: in free-form chat it is not in the
    # eager surface. Forced calls execute directly against the (always-registered)
    # tool pool, so the auto-wait must fire regardless of the visible surface,
    # otherwise the turn ends on a "started" placeholder.
    result = {"kind": "video", "status": "pending", "job_id": "JOB1"}
    assert _auto_tool_calls_from_result(result, set(), source_tool_name="generate_file") == [
        {"name": "wait_media_jobs", "arguments": {"job_ids": ["JOB1"]}}
    ]


def test_pending_audio_forces_wait_media_jobs():
    result = {"kind": "audio", "status": "pending", "job_id": "AUDIO1"}
    assert _auto_tool_calls_from_result(result, set(), source_tool_name="generate_file") == [
        {"name": "wait_media_jobs", "arguments": {"job_ids": ["AUDIO1"]}}
    ]


def test_no_wait_for_completed_video():
    result = {"kind": "video", "status": "completed", "job_id": "JOB1"}
    assert _auto_tool_calls_from_result(result, {"wait_media_jobs"}, source_tool_name="generate_file") == []


def test_no_wait_without_job_id():
    result = {"kind": "video", "status": "pending"}
    assert _auto_tool_calls_from_result(result, {"wait_media_jobs"}, source_tool_name="generate_file") == []


def test_other_tool_cannot_forge_media_wait_followup():
    result = {"kind": "video", "status": "pending", "job_id": "JOB1"}
    assert _auto_tool_calls_from_result(result, set(), source_tool_name="read_file") == []


@pytest.mark.asyncio
async def test_llm_driven_pending_video_forces_wait_before_final_response():
    """The ordinary LLM tool path must use the same auto-wait as forced calls.

    Media jobs do not advertise provider recommendations, and only the
    first-party generate_file tool may schedule this wait.
    """

    tool_schema = {
        "type": "function",
        "function": {
            "name": "generate_file",
            "description": "Generate a file.",
            "parameters": {
                "type": "object",
                "properties": {"kind": {"type": "string"}},
            },
        },
    }
    llm = AsyncMock(
        side_effect=[
            (
                "",
                [
                    {
                        "id": "call_generate_video",
                        "name": "generate_file",
                        "arguments": {"kind": "video"},
                    }
                ],
                {"total": 1, "finish_reason": "tool_calls"},
            ),
            (
                "Video generation failed: the selected model needs a source image.",
                None,
                {"total": 1, "finish_reason": "stop"},
            ),
        ]
    )

    async def execute(name: str, _args: dict) -> str:
        if name == "generate_file":
            return json.dumps(
                {
                    "kind": "video",
                    "status": "pending",
                    "job_id": "job_123",
                    "name": "cat.mp4",
                }
            )
        if name == "wait_media_jobs":
            return json.dumps(
                {
                    "kind": "media_jobs",
                    "status": "failed",
                    "jobs": [
                        {
                            "job_id": "job_123",
                            "kind": "video",
                            "status": "failed",
                            "error": "The selected model needs a source image.",
                        }
                    ],
                    "failed_job_ids": ["job_123"],
                }
            )
        raise AssertionError(f"Unexpected tool: {name}")

    executor = AsyncMock(side_effect=execute)
    with patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        llm,
    ):
        result = await agentic_loop(
            system_prompt="Use tools and report their real result.",
            user_message="Generate a cat video.",
            tools=[tool_schema],
            tool_executor=executor,
            max_rounds=4,
        )

    executed = [call.args[0] for call in executor.await_args_list]
    assert executed == ["generate_file", "wait_media_jobs"]
    assert result.tool_calls_made == ["generate_file", "wait_media_jobs"]
    assert "needs a source image" in result.content


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object"},
        },
    }


def _call(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


def _completed_result(name: str) -> str:
    return json.dumps({"kind": "audio" if name == "generate_file" else "video", "status": "completed", "path": f"{name}.mp4"})


@pytest.mark.asyncio
async def test_completed_audio_continues_to_compose_and_validated_final_response():
    audio = _call("call_audio", "generate_file", kind="audio")
    compose = _call("call_compose", "compose_video_timeline", audio_path="voice.mp3")
    llm = AsyncMock(side_effect=[
        ("", [audio], {"finish_reason": "tool_calls"}),
        ("", [compose], {"finish_reason": "tool_calls"}),
        ("The narrated video is ready.", None, {"finish_reason": "stop"}),
    ])
    executor = AsyncMock(side_effect=lambda name, _args: _completed_result(name))
    contract = SimpleNamespace(
        instructions="Only claim a finished artifact after reviewing tool results.",
        response_format=None,
        validate=AsyncMock(side_effect=lambda content: FinalResponseDecision(
            action=FinalResponseAction.ACCEPT, content=content,
        )),
    )
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", llm):
        result = await agentic_loop(
            system_prompt="Finish the video.",
            user_message="Create a narrated video, not just a voice track.",
            tools=[_schema("generate_file"), _schema("compose_video_timeline")],
            tool_executor=executor,
            final_response_contract=contract,
            max_rounds=5,
        )

    assert result.stop_reason == "completed"
    assert result.content == "The narrated video is ready."
    assert result.rounds == 3
    assert [call.args[0] for call in executor.await_args_list] == ["generate_file", "compose_video_timeline"]
    contract.validate.assert_awaited_once_with("The narrated video is ready.")
    assert "Intermediate assets are not final delivery" in llm.await_args_list[0].args[0][0]["content"]


@pytest.mark.asyncio
async def test_same_batch_completed_audio_does_not_discard_compose_sibling():
    audio = _call("call_audio", "generate_file", kind="audio")
    compose = _call("call_compose", "compose_video_timeline", audio_path="voice.mp3")
    llm = AsyncMock(side_effect=[
        ("", [audio, compose], {"finish_reason": "tool_calls"}),
        ("Video with narration is ready.", None, {"finish_reason": "stop"}),
    ])
    executor = AsyncMock(side_effect=lambda name, _args: _completed_result(name))
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", llm):
        result = await agentic_loop(
            system_prompt="Finish the video.",
            user_message="Generate voice and compose it into the video.",
            tools=[_schema("generate_file"), _schema("compose_video_timeline")],
            tool_executor=executor,
            max_rounds=4,
        )

    assert result.stop_reason == "completed"
    assert result.rounds == 2
    assert [call.args[0] for call in executor.await_args_list] == ["generate_file", "compose_video_timeline"]
    assert result.tool_calls_made == ["generate_file", "compose_video_timeline"]


@pytest.mark.asyncio
async def test_forced_pending_video_waits_for_completed_job_before_final():
    generate = _call("call_video", "generate_file", kind="video")
    llm = AsyncMock(return_value=("Rendered video is ready.", None, {"finish_reason": "stop"}))

    async def execute(name: str, _args: dict) -> str:
        if name == "generate_file":
            return json.dumps({"kind": "video", "status": "pending", "job_id": "job_123"})
        if name == "wait_media_jobs":
            return json.dumps({"kind": "media_jobs", "status": "completed", "jobs": [
                {"job_id": "job_123", "kind": "video", "status": "completed", "path": "rendered.mp4"},
            ]})
        raise AssertionError(f"Unexpected tool: {name}")

    executor = AsyncMock(side_effect=execute)
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", llm):
        result = await agentic_loop(
            system_prompt="Report the actual video job result.",
            user_message="Generate a video.",
            tools=[_schema("generate_file"), _schema("wait_media_jobs")],
            tool_executor=executor,
            forced_tool_calls=[generate],
            max_rounds=4,
        )

    assert result.stop_reason == "completed"
    assert [call.args[0] for call in executor.await_args_list] == ["generate_file", "wait_media_jobs"]
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_forced_audio_compose_batch_reaches_output_schema_repair():
    audio = _call("call_audio", "generate_file", kind="audio")
    compose = _call("call_compose", "compose_video_timeline", audio_path="voice.mp3")
    llm = AsyncMock(return_value=('{"delivered": false}', None, {"finish_reason": "stop"}))
    repair_llm = AsyncMock(return_value=('{"delivered": true}', {"finish_reason": "stop"}))
    executor = AsyncMock(side_effect=lambda name, _args: _completed_result(name))
    contract = SimpleNamespace(
        instructions="Validate final delivery.",
        response_format=None,
        validate=AsyncMock(side_effect=lambda content: FinalResponseDecision(
            action=FinalResponseAction.ACCEPT, content=content,
        )),
    )
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", llm), patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", repair_llm,
    ):
        result = await agentic_loop(
            system_prompt="Finish the video.",
            user_message="Generate voice and compose it into the video.",
            tools=[_schema("generate_file"), _schema("compose_video_timeline")],
            tool_executor=executor,
            forced_tool_calls=[audio, compose],
            final_response_contract=contract,
            output_schema={
                "type": "object",
                "properties": {"delivered": {"const": True}},
                "required": ["delivered"],
                "additionalProperties": False,
            },
            max_rounds=4,
        )

    assert result.stop_reason == "completed"
    assert json.loads(result.content) == {"delivered": True}
    assert [call.args[0] for call in executor.await_args_list] == ["generate_file", "compose_video_timeline"]
    llm.assert_awaited_once()
    repair_llm.assert_awaited_once()
    assert contract.validate.await_count == 2


@pytest.mark.asyncio
async def test_resumed_completed_audio_keeps_remaining_compose_call():
    audio = _call("call_audio", "generate_file", kind="audio")
    compose = _call("call_compose", "compose_video_timeline", audio_path="voice.mp3")
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "Finish the video."},
            {"role": "user", "content": "Create a narrated video."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"]),
                }} for call in (audio, compose)
            ]},
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call=audio,
        remaining_tool_calls=[compose],
        pending_tool_result={"id": audio["id"], "content": _completed_result("generate_file")},
    ).to_dict()
    llm = AsyncMock(return_value=("Narrated video is ready.", None, {"finish_reason": "stop"}))
    executor = AsyncMock(side_effect=lambda name, _args: _completed_result(name))
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", llm):
        result = await agentic_loop(
            system_prompt="Finish the video.",
            user_message="Create a narrated video.",
            tools=[_schema("generate_file"), _schema("compose_video_timeline")],
            tool_executor=executor,
            resume_checkpoint=checkpoint,
            max_rounds=4,
        )

    assert result.stop_reason == "completed"
    assert result.content == "Narrated video is ready."
    assert [call.args[0] for call in executor.await_args_list] == ["compose_video_timeline"]
    assert "Intermediate assets are not final delivery" in result.messages[0]["content"]
    assert result.messages[0]["content"].count("Intermediate assets are not final delivery") == 1


@pytest.mark.asyncio
async def test_explicit_media_terminal_policy_still_stops_the_loop():
    audio = _call("call_audio", "generate_file", kind="audio")
    llm = AsyncMock(return_value=("", [audio], {"finish_reason": "tool_calls"}))
    executor = AsyncMock(return_value=_completed_result("generate_file"))
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", llm):
        result = await agentic_loop(
            system_prompt="Generate a single audio artifact.",
            user_message="Make an audio sample.",
            tools=[_schema("generate_file")],
            tool_executor=executor,
            terminal_tool_result_policy={"terminal_tool_results": [{
                "tool_names": ["generate_file"],
                "statuses": ["completed"],
                "stop_reason": "explicit_audio_complete",
                "notice": "Audio sample completed by explicit policy.",
            }]},
            max_rounds=4,
        )

    assert result.stop_reason == "explicit_audio_complete"
    assert result.content == "Audio sample completed by explicit policy."
    llm.assert_awaited_once()
