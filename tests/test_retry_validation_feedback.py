"""Regression: dispatcher retries must carry the prior attempt's error.

Before this, a step that failed output validation was re-leased with identical
params — the model re-ran the exact prompt that just failed, so attempts 2 and
3 were indistinguishable from attempt 1 (see the X Growth publish tasks that
burned 3 attempts per plan on the same OutputSchemaError). The worker snapshot
now carries ``prior_error`` (step.error) and the llm/subagent executors prepend
a compact feedback note so the retry can actually correct the output.
"""

import pytest
from types import SimpleNamespace

from packages.core.workers.internal import _prior_attempt_feedback


def test_feedback_note_includes_validation_errors():
    note = _prior_attempt_feedback({
        "type": "OutputSchemaError",
        "message": "step publish_post: output validation failed (2 error(s))",
        "errors": [
            {"path": "$", "message": "'post_url' is a required property"},
            {"path": "$", "message": "'post_text' is a required property"},
        ],
    })

    assert note is not None
    assert "OutputSchemaError" in note
    assert "'post_url' is a required property" in note
    assert "'post_text' is a required property" in note
    assert "do not repeat the same mistake" in note


def test_feedback_note_handles_generic_error_and_truncates():
    note = _prior_attempt_feedback({"type": "RuntimeError", "message": "x" * 1000})

    assert note is not None
    assert "RuntimeError" in note
    # message truncated to 300 chars
    assert "x" * 301 not in note


def test_feedback_note_absent_without_prior_error():
    assert _prior_attempt_feedback(None) is None
    assert _prior_attempt_feedback({}) is None
    assert _prior_attempt_feedback("boom") is None


@pytest.mark.asyncio
async def test_exec_llm_prepends_feedback_on_retry(monkeypatch):
    import packages.core.workers.internal as internal

    captured: dict = {}

    async def fake_llm_step(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="ok", usage={})

    monkeypatch.setattr(
        internal, "runtime_execute_internal_worker_llm_step", fake_llm_step
    )

    await internal._exec_llm({
        "params": {"prompt": "Write the post."},
        "prior_error": {
            "type": "OutputSchemaError",
            "message": "output validation failed",
            "errors": [{"path": "$", "message": "'post_text' is a required property"}],
        },
    })

    assert captured["prompt"].startswith("[Retry context]")
    assert "'post_text' is a required property" in captured["prompt"]
    assert captured["prompt"].endswith("Write the post.")


@pytest.mark.asyncio
async def test_exec_llm_prompt_unchanged_on_first_attempt(monkeypatch):
    import packages.core.workers.internal as internal

    captured: dict = {}

    async def fake_llm_step(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="ok", usage={})

    monkeypatch.setattr(
        internal, "runtime_execute_internal_worker_llm_step", fake_llm_step
    )

    await internal._exec_llm({"params": {"prompt": "Write the post."}})

    assert captured["prompt"] == "Write the post."


def test_human_guidance_note_renders_answer_and_author():
    from packages.core.workers.internal import _human_guidance_note

    note = _human_guidance_note({
        "human_input_response": {
            "choice": "retry",
            "note": "use the landing page screenshot",
            "user": "Calvin",
        },
    })
    assert note is not None
    assert note.startswith("[Operator guidance]")
    assert "Calvin" in note
    assert "use the landing page screenshot" in note

    # Retry endpoints store the text under "response" instead of "note".
    legacy = _human_guidance_note({
        "human_input_response": {"response": "post to the company account"},
    })
    assert legacy is not None
    assert "post to the company account" in legacy


def test_human_guidance_note_absent_without_answer_text():
    from packages.core.workers.internal import _human_guidance_note

    assert _human_guidance_note(None) is None
    assert _human_guidance_note({}) is None
    assert _human_guidance_note({"human_input_response": None}) is None
    assert _human_guidance_note({"human_input_response": {"choice": "retry"}}) is None


@pytest.mark.asyncio
async def test_exec_llm_prepends_human_guidance_on_resumed_step(monkeypatch):
    """A guidance note typed at retry time must reach the retried model —
    previously human_input_response was copied into params but never
    rendered, so 'retry with guidance' re-ran the identical prompt."""
    import packages.core.workers.internal as internal

    captured: dict = {}

    async def fake_llm_step(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="ok", usage={})

    monkeypatch.setattr(
        internal, "runtime_execute_internal_worker_llm_step", fake_llm_step
    )

    await internal._exec_llm({
        "params": {
            "prompt": "Publish the post.",
            "human_input_response": {
                "choice": "retry",
                "note": "use the landing page screenshot",
                "user": "Calvin",
            },
        },
    })

    assert captured["prompt"].startswith("[Operator guidance]")
    assert "use the landing page screenshot" in captured["prompt"]
    assert captured["prompt"].endswith("Publish the post.")
