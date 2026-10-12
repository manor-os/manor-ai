"""Final artifact identity comes from the output contract, not prose scans."""
from __future__ import annotations

from copy import deepcopy

import pytest

from packages.core.contracts.task_output import plan_output_contract_schema, task_output_envelope_schema
from packages.core.services.artifact_delivery import (
    ArtifactDeliveryError,
    resolve_artifact_delivery_submission,
    submitted_artifact_refs,
)


REF = {"fs_path": "Workspaces/_by_id/ws/release.md", "document_id": "doc-1"}
IDENTITY = {
    "type": "object",
    "required": ["fs_path"],
    "properties": {"fs_path": {"type": "string"}, "document_id": {"type": "string"}},
    "additionalProperties": False,
}


def _task_schema(payload_schema):
    return task_output_envelope_schema(payload_schema)


def test_task_envelope_accepts_required_nested_identity_without_business_key_alias():
    schema = _task_schema({
        "type": "object", "required": ["artifact_evidence"],
        "properties": {"artifact_evidence": IDENTITY, "summary": {"type": "string"}},
        "additionalProperties": False,
    })
    result = {"status": "succeeded", "outputs": {"data": {"artifact_evidence": REF, "summary": "ready"}}}
    original = deepcopy(result)

    with pytest.raises(ArtifactDeliveryError) as old:
        submitted_artifact_refs(result)
    assert old.value.code == "artifact_reference_missing"
    assert submitted_artifact_refs(result, output_schema=schema) == [REF]
    assert result == original


def test_explicit_marker_chooses_final_slot_over_other_schema_identity_objects():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["source", "final"],
        "properties": {
            "source": IDENTITY,
            "final": {**IDENTITY, "x-manor-deliverable": True},
        },
    })
    result = {"source": {"fs_path": "Workspaces/_by_id/ws/source.md"}, "final": REF}

    assert submitted_artifact_refs(result, output_schema=schema) == [REF]


def test_unmarked_multiple_required_identity_slots_fail_ambiguous_not_source_promoted():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["source", "final"],
        "properties": {"source": IDENTITY, "final": IDENTITY},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({"source": REF, "final": REF}, output_schema=schema)

    assert error.value.code == "artifact_reference_ambiguous"
    assert error.value.details["candidate_pointers"] == ["/source", "/final"]


def test_one_required_array_slot_can_deliver_multiple_files():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["produced"],
        "properties": {"produced": {"type": "array", "minItems": 1, "items": IDENTITY}},
    })
    refs = [REF, {"fs_path": "Workspaces/_by_id/ws/other.md"}]

    assert submitted_artifact_refs({"produced": refs}, output_schema=schema) == refs


def test_marked_optional_slot_missing_does_not_fall_back_to_source():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["source"],
        "properties": {"source": IDENTITY, "final": {**IDENTITY, "x-manor-deliverable": True}},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({"source": REF}, output_schema=schema)

    assert error.value.code == "artifact_reference_missing"
    assert error.value.details["pointer"] == "/final"


def test_marked_slot_under_absent_optional_container_still_fails_closed():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["source"],
        "properties": {
            "source": IDENTITY,
            "delivery": {"type": "object", "properties": {
                "final": {**IDENTITY, "x-manor-deliverable": True},
            }},
        },
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({"source": REF}, output_schema=schema)

    assert error.value.code == "artifact_reference_missing"
    assert error.value.details["pointer"] == "/delivery/final"


def test_schema_only_follows_declared_properties_not_extra_runtime_keys_or_prose():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({
            "summary": "Found a candidate", "source": {"fs_path": "secret.md"},
        }, output_schema=schema)
    assert error.value.code == "artifact_reference_missing"


@pytest.mark.parametrize("invalid,kind", [(123, "int"), (float("nan"), "float"), (False, "bool")])
def test_declared_identity_wrong_type_fails_with_exact_pointer(invalid, kind):
    schema = plan_output_contract_schema({
        "type": "object", "required": ["final"], "properties": {"final": IDENTITY},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({"final": {"fs_path": invalid}}, output_schema=schema)

    assert error.value.code == "artifact_identity_invalid"
    assert error.value.details == {"pointer": "/final/fs_path", "expected": "nonempty string", "actual": kind}


def test_missing_identity_has_field_level_diagnostics():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["final"], "properties": {"final": IDENTITY},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({"final": {}}, output_schema=schema)

    assert error.value.code == "artifact_reference_missing"
    assert error.value.details == {"pointer": "/final/fs_path", "expected": "nonempty artifact identity", "actual": "missing"}


def test_local_schema_ref_identifies_one_structural_slot():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["delivery"],
        "$defs": {"file": IDENTITY},
        "properties": {"delivery": {"$ref": "#/$defs/file"}},
    })

    assert submitted_artifact_refs({"delivery": REF}, output_schema=schema) == [REF]


def test_task_envelope_local_ref_preserves_explicit_final_pointer():
    schema = _task_schema({
        "type": "object", "required": ["source", "delivery"],
        "$defs": {"file": IDENTITY},
        "properties": {
            "source": {"$ref": "#/$defs/file"},
            "delivery": {"$ref": "#/$defs/file", "x-manor-deliverable": True},
        },
    })
    result = {"outputs": {"data": {
        "source": {"fs_path": "Workspaces/_by_id/ws/input.md"},
        "delivery": REF,
    }}}

    resolved = resolve_artifact_delivery_submission(result, output_schema=schema)
    assert resolved.refs == [REF]
    assert resolved.declaration == {
        "basis": "explicit_schema",
        "pointers": ["/outputs/data/delivery"],
        "requires_semantic_assessment": True,
    }


def test_unique_required_source_is_labeled_candidate_not_claimed_final_proof():
    schema = _task_schema({
        "type": "object", "required": ["source", "final_text"],
        "properties": {"source": IDENTITY, "final_text": {"type": "string"}},
    })
    result = {"outputs": {"data": {"source": REF, "final_text": "No file was produced."}}}

    resolved = resolve_artifact_delivery_submission(result, output_schema=schema)
    assert resolved.refs == [REF]
    assert resolved.declaration == {
        "basis": "unique_required_schema",
        "pointers": ["/outputs/data/source"],
        "requires_semantic_assessment": True,
    }


def test_optional_unmarked_final_does_not_beat_required_source_candidate():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["source"],
        "properties": {"source": IDENTITY, "final": IDENTITY},
    })

    resolved = resolve_artifact_delivery_submission({"source": REF}, output_schema=schema)
    assert resolved.declaration["basis"] == "unique_required_schema"
    assert resolved.declaration["pointers"] == ["/source"]
    assert resolved.declaration["requires_semantic_assessment"] is True


def test_legacy_explicit_collection_reports_exact_conventional_pointer():
    result = {"outputs": {"files": [REF]}}

    resolved = resolve_artifact_delivery_submission(result)
    assert resolved.refs == [REF]
    assert resolved.declaration == {
        "basis": "legacy_explicit",
        "pointers": ["/outputs/files/0"],
        "requires_semantic_assessment": True,
    }


def test_local_schema_ref_cycle_fails_bounded():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["delivery"],
        "$defs": {"loop": {"$ref": "#/$defs/loop"}},
        "properties": {"delivery": {"$ref": "#/$defs/loop"}},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({"delivery": REF}, output_schema=schema)
    assert error.value.code == "artifact_schema_unsupported"
    assert error.value.details == {"pointer": "/delivery"}


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf"])
def test_marked_delivery_under_unsupported_composition_never_falls_back_to_legacy_source(keyword):
    schema = plan_output_contract_schema({
        "type": "object",
        keyword: [{"properties": {"final": {**IDENTITY, "x-manor-deliverable": True}}}],
    })
    result = {"files": [{"fs_path": "Workspaces/_by_id/ws/input.md"}], "generated": True}

    with pytest.raises(ArtifactDeliveryError) as error:
        resolve_artifact_delivery_submission(result, output_schema=schema)
    assert error.value.code == "artifact_schema_unsupported"
    assert error.value.details == {"schema_pointer": f"/{keyword}", "unsupported_keyword": keyword}


def test_dynamic_ref_with_explicit_delivery_marker_fails_typed_not_legacy_fallback():
    schema = plan_output_contract_schema({
        "type": "object", "$dynamicRef": "#candidate",
        "properties": {"final": {**IDENTITY, "x-manor-deliverable": True}},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        resolve_artifact_delivery_submission({"files": [REF]}, output_schema=schema)
    assert error.value.code == "artifact_schema_unsupported"
    assert error.value.details == {"schema_pointer": "/$dynamicRef", "unsupported_keyword": "$dynamicRef"}


def test_non_schema_example_data_does_not_trigger_unsupported_keyword_guard():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["final"],
        "examples": [{"allOf": "not a schema keyword here"}],
        "properties": {"final": {**IDENTITY, "x-manor-deliverable": True}},
    })
    assert submitted_artifact_refs({"final": REF}, output_schema=schema) == [REF]


def test_unreachable_deep_explicit_marker_does_not_fall_back_to_source():
    slot = {**IDENTITY, "x-manor-deliverable": True}
    for _ in range(9):
        slot = {"type": "object", "properties": {"next": slot}}
    schema = plan_output_contract_schema({
        "type": "object", "properties": {"delivery": slot},
    })

    with pytest.raises(ArtifactDeliveryError) as error:
        resolve_artifact_delivery_submission({"files": [REF]}, output_schema=schema)
    assert error.value.code == "artifact_schema_unsupported"
    assert error.value.details["reason"] == "marked_delivery_slot_unresolved"
    assert error.value.details["schema_pointer"].endswith("/properties/next" * 9)


def test_declared_array_reference_count_stays_bounded():
    schema = plan_output_contract_schema({
        "type": "object", "required": ["files"],
        "properties": {"files": {"type": "array", "minItems": 1, "items": IDENTITY}},
    })
    with pytest.raises(ArtifactDeliveryError) as error:
        submitted_artifact_refs({"files": [{"fs_path": f"{i}.md"} for i in range(33)]}, output_schema=schema)
    assert error.value.code == "artifact_reference_limit"
