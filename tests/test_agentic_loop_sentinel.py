"""Regression: the final-response marker must never leak into task/plan output."""

from packages.core.ai.agentic_loop import (
    FINAL_RESPONSE_SENTINEL,
    _strip_final_response_sentinel,
)


def test_strip_marker_at_start_keeps_answer():
    text = f"{FINAL_RESPONSE_SENTINEL}\nI'm unable to publish to LinkedIn."
    assert _strip_final_response_sentinel(text) == "I'm unable to publish to LinkedIn."


def test_strip_drops_progress_before_marker():
    text = f"Working on it...{FINAL_RESPONSE_SENTINEL}Here is the final answer."
    assert _strip_final_response_sentinel(text) == "Here is the final answer."


def test_no_marker_is_unchanged_apart_from_trim():
    assert _strip_final_response_sentinel("  just a normal answer  ") == "just a normal answer"


def test_closing_variant_is_removed():
    assert _strip_final_response_sentinel("answer</manor-final-response>") == "answer"


def test_none_and_empty():
    assert _strip_final_response_sentinel(None) == ""
    assert _strip_final_response_sentinel("") == ""


def test_marker_only_yields_empty():
    assert _strip_final_response_sentinel(FINAL_RESPONSE_SENTINEL) == ""
