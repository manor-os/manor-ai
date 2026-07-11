"""Regression: an empty supervisor response must not block a completed task.

When the supervisor LLM call returns nothing (e.g. a BYOK gateway returning an
empty body), runtime_parse_task_supervisor_json used to fall through to
needs_replan — which the task runner maps to a "blocked" task, marking
otherwise-completed agent work (Daily Content Brief, Weekly Data Report, …) as
failed. An empty response now means "review unavailable → accept the agent
result" (verdict=done). A non-empty-but-unparseable response stays conservative.
"""
from packages.core.ai.runtime.task_agent import (
    RUNTIME_TASK_VERDICT_DONE,
    RUNTIME_TASK_VERDICT_NEEDS_REPLAN,
    runtime_parse_task_supervisor_json,
)


def test_empty_supervisor_response_accepts_agent_result():
    for raw in ["", "   ", "\n\n", None]:
        verdict = runtime_parse_task_supervisor_json(raw)
        assert verdict["verdict"] == RUNTIME_TASK_VERDICT_DONE, repr(raw)


def test_nonempty_unparseable_still_needs_replan():
    # A non-empty but garbage response means the supervisor *said* something we
    # can't trust — keep the conservative needs_replan (not a silent accept).
    verdict = runtime_parse_task_supervisor_json("not json at all {oops")
    assert verdict["verdict"] == RUNTIME_TASK_VERDICT_NEEDS_REPLAN


def test_valid_verdicts_preserved():
    assert runtime_parse_task_supervisor_json(
        '{"verdict": "done", "reason": "ok"}'
    )["verdict"] == RUNTIME_TASK_VERDICT_DONE
    assert runtime_parse_task_supervisor_json(
        '{"verdict": "needs_replan", "reason": "broken"}'
    )["verdict"] == RUNTIME_TASK_VERDICT_NEEDS_REPLAN
