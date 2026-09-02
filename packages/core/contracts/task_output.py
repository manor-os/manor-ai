"""Hard task-output contracts carried by terminal agent steps.

``Task.expected_output`` is authored before planning.  Unlike a Planner-guessed
step schema, it is the task's actual deliverable contract and must therefore be
enforced before the terminal step can complete.

The terminal agent step bound to ``Task.expected_output`` uses the standard
StepResult envelope.  Its task payload lives under ``outputs.data`` so status,
summary, files, and failure control signals keep their stable runtime shape
while the deliverable itself retains its exact JSON Schema (including required
fields and item-count constraints).  In contrast, a Planner-authored explicit
PlanStep ``output_shape``/``expected_output_schema`` is a bare payload contract:
its fields are referenced directly under ``steps.<key>.result``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Iterable

from packages.core.contracts.envelope import (
    StepResultStatus,
    is_step_result_envelope_schema,
    normalize_step_result_status,
    step_result_envelope_schema,
)
from packages.core.contracts.json_schema import (
    SchemaContractError,
    SchemaContractValidatorFactory,
    iter_schema_refs,
    validate_schema_contract,
)


TASK_OUTPUT_CONTRACT_SOURCE_KEY = "x-manor-contract-source"
TASK_OUTPUT_CONTRACT_SOURCE = "task.expected_output"
PLAN_OUTPUT_CONTRACT_SOURCE = "plan.output_shape"
PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE = "plan.expected_output_schema"
_PLAN_OUTPUT_CONTRACT_SOURCES = frozenset(
    {PLAN_OUTPUT_CONTRACT_SOURCE, PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE}
)

_TASK_OUTPUT_SCHEMA_DECLARATION_KEYS = frozenset(
    {
        *SchemaContractValidatorFactory.assertion_keywords(),
        "$schema",
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

_COMPOSED_SCHEMA_KEYS = frozenset(
    {
        "$ref",
        "$dynamicRef",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentSchemas",
        "patternProperties",
        "unevaluatedProperties",
    }
)

# The reference parser accepts the same dotted/indexed grammar.  Keep a
# strict schema-path grammar here instead of extracting tokens with a loose
# regex: ``files[0].name`` is provable, while malformed paths such as
# ``files..name`` or ``files[foo]`` must fail closed before they reach refs.py.
_SCHEMA_PATH_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[\d+\]))*"
)
_SCHEMA_PATH_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\[(\d+)\]")


def _normalize_step_kind(step_kind: Any) -> str:
    """Normalize persisted/API step-kind values before contract decisions."""

    return str(step_kind or "").strip().lower()


class OutputContractKind(str, Enum):
    """The runtime representation of a step's declared output contract.

    The marker is deliberately about *representation*, not the producer kind:
    a task deliverable keeps the legacy StepResult envelope, while Planner
    output contracts are bare payloads.  Keeping this distinction in one enum
    prevents the submit tool, coercer, linker, and dispatcher from each
    re-implementing the source-marker rules slightly differently.
    """

    NONE = "none"
    LEGACY_ENVELOPE = "legacy_envelope"
    TASK_ENVELOPE = "task_envelope"
    PLAN_CANONICAL = "plan_canonical"
    PLAN_CUSTOM = "plan_custom"
    UNMARKED_SCHEMA = "unmarked_schema"


class TaskOutputValueKind(str, Enum):
    """Runtime representation of one value submitted for a Task contract."""

    TASK_PAYLOAD = "task_payload"
    STEP_RESULT_ENVELOPE = "step_result_envelope"
    STEP_RESULT_FAILURE = "step_result_failure"


class MissingOutputContract(ValueError):
    """A hard contract submission omitted its required ``result`` member."""


class TaskOutputProtocolError(ValueError):
    """A Task completion omitted or violated its representation contract."""


@dataclass(frozen=True)
class OutputContract:
    """Immutable view of one materialized ``expected_output_schema``.

    ``payload_schema`` is the model-facing value schema.  It is the nested
    ``outputs.data`` schema for task contracts and the schema itself for bare
    PlanStep contracts.  An unmarked schema remains advisory for validation on
    legacy llm/subagent rows, but its declared JSON value is still retained
    natively; structured/action rows use the same schema as a hard contract.
    """

    kind: OutputContractKind
    schema: dict[str, Any] | None = None
    payload_schema: dict[str, Any] | None = None

    @property
    def has_schema(self) -> bool:
        return self.schema is not None

    @property
    def is_envelope(self) -> bool:
        return self.kind in {
            OutputContractKind.LEGACY_ENVELOPE,
            OutputContractKind.TASK_ENVELOPE,
        }

    @property
    def is_bare_payload(self) -> bool:
        return self.kind in {
            OutputContractKind.PLAN_CANONICAL,
            OutputContractKind.PLAN_CUSTOM,
        }

    @property
    def preserves_native_payload(self) -> bool:
        """Whether the validated value must be persisted without an envelope.

        Planner-authored contracts are explicitly bare, but provider schemas
        attached to structured/action steps are bare too even when they predate
        the provenance marker.  Keeping this representation rule separate from
        ``is_hard`` preserves legacy advisory LLM *validation* behavior while
        still retaining the schema's native JSON type; this prevents
        array/string/null action results from being silently rewritten as
        ``{"value": ...}``.
        """

        return self.has_schema and not self.is_envelope

    def preserves_native_payload_for(self, step_kind: str | None) -> bool:
        """Whether runtime enrichment/coercion must keep a native value.

        ``preserves_native_payload`` describes the persisted wire value and is
        intentionally true for every declared non-envelope schema.  Legacy
        unmarked LLM/subagent rows still use the historical best-effort
        enrichment path, however; callers that know the producer kind can use
        this narrower gate without changing the storage representation.
        """
        if not self.preserves_native_payload:
            return False
        if self.kind is OutputContractKind.UNMARKED_SCHEMA:
            return _normalize_step_kind(step_kind) not in {"llm", "subagent"}
        return True

    @property
    def canonical_shape_name(self) -> str | None:
        """Recover the registered canonical shape for a marked payload.

        Canonical shapes are persisted as their JSON Schema plus provenance;
        keeping the registry name out of the wire schema avoids introducing a
        second vendor keyword that tool providers might reject. Equality is
        deterministic because registry shapes own their schema dictionaries.
        """
        if self.kind is not OutputContractKind.PLAN_CANONICAL or not isinstance(
            self.payload_schema, dict
        ):
            return None
        try:
            from packages.core.contracts.shapes import get_shape, shape_names

            for name in shape_names():
                if get_shape(name).json_schema() == self.payload_schema:
                    return name
        except Exception:  # noqa: BLE001 - classification must stay best-effort
            return None
        return None

    @property
    def is_hard(self) -> bool:
        return self.kind in {
            OutputContractKind.TASK_ENVELOPE,
            OutputContractKind.PLAN_CANONICAL,
            OutputContractKind.PLAN_CUSTOM,
        }

    @property
    def requires_result(self) -> bool:
        """Whether an omitted worker result must be rejected.

        Explicit contracts require the tool's ``result`` member.  This is
        intentionally separate from ``result is None`` because JSON ``null``
        may be a valid value under a custom schema; the dispatcher must still
        run the schema validator to distinguish omission from allowed null.
        A failed Task envelope is the sole exception: it carries control
        failure evidence instead of pretending to have produced a deliverable.
        """

        return self.is_hard

    def requires_submission_result_for(self, step_kind: str | None) -> bool:
        """Whether a worker protocol must include the ``result`` member.

        Unmarked llm/subagent schemas are the explicitly supported legacy
        advisory path. Structured providers (actions/code/etc.) have a real
        external schema even when the materializer cannot attach a provenance
        marker, so omission must not be silently treated as an allowed null.
        """
        if self.requires_result:
            return True
        return self.has_schema and _normalize_step_kind(step_kind) not in {"llm", "subagent"}

    def require_submission_result(self, payload: Any) -> None:
        """Reject an omitted result while preserving explicit JSON ``null``.

        The submit tool remains tolerant so malformed model arguments can be
        captured for diagnostics.  The worker calls this gate immediately
        after capture; a payload containing ``result: null`` is deliberately
        allowed through to JSON Schema validation, where a ``type: null``
        contract can accept it. Failed Task envelopes may omit the deliverable
        because their failure block is the authoritative terminal evidence.
        """

        failed_task_envelope = (
            self.kind is OutputContractKind.TASK_ENVELOPE
            and isinstance(payload, dict)
            and normalize_step_result_status(payload.get("status"))
            is StepResultStatus.FAILED
        )
        if self.requires_result and not failed_task_envelope and (
            not isinstance(payload, dict) or "result" not in payload
        ):
            raise MissingOutputContract(
                f"{self.kind.value} requires submit_result.result"
            )

    def require_task_output_value_kind(
        self,
        value: Any,
        *,
        declared_kind: TaskOutputValueKind | str | None = None,
    ) -> TaskOutputValueKind:
        """Return the explicitly declared v2 representation for a Task value."""

        if self.kind is not OutputContractKind.TASK_ENVELOPE:
            return TaskOutputValueKind.TASK_PAYLOAD
        if declared_kind is None:
            raise TaskOutputProtocolError(
                "Task output completion requires task_output_value_kind"
            )
        try:
            explicit_kind = TaskOutputValueKind(declared_kind)
        except (TypeError, ValueError) as exc:
            raise TaskOutputProtocolError(
                f"unknown task_output_value_kind: {declared_kind!r}"
            ) from exc
        if (
            explicit_kind is not TaskOutputValueKind.TASK_PAYLOAD
            and not isinstance(value, dict)
        ):
            raise TaskOutputProtocolError(
                f"{explicit_kind.value} requires an object result"
            )
        if isinstance(value, dict):
            status = value.get("status")
            if explicit_kind is TaskOutputValueKind.STEP_RESULT_ENVELOPE and status not in {
                StepResultStatus.SUCCEEDED.value,
                StepResultStatus.PARTIAL.value,
            }:
                raise TaskOutputProtocolError(
                    "step_result_envelope requires succeeded or partial status"
                )
            if (
                explicit_kind is TaskOutputValueKind.STEP_RESULT_FAILURE
                and status != StepResultStatus.FAILED.value
            ):
                raise TaskOutputProtocolError(
                    "step_result_failure requires failed status"
                )
        return explicit_kind


class OutputContractFactory:
    """Classify a materialized schema into the one runtime contract kind."""

    @classmethod
    def from_schema(cls, schema: Any) -> OutputContract:
        if not isinstance(schema, dict):
            return OutputContract(OutputContractKind.NONE)

        if is_task_output_contract_schema(schema):
            return OutputContract(
                OutputContractKind.TASK_ENVELOPE,
                schema=schema,
                payload_schema=task_output_payload_schema(schema),
            )

        source = schema.get(TASK_OUTPUT_CONTRACT_SOURCE_KEY)
        if source == PLAN_OUTPUT_CONTRACT_SOURCE:
            payload = deepcopy(schema)
            payload.pop(TASK_OUTPUT_CONTRACT_SOURCE_KEY, None)
            return OutputContract(
                OutputContractKind.PLAN_CANONICAL,
                schema=schema,
                payload_schema=payload,
            )
        if source == PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE:
            payload = deepcopy(schema)
            payload.pop(TASK_OUTPUT_CONTRACT_SOURCE_KEY, None)
            return OutputContract(
                OutputContractKind.PLAN_CUSTOM,
                schema=schema,
                payload_schema=payload,
            )

        if is_step_result_envelope_schema(schema):
            return OutputContract(
                OutputContractKind.LEGACY_ENVELOPE,
                schema=schema,
            )

        return OutputContract(OutputContractKind.UNMARKED_SCHEMA, schema=schema)


def output_contract_for_schema(schema: Any) -> OutputContract:
    """Return the canonical runtime classification for ``schema``."""

    return OutputContractFactory.from_schema(schema)


def known_top_level_keys(
    schema: Any,
    *,
    require_required: bool = False,
) -> set[str] | None:
    """Return fields guaranteed by a simple object schema, else ``None``.

    Composition, open-world schemas, missing ``type``, and valid-but-unresolved
    shapes are intentionally unknown.  The linker uses ``None`` to fail closed
    instead of promising a field the runtime value may not contain.  A known
    non-object or a closed empty object returns an empty set.
    """

    try:
        validate_schema_contract(schema)
    except SchemaContractError:
        return None
    if not isinstance(schema, dict):
        return None

    # The linker intentionally proves only simple object contracts.  A
    # composed schema can remove or replace a property on another branch even
    # when a top-level ``properties`` block is present, so it is unresolved
    # until a full schema evaluator is introduced.
    if _COMPOSED_SCHEMA_KEYS.intersection(schema):
        return None

    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        if schema_type != "object":
            return set()
    elif isinstance(schema_type, list):
        # A union that includes non-object values does not guarantee fields.
        if schema_type != ["object"] and set(schema_type) != {"object"}:
            return set()
    else:
        return None

    properties = schema.get("properties")
    if isinstance(properties, dict):
        keys = set(str(key) for key in properties)
        if require_required:
            required = schema.get("required")
            if not isinstance(required, list):
                # An optional field is not a stable contract for a consumer
                # reference: a valid producer result may omit it.
                return set()
            keys &= {str(key) for key in required}
        return keys
    if schema.get("additionalProperties") is False:
        return set()
    return None


def known_schema_path(
    schema: Any,
    path: str | None,
    *,
    require_required: bool = False,
) -> bool | None:
    """Prove that a dotted/indexed result path is always available.

    ``known_top_level_keys`` is sufficient for ``result.files`` but not for
    ``result.files[0].fs_path``.  Returning a tri-state keeps the linker
    fail-closed: ``True`` is a proof, ``False`` is a known missing/optional
    segment, and ``None`` means the schema is too dynamic to prove locally.
    """
    if not isinstance(path, str) or not path:
        return True
    try:
        validate_schema_contract(schema)
    except SchemaContractError:
        return None
    if _SCHEMA_PATH_RE.fullmatch(path) is None:
        return None
    tokens: list[tuple[str, str | int]] = []
    for match in _SCHEMA_PATH_TOKEN_RE.finditer(path):
        raw = match.group(0)
        if raw.startswith("["):
            tokens.append(("index", int(match.group(1))))
        else:
            tokens.append(("key", raw))
    cursor: Any = schema
    for token_kind, token in tokens:
        if not isinstance(cursor, dict) or _COMPOSED_SCHEMA_KEYS.intersection(cursor):
            return None
        schema_type = cursor.get("type")
        if schema_type == "object":
            if token_kind != "key":
                return False
            properties = cursor.get("properties")
            if not isinstance(properties, dict):
                return False if cursor.get("additionalProperties") is False else None
            if token not in properties:
                return False if cursor.get("additionalProperties") is False else None
            if require_required and token not in set(cursor.get("required") or []):
                return False
            cursor = properties[token]
            continue
        if schema_type == "array":
            if token_kind != "index":
                return False
            index = int(token)
            # An item schema describes values *if present*, not the existence
            # of an item.  Without minItems, ``files[0]`` is valid against an
            # empty array, so accepting the path would recreate the runtime
            # mismatch that this linker is meant to prevent.
            min_items = cursor.get("minItems")
            if not isinstance(min_items, int) or isinstance(min_items, bool) or min_items <= index:
                return False
            prefix_items = cursor.get("prefixItems")
            if isinstance(prefix_items, list) and index < len(prefix_items):
                # Tuple validation takes precedence over ``items`` for the
                # corresponding indexes.  Looking at ``items`` first can
                # falsely prove e.g. ``files[0].name`` when prefixItems[0] is
                # a scalar but the fallback items schema is an object.
                cursor = prefix_items[index]
                continue
            items = cursor.get("items")
            if isinstance(items, dict):
                cursor = items
                continue
            return None
        # A scalar (or a union that includes a scalar) cannot provide a
        # property/index.  Treat it as a known dangling path rather than
        # allowing runtime refs.py to degrade it to a JSON string.
        return False
    return True


def task_expected_output_declares_schema(value: Any) -> bool:
    """Whether Task.expected_output declares runtime schema vocabulary."""

    return isinstance(value, dict) and bool(
        _TASK_OUTPUT_SCHEMA_DECLARATION_KEYS & value.keys()
    )


def task_expected_output_json_schema(value: Any) -> dict[str, Any] | None:
    """Return the enforceable JSON Schema portion of Task.expected_output.

    Metadata-only expected-output payloads (for example ``deliverables`` used
    by artifact acceptance) intentionally return ``None``.  A malformed schema
    also returns ``None``; planning/supervision can then report the contract gap
    without turning every worker attempt into a validator crash.
    """

    if not task_expected_output_declares_schema(value):
        return None

    schema = deepcopy(value)
    properties = schema.get("properties")
    property_names = set(properties) if isinstance(properties, dict) else set()
    for key in _TASK_OUTPUT_METADATA_KEYS:
        if key not in property_names:
            schema.pop(key, None)

    try:
        validate_schema_contract(schema)
        if not SchemaContractValidatorFactory.constrains_values(schema):
            return None
    except SchemaContractError:
        return None
    return schema


def task_failure_control_schema() -> dict[str, Any]:
    """Return the strict failure branch for a Task terminal result."""

    return {
        "type": "object",
        "required": ["reason", "retryable"],
        "properties": {
            "reason": {"type": "string", "minLength": 1},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "retryable": {"type": "boolean"},
            "requires_human": {"type": "boolean"},
        },
        # Human-required failures cannot be repaired by an identical retry.
        "not": {
            "required": ["retryable", "requires_human"],
            "properties": {
                "retryable": {"const": True},
                "requires_human": {"const": True},
            },
        },
    }


def task_output_terminal_branch_schema(
    deliverable_member: str,
    *,
    success_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exclusive success/failure branch for a Task result.

    Both the model-facing ``submit_result`` tool and the persisted StepResult
    envelope use this factory so their required and forbidden members cannot
    drift. An omitted tool status intentionally follows the success branch;
    ``step_result_from_submit`` normalizes that omission to ``succeeded``.
    """

    success_branch: dict[str, Any] = {
        "required": [deliverable_member],
        "not": {"required": ["failure"]},
    }
    if success_properties:
        success_branch["properties"] = deepcopy(success_properties)
    return {
        "if": {
            "properties": {
                "status": {
                    "enum": [
                        StepResultStatus.SUCCEEDED.value,
                        StepResultStatus.PARTIAL.value,
                    ]
                }
            },
        },
        "then": success_branch,
        "else": {
            "required": ["failure"],
            "not": {"required": [deliverable_member]},
        },
    }


def task_output_envelope_schema(expected_output: Any) -> dict[str, Any] | None:
    """Compose a StepResult envelope whose ``outputs.data`` is task-shaped."""

    task_schema = task_expected_output_json_schema(expected_output)
    if task_schema is None:
        return None
    # A payload-local ``$id`` would create a nested resource whose ``#`` base
    # no longer points at the envelope path.  The task contract is embedded
    # under the fixed envelope root, so discard that non-semantic identity
    # before rebasing local refs.
    if "$id" in task_schema and any(iter_schema_refs(task_schema)):
        task_schema.pop("$id", None)

    schema = deepcopy(step_result_envelope_schema())
    schema[TASK_OUTPUT_CONTRACT_SOURCE_KEY] = TASK_OUTPUT_CONTRACT_SOURCE
    schema["properties"]["failure"] = task_failure_control_schema()
    outputs_schema = schema["properties"]["outputs"]
    # A task schema is authored as a standalone JSON Schema, where ``#`` and
    # ``#/$defs/...`` resolve against its own root. Once nested under
    # ``outputs.data`` those pointers would otherwise resolve against the
    # envelope root and crash jsonschema's resolver at lease completion.
    outputs_schema["properties"]["data"] = _prefix_local_schema_refs(
        task_schema,
        "#/properties/outputs/properties/data",
    )
    schema.setdefault("allOf", []).append(
        task_output_terminal_branch_schema(
            "outputs",
            success_properties={"outputs": {"required": ["data"]}},
        )
    )
    return schema


def _prefix_local_schema_refs(schema: Any, prefix: str) -> Any:
    """Retarget standalone payload refs after embedding under an envelope."""

    if isinstance(schema, dict):
        output: dict[str, Any] = {}
        for key, value in schema.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(value, str):
                if value == "#":
                    output[key] = prefix
                elif value.startswith("#/"):
                    output[key] = prefix + value[1:]
                else:
                    output[key] = value
            else:
                output[key] = _prefix_local_schema_refs(value, prefix)
        return output
    if isinstance(schema, list):
        return [_prefix_local_schema_refs(item, prefix) for item in schema]
    return schema


def _unprefix_local_schema_refs(schema: Any, prefix: str) -> Any:
    """Restore a nested task schema to a standalone tool-schema root."""

    if isinstance(schema, dict):
        output: dict[str, Any] = {}
        for key, value in schema.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(value, str):
                if value == prefix:
                    output[key] = "#"
                elif value.startswith(prefix + "/"):
                    output[key] = "#" + value[len(prefix):]
                else:
                    output[key] = value
            else:
                output[key] = _unprefix_local_schema_refs(value, prefix)
        return output
    if isinstance(schema, list):
        return [_unprefix_local_schema_refs(item, prefix) for item in schema]
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

    return output_contract_for_schema(schema).is_hard


def task_output_payload_schema(schema: Any) -> dict[str, Any] | None:
    """Extract the original task payload schema from a composed envelope."""

    if not is_task_output_contract_schema(schema):
        return None
    try:
        data_schema = schema["properties"]["outputs"]["properties"]["data"]
    except (KeyError, TypeError):
        return None
    if not isinstance(data_schema, dict):
        return None
    return _unprefix_local_schema_refs(
        deepcopy(data_schema),
        "#/properties/outputs/properties/data",
    )


def declared_output_payload_schema(schema: Any) -> dict[str, Any] | None:
    """Return the model-facing payload contract for any hard output schema."""

    return output_contract_for_schema(schema).payload_schema


def is_concrete_output_contract_schema(schema: Any) -> bool:
    """Whether a declared schema constrains at least one JSON value shape.

    ``{}`` is valid JSON Schema syntax (it means "anything"), but it is not a
    useful producer contract: the linker cannot prove fields and the worker
    cannot distinguish a malformed payload. Metadata-only vocabulary is also
    excluded by this predicate; Task.expected_output handles that separately.
    """
    contract = output_contract_for_schema(schema)
    payload = (
        contract.payload_schema
        if contract.payload_schema is not None
        else contract.schema
    )
    if not isinstance(payload, dict):
        return False
    try:
        return SchemaContractValidatorFactory.constrains_values(payload)
    except SchemaContractError:
        return False


def task_output_payload(result: Any, schema: Any) -> Any:
    """Return outputs.data from a task-contracted StepResult, if present."""

    if not is_task_output_contract_schema(schema) or not isinstance(result, dict):
        return None
    outputs = result.get("outputs")
    return outputs.get("data") if isinstance(outputs, dict) and "data" in outputs else None


def terminal_agent_step_key(steps: Iterable[Any]) -> str | None:
    """Return the unique terminal llm/subagent step, or ``None`` if ambiguous."""

    step_list = list(steps)
    def step_key(step: Any) -> str:
        # PlanStep uses ``key`` while hydrated ExecutionStep rows use
        # ``step_key``.  The terminal binding is a cross-layer contract, so
        # the factory must classify both representations identically.
        return str(
            getattr(step, "key", None)
            or getattr(step, "step_key", None)
            or ""
        )

    depended_on = {
        str(dep)
        for step in step_list
        for dep in (getattr(step, "depends_on", None) or [])
    }
    candidates = [
        step
        for step in step_list
        if step_key(step) not in depended_on
        and _normalize_step_kind(getattr(step, "kind", "")) in {"llm", "subagent"}
    ]
    if len(candidates) != 1:
        return None
    return step_key(candidates[0]) or None
