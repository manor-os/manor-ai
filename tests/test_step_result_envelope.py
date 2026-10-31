"""StepResult envelope: the default output contract for llm/subagent steps.

The envelope's guarantee is that ``build_step_result_envelope`` never raises
and always yields a dict that validates against the envelope schema — which
makes dispatcher output validation a tautology for envelope-shaped steps.
"""
import pytest
from jsonschema import Draft202012Validator

from packages.core.contracts.envelope import (
    STEP_RESULT_ENVELOPE_ID,
    build_step_result_envelope,
    envelope_indicates_failure,
    is_step_result_envelope_schema,
    step_result_envelope_schema,
)
from packages.core.contracts.shapes import get_shape
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema


def _validate(envelope) -> None:
    Draft202012Validator(step_result_envelope_schema()).validate(envelope)


# ── constructor battery: any input → valid envelope, never raises ─────

_VALID_ENVELOPE = {
    "status": "succeeded",
    "summary": "wrote the report",
    "outputs": {"text": "full report text", "files": [{"name": "report.md", "path": "ws/report.md"}]},
}

_BATTERY = [
    _VALID_ENVELOPE,
    {"text": "partial dict with content but no status"},
    {"error": "upstream tool exploded", "attempts": 3},
    "a plain string result " * 30,
    "",
    None,
    {},
    [1, "two", {"three": 3}],
    42,
    {"files": [{"name": "kept.md", "path": "ws/kept.md"}, {"path": "no-name.md"}, "not-a-dict"]},
    {
        "status": "weird",
        "summary": 123,
        "outputs": {"text": {"nested": ["junk"]}, "files": "nope"},
        "progress": "half",
        "failure": {
            "reason": 0,
            "blockers": "b",
            "retryable": "yes",
            "requires_human": "yes",
        },
        "next_steps": [1, "do the next thing"],
        "misc": {"deep": [{"weird": None}]},
    },
]


@pytest.mark.parametrize("raw", _BATTERY, ids=[f"case_{i}" for i in range(len(_BATTERY))])
def test_build_always_returns_valid_envelope(raw):
    envelope = build_step_result_envelope(raw)
    _validate(envelope)


def test_build_passes_valid_envelope_through_unchanged():
    assert build_step_result_envelope(_VALID_ENVELOPE) == _VALID_ENVELOPE


@pytest.mark.parametrize("raw", _BATTERY, ids=[f"case_{i}" for i in range(len(_BATTERY))])
def test_build_never_invents_succeeded(raw):
    envelope = build_step_result_envelope(raw)
    if envelope["status"] == "succeeded":
        assert isinstance(raw, dict) and raw.get("status") == "succeeded"


def test_build_from_string_moves_full_text_to_outputs():
    raw = "x" * 500
    envelope = build_step_result_envelope(raw)
    assert envelope["status"] == "partial"
    assert envelope["outputs"]["text"] == raw
    assert len(envelope["summary"]) <= 200


def test_build_from_empty_inputs_is_failed():
    for raw in (None, "", {}):
        envelope = build_step_result_envelope(raw)
        assert envelope["status"] == "failed"
        assert envelope["failure"]["reason"] == "empty output"


def test_build_dict_with_error_key_is_failed():
    assert build_step_result_envelope({"error": "boom"})["status"] == "failed"


def test_build_drops_nameless_file_entries():
    envelope = build_step_result_envelope(
        {"files": [{"name": "kept.md", "path": "ws/kept.md"}, {"path": "dropped.md"}]}
    )
    assert envelope["outputs"]["files"] == [{"name": "kept.md", "path": "ws/kept.md"}]


def test_build_preserves_unrecognized_keys():
    envelope = build_step_result_envelope({"text": "content", "custom_key": {"a": 1}})
    assert envelope["custom_key"] == {"a": 1}
    assert envelope["outputs"]["text"] == "content"


def test_build_from_empty_text_dict_is_failed():
    """The exact staging payload: an llm step that returned "" is NOT a success."""
    envelope = build_step_result_envelope({"text": ""})
    assert envelope["status"] == "failed"
    assert envelope["summary"] == "no structured summary provided"


# ── status is a control signal, not decoration ─────────────────────────

def test_envelope_indicates_failure_only_for_failed_status():
    assert envelope_indicates_failure({"status": "failed", "summary": "nothing"}) is True
    assert envelope_indicates_failure({"status": "partial", "summary": "some"}) is False
    assert envelope_indicates_failure({"status": "succeeded", "summary": "all"}) is False


def test_envelope_indicates_failure_ignores_non_envelope_values():
    # No status key, unknown status, or a non-dict result: never a failure
    # signal — those are ordinary payloads, not envelopes.
    assert envelope_indicates_failure({"summary": "no status here"}) is False
    assert envelope_indicates_failure({"status": "weird"}) is False
    assert envelope_indicates_failure("failed") is False
    assert envelope_indicates_failure(None) is False


def test_is_step_result_envelope_schema():
    assert is_step_result_envelope_schema(step_result_envelope_schema())
    assert not is_step_result_envelope_schema({"type": "object"})
    assert not is_step_result_envelope_schema(None)
    assert not is_step_result_envelope_schema("manor:step-result-envelope/v1")


# ── shape registry ─────────────────────────────────────────────────────

def test_step_result_shape_is_registered():
    shape = get_shape("StepResult")
    assert shape.json_schema()["$id"] == STEP_RESULT_ENVELOPE_ID
    raw = {"text": "hello"}
    assert shape.normalize(raw) == build_step_result_envelope(raw)


# ── dispatcher coercion: envelope steps can never fail validation ──────

@pytest.mark.parametrize("garbage", _BATTERY, ids=[f"case_{i}" for i in range(len(_BATTERY))])
def test_coerce_step_output_for_envelope_schema_always_validates(garbage):
    coerced = coerce_step_output_for_schema(step_result_envelope_schema(), garbage)
    _validate(coerced)


def test_coerce_step_output_non_envelope_schema_unchanged():
    schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    assert coerce_step_output_for_schema(schema, None) is None