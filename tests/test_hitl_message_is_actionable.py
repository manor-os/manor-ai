"""The hold message must tell an operator what happened and what to do.

Staging showed this, verbatim, and nothing else:

    The plan ran into issues and needs your input:
    recover or render finished video: step reported status=partial
    Please add a comment with guidance, or change the task status.

``status=partial`` is an internal enum value. The message named no
deliverable, listed nothing the run had produced, and offered no concrete
option — while the same run had already stored an agent summary saying, in
plain words, that it had generated six scene clips but no saveable MP4.
Every fact needed was on hand; none of it was shown.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.plans.executor import (
    _NON_INFORMATIVE_SUMMARIES,
    _agent_summaries,
    _hitl_request_message,
    _produced_artifact_labels,
)


def _step(key: str, *, summary: str = "", files: list | None = None, status: str = "partial"):
    result: dict = {"status": status}
    if summary:
        result["summary"] = summary
    if files:
        result["files"] = files
    return SimpleNamespace(step_key=key, step_status="done", result=result)


def _task():
    return SimpleNamespace(
        title="Produce today's finished stickman video",
        description="produce the final MP4",
        workspace_id="ws_1",
        details={},
        actual_output=None,
    )


REAL_STEPS = [
    _step("discover_style_and_topic", summary="Result submitted."),
    _step(
        "materialize_finished_video_artifact",
        summary="未完成最终 MP4 交付；已启动 6 个场景片段生成，但尚未取得可保存的成片 artifact。",
        files=[
            {"type": "image", "source": "fs_path", "fs_path": "daily_stickman/images/scene-01-hook.png"},
            {"type": "image", "source": "fs_path", "fs_path": "daily_stickman/images/scene-02-monster.png"},
        ],
    ),
    _step("recover_or_render_finished_video", summary="Result submitted."),
]


def test_message_never_shows_a_raw_internal_status():
    message = _hitl_request_message(
        _task(), REAL_STEPS,
        structured_issue="recover or render finished video: step reported status=partial",
        artifact_issue=None,
        failed_steps=[],
    )
    assert "status=partial" not in message, (
        "an enum value is not an explanation an operator can act on"
    )


def test_message_states_what_exists_what_is_missing_and_what_to_do():
    message = _hitl_request_message(
        _task(), REAL_STEPS,
        structured_issue=None,
        artifact_issue="This workspace task needs a saved file/media/document deliverable...",
        failed_steps=[],
    )

    assert "scene-01-hook.png" in message          # what exists
    assert "Missing:" in message                    # what is absent
    assert "Retry the task" in message              # what to do
    assert "Mark the task complete" in message


def test_the_agents_own_account_is_surfaced():
    """The one genuinely informative sentence in the whole run."""
    message = _hitl_request_message(
        _task(), REAL_STEPS,
        structured_issue=None,
        artifact_issue="missing deliverable",
        failed_steps=[],
    )
    assert "未完成最终 MP4 交付" in message


def test_bookkeeping_summaries_are_not_mistaken_for_an_account():
    summaries = _agent_summaries(REAL_STEPS)
    assert summaries, "the informative summary must survive filtering"
    assert all("Result submitted" not in item for item in summaries)
    assert "Result submitted." in _NON_INFORMATIVE_SUMMARIES


def test_produced_artifacts_are_listed_by_filename():
    labels = _produced_artifact_labels(REAL_STEPS)
    assert labels == ["scene-01-hook.png", "scene-02-monster.png"]


def test_a_run_with_no_artifacts_still_produces_a_usable_message():
    message = _hitl_request_message(
        _task(),
        [_step("render", summary="Result submitted.")],
        structured_issue=None,
        artifact_issue="missing deliverable",
        failed_steps=[],
    )
    assert "Produced so far" not in message
    assert "Missing:" in message
    assert "You can:" in message


def test_failed_steps_are_reported_with_their_error():
    failed = SimpleNamespace(
        step_key="render_finished_video", step_status="failed",
        result=None, error={"message": "ffmpeg exited 1"},
    )
    message = _hitl_request_message(
        _task(), [failed],
        structured_issue=None, artifact_issue=None, failed_steps=[failed],
    )
    assert "ffmpeg exited 1" in message
    assert "render finished video" in message


def test_unverified_completion_says_so_plainly():
    message = _hitl_request_message(
        _task(), [_step("do_it", summary="did the thing", status="succeeded")],
        structured_issue=None, artifact_issue=None, failed_steps=[],
    )
    assert "could not confirm" in message
    assert "You can:" in message


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_missing_step_results_do_not_break_the_message(bad):
    step = SimpleNamespace(step_key="s", step_status="done", result=bad)
    message = _hitl_request_message(
        _task(), [step], structured_issue=None, artifact_issue=None, failed_steps=[],
    )
    assert "You can:" in message
