"""A pending video job must auto-force wait_media_jobs in the agentic loop,
so the turn reports the real outcome instead of ending on a 'started' placeholder.
"""

from __future__ import annotations

from packages.core.ai.agentic_loop import (
    _auto_tool_calls_from_result,
    _detect_forced_media_generation_result,
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
