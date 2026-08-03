"""StepResult envelope: the default output contract for llm/subagent steps.

The envelope's guarantee is that ``build_step_result_envelope`` never raises
and always yields a dict that validates against the envelope schema — which
makes dispatcher output validation a tautology for envelope-shaped steps.
"""
import json

import pytest
from jsonschema import Draft202012Validator

from packages.core.contracts.envelope import (
    STEP_RESULT_ENVELOPE_ID,
    build_step_result_envelope,
    envelope_indicates_failure,
    is_step_result_envelope_schema,
    step_result_envelope_schema,
)
from packages.core.contracts.linker import repair_plan
from packages.core.contracts.shapes import get_shape
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.plans.schema import PlanStep
from packages.core.plans.service import (
    _resolve_output_shapes,
    _step_from_pydantic,
    plan_contract_gaps,
)


def _validate(envelope) -> None:
    Draft202012Validator(step_result_envelope_schema()).validate(envelope)


def _llm_step(key, kind="llm", output_shape=None, params=None):
    p = {"prompt": "do the thing"}
    if params:
        p.update(params)
    return PlanStep(key=key, kind=kind, service_key="content_creation", output_shape=output_shape, params=p)


def _plan_row():
    return type("_PlanRow", (), {"id": "p", "entity_id": "e", "workspace_id": None})()


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
        "failure": {"reason": 0, "blockers": "b", "retryable": "yes"},
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


# ── linker: unshaped llm/subagent steps default to the envelope ────────

def test_repair_defaults_unshaped_llm_step_to_step_result():
    steps = [{"key": "a", "kind": "subagent", "output_shape": None, "input_refs": []}]
    repaired, remaining = repair_plan(steps)
    assert repaired[0]["output_shape"] == "StepResult"
    assert remaining == []


def test_repair_keeps_field_inference_over_default():
    steps = [
        {"key": "a", "kind": "llm", "output_shape": None, "input_refs": []},
        {"key": "b", "kind": "llm", "output_shape": "TextResult", "input_refs": [("a", "drafts")]},
    ]
    repaired, _ = repair_plan(steps)
    assert next(s for s in repaired if s["key"] == "a")["output_shape"] == "DraftPack"


def test_repair_leaves_action_steps_unshaped():
    steps = [{"key": "a", "kind": "action", "output_shape": None, "input_refs": []}]
    repaired, _ = repair_plan(steps)
    assert repaired[0]["output_shape"] is None


def test_plan_contract_gaps_clean_for_unshaped_llm_steps():
    steps = [
        _llm_step("a", kind="subagent"),
        _llm_step("b", params={"prompt": "use ${{ steps.a.result.outputs.text }} when ${{ steps.a.result.status }}"}),
    ]
    assert plan_contract_gaps(steps) == []


def test_ref_outside_envelope_fields_is_still_a_gap():
    steps = [
        _llm_step("a"),
        _llm_step("b", params={"prompt": "use ${{ steps.a.result.content }}"}),
    ]
    gaps = plan_contract_gaps(steps)
    assert any(g.step_key == "b" and g.kind == "dangling_reference" for g in gaps)


# ── materialization: envelope schema, receipt strip, explicit shapes ───

def test_step_from_pydantic_defaults_to_envelope_schema():
    ps = _llm_step("a", kind="subagent")
    shapes = _resolve_output_shapes([ps])
    step = _step_from_pydantic(_plan_row(), ps, max_attempts=3, output_shape=shapes.get("a"))
    assert step.expected_output_schema == step_result_envelope_schema()


def test_step_from_pydantic_strips_receipt_fields_from_surviving_custom_schema():
    custom = {
        "type": "object",
        "required": ["tweet_id", "platform", "post_text"],
        "properties": {
            "tweet_id": {"type": "string"},
            "platform": {"type": "string"},
            "post_text": {"type": "string"},
        },
    }
    ps = _llm_step("a", kind="subagent").model_copy(update={"expected_output_schema": custom})
    step = _step_from_pydantic(_plan_row(), ps, max_attempts=3, output_shape=None)
    assert step.expected_output_schema["required"] == ["post_text"]
    # Properties are kept — only the requirement is dropped.
    assert "tweet_id" in step.expected_output_schema["properties"]
    # The source schema is not mutated.
    assert custom["required"] == ["tweet_id", "platform", "post_text"]


def test_step_from_pydantic_explicit_artifact_shape_unchanged():
    ps = _llm_step("a", output_shape="ArtifactResult")
    step = _step_from_pydantic(_plan_row(), ps, max_attempts=3, output_shape="ArtifactResult")
    assert "files" in step.expected_output_schema["properties"]
    assert step.expected_output_schema.get("$id") != STEP_RESULT_ENVELOPE_ID


# ── dispatcher coercion: envelope steps can never fail validation ──────

@pytest.mark.parametrize("garbage", _BATTERY, ids=[f"case_{i}" for i in range(len(_BATTERY))])
def test_coerce_step_output_for_envelope_schema_always_validates(garbage):
    coerced = coerce_step_output_for_schema(step_result_envelope_schema(), garbage)
    _validate(coerced)


def test_coerce_step_output_non_envelope_schema_unchanged():
    schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    assert coerce_step_output_for_schema(schema, None) is None


# ── planner prompt + JSON hint ─────────────────────────────────────────

def test_planner_prompt_describes_envelope_and_forbids_custom_schema():
    from packages.core.ai.runtime.planning import runtime_planner_system_prompt

    prompt = runtime_planner_system_prompt(
        subscriptions=[],
        agents_by_id={},
        allowed_service_keys=[],
    )
    assert "StepResult" in prompt
    assert "Do NOT author" in prompt
    assert "platform receipts" in prompt
    assert "steps.<key>.result.outputs.text" in prompt
    # Explicit specialized shapes remain available.
    assert "ArtifactResult" in prompt


def test_plan_json_hint_does_not_seed_expected_output_schema():
    from packages.core.ai.runtime.planning import RUNTIME_PLAN_JSON_HINT

    assert "expected_output_schema" not in json.dumps(RUNTIME_PLAN_JSON_HINT)
