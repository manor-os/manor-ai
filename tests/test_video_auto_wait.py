"""A pending video job must auto-force wait_media_jobs in the agentic loop,
so the turn reports the real outcome instead of ending on a 'started' placeholder.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import (
    _auto_tool_calls_from_result,
    _detect_forced_media_generation_result,
    agentic_loop,
)


def test_pending_video_forces_wait_media_jobs():
    result = {"kind": "video", "status": "pending", "job_id": "JOB1", "name": "x.mp4"}
    calls = _auto_tool_calls_from_result(result, {"wait_media_jobs", "generate_file"})
    assert calls == [{"name": "wait_media_jobs", "arguments": {"job_ids": ["JOB1"]}}]


def test_pending_video_forces_wait_even_when_not_preloaded():
    # wait_media_jobs is a deferred tool: in free-form chat it is not in the
    # eager surface. Forced calls execute directly against the (always-registered)
    # tool pool, so the auto-wait must fire regardless of the visible surface,
    # otherwise the turn ends on a "started" placeholder.
    result = {"kind": "video", "status": "pending", "job_id": "JOB1"}
    assert _auto_tool_calls_from_result(result, set()) == [
        {"name": "wait_media_jobs", "arguments": {"job_ids": ["JOB1"]}}
    ]


def test_no_wait_for_completed_video():
    result = {"kind": "video", "status": "completed", "job_id": "JOB1"}
    assert _auto_tool_calls_from_result(result, {"wait_media_jobs"}) == []


def test_no_wait_without_job_id():
    result = {"kind": "video", "status": "pending"}
    assert _auto_tool_calls_from_result(result, {"wait_media_jobs"}) == []


@pytest.mark.asyncio
async def test_llm_driven_pending_video_forces_wait_before_final_response():
    """The ordinary LLM tool path must use the same auto-wait as forced calls.

    This catches a call-site regression where auto follow-ups were evaluated
    only for results that also advertised MCP ``recommended_next_calls``.
    Media jobs do not advertise those calls, so Chat used to finish while the
    video was still pending and surface a later failure only as a notification.
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


def _gen_file_result(payload):
    import json

    tool_call = {"name": "generate_file", "arguments": {"kind": "video"}}
    return [(tool_call, json.dumps(payload))]


def test_forced_pending_video_is_not_terminal():
    # A forced generate_file that returns a *pending* async job must NOT end the
    # turn: the loop needs to chain wait_media_jobs and report the real outcome.
    results = _gen_file_result({"kind": "video", "status": "pending", "job_id": "JOB1", "message": "Starting..."})
    assert _detect_forced_media_generation_result(results, "make a video") is None


def test_forced_completed_video_is_terminal():
    # A synchronous/completed media result (nothing to await) still stops the turn.
    results = _gen_file_result({"kind": "video", "status": "completed", "result_url": "/api/v1/fs/e/v.mp4"})
    control = _detect_forced_media_generation_result(results, "make a video")
    assert control is not None and control.get("terminal") is True


def test_forced_image_is_terminal():
    # Image generation is synchronous (no job to await) → terminal as before.
    results = _gen_file_result({"kind": "image", "result_url": "/api/v1/fs/e/i.png"})
    control = _detect_forced_media_generation_result(results, "make an image")
    assert control is not None and control.get("terminal") is True


def test_forced_media_error_is_not_terminal():
    results = _gen_file_result(
        {
            "kind": "image",
            "status": "failed",
            "error": "image provider unavailable",
        }
    )
    assert _detect_forced_media_generation_result(results, "make an image") is None
