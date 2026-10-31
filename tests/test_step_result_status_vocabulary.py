"""One step-result status vocabulary, end to end.

Staging incident (task 01KYGH41T0D0ACSNN9XKVSPP1B): chat announced
"✅ Task complete — Completed 1/1 step" while the task sat in
``waiting_on_customer`` showing "Needs input" with an unresolvable prompt.

Two vocabularies caused it: the agent was offered ``done | partial | blocked``
while the envelope only accepted ``succeeded | partial | failed``. An agent
reporting ``done`` fell through the envelope's "unknown word" branch and was
inferred as ``partial``; ``blocked`` — an explicit "I could not proceed" — was
likewise recorded as partial success.
"""
from __future__ import annotations

import pytest

from packages.core.contracts.envelope import (
    StepResultStatus,
    build_step_result_envelope,
    envelope_indicates_failure,
    normalize_step_result_status,
)


# ── One vocabulary ────────────────────────────────────────────────────


def test_envelope_emitted_as_json_text_is_parsed_not_double_wrapped():
    """A model that writes its envelope as text instead of calling the tool
    used to get wrapped: the real status was buried in outputs.text while the
    OUTER status was inferred as partial. The UI rendered the inner one and
    the supervisor read the outer one."""
    import json

    inner = {
        "status": "succeeded",
        "summary": "picked a topic",
        "outputs": {"data": {"topic_slug": "10_minute_bad_workday_reset"}},
    }
    envelope = build_step_result_envelope(json.dumps(inner, ensure_ascii=False))

    assert envelope["status"] == StepResultStatus.SUCCEEDED.value
    assert envelope["outputs"]["data"]["topic_slug"] == "10_minute_bad_workday_reset"
    assert "text" not in envelope.get("outputs", {})


def test_non_envelope_text_is_still_wrapped_as_output():
    for raw in ("just some prose", '{"unrelated": true}', "[1, 2, 3]", "{not json"):
        envelope = build_step_result_envelope(raw)
        assert envelope["status"] in StepResultStatus.values()
        assert envelope["outputs"]


# ── The downgrade that caused the incident ────────────────────────────


@pytest.mark.parametrize(
    "declared,expected",
    [
        ("done", StepResultStatus.SUCCEEDED),
        ("succeeded", StepResultStatus.SUCCEEDED),
        ("completed", StepResultStatus.SUCCEEDED),
        ("blocked", StepResultStatus.FAILED),
        ("failed", StepResultStatus.FAILED),
        ("partial", StepResultStatus.PARTIAL),
        ("nonsense-word", None),
        ("", None),
        (None, None),
    ],
)
def test_status_normalization(declared, expected):
    assert normalize_step_result_status(declared) is expected


def test_agent_reporting_done_is_not_downgraded_to_partial():
    envelope = build_step_result_envelope(
        {"status": "done", "summary": "produced the video", "text": "final.mp4"}
    )
    assert envelope["status"] == StepResultStatus.SUCCEEDED.value
    assert not envelope_indicates_failure(envelope)


def test_agent_reporting_blocked_is_a_failure_not_partial_success():
    envelope = build_step_result_envelope(
        {"status": "blocked", "summary": "no channel credentials", "text": "x"}
    )
    assert envelope["status"] == StepResultStatus.FAILED.value
    assert envelope_indicates_failure(envelope)


def test_inference_still_never_invents_success():
    assert build_step_result_envelope("some prose")["status"] == "partial"
    assert build_step_result_envelope(None)["status"] == "failed"
    assert build_step_result_envelope({"error": "boom"})["status"] == "failed"
