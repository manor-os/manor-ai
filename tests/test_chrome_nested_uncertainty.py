"""Nested Chrome uncertainty keeps its evidence but never stops the parent.

A failed or uncertain child result is an observation for the invoking model:
the exact uncertainty control survives every Skill wrapper, while the parent
loop keeps its tools and decides its next call itself.
"""

from __future__ import annotations

import json


def _formatted_chrome_result(*, stop_reason: str, control: dict) -> str:
    from packages.core.ai.runtime.chrome_run import chrome_outcome_for_result
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    outcome = chrome_outcome_for_result(
        {},
        skill="chrome",
        goal="Upload the video",
        content="Chrome mutation outcome is unknown; observe and reconcile.",
        stop_reason=stop_reason,
        error="mutation_timeout_uncertain" if stop_reason == "uncertain_result" else "timeout",
        control=control,
    )
    return runtime_format_invoke_skill_result(
        "chrome",
        {
            "skill": "chrome",
            "content": "Chrome mutation outcome is unknown; observe and reconcile.",
            "stop_reason": stop_reason,
            "stop_parent": True,
            "control": control,
            "chrome_outcome": outcome.to_dict(),
        },
    )


def test_nested_confirmed_chrome_uncertainty_is_preserved_as_parent_observation() -> None:
    from packages.core.ai.agentic_loop import _detect_stop_parent_tool_result
    from packages.core.ai.runtime.control import runtime_terminal_uncertainty_control
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    control = {
        "stop_parent": True,
        "terminal_failure": True,
        "action_executed": "unknown",
        "retryable": False,
        "do_not_replay": True,
    }
    result = _formatted_chrome_result(stop_reason="uncertain_result", control=control)

    payload = json.loads(result)
    assert runtime_terminal_uncertainty_control(payload) is not None
    # A failure stop_parent is an observation for the parent model, never a
    # parent stop control.
    assert _detect_stop_parent_tool_result(
        [({"name": "invoke_skill"}, result)], "Upload the video"
    ) is None

    youtube_result = runtime_format_invoke_skill_result(
        "youtube_studio_publisher",
        {
            "skill": "youtube_studio_publisher",
            "content": payload["summary"],
            "stop_parent": True,
            "stop_reason": "uncertain_result",
            "control": {**control, "terminal": True},
        },
    )
    # The exact uncertainty evidence survives the outer Skill wrapper ...
    assert runtime_terminal_uncertainty_control(youtube_result) is not None
    # ... and still reaches the root model as an observation, not a stop.
    assert _detect_stop_parent_tool_result(
        [({"name": "invoke_skill"}, youtube_result)], "Upload the video"
    ) is None


def test_nested_chrome_timeout_does_not_become_terminal_uncertainty() -> None:
    from packages.core.ai.agentic_loop import _detect_stop_parent_tool_result
    from packages.core.ai.runtime.control import runtime_terminal_uncertainty_control

    result = _formatted_chrome_result(
        stop_reason="timeout",
        control={"stop_parent": True, "retryable": True},
    )

    assert runtime_terminal_uncertainty_control(result) is None
    assert _detect_stop_parent_tool_result(
        [({"name": "invoke_skill"}, result)], "Upload the video"
    ) is None
