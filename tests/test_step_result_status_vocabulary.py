"""One status vocabulary, end to end.

Staging incident (task 01KYGH41T0D0ACSNN9XKVSPP1B): chat announced
"✅ Task complete — Completed 1/1 step" while the task sat in
``waiting_on_customer`` showing "Needs input" with an unresolvable prompt.

Two vocabularies caused it. ``submit_result`` advertised
``done | partial | blocked`` to the agent; the envelope only accepted
``succeeded | partial | failed``. An agent reporting ``done`` fell through
the envelope's "unknown word" branch and was inferred as ``partial`` — which
the dispatcher counts as success (step done → plan completed → announcement)
and the executor's supervisor counts as a blocker (task → waiting_on_customer).
``blocked`` — an explicit "I could not proceed" — was likewise recorded as
partial success.
"""
from __future__ import annotations

import pytest

from packages.core.contracts.envelope import (
    StepResultStatus,
    build_step_result_envelope,
    envelope_indicates_failure,
    normalize_step_result_status,
    step_result_envelope_schema,
)


# ── One vocabulary ────────────────────────────────────────────────────


def test_submit_result_tool_advertises_the_canonical_enum():
    """The words offered to the agent must be the words the system accepts."""
    from packages.core.workers.submit_result import build_submit_result_tool

    schema = build_submit_result_tool(None)
    advertised = schema["function"]["parameters"]["properties"]["status"]["enum"]

    assert advertised == list(StepResultStatus.values())
    assert (
        advertised
        == step_result_envelope_schema()["properties"]["status"]["enum"]
    ), "submit_result and the envelope schema must share one enum"


def test_canonical_statuses_are_judged_by_the_enum_not_the_keyword_set():
    """The keyword set exists only for custom (non-envelope) schemas.

    Listing canonical values there too would mean two judgements of one word —
    exactly the split that let a step be "done" to the agent and "incomplete"
    to the supervisor."""
    from packages.core.plans.executor import (
        _STRUCTURED_BLOCKER_STATUSES,
        _structured_result_blocker,
    )

    for member in StepResultStatus:
        assert member.value not in _STRUCTURED_BLOCKER_STATUSES
    # Words that normalize onto the enum are owned by the enum branch too.
    for alias in ("done", "blocked", "failure", "incomplete"):
        assert alias not in _STRUCTURED_BLOCKER_STATUSES

    def blocker(result):
        return _structured_result_blocker(result, artifact_required=False)

    assert blocker({"status": "succeeded", "summary": "s"}) is None
    assert blocker({"status": "done", "summary": "s"}) is None
    assert blocker({"status": "partial", "summary": "s"}) == "step reported status=partial"
    assert blocker({"status": "blocked", "summary": "s"}) == "step reported status=failed"
    # Custom-schema words still handled by the keyword set.
    assert blocker({"status": "needs_input"}) == "step reported status=needs_input"


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


def test_submit_without_explicit_status_counts_as_succeeded():
    """submit_result IS the deliberate hand-off — it registers a terminal
    SUCCESS stop — and `status` is optional in its schema. Inferring partial
    from an omitted optional field asked the operator to resolve steps that
    had finished."""
    from packages.core.workers.submit_result import step_result_from_submit

    captured = step_result_from_submit({"summary": "wrote the script"})
    assert captured["status"] == StepResultStatus.SUCCEEDED.value

    envelope = build_step_result_envelope(captured)
    assert envelope["status"] == StepResultStatus.SUCCEEDED.value

    # An explicit non-success answer is still honoured.
    blocked = step_result_from_submit({"summary": "no creds", "status": "blocked"})
    assert blocked["status"] == StepResultStatus.FAILED.value


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


def test_submit_result_payload_keeps_the_agents_declared_outcome():
    from packages.core.workers.submit_result import step_result_from_submit

    captured = step_result_from_submit(
        {"summary": "made the video", "status": "done", "result": {"path": "final.mp4"}}
    )
    assert captured["status"] == StepResultStatus.SUCCEEDED.value

    envelope = build_step_result_envelope(captured)
    assert envelope["status"] == StepResultStatus.SUCCEEDED.value


# ── The announcement must not contradict the supervisor ───────────────


@pytest.mark.asyncio
async def test_completion_is_not_announced_when_supervisor_held_the_task(monkeypatch):
    from packages.core.plans import executor as executor_module

    calls: list[str] = []

    async def fake_completed(**_kwargs):
        calls.append("completed")

    async def fake_started(**_kwargs):
        calls.append("started")

    monkeypatch.setattr(executor_module.chat_notify, "notify_plan_completed", fake_completed)
    monkeypatch.setattr(executor_module.chat_notify, "notify_plan_started", fake_started)

    await executor_module.PlanExecutor._announce(
        "ent_1", "ws_1", "plan_1",
        task_id="task_1",
        started=False,
        step_count=1,
        execution_mode="auto",
        chat_events=[],
        plan_done="completed",
        plan_started_at=None,
        plan_completed_at=None,
        plan_cost=None,
        plan_error=None,
        task_title="Clean retry: generate a 3-minute stickman video",
        step_snapshots=[],
        task_status="waiting_on_customer",
    )
    assert calls == [], "held task must not be announced as complete"


@pytest.mark.asyncio
async def test_completion_is_announced_when_the_task_actually_completed(monkeypatch):
    from packages.core.plans import executor as executor_module

    calls: list[str] = []

    async def fake_completed(**_kwargs):
        calls.append("completed")

    monkeypatch.setattr(executor_module.chat_notify, "notify_plan_completed", fake_completed)

    await executor_module.PlanExecutor._announce(
        "ent_1", "ws_1", "plan_1",
        task_id="task_1",
        started=False,
        step_count=1,
        execution_mode="auto",
        chat_events=[],
        plan_done="completed",
        plan_started_at=None,
        plan_completed_at=None,
        plan_cost=None,
        plan_error=None,
        task_title="ok",
        step_snapshots=[],
        task_status="completed",
    )
    assert calls == ["completed"]


def test_task_status_from_event_reads_the_committed_status():
    from packages.core.plans.executor import _task_status_from_event

    assert _task_status_from_event(
        {"payload": {"task_status": "waiting_on_customer"}}
    ) == "waiting_on_customer"
    assert _task_status_from_event(None) is None
    assert _task_status_from_event({"payload": {}}) is None
