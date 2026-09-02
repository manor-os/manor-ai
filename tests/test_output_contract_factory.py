"""The output-contract classifier is shared by planner, worker, and dispatcher."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from packages.core.contracts.envelope import step_result_envelope_schema
from packages.core.contracts.json_schema import SchemaContractValidatorFactory
from packages.core.contracts.shapes import get_shape
from packages.core.contracts.task_output import (
    MissingOutputContract,
    OutputContractKind,
    SchemaContractError,
    TaskOutputValueKind,
    is_concrete_output_contract_schema,
    known_schema_path,
    known_top_level_keys,
    output_contract_for_schema,
    plan_output_contract_schema,
    task_expected_output_declares_schema,
    task_expected_output_json_schema,
    task_output_envelope_schema,
    task_output_payload_schema,
    terminal_agent_step_key,
    validate_schema_contract,
)
from packages.core.dispatcher.validation import (
    SchemaError,
    maybe_collect_errors,
    validate_step_output,
)
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema


def test_factory_distinguishes_envelope_and_bare_payload_contracts() -> None:
    task = output_contract_for_schema(task_output_envelope_schema({"type": "string"}))
    canonical = output_contract_for_schema(
        plan_output_contract_schema(get_shape("ArtifactResult").json_schema())
    )
    custom = output_contract_for_schema(
        plan_output_contract_schema(
            {"type": "object", "required": ["packet"], "properties": {"packet": {"type": "string"}}},
            source="plan.expected_output_schema",
        )
    )
    legacy = output_contract_for_schema(step_result_envelope_schema())
    unmarked = output_contract_for_schema({"type": "object", "properties": {"value": {}}})

    assert task.kind is OutputContractKind.TASK_ENVELOPE
    assert task.is_envelope and task.is_hard and task.requires_result
    assert not task.preserves_native_payload
    assert canonical.kind is OutputContractKind.PLAN_CANONICAL
    assert canonical.is_bare_payload and canonical.payload_schema["required"] == ["files"]
    assert canonical.preserves_native_payload
    assert custom.kind is OutputContractKind.PLAN_CUSTOM
    assert custom.is_bare_payload and custom.payload_schema["required"] == ["packet"]
    assert custom.preserves_native_payload
    assert legacy.kind is OutputContractKind.LEGACY_ENVELOPE
    assert legacy.is_envelope and not legacy.is_hard
    assert not legacy.preserves_native_payload
    assert unmarked.kind is OutputContractKind.UNMARKED_SCHEMA
    assert not unmarked.is_hard
    assert unmarked.preserves_native_payload
    assert not unmarked.preserves_native_payload_for("subagent")
    assert unmarked.preserves_native_payload_for("action")
    assert not unmarked.preserves_native_payload_for(" SUBAGENT ")
    assert unmarked.preserves_native_payload_for(" ACTION ")
    assert not unmarked.requires_submission_result_for("SUBAGENT")
    assert unmarked.requires_submission_result_for("ACTION")
    assert output_contract_for_schema(None).kind is OutputContractKind.NONE


def test_terminal_agent_marker_matching_normalizes_historical_kind_case() -> None:
    steps = [
        SimpleNamespace(key="research", kind="ACTION", depends_on=[]),
        SimpleNamespace(key="draft", kind="SUBAGENT", depends_on=["research"]),
    ]
    assert terminal_agent_step_key(steps) == "draft"


def test_schema_contract_rejects_dangling_and_external_refs() -> None:
    with pytest.raises(SchemaContractError, match="unresolved local"):
        validate_schema_contract({"$ref": "#/$defs/Missing", "$defs": {}})
    with pytest.raises(SchemaContractError, match="non-local"):
        validate_schema_contract({"$ref": "https://example.test/schema.json"})

    # A resolvable local ref is syntactically safe even though the linker keeps
    # composed shapes unresolved for field-binding purposes.
    validate_schema_contract(
        {
            "$ref": "#/$defs/Payload",
            "$defs": {
                "Payload": {
                    "type": "object",
                    "required": ["packet"],
                    "properties": {"packet": {"type": "string"}},
                }
            },
        }
    )
    validate_schema_contract(
        {
            "$ref": "#Payload",
            "$defs": {"Payload": {"$anchor": "Payload", "type": "string"}},
        }
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},
        {
            "$defs": {
                "left": {"$ref": "#/$defs/right"},
                "right": {"$ref": "#/$defs/left"},
            },
            "$ref": "#/$defs/left",
        },
        {"allOf": [{"$ref": "#"}]},
    ],
)
def test_schema_contract_rejects_non_consuming_recursive_refs(
    schema: dict[str, Any],
) -> None:
    with pytest.raises(SchemaContractError, match="non-consuming recursive"):
        validate_schema_contract(schema)


def test_schema_contract_accepts_productive_recursive_child_refs() -> None:
    schema = {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "child": {"$ref": "#/$defs/node"},
                },
                "required": ["value"],
                "additionalProperties": False,
            }
        },
        "$ref": "#/$defs/node",
    }

    validator = SchemaContractValidatorFactory.build(schema)

    assert validator.is_valid({"value": "root", "child": {"value": "leaf"}})
    assert not validator.is_valid({"value": "root", "child": {"value": 42}})


def test_schema_contract_accepts_unreachable_recursive_conditional_branch() -> None:
    schema = {
        "if": False,
        "then": {"$ref": "#"},
        "else": {"type": "string"},
    }

    validator = SchemaContractValidatorFactory.build(schema)

    assert validator.is_valid("ready")
    assert not validator.is_valid(42)


def test_schema_contract_does_not_scan_dead_branch_as_a_new_recursive_root() -> None:
    schema = {
        "if": False,
        "then": {
            "$id": "urn:manor:dead-branch",
            "$ref": "#",
        },
        "else": {"type": "string"},
    }

    validator = SchemaContractValidatorFactory.build(schema)

    assert validator.is_valid("ready")
    assert not validator.is_valid(42)


def test_known_schema_path_prefers_tuple_prefix_items_over_fallback_items() -> None:
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "minItems": 1,
                "prefixItems": [{"type": "string"}],
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
            }
        },
    }
    # Index 0 is governed by prefixItems, not the fallback items schema.
    assert known_schema_path(schema, "files[0].name", require_required=True) is False


def test_known_schema_path_uses_items_after_tuple_prefix() -> None:
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "minItems": 2,
                "prefixItems": [{"type": "string"}],
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
            }
        },
    }
    assert known_schema_path(schema, "files[1].name", require_required=True) is True


def test_task_envelope_rebases_local_refs_and_tool_restores_payload_root() -> None:
    payload = {
        "$ref": "#/$defs/Payload",
        "$defs": {
            "Payload": {
                "type": "object",
                "required": ["packet"],
                "properties": {"packet": {"type": "string"}},
            }
        },
    }
    envelope = task_output_envelope_schema(payload)
    assert envelope is not None
    assert envelope["properties"]["outputs"]["properties"]["data"]["$ref"] == (
        "#/properties/outputs/properties/data/$defs/Payload"
    )
    assert task_output_payload_schema(envelope)["$ref"] == "#/$defs/Payload"
    validate_schema_contract(envelope)


@pytest.mark.parametrize(
    ("schema", "accepted", "rejected"),
    [
        ({"multipleOf": 2}, 4, 3),
        ({"exclusiveMinimum": 0}, 1, 0),
        ({"exclusiveMaximum": 10}, 9, 10),
        ({"format": "email"}, "valid@example.com", "not-an-email"),
        ({"format": "uri"}, "https://example.com/path", "not a uri"),
    ],
)
def test_keyword_only_task_schema_is_detected_and_enforced(
    schema: dict,
    accepted: Any,
    rejected: Any,
) -> None:
    assert task_expected_output_declares_schema(schema)
    assert task_expected_output_json_schema(schema) == schema

    envelope_schema = task_output_envelope_schema(schema)
    accepted_envelope = coerce_step_output_for_schema(
        envelope_schema,
        accepted,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    validate_step_output(
        SimpleNamespace(step_key="terminal", expected_output_schema=envelope_schema),
        accepted_envelope,
    )

    rejected_envelope = coerce_step_output_for_schema(
        envelope_schema,
        rejected,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    assert maybe_collect_errors(envelope_schema, rejected_envelope)
    with pytest.raises(SchemaError):
        validate_step_output(
            SimpleNamespace(step_key="terminal", expected_output_schema=envelope_schema),
            rejected_envelope,
        )


def test_schema_contract_rejects_unknown_format_instead_of_ignoring_it() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string", "format": "manor-custom"}
        },
    }
    with pytest.raises(SchemaContractError, match="unsupported JSON Schema format"):
        validate_schema_contract(schema)

    soft_errors = maybe_collect_errors(schema, {"value": "anything"})
    assert soft_errors[0]["path"] == "$"
    assert "unsupported JSON Schema format" in soft_errors[0]["message"]


def test_schema_contract_rejects_a_declared_legacy_dialect() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "dependencies": {"credit_card": ["billing_address"]},
    }

    with pytest.raises(SchemaContractError, match="unsupported JSON Schema dialect"):
        validate_schema_contract(schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"title": "Shortlist"},
        {"description": "Return a shortlist."},
        {"default": {"status": "ready"}},
        {"examples": [{"status": "ready"}]},
        {"$defs": {"Result": {"type": "object"}}},
    ],
)
def test_annotation_or_definition_only_task_output_is_not_a_hard_schema(
    schema: dict[str, Any],
) -> None:
    assert task_expected_output_declares_schema(schema) is False
    assert task_expected_output_json_schema(schema) is None
    assert task_output_envelope_schema(schema) is None
    assert is_concrete_output_contract_schema(schema) is False


@pytest.mark.parametrize(
    "schema",
    [
        {"if": {"type": "string"}},
        {"properties": {}},
        {"required": []},
        {"allOf": []},
    ],
)
def test_noop_task_schema_is_declared_but_not_a_concrete_contract(
    schema: dict[str, Any],
) -> None:
    assert task_expected_output_declares_schema(schema) is True
    assert task_expected_output_json_schema(schema) is None
    assert task_output_envelope_schema(schema) is None
    assert is_concrete_output_contract_schema(schema) is False


@pytest.mark.parametrize(
    "schema",
    [
        {"enum": []},
        {"not": {}},
        {"allOf": [False]},
        {"oneOf": [{}, {}]},
        {"const": 1, "type": "string"},
        {"enum": [1, 2], "type": "string"},
    ],
)
def test_unsatisfiable_task_schema_is_not_a_concrete_contract(
    schema: dict[str, Any],
) -> None:
    with pytest.raises(SchemaContractError, match="does not accept any JSON value"):
        validate_schema_contract(schema)
    assert task_expected_output_json_schema(schema) is None
    assert task_output_envelope_schema(schema) is None
    assert is_concrete_output_contract_schema(schema) is False


@pytest.mark.parametrize(
    "schema",
    [
        {"anyOf": [{"type": "number"}, {"not": {"type": "number"}}]},
        {"oneOf": [{"type": "number"}, {"not": {"type": "number"}}]},
        {
            "anyOf": [
                {"type": "array"},
                {"type": "boolean"},
                {"type": "integer"},
                {"type": "null"},
                {"type": "number"},
                {"type": "object"},
                {"type": "string"},
            ]
        },
        {
            "anyOf": [
                {"required": ["packet"]},
                {"not": {"required": ["packet"]}},
            ]
        },
    ],
)
def test_tautological_compositions_are_not_concrete_contracts(
    schema: dict[str, Any],
) -> None:
    validator = SchemaContractValidatorFactory.build(schema)
    assert all(
        validator.is_valid(value)
        for value in (None, True, 1, 1.5, "text", [], {})
    )
    assert SchemaContractValidatorFactory.constrains_values(schema) is False
    assert task_expected_output_json_schema(schema) is None


def test_narrow_type_union_remains_a_concrete_contract() -> None:
    schema = {"anyOf": [{"type": "number"}, {"type": "string"}]}

    assert SchemaContractValidatorFactory.constrains_values(schema) is True
    assert task_expected_output_json_schema(schema) == schema


def test_complement_detection_respects_nested_resource_scope() -> None:
    schema = {
        "anyOf": [
            {
                "$id": "urn:manor:left",
                "$defs": {"value": {"type": "string"}},
                "$ref": "#/$defs/value",
            },
            {
                "$id": "urn:manor:right",
                "$defs": {"value": {"type": "number"}},
                "not": {"$ref": "#/$defs/value"},
            },
        ]
    }

    validator = SchemaContractValidatorFactory.build(schema)

    assert validator.is_valid("text")
    assert not validator.is_valid(1)
    assert SchemaContractValidatorFactory.constrains_values(schema) is True
    assert task_expected_output_json_schema(schema) == schema


@pytest.mark.parametrize(
    "schema",
    [
        {"items": False},
        {"properties": {"forbidden": False}},
        {"not": {"items": False}},
    ],
)
def test_conditional_applicators_remain_concrete_contracts(
    schema: dict[str, Any],
) -> None:
    assert SchemaContractValidatorFactory.constrains_values(schema) is True
    assert task_expected_output_json_schema(schema) == schema


def test_schema_contract_resolves_refs_inside_nested_resources() -> None:
    schema = {
        "$defs": {
            "inner": {
                "$id": "inner.json",
                "$defs": {"value": {"type": "string"}},
                "$ref": "#/$defs/value",
            }
        },
        "$ref": "#/$defs/inner",
    }

    validator = SchemaContractValidatorFactory.build(schema)

    assert validator.is_valid("ready")
    assert not validator.is_valid(42)


def test_schema_contract_rejects_ref_to_non_schema_instance_data() -> None:
    schema = {"default": "not-a-schema", "$ref": "#/default"}

    with pytest.raises(SchemaContractError, match="does not target a schema"):
        validate_schema_contract(schema)


def test_schema_contract_does_not_treat_instance_data_as_nested_schema() -> None:
    validate_schema_contract(
        {
            "const": {
                "format": "manor-custom",
                "$ref": "https://instance-data.example/reference",
            }
        }
    )


def test_known_top_level_keys_fails_closed_for_non_guaranteed_shapes() -> None:
    assert known_top_level_keys(
        {"type": "object", "required": ["packet"], "properties": {"packet": {}}}
    ) == {"packet"}
    assert known_top_level_keys({"type": "array", "items": {}}) == set()
    assert known_top_level_keys({"properties": {"packet": {}}}) is None
    assert known_top_level_keys({"$ref": "#/$defs/Missing", "$defs": {}}) is None


def test_hard_submission_gate_preserves_explicit_null() -> None:
    contract = output_contract_for_schema(
        plan_output_contract_schema(
            {"type": ["null", "object"], "properties": {"value": {}}}
        )
    )

    with pytest.raises(MissingOutputContract):
        contract.require_submission_result({"summary": "omitted"})
    # Explicit null is present and is left to JSON Schema validation.
    contract.require_submission_result({"summary": "empty", "result": None})


def test_dispatcher_validates_none_for_hard_contract_but_accepts_nullable_null() -> None:
    required_payload = plan_output_contract_schema(
        {
            "type": "object",
            "required": ["files"],
            "properties": {"files": {"type": "array"}},
        }
    )
    step = SimpleNamespace(
        plan_id="plan_1",
        step_key="prepare",
        expected_output_schema=required_payload,
    )

    with pytest.raises(SchemaError):
        validate_step_output(step, None)

    nullable = plan_output_contract_schema({"type": "null"})
    nullable_step = SimpleNamespace(
        plan_id="plan_1",
        step_key="optional",
        expected_output_schema=nullable,
    )
    validate_step_output(nullable_step, None)


def test_task_envelope_coerces_raw_terminal_llm_payload_under_outputs_data() -> None:
    payload_schema = {
        "type": "object",
        "required": ["packet"],
        "properties": {"packet": {"type": "string"}},
    }
    envelope_schema = task_output_envelope_schema(payload_schema)
    raw = {"packet": "ready"}
    coerced = coerce_step_output_for_schema(
        envelope_schema,
        raw,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    assert coerced["outputs"]["data"] == raw
    validate_step_output(
        SimpleNamespace(step_key="terminal", expected_output_schema=envelope_schema),
        coerced,
    )


def test_canonical_shape_normalizer_runs_after_bare_contract_classification() -> None:
    schema = plan_output_contract_schema(get_shape("ArtifactResult").json_schema())
    coerced = coerce_step_output_for_schema(
        schema,
        {"files": [{"name": "brief.md", "path": "workspace/brief.md"}]},
    )
    assert coerced["files"][0]["fs_path"] == "workspace/brief.md"
