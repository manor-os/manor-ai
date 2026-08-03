"""A refused tool call is not a delivery.

Staging step ``produce_final_mp4`` (plan HA3CDY) ran for 30 minutes — 14
``generate_image``, 12 ``generate_video``, 8 ``generate_file``, a skill
invocation — then handed back exactly this:

    {"status": "partial", "outputs": {"text": "Result submitted."},
     "summary": "Result submitted."}

``Result submitted.`` is the loop's terminal NOTICE, bookkeeping the code
comments call "not user-facing content to preserve". It became the step's
whole result because:

1. the surface allowlist denied ``submit_result`` —
   ``tool_not_in_runtime_surface`` on ``scheduled_agent_run`` /
   ``background_worker`` — so the capture handler never ran and every path
   the agent produced was lost; and
2. the terminal rule matches on the tool NAME alone, so the refused call
   still ended the loop as a success and the notice was recorded as output.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.policies import (
    RuntimeInjectedTool,
    RuntimeToolPolicyCode,
    RuntimeToolPolicyDecision,
    check_runtime_tool_policy,
    is_runtime_policy_denial,
)


# ── Runtime-injected tools are not surface capabilities ───────────────


def _envelope(allowed: set[str]):
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    return SimpleNamespace(
        allowed_tool_names=allowed,
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        profile=RuntimeProfile.BACKGROUND_WORKER,
        metadata={},
    )


@pytest.mark.parametrize("tool", [member.value for member in RuntimeInjectedTool])
def test_injected_tools_pass_a_surface_allowlist_that_omits_them(tool):
    decision = check_runtime_tool_policy(
        envelope=_envelope({"generate_image", "generate_video"}),
        tool_name=tool,
    )
    assert decision.allowed, (
        f"{tool} is armed by the runtime, not discovered by the model — "
        "the surface allowlist must not gate it"
    )


def test_ordinary_tools_are_still_gated_by_the_allowlist():
    decision = check_runtime_tool_policy(
        envelope=_envelope({"generate_image"}),
        tool_name="bash",
    )
    assert not decision.allowed
    assert decision.code == RuntimeToolPolicyCode.TOOL_NOT_IN_RUNTIME_SURFACE.value


def test_submit_result_is_the_worker_tool_the_enum_names():
    from packages.core.workers.submit_result import SUBMIT_RESULT_TOOL_NAME

    assert SUBMIT_RESULT_TOOL_NAME in RuntimeInjectedTool.names()


def test_channel_attachment_is_the_tool_the_enum_names():
    from packages.core.ai.runtime.channel_tools import (
        RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME,
    )

    assert RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME in RuntimeInjectedTool.names()


# ── Denial detection is structural, by enum ───────────────────────────


def test_policy_denial_is_recognized_by_code_not_message_text():
    denial = RuntimeToolPolicyDecision(
        False,
        code=RuntimeToolPolicyCode.TOOL_NOT_IN_RUNTIME_SURFACE.value,
        reason="`submit_result` is not available in the ... surface",
        tool_name="submit_result",
    ).to_tool_result()

    assert is_runtime_policy_denial(denial)
    # A tool that ran and reported its own error is NOT a policy denial.
    assert not is_runtime_policy_denial('{"error": "ffmpeg exited 1"}')
    assert not is_runtime_policy_denial('{"ok": true}')
    assert not is_runtime_policy_denial("Result submitted.")
    assert not is_runtime_policy_denial(None)


# ── A refused call cannot end the loop as a success ───────────────────


def test_denied_terminal_tool_does_not_end_the_loop():
    from packages.core.ai.agentic_loop import _detect_terminal_tool_result
    from packages.core.workers.submit_result import (
        SUBMIT_RESULT_TERMINAL_POLICY,
        SUBMIT_RESULT_TOOL_NAME,
    )

    denial = RuntimeToolPolicyDecision(
        False,
        code=RuntimeToolPolicyCode.TOOL_NOT_IN_RUNTIME_SURFACE.value,
        reason="denied",
        tool_name=SUBMIT_RESULT_TOOL_NAME,
    ).to_tool_result()

    control = _detect_terminal_tool_result(
        [({"name": SUBMIT_RESULT_TOOL_NAME, "id": "c1"}, denial)],
        SUBMIT_RESULT_TERMINAL_POLICY,
        "make the video",
    )
    assert control is None, "a refused call must not be recorded as delivery"


def test_successful_terminal_tool_still_ends_the_loop():
    from packages.core.ai.agentic_loop import _detect_terminal_tool_result
    from packages.core.workers.submit_result import (
        SUBMIT_RESULT_TERMINAL_POLICY,
        SUBMIT_RESULT_TOOL_NAME,
    )

    control = _detect_terminal_tool_result(
        [({"name": SUBMIT_RESULT_TOOL_NAME, "id": "c1"}, '{"ok": true}')],
        SUBMIT_RESULT_TERMINAL_POLICY,
        "make the video",
    )
    assert control is not None
    assert control.get("terminal") is True


# ── Evidence channel: what the agent DID, not what it said ────────────


def test_completed_media_jobs_are_collected_as_artifacts():
    """The async media pipeline reports finished files inside ``jobs[]``.

    That container was not recognized, so a step that generated 12 videos
    recorded no artifact at all — nothing for the task output, nothing for
    the supervisor's delivery gate to check, and no record of where the
    files landed."""
    from packages.core.models.media_job import MediaJobStatus
    from packages.core.workers.internal import _artifact_refs_from_tool_payload

    payload = {
        "kind": "media_jobs",
        "status": MediaJobStatus.COMPLETED.value,
        "jobs": [
            {
                "job_id": "j1", "kind": "video",
                "status": MediaJobStatus.COMPLETED.value,
                "fs_path": "runs/plan_1/steps/produce/final.mp4",
                "document_id": "doc_1",
            },
            {"job_id": "j2", "kind": "video", "status": MediaJobStatus.PENDING.value},
            {
                "job_id": "j3", "kind": "image",
                "status": MediaJobStatus.FAILED.value,
                "fs_path": "runs/plan_1/steps/produce/never-made.png",
            },
        ],
    }

    refs = _artifact_refs_from_tool_payload(payload)
    paths = [ref.get("fs_path") for ref in refs]
    assert paths == ["runs/plan_1/steps/produce/final.mp4"], (
        "only completed jobs are deliverables — a pending job has no file "
        "yet and a failed one never will"
    )


def test_executor_collects_completed_media_jobs_too():
    from packages.core.models.media_job import MediaJobStatus
    from packages.core.plans.executor import _artifact_refs_from_result

    refs = _artifact_refs_from_result({
        "kind": "media_jobs",
        "jobs": [
            {"kind": "video", "status": MediaJobStatus.COMPLETED.value,
             "fs_path": "runs/p/final.mp4"},
            {"kind": "video", "status": MediaJobStatus.PROCESSING.value,
             "fs_path": "runs/p/wip.mp4"},
        ],
    })
    assert [ref.get("fs_path") for ref in refs] == ["runs/p/final.mp4"]


def test_media_job_terminal_statuses_have_one_definition():
    from packages.core.ai.tools.media_tools import TERMINAL_JOB_STATUSES
    from packages.core.models.media_job import MediaJobStatus

    assert TERMINAL_JOB_STATUSES == MediaJobStatus.terminal()
    assert MediaJobStatus.is_completed("completed")
    assert MediaJobStatus.is_completed(" COMPLETED ")
    assert not MediaJobStatus.is_completed("pending")
    assert not MediaJobStatus.is_completed(None)
