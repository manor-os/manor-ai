"""Hard task-output contracts carried by terminal agent steps.

``Task.expected_output`` is authored before planning.  Unlike a Planner-guessed
step schema, it is the task's actual deliverable contract and must therefore be
enforced before the terminal step can complete.

Agent steps still use the standard StepResult envelope.  The task payload lives
under ``outputs.data`` so status, summary, files, and failure control signals
keep their stable runtime shape while the deliverable itself retains its exact
JSON Schema (including required fields and item-count constraints).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from packages.core.contracts.envelope import step_result_envelope_schema


TASK_OUTPUT_CONTRACT_SOURCE_KEY = "x-manor-contract-source"
TASK_OUTPUT_CONTRACT_SOURCE = "task.expected_output"
PLAN_OUTPUT_CONTRACT_SOURCE = "plan.output_shape"
PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE = "plan.expected_output_schema"
_PLAN_OUTPUT_CONTRACT_SOURCES = frozenset(
    {PLAN_OUTPUT_CONTRACT_SOURCE, PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE}
)

_STRUCTURAL_SCHEMA_KEYS = frozenset(
    {
        "$ref",
        "allOf",
        "anyOf",
        "const",
        "contains",
        "dependentRequired",
        "dependentSchemas",
        "enum",
        "if",
        "items",
        "maxItems",
        "maxProperties",
        "minItems",
        "minProperties",
        "not",
        "oneOf",
        "patternProperties",
        "prefixItems",
        "properties",
        "required",
        "then",
        "type",
        "unevaluatedProperties",
    }
)

# Manor acceptance/evidence metadata can share Task.expected_output with the
# JSON Schema.  It is useful to the Planner/Supervisor but is not part of the
# model-facing schema and some tool-schema providers reject unknown keywords.
_TASK_OUTPUT_METADATA_KEYS = frozenset(
    {
        "artifact_required",
        "artifact_type",
        "deliverables",
        "kind",
        "requires_artifact",
    }
)


def task_expected_output_json_schema(value: Any) -> dict[str, Any] | None:
    """Return the enforceable JSON Schema portion of Task.expected_output.

    Metadata-only expected-output payloads (for example ``deliverables`` used
    by artifact acceptance) intentionally return ``None``.  A malformed schema
    also returns ``None``; planning/supervision can then report the contract gap
    without turning every worker attempt into a validator crash.
    """

    if not isinstance(value, dict) or not (_STRUCTURAL_SCHEMA_KEYS & value.keys()):
        return None

    schema = deepcopy(value)
    properties = schema.get("properties")
    property_names = set(properties) if isinstance(properties, dict) else set()
    for key in _TASK_OUTPUT_METADATA_KEYS:
        if key not in property_names:
            schema.pop(key, None)

    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        return None
    return schema


def task_output_envelope_schema(expected_output: Any) -> dict[str, Any] | None:
    """Compose a StepResult envelope whose ``outputs.data`` is task-shaped."""

    task_schema = task_expected_output_json_schema(expected_output)
    if task_schema is None:
        return None

    schema = deepcopy(step_result_envelope_schema())
    schema[TASK_OUTPUT_CONTRACT_SOURCE_KEY] = TASK_OUTPUT_CONTRACT_SOURCE
    required = list(schema.get("required") or [])
    if "outputs" not in required:
        required.append("outputs")
    schema["required"] = required

    outputs_schema = schema["properties"]["outputs"]
    outputs_schema["required"] = ["data"]
    outputs_schema["properties"]["data"] = task_schema
    return schema


def is_task_output_contract_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get(TASK_OUTPUT_CONTRACT_SOURCE_KEY) == TASK_OUTPUT_CONTRACT_SOURCE


def plan_output_contract_schema(
    schema: Any,
    *,
    source: str = PLAN_OUTPUT_CONTRACT_SOURCE,
) -> dict[str, Any] | None:
    """Mark an explicitly declared PlanStep payload as a hard contract."""

    if not isinstance(schema, dict) or source not in _PLAN_OUTPUT_CONTRACT_SOURCES:
        return None
    output = deepcopy(schema)
    output[TASK_OUTPUT_CONTRACT_SOURCE_KEY] = source
    return output


def is_plan_output_contract_schema(schema: Any) -> bool:
    return (
        isinstance(schema, dict)
        and schema.get(TASK_OUTPUT_CONTRACT_SOURCE_KEY) in _PLAN_OUTPUT_CONTRACT_SOURCES
    )


def is_hard_output_contract_schema(schema: Any) -> bool:
    """Whether a schema was explicitly declared before the step ran."""

    return is_task_output_contract_schema(schema) or is_plan_output_contract_schema(schema)


def task_output_payload_schema(schema: Any) -> dict[str, Any] | None:
    """Extract the original task payload schema from a composed envelope."""

    if not is_task_output_contract_schema(schema):
        return None
    try:
        data_schema = schema["properties"]["outputs"]["properties"]["data"]
    except (KeyError, TypeError):
        return None
    return deepcopy(data_schema) if isinstance(data_schema, dict) else None


def declared_output_payload_schema(schema: Any) -> dict[str, Any] | None:
    """Return the model-facing payload contract for any hard output schema."""

    task_schema = task_output_payload_schema(schema)
    if task_schema is not None:
        return task_schema
    if not is_plan_output_contract_schema(schema):
        return None
    output = deepcopy(schema)
    output.pop(TASK_OUTPUT_CONTRACT_SOURCE_KEY, None)
    return output


def task_output_payload(result: Any, schema: Any) -> Any:
    """Return outputs.data from a task-contracted StepResult, if present."""

    if not is_task_output_contract_schema(schema) or not isinstance(result, dict):
        return None
    outputs = result.get("outputs")
    return outputs.get("data") if isinstance(outputs, dict) and "data" in outputs else None


def terminal_agent_step_key(steps: Iterable[Any]) -> str | None:
    """Return the unique terminal llm/subagent step, or ``None`` if ambiguous."""

    step_list = list(steps)
    depended_on = {str(dep) for step in step_list for dep in (getattr(step, "depends_on", None) or [])}
    candidates = [
        step
        for step in step_list
        if str(getattr(step, "key", "")) not in depended_on and str(getattr(step, "kind", "")) in {"llm", "subagent"}
    ]
    if len(candidates) != 1:
        return None
    return str(getattr(candidates[0], "key", "")) or None
